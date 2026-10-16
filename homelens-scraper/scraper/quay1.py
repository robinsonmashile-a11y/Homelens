"""
Quay 1 International Realty — Cape Town listing scraper.

Scrapes Cape Town residential for-sale and to-rent listings from quay1.co.za.

The site was rebuilt in Oct 2026 (old /results/residential/... URLs now redirect):
search pages are /properties?type=for-sale|to-rent&kind=residential, paginated
with &page=N (24 cards/page). Quay 1 is a Cape Town agency, so only Cape Town is
scraped.

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
    normalise_price,
    parse_price,
    normalise_suburb,
    compute_price_per_m2,
    validate_image_url,
    ForeignCurrencyError,
    assert_rand_price,
)
from scraper.cities import CITIES
from scraper.enrich import CostEnricher, read_card_status
from scraper.property_types import classify_property_type

logger = logging.getLogger('homelens.quay1')

# ═══════════════════════════════════════════════════════════════════════════
# SITE SELECTORS — listing status + monthly levy/rates.
# The only place to edit when this site's markup changes. Confirmed against
# saved pages in tests/fixtures/ (re-capture: scripts/capture_fixtures.py).
# ═══════════════════════════════════════════════════════════════════════════
SELECTORS = {
    # The rebuilt site has no status badges on result cards, so status comes from
    # the shared text fallback. Listing pages show levy/rates as plain text
    # ("RATES  R 658 p/m"), which the shared text fallback also handles.
    'status_badge': None,
    'status_map':   {},
    'cost_rows':    None,
    'description':  None,
}


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL       = 'https://www.quay1.co.za'
USER_AGENT     = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)

SALE_BASE_PATH = BASE_URL + '/properties?type=for-sale&kind=residential'
RENT_BASE_PATH = BASE_URL + '/properties?type=to-rent&kind=residential'

# Result cards: <a href="/for-sale|to-rent/{suburb}/{slug}-{code}"> with the
# price in a <p class="font-serif">. Nav / suburb-filter links have only 2 path
# segments and no price, so they never match.
CARD_SELECTOR = 'a[href^="/for-sale/"], a[href^="/to-rent/"]'

# sale ≈ 21 pages x 24, rent similar; generous margin — pagination stops on an
# empty page or when a page repeats.
MAX_PAGES: dict[str, int] = {
    'sale': 40,
    'rent': 60,
}

SCROLL_STEPS   = 12    # increments to trigger lazy-loaded cards
SCROLL_PAUSE   = 0.30  # seconds between scroll steps
PAGE_LOAD_WAIT = 3.0   # seconds after networkidle before scraping
RETRY_WAIT     = 15.0  # seconds to wait before retrying an empty page


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _page_url(base: str, page: int, search_id: str | None = None) -> str:
    """Page 1 is the base URL; page N appends &page=N."""
    return base if page == 1 else f'{base}&page={page}'


def _parse_beds(text: str) -> int | None:
    m = re.search(r'(\d+(?:\.\d+)?)\s*bed', text, re.I)
    if not m:
        return None
    # "0.5 BED" is how the site shows a studio; stored as 0 (see fix_suburbs).
    return int(float(m.group(1)))


def _parse_baths(text: str) -> int | None:
    m = re.search(r'(\d+)\s*bath', text, re.I)
    return int(m.group(1)) if m else None


def _parse_size(text: str) -> float | None:
    """Extract numeric size from strings like '80m²', '340 m²', '115m2'."""
    m = re.search(r'([\d\s,]+\.?\d*)\s*m', text)
    if m:
        raw = m.group(1).replace(' ', '').replace(',', '')
        try:
            val = float(raw)
            return val if 10 <= val <= 50_000 else None
        except ValueError:
            return None
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

    raw_card_count — total listing cards found.
    listing_dicts  — parsed listings from this page.
    """
    page = await context.new_page()
    try:
        logger.info('  [%s] Scraping page %d → %s', lt_name, page_num, url)
        await page.goto(url, wait_until='domcontentloaded', timeout=45_000)
        try:
            await page.wait_for_selector(CARD_SELECTOR, timeout=20_000)
        except Exception:
            pass                      # empty page (end of results) or slow load
        await asyncio.sleep(PAGE_LOAD_WAIT)
        await _scroll_to_load_all(page)

        raw_count = len(await page.eval_on_selector_all(CARD_SELECTOR, _CARD_JS))

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


_CARD_JS = """cards => cards.map(a => {
  const href = a.getAttribute('href') || '';
  if (href.split('/').filter(Boolean).length !== 3) return null;     // not a listing
  const priceEl = a.querySelector('p.font-serif');
  if (!priceEl) return null;
  const ps = [...a.querySelectorAll('div.p-5 > p')].map(p => p.innerText.trim());
  const img = a.querySelector('img');
  return {
    href, ps,
    src: img ? (img.getAttribute('src') || '') : '',
    srcset: img ? (img.getAttribute('srcset') || '') : '',
    text: a.innerText || '',
  };
}).filter(Boolean)"""

