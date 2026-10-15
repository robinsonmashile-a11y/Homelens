"""
Pam Golding Properties — Cape Town listing scraper.

Uses Pam Golding's Cape Town area search pages (area ID 3080) to return
only Cape Town listings, then filters to confirmed Cape Town suburbs via
SUBURB_MAP normalisation.

POPIA: no personal info stored; all description text is sanitised.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import json
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
    SUBURB_MAP,
)
from scraper.cities import CITIES
from scraper.enrich import CostEnricher, read_card_status

logger = logging.getLogger('homelens.pamgolding')

# ═══════════════════════════════════════════════════════════════════════════
# SITE SELECTORS — listing status + monthly levy/rates.
# The only place to edit when this site's markup changes. Confirmed against
# saved pages in tests/fixtures/ (re-capture: scripts/capture_fixtures.py).
# ═══════════════════════════════════════════════════════════════════════════
SELECTORS = {
    # Search-result card: <ul class="results__item-banners">
    #   <li class="results__item-banner-item"> Under offer</li> …
    # (also "On Show", "Price reduced", "Exclusive mandate" — ignored).
    # NB: the results page has a "Sold" filter button — outside any card, so
    # it is never read; status is only taken from inside a card.
    'status_badge': '.results__item-banner-item',
    'status_map':   {},
    # Listing page, "Expenses" block — two <div>s per figure:
    #   <div class="price-display__container">
    #     <div>Rates and Taxes:&nbsp;</div><div>R 4,986 per month</div></div>
    #   <div class="price-display__container">
    #     <div>Levies:&nbsp;</div><div>R 4,851 per month</div></div>
    'cost_rows':    '.cashflow__container .price-display__container',
    'cost_label':   'div:nth-of-type(1)',
    'cost_value':   'div:nth-of-type(2)',
    'description':  None,
}


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL   = 'https://www.pamgolding.co.za'
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)

# Cape Town area ID 3080 — returns CT-only listings.
# Pagination: page 1 = base URL, page 2+ = base?page=N
SALE_BASE_URL = BASE_URL + '/property-search/properties-for-sale-cape-town/3080'
RENT_BASE_URL = BASE_URL + '/property-search/properties-to-rent-cape-town/3080?sort=1'

# Johannesburg: Pam Golding only exposes it as regional areas. "Johannesburg
# North" (ID 624, covers the northern suburbs: Sandton, Randburg, ...) is the
# one confirmed to work for both sale and rent. Add further areas here
# (e.g. Johannesburg South / East / West) once their IDs are known.
CITY_SEARCH_URLS: dict[str, list[tuple[str, str]]] = {
    'cape-town': [('sale', SALE_BASE_URL), ('rent', RENT_BASE_URL)],
    'johannesburg': [
        ('sale', BASE_URL + '/property-search/properties-for-sale-johannesburg-north-gauteng/624'),
        ('rent', BASE_URL + '/property-search/properties-to-rent-johannesburg-north-gauteng/624'),
    ],
}

# The site calls /api/geo on load and, for visitors outside ZA (e.g. GitHub's US
# runners), switches every price to the visitor's currency ("$ 129,303"). Answer
# that call as if we were in South Africa so prices stay in rands.
_GEO_ZA = {
    'status': 'success', 'country': 'South Africa', 'countryCode': 'ZA',
    'currency': 'ZAR',
}


async def force_rand_pricing(context, geo: dict = _GEO_ZA) -> None:
    await context.route(
        '**/api/geo*',
        lambda route: route.fulfill(
            status=200, content_type='application/json', body=json.dumps(geo),
        ),
    )


PAGE_LOAD_ATTEMPTS = 3


# Set by _run_pamgolding_city. Cape Town results are filtered against the CT
# suburb whitelist; other cities trust the city-scoped search and take the
# suburb straight from the URL slug.
_active_city: str = 'cape-town'

# sale ≈ 537 listings / 20 per page ≈ 27 pages (set max to 35 with margin)
# rent ≈ 200+ listings / 20 per page ≈ 10+ pages (set max to 20)
MAX_PAGES: dict[str, int] = {
    'sale': 35,
    'rent': 20,
}

SCROLL_STEPS    = 15    # increments to trigger lazy-loaded cards
SCROLL_PAUSE    = 0.35  # seconds between steps
PAGE_LOAD_WAIT  = 3.5   # seconds after networkidle
RETRY_WAIT      = 15.0  # seconds to wait before retrying an empty page

# Canonical Cape Town suburb slugs — secondary filter for any edge-case results.
_CT_SLUG_SET: frozenset[str] = frozenset(SUBURB_MAP.values())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _page_url(base: str, page: int) -> str:
    """Return the URL for the given page number.

    Page 1 is the base URL with no params.
    Page 2+ appends page=N, using & if the base already has a query string.
    """
    if page == 1:
        return base
    sep = '&' if '?' in base else '?'
    return f'{base}{sep}page={page}'


def _parse_beds(text: str) -> int | None:
    m = re.search(r'(\d+)\s*bed', text, re.I)
    return int(m.group(1)) if m else None


def _parse_baths(text: str) -> int | None:
    m = re.search(r'(\d+)\s*bath', text, re.I)
    return int(m.group(1)) if m else None


def _parse_size(text: str) -> float | None:
    """
    Extract floor/building size from strings like:
      'Building 340 m²'  'Floor 115m²'  '280 m²'
    Returns None for ERF/land/plot area labels — those are plot sizes,
    not floor sizes, and must never be used for price-per-m² calculations.
    """
    text_lc = text.lower()
    # Reject ERF/land area labels
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


def _extract_suburb_from_title(title: str) -> str | None:
    """
    Extract raw suburb name from Pam Golding title patterns:
      '2 bedroom apartment for sale in Sea Point'
      '3 bed house to rent in Camps Bay'
      'Luxury villa for sale in Clifton - AS1611837'
    """
    for pattern in (
        r'\bin\s+([A-Za-z\s]+?)(?:\s+for\s+sale|\s+to\s+let|\s+for\s+rent)?$',
        r'\bin\s+([A-Za-z\s]+?)(?:\s*[-–]\s*|\s*,|\s*$)',
    ):
        m = re.search(pattern, title, re.I)
        if m:
            return m.group(1).strip()
    return None


def _is_cape_town_slug(slug: str | None) -> bool:
    """Return True only if the slug maps to a known Cape Town suburb."""
    return slug in _CT_SLUG_SET


def _suburb_from_href(href: str) -> str | None:
    """
    Extract suburb from the Pam Golding URL title slug.

    URL format:
      /property-details/apartment-for-sale-in-sea-point/as1657238
      /property-details/3-bedroom-house-to-rent-in-camps-bay/kw1693426

    Strategy: split the slug on hyphens, try progressively shorter
    word sub-sequences starting after the word "in" (if present),
    then fall back to all sub-sequences. Only returns a slug that
    exists in SUBURB_MAP values (known CT suburbs).
    """
    parts = href.rstrip('/').split('/')
    if len(parts) < 3:
        return None
    # Second-to-last segment is the title slug; last is the ref code
    slug = parts[-2]
    words = slug.split('-')
    known = _CT_SLUG_SET  # canonical slug values from SUBURB_MAP

    if _active_city != 'cape-town':
        # No whitelist for other cities: the suburb is whatever follows "in".
        if 'in' not in words:
            return None
        after = words[len(words) - words[::-1].index('in'):]  # after the LAST "in"
        return normalise_suburb('-'.join(after)) if after else None

    # Try subsequences after the word "in"
    if 'in' in words:
        in_idx = words.index('in')
        after = words[in_idx + 1:]
        for length in range(len(after), 0, -1):
            candidate = '-'.join(after[:length])
            result = normalise_suburb(candidate)
            if result in known:
                return result

    # Try all word subsequences (longer first to prefer more specific matches)
    max_len = min(len(words), 4)
    for length in range(max_len, 0, -1):
        for start in range(len(words) - length + 1):
            candidate = '-'.join(words[start:start + length])
            result = normalise_suburb(candidate)
            if result in known:
                return result

    return None


def _listing_id_from_href(href: str) -> str:
    """
    Build a stable listing_id from the Pam Golding URL path.
    URL format: /property-details/{title-slug}/{ref-code}
    e.g. /property-details/apartment-for-sale-sea-point/as1657238
    Ref code looks like: as1657238, kw1693426, 1mdm1740201
    """
    parts = href.rstrip('/').split('/')
    ref   = parts[-1].lower() if parts else ''
    if re.match(r'^[a-z0-9]{6,}$', ref):
        return f'pamgolding_{ref}'
    return 'pamgolding_' + hashlib.md5(href.encode()).hexdigest()[:12]


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
    Load a single search results page and return (raw_card_count, ct_listing_dicts).

    raw_card_count — total article.results__item elements found (before CT filter).
    ct_listing_dicts — listings that pass the Cape Town suburb filter.
    """
    page = await context.new_page()
    try:
        logger.info('  [%s] Scraping page %d → %s', lt_name, page_num, url)
        await page.goto(url, wait_until='networkidle', timeout=60_000)
        await asyncio.sleep(PAGE_LOAD_WAIT)
        await _scroll_to_load_all(page)

        raw_cards = await page.query_selector_all('article.results__item')
        raw_count = len(raw_cards)

        ct_listings = await _extract_cards(page, lt_name)
        logger.info(
            '  [%s] Page %d: %d total cards, %d CT listings extracted',
            lt_name, page_num, raw_count, len(ct_listings),
        )
        return raw_count, ct_listings

    finally:
        try:
            await page.close()
        except Exception:
            pass


