"""
Diagnostic script — loads the Pam Golding Cape Town results page and dumps HTML + selector probe.
Run this first if the main scraper returns 0 listings.

Usage:
    python scripts/debug_pamgolding.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright

SALE_URL = 'https://www.pamgolding.co.za/property-search/results?area=Cape+Town&listingType=1'
RENT_URL = 'https://www.pamgolding.co.za/property-search/results?area=Cape+Town&listingType=2'
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'

OUT_DIR = Path(__file__).resolve().parent.parent / 'debug'
OUT_DIR.mkdir(exist_ok=True)


async def probe_page(pw, url: str, label: str) -> None:
    browser = await pw.chromium.launch(headless=True, args=['--no-sandbox'])
    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={'width': 1280, 'height': 900},
        locale='en-ZA',
        timezone_id='Africa/Johannesburg',
    )
    page = await context.new_page()

    print(f'\n{"="*60}')
    print(f'Loading {label}: {url}')
    print('='*60)

    response = await page.goto(url, wait_until='networkidle', timeout=90_000)
    print(f'Status   : {response.status if response else "unknown"}')
    print(f'Final URL: {page.url}')

    # Extra wait for JS rendering
    await asyncio.sleep(5)

    # Screenshot
    ss = OUT_DIR / f'pamgolding_{label}.png'
    await page.screenshot(path=str(ss), full_page=False)
    print(f'Screenshot → {ss}')

    # Raw HTML
    html = await page.content()
    html_path = OUT_DIR / f'pamgolding_{label}.html'
    html_path.write_text(html, encoding='utf-8')
    print(f'HTML saved → {html_path}  ({len(html):,} bytes)')

    # Page title
    print(f'Page title: {await page.title()}')

    print('\n--- Probing selectors ---')
    selectors = [
        'article',
        '[class*="property-card"]',
        '[class*="PropertyCard"]',
        '[class*="listing-card"]',
        '[class*="ListingCard"]',
        '[class*="property-item"]',
        '[class*="PropertyItem"]',
        '[class*="listing-item"]',
        '[class*="result-item"]',
        '[class*="ResultItem"]',
        '[class*="property-result"]',
        '[class*="search-result"]',
        '[class*="tile"]',
        '[class*="card"]',
        '[data-testid]',
        '[data-property-id]',
        '[data-listing-id]',
        'a[href*="/property/"]',
        'a[href*="/listing/"]',
        'a[href*="/for-sale/"]',
        'a[href*="/to-rent/"]',
        '.property',
        '.listing',
        # Pam Golding specific guesses
        '[class*="pam"]',
        '[class*="Pam"]',
        '[class*="SearchResult"]',
        '[class*="PropertyListing"]',
    ]

    for sel in selectors:
        try:
            els = await page.query_selector_all(sel)
            if els:
                first_class = await els[0].get_attribute('class') or ''
                first_tag   = await els[0].evaluate('e => e.tagName.toLowerCase()')
                first_href  = await els[0].get_attribute('href') or ''
                print(f'  {sel!r:50s} → {len(els):3d} | <{first_tag} class="{first_class[:60]}">')
                if first_href:
                    print(f'    href: {first_href[:80]}')
            else:
                print(f'  {sel!r:50s} → 0')
        except Exception as e:
            print(f'  {sel!r:50s} → ERROR: {e}')

    # Try to find price elements
    print('\n--- Probing price elements ---')
    price_selectors = [
        '[class*="price"]',
        '[class*="Price"]',
        '[class*="amount"]',
        '[class*="Amount"]',
        'span:has-text("R ")',
        'p:has-text("R ")',
        'div:has-text("R ")',
    ]
    for sel in price_selectors:
        try:
            els = await page.query_selector_all(sel)
            if els:
                first_text = await els[0].inner_text()
                first_class = await els[0].get_attribute('class') or ''
                print(f'  {sel!r:50s} → {len(els):3d} | text="{first_text[:40].strip()}" class="{first_class[:40]}"')
            else:
                print(f'  {sel!r:50s} → 0')
        except Exception as e:
            print(f'  {sel!r:50s} → ERROR: {e}')

    # Extract first card's full outer HTML for manual inspection
    card_selectors = [
        'article',
        '[class*="property-card"]',
        '[class*="PropertyCard"]',
        '[class*="listing-card"]',
        '[class*="property-item"]',
        '[class*="card"]',
    ]
    for sel in card_selectors:
        first_card = await page.query_selector(sel)
        if first_card:
            outer = await first_card.evaluate('e => e.outerHTML')
            card_path = OUT_DIR / f'pamgolding_{label}_first_card.html'
            card_path.write_text(outer[:10000], encoding='utf-8')
            print(f'\nFirst card ({sel}) HTML → {card_path}')
            print(f'Card preview (first 500 chars):\n{outer[:500]}')
            break

    # Count total results if we can find a results count element
    print('\n--- Looking for result count ---')
    count_selectors = [
        '[class*="result-count"]',
        '[class*="ResultCount"]',
        '[class*="total"]',
        'h1', 'h2', 'h3',
    ]
    for sel in count_selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                if text and len(text) < 100:
                    print(f'  {sel}: "{text}"')
        except Exception:
            pass

    # Check for pagination
    print('\n--- Probing pagination ---')
    pagination_selectors = [
        '[class*="pagination"]',
        '[class*="Pagination"]',
        'a[aria-label*="next"]',
        'a[aria-label*="Next"]',
        'button[aria-label*="next"]',
        '[class*="next-page"]',
        '[class*="NextPage"]',
        'a[rel="next"]',
        '[class*="page"]',
    ]
    for sel in pagination_selectors:
        try:
            els = await page.query_selector_all(sel)
            if els:
                first_class = await els[0].get_attribute('class') or ''
                first_text  = (await els[0].inner_text()).strip()
                first_href  = await els[0].get_attribute('href') or ''
                print(f'  {sel!r:45s} → {len(els):3d} | "{first_text[:30]}" href="{first_href[:60]}"')
            else:
                print(f'  {sel!r:45s} → 0')
        except Exception as e:
            print(f'  {sel!r:45s} → ERROR: {e}')

    await browser.close()


async def main() -> None:
    async with async_playwright() as pw:
        await probe_page(pw, SALE_URL, 'sale')

    print(f'\n\nDone. Check debug/ folder for HTML files.')
    print('Open debug/pamgolding_sale.html to inspect the full structure.')


if __name__ == '__main__':
    asyncio.run(main())
