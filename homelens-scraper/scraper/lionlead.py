"""
LIONLEAD Real Estate — Cape Town listing scraper.

Site: lionleadrealestate.co.za
Architecture: Custom React SPA (client-side rendered).
  Playwright required — cards are NOT in raw HTML.

Card structure (.property-list-listing):
  .property-list-price-heading      → "R [price]"
  .property-list-marketing-heading  → address / suburb text
  .property-list-meta div span      → beds / baths / size (positional)
  .property-list-image-thumb img    → CloudFront CDN image (.avif)
  a[href]                           → detail URL

Detail URL: /results/residential/{listing_type}/cape-town/{suburb}/{type}/{id}/
  ID is the last numeric segment.
  Suburb is the segment after 'cape-town'.

Approximate Cape Town listings: ~49 for sale, plus rentals.
Pagination: ?page=N

POPIA: no personal info stored.
"""
from __future__ import annotations

import asyncio
import logging
import random
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

logger = logging.getLogger('homelens.lionlead')

# ═══════════════════════════════════════════════════════════════════════════
# SITE SELECTORS — listing status + monthly levy/rates.
# The only place to edit when this site's markup changes. Confirmed against
# saved pages in tests/fixtures/ (re-capture: scripts/capture_fixtures.py).
# ═══════════════════════════════════════════════════════════════════════════
SELECTORS = {
    # Search-result card image overlay:
    #   <div class="info-labels"><div class="transparent-red top left">UNDER OFFER</div>…
    # The same box also carries "NEW", "PRICE REDUCED", "NO TRANSFER DUTY"
    # (ignored) and a photo count in .transparent-black (ignored). The site's
    # stylesheet also styles .transparent-black.sold — included in case a sold
    # label uses it (text must still read "sold" to count).
    'status_badge': '.info-labels .transparent-red, .info-labels .transparent-black.sold',
    'status_map':   {},
    # Listing page, label and value share one element:
    #   <div class="bond-assumptions"><div class="pull-left">
    #     <strong class="sub-headings-color">Monthly Levy</strong><br> R3,569.77</div>…
    # (the value is the row's own text; "Deeds Office Levy" etc. sit in a
    # separate transfer-cost table and are never matched)
    'cost_rows':    '.bond-assumptions .pull-left',
    'cost_label':   'strong.sub-headings-color',
    'cost_value':   None,
    'description':  '.listing-description',
}


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL   = 'https://www.lionleadrealestate.co.za'
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)

SALE_BASE = BASE_URL + '/results/residential/for-sale/cape-town/'
RENT_BASE = BASE_URL + '/results/residential/to-let/cape-town/'

MAX_PAGES: dict[str, int] = {
    'sale': 10,
    'rent': 10,
}

SCROLL_STEPS   = 10
SCROLL_PAUSE   = 0.30
INITIAL_WAIT   = 3.0
CARD_WAIT_MS   = 20_000
POST_CARD_WAIT = 2.0
RETRY_WAIT     = 15.0

_CARD_SEL  = '.property-list-listing'
_MAX_BEDS  = 10
_SKIP_TYPES = ('commercial', 'industrial', 'retail', 'mixed-use', 'agricultural', 'vacant-land')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _page_url(base: str, page_num: int) -> str:
    if page_num == 1:
        return base
    return f'{base}?page={page_num}'


def _extract_suburb_from_href(data_href: str) -> str | None:
    """
    data-href: /results/residential/for-sale/cape-town/{suburb}/{type}/{id}/
    Returns suburb slug after 'cape-town'.
    """
    path  = urlparse(data_href).path.rstrip('/')
    parts = [p for p in path.split('/') if p]
    ct_idx = city_index(parts)
    if ct_idx is None:
        return None
    if ct_idx + 1 < len(parts):
        suburb = parts[ct_idx + 1]
        if suburb not in _SKIP_TYPES and not suburb.isdigit():
            return suburb
    return None


def _parse_int_span(text: str) -> int | None:
    """Parse a numeric span value like '3' or '3.5' → int (floor)."""
    m = re.search(r'(\d+)', text.strip())
    if m:
        val = int(m.group(1))
        return val if 0 < val <= _MAX_BEDS else None
    return None


