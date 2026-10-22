"""
Dry run: Greeff detail-page extraction. NEVER writes to Supabase.

    python scripts/dry_run_greeff.py --limit 15            # live: sale + rent sample
    python scripts/dry_run_greeff.py --fixtures            # offline: tests/fixtures/greeff_detail_*.html

Prints a per-listing table and a coverage table. Saves JSON to
reports/dry_run_greeff_<timestamp>.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.async_api import async_playwright  # noqa: E402

from scraper import greeff  # noqa: E402
from scraper.detail import DETAIL_FIELDS, log_missing, parse_detail_fields  # noqa: E402
from scraper.enrich import read_costs_from_page  # noqa: E402

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
log = logging.getLogger('dry_run_greeff')

SEL = {
    **greeff.SELECTORS,
    'info_rows':  '.listing-details-info-detail',
    'info_label': '.info-detail-label',
    'info_value': '.info-detail-value',
    'blocks':     '.listing-details-features .features-block',
}

_JS_BLOCKS = '''(css) => {
    const out = {};
    document.querySelectorAll(css).forEach(b => {
        const h = (b.querySelector(':scope > span') || {}).textContent || 'other';
        out[h.trim()] = Array.from(b.querySelectorAll('.features-icons span'))
            .filter(s => !s.querySelector('span') || s.classList.contains('carports-text'))
            .map(s => s.textContent.replace(/\\s+/g, ' ').trim());
    });
    return out;
}'''


async def read_greeff_detail(page) -> dict:
    """Read one loaded Greeff listing page → detail fields (+ costs). Never raises."""
    rows: list[tuple[str, str]] = []
    try:
        for row in await page.query_selector_all(SEL['info_rows']):
            lab = await row.query_selector(SEL['info_label'])
            val = await row.query_selector(SEL['info_value'])
            if lab and val:
                rows.append(((await lab.inner_text()).strip(), (await val.inner_text()).strip()))
    except Exception as e:
        log.warning('info rows failed: %s', e)

    try:
        blocks = await page.evaluate(_JS_BLOCKS, SEL['blocks'])
    except Exception as e:
        log.warning('feature blocks failed: %s', e)
        blocks = {}

    description = None
    try:
        el = await page.query_selector(SEL['description'])
        description = (await el.inner_text()) if el else None
    except Exception as e:
        log.warning('description failed: %s', e)

    fields = parse_detail_fields(rows, blocks, description)

    # Costs: reuse the existing, tested cost reader; keep the table value if it found one.
    try:
        costs = await read_costs_from_page(page, SEL)
        for k in ('levy_monthly', 'rates_monthly'):
            if fields[k] is None:
                fields[k] = costs.get(k)
    except Exception as e:
        log.warning('costs failed: %s', e)
    return fields


def coverage(results: list[dict]) -> str:
    n = len(results)
    lines = [f'{"field":<14}{"found":>6}{"of":>5}{"cov":>7}']
    for f in DETAIL_FIELDS:
        got = sum(1 for r in results if r['fields'].get(f) is not None)
        lines.append(f'{f:<14}{got:>6}{n:>5}{100 * got / n if n else 0:>6.0f}%')
    return '\n'.join(lines)


async def run_fixtures() -> list[dict]:
    results = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        for path in sorted((ROOT / 'tests' / 'fixtures').glob('greeff_detail_*.html')):
            await page.set_content(path.read_text(), wait_until='domcontentloaded')
            fields = await read_greeff_detail(page)
            log_missing('greeff', path.name, fields)
            results.append({'listing_id': path.name, 'listing_type': 'sale', 'fields': fields})
        await browser.close()
    return results


async def run_live(limit: int) -> list[dict]:
    results = []
    per_type = max(1, limit // 2)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        ctx = await browser.new_context(user_agent=greeff.USER_AGENT, locale='en-ZA',
                                        viewport={'width': 1280, 'height': 900})
        page = await ctx.new_page()
        for lt, base in (('sale', greeff.SALE_BASE), ('rent', greeff.RENT_BASE)):
            _, cards = await greeff._fetch_page_cards(browser, base, lt, 1)
            for card in cards[:per_type]:
                try:
                    await page.goto(card['listing_url'], wait_until='domcontentloaded', timeout=45_000)
                    await asyncio.sleep(1.5)
                    fields = await read_greeff_detail(page)
                except Exception as e:
                    log.warning('visit failed %s: %s', card['listing_url'], e)
                    fields = {k: None for k in DETAIL_FIELDS}
                # card values fill gaps only
                for k in ('size_m2', 'bedrooms', 'bathrooms'):
                    if fields.get(k) is None:
                        fields[k] = card.get(k)
                log_missing('greeff', card['listing_id'], fields)
                results.append({'listing_id': card['listing_id'], 'listing_type': lt,
                                'url': card['listing_url'], 'fields': fields})
                await asyncio.sleep(2.0)
        await browser.close()
    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=16, help='total listings (split sale/rent)')
    ap.add_argument('--fixtures', action='store_true', help='use saved HTML, no network')
    args = ap.parse_args()

    results = asyncio.run(run_fixtures() if args.fixtures else run_live(args.limit))

    for r in results:
        f = r['fields']
        d = f['description']
        print(f"\n{r['listing_id']} [{r['listing_type']}]")
        for k in DETAIL_FIELDS:
            v = f[k]
            if k == 'description':
                v = f'{len(d)} chars: {d[:70]!r}…' if d else None
            print(f'  {k:<14}{v!r}')

    print('\nCOVERAGE (all)\n' + coverage(results))
    sale = [r for r in results if r['listing_type'] == 'sale']
    if sale and len(sale) != len(results):
        print('\nCOVERAGE (sale only)\n' + coverage(sale))

    out = ROOT / 'reports'
    out.mkdir(exist_ok=True)
    path = out / f'dry_run_greeff_{datetime.now():%Y%m%d_%H%M%S}.json'
    path.write_text(json.dumps(results, indent=2, default=str))
    print(f'\nsaved {path}  (no database writes performed)')


if __name__ == '__main__':
    main()
