"""
Lew Geffen Sotheby's International Realty — Cape Town listing scraper.

Scrapes Cape Town residential for-sale and to-let listings from sothebysrealty.co.za.
A single search URL (?q=Cape+Town) returns all listing types together; listing_type
(sale/rent) is inferred from the href path.

Pagination: ?page=N&q=Cape+Town (page 1 has no page param).  ~16 listings per page.
Suburb is derived from the URL path (5th segment after /results/).

POPIA: no personal info stored; all description text is sanitised.
"""
from __future__ import annotations

import asyncio
import logging
from scraper.utils import goto_with_retry
from typing import AsyncGenerator

from playwright.async_api import async_playwright, Page

from scraper.utils import (
    clean_personal_info,
    normalise_price,
    parse_price,
    normalise_suburb,
    compute_price_per_m2,
    validate_image_url,
)
from scraper.cities import CITIES, city_index, city_url
from scraper.enrich import CostEnricher, read_card_status

logger = logging.getLogger('homelens.sothebys')

# ═══════════════════════════════════════════════════════════════════════════
# SITE SELECTORS — listing status + monthly levy/rates.
# The only place to edit when this site's markup changes. Confirmed against
# saved pages in tests/fixtures/ (re-capture: scripts/capture_fixtures.py).
# ═══════════════════════════════════════════════════════════════════════════
SELECTORS = {
    # Search-result card: <div class="card-badges"><div class="card-badge">Under offer</div>…
    # (other badges — "New", "Reduced" — are ignored).
    'status_badge': '.card-badges .card-badge',
    'status_map':   {},
    # Listing page, one row per figure:
    #   <div class="listing-details-info-detail">
    #     <span class="info-detail-label">Monthly Levy</span>
    #     <span class="info-detail-value">R7,233.50</span></div>
    # A "Total" row sits alongside — it is skipped by label, never used.
    'cost_rows':    '.listing-details-info-detail',
    'cost_label':   '.info-detail-label',
    'cost_value':   '.info-detail-value',
    'description':  '.overview-content-intro',
}


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL   = 'https://www.sothebysrealty.co.za'
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)

# Separate URLs for sale and rent — more reliable than the combined ?q=Cape%20Town
# search which mixes types and requires inferring listing_type from the href.
SALE_BASE = BASE_URL + '/results/residential/for-sale/cape-town/'
RENT_BASE = BASE_URL + '/results/residential/to-let/cape-town/'

# sale ≈ 537 / 16 per page ≈ 34 pages  (max 40 with margin)
# rent ≈ 268 / 16 per page ≈ 17 pages  (max 22 with margin)
MAX_PAGES: dict[str, int] = {
    'sale': 40,
    'rent': 22,
}

SCROLL_STEPS   = 8     # increments to trigger lazy-loaded cards
SCROLL_PAUSE   = 0.35  # seconds between scroll steps
PAGE_LOAD_WAIT = 4.5   # seconds after networkidle before scraping
RETRY_WAIT     = 15.0  # seconds to wait before retrying an empty page


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _page_url(base: str, page_num: int) -> str:
    """Page 1 has no page param; subsequent pages add ?page=N."""
    if page_num == 1:
        return base
    return f'{base}?page={page_num}'


def _listing_id(data_id: str) -> str:
    return f'sothebys_{data_id}'


def _suburb_from_href(href: str) -> str | None:
    """
    Extract suburb from URLs like:
      /results/residential/for-sale/cape-town/DE-WATERKANT/apartment/ID/slug/
    The suburb slug sits at index 4 of the path segments.
    """
    parts = [p for p in href.split('/') if p]
    # ['results', 'residential', 'for-sale', 'cape-town', 'SUBURB', ...]
    ci = city_index(parts)
    if ci is not None and len(parts) > ci + 1:
        raw = parts[ci + 1].replace('-', ' ').title()
        return raw if raw.lower() not in ('cape town', 'johannesburg', '') else None
    return None


# ---------------------------------------------------------------------------
# Page-level helpers
# ---------------------------------------------------------------------------

