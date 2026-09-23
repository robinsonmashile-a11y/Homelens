"""
Diagnostic script — probes LIONLEAD and ABC International for correct selectors.
Run this if the main scrapers return 0 listings.

Usage:
    python scripts/debug_batch5.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)

OUT_DIR = Path(__file__).resolve().parent.parent / 'debug'
OUT_DIR.mkdir(exist_ok=True)

SITES = [
    {
        'name': 'lionlead',
        'url': 'https://www.lionleadrealestate.co.za/results/residential/for-sale/cape-town/',
        'card_sel': '.property-list-listing',
        'wait_sel': '.property-list-listing',
        'wait_ms': 20_000,
        'extra_wait': 5.0,
    },
    {
        'name': 'abc_international',
        'url': 'https://www.abcproperties.co.za/results/residential/for-sale/western-cape/cape-town/',
        'card_sel': '.property-list-item',
        'wait_sel': '.property-list-item',
        'wait_ms': 20_000,
        'extra_wait': 5.0,
    },
]

PROBE_SELECTORS = [
    'a[href*="/property"]',
    'a[href*="/listing"]',
    'a[href*="cape-town"]',
    '[class*="property"]',
    '[class*="listing"]',
    '[class*="card"]',
    '[class*="result"]',
    'article',
    '[data-id]',
    '[data-property]',
    '[class*="item"]',
    'li[id]',
    'section li',
    'ul li',
]


async def debug_site(browser, cfg: dict) -> None:
    name = cfg['name']
    url  = cfg['url']

    print(f'\n{"=" * 60}')
    print(f'  DEBUG: {name.upper()}')
    print(f'  URL:   {url}')
    print('=' * 60)

    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={'width': 1280, 'height': 900},
        locale='en-ZA',
        timezone_id='Africa/Johannesburg',
    )
    page = await context.new_page()

    try:
        print(f'  Loading page (domcontentloaded)...')
        response = await page.goto(url, wait_until='domcontentloaded', timeout=60_000)
        print(f'  HTTP status : {response.status if response else "unknown"}')
        print(f'  Final URL   : {page.url}')

        # Wait for expected card selector
        print(f'  Waiting for "{cfg["wait_sel"]}" (up to {cfg["wait_ms"] / 1000:.0f}s)...')
        try:
            await page.wait_for_selector(cfg['wait_sel'], timeout=cfg['wait_ms'])
            print(f'  wait_for_selector: FOUND')
        except Exception:
            print(f'  wait_for_selector: TIMED OUT — selector not present')

        await asyncio.sleep(cfg['extra_wait'])

        # Probe selectors
        print(f'\n  --- Selector probe ---')
        for sel in PROBE_SELECTORS:
            els = await page.query_selector_all(sel)
            if els:
                first_class = (await els[0].get_attribute('class') or '').strip()[:80]
                first_tag   = await els[0].evaluate('e => e.tagName.toLowerCase()')
                print(f'  {sel!r:45s} → {len(els):3d} | <{first_tag} class="{first_class}">')
            else:
                print(f'  {sel!r:45s} → 0')

        # Dump first card if found by known selector
        cards = await page.query_selector_all(cfg['card_sel'])
        if cards:
            print(f'\n  Found {len(cards)} card(s) with "{cfg["card_sel"]}"')
            outer = await cards[0].evaluate('e => e.outerHTML')
            card_path = OUT_DIR / f'{name}_first_card.html'
            card_path.write_text(outer[:6000], encoding='utf-8')
            print(f'  First card HTML saved → {card_path}')

            # Dump the href of the first anchor inside the card
            link = await cards[0].query_selector('a')
            if link:
                href = await link.get_attribute('href')
                print(f'  First card a[href] = {href}')
            else:
                print(f'  First card: no <a> found inside')
        else:
            print(f'\n  0 cards with "{cfg["card_sel"]}"')

            # Dump the first matching element from probe selectors as a clue
            for sel in ['[class*="property"]', '[class*="listing"]', 'article', 'ul li']:
                els = await page.query_selector_all(sel)
                if els:
                    outer = await els[0].evaluate('e => e.outerHTML')
                    safe = sel.replace('[','').replace(']','').replace('*=','').replace('"','').replace('/','_').replace(' ','_').strip()
                    clue_path = OUT_DIR / f'{name}_clue_{safe}.html'
                    clue_path.write_text(outer[:4000], encoding='utf-8')
                    print(f'  Clue card ({sel}) saved → {clue_path}')
                    break

        # Save full HTML
        html = await page.content()
        html_path = OUT_DIR / f'{name}_page.html'
        html_path.write_text(html, encoding='utf-8')
        print(f'\n  Full HTML saved → {html_path} ({len(html):,} bytes)')

        # Screenshot
        ss_path = OUT_DIR / f'{name}_page.png'
        await page.screenshot(path=str(ss_path), full_page=False)
        print(f'  Screenshot     → {ss_path}')

    finally:
        await context.close()


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage'],
        )

        for cfg in SITES:
            await debug_site(browser, cfg)

        await browser.close()

    print(f'\n\nDone. Inspect debug/ folder for HTML files.')


if __name__ == '__main__':
    asyncio.run(main())
