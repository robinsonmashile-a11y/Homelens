"""
Capture real search-results and listing-detail pages from every agency site
as test fixtures (tests/fixtures/<source>_search.html, <source>_detail_N.html).

Re-run for one site when the run report flags its selectors as broken:
    python scripts/capture_fixtures.py remax greeff

Pages are trimmed (styles, external scripts and SVG sprites removed) so the
fixtures stay small; inline JSON payloads are kept because some sites carry
status/cost data there.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright

from scraper import (
    abc_international, century21, chaseveritt, dgproperties, duarte,
    engelvolkers, exprealty, greeff, gumtree, harcourts, jawitz, justproperty,
    lionlead, pamgolding, quay1, rawson, remax, seeff, sothebys, tyson,
)

FIXTURES = Path(__file__).resolve().parent.parent / 'tests' / 'fixtures'
DETAILS_PER_SITE = 3

_PROPDATA_CARD = ('a.property-card-sm', 'href')
_PROPDATA_LIST = ('div.property-list-listing', 'data-href')

# source -> (search url, card selector, attribute holding the detail href)
SITES: dict[str, tuple[str, str, str]] = {
    'century21':         (century21.SALE_BASE, *_PROPDATA_CARD),
    'chaseveritt':       (chaseveritt.SALE_BASE, *_PROPDATA_CARD),
    'dgproperties':      (dgproperties.SALE_BASE, *_PROPDATA_CARD),
    'greeff':            (greeff.SALE_BASE, *_PROPDATA_CARD),
    'harcourts':         (harcourts.SALE_BASE, *_PROPDATA_CARD),
    'justproperty':      (justproperty.SALE_BASE, *_PROPDATA_CARD),
    'sothebys':          (sothebys.SALE_BASE, *_PROPDATA_CARD),
    'quay1':             (quay1._page_url(quay1.SALE_BASE_PATH, 1), 'a[href^="/for-sale/"]:has(p.font-serif)', 'href'),
    'tyson':             (tyson._page_url(tyson.SALE_BASE_PATH, 1, tyson.SALE_SEARCH_ID), *_PROPDATA_CARD),
    'jawitz':            (jawitz.SALE_BASE, *_PROPDATA_LIST),
    'lionlead':          (lionlead.SALE_BASE, *_PROPDATA_LIST),
    'abc_international': (abc_international.SALE_BASE, *_PROPDATA_LIST),
    'pamgolding':        (pamgolding.SALE_BASE_URL, 'article.results__item a.results__item-heading-link', 'href'),
    'seeff':             (seeff._page_url(seeff.SALE_BASE_PATH, 1, seeff.SALE_SEARCH_ID),
                          'div.seeff-listing-card a[href*="/results/residential/"]', 'href'),
    'engelvolkers':      (engelvolkers.SALE_URL, 'article[data-testid^="search-components_result-card_"] a', 'href'),
    'duarte':            (duarte.SALE_BASE, '.item-wrap-v6 .item-title a', 'href'),
    'remax':             (remax.BASE_URLS['sale'], 'div.shadow-xl.bg-white a[href*="property-for-sale"]', 'href'),
    'rawson':            (rawson.SALE_BASE_URL, 'div.card a.card__link', 'href'),
    'gumtree':           (gumtree.GUMTREE_URLS['sale'], 'div.tileV2', 'data-brevo-ad-link'),
}

BASE_URLS = {
    'century21': century21.BASE_URL, 'chaseveritt': chaseveritt.BASE_URL,
    'dgproperties': dgproperties.BASE_URL, 'greeff': greeff.BASE_URL,
    'harcourts': harcourts.BASE_URL, 'justproperty': justproperty.BASE_URL,
    'sothebys': sothebys.BASE_URL, 'quay1': quay1.BASE_URL, 'tyson': tyson.BASE_URL,
    'jawitz': jawitz.BASE_URL, 'lionlead': lionlead.BASE_URL,
    'abc_international': abc_international.BASE_URL, 'pamgolding': pamgolding.BASE_URL,
    'seeff': seeff.BASE_URL, 'engelvolkers': engelvolkers.BASE_URL,
    'duarte': duarte.BASE_URL, 'remax': 'https://www.remax.co.za',
    'rawson': rawson.BASE_URL, 'gumtree': 'https://www.gumtree.co.za',
}


def _trim(html: str) -> str:
    html = re.sub(r'<style\b.*?</style>', '', html, flags=re.S | re.I)
    html = re.sub(r'<script\b[^>]*\bsrc=[^>]*>\s*</script>', '', html, flags=re.S | re.I)
    html = re.sub(r'<svg\b.*?</svg>', '<svg></svg>', html, flags=re.S | re.I)
    html = re.sub(r'<link\b[^>]*rel="(?:stylesheet|preload|prefetch|modulepreload)"[^>]*>', '', html, flags=re.I)
    return html


async def _load(page, url: str) -> None:
    await page.goto(url, wait_until='domcontentloaded', timeout=60_000)
    try:
        await page.wait_for_load_state('networkidle', timeout=15_000)
    except Exception:
        pass
    await asyncio.sleep(3)
    await engelvolkers._dismiss_consent(page)
    for i in range(1, 8):
        await page.evaluate(f'window.scrollTo(0, document.body.scrollHeight * {i} / 7)')
        await asyncio.sleep(0.3)


async def capture_site(browser, source: str) -> None:
    url, card_sel, attr = SITES[source]
    context = await browser.new_context(
        user_agent=gumtree.USER_AGENT, viewport={'width': 1280, 'height': 900},
        locale='en-ZA', timezone_id='Africa/Johannesburg',
    )
    page = await context.new_page()
    try:
        await _load(page, url)
        (FIXTURES / f'{source}_search.html').write_text(_trim(await page.content()))
        hrefs: list[str] = []
        for el in await page.query_selector_all(card_sel):
            href = await el.get_attribute(attr)
            if href and href not in hrefs:
                hrefs.append(href)
        print(f'{source}: search saved, {len(hrefs)} detail links')
        for n, href in enumerate(hrefs[:DETAILS_PER_SITE], start=1):
            full = href if href.startswith('http') else BASE_URLS[source] + href
            try:
                await _load(page, full)
                html = _trim(await page.content())
                (FIXTURES / f'{source}_detail_{n}.html').write_text(
                    f'<!-- source-url: {full} -->\n' + html)
                print(f'  detail {n}: {full}')
            except Exception as e:
                print(f'  detail {n} failed: {full} — {e}')
    except Exception as e:
        print(f'{source}: FAILED — {e}')
    finally:
        await context.close()


def capture_exprealty() -> None:
    req = urllib.request.Request(
        f'{exprealty._SUPABASE_BASE}?listing_type=in.{exprealty._SALE_TYPES}'
        f'&country_code=eq.ZA&status={exprealty._STATUS_FILTER}&select=*&limit=24',
        headers=exprealty._api_headers(),
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        records = json.loads(resp.read())
    (FIXTURES / 'exprealty_api.json').write_text(json.dumps(records, indent=1))
    print(f'exprealty: {len(records)} API records saved')


async def main(only: list[str]) -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    if not only or 'exprealty' in only:
        try:
            capture_exprealty()
        except Exception as e:
            print(f'exprealty: FAILED — {e}')
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=['--no-sandbox'])
        for source in SITES:
            if only and source not in only:
                continue
            await capture_site(browser, source)
        await browser.close()


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1:]))
