"""
Duarte Properties — Cape Town listing scraper.

WordPress / Houzez Real Estate theme — fully server-rendered HTML.
  card:   .item-wrap-v6  (was .item-wrap-v5 before June 2026 redesign)
  id:     data-hz-id attribute on card
  price:  .item-price     → "R3 000 000"  (note: space between R and amount)
  title:  .item-title a   (e.g. "MONT BLU")
  beds:   .h-beds .hz-figure   → "3"
  baths:  .h-baths .hz-figure  → "2"
  size:   .h-area .hz-figure   → e.g. "478" (only on some listings)
  image:  img.houzez-lazyload[data-src] (lazy-loaded placeholder)

URL structure:
  For sale: /?s&post_type=property&status=for-sale
  For rent: /?s&post_type=property&status=for-rent
  Pagination: /page/{N}/?s&post_type=property&status=...

Detail URL: /property/{slug}/
  Slug used as listing_id: duarte_{slug}
  Suburb extracted from slug (first hyphen-segment or from title).

Approximate total: ~180 listings, ~20 pages for rent, fewer for sale.

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
from scraper.enrich import CostEnricher, read_card_status

logger = logging.getLogger('homelens.duarte')

# ═══════════════════════════════════════════════════════════════════════════
# SITE SELECTORS — listing status + monthly levy/rates.
# The only place to edit when this site's markup changes. Confirmed against
# saved pages in tests/fixtures/ (re-capture: scripts/capture_fixtures.py).
# ═══════════════════════════════════════════════════════════════════════════
SELECTORS = {
    # Result card (Houzez theme): <span class="label-status label">For Sale</span>
    # or "Sold". "For Sale"/"Residential"/"Featured" labels are ignored.
    'status_badge': '.label-status',
    'status_map':   {},
    # No structured levy/rates rows on Duarte listing pages (3 saved pages
    # checked: only a mortgage-repayment calculator). cost_rows is None on
    # purpose — costs come from the text fallback over the page + description
    # if an agent types them in; the run report does not flag this site.
    'text_only':    True,   # costs are never in structured rows; see above
    'cost_rows':    None,
    'cost_label':   None,
    'cost_value':   None,
    'description':  '.property-description-content',
    # The "Bond Calculator" block prints "Monthly HOA Fees R250 00" — a calculator
    # default, identical on every listing (seen on 3 different properties), not
    # the property's levy. Hidden before the text fallback runs.
    'text_exclude': '.property-mortgage-calculator-wrap',
}


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL   = 'https://www.duarteproperties.co.za'
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)

SALE_BASE = BASE_URL + '/?s&post_type=property&status=for-sale'
RENT_BASE = BASE_URL + '/?s&post_type=property&status=for-rent'

MAX_PAGES: dict[str, int] = {
    'sale': 25,
    'rent': 25,
}

SCROLL_STEPS   = 8
SCROLL_PAUSE   = 0.30
PAGE_LOAD_WAIT = 4.0
RETRY_WAIT     = 12.0

_MAX_BEDS = 10


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _page_url(base: str, page_num: int) -> str:
    """
    Page 1: /?s&post_type=property&status=for-sale
    Page 2: /page/2/?s&post_type=property&status=for-sale
    """
    if page_num == 1:
        return base
    # Insert /page/N/ before the query string
    query = base.split('/?')[1]
    return f'{BASE_URL}/page/{page_num}/?{query}'


def _slug_from_url(url: str) -> str | None:
    """Extract slug from /property/{slug}/ URLs."""
    path  = urlparse(url).path.rstrip('/')
    parts = [p for p in path.split('/') if p]
    # Structure: /property/{slug}
    try:
        prop_idx = parts.index('property')
        if prop_idx + 1 < len(parts):
            return parts[prop_idx + 1]
    except ValueError:
        pass
    return None


def _suburb_from_title_or_slug(title: str, slug: str | None) -> str | None:
    """
    Duarte titles are typically the suburb name in uppercase:
      "CAMPS BAY", "SEA POINT", "HOUT BAY VILLA"
    Normalise the title directly; fall back to the slug's first hyphen-segment.
    """
    if title:
        # Try each word-group in title
        candidate = title.lower().strip()
        result = normalise_suburb(candidate)
        if result:
            return result

    if slug:
        # e.g. "camps-bay-luxury-villa" → "camps-bay"
        # Try progressively longer prefix until it maps to a known suburb
        parts = slug.split('-')
        for length in range(len(parts), 0, -1):
            test = '-'.join(parts[:length])
            result = normalise_suburb(test)
            if result and result != test.replace(' ', '-'):
                return result
        return normalise_suburb(parts[0])  # final fallback: first word

    return None


def _parse_beds(text: str) -> int | None:
    """'Beds: 5' → 5"""
    m = re.search(r'(\d+)', text)
    if m:
        val = int(m.group(1))
        return val if val <= _MAX_BEDS else None
    return None


def _parse_baths(text: str) -> int | None:
    """'Baths: 3' → 3"""
    m = re.search(r'(\d+)', text)
    if m:
        val = int(m.group(1))
        return val if val <= _MAX_BEDS else None
    return None


def _parse_size(text: str) -> float | None:
    """Plain number e.g. '740' → 740.0  or  '740 m²' → 740.0"""
    text_lc = text.lower()
    if any(kw in text_lc for kw in ('erf', 'land', 'stand', 'plot', 'lot')):
        return None
    m = re.search(r'([\d,]+\.?\d*)', text)
    if m:
        try:
            val = float(m.group(1).replace(',', ''))
            return val if 15 <= val <= 5_000 else None
        except ValueError:
            return None
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
        await page.goto(url, wait_until='networkidle', timeout=60_000)
        await asyncio.sleep(PAGE_LOAD_WAIT)
        await _scroll_to_load_all(page)

        raw_cards = await page.query_selector_all('.item-wrap-v6')
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
    Extract listing dicts from .item-wrap-v6 elements (Houzez v6 theme).

    Card structure (redesigned June 2026):
      div.item-listing-wrap.item-wrap-v6[data-hz-id][data-images]
        .listing-featured-thumb a[href]    → detail URL /property/{slug}/
        img.houzez-lazyload[data-src]      → lazy-loaded property image
        .item-title a                      → property name (often suburb)
        .item-price                        → "R3 000 000"
        .h-beds .hz-figure                 → beds count
        .h-baths .hz-figure                → baths count
        .h-area .hz-figure (optional)      → floor area m²
    """
    cards = await page.query_selector_all('.item-wrap-v6')
    if not cards:
        return []

    results: list[dict] = []

    for card in cards:
        try:
            # ── URL + ID ──────────────────────────────────────────────────────
            # Prefer .item-title a for the detail URL (most reliable)
            title_link = await card.query_selector('.item-title a')
            link_el    = title_link or await card.query_selector('.listing-featured-thumb a, a')
            if not link_el:
                continue
            href = (await link_el.get_attribute('href') or '').strip()
            if not href:
                continue
            full_url = href if href.startswith('http') else BASE_URL + href

            slug = _slug_from_url(full_url)
            if not slug:
                continue
            listing_id = f'duarte_{slug}'

            # ── Title → suburb ────────────────────────────────────────────────
            title = (await link_el.inner_text()).strip()
            suburb_normalised = _suburb_from_title_or_slug(title, slug)

            # ── Price ─────────────────────────────────────────────────────────
            price_el    = await card.query_selector('.item-price')
            price_text  = (await price_el.inner_text()).strip() if price_el else ''
            price, price_status = parse_price(price_text)
            if not price_el:
                price_status = 'extraction_failed'

            # ── Beds / Baths / Size via Houzez v6 selectors ───────────────────
            bedrooms  = None
            bathrooms = None
            size_m2   = None

            beds_el = await card.query_selector('.h-beds .hz-figure')
            if beds_el:
                bedrooms = _parse_beds((await beds_el.inner_text()).strip())

            baths_el = await card.query_selector('.h-baths .hz-figure')
            if baths_el:
                bathrooms = _parse_baths((await baths_el.inner_text()).strip())

            area_el = await card.query_selector('.h-area .hz-figure')
            if area_el:
                size_m2 = _parse_size((await area_el.inner_text()).strip())

            # ── Image (lazy-loaded data-src on .houzez-lazyload) ──────────────
            raw_image = await card.evaluate('''card => {
                const lazy = card.querySelector("img.houzez-lazyload[data-src]");
                if (lazy) {
                    const src = lazy.getAttribute("data-src");
                    if (src && src.startsWith("http")) return src;
                }
                const img = card.querySelector("img");
                if (img) {
                    const src = img.src || img.getAttribute("src");
                    if (src && src.startsWith("http") && !src.startsWith("data:")) return src;
                }
                return null;
            }''')
            image_url = validate_image_url(raw_image)

            # ── Validate ──────────────────────────────────────────────────────
            if not _validate_listing(price, size_m2, bedrooms, listing_type):
                logger.debug(
                    'Skipping %s: price=%s size=%s beds=%s',
                    listing_id, price, size_m2, bedrooms,
                )
                continue

            status, status_source = await read_card_status(card, SELECTORS)

            results.append({
                'listing_id':        listing_id,
                'listing_url':       full_url,
                'source':            'duarte',
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

async def run_duarte_scraper() -> AsyncGenerator[dict, None]:
    """Async generator — yields one listing dict at a time."""
    seen_ids: set[str] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage'],
        )
        enricher = CostEnricher('duarte', browser, SELECTORS)
        await enricher.start()

        lt_configs = [
            ('sale', SALE_BASE),
            ('rent', RENT_BASE),
        ]

        for lt_name, base_path in lt_configs:
            max_pages = MAX_PAGES[lt_name]
            logger.info('── Starting Duarte %s scrape (max %d pages) ──', lt_name, max_pages)

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
