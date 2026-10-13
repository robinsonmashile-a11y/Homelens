"""
Async Playwright scraper for RE/MAX South Africa — Cape Town property listings.

HTML structure confirmed from live page inspection (May 2026):
  - Cards: div.shadow-xl.bg-white inside a CSS grid
  - All data embedded in HTML at page load (Next.js SSR) — no extra API calls needed
  - Bedrooms/bathrooms identified via img[alt="Number of X"] → nextElementSibling
  - Suburb extracted from URL path segment [4]
  - Listing ID: trailing numeric segment of URL slug
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import AsyncGenerator

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
)

from .utils import clean_personal_info, normalise_price, parse_price, normalise_suburb, validate_image_url
from .enrich import apply_costs, costs_from_texts, read_card_status
from .extractors import cost_in_bounds

from scraper.cities import CITIES, city_url

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# SITE SELECTORS — listing status + monthly levy/rates.
# The only place to edit when this site's markup changes. Confirmed against
# saved pages in tests/fixtures/ (re-capture: scripts/capture_fixtures.py).
# ═══════════════════════════════════════════════════════════════════════════
SELECTORS = {
    # Result card: <span class="… bg-remax-red/80 …">Offer Made</span> (also
    # "New", "On Show", "Price Reduced", "Sole Mandate" — ignored). The page
    # also has "Sold"/"Offer Made" filter buttons, outside any card.
    'status_badge': 'span[class*="bg-remax-red"], span[class*="bg-remax-blue"]',
    'status_map':   {'offer made': 'under_offer'},
    # Listing page: the "Monthly Costs" accordion is collapsed (its rows are not
    # in the DOM), but the Next.js payload carries the figures — quotes are
    # backslash-escaped inside the script string:  \\"levy\\":2325.32,\\"rates\\":574.52
    # 0 means "not set" and is discarded by the bounds check. "special_levies"
    # is a one-off charge, NOT the monthly levy, and is deliberately not read.
    'cost_json': {
        'levy':  r'\\?"levy\\?"\s*:\s*(\d+(?:\.\d+)?)',
        'rates': r'\\?"rates\\?"\s*:\s*(\d+(?:\.\d+)?)',
    },
    'description': None,
}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15',
]

BASE_URLS: dict[str, str] = {
    'sale': 'https://www.remax.co.za/property-for-sale-south-africa/western-cape/cape-town',
    'rent': 'https://www.remax.co.za/property-to-rent-south-africa/western-cape/cape-town',
}

# Suburbs scraped in addition to the base Cape Town page
SUBURBS = [
    'sea-point', 'green-point', 'observatory', 'gardens', 'woodstock',
    'de-waterkant', 'bo-kaap', 'salt-river', 'tamboerskloof',
    'claremont', 'rondebosch', 'newlands', 'mowbray', 'wynberg',
    'mouille-point', 'three-anchor-bay', 'fresnaye', 'bantry-bay',
    'clifton', 'camps-bay', 'oranjezicht', 'vredehoek', 'higgovale', 'city-bowl',
]

MIN_DELAY   = 3.0
MAX_DELAY   = 6.0
DETAIL_WAIT = 4.0
MAX_RETRIES = 2
MAX_PAGES   = 30


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _random_delay() -> None:
    await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


def _is_blocked(page: Page) -> bool:
    url = page.url.lower()
    return any(k in url for k in ['captcha', 'blocked', 'robot', 'challenge', 'access-denied'])


def _extract_id_from_url(url: str) -> str | None:
    """Pull the trailing numeric listing ID from a RE/MAX URL slug."""
    m = re.search(r'-(\d{5,})(?:/|$|\?)', url)
    return m.group(1) if m else None


def _extract_suburb_from_url(url: str) -> str:
    """
    RE/MAX URL format:
      /property-for-sale-south-africa/western-cape/cape-town/{suburb}/{slug}
    Path segment [4] (0-indexed after split on '/') is the suburb slug.
    """
    parts = url.rstrip('/').split('/')
    # parts[0]='' [1]=property-for-sale... [2]=western-cape [3]=cape-town [4]=suburb [5]=slug
    return parts[4] if len(parts) > 4 else ''


def _build_page_url(base: str, page_num: int) -> str:
    if page_num <= 1:
        return base
    sep = '&' if '?' in base else '?'
    return f'{base}{sep}page={page_num}'


# ---------------------------------------------------------------------------
# Card extraction — confirmed selectors from live HTML
# ---------------------------------------------------------------------------

async def _get_icon_sibling_text(card, alt_text: str):
    """
    RE/MAX encodes bed/bath/garage counts as:
      <img alt="Number of bedrooms"> <div>3</div>
    Use JS evaluation to get the text of the element right after the icon img.
    """
    try:
        result = await card.evaluate(f'''card => {{
            const img = card.querySelector('img[alt="{alt_text}"]');
            if (!img) return null;
            const next = img.nextElementSibling;
            return next ? next.textContent.trim() : null;
        }}''')
        return result
    except Exception:
        return None


async def _extract_cards(page: Page, listing_type: str) -> list[dict]:
    """
    Extract all listing cards from the current RE/MAX results page.
    Cards are div.shadow-xl.bg-white elements inside a CSS grid.
    """
    cards: list[dict] = []

    # Primary selector confirmed from live HTML inspection
    card_els = await page.query_selector_all('div.shadow-xl.bg-white')

    # Fallback: any div containing a property-sale/rent href
    if not card_els:
        all_links = await page.query_selector_all(
            'a[href*="property-for-sale-south-africa"], a[href*="property-to-rent-south-africa"]'
        )
        # Walk up to find a common card wrapper
        for link in all_links[:1]:
            parent = await link.evaluate_handle('el => el.parentElement?.parentElement')
            if parent:
                parent_tag = await parent.evaluate('el => el.tagName')
                if parent_tag:
                    card_els = await page.query_selector_all(
                        f'{parent_tag.lower()}[class*="rounded"]'
                    )
                    break

    logger.debug('Card elements found: %d', len(card_els))
    seen_ids: set[str] = set()

    for card in card_els:
        try:
            # ---- URL ----
            link_el = await card.query_selector('a[href]')
            href = await link_el.get_attribute('href') if link_el else None
            if not href or 'property-for-sale' not in href and 'property-to-rent' not in href:
                continue
            listing_url = f'https://www.remax.co.za{href}' if href.startswith('/') else href

            listing_id = _extract_id_from_url(listing_url)
            if not listing_id or listing_id in seen_ids:
                continue
            seen_ids.add(listing_id)

            # ---- Suburb from URL ----
            suburb_slug = _extract_suburb_from_url(href)
            suburb_normalised = normalise_suburb(suburb_slug) or suburb_slug

            # ---- Title ----
            # RE/MAX: <div class="text-black text-sm font-bold mb-1">...</div>
            title_el = await card.query_selector('div.font-bold.mb-1')
            title = (await title_el.inner_text()).strip() if title_el else ''

            # ---- Address (has title attribute) ----
            addr_el = await card.query_selector('[title]')
            suburb_raw = await addr_el.get_attribute('title') if addr_el else suburb_slug

            # ---- Price ----
            # RE/MAX: <div class="text-[15px] font-bold whitespace-nowrap">R 45,000,000</div>
            price_el = await card.query_selector('div.font-bold.whitespace-nowrap')
            price_raw = (await price_el.inner_text()).strip() if price_el else ''
            price, price_status = parse_price(price_raw)
            if not price_el:
                price_status = 'extraction_failed'

            # ---- Bedrooms / Bathrooms / Garages via icon alt text ----
            bedrooms: int | None = None
            bathrooms: float | None = None
            has_parking: bool | None = None

            beds_txt = await _get_icon_sibling_text(card, 'Number of bedrooms')
            if beds_txt:
                try:
                    bedrooms = int(beds_txt)
                except ValueError:
                    pass

            bath_txt = await _get_icon_sibling_text(card, 'Number of bathrooms')
            if bath_txt:
                try:
                    bathrooms = float(bath_txt)
                except ValueError:
                    pass

            garage_txt = await _get_icon_sibling_text(card, 'Number of garages')
            if garage_txt:
                try:
                    has_parking = int(garage_txt) > 0
                except ValueError:
                    has_parking = False

            status, status_source = await read_card_status(card, SELECTORS)

            card_dict: dict = {
                'listing_id':        listing_id,
                'source':            'remax',
                'listing_type':      listing_type,
                'title':             title,
                'price':             price,
                'price_status':      price_status,
                'suburb':            suburb_raw or suburb_slug,
                'suburb_normalised': suburb_normalised,
                'listing_url':       listing_url,
                'is_active':         True,
                'listing_status':    status,
                'status_source':     status_source,
            }
            if bedrooms is not None:
                card_dict['bedrooms'] = bedrooms
            if bathrooms is not None:
                card_dict['bathrooms'] = bathrooms
            if has_parking is not None:
                card_dict['has_parking'] = has_parking

            cards.append(card_dict)

        except Exception as e:
            logger.debug('Card extraction error: %s', e)
            continue

    return cards


# ---------------------------------------------------------------------------
# Detail page extraction
# ---------------------------------------------------------------------------

def _read_costs(page_html: str, page_text: str, description: str | None) -> dict:
    """Levy/rates from the page's JSON payload ('site'), else the text fallback."""
    found: dict[str, float] = {}
    for kind, pattern in SELECTORS['cost_json'].items():
        m = re.search(pattern, page_html)
        if m:
            amount = cost_in_bounds(kind, float(m.group(1)))
            if amount is not None:
                found[kind] = amount
    if found:
        return {
            'levy_monthly':                  found.get('levy'),
            'rates_monthly':                 found.get('rates'),
            'levies_rates_combined_monthly': None,
            'costs_source':                  'site',
            'costs_raw_snippet':             ' '.join(f'{k}={v}' for k, v in found.items()),
        }
    return costs_from_texts(page_text, description)


