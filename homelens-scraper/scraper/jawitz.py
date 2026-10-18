"""
Jawitz Properties — Cape Town listing scraper.

Scrapes Cape Town residential for-sale and to-rent listings from jawitz.co.za.
Jawitz uses a list-view layout (NOT the shared property-card-sm platform).

Card structure:
  div.property-list-listing   → container with data-href and data-id attrs
  span.property-list-price-heading → "R11,000,000"
  a.property-list-marketing-heading → title text + href
  div.property-list-beds span[itemprop]  → bedrooms count
  div.property-list-baths span[itemprop] → bathrooms count
  img[itemprop="image"]       → 640×480 property photo

Size: parsed from card inner text via regex (not always present in list view).
10 listings per page.

Sale:  ~103 listings → max 15 pages
Rent:  ~143 listings → max 18 pages

Pagination: ?page=N

POPIA: no personal info stored; all description text is sanitised.
"""
from __future__ import annotations

import asyncio
import logging
from scraper.utils import goto_with_retry
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

logger = logging.getLogger('homelens.jawitz')

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

BASE_URL   = 'https://www.jawitz.co.za'
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)

SALE_BASE = BASE_URL + '/results/residential/for-sale/cape-town/'
RENT_BASE = BASE_URL + '/results/residential/to-rent/cape-town/'

MAX_PAGES: dict[str, int] = {
    'sale': 15,
    'rent': 18,
}

SCROLL_STEPS   = 10
SCROLL_PAUSE   = 0.35
PAGE_LOAD_WAIT = 5.0
RETRY_WAIT     = 15.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _page_url(base: str, page_num: int) -> str:
    if page_num == 1:
        return base
    return f'{base}?page={page_num}'


def _extract_suburb_from_url(url: str) -> str | None:
    """
    /results/residential/for-sale/cape-town/{suburb}/{type}/{id}/
    Suburb at path index 4.
    """
    path  = urlparse(url).path.rstrip('/')
    parts = [p for p in path.split('/') if p]
    ct_idx = city_index(parts)
    if ct_idx is not None and len(parts) > ct_idx + 1:
        return parts[ct_idx + 1]
    return None


def _parse_int(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r'\d+', text)
    return int(m.group()) if m else None


