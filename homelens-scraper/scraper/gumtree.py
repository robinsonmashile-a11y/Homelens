"""
Async Playwright scraper for Gumtree South Africa — Cape Town property listings.

Card data strategy: Gumtree embeds clean structured data in data-brevo-* attributes
on each div.tileV2 card. We read those directly instead of fragile CSS parsing.
Detail pages are visited only for the full description text.
"""
from __future__ import annotations

import asyncio
import html as html_lib
import json
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

from .utils import clean_personal_info, normalise_suburb
from .detail_page import apply_details, read_detail_from_page
from .enrich import apply_costs, read_costs_from_page, resolve_status

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# SITE SELECTORS — listing status + monthly levy/rates.
# The only place to edit when this site's markup changes. Confirmed against
# saved pages in tests/fixtures/ (re-capture: scripts/capture_fixtures.py).
# ═══════════════════════════════════════════════════════════════════════════
SELECTORS = {
    # Gumtree is a private-seller classifieds site: no status badge exists on
    # cards or ad pages, and sold ads are simply removed. Status is only read
    # from the shared fallback (a standalone "SOLD" / "UNDER OFFER" line in the
    # ad text), so a seller who types it into the ad is still caught.
    'status_badge': None,
    'status_map':   {},
    # The ad's structured attributes (data-brevo-attributes) have no levy or
    # rates field, so levies/rates only ever appear in the seller's free text.
    'text_only':    True,
    'cost_rows':    None,
    'cost_label':   None,
    'cost_value':   None,
    'description':  ('.description-website-container, [class*="vip-description"], '
                     '[class*="description-text"], [itemprop="description"]'),
}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)

GUMTREE_URLS: dict[str, str] = {
    'rent': 'https://www.gumtree.co.za/s-house-rentals-flat-rentals-offered/cape-town/v1c9071l3100006p1',
    'sale': 'https://www.gumtree.co.za/s-houses-flats-for-sale/cape-town/v1c9074l3100006p1',
}

CAPE_TOWN_SUBURBS = [
    'sea-point', 'green-point', 'observatory', 'gardens', 'woodstock',
    'de-waterkant', 'bo-kaap', 'salt-river', 'tamboerskloof',
    'claremont', 'rondebosch', 'newlands', 'mowbray', 'wynberg',
    'bellville', 'parow', 'milnerton', 'tableview', 'blouberg',
]

# POPIA: attribute names in brevo-attributes JSON that must never be stored
_BLOCKED_ATTRS = {'Phone', 'UserName', 'Email'}

MIN_DELAY = 3.0
MAX_DELAY = 7.0
MAX_RETRIES = 2
BASE_RATE_LIMIT = 4.0
MAX_PAGES = 30


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _random_delay() -> None:
    await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


def _is_blocked(page: Page) -> bool:
    url = page.url.lower()
    return any(kw in url for kw in ['captcha', 'blocked', 'robot', 'challenge'])


def _parse_brevo_attributes(raw_json_html: str) -> dict:
    """
    Parse the HTML-encoded JSON in data-brevo-attributes into a clean dict.
    Skips any POPIA-blocked fields (Phone, UserName, Email).
    Returns keys: bedrooms, bathrooms, size_m2, has_parking, pet_friendly
    """
    result: dict = {}
    try:
        decoded = html_lib.unescape(raw_json_html)
        attrs = json.loads(decoded)
        for attr in attrs:
            name = attr.get('name', '')
            if name in _BLOCKED_ATTRS:
                continue
            value = attr.get('value', {}).get('attributeValue')
            if value is None:
                continue

            if name == 'NumberBedrooms':
                try:
                    result['bedrooms'] = int(value)
                except (ValueError, TypeError):
                    pass
            elif name == 'NumberBathrooms':
                try:
                    result['bathrooms'] = float(value)
                except (ValueError, TypeError):
                    pass
            elif name == 'AreaInMeters':
                try:
                    result['size_m2'] = float(value)
                except (ValueError, TypeError):
                    pass
            elif name == 'Parking':
                result['has_parking'] = str(value).lower() not in ('none', 'no', 'false', '0', '')
            elif name == 'PetsAllowed':
                result['pet_friendly'] = str(value).lower() in ('yes', 'true', '1')
    except (json.JSONDecodeError, Exception) as e:
        logger.debug('brevo-attributes parse error: %s', e)
    return result