async def _extract_detail(page: Page, listing_type: str) -> dict:
    """
    Visit a RE/MAX listing detail page and return enriched fields.
    Looks for description text and any size_m2 / feature data not on the card.
    """
    detail: dict = {}
    try:
        # Give JS time to render
        await asyncio.sleep(2)

        # Description — RE/MAX detail pages wrap it in a div with prose-like classes
        # Try multiple candidate selectors
        for sel in [
            'div[class*="description"]',
            'div[class*="Description"]',
            'section[class*="description"]',
            'div[class*="overview"]',
            'div[class*="about"]',
            'p[class*="description"]',
        ]:
            desc_el = await page.query_selector(sel)
            if desc_el:
                raw = (await desc_el.inner_text()).strip()
                if len(raw) > 30:
                    detail['description'] = clean_personal_info(raw)
                    break

        # If no specific selector worked, grab all paragraph text
        if 'description' not in detail:
            paras = await page.query_selector_all('main p, article p, section p')
            combined = ' '.join([
                (await p.inner_text()).strip()
                for p in paras
                if len((await p.inner_text()).strip()) > 40
            ])
            if combined:
                detail['description'] = clean_personal_info(combined[:3000])

        # Floor size — extract from Next.js embedded JSON.
        # RE/MAX pages contain both "floor_area" and "land_area" in a JSON blob.
        # We ONLY use floor_area; land_area (ERF/plot size) is never used.
        page_html = await page.content()
        floor_m = re.search(r'"floor_area"\s*:\s*(\d+(?:\.\d+)?)', page_html)
        if floor_m:
            try:
                val = float(floor_m.group(1))
                if 15 <= val <= 2000:
                    detail['size_m2'] = val
            except ValueError:
                pass

        # Solar / garden / parking keywords in full page text
        page_text = await page.evaluate('() => document.body.innerText')
        lower_text = page_text.lower()
        if any(w in lower_text for w in ('solar', 'inverter', 'photovoltaic', 'pv panel', 'solar geyser')):
            detail['has_solar'] = True
        if any(w in lower_text for w in ('garden', 'yard', 'braai area', 'landscaped')):
            detail['has_garden'] = True
        if 'parking' in lower_text or 'garage' in lower_text or 'carport' in lower_text:
            detail['has_parking'] = True

        # Monthly levy / rates — sale listings only (rental levies aren't shown).
        # Site payload first, then the shared text fallback. The page is already
        # loaded for every listing, so there is no extra visit.
        if listing_type == 'sale':
            apply_costs(detail, _read_costs(page_html, page_text, detail.get('description')))

        # Image — og:image is an HTML meta tag, available even when image requests are aborted
        raw_image = await page.evaluate('''() => {
            const meta = document.querySelector('meta[property="og:image"]');
            return meta ? meta.content : null;
        }''')
        detail['image_url'] = validate_image_url(raw_image)

    except Exception as e:
        logger.debug('Detail extraction error: %s', e)

    return detail


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

