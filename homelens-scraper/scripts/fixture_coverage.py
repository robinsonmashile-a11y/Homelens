"""
Offline: run the shared detail reader over every saved detail page in
tests/fixtures/ with each scraper's own SELECTORS and print per-site coverage.
No network, no database.

    python scripts/fixture_coverage.py            # table
    python scripts/fixture_coverage.py greeff     # one site, field by field
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.async_api import async_playwright  # noqa: E402

from scraper.detail import DETAIL_FIELDS  # noqa: E402
from scraper.detail_page import read_detail_from_page  # noqa: E402

FIX = ROOT / 'tests' / 'fixtures'
SITES = sorted({p.name.split('_detail')[0] for p in FIX.glob('*_detail_*.html')})


async def main(only: str | None) -> None:
    out = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        for site in SITES:
            if only and site != only:
                continue
            try:
                sel = importlib.import_module(f'scraper.{site}').SELECTORS
            except Exception as e:
                print(f'{site}: cannot import ({e})')
                continue
            res = []
            for f in sorted(FIX.glob(f'{site}_detail_*.html')):
                await page.set_content(f.read_text(errors='ignore'), wait_until='domcontentloaded')
                res.append(await read_detail_from_page(page, sel))
            out[site] = res
        await browser.close()

    cols = ['description', 'size_m2', 'bedrooms', 'bathrooms', 'parking', 'erf_size',
            'pet_friendly', 'is_furnished', 'security', 'views', 'building_name']
    print(f'{"site":<18}{"n":>2} ' + ' '.join(c[:9].rjust(9) for c in cols))
    for site, res in out.items():
        print(f'{site:<18}{len(res):>2} ' + ' '.join(
            f'{sum(1 for r in res if r[c] is not None)}/{len(res)}'.rjust(9) for c in cols))
    if only:
        for i, r in enumerate(out.get(only, []), 1):
            print(f'\n--- {only} detail_{i}')
            for k in DETAIL_FIELDS:
                v = r[k]
                print(f'  {k:<14}{(v[:70] + "...") if isinstance(v, str) and len(v) > 70 else v!r}')


asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