async def _scroll_to_load_all(page: Page) -> None:
    """Scroll incrementally to trigger lazy-loaded listing cards."""
    try:
        scroll_height = await page.evaluate('document.body.scrollHeight')
        step = max(400, scroll_height // SCROLL_STEPS)
        for i in range(1, SCROLL_STEPS + 1):
            await page.evaluate(f'window.scrollTo(0, {step * i})')
            await asyncio.sleep(SCROLL_PAUSE)
        await asyncio.sleep(0.5)
    except Exception as e:
        logger.debug('Scroll error (non-fatal): %s', e)


async def _fetch_page_cards(
    browser,
    url: str,
    lt_name: str,
    page_num: int,
) -> tuple[int, list[dict]]:
    """
    Load one search-results page and return (raw_card_count, listing_dicts).

    A fresh browser context is created for every request so that cookies /
    session state from previous pages cannot interfere with card rendering.
    """
    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={'width': 1280, 'height': 900},
        locale='en-ZA',
        timezone_id='Africa/Johannesburg',
    )
    page = await context.new_page()
    try:
        logger.info('  [%s] Scraping page %d → %s', lt_name, page_num, url)
        await goto_with_retry(page, url, wait_until='networkidle', timeout=60_000)
        await asyncio.sleep(PAGE_LOAD_WAIT)
        await _scroll_to_load_all(page)

        raw_cards = await page.query_selector_all('a.property-card-sm')
        raw_count = len(raw_cards)

        listings = await _extract_cards(page, lt_name)
        logger.info(
            '  [%s] Page %d: %d total cards, %d listings extracted',
            lt_name, page_num, raw_count, len(listings),
        )
        return raw_count, listings

    finally:
        try:
            await context.close()
        except Exception:
            pass


async def _extract_cards(page: Page, listing_type: str) -> list[dict]:
    """
    Extract listing dicts from a.property-card-sm elements.

    Card structure (Sotheby's):
      a.property-card-sm     → card IS the anchor; data-id + href live on it
      p.card-price           → "R3,150,000" or "R18,500 per month"
      p.card-description     → "1 Bedroom Apartment For Sale in De Waterkant"
      .card-stats div p      → stat texts: "1 Bed", "1 Bath", "49m²"
    """
    cards = await page.query_selector_all('a.property-card-sm')
    if not cards:
        return []

    results: list[dict] = []

    for card in cards:
        try:
            # ── ID + URL ──────────────────────────────────────────────────────
            data_id = (await card.get_attribute('data-id') or '').strip()
            if not data_id:
                continue
            listing_id = _listing_id(data_id)

            href = (await card.get_attribute('href') or '').strip()
            if not href:
                continue
            full_url = href if href.startswith('http') else BASE_URL + href

            # ── Suburb from URL ───────────────────────────────────────────────
            raw_suburb        = _suburb_from_href(href)
            suburb_normalised = normalise_suburb(raw_suburb) if raw_suburb else None

            # ── Price ─────────────────────────────────────────────────────────
            price_el    = await card.query_selector('p.card-price')
            price_text  = (await price_el.inner_text()).strip() if price_el else ''
            price, price_status = parse_price(price_text)
            if not price_el:
                price_status = 'extraction_failed'

            # ── Title ─────────────────────────────────────────────────────────
            desc_el = await card.query_selector('p.card-description')
            title   = (await desc_el.inner_text()).strip() if desc_el else ''

            # ── Beds / Baths / Size ───────────────────────────────────────────
            bedrooms  = None
            bathrooms = None
            size_m2   = None

            stat_els = await card.query_selector_all('.card-stats div p')
            for stat_el in stat_els:
                text = (await stat_el.inner_text()).strip()
                if 'Bed' in text:
                    try:
                        bedrooms = int(text.split()[0])
                    except (ValueError, TypeError):
                        pass
                elif 'Bath' in text:
                    try:
                        bathrooms = int(text.split()[0])
                    except (ValueError, TypeError):
                        pass
                elif 'm²' in text:
                    try:
                        size_m2 = float(
                            text.replace('m²', '').replace(',', '').strip()
                        )
                    except (ValueError, TypeError):
                        pass

            price_per_m2 = compute_price_per_m2(price, size_m2)

            # ── Image — card thumbnail (card IS the anchor element) ──────────
            raw_image = await card.evaluate('''card => {
                const selectors = [
                    ".card-image img", ".card-media img", ".card-photo img",
                    ".card-header img", "img[class*=\\"card\\"]",
                    "img[class*=\\"property\\"]", "img[loading=\\"lazy\\"]", "img"
                ];
                for (const sel of selectors) {
                    const img = card.querySelector(sel);
                    if (!img) continue;
                    const src = img.src || img.dataset.src || img.getAttribute("data-src");
                    if (src && src.startsWith("http")) return src;
                }
                return null;
            }''')
            image_url = validate_image_url(raw_image)

            status, status_source = await read_card_status(card, SELECTORS)

            results.append({
                'listing_id':        listing_id,
                'listing_url':       full_url,
                'source':            'sothebys',
                'listing_type':      listing_type,  # passed in — sale or rent
                'title':             clean_personal_info(title[:250]),
                'price':             price,
                'price_status':      price_status,
                'bedrooms':          bedrooms,
                'bathrooms':         bathrooms,
                'size_m2':           size_m2,
                'suburb_normalised': suburb_normalised,
                'description':       None,
                'price_per_m2':      price_per_m2,
                'image_url':         image_url,
                'is_active':         True,
                'listing_status':    status,
                'status_source':     status_source,
            })

        except Exception as e:
            logger.debug('Card parse error: %s', e)
            continue

    return results


