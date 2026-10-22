"""
Rawson Properties — Cape Town listing scraper (v2).

Scrapes Cape Town residential for-sale and to-rent listings from rawson.co.za.

Two-phase scrape:
  1. Collect listing cards from search results (URL, title, price, beds, baths,
     suburb, synopsis).
  2. Visit each listing detail page to extract:
       - Building Size (size_m2)
       - Full description
       - has_parking (Garage / Parking spaces)
       - has_garden

Pagination: ?page=N (page 1 has no param).  20 listings per page.
Random delay 4–8 s between detail-page visits to be polite.

POPIA: no personal info stored; all text is sanitised via clean_personal_info.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import AsyncGenerator

from playwright.async_api import async_playwright, Browser, Page

from scraper.utils import (
    clean_personal_info,
    normalise_price,
    parse_price,
    normalise_suburb,
    compute_price_per_m2,
    validate_image_url,
)
from scraper.cities import CITIES, city_index, city_url
from scraper.detail import DETAIL_FIELDS, LOG_EXPECTED, log_missing
from scraper.detail_page import apply_details, read_detail_from_page
from scraper.enrich import apply_costs, read_card_status, read_costs_from_page

logger = logging.getLogger('homelens.rawson')

# ═══════════════════════════════════════════════════════════════════════════
# SITE SELECTORS — listing status + monthly levy/rates.
# The only place to edit when this site's markup changes. Confirmed against
# saved pages in tests/fixtures/ (re-capture: scripts/capture_fixtures.py).
# ═══════════════════════════════════════════════════════════════════════════
SELECTORS = {
    # Result card image overlay:
    #   <div class="badges"><div class="badge badge--red">Sold</div>
    #   <div class="badge badge--white">Sole Mandate</div> …
    # ("New Listing", "Sole/Joint Mandate" are ignored.)
    'status_badge': '.badges .badge',
    'status_map':   {},
    # Listing page, in the same features grid as Building Size / Garages — one
    # text per item, label and amount together:
    #   <div class="features__label">Rates R6,800 per month</div>
    'cost_rows':    '.features__item .features__label',
    'cost_label':   None,
    'cost_value':   None,
    'description':  '.content-block__synopsis--large',
}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL   = 'https://rawson.co.za'
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)

SALE_BASE_URL = BASE_URL + '/property/for-sale/cape-town'
RENT_BASE_URL = BASE_URL + '/property/to-rent/cape-town'

# Approximate listing counts — 20 cards per page.
# Allow generous upper bounds; pagination stops on empty pages.
MAX_PAGES: dict[str, int] = {
    'sale': 20,
    'rent': 20,
}

SCROLL_STEPS    = 10     # increments to trigger lazy-loaded cards
SCROLL_PAUSE    = 0.30   # seconds between scroll steps
PAGE_LOAD_WAIT  = 4.5    # seconds after networkidle before scraping
RETRY_WAIT      = 15.0   # seconds before retrying an empty page
DETAIL_DELAY_LO = 4.0    # minimum seconds between detail-page visits
DETAIL_DELAY_HI = 8.0    # maximum seconds between detail-page visits

SIZE_MIN = 15.0   # m² — below this is almost certainly noise
SIZE_MAX = 800.0  # m² — above this is almost certainly ERF/plot size

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _page_url(base: str, page: int) -> str:
    """Page 1 has no query param; page N uses ?page=N."""
    return base if page == 1 else f'{base}?page={page}'


def _listing_id_from_url(url: str) -> str:
    """
    Extract listing ID from Rawson URL.
    URL format: /property/(for-sale|to-rent)/BRANCH/ID
    e.g. https://rawson.co.za/property/for-sale/constantia/1347494
    → 'rawson_1347494'
    """
    ref = url.rstrip('/').split('/')[-1]
    return f'rawson_{ref}' if ref else f'rawson_{hash(url)}'


def _extract_suburb_from_title(title: str) -> str | None:
    """
    Extract raw suburb from Rawson titles like:
      '4 Bedroom house for sale in Constantia, Cape Town'
      '2 Bedroom flat to rent in Sea Point'
    """
    m = re.search(
        r'\bin\s+([A-Za-z][A-Za-z\s\-\'\.]+?)(?:,\s*(?:Cape Town|Johannesburg)|\s*[-–]|\s*$)',
        title,
        re.I,
    )
    return m.group(1).strip() if m else None


def _parse_building_size(label_text: str) -> float | None:
    """
    Parse 'Building Size 527m²' or 'Building Size 1,230 m²' → 527.0 / 1230.0.
    Returns None if the value is outside [SIZE_MIN, SIZE_MAX].
    """
    m = re.search(r'Building Size\s*([\d,]+(?:\.\d+)?)\s*m', label_text, re.I)
    if not m:
        return None
    try:
        val = float(m.group(1).replace(',', ''))
    except ValueError:
        return None
    if SIZE_MIN <= val <= SIZE_MAX:
        return val
    return None


def _new_context(browser: Browser):
    """Create a fresh browser context (no shared cookies/session)."""
    return browser.new_context(
        user_agent=USER_AGENT,
        viewport={'width': 1280, 'height': 900},
        locale='en-ZA',
        timezone_id='Africa/Johannesburg',
    )


# ---------------------------------------------------------------------------
# Search-results page: collect cards
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


async def _extract_cards(page: Page, listing_type: str) -> list[dict]:
    """
    Extract basic listing dicts from div.card elements on a search-results page.
    Does NOT visit detail pages — only data available on the card is collected.

    Returns a list of partial dicts; detail-page fields are set to None.
    """
    cards = await page.query_selector_all('div.card')
    if not cards:
        return []

    results: list[dict] = []

    for card in cards:
        try:
            # ── URL + Listing ID ────────────────────────────────────────────
            link_el = await card.query_selector('a.card__link')
            if not link_el:
                continue
            href = (await link_el.get_attribute('href') or '').strip()
            if not href:
                continue
            full_url   = href if href.startswith('http') else BASE_URL + href
            listing_id = _listing_id_from_url(full_url)

            # ── Title ────────────────────────────────────────────────────────
            title_el = await card.query_selector('h3.card__title a')
            title    = (await title_el.inner_text()).strip() if title_el else ''
            if not title:
                continue

            # ── Suburb ───────────────────────────────────────────────────────
            raw_suburb        = _extract_suburb_from_title(title)
            suburb_normalised = normalise_suburb(raw_suburb) if raw_suburb else None

            # ── Price ─────────────────────────────────────────────────────────
            price_el    = await card.query_selector('.card__price')
            price_text  = (await price_el.inner_text()).strip() if price_el else ''
            price, price_status = parse_price(price_text)
            if not price_el:
                price_status = 'extraction_failed'

            # ── Beds / Baths ──────────────────────────────────────────────────
            bedrooms  = None
            bathrooms = None

            feature_items = await card.query_selector_all('.features__item')
            for item in feature_items:
                icon_el = await item.query_selector('[class*="icon-bed"]')
                if icon_el and bedrooms is None:
                    label_el = await item.query_selector('.features__label')
                    if label_el:
                        try:
                            bedrooms = int((await label_el.inner_text()).strip())
                        except (ValueError, TypeError):
                            pass
                    continue

                icon_el = await item.query_selector('[class*="icon-bath"]')
                if icon_el and bathrooms is None:
                    label_el = await item.query_selector('.features__label')
                    if label_el:
                        try:
                            bathrooms = int((await label_el.inner_text()).strip())
                        except (ValueError, TypeError):
                            pass

            status, status_source = await read_card_status(card, SELECTORS)

            # ── Synopsis (card snippet — used as fallback only) ────────────────
            synopsis_el = await card.query_selector('.card__synopsis p')
            synopsis    = (await synopsis_el.inner_text()).strip() if synopsis_el else ''

            results.append({
                'listing_id':        listing_id,
                'listing_url':       full_url,
                'source':            'rawson',
                'listing_type':      listing_type,
                'title':             clean_personal_info(title[:250]),
                'price':             price,
                'price_status':      price_status,
                'bedrooms':          bedrooms,
                'bathrooms':         bathrooms,
                'size_m2':           None,   # filled from detail page
                'price_per_m2':      None,   # filled after size_m2 known
                'suburb_normalised': suburb_normalised,
                'description':       clean_personal_info(synopsis[:2000]) if synopsis else None,
                'has_parking':       None,   # filled from detail page
                'has_garden':        None,   # filled from detail page
                'is_active':         True,
                'listing_status':    status,
                'status_source':     status_source,
                '_synopsis':         synopsis,  # temp; for fallback if detail fails
            })

        except Exception as e:
            logger.debug('Card parse error: %s', e)
            continue

    return results


async def _fetch_page_cards(
    browser: Browser,
    url: str,
    lt_name: str,
    page_num: int,
) -> tuple[int, list[dict]]:
    """
    Load one search-results page and return (raw_card_count, listing_dicts).
    Uses a fresh browser context per request.
    """
    context = await _new_context(browser)
    page    = await context.new_page()
    try:
        logger.info('  [%s] Scraping page %d → %s', lt_name, page_num, url)
        await page.goto(url, wait_until='domcontentloaded', timeout=45_000)
        try:
            await page.wait_for_selector('div.card', timeout=20_000)
        except Exception:
            pass
        await asyncio.sleep(PAGE_LOAD_WAIT)
        await _scroll_to_load_all(page)

        raw_cards = await page.query_selector_all('div.card')
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


# ---------------------------------------------------------------------------
# Detail page: enrich with size_m2, description, has_parking, has_garden
# ---------------------------------------------------------------------------

async def _fetch_detail(browser: Browser, url: str) -> dict:
    """
    Visit a listing detail page and return a dict with:
      size_m2       – float | None
      description   – str | None  (full description, POPIA sanitised)
      has_parking   – bool
      has_garden    – bool

    Returns sensible defaults on any error.
    """
    defaults = {
        'size_m2':     None,
        'description': None,
        'has_parking': False,
        'has_garden':  False,
        'image_url':   None,
        'fields':      None,
    }

    context = await _new_context(browser)
    page    = await context.new_page()
    try:
        await page.goto(url, wait_until='domcontentloaded', timeout=45_000)
        try:
            await page.wait_for_selector(
                '.features__item, .content-block__synopsis--large', timeout=15_000,
            )
        except Exception:
            pass  # page may lack these; fall through with whatever rendered
        await asyncio.sleep(2.0)

        size_m2     = None
        has_parking = False
        has_garden  = False

        # ── Property features (Building Size, Parking, Garden) ─────────────
        feature_labels = await page.query_selector_all('.features__item .features__label')
        for lbl_el in feature_labels:
            text = (await lbl_el.inner_text()).strip()

            if size_m2 is None and 'Building Size' in text:
                size_m2 = _parse_building_size(text)

            low = text.lower()
            if not has_parking and ('garage' in low or 'parking' in low):
                has_parking = True

            if not has_garden and 'garden' in low:
                has_garden = True

        # ── Full description ────────────────────────────────────────────────
        description = None
        desc_el = await page.query_selector('.content-block__synopsis--large')
        if desc_el:
            raw = (await desc_el.inner_text()).strip()
            if raw:
                description = clean_personal_info(raw[:4000])

        # ── Image — gallery first, og:image fallback ────────────────────────
        raw_image = await page.evaluate('''() => {
            const gallerySelectors = [
                '.swiper-slide img',
                '.gallery img',
                'div[class*="gallery"] img',
                '.main-image img',
                '.hero-image img',
                '.property-images img'
            ];
            for (const sel of gallerySelectors) {
                const img = document.querySelector(sel);
                if (img && img.src && img.src.startsWith('http')) return img.src;
            }
            const meta = document.querySelector('meta[property="og:image"]');
            return meta ? meta.content : null;
        }''')
        image_url = validate_image_url(raw_image)

        # ── Monthly levy / rates (site rows first, then text fallback) ──────
        costs = await read_costs_from_page(page, SELECTORS)
        fields = await read_detail_from_page(page, SELECTORS)

        return {
            'fields':      fields,
            'size_m2':     size_m2,
            'description': description,
            'has_parking': has_parking,
            'has_garden':  has_garden,
            'image_url':   image_url,
            'costs':       costs,
        }

    except Exception as e:
        logger.warning('Detail fetch failed for %s: %s', url, e)
        return defaults
    finally:
        try:
            await context.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main scraper generator
# ---------------------------------------------------------------------------

async def _run_rawson_city(city: dict) -> AsyncGenerator[dict, None]:
    """
    Async generator — yields one enriched listing dict at a time.

    Phase 1: Iterate search-result pages for both sale and rent,
             collecting basic card data.
    Phase 2: For each new listing, visit the detail page to get
             size_m2, full description, has_parking, has_garden.
             Adds a random 4–8 s delay between detail requests.

    Deduplicates globally (by listing_id) to avoid revisiting listings
    that appear on multiple pages.
    """
    seen_ids: set[str] = set()
    total_with_size = 0
    total_yielded   = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage'],
        )

        lt_configs = [
            ('sale', city_url(SALE_BASE_URL, city)),
            ('rent', city_url(RENT_BASE_URL, city)),
        ]

        for lt_name, base_url in lt_configs:
            max_pages = MAX_PAGES[lt_name]
            logger.info('── Starting %s scrape (max %d pages) ──', lt_name, max_pages)

            for page_num in range(1, max_pages + 1):
                url = _page_url(base_url, page_num)

                # ── Load search-results page ────────────────────────────────
                try:
                    raw_count, cards = await _fetch_page_cards(
                        browser, url, lt_name, page_num
                    )
                except Exception as e:
                    logger.warning(
                        '[%s] page %d failed to load: %s — stopping this type',
                        lt_name, page_num, e,
                    )
                    break

                # ── Retry on empty page (possible soft block) ───────────────
                if raw_count == 0:
                    logger.info(
                        '[%s] page %d returned 0 cards — waiting %.0fs then retrying...',
                        lt_name, page_num, RETRY_WAIT,
                    )
                    await asyncio.sleep(RETRY_WAIT)
                    try:
                        raw_count, cards = await _fetch_page_cards(
                            browser, url, lt_name, page_num
                        )
                    except Exception as e:
                        logger.warning(
                            '[%s] page %d retry failed: %s — stopping',
                            lt_name, page_num, e,
                        )
                        break

                    if raw_count == 0:
                        logger.info(
                            '[%s] Stopping at page %d — no cards after retry',
                            lt_name, page_num,
                        )
                        break

                # ── Filter to unseen listings ───────────────────────────────
                new_cards = [c for c in cards if c['listing_id'] not in seen_ids]

                logger.info(
                    '[%s] page %3d: %2d raw | %2d extracted | %2d new  (seen: %d)',
                    lt_name, page_num,
                    raw_count, len(cards), len(new_cards),
                    len(seen_ids) + len(new_cards),
                )

                if not new_cards and raw_count > 0:
                    logger.info(
                        '[%s] Stopping at page %d — all listings already seen',
                        lt_name, page_num,
                    )
                    break

                # ── Enrich each new listing via its detail page ─────────────
                for card in new_cards:
                    seen_ids.add(card['listing_id'])
                    listing_url = card['listing_url']

                    logger.debug(
                        '[%s] Fetching detail: %s', lt_name, listing_url
                    )
                    detail = await _fetch_detail(browser, listing_url)

                    # Merge detail into card
                    card['size_m2']     = detail['size_m2']
                    card['has_parking'] = detail['has_parking']
                    card['has_garden']  = detail['has_garden']
                    card['image_url']   = detail['image_url']

                    # Levies/rates: sale listings only (not shown on rentals)
                    if lt_name == 'sale' and detail.get('costs'):
                        apply_costs(card, detail['costs'])

                    # Prefer full description from detail; fall back to synopsis
                    if detail['description']:
                        card['description'] = detail['description']
                    else:
                        synopsis = card.get('_synopsis', '')
                        card['description'] = (
                            clean_personal_info(synopsis[:2000]) if synopsis else None
                        )

                    # Shared detail reader: erf, parking count, furnished, security,
                    # views … (keeps the size/description read above when present)
                    if detail.get('fields'):
                        apply_details(card, detail['fields'])
                        log_missing('rawson', card['listing_id'],
                                    {k: card.get(k) for k in DETAIL_FIELDS}, expected=LOG_EXPECTED)

                    # Compute price_per_m2 if we have both
                    if card['size_m2'] and card['price']:
                        card['price_per_m2'] = compute_price_per_m2(
                            card['price'], card['size_m2']
                        )

                    # Remove temp field
                    card.pop('_synopsis', None)

                    if card['size_m2']:
                        total_with_size += 1
                    total_yielded += 1

                    yield card

                    # Polite delay between detail requests
                    delay = random.uniform(DETAIL_DELAY_LO, DETAIL_DELAY_HI)
                    await asyncio.sleep(delay)

        await browser.close()

    logger.info(
        'Rawson scrape complete — %d listings yielded, %d with size_m2 (%.0f%%)',
        total_yielded,
        total_with_size,
        100 * total_with_size / total_yielded if total_yielded else 0,
    )


async def run_rawson_scraper() -> AsyncGenerator[dict, None]:
    """Scrapes every city in scraper.cities.CITIES, tagging each listing with its city."""
    for city in CITIES:
        logger.info('══ rawson — %s ══', city['name'])
        async for listing in _run_rawson_city(city):
            listing['city'] = city['slug']
            yield listing
