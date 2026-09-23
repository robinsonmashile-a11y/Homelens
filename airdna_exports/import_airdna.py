"""
Homelens — AirDNA short-term stats importer.

AirDNA exports come as WIDE, per-metric files (bedrooms across columns, one file
per metric per suburb). Your str_market_stats table is LONG (one row per
suburb + month + bedroom). This script transforms the former into the latter.

For each suburb it needs two files:
    rateByBedroom_*_<suburb>.csv       -> avg_daily_rate      per bedroom/month
    revenueByBedroom_*_<suburb>.csv    -> avg_monthly_revenue per bedroom/month
The suburb-wide summary files (rateByDailyAverage_*, revenueAverage_*) are NOT
imported — they duplicate the per-bedroom data at a coarser grain.

The SUBURB is derived from the filename suffix (e.g. "..._sea-point.csv" ->
"sea-point"), so drop all suburbs' files in one folder and run once.

USAGE
    # dry run: build the import CSV, change nothing in the DB
    python import_airdna.py --dir ./airdna_exports --out str_market_stats_import.csv

    # also upload straight to Supabase (needs SUPABASE_URL / SUPABASE_KEY)
    python import_airdna.py --dir ./airdna_exports --upload

SUBURB NAMING
    The filename suffix must match your suburb_normalised values. Map any that
    don't in SUBURB_ALIASES below (e.g. 'cbd' -> 'cape-town-city-centre').
"""
from __future__ import annotations

import argparse
import csv
import glob
import logging
import os
import re
import sys
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s',
                    datefmt='%H:%M:%S')
logger = logging.getLogger('airdna')

# Filename suffix -> suburb_normalised, for any that don't already match.
# AirDNA exports use squashed names (seapoint); the DB uses hyphenated
# suburb_normalised values (sea-point). Map every one here.
SUBURB_ALIASES = {
    'cbd':         'cape-town-city-centre',
    'seapoint':    'sea-point',
    'greenpoint':  'green-point',
    'dewaterkant': 'de-waterkant',
    'fresnaye':    'fresnaye',
    'rondebosch':  'rondebosch',
    'vredehoek':   'vredehoek',
    'woodstock':   'woodstock',
    'gardens':     'gardens',
}

BEDROOM_COLUMNS = {
    '1 bedroom': '1', '2 bedroom': '2', '3 bedroom': '3',
    '4 bedroom': '4', '5 bedroom': '5', '6+ bedroom': '6+',
}

RATE_PREFIX = 'ratebybedroom'
REV_PREFIX  = 'revenuebybedroom'


def _num(v):
    """Parse a numeric cell; blank / '0' / junk -> None (means 'no data')."""
    if v is None:
        return None
    v = v.strip().strip('"')
    if v in ('', '0', '0.0', '0.00'):
        return None
    try:
        f = float(v)
        return f if f > 0 else None
    except ValueError:
        return None


def _suburb_from_filename(path: str) -> str | None:
    """
    Derive suburb_normalised from a filename like
    'revenueByBedroom_last_3_years_sea-point.csv'.
    Takes the trailing token after the known '..._last_3_years_' marker, or the
    last underscore-separated token before .csv as a fallback.
    """
    name = os.path.basename(path).rsplit('.', 1)[0].lower()
    m = re.search(r'last_\d+_years?_(.+)$', name)
    suffix = m.group(1) if m else name.split('_')[-1]
    return SUBURB_ALIASES.get(suffix, suffix)


def _load_wide(path: str) -> dict[tuple[str, str], float]:
    """
    Read a wide per-bedroom file. Returns {(month, bed_code): value} for every
    usable cell (blanks/zeros dropped). BOM handled via utf-8-sig.
    """
    out: dict[tuple[str, str], float] = {}
    with open(path, encoding='utf-8-sig', newline='') as fh:
        reader = csv.DictReader(fh)
        # normalise header keys (strip stray quotes/whitespace)
        for row in reader:
            row = { (k or '').strip().strip('"'): v for k, v in row.items() }
            month = (row.get('Date') or '').strip().strip('"')
            if not month:
                continue
            month = month[:10]  # YYYY-MM-DD
            for col, bed in BEDROOM_COLUMNS.items():
                val = _num(row.get(col))
                if val is not None:
                    out[(month, bed)] = val
    return out


