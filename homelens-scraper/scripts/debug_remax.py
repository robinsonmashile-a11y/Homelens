"""
Diagnostic script — loads the RE/MAX Cape Town page and dumps HTML + selector probe.
Run this first if the main scraper returns 0 listings.

Usage:
    python scripts/debug_remax.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright

URL = 'https://www.remax.co.za/property-for-sale-south-africa/western-cape/cape-town'
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'

OUT_DIR = Path(__file__).resolve().parent.parent / 'debug'
OUT_DIR.mkdir(exist_ok=True)


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=['--no-sandbox'])
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={'width': 1280, 'height': 900},
            locale='en-ZA',
            timezone_id='Africa/Johannesburg',
        )
        page = await context.new_page()

        print(f'Loading {URL} ...')
        response = await page.goto(URL, wait_until='networkidle', timeout=60_000)
        print(f'Status   : {response.status if response else "unknown"}')
        print(f'Final URL: {page.url}')

        # Extra wait for JS rendering
        await asyncio.sleep(4)

        # Screenshot
        ss = OUT_DIR / 'remax_page.png'
        await page.screenshot(path=str(ss), full_page=False)
        print(f'Screenshot → {ss}')

        # Raw HTML
        html = await page.content()
        html_path = OUT_DIR / 'remax_page.html'
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
            '[class*="listing-item"]',
            '[class*="result"]',
            '[class*="tile"]',
            '[class*="card"]',
            '[data-testid]',
            'a[href*="/property"]',
            'a[href*="/listing"]',
        ]

        for sel in selectors:
            els = await page.query_selector_all(sel)
            if els:
                first_class = await els[0].get_attribute('class') or ''
                first_tag   = await els[0].evaluate('e => e.tagName.toLowerCase()')
                print(f'  {sel!r:45s} → {len(els):3d} | e.g. <{first_tag} class="{first_class[:60]}">')
            else:
                print(f'  {sel!r:45s} → 0')

        # Extract first card's full outer HTML for manual inspection
        first_card = await page.query_selector('article, [class*="property-card"], [class*="listing-card"]')
        if first_card:
            outer = await first_card.evaluate('e => e.outerHTML')
            card_path = OUT_DIR / 'remax_first_card.html'
            card_path.write_text(outer[:8000], encoding='utf-8')
            print(f'\nFirst card HTML (truncated) → {card_path}')

        await browser.close()
        print('\nDone. Open debug/remax_page.html to inspect the full structure.')


if __name__ == '__main__':
    asyncio.run(main())