def _validate_listing(price: int | None, size_m2: float | None, bedrooms: int | None, listing_type: str = 'sale') -> bool:
    if price is not None:
        if listing_type == 'rent':
            if price < 1_000 or price > 500_000:
                return False
        else:
            if price < 50_000 or price > 500_000_000:
                return False
    if size_m2 is not None and (size_m2 < 15 or size_m2 > 5_000):
        return False
    if bedrooms is not None and bedrooms > _MAX_BEDS:
        return False
    return True


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
        await page.goto(url, wait_until='domcontentloaded', timeout=60_000)
        await asyncio.sleep(INITIAL_WAIT)

        try:
            await page.wait_for_selector(_CARD_SEL, timeout=CARD_WAIT_MS)
        except Exception:
            logger.debug('[%s] page %d: wait_for_selector timed out (likely empty page)', lt_name, page_num)

        await asyncio.sleep(POST_CARD_WAIT)
        await _scroll_to_load_all(page)

        raw_cards = await page.query_selector_all(_CARD_SEL)
        raw_count = len(raw_cards)
        listings  = await _extract_cards(page, lt_name)
        logger.info(
            '  [%s] Page %d: %d cards → %d listings extracted',
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
    Extract listing dicts from .property-list-listing elements.

    Actual card structure (PropData platform):
      div.property-list-listing[data-id][data-href]
        .property-list-image-thumb img[src]     → CloudFront avif image
        .property-list-price-heading            → "R40,000,000"
        h3 > a.property-list-marketing-heading  → title + detail href
        .property-list-beds span                → bedrooms count
        .property-list-baths span               → bathrooms count
    """
    cards = await page.query_selector_all(_CARD_SEL)
    if not cards:
        return []

    results: list[dict] = []

    for card in cards:
        try:
            # ── ID + URL from card attributes ──────────────────────────────────
            data_id   = (await card.get_attribute('data-id')   or '').strip()
            data_href = (await card.get_attribute('data-href') or '').strip()
            if not data_id:
                continue

            listing_id = f'lionlead_{data_id}'

            if data_href:
                if any(t in data_href.lower() for t in _SKIP_TYPES):
                    continue
                full_url = BASE_URL + data_href if not data_href.startswith('http') else data_href
            else:
                # Fallback: detail link on .property-list-marketing-heading
                mh = await card.query_selector('.property-list-marketing-heading')
                href = (await mh.get_attribute('href') or '').strip() if mh else ''
                if not href:
                    continue
                full_url = BASE_URL + href if not href.startswith('http') else href
                data_href = href

            # ── Suburb from path after 'cape-town' ────────────────────────────
            suburb_slug       = _extract_suburb_from_href(data_href)
            suburb_normalised = normalise_suburb(suburb_slug) if suburb_slug else None

            # ── Title ─────────────────────────────────────────────────────────
            mh_el = await card.query_selector('.property-list-marketing-heading')
            title  = (await mh_el.inner_text()).strip() if mh_el else ''

            if not suburb_normalised and title:
                suburb_normalised = normalise_suburb(title.split(',')[0].strip())

            # ── Price ─────────────────────────────────────────────────────────
            price_el    = await card.query_selector('.property-list-price-heading')
            price_text  = (await price_el.inner_text()).strip() if price_el else ''
            price, price_status = parse_price(price_text)
            if not price_el:
                price_status = 'extraction_failed'

            # ── Beds / Baths ───────────────────────────────────────────────────
            bedrooms  = None
            bathrooms = None

            beds_el = await card.query_selector('.property-list-beds span')
            if beds_el:
                bedrooms = _parse_int_span((await beds_el.inner_text()))

            baths_el = await card.query_selector('.property-list-baths span')
            if baths_el:
                bathrooms = _parse_int_span((await baths_el.inner_text()))

            # ── Image (CloudFront CDN — avif, already loaded) ─────────────────
            raw_image = await card.evaluate('''card => {
                const img = card.querySelector(".property-list-image-thumb img");
                if (img) {
                    const src = img.src || img.getAttribute("src");
                    if (src && src.startsWith("http") && !src.startsWith("data:")) return src;
                }
                return null;
            }''')
            image_url = validate_image_url(raw_image)

            # ── Validate ──────────────────────────────────────────────────────
            if not _validate_listing(price, None, bedrooms, listing_type):
                logger.debug(
                    'Skipping %s: price=%s beds=%s',
                    listing_id, price, bedrooms,
                )
                continue

            status, status_source = await read_card_status(card, SELECTORS)

            results.append({
                'listing_id':        listing_id,
                'listing_url':       full_url,
                'source':            'lionlead',
                'listing_type':      listing_type,
                'title':             clean_personal_info(title[:250]),
                'price':             price,
                'price_status':      price_status,
                'bedrooms':          bedrooms,
                'bathrooms':         bathrooms,
                'size_m2':           None,   # not shown on listing card
                'suburb_normalised': suburb_normalised,
                'description':       None,
                'price_per_m2':      None,
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

async def _run_lionlead_city(city: dict) -> AsyncGenerator[dict, None]:
    """Async generator — yields one listing dict at a time."""
    seen_ids: set[str] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage'],
        )
        enricher = CostEnricher('lionlead', browser, SELECTORS)
        await enricher.start()

        lt_configs = [
            ('sale', city_url(SALE_BASE, city)),
            ('rent', city_url(RENT_BASE, city)),
        ]

        for lt_name, base_path in lt_configs:
            max_pages = MAX_PAGES[lt_name]
            logger.info('── Starting LIONLEAD %s scrape (max %d pages) ──', lt_name, max_pages)

            for page_num in range(1, max_pages + 1):
                url = _page_url(base_path, page_num)

                try:
                    raw_count, cards = await _fetch_page_cards(browser, url, lt_name, page_num)
                except Exception as e:
                    logger.warning('[%s] Page %d failed: %s — stopping', lt_name, page_num, e)
                    break

                if raw_count == 0:
                    logger.info('[%s] Page %d: 0 cards — waiting %.0fs then retrying', lt_name, page_num, RETRY_WAIT)
                    await asyncio.sleep(RETRY_WAIT)
                    try:
                        raw_count, cards = await _fetch_page_cards(browser, url, lt_name, page_num)
                    except Exception as e:
                        logger.warning('[%s] Page %d retry failed: %s', lt_name, page_num, e)
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
                    logger.info('[%s] All listings already seen — stopping', lt_name)
                    break

                for card in new_cards:
                    seen_ids.add(card['listing_id'])
                    await enricher.enrich(card)
                    yield card

                if page_num < max_pages and new_cards:
                    delay = random.uniform(5, 8)
                    logger.debug('[%s] Waiting %.1fs before next page', lt_name, delay)
                    await asyncio.sleep(delay)

        await enricher.close()
        await browser.close()


async def run_lionlead_scraper() -> AsyncGenerator[dict, None]:
    """Scrapes every city in scraper.cities.CITIES, tagging each listing with its city."""
    for city in CITIES:
        logger.info('══ lionlead — %s ══', city['name'])
        async for listing in _run_lionlead_city(city):
            listing['city'] = city['slug']
            yield listing