# ---------------------------------------------------------------------------
# Main scraper generator
# ---------------------------------------------------------------------------

async def _run_sothebys_city(city: dict) -> AsyncGenerator[dict, None]:
    """
    Async generator — yields one listing dict at a time.

    Iterates through all Cape Town search result pages (sale and rent are
    combined in the ?q=Cape+Town search).
    Uses ?page=N pagination (page 1 has no param).
    On a 0-card page, waits RETRY_WAIT seconds and retries once before stopping.
    Deduplicates globally to avoid yielding the same listing twice.
    """
    seen_ids: set[str] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage'],
        )
        enricher = CostEnricher('sothebys', browser, SELECTORS)
        await enricher.start()

        lt_configs = [
            ('sale', city_url(SALE_BASE, city), MAX_PAGES['sale']),
            ('rent', city_url(RENT_BASE, city), MAX_PAGES['rent']),
        ]

        for lt_name, base_url, max_pages in lt_configs:
            logger.info(
                "── Starting Sotheby's %s scrape (max %d pages) ──",
                lt_name, max_pages,
            )

            for page_num in range(1, max_pages + 1):
                url = _page_url(base_url, page_num)

                # ── Load page (fresh context per request) ──────────────────
                try:
                    raw_count, cards = await _fetch_page_cards(
                        browser, url, lt_name, page_num
                    )
                except Exception as e:
                    logger.warning(
                        '[%s] Page %d failed: %s — stopping', lt_name, page_num, e
                    )
                    break

                # ── Retry on empty page ────────────────────────────────────
                if raw_count == 0:
                    logger.info(
                        '[%s] Page %d returned 0 cards — waiting %.0fs then retrying...',
                        lt_name, page_num, RETRY_WAIT,
                    )
                    await asyncio.sleep(RETRY_WAIT)
                    try:
                        raw_count, cards = await _fetch_page_cards(
                            browser, url, lt_name, page_num
                        )
                    except Exception as e:
                        logger.warning(
                            '[%s] Page %d retry failed: %s — stopping', lt_name, page_num, e
                        )
                        break

                    if raw_count == 0:
                        logger.info(
                            '[%s] Stopping at page %d — no cards after retry',
                            lt_name, page_num,
                        )
                        break

                # ── Filter to unseen listings ──────────────────────────────
                new_cards = [c for c in cards if c['listing_id'] not in seen_ids]

                logger.info(
                    '[%s] page %3d: %2d raw | %2d extracted | %2d new  (total: %d)',
                    lt_name, page_num,
                    raw_count, len(cards), len(new_cards),
                    len(seen_ids) + len(new_cards),
                )

                if not new_cards and raw_count > 0:
                    logger.info(
                        '[%s] Stopping at page %d — pagination cycle detected',
                        lt_name, page_num,
                    )
                    break

                for card in new_cards:
                    seen_ids.add(card['listing_id'])
                    await enricher.enrich(card)
                    yield card

        await enricher.close()
        await browser.close()


async def run_sothebys_scraper() -> AsyncGenerator[dict, None]:
    """Scrapes every city in scraper.cities.CITIES, tagging each listing with its city."""
    for city in CITIES:
        logger.info('══ sothebys — %s ══', city['name'])
        async for listing in _run_sothebys_city(city):
            listing['city'] = city['slug']
            yield listing
