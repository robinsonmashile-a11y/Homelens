"""
Diagnostic script — loads Gumtree and dumps page HTML + screenshots
so we can identify the correct CSS selectors.

Usage:
    python scripts/debug_selectors.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright

URL = 'https://www.gumtree.co.za/s-house-rentals-flat-rentals-offered/cape-town/v1c9071l3100006p1'
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)

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
        response = await page.goto(URL, wait_until='domcontentloaded', timeout=30_000)
        print(f'Status: {response.status if response else "unknown"}')
        print(f'Final URL: {page.url}')

        await asyncio.sleep(3)

        # Screenshot
        screenshot_path = OUT_DIR / 'gumtree_page.png'
        await page.screenshot(path=str(screenshot_path), full_page=False)
        print(f'Screenshot saved → {screenshot_path}')

        # Save raw HTML
        html = await page.content()
        html_path = OUT_DIR / 'gumtree_page.html'
        html_path.write_text(html, encoding='utf-8')
        print(f'HTML saved → {html_path}  ({len(html):,} bytes)')

        # Try to find listing-like elements and print what we see
        print('\n--- Probing common selectors ---')
        selectors_to_try = [
            'article',
            '[class*="listing"]',
            '[class*="result"]',
            '[class*="ad-"]',
            '[class*="tile"]',
            '[data-q]',
            'li[class]',
            'div[class*="item"]',
        ]
        for sel in selectors_to_try:
            els = await page.query_selector_all(sel)
            if els:
                # Print class of first few matches
                sample_classes = []
                for el in els[:3]:
                    cls = await el.get_attribute('class') or ''
                    tag = await el.evaluate('el => el.tagName.toLowerCase()')
                    sample_classes.append(f'{tag}.{cls[:80]}')
                print(f'  {sel!r:35s} → {len(els):3d} matches | e.g. {sample_classes[0]}')
            else:
                print(f'  {sel!r:35s} → 0 matches')

        # Print page title so we know if it's a captcha/block page
        title = await page.title()
        print(f'\nPage title: {title}')

        await browser.close()
        print('\nDone. Open debug/gumtree_page.html in your browser to inspect the structure.')


if __name__ == '__main__':
    asyncio.run(main())