async def _extract_cards(page: Page, listing_type: str) -> list[dict]:
    """
    Extract all listing dicts from article.results__item cards.
    Returns only listings whose suburb normalises to a known Cape Town slug.
    """
    cards = await page.query_selector_all('article.results__item')
    if not cards:
        return []

    results: list[dict] = []

    for card in cards:
        try:
            # ── Link + Title ────────────────────────────────────────────────
            link_el = await card.query_selector('a.results__item-heading-link')
            if not link_el:
                continue

            href = (await link_el.get_attribute('href') or '').strip()
            if not href:
                continue

            title = (
                await link_el.get_attribute('title')
                or await link_el.inner_text()
                or ''
            ).strip()
            if not title:
                continue

            full_url = BASE_URL + href if href.startswith('/') else href

            # ── Suburb ──────────────────────────────────────────────────────
            # Primary: extract from title using "in {suburb}" pattern
            raw_suburb        = _extract_suburb_from_title(title)
            suburb_normalised = normalise_suburb(raw_suburb) if raw_suburb else None
            # Fallback: extract from URL slug when title doesn't have "in"
            # (e.g. "Sea Point | Modern Apartment", "Rondebosch — Family Home")
            if not suburb_normalised:
                suburb_normalised = _suburb_from_href(href)

            # Skip listings with no identifiable suburb
            if not suburb_normalised:
                continue

            # ── Price ───────────────────────────────────────────────────────
            price_el = (
                await card.query_selector('.results__item-price')
                or await card.query_selector('.price-display__container')
                or await card.query_selector('.price-display')
            )
            price_text  = (await price_el.inner_text()).strip() if price_el else ''
            assert_rand_price(price_text)
            price, price_status = parse_price(price_text)
            if not price_el:
                price_status = 'extraction_failed'

            # ── Beds / Baths / Size ─────────────────────────────────────────
            body_items = await card.query_selector_all('li.results__item-body-item')
            bedrooms   = None
            bathrooms  = None
            size_m2    = None

            for item_el in body_items:
                text    = (await item_el.inner_text()).strip()
                text_lc = text.lower()
                if 'bed' in text_lc and bedrooms is None:
                    bedrooms = _parse_beds(text)
                elif 'bath' in text_lc and bathrooms is None:
                    bathrooms = _parse_baths(text)
                elif 'm' in text_lc and 'm²' in text and size_m2 is None:
                    size_m2 = _parse_size(text)

            # ── Description ─────────────────────────────────────────────────
            desc_el     = await card.query_selector('.results__item-body-description')
            raw_desc    = (await desc_el.inner_text()).strip() if desc_el else ''
            description = clean_personal_info(raw_desc)[:2000] if raw_desc else None

            # ── Listing ID ──────────────────────────────────────────────────
            listing_id = _listing_id_from_href(href)

            # ── Image — card thumbnail ───────────────────────────────────────
            raw_image = await card.evaluate('''card => {
                const selectors = [
                    ".results__item-image img",
                    ".results__image img",
                    "img[class*=\\"result\\"]",
                    "img[class*=\\"property\\"]",
                    "img[class*=\\"listing\\"]",
                    "img[loading=\\"lazy\\"]",
                    "img"
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
                'source':            'pamgolding',
                'listing_type':      listing_type,
                'title':             clean_personal_info(title[:250]),
                'price':             price,
                'price_status':      price_status,
                'bedrooms':          bedrooms,
                'bathrooms':         bathrooms,
                'size_m2':           size_m2,
                'suburb_normalised': suburb_normalised,
                'description':       description,
                'price_per_m2':      compute_price_per_m2(price, size_m2),
                'image_url':         image_url,
                'is_active':         True,
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

async def _run_pamgolding_city(city: dict) -> AsyncGenerator[dict, None]:
    """
    Async generator — yields one listing dict at a time.

    Iterates through Cape Town area pages (area ID 3080) for both sale and rent.
    Uses ?page=N pagination. On a 0-card page, waits RETRY_WAIT seconds and
    retries once before treating the page as the end of results.
    Deduplicates globally to avoid yielding the same listing twice.
    """
    global _active_city
    _active_city = city['slug']
    seen_ids: set[str] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage'],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={'width': 1280, 'height': 900},
            locale='en-ZA',
            timezone_id='Africa/Johannesburg',
        )
        await force_rand_pricing(context)
        # Share the context so the enricher's detail pages also see rands.
        enricher = CostEnricher('pamgolding', context, SELECTORS)
        await enricher.start()

        for lt_name, base_url in CITY_SEARCH_URLS[city['slug']]:
            max_pages = MAX_PAGES[lt_name]
            logger.info('── Starting %s scrape (max %d pages) ──', lt_name, max_pages)

            for page_num in range(1, max_pages + 1):
                url = _page_url(base_url, page_num)

                # ── Load page ────────────────────────────────────────────────
                try:
                    for attempt in range(1, PAGE_LOAD_ATTEMPTS + 1):
                        try:
                            raw_count, cards = await _fetch_page_cards(
                                context, url, lt_name, page_num
                            )
                            break
                        except ForeignCurrencyError:
                            raise
                        except Exception as e:
                            if attempt == PAGE_LOAD_ATTEMPTS:
                                raise
                            logger.warning(
                                '[%s] page %d load failed (attempt %d/%d): %s — retrying',
                                lt_name, page_num, attempt, PAGE_LOAD_ATTEMPTS, e,
                            )
                            await asyncio.sleep(RETRY_WAIT)
                except ForeignCurrencyError:
                    raise
                except Exception as e:
                    logger.warning(
                        '[%s] page %d failed to load: %s — stopping this type',
                        lt_name, page_num, e,
                    )
                    break

                # ── Retry on empty page (possible soft block) ─────────────────
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
                    except ForeignCurrencyError:
                        raise
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

                # ── Filter to unseen listings ─────────────────────────────────
                new_cards = [c for c in cards if c['listing_id'] not in seen_ids]

                logger.info(
                    '[%s] page %3d: %2d raw cards | %2d CT | %2d new  (total seen: %d)',
                    lt_name, page_num,
                    raw_count, len(cards), len(new_cards),
                    len(seen_ids) + len(new_cards),
                )

                if not new_cards and raw_count > 0:
                    # All CT cards on this page already seen — likely cycled back
                    logger.info(
                        '[%s] Stopping at page %d — all CT listings already seen (pagination cycle)',
                        lt_name, page_num,
                    )
                    break

                for card in new_cards:
                    seen_ids.add(card['listing_id'])
                    await enricher.enrich(card)
                    yield card

        await enricher.close()
        await browser.close()


async def run_pamgolding_scraper() -> AsyncGenerator[dict, None]:
    """Scrapes every city in scraper.cities.CITIES, tagging each listing with its city."""
    for city in CITIES:
        logger.info('══ pamgolding — %s ══', city['name'])
        async for listing in _run_pamgolding_city(city):
            listing['city'] = city['slug']
            yield listing
