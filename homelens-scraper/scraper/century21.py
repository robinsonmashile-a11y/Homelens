"""
Century 21 South Africa — Cape Town listing scraper.

Scrapes Cape Town residential for-sale and to-let listings from century21.co.za.
Uses the same underlying platform as Greeff/Sothebys/Tyson/Quay1:
  card selector: a.property-card-sm (card IS the anchor)
  image:         data-images JSON on the swiper element → first entry

URL structure:
  /results/residential/for-sale/cape-town/{suburb}/{type}/{id}/
  Suburb at path index 4.

Pagination: ?page=N  (~16 listings per page)
  Sale: ~32 listings → max 5 pages
  Rent: ~7 listings  → max 3 pages

Note: Century 21 has a small Cape Town inventory but covers
Atlantic Seaboard suburbs not well represented elsewhere.

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

logger = logging.getLogger('homelens.century21')

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

BASE_URL   = 'https://www.century21.co.za'
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)

SALE_BASE = BASE_URL + '/results/residential/for-sale/cape-town/'
RENT_BASE = BASE_URL + '/results/residential/to-let/cape-town/'

MAX_PAGES: dict[str, int] = {
    'sale': 5,
    'rent': 3,
}

SCROLL_STEPS   = 10
SCROLL_PAUSE   = 0.30
PAGE_LOAD_WAIT = 4.0
RETRY_WAIT     = 15.0

_SKIP_TYPES = ('commercial', 'industrial', 'retail', 'mixed-use', 'agricultural', 'vacant-land')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _page_url(base: str, page_num: int) -> str:
    if page_num == 1:
        return base
    return f'{base}?page={page_num}'


def _extract_suburb_from_url(url: str) -> str | None:
    path  = urlparse(url).path.rstrip('/')
    parts = [p for p in path.split('/') if p]
    ct_idx = city_index(parts)
    if ct_idx is not None and len(parts) > ct_idx + 1:
        suburb = parts[ct_idx + 1]
        if suburb in _SKIP_TYPES:
            return None
        return suburb
    return None


def _parse_beds(text: str) -> int | None:
    m = re.search(r'(\d+(?:\.\d+)?)\s*bed', text, re.I)
    if m:
        try:
            return int(float(m.group(1)))
        except ValueError:
            return None
    return None


def _parse_baths(text: str) -> int | None:
    m = re.search(r'(\d+(?:\.\d+)?)\s*bath', text, re.I)
    if m:
        try:
            return int(float(m.group(1)))
        except ValueError:
            return None
    return None


def _parse_size(text: str) -> float | None:
    text_lc = text.lower()
    if any(kw in text_lc for kw in ('erf', 'land area', 'stand size', 'plot size', 'lot size')):
        return None
    m = re.search(r'([\d\s,]+\.?\d*)\s*m', text)
    if m:
        raw = m.group(1).replace(' ', '').replace(',', '')
        try:
            val = float(raw)
            return val if 15 <= val <= 50_000 else None
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Page-level extraction
# ---------------------------------------------------------------------------

async def _scroll_to_load_all(page: Page) -> None:
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
    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={'width': 1280, 'height': 900},
        locale='en-ZA',
        timezone_id='Africa/Johannesburg',
    )
    page = await context.new_page()
    try:
        logger.info('  [%s] Scraping page %d → %s', lt_name, page_num, url)
        await page.goto(url, wait_until='networkidle', timeout=60_000)
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
    Extract all listing dicts from a.property-card-sm elements.

    Card structure (Century 21 — same platform as Greeff/Sothebys/Tyson/Quay1):
      a.property-card-sm     → card IS the anchor; data-id + href live on it
      .card-price            → "R3,200,000"
      .card-description      → "1 Bedroom Apartment For Sale in Cape Town City Centre"
      .card-stats div p      → "1 Bed", "1 Bath", "71m²"
      .swiper[data-images]   → JSON array [{src, srcset, alt}, ...]
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
            listing_id = f'century21_{data_id}'

            href = (await card.get_attribute('href') or '').strip()
            if not href:
                continue
            full_url = href if href.startswith('http') else BASE_URL + href

            if any(t in href.lower() for t in _SKIP_TYPES):
                continue

            # ── Suburb ────────────────────────────────────────────────────────
            suburb_slug       = _extract_suburb_from_url(full_url)
            suburb_normalised = normalise_suburb(suburb_slug) if suburb_slug else None

            # ── Price ─────────────────────────────────────────────────────────
            price_el    = await card.query_selector('.card-price')
            price_text  = (await price_el.inner_text()).strip() if price_el else ''
            price, price_status = parse_price(price_text)
            if not price_el:
                price_status = 'extraction_failed'

            # ── Title ─────────────────────────────────────────────────────────
            desc_el = await card.query_selector('.card-description')
            title   = (await desc_el.inner_text()).strip() if desc_el else ''

            # ── Beds / Baths / Size ───────────────────────────────────────────
            bedrooms  = None
            bathrooms = None
            size_m2   = None

            stat_els = await card.query_selector_all('.card-stats div p')
            for stat_el in stat_els:
                text = (await stat_el.inner_text()).strip()
                if 'Bed' in text and bedrooms is None:
                    bedrooms = _parse_beds(text)
                elif 'Bath' in text and bathrooms is None:
                    bathrooms = _parse_baths(text)
                elif 'm' in text.lower() and size_m2 is None:
                    candidate = _parse_size(text)
                    if candidate:
                        size_m2 = candidate

            # ── Image — data-images JSON → active slide → any img ─────────────
            raw_image = await card.evaluate('''card => {
                const swiper = card.querySelector(".swiper[data-images]");
                if (swiper) {
                    try {
                        const imgs = JSON.parse(swiper.getAttribute("data-images"));
                        if (imgs && imgs[0] && imgs[0].src && imgs[0].src.startsWith("http"))
                            return imgs[0].src;
                    } catch(e) {}
                }
                const active = card.querySelector(".swiper-slide-active img");
                if (active) {
                    const src = active.src || active.dataset.src || active.getAttribute("data-src");
                    if (src && src.startsWith("http")) return src;
                }
                const selectors = [
                    ".card-image img", ".card-media img", ".card-img img",
                    "img[loading=\\"lazy\\"]", "img"
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
                'source':            'century21',
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

async def _run_century21_city(city: dict) -> AsyncGenerator[dict, None]:
    """
    Async generator — yields one listing dict at a time.

    Iterates through Cape Town pages for sale and to-let.
    Uses ?page=N pagination (~16 listings per page).
    Retries once on empty page. Deduplicates globally.
    """
    seen_ids: set[str] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage'],
        )
        enricher = CostEnricher('century21', browser, SELECTORS)
        await enricher.start()

        lt_configs = [
            ('sale', city_url(SALE_BASE, city)),
            ('rent', city_url(RENT_BASE, city)),
        ]

        for lt_name, base_path in lt_configs:
            max_pages = MAX_PAGES[lt_name]
            logger.info('── Starting Century 21 %s scrape (max %d pages) ──', lt_name, max_pages)

            for page_num in range(1, max_pages + 1):
                url = _page_url(base_path, page_num)

                try:
                    raw_count, cards = await _fetch_page_cards(browser, url, lt_name, page_num)
                except Exception as e:
                    logger.warning('[%s] page %d failed: %s — stopping', lt_name, page_num, e)
                    break

                if raw_count == 0:
                    logger.info('[%s] page %d: 0 cards — waiting %.0fs then retrying', lt_name, page_num, RETRY_WAIT)
                    await asyncio.sleep(RETRY_WAIT)
                    try:
                        raw_count, cards = await _fetch_page_cards(browser, url, lt_name, page_num)
                    except Exception as e:
                        logger.warning('[%s] page %d retry failed: %s', lt_name, page_num, e)
                        break
                    if raw_count == 0:
                        logger.info('[%s] Stopping at page %d — end of results', lt_name, page_num)
                        break

                new_cards = [c for c in cards if c['listing_id'] not in seen_ids]

                logger.info(
                    '[%s] page %3d: %2d raw | %2d extracted | %2d new (total seen: %d)',
                    lt_name, page_num, raw_count, len(cards), len(new_cards), len(seen_ids) + len(new_cards),
                )

                if not new_cards and raw_count > 0:
                    logger.info('[%s] All listings already seen — pagination cycle, stopping', lt_name)
                    break

                for card in new_cards:
                    seen_ids.add(card['listing_id'])
                    await enricher.enrich(card)
                    yield card

        await enricher.close()
        await browser.close()


async def run_century21_scraper() -> AsyncGenerator[dict, None]:
    """Scrapes every city in scraper.cities.CITIES, tagging each listing with its city."""
    for city in CITIES:
        logger.info('══ century21 — %s ══', city['name'])
        async for listing in _run_century21_city(city):
            listing['city'] = city['slug']
            yield listing