_CODE_RE = re.compile(r'-([a-z]{1,3}\d+)$')


def _pick_image(src: str, srcset: str) -> str | None:
    """Prefer the 640px rendition from srcset; fall back to src."""
    urls = [part.strip().split(' ')[0] for part in srcset.split(',') if part.strip()]
    best = next((u for u in urls if 'w_640' in u), urls[0] if urls else src)
    return validate_image_url(best or src)


async def _extract_cards(page: Page, listing_type: str) -> list[dict]:
    """
    Extract listing dicts from the result cards on a /properties page.

    Per card (div.p-5 > p, in order): price | title | "Type · Suburb" | stats
    ("2 BED · 1 BATH · 1 PARKING · 54 M²"; stats are absent on some listings).
    """
    from scraper.enrich import resolve_status

    cards = await page.eval_on_selector_all(CARD_SELECTOR, _CARD_JS)
    results: list[dict] = []

    for c in cards:
        try:
            href = c['href']
            path_parts = [x for x in href.split('/') if x]
            ltype = 'sale' if path_parts[0] == 'for-sale' else 'rent'

            m = _CODE_RE.search(path_parts[2])
            if not m:
                continue
            listing_id = f'quay1_{m.group(1)}'          # e.g. quay1_rl1493
            full_url   = BASE_URL + href

            ps         = c['ps'] + [''] * 4
            price_text = ps[0]
            title      = ps[1] or path_parts[2].replace('-', ' ').title()
            type_line  = ps[2]
            stats      = ps[3]

            suburb_text = type_line.split('·', 1)[1].strip() if '·' in type_line else path_parts[1]
            suburb_normalised = (
                normalise_suburb(re.sub(r'\s+', '-', suburb_text.strip().lower())) if suburb_text else None
            )

            price, price_status = parse_price(price_text)
            assert_rand_price(price_text)

            bedrooms  = _parse_beds(stats)
            bathrooms = _parse_baths(stats)
            size_m2   = _parse_size(stats.lower()) if re.search(r'm\s*[²2]', stats, re.I) else None

            status, status_source = resolve_status([], c['text'], SELECTORS)
            # The card's own type label ("Sectional Title · Camps Bay") first, title second.
            ptype = (classify_property_type(type_line.split('·', 1)[0])
                     or 'other')
            if ptype == 'other':
                ptype = classify_property_type(title)

            results.append({
                'listing_id':        listing_id,
                'listing_url':       full_url,
                'source':            'quay1',
                'listing_type':      ltype,
                'title':             clean_personal_info(title[:250]),
                'price':             price,
                'price_status':      price_status,
                'bedrooms':          bedrooms,
                'bathrooms':         bathrooms,
                'size_m2':           size_m2,
                'suburb_normalised': suburb_normalised,
                'description':       None,
                'price_per_m2':      compute_price_per_m2(price, size_m2),
                'image_url':         _pick_image(c['src'], c['srcset']),
                'is_active':         True,
                'property_type':     ptype,
                'listing_status':    status,
                'status_source':     status_source,
            })

        except ForeignCurrencyError:
            raise
        except Exception as e:
            logger.debug('Card parse error: %s', e)
            continue

    return results


# ---------------------------------------------------------------------------
# Main scraper generator
# ---------------------------------------------------------------------------

async def _run_quay1_city(city: dict) -> AsyncGenerator[dict, None]:
    """
    Async generator — yields one listing dict at a time.

    Iterates through Cape Town area pages for both sale and rent.
    Uses ?page=N pagination (with &advanced_search=UUID for sale).
    On a 0-card page, waits RETRY_WAIT seconds and retries once before
    treating the page as the end of results.
    Deduplicates globally to avoid yielding the same listing twice.
    """
    seen_ids: set[str] = set()

    if city['slug'] != 'cape-town':
        logger.info('quay1 only lists Cape Town properties — skipping %s', city['name'])
        return

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage'],
        )
        enricher = CostEnricher('quay1', browser, SELECTORS)
        await enricher.start()
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={'width': 1280, 'height': 900},
            locale='en-ZA',
            timezone_id='Africa/Johannesburg',
        )

        lt_configs = [
            ('sale', SALE_BASE_PATH, None),
            ('rent', RENT_BASE_PATH, None),
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


async def run_quay1_scraper() -> AsyncGenerator[dict, None]:
    """Scrapes every city in scraper.cities.CITIES, tagging each listing with its city."""
    for city in CITIES:
        logger.info('══ quay1 — %s ══', city['name'])
        async for listing in _run_quay1_city(city):
            listing['city'] = city['slug']
            yield listing