# ---------------------------------------------------------------------------
# Card-level extraction — reads data-brevo-* attributes directly
# ---------------------------------------------------------------------------

async def _extract_cards(page: Page, listing_type: str) -> list[dict]:
    """
    Extract all listing cards from the current results page.
    Reads clean data from data-brevo-* attributes on div.tileV2 elements.
    """
    cards: list[dict] = []
    card_els = await page.query_selector_all('div.tileV2')
    logger.debug('Raw tileV2 elements: %d', len(card_els))

    for card in card_els:
        try:
            # All the good stuff lives in data attributes
            short_id  = await card.get_attribute('data-short-id')
            title     = await card.get_attribute('data-brevo-ad-title')
            price_raw = await card.get_attribute('data-brevo-ad-price')
            href      = await card.get_attribute('data-brevo-ad-link')
            location  = await card.get_attribute('data-brevo-location')
            attrs_raw = await card.get_attribute('data-brevo-attributes')

            if not short_id or not href:
                continue

            listing_url = f'https://www.gumtree.co.za{href}'
            title = html_lib.unescape(title or '').strip()
            location_raw = html_lib.unescape(location or '').strip()

            try:
                price = int(price_raw) if price_raw else None
            except (ValueError, TypeError):
                price = None

            suburb_normalised = normalise_suburb(location_raw)

            # Parse structured attributes from the embedded JSON
            structured = _parse_brevo_attributes(attrs_raw or '')

            card_dict: dict = {
                'listing_id':        short_id,
                'title':             title,
                'price':             price,
                'suburb':            location_raw,
                'suburb_normalised': suburb_normalised,
                'listing_url':       listing_url,
                'listing_type':      listing_type,
                'source':            'gumtree',
            }
            card_dict['listing_status'], card_dict['status_source'] = resolve_status(
                [], title, SELECTORS)
            card_dict.update(structured)
            cards.append(card_dict)

        except Exception as e:
            logger.debug('Card extraction error: %s', e)
            continue

    return cards


# ---------------------------------------------------------------------------
# Detail page — only fetched for description text
# ---------------------------------------------------------------------------

async def _extract_detail(page: Page, url: str) -> dict:
    """
    Visit a listing detail page and return the full description text.
    Structured attributes (bedrooms etc.) are already extracted from card data.
    """
    detail: dict = {}
    try:
        # Gumtree SA detail description selectors (try most specific first)
        desc_el = await page.query_selector(
            '.description-website-container, '
            '[class*="vip-description"], '
            '[class*="description-text"], '
            '[itemprop="description"]'
        )
        if desc_el:
            raw_desc = (await desc_el.inner_text()).strip()
            detail['description'] = clean_personal_info(raw_desc)
            # A seller may type SOLD / UNDER OFFER on its own line in the ad.
            detail['listing_status'], detail['status_source'] = resolve_status(
                [], raw_desc, SELECTORS)

        # Levies / rates from the ad text (page text first, then description).
        detail['costs'] = await read_costs_from_page(page, SELECTORS)
        detail['fields'] = await read_detail_from_page(page, SELECTORS)

        # Date posted
        date_el = await page.query_selector(
            '[class*="date-posted"], time, [class*="listing-date"]'
        )
        if date_el:
            detail['date_posted'] = (await date_el.inner_text()).strip()

    except Exception as e:
        logger.debug('Detail extraction error for %s: %s', url, e)

    return detail


# ---------------------------------------------------------------------------
# Pagination — reads <link rel="next"> from page <head>
# ---------------------------------------------------------------------------