def _parse_size_from_text(text: str) -> float | None:
    """
    Extract floor size from free text like '217m²' or '115 m2'.
    Skips ERF / land area keywords.
    Validates 15–50 000 m².
    """
    text_lc = text.lower()
    if any(kw in text_lc for kw in ('erf', 'land area', 'stand size', 'plot size', 'lot size')):
        return None
    m = re.search(r'([\d\s,]+\.?\d*)\s*m[²2]', text)
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
        await goto_with_retry(page, url, wait_until='networkidle', timeout=60_000)
        await asyncio.sleep(PAGE_LOAD_WAIT)
        await _scroll_to_load_all(page)

        raw_cards = await page.query_selector_all('div.property-list-listing')
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
    Extract all listing dicts from div.property-list-listing elements.

    Card structure (Jawitz list-view):
      div.property-list-listing  → container; data-id, data-href on this element
      span.property-list-price-heading → price text
      a.property-list-marketing-heading → title + href
      div.property-list-beds span[itemprop="numberOfBedrooms"] → beds
      div.property-list-baths span[itemprop="numberOfBathroomsTotal"] → baths
      img[itemprop="image"] → property photo (640×480 avif)
      card inner text → may contain "Nm²" for floor size
    """
    cards = await page.query_selector_all('div.property-list-listing')
    if not cards:
        return []

    results: list[dict] = []

    for card in cards:
        try:
            # ── ID + URL ──────────────────────────────────────────────────────
            data_id = (await card.get_attribute('data-id') or '').strip()
            if not data_id:
                continue
            listing_id = f'jawitz_{data_id}'

            data_href = (await card.get_attribute('data-href') or '').strip()
            # Also check the title anchor
            if not data_href:
                link_el   = await card.query_selector('a.property-list-marketing-heading')
                data_href = (await link_el.get_attribute('href') or '').strip() if link_el else ''
            if not data_href:
                continue
            full_url = data_href if data_href.startswith('http') else BASE_URL + data_href

            # ── Title (extracted early — needed for suburb fallback) ───────────
            title_el = await card.query_selector('a.property-list-marketing-heading')
            title    = (await title_el.inner_text()).strip() if title_el else ''

            # ── Suburb from URL ───────────────────────────────────────────────
            suburb_slug       = _extract_suburb_from_url(full_url)
            suburb_normalised = normalise_suburb(suburb_slug) if suburb_slug else None

            # Fallback: extract from title when URL has no suburb segment
            if not suburb_normalised and title:
                for _pat in (
                    r'\bin\s+([A-Za-z\s]+?)(?:\s+for\s+sale|\s+to\s+let|\s+for\s+rent)?$',
                    r'\bin\s+([A-Za-z\s]+?)(?:\s*[-–]\s*|\s*,|\s*$)',
                ):
                    _m = re.search(_pat, title, re.I)
                    if _m:
                        suburb_normalised = normalise_suburb(_m.group(1).strip())
                        if suburb_normalised:
                            break

            # Skip listings with no identifiable suburb
            if not suburb_normalised:
                continue

            # ── Price ─────────────────────────────────────────────────────────
            price_el    = await card.query_selector('span.property-list-price-heading')
            price_text  = (await price_el.inner_text()).strip() if price_el else ''
            price, price_status = parse_price(price_text)
            if not price_el:
                price_status = 'extraction_failed'

            # ── Beds / Baths ──────────────────────────────────────────────────
            beds_el   = await card.query_selector('span[itemprop="numberOfBedrooms"]')
            baths_el  = await card.query_selector('span[itemprop="numberOfBathroomsTotal"]')
            beds_text = (await beds_el.inner_text()).strip() if beds_el else None
            bath_text = (await baths_el.inner_text()).strip() if baths_el else None
            bedrooms  = _parse_int(beds_text)
            bathrooms = _parse_int(bath_text)

            # ── Size — regex on card inner text ───────────────────────────────
            card_text = (await card.inner_text()).strip()
            size_m2   = _parse_size_from_text(card_text)

            # ── Image ─────────────────────────────────────────────────────────
            img_el    = await card.query_selector('img[itemprop="image"]')
            if not img_el:
                img_el = await card.query_selector('img')
            raw_image = None
            if img_el:
                raw_image = (
                    await img_el.get_attribute('src')
                    or await img_el.get_attribute('data-src')
                )
            image_url = validate_image_url(raw_image)

            status, status_source = await read_card_status(card, SELECTORS)

            results.append({
                'listing_id':        listing_id,
                'listing_url':       full_url,
                'source':            'jawitz',
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

async def _run_jawitz_city(city: dict) -> AsyncGenerator[dict, None]:
    """
    Async generator — yields one listing dict at a time.

    Iterates through Cape Town pages for sale and to-rent.
    Uses ?page=N pagination (~10 listings per page).
    Retries once on empty page before treating as end of results.
    Deduplicates globally.
    """
    seen_ids: set[str] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage'],
        )
        enricher = CostEnricher('jawitz', browser, SELECTORS)
        await enricher.start()

        lt_configs = [
            ('sale', city_url(SALE_BASE, city)),
            ('rent', city_url(RENT_BASE, city)),
        ]

        for lt_name, base_path in lt_configs:
            max_pages = MAX_PAGES[lt_name]
            logger.info('── Starting Jawitz %s scrape (max %d pages) ──', lt_name, max_pages)

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


async def run_jawitz_scraper() -> AsyncGenerator[dict, None]:
    """Scrapes every city in scraper.cities.CITIES, tagging each listing with its city."""
    for city in CITIES:
        logger.info('══ jawitz — %s ══', city['name'])
        async for listing in _run_jawitz_city(city):
            listing['city'] = city['slug']
            yield listing