def _find_pairs(directory: str) -> dict[str, dict[str, str]]:
    """Group files by suburb -> {'rate': path, 'rev': path}."""
    pairs: dict[str, dict[str, str]] = defaultdict(dict)
    for path in glob.glob(os.path.join(directory, '*.csv')):
        base = os.path.basename(path).lower()
        suburb = _suburb_from_filename(path)
        if base.startswith(RATE_PREFIX):
            pairs[suburb]['rate'] = path
        elif base.startswith(REV_PREFIX):
            pairs[suburb]['rev'] = path
        # summary files (rateByDailyAverage, revenueAverage) ignored on purpose
    return pairs


def build_rows(directory: str) -> list[dict]:
    pairs = _find_pairs(directory)
    if not pairs:
        logger.error('No rateByBedroom / revenueByBedroom files found in %s', directory)
        return []

    rows: list[dict] = []
    for suburb, files in sorted(pairs.items()):
        if 'rev' not in files:
            logger.warning('%s: no revenueByBedroom file — skipped (revenue is required)', suburb)
            continue
        rev  = _load_wide(files['rev'])
        rate = _load_wide(files['rate']) if 'rate' in files else {}
        if 'rate' not in files:
            logger.warning('%s: no rateByBedroom file — daily rate will be blank', suburb)

        n = 0
        for (month, bed), revenue in sorted(rev.items()):
            rows.append({
                'suburb_normalised':   suburb,
                'month':               month,
                'bedroom_count':       bed,
                'avg_daily_rate':      rate.get((month, bed), ''),
                'avg_monthly_revenue': round(revenue, 2),
                'source':              'airdna',
            })
            n += 1
        logger.info('%-24s %3d rows  (%d months)', suburb, n,
                    len({m for m, _ in rev}))
    return rows


def write_csv(rows: list[dict], out_path: str) -> None:
    cols = ['suburb_normalised', 'month', 'bedroom_count',
            'avg_daily_rate', 'avg_monthly_revenue', 'source']
    with open(out_path, 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    logger.info('Wrote %d rows -> %s', len(rows), out_path)


def upload(rows: list[dict]) -> None:
    for key in ('SUPABASE_URL', 'SUPABASE_KEY'):
        if not os.environ.get(key):
            logger.error('Missing %s — cannot upload', key)
            sys.exit(1)
    from supabase import create_client
    client = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_KEY'])
    # normalise blank daily rate to None for the DB
    payload = [{**r, 'avg_daily_rate': (r['avg_daily_rate'] or None)} for r in rows]
    up = 0
    for i in range(0, len(payload), 500):
        chunk = payload[i:i + 500]
        # upsert on the natural key so re-imports don't duplicate
        client.table('str_market_stats').upsert(
            chunk, on_conflict='suburb_normalised,month,bedroom_count,source'
        ).execute()
        up += len(chunk)
    logger.info('Upserted %d rows into str_market_stats', up)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', required=True, help='folder of AirDNA CSV exports')
    ap.add_argument('--out', default='str_market_stats_import.csv',
                    help='output CSV path (default: str_market_stats_import.csv)')
    ap.add_argument('--upload', action='store_true',
                    help='also upsert into Supabase str_market_stats')
    args = ap.parse_args()

    rows = build_rows(args.dir)
    if not rows:
        sys.exit(1)
    write_csv(rows, args.out)
    # quick sanity summary
    subs = sorted({r['suburb_normalised'] for r in rows})
    logger.info('Suburbs: %s', ', '.join(subs))
    if args.upload:
        upload(rows)
    else:
        logger.info('Dry run — no DB changes. Import %s via Supabase, '
                    'or re-run with --upload.', args.out)


if __name__ == '__main__':
    main()
