"""
Seeff Properties — Cape Town listing scraper.

Scrapes Cape Town residential for-sale and to-rent listings from seeff.com.
Uses the Cape Town area search pages with the confirmed advanced_search UUID
for sale listings, and the direct Cape Town to-rent URL for rentals.

POPIA: no personal info stored; all description text is sanitised.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import AsyncGenerator
from urllib.parse import urlparse

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

logger = logging.getLogger('homelens.seeff')

# ═══════════════════════════════════════════════════════════════════════════
# SITE SELECTORS — listing status + monthly levy/rates.
# The only place to edit when this site's markup changes. Confirmed against
# saved pages in tests/fixtures/ (re-capture: scripts/capture_fixtures.py).
# ═══════════════════════════════════════════════════════════════════════════
SELECTORS = {
    # Search-result card: <div class="card-badges"><div class="card-badge badge-under-offer">Under Offer</div>…
    # (other badges — "New", "Reduced" — are ignored).
    'status_badge': '.card-badges .card-badge',
    'status_map':   {},
    # Listing page, one row per figure:
    #   <div class="listing-details-info-detail">
    #     <span class="info-detail-label">Monthly Levy</span>
    #     <span class="info-detail-value">R7,233.50</span></div>
    # A "Total" row and a "Monthly Repayment" (bond) row sit alongside —
    # neither is a levy/rates label, so both are skipped.
    'cost_rows':    '.listing-details-info-detail',
    'cost_label':   '.info-detail-label',
    'cost_value':   '.info-detail-value',
    'description':  '.overview-content-intro',
}


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL       = 'https://www.seeff.com'
USER_AGENT     = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)

SALE_BASE_PATH = BASE_URL + '/results/residential/for-sale/cape-town/'
RENT_BASE_PATH = BASE_URL + '/results/residential/to-rent/cape-town/'

# advanced_search UUID that scopes the sale search to Cape Town (not used for other cities)
SALE_SEARCH_ID = 'e2c1a2f7-a5b9-5184-a318-7964bc399818'

# sale ≈ 310 listings / 16 per page ≈ 20 pages  (max 25 with margin)
# rent ≈ 441 listings / 16 per page ≈ 28 pages  (max 32 with margin)
MAX_PAGES: dict[str, int] = {
    'sale': 25,
    'rent': 32,
}

SCROLL_STEPS   = 12    # increments to trigger lazy-loaded cards
SCROLL_PAUSE   = 0.30  # seconds between scroll steps
PAGE_LOAD_WAIT = 3.0   # seconds after networkidle before scraping
RETRY_WAIT     = 15.0  # seconds to wait before retrying an empty page


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _page_url(base: str, page: int, search_id: str | None = None) -> str:
    """Build the URL for a given page number.

    Sale page 1:  base?advanced_search=UUID
    Sale page N:  base?page=N&advanced_search=UUID
    Rent page 1:  base  (no params needed)
    Rent page N:  base?page=N
    """
    if page == 1:
        return (base + f'?advanced_search={search_id}') if search_id else base
    if search_id:
        return f'{base}?page={page}&advanced_search={search_id}'
    return f'{base}?page={page}'


def _parse_beds(text: str) -> int | None:
    m = re.search(r'(\d+)\s*bed', text, re.I)
    return int(m.group(1)) if m else None


def _parse_baths(text: str) -> int | None:
    m = re.search(r'(\d+)\s*bath', text, re.I)
    return int(m.group(1)) if m else None


def _parse_size(text: str) -> float | None:
    """
    Extract floor/building size from strings like '190m²', '340 m²', '115m2'.
    Returns None for ERF/land/plot area labels — those are plot sizes,
    not floor sizes, and must never be used for price-per-m² calculations.
    """
    text_lc = text.lower()
    if any(kw in text_lc for kw in ('erf', 'land area', 'stand size', 'plot size', 'lot size')):
        return None
    m = re.search(r'([\d\s,]+\.?\d*)\s*m', text)
    if m:
        raw = m.group(1).replace(' ', '').replace(',', '')
        try:
            val = float(raw)
            return val if 10 <= val <= 50_000 else None
        except ValueError:
            return None
    return None


def _extract_suburb_from_url(url: str) -> str | None:
    """
    Extract suburb slug from a Seeff listing URL path.

    URL format:
      /results/residential/for-sale/cape-town/SUBURB/type/id/
      /results/residential/to-rent/cape-town/SUBURB/type/id/

    Example:
      /results/residential/for-sale/cape-town/kenilworth-upper/house/3176959/
      → 'kenilworth-upper'

    The slug is already hyphenated so it can be used directly as
    suburb_normalised (or passed through normalise_suburb for SUBURB_MAP lookup).
    """
    path  = urlparse(url).path.rstrip('/')
    parts = [p for p in path.split('/') if p]
    # parts: ['results', 'residential', 'for-sale', 'cape-town', SUBURB, type, id]
    #          0            1               2            3           4      5    6
    ct_idx = city_index(parts)
    if ct_idx is not None and len(parts) > ct_idx + 1:
        return parts[ct_idx + 1]   # e.g. 'kenilworth-upper'
    return None


# ---------------------------------------------------------------------------
# Page-level extraction
# ---------------------------------------------------------------------------

async def _scroll_to_load_all(page: Page) -> None:
    """Scroll the page incrementally to trigger lazy-loaded listing cards."""
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
    context,
    url: str,
    lt_name: str,
    page_num: int,
) -> tuple[int, list[dict]]:
    """
    Load a single search results page and return (raw_card_count, listing_dicts).

    raw_card_count — total div.seeff-listing-card elements found.
    listing_dicts  — parsed listings from this page.
    """
    page = await context.new_page()
    try:
        logger.info('  [%s] Scraping page %d → %s', lt_name, page_num, url)
        await page.goto(url, wait_until='networkidle', timeout=60_000)
        await asyncio.sleep(PAGE_LOAD_WAIT)
        await _scroll_to_load_all(page)

        raw_cards = await page.query_selector_all('div.seeff-listing-card')
        raw_count = len(raw_cards)

        listings = await _extract_cards(page, lt_name)
        logger.info(
            '  [%s] Page %d: %d total cards, %d listings extracted',
            lt_name, page_num, raw_count, len(listings),
        )
        return raw_count, listings

    finally:
        try:
            await page.close()
        except Exception:
            pass


async def _extract_cards(page: Page, listing_type: str) -> list[dict]:
    """Extract all listing dicts from div.seeff-listing-card elements."""
    cards = await page.query_selector_all('div.seeff-listing-card')
    if not cards:
        return []

    results: list[dict] = []

    for card in cards:
        try:
            # ── Listing ID ──────────────────────────────────────────────────
            data_id = (await card.get_attribute('data-id') or '').strip()
            if not data_id:
                continue
            listing_id = f'seeff_{data_id}'

            # ── URL ─────────────────────────────────────────────────────────
            # Prefer the first in-card link that points to a detail page.
            # The image/swiper anchor in .card-header carries the full path.
            link_el = await card.query_selector('a[href*="/results/residential/"]')
            if not link_el:
                link_el = await card.query_selector('.card-header a')
            if not link_el:
                continue

            href = (await link_el.get_attribute('href') or '').strip()
            if not href:
                continue
            full_url = BASE_URL + href if href.startswith('/') else href

            # ── Title ───────────────────────────────────────────────────────
            heading_el = await card.query_selector('.card-heading')
            title = (await heading_el.inner_text()).strip() if heading_el else ''
            if not title:
                # Derive from URL slug as last-resort fallback
                slug  = href.rstrip('/').split('/')[-2] if '/' in href else ''
                title = slug.replace('-', ' ').title()

            # ── Suburb (from URL path, already hyphenated slug) ──────────────
            suburb_slug       = _extract_suburb_from_url(full_url)
            suburb_normalised = normalise_suburb(suburb_slug) if suburb_slug else None

            # ── Price ────────────────────────────────────────────────────────
            price_el    = await card.query_selector('.card-price')
            price_text  = (await price_el.inner_text()).strip() if price_el else ''
            price, price_status = parse_price(price_text)
            if not price_el:
                price_status = 'extraction_failed'

            # ── Beds / Baths / Size ──────────────────────────────────────────
            # Each stat lives in a .card-stat element:
            #   "4 Beds"  |  "2 Baths"  |  "190m²"
            stat_els  = await card.query_selector_all('.card-stat')
            bedrooms  = None
            bathrooms = None
            size_m2   = None

            for stat_el in stat_els:
                text    = (await stat_el.inner_text()).strip()
                text_lc = text.lower()
                if 'bed' in text_lc and bedrooms is None:
                    bedrooms = _parse_beds(text)
                elif 'bath' in text_lc and bathrooms is None:
                    bathrooms = _parse_baths(text)
                elif 'm' in text_lc and size_m2 is None:
                    candidate = _parse_size(text)
                    if candidate:
                        size_m2 = candidate

            # ── Image — card thumbnail ───────────────────────────────────────
            raw_image = await card.evaluate('''card => {
                const selectors = [
                    ".card-image img", ".card-header img", ".card-media img",
                    ".card-photo img", "img[class*=\\"card\\"]",
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
                'source':            'seeff',
                'listing_type':      listing_type,
                'title':             clean_personal_info(title[:250]),
                'price':             price,
                'price_status':      price_status,
                'bedrooms':          bedrooms,
                'bathrooms':         bathrooms,
                'size_m2':           size_m2,
                'suburb_normalised': suburb_normalised,
                'description':       None,
                'price_per_m2':      compute_price_per_m2(price, size_m2),
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

async def _run_seeff_city(city: dict) -> AsyncGenerator[dict, None]:
    """
    Async generator — yields one listing dict at a time.

    Iterates through Cape Town area pages for both sale and rent.
    Uses ?page=N pagination (with &advanced_search=UUID for sale).
    On a 0-card page, waits RETRY_WAIT seconds and retries once before
    treating the page as the end of results.
    Deduplicates globally to avoid yielding the same listing twice.
    """
    seen_ids: set[str] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage'],
        )
        enricher = CostEnricher('seeff', browser, SELECTORS)
        await enricher.start()
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={'width': 1280, 'height': 900},
            locale='en-ZA',
            timezone_id='Africa/Johannesburg',
        )

        lt_configs = [
            ('sale', city_url(SALE_BASE_PATH, city), SALE_SEARCH_ID if city['slug'] == 'cape-town' else None),
            ('rent', city_url(RENT_BASE_PATH, city), None),
        ]

        for lt_name, base_path, search_id in lt_configs:
            max_pages = MAX_PAGES[lt_name]
            logger.info('── Starting %s scrape (max %d pages) ──', lt_name, max_pages)

            for page_num in range(1, max_pages + 1):
                url = _page_url(base_path, page_num, search_id)

                # ── Load page ─────────────────────────────────────────────
                try:
                    raw_count, cards = await _fetch_page_cards(
                        context, url, lt_name, page_num
                    )
                except Exception as e:
                    logger.warning(
                        '[%s] page %d failed to load: %s — stopping this type',
                        lt_name, page_num, e,
                    )
                    break

                # ── Retry on empty page (possible soft block) ──────────────
                if raw_count == 0:
                    logger.info(
                        '[%s] page %d returned 0 cards — waiting %.0fs then retrying...',
                        lt_name, page_num, RETRY_WAIT,
                    )
                    await asyncio.sleep(RETRY_WAIT)
                    try:
                        raw_count, cards = await _fetch_page_cards(
                            context, url, lt_name, page_num
                        )
                    except Exception as e:
                        logger.warning(
                            '[%s] page %d retry failed: %s — stopping',
                            lt_name, page_num, e,
                        )
                        break

                    if raw_count == 0:
                        logger.info(
                            '[%s] Stopping at page %d — no cards after retry (end of results)',
                            lt_name, page_num,
                        )
                        break

                # ── Filter to unseen listings ──────────────────────────────
                new_cards = [c for c in cards if c['listing_id'] not in seen_ids]

                logger.info(
                    '[%s] page %3d: %2d raw cards | %2d extracted | %2d new  (total seen: %d)',
                    lt_name, page_num,
                    raw_count, len(cards), len(new_cards),
                    len(seen_ids) + len(new_cards),
                )

                if not new_cards and raw_count > 0:
                    # All listings on this page already seen — pagination cycled
                    logger.info(
                        '[%s] Stopping at page %d — all listings already seen (pagination cycle)',
                        lt_name, page_num,
                    )
                    break

                for card in new_cards:
                    seen_ids.add(card['listing_id'])
                    await enricher.enrich(card)
                    yield card

        await enricher.close()
        await browser.close()


async def run_seeff_scraper() -> AsyncGenerator[dict, None]:
    """Scrapes every city in scraper.cities.CITIES, tagging each listing with its city."""
    for city in CITIES:
        logger.info('══ seeff — %s ══', city['name'])
        async for listing in _run_seeff_city(city):
            listing['city'] = city['slug']
            yield listing