async def _get_next_page_url(page: Page) -> str | None:
    """Read the canonical next-page URL from the <link rel="next"> tag."""
    try:
        next_el = await page.query_selector('link[rel="next"]')
        if not next_el:
            return None
        href = await next_el.get_attribute('href')
        if not href:
            return None
        return href if href.startswith('http') else f'https://www.gumtree.co.za{href}'
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Core scraper
# ---------------------------------------------------------------------------

async def scrape_listing_type(
    context: BrowserContext,
    listing_type: str,
    start_url: str,
) -> AsyncGenerator[dict, None]:
    """Scrape all pages for one listing type. Yields merged listing dicts."""
    page = await context.new_page()
    current_url: str | None = start_url
    page_num = 0

    while current_url and page_num < MAX_PAGES:
        page_num += 1
        logger.info('Scraping %s page %d: %s', listing_type, page_num, current_url)

        try:
            await page.goto(current_url, wait_until='domcontentloaded', timeout=30_000)
            await _random_delay()
        except PWTimeout:
            logger.warning('Timeout loading page %d for %s', page_num, listing_type)
            break

        if _is_blocked(page):
            logger.error('Blocked/captcha on %s', current_url)
            break

        cards = await _extract_cards(page, listing_type)
        logger.info('Found %d cards on page %d', len(cards), page_num)

        if not cards:
            logger.info('No cards on page %d — done paginating %s', page_num, listing_type)
            break

        # Determine next page URL before opening detail pages
        next_url = await _get_next_page_url(page)

        # Visit detail pages for descriptions
        detail_page = await context.new_page()
        for card in cards:
            retries = 0
            while retries <= MAX_RETRIES:
                try:
                    await detail_page.goto(
                        card['listing_url'], wait_until='domcontentloaded', timeout=30_000
                    )
                    await asyncio.sleep(BASE_RATE_LIMIT + random.uniform(0, 2))

                    if _is_blocked(detail_page):
                        logger.warning('Blocked on detail: %s', card['listing_url'])
                        yield card  # yield card without description rather than lose it
                        break

                    detail = await _extract_detail(detail_page, card['listing_url'])
                    costs = detail.pop('costs', None)
                    fields = detail.pop('fields', None)
                    # Don't let a "live/default" from a page with no status text
                    # overwrite a card-level status that was found.
                    if detail.get('listing_status') == 'live':
                        detail.pop('listing_status'); detail.pop('status_source', None)
                    merged = {**card, **detail}
                    if fields:
                        apply_details(merged, fields)     # card values kept; erf/parking/extras added
                    if listing_type == 'sale' and costs:      # levies/rates: sale only
                        apply_costs(merged, costs)
                    yield merged
                    break

                except PWTimeout:
                    retries += 1
                    if retries > MAX_RETRIES:
                        logger.warning('Max retries for %s — yielding card data only', card['listing_url'])
                        yield card
                    else:
                        await asyncio.sleep(BASE_RATE_LIMIT * retries)
                except Exception as e:
                    logger.warning('Detail page error for %s: %s', card['listing_url'], e)
                    yield card  # still save what we have
                    break

        await detail_page.close()

        current_url = next_url
        if current_url:
            await _random_delay()

    await page.close()


async def run_scraper() -> AsyncGenerator[dict, None]:
    """Top-level entry point. Yields all scraped listings across rent + sale."""
    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage'],
        )
        context: BrowserContext = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={'width': 1280, 'height': 900},
            locale='en-ZA',
            timezone_id='Africa/Johannesburg',
        )

        # Block images/fonts to speed up page loads
        await context.route(
            '**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf,otf}',
            lambda route: route.abort(),
        )

        try:
            for listing_type, start_url in GUMTREE_URLS.items():
                async for listing in scrape_listing_type(context, listing_type, start_url):
                    yield listing
        finally:
            await context.close()
            await browser.close()