async def _has_next_page(page: Page, page_num: int) -> bool:
    """Check if there's a next page available."""
    try:
        # <link rel="next"> in head
        next_link = await page.query_selector('link[rel="next"]')
        if next_link:
            return True
        # Visible next button / link
        next_btn = await page.query_selector(
            f'a[href*="page={page_num + 1}"], '
            'a[aria-label*="next" i], '
            'button[aria-label*="next" i]'
        )
        if next_btn and await next_btn.is_visible():
            return True
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Scrape one URL with pagination
# ---------------------------------------------------------------------------

async def _scrape_url(
    context: BrowserContext,
    listing_type: str,
    base_url: str,
    label: str,
) -> AsyncGenerator[dict, None]:
    """Scrape all pages at a single base URL. Yields listing dicts."""
    page = await context.new_page()
    page_num = 0
    seen_ids: set[str] = set()

    while page_num < MAX_PAGES:
        page_num += 1
        url = _build_page_url(base_url, page_num)
        logger.info('[%s/%s] page %d: %s', label, listing_type, page_num, url)

        # Load with retries
        # Use 'load' not 'networkidle' — remax now has a map+list layout and the
        # map continuously loads tile images, so networkidle never fires.
        loaded = False
        for attempt in range(1, MAX_RETRIES + 2):
            try:
                await page.goto(url, wait_until='load', timeout=60_000)
                await asyncio.sleep(3.0)   # let Next.js hydrate and cards render
                loaded = True
                break
            except PWTimeout:
                logger.warning('Timeout page %d attempt %d', page_num, attempt)
                await asyncio.sleep(4 * attempt)

        if not loaded:
            logger.error('Failed to load page %d for %s — skipping', page_num, label)
            break

        if _is_blocked(page):
            logger.error('Blocked on %s page %d', label, page_num)
            break

        cards = await _extract_cards(page, listing_type)
        logger.info('%d cards on page %d', len(cards), page_num)

        if not cards:
            logger.info('No cards on page %d — end of results for %s', page_num, label)
            break

        # Deduplicate across pages
        new_cards = [c for c in cards if c['listing_id'] not in seen_ids]
        for c in new_cards:
            seen_ids.add(c['listing_id'])

        if not new_cards:
            logger.info('All %d cards already seen — stopping pagination', len(cards))
            break

        # Fetch detail pages
        detail_page = await context.new_page()
        for card in new_cards:
            for attempt in range(1, MAX_RETRIES + 2):
                try:
                    await detail_page.goto(
                        card['listing_url'], wait_until='load', timeout=60_000
                    )
                    await asyncio.sleep(DETAIL_WAIT + random.uniform(0, 2))

                    if _is_blocked(detail_page):
                        yield card
                        break

                    detail = await _extract_detail(detail_page, listing_type)
                    merged = {**card}
                    for k, v in detail.items():
                        if k not in merged or merged[k] is None:
                            merged[k] = v

                    # Price per m²
                    if merged.get('price') and merged.get('size_m2'):
                        from .utils import compute_price_per_m2
                        merged['price_per_m2'] = compute_price_per_m2(
                            merged['price'], merged['size_m2']
                        )

                    yield merged
                    break

                except PWTimeout:
                    if attempt > MAX_RETRIES:
                        logger.warning('Max retries for %s — saving card data', card['listing_url'])
                        yield card
                    else:
                        await asyncio.sleep(4 * attempt)
                except Exception as e:
                    logger.warning('Detail error %s: %s', card['listing_url'], e)
                    yield card
                    break

        await detail_page.close()

        # Stop when no new listings are found rather than checking for a
        # "next page" link — RE/MAX Next.js pages don't always include
        # link[rel="next"] and the next-button selector varies.
        # new_cards check above already handles stopping; just delay here.
        await _random_delay()

    await page.close()


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

