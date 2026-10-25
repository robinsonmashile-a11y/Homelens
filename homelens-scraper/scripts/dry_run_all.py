"""
Dry run: run real scrapers on a small sample and report field coverage.
NEVER writes to Supabase — the client is wrapped so insert/update/upsert/delete raise.

    python scripts/dry_run_all.py greeff sothebys --limit 12      # specific sites
    python scripts/dry_run_all.py --all --limit 12                # every site, sequentially
    python scripts/dry_run_all.py --report                        # combine saved per-site results

Per site: first page of sale + first page of rent, up to --limit listings of each type.
Results are saved to reports/dry_run/<site>.json; --report prints the combined table
(all listings, and sale-only for the levy/rates columns).
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / '.env', override=True)

OUT = ROOT / 'reports' / 'dry_run'
SITES = ['greeff', 'sothebys', 'tyson', 'quay1', 'century21', 'chaseveritt', 'dgproperties',
         'harcourts', 'justproperty', 'seeff', 'jawitz', 'lionlead', 'abc_international',
         'duarte', 'engelvolkers', 'pamgolding', 'rawson', 'remax', 'exprealty']
FIELDS = ['description', 'size_m2', 'bedrooms', 'bathrooms', 'parking', 'levy_monthly',
          'rates_monthly', 'erf_size', 'pet_friendly', 'is_furnished', 'security', 'views',
          'building_name', 'levy_includes']
_WRITES = ('insert', 'update', 'upsert', 'delete')


class _ReadOnlyBuilder:
    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        if name in _WRITES:
            raise RuntimeError(f'DRY RUN: refusing to call .{name}() on Supabase')
        return getattr(self._inner, name)


class _ReadOnlyClient:
    def __init__(self, inner):
        self._inner = inner

    def table(self, name):
        return _ReadOnlyBuilder(self._inner.table(name))

    def __getattr__(self, name):
        if name in ('rpc',):
            raise RuntimeError('DRY RUN: refusing rpc()')
        return getattr(self._inner, name)


def _make_read_only() -> None:
    import os
    os.environ['RECHECK_COSTS'] = '1'     # re-read levy/rates even for listings checked recently
    from scraper import database
    real = database.get_client
    database.get_client = lambda: _ReadOnlyClient(real())
    # Pretend the migration is applied: the sample should exercise the new columns
    # in the listing dicts (nothing is written either way).
    database._detail_cols_exist = False        # get_details_checked_ids() -> set(): visit everything


def _limit_pages(mod) -> None:
    mp = getattr(mod, 'MAX_PAGES', None)
    if isinstance(mp, dict):
        for k in mp:
            mp[k] = 1
    elif isinstance(mp, int):
        mod.MAX_PAGES = 1
    mr = getattr(mod, 'MAX_RECORDS', None)
    if isinstance(mr, dict):
        for k in mr:
            mr[k] = 60


async def run_site(site: str, limit: int, timeout: int) -> list[dict]:
    mod = importlib.import_module(f'scraper.{site}')
    _limit_pages(mod)
    gen = getattr(mod, f'run_{site}_scraper')()
    got: dict[str, list[dict]] = {'sale': [], 'rent': []}
    t0 = time.time()
    async def _collect():
        async for listing in gen:
            lt = listing.get('listing_type')
            if lt in got and len(got[lt]) < limit:
                got[lt].append({k: (v if not isinstance(v, (set, bytes)) else str(v)) for k, v in listing.items()})
            if all(len(v) >= limit for v in got.values()):
                break
    try:
        await asyncio.wait_for(_collect(), timeout)      # hard deadline, even if the scraper never yields
    except asyncio.TimeoutError:
        logging.getLogger('dry_run').warning('%s: hit %ss deadline with %s', site, timeout,
                                             {k: len(v) for k, v in got.items()})
    finally:
        try:
            await gen.aclose()
        except Exception:
            pass
    return got['sale'] + got['rent']


def coverage_rows(listings: list[dict]) -> dict[str, tuple[int, int]]:
    n = len(listings)
    return {f: (sum(1 for l in listings if l.get(f) is not None and l.get(f) != ''), n) for f in FIELDS}


def report() -> None:
    files = sorted(OUT.glob('*.json'))
    if not files:
        print('no results in', OUT)
        return
    cols = ['description', 'size_m2', 'bedrooms', 'bathrooms', 'parking', 'levy_monthly',
            'rates_monthly', 'erf_size', 'pet_friendly', 'is_furnished', 'security', 'views']
    for title, pick in (('ALL LISTINGS', lambda l: True), ('SALE ONLY', lambda l: l.get('listing_type') == 'sale')):
        print(f'\n{title}\n{"site":<18}{"n":>4} ' + ' '.join(c[:8].rjust(8) for c in cols))
        tot = {c: [0, 0] for c in cols}
        for f in files:
            ls = [l for l in json.loads(f.read_text()) if pick(l)]
            if not ls:
                continue
            row = []
            for c in cols:
                k = sum(1 for l in ls if l.get(c) is not None and l.get(c) != '')
                tot[c][0] += k
                tot[c][1] += len(ls)
                row.append(f'{100 * k / len(ls):.0f}%'.rjust(8))
            print(f'{f.stem:<18}{len(ls):>4} ' + ' '.join(row))
        n_all = max(t[1] for t in tot.values())
        print(f'{"TOTAL":<18}{n_all:>4} ' + ' '.join(f'{100 * t[0] / t[1]:.0f}%'.rjust(8) if t[1] else '   -'.rjust(8) for t in tot.values()))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('sites', nargs='*')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--limit', type=int, default=12, help='listings per type per site')
    ap.add_argument('--timeout', type=int, default=900, help='seconds per site')
    ap.add_argument('--report', action='store_true')
    args = ap.parse_args()

    if args.report:
        report()
        return

    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    _make_read_only()
    OUT.mkdir(parents=True, exist_ok=True)
    for site in (SITES if args.all else args.sites):
        t0 = time.time()
        try:
            listings = asyncio.run(run_site(site, args.limit, args.timeout))
        except Exception as e:
            print(f'{site}: FAILED {type(e).__name__}: {e}')
            continue
        (OUT / f'{site}.json').write_text(json.dumps(listings, indent=1, default=str))
        cov = coverage_rows(listings)
        print(f'{site}: {len(listings)} listings in {time.time() - t0:.0f}s | ' +
              ' '.join(f'{f}={100 * a / b:.0f}%' for f, (a, b) in cov.items() if b and f in
                       ('description', 'size_m2', 'levy_monthly', 'parking')))


if __name__ == '__main__':
    main()
