"""
Just Property — Cape Town listing scraper.

Domain: just.property (justproperty.co.za redirects here)
Same Prop Data platform as Greeff / Harcourts / Sothebys / Quay1.
  card selector: a.property-card-sm  (card IS the anchor — same as Greeff)
  image:         data-images JSON on swiper element → first entry

URL structure (PropData standard):
  /results/residential/for-sale/cape-town/{suburb}/{type}/{id}/
  Suburb at position after 'cape-town' in path.

Pagination: ?page=N
  Sale:  ~22 listings → up to 5 pages
  Rent:  ~42 listings → up to 5 pages

Wait strategy: domcontentloaded + explicit wait_for_selector.

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

logger = logging.getLogger('homelens.justproperty')

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

BASE_URL   = 'https://www.just.property'
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)

SALE_BASE = BASE_URL + '/results/residential/for-sale/cape-town/'
RENT_BASE = BASE_URL + '/results/residential/to-let/cape-town/'

MAX_PAGES: dict[str, int] = {
    'sale': 5,
    'rent': 5,
}

SCROLL_STEPS   = 10
SCROLL_PAUSE   = 0.30
INITIAL_WAIT   = 3.0
CARD_WAIT_MS   = 20_000
POST_CARD_WAIT = 2.0
RETRY_WAIT     = 15.0

_SKIP_TYPES = ('commercial', 'industrial', 'retail', 'mixed-use', 'agricultural', 'vacant-land')
_MAX_BEDS   = 10


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
    if ct_idx is None:
        return None
    if ct_idx + 1 < len(parts):
        suburb = parts[ct_idx + 1]
        if suburb not in _SKIP_TYPES and not suburb.isdigit():
            return suburb
    return None


def _parse_beds(text: str) -> int | None:
    m = re.search(r'(\d+(?:\.\d+)?)\s*bed', text, re.I)
    if m:
        try:
            val = int(float(m.group(1)))
            return val if val <= _MAX_BEDS else None
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
    if any(kw in text_lc for kw in ('erf', 'land area', 'stand size', 'plot', 'lot size')):
        return None
    m = re.search(r'([\d\s,]+\.?\d*)\s*m', text)
    if m:
        raw = m.group(1).replace(' ', '').replace(',', '')
        try:
            val = float(raw)
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
        await page.goto(url, wait_until='domcontentloaded', timeout=60_000)
        await asyncio.sleep(INITIAL_WAIT)

        try:
            await page.wait_for_selector('a.property-card-sm', timeout=CARD_WAIT_MS)
        except Exception:
            logger.debug('[%s] page %d: wait_for_selector timed out (likely empty page)', lt_name, page_num)

        await asyncio.sleep(POST_CARD_WAIT)
        await _scroll_to_load_all(page)

        raw_cards = await page.query_selector_all('a.property-card-sm')
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
    Extract listing dicts from a.property-card-sm elements.
    Same PropData card structure as Greeff / Sothebys / Quay1.
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
            listing_id = f'justproperty_{data_id}'

            href = (await card.get_attribute('href') or '').strip()
            if not href:
                continue
            full_url = href if href.startswith('http') else BASE_URL + href

            if any(t in href.lower() for t in _SKIP_TYPES):
                continue

            # ── Suburb from URL ───────────────────────────────────────────────
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
                    size_m2 = _parse_size(text)

            # ── Image — data-images JSON ──────────────────────────────────────
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
                    ".card-image img", ".card-media img",
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

            # ── Validate ──────────────────────────────────────────────────────
            if not _validate_listing(price, size_m2, bedrooms, listing_type):
                logger.debug('Skipping %s: price=%s size=%s beds=%s',
                             listing_id, price, size_m2, bedrooms)
                continue

            status, status_source = await read_card_status(card, SELECTORS)

            results.append({
                'listing_id':        listing_id,
                'listing_url':       full_url,
                'source':            'justproperty',
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

async def _run_justproperty_city(city: dict) -> AsyncGenerator[dict, None]:
    """Async generator — yields one listing dict at a time."""
    seen_ids: set[str] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage'],
        )
        enricher = CostEnricher('justproperty', browser, SELECTORS)
        await enricher.start()

        lt_configs = [
            ('sale', city_url(SALE_BASE, city)),
            ('rent', city_url(RENT_BASE, city)),
        ]

        for lt_name, base_path in lt_configs:
            max_pages = MAX_PAGES[lt_name]
            logger.info('── Starting Just Property %s scrape (max %d pages) ──', lt_name, max_pages)

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


async def run_justproperty_scraper() -> AsyncGenerator[dict, None]:
    """Scrapes every city in scraper.cities.CITIES, tagging each listing with its city."""
    for city in CITIES:
        logger.info('══ justproperty — %s ══', city['name'])
        async for listing in _run_justproperty_city(city):
            listing['city'] = city['slug']
            yield listing