async def _run_remax_city(city: dict) -> AsyncGenerator[dict, None]:
    """
    Yields all RE/MAX Cape Town listings (sale + rent).
    Scrapes the base Cape Town page first for maximum volume,
    then each target suburb for completeness.
    """
    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage'],
        )
        context: BrowserContext = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport={'width': 1280, 'height': 900},
            locale='en-ZA',
            timezone_id='Africa/Johannesburg',
        )

        # Block images/media to reduce bandwidth
        await context.route(
            '**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf,otf,mp4,webm}',
            lambda route: route.abort(),
        )

        try:
            for listing_type, base_url in BASE_URLS.items():
                base_url = city_url(base_url, city)
                # 1. Whole city (no suburb filter) — max volume
                async for listing in _scrape_url(context, listing_type, base_url, city['slug']):
                    yield listing

                # 2. Per suburb — catch anything the base page missed.
                #    SUBURBS is a Cape Town list, so other cities use the city page only.
                for suburb in (SUBURBS if city['slug'] == 'cape-town' else []):
                    suburb_url = f'{base_url}/{suburb}'
                    async for listing in _scrape_url(context, listing_type, suburb_url, suburb):
                        yield listing
                    # Rotate user agent between suburbs
                    await context.set_extra_http_headers(
                        {'User-Agent': random.choice(USER_AGENTS)}
                    )
        finally:
            await context.close()
            await browser.close()


async def run_remax_scraper() -> AsyncGenerator[dict, None]:
    """Scrapes every city in scraper.cities.CITIES, tagging each listing with its city."""
    for city in CITIES:
        logger.info('══ remax — %s ══', city['name'])
        async for listing in _run_remax_city(city):
            listing['city'] = city['slug']
            yield listing
