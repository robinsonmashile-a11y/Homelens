"""
Engel & Völkers South Africa — Cape Town listing scraper.

Scrapes Cape Town residential for-sale and to-rent listings from engelvoelkers.com.

Card platform: React/Next.js SPA with data-testid selectors.
  card:      article[data-testid^="search-components_result-card_"]
  UUID:      extracted from data-testid (last segment after final "_")
  link:      first <a> → href "/za/en/exposes/{UUID}"
  title:     [data-testid="search-components_result-card_headline"]
  location:  [data-testid="search-components_result-card_location"]
             e.g. "Sea Point, Cape Town, Western Cape, South Africa"
             → first segment before "," = suburb
  price:     [data-testid="search-components_result-card_price"]
             e.g. "ZAR 4,850,000"
  bedrooms:  span[data-testid$="-bedrooms"]   → "3 Bedrooms"
  bathrooms: span[data-testid$="-bathrooms"]  → "2 Bathrooms"
  size:      span[data-testid$="-livingArea"]  → "~138 m² Living area"
             NOTE: plotSurface is ERF — we ignore it
  image:     first img[data-src] → uploadcare CDN URL

Pagination: ?page=N (page 1 has no param), with load-more button fallback.
  sale ≈ 135 listings / ~24 per page ≈ 6 pages  (max 10 with margin)
  rent ≈  60 listings / ~24 per page ≈ 3 pages  (max  6 with margin)
  Sale: /za/en/properties/res/sale/real-estate/western-cape/cape-town/
  Rent: /za/en/properties/res/rent/real-estate/western-cape/cape-town/

Consent wall: dismissed via button[id*="agree"] on first load.

POPIA: no personal info stored; all description text is sanitised.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import AsyncGenerator

from playwright.async_api import async_playwright, Page

from scraper.utils import (
    clean_personal_info,
    parse_price,
    normalise_suburb,
    compute_price_per_m2,
    validate_image_url,
)
from scraper.cities import CITIES, city_index, city_url
from scraper.enrich import CostEnricher, read_card_status

logger = logging.getLogger('homelens.engelvolkers')

# ═══════════════════════════════════════════════════════════════════════════
# SITE SELECTORS — listing status + monthly levy/rates.
# The only place to edit when this site's markup changes. Confirmed against
# saved pages in tests/fixtures/ (re-capture: scripts/capture_fixtures.py).
# ═══════════════════════════════════════════════════════════════════════════
SELECTORS = {
    # No status badge exists on E&V result cards (checked against a saved page
    # of 35 cards) — status comes from the shared fallback on the card text.
    'status_badge': None,
    'status_map':   {},
    # E&V has no structured levy/rates rows: agents type them into the
    # description ("… Levies R4,692.00 Rates R1,280.00"). cost_rows is None on
    # purpose, so costs come from the text fallback over the listing page and
    # its description; the run report does not flag this site for that.
    'text_only':    True,   # costs are never in structured rows; see above
    'cost_rows':    None,
    'cost_label':   None,
    'cost_value':   None,
    'description':  '[data-testid="expose-property-description-text"]',
}


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL   = 'https://www.engelvoelkers.com'
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)

SALE_URL = BASE_URL + '/za/en/properties/res/sale/real-estate/western-cape/cape-town/'
RENT_URL = BASE_URL + '/za/en/properties/res/rent/real-estate/western-cape/cape-town/'

# sale ≈ 135 / ~24 per page ≈ 6 pages  (max 10 with margin)
# rent ≈  60 / ~24 per page ≈ 3 pages  (max 6 with margin)
MAX_PAGES: dict[str, int] = {
    'sale': 10,
    'rent': 6,
}

SCROLL_STEPS   = 15   # more scroll steps to trigger lazy-loaded cards
SCROLL_PAUSE   = 0.35
PAGE_LOAD_WAIT = 6.0
LOAD_MORE_WAIT = 3.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_ev_price(text: str) -> tuple[int | None, str]:
    """
    Parse "ZAR 4,850,000" or "ZAR 47,000" → (price, price_status).
    Delegates to parse_price() so POA variants are detected consistently.
    """
    return parse_price(text)


def _parse_ev_suburb(location_text: str) -> str | None:
    """
    Extract suburb from location string.
    "Sea Point, Cape Town, Western Cape, South Africa" → "Sea Point"
    """
    if not location_text:
        return None
    first = location_text.split(',')[0].strip()
    return first if first else None


def _parse_beds(text: str) -> int | None:
    """Parse "3 Bedrooms" or "0.5 Bedrooms" → int."""
    m = re.search(r'([\d.]+)\s*bedroom', text, re.I)
    if m:
        try:
            return int(float(m.group(1)))
        except ValueError:
            return None
    return None


def _parse_baths(text: str) -> int | None:
    """Parse "2 Bathrooms" → int."""
    m = re.search(r'([\d.]+)\s*bathroom', text, re.I)
    if m:
        try:
            return int(float(m.group(1)))
        except ValueError:
            return None
    return None


def _parse_living_area(text: str) -> float | None:
    """
    Parse "~138 m² Living area" → 138.0.
    Returns None for plot surface or out-of-range values.
    """
    text_lc = text.lower()
    # Only parse livingArea attribute — skip anything mentioning plot/land/erf
    if any(kw in text_lc for kw in ('plot', 'land', 'erf', 'stand', 'lot')):
        return None
    m = re.search(r'~?\s*([\d,]+(?:\.\d+)?)\s*m', text)
    if m:
        raw = m.group(1).replace(',', '')
        try:
            val = float(raw)
            return val if 15 <= val <= 50_000 else None
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Page-level extraction
# ---------------------------------------------------------------------------

async def _dismiss_consent(page: Page) -> None:
    """Click GDPR/cookie consent button if present."""
    for sel in [
        'button[id*="agree"]',
        'button[id*="accept"]',
        '[data-testid*="accept"]',
        'button:has-text("Accept all")',
        'button:has-text("Accept")',
    ]:
        try:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                logger.debug('Dismissed consent via: %s', sel)
                await asyncio.sleep(2)
                return
        except Exception:
            continue


async def _scroll_to_load_all(page: Page) -> None:
    try:
        scroll_height = await page.evaluate('document.body.scrollHeight')
        step = max(400, scroll_height // SCROLL_STEPS)
        for i in range(1, SCROLL_STEPS + 1):
            await page.evaluate(f'window.scrollTo(0, {step * i})')
            await asyncio.sleep(SCROLL_PAUSE)
        await asyncio.sleep(1.0)
    except Exception as e:
        logger.debug('Scroll error (non-fatal): %s', e)


def _page_url(base: str, page_num: int) -> str:
    """E&V uses ?page=N (1-indexed, page 1 has no param)."""
    if page_num == 1:
        return base
    return f'{base}?page={page_num}'


async def _click_load_more(page) -> bool:
    """
    Try to click a 'Load more' / 'Show more' button.
    Returns True if clicked successfully.
    E&V may use either pagination links or a load-more button depending on
    the page size — try both approaches.
    """
    for sel in [
        'button:has-text("Load more")',
        'button:has-text("Show more")',
        'button:has-text("Mehr laden")',
        '[data-testid*="load-more"]',
        '[data-testid*="show-more"]',
        'a[data-testid*="pagination-next"]',
        'a[aria-label*="next" i]',
    ]:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                await btn.click()
                await asyncio.sleep(LOAD_MORE_WAIT)
                return True
        except Exception:
            continue
    return False


async def _fetch_listings(
    browser,
    base_url: str,
    lt_name: str,
    consent_dismissed: list[bool],
) -> list[dict]:
    """
    Load all listing pages for one type (sale or rent).
    Tries ?page=N pagination first; falls back to clicking 'Load more'.
    """
    all_results: list[dict] = []
    seen_ids: set[str] = set()
    max_pages = MAX_PAGES[lt_name]

    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={'width': 1280, 'height': 900},
        locale='en-ZA',
        timezone_id='Africa/Johannesburg',
    )
    page = await context.new_page()

    try:
        # Load page 1
        url = _page_url(base_url, 1)
        logger.info('  [%s] Loading page 1: %s', lt_name, url)
        await page.goto(url, wait_until='domcontentloaded', timeout=60_000)
        await asyncio.sleep(PAGE_LOAD_WAIT)

        # Dismiss consent wall on first load only
        if not consent_dismissed[0]:
            await _dismiss_consent(page)
            consent_dismissed[0] = True
            await asyncio.sleep(2)

        await _scroll_to_load_all(page)
        cards = await _extract_cards(page, lt_name)
        new_cards = [c for c in cards if c['listing_id'] not in seen_ids]
        for c in new_cards:
            seen_ids.add(c['listing_id'])
        all_results.extend(new_cards)
        logger.info('  [%s] Page 1: %d cards (%d new)', lt_name, len(cards), len(new_cards))

        # Try ?page=N pagination for subsequent pages
        for page_num in range(2, max_pages + 1):
            # First attempt: URL-based pagination
            paged_url = _page_url(base_url, page_num)
            if paged_url != url:
                try:
                    await page.goto(paged_url, wait_until='domcontentloaded', timeout=60_000)
                    await asyncio.sleep(PAGE_LOAD_WAIT)
                    await _scroll_to_load_all(page)
                    cards = await _extract_cards(page, lt_name)
                    new_cards = [c for c in cards if c['listing_id'] not in seen_ids]
                    if not new_cards:
                        logger.info('  [%s] No new cards on page %d — done', lt_name, page_num)
                        break
                    for c in new_cards:
                        seen_ids.add(c['listing_id'])
                    all_results.extend(new_cards)
                    logger.info(
                        '  [%s] Page %d: %d cards (%d new, total %d)',
                        lt_name, page_num, len(cards), len(new_cards), len(all_results),
                    )
                    url = paged_url
                    continue
                except Exception as e:
                    logger.debug('[%s] Page %d URL failed (%s) — trying load-more', lt_name, page_num, e)

            # Fallback: load-more button
            prev_count = len(all_results)
            clicked = await _click_load_more(page)
            if not clicked:
                logger.info('  [%s] No more pages or load-more button found — done', lt_name)
                break
            await _scroll_to_load_all(page)
            cards = await _extract_cards(page, lt_name)
            new_cards = [c for c in cards if c['listing_id'] not in seen_ids]
            if not new_cards:
                logger.info('  [%s] Load-more clicked but no new cards — done', lt_name)
                break
            for c in new_cards:
                seen_ids.add(c['listing_id'])
            all_results.extend(new_cards)
            logger.info(
                '  [%s] Load-more %d: %d new cards (total %d)',
                lt_name, page_num, len(new_cards), len(all_results),
            )

        logger.info('  [%s] Total extracted: %d', lt_name, len(all_results))
        return all_results

    finally:
        try:
            await context.close()
        except Exception:
            pass


async def _extract_cards(page: Page, listing_type: str) -> list[dict]:
    """
    Extract listing dicts from article[data-testid^="search-components_result-card_"] elements.

    Uses stable data-testid attributes throughout — not CSS classes (which are hashed).
    """
    articles = await page.query_selector_all(
        'article[data-testid^="search-components_result-card_"]'
    )
    if not articles:
        return []

    results: list[dict] = []

    for article in articles:
        try:
            # ── ID from data-testid ───────────────────────────────────────────
            dt = (await article.get_attribute('data-testid') or '').strip()
            # format: "search-components_result-card_{UUID}"
            uuid = dt.split('_result-card_')[-1] if '_result-card_' in dt else ''
            if not uuid:
                continue
            listing_id = f'engelvolkers_{uuid}'

            # ── URL ───────────────────────────────────────────────────────────
            link_el = await article.query_selector('a')
            if not link_el:
                continue
            href = (await link_el.get_attribute('href') or '').strip()
            if not href:
                continue
            full_url = href if href.startswith('http') else BASE_URL + href

            # ── Title ─────────────────────────────────────────────────────────
            title_el = await article.query_selector(
                '[data-testid="search-components_result-card_headline"]'
            )
            title = (await title_el.inner_text()).strip() if title_el else ''

            # ── Location → suburb ─────────────────────────────────────────────
            loc_el = await article.query_selector(
                '[data-testid="search-components_result-card_location"]'
            )
            location_text = (await loc_el.inner_text()).strip() if loc_el else ''
            suburb_raw    = _parse_ev_suburb(location_text)
            suburb_normalised = normalise_suburb(suburb_raw) if suburb_raw else None

            # ── Price ─────────────────────────────────────────────────────────
            price_el   = await article.query_selector(
                '[data-testid="search-components_result-card_price"]'
            )
            price_text  = (await price_el.inner_text()).strip() if price_el else ''
            price, price_status = _parse_ev_price(price_text)
            if not price_el:
                price_status = 'extraction_failed'

            # ── Beds / Baths / Living area ────────────────────────────────────
            bedrooms  = None
            bathrooms = None
            size_m2   = None

            # All attribute spans — data-testid ends with -bedrooms, -bathrooms, -livingArea
            attr_spans = await article.query_selector_all('span[data-testid]')
            for span in attr_spans:
                span_dt = (await span.get_attribute('data-testid') or '')
                text    = (await span.inner_text()).strip()
                if span_dt.endswith('-bedrooms') and bedrooms is None:
                    bedrooms = _parse_beds(text)
                elif span_dt.endswith('-bathrooms') and bathrooms is None:
                    bathrooms = _parse_baths(text)
                elif span_dt.endswith('-livingArea') and size_m2 is None:
                    size_m2 = _parse_living_area(text)
                # plotSurface is ERF — skip intentionally

            # ── Image — first img[data-src] (uploadcare CDN) ──────────────────
            raw_image = await article.evaluate('''art => {
                // Prefer data-src (lazy loaded uploadcare URL)
                const img1 = art.querySelector("img[data-src]");
                if (img1) {
                    const src = img1.getAttribute("data-src");
                    if (src && src.startsWith("http")) return src;
                }
                // Fallback to src
                const img2 = art.querySelector("img");
                if (img2) {
                    const src = img2.src || img2.getAttribute("src");
                    if (src && src.startsWith("http") && !src.startsWith("data:"))
                        return src;
                }
                return null;
            }''')
            image_url = validate_image_url(raw_image)

            status, status_source = await read_card_status(article, SELECTORS)

            results.append({
                'listing_id':        listing_id,
                'listing_url':       full_url,
                'source':            'engelvolkers',
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

async def _run_engelvolkers_city(city: dict) -> AsyncGenerator[dict, None]:
    """
    Async generator — yields one listing dict at a time.

    Iterates Cape Town sale and rent pages using ?page=N pagination
    with a load-more button fallback.  Deduplicates globally by listing_id.
    """
    seen_ids: set[str] = set()
    # Shared mutable flag so _fetch_listings can track consent across calls
    consent_dismissed: list[bool] = [False]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage'],
        )
        enricher = CostEnricher('engelvolkers', browser, SELECTORS)
        await enricher.start()

        lt_configs = [
            ('sale', city_url(SALE_URL, city)),
            ('rent', city_url(RENT_URL, city)),
        ]

        for lt_name, url in lt_configs:
            logger.info('── Starting Engel & Völkers %s scrape ──', lt_name)

            try:
                cards = await _fetch_listings(browser, url, lt_name, consent_dismissed)
            except Exception as e:
                logger.warning('[%s] Failed to load listings: %s', lt_name, e)
                continue

            new_cards = [c for c in cards if c['listing_id'] not in seen_ids]
            logger.info(
                '[%s] %d extracted | %d new',
                lt_name, len(cards), len(new_cards),
            )

            for card in new_cards:
                seen_ids.add(card['listing_id'])
                await enricher.enrich(card)
                yield card

        await enricher.close()
        await browser.close()


async def run_engelvolkers_scraper() -> AsyncGenerator[dict, None]:
    """Scrapes every city in scraper.cities.CITIES, tagging each listing with its city."""
    for city in CITIES:
        logger.info('══ engelvolkers — %s ══', city['name'])
        async for listing in _run_engelvolkers_city(city):
            listing['city'] = city['slug']
            yield listing
