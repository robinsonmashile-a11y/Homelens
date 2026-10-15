"""
Homelens — Post-scrape data quality validator.

Run this in two stages:

  STAGE 1 — immediately after run_all_scrapers.py:
      python scripts/validate_listings.py --stage post-scrape

  STAGE 2 — immediately after fix_suburbs.py:
      python scripts/validate_listings.py --stage post-suburb

Each stage prints a checklist with PASS / WARN / FAIL per check,
and exits with code 1 if any FAIL is found (so CI can catch it).

Anomalies found in current dataset (as of 2025-07-07):
  - exprealty rent price nulls 66% (extraction_failed)
  - exprealty missing suburb_normalised 32%
  - rawson/sothebys/tyson rent price nulls 35-45%
  - duarte ~37% missing images
  - duarte 'test' junk record (R56,651, suburb='test')
  - sothebys size_m2 up to 3,073 m² on 'other' type (commercial)
  - 68 rent listings >R200K/month (likely commercial misclassification)
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(override=True)


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

# Maximum acceptable null-price rate per source x listing_type.
# Anything above this → FAIL.  These are generous; tighten once scrapers
# are stable.
PRICE_NULL_FAIL: dict[tuple[str, str], int] = {
    # (source, listing_type): max allowed null %
    ('exprealty',    'rent'):  70,   # known issue — still very high
    ('sothebys',     'rent'):  50,
    ('rawson',       'rent'):  45,
    ('tyson',        'rent'):  40,
    ('pamgolding',   'rent'):  40,
    ('engelvolkers', 'rent'):  35,
    ('quay1',        'rent'):  35,
    ('remax',        'rent'):  30,
    ('seeff',        'rent'):  30,
    # sale — should rarely be null
    ('DEFAULT',      'sale'):  10,
    ('DEFAULT',      'rent'):  60,   # fallback for any source not listed above
}

# Minimum expected listing count per source (catches a scraper returning 0)
MIN_LISTINGS: dict[str, int] = {
    'pamgolding':        500,
    'sothebys':          300,
    'tyson':             300,
    'rawson':            300,
    'seeff':             300,
    'remax':             250,
    'quay1':             100,
    'chaseveritt':        50,
    'greeff':            100,
    'jawitz':             50,
    'engelvolkers':       50,
    'century21':          20,
    'harcourts':          50,
    'lionlead':           30,
    'abc_international':  20,
    'justproperty':       20,
    'dgproperties':       20,
    'duarte':             50,
    'exprealty':         500,
}

# Max acceptable % missing image per source
IMAGE_NULL_WARN = 40   # WARN threshold
IMAGE_NULL_FAIL = 60   # FAIL threshold

# Max acceptable % missing suburb_normalised  (post-scrape; post-suburb has tighter)
SUBURB_NULL_FAIL_POST_SCRAPE  = 50
SUBURB_NULL_FAIL_POST_SUBURB  = 5   # after fix_suburbs, almost all should resolve

# Rent price range: outside this → anomaly printed (not a FAIL, just flagged)
RENT_MIN  =    500
RENT_MAX  = 200_000

# Sale price range
SALE_MIN  =  100_000
SALE_MAX  = 200_000_000


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------

GREEN  = '\033[92m'
YELLOW = '\033[93m'
RED    = '\033[91m'
BOLD   = '\033[1m'
RESET  = '\033[0m'

def _pass(msg: str)  -> str: return f'{GREEN}PASS{RESET}  {msg}'
def _warn(msg: str)  -> str: return f'{YELLOW}WARN{RESET}  {msg}'
def _fail(msg: str)  -> str: return f'{RED}FAIL{RESET}  {msg}'
def _info(msg: str)  -> str: return f'      {msg}'


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------

def _fetch_active_listings(city: str = 'cape-town') -> list[dict]:
    """Fetch all active listings (one city, or 'all') from Supabase (paginated)."""
    from scraper.database import get_client
    client  = get_client()
    batch   = 1000
    offset  = 0
    results = []

    while True:
        query = (
            client.table('listings')
            .select(
                'listing_id, source, listing_type, price, price_status,'
                'suburb, suburb_normalised, bedrooms, image_url,'
                'size_m2, property_type, listing_url, title, is_active'
            )
            .eq('is_active', True)
        )
        if city != 'all':
            query = query.eq('city', city)
        resp = query.order('listing_id').range(offset, offset + batch - 1).execute()
        chunk = resp.data or []
        results.extend(chunk)
        if len(chunk) < batch:
            break
        offset += batch

    return results


# ---------------------------------------------------------------------------
# Check helpers
# ---------------------------------------------------------------------------

def _null(val) -> bool:
    return val is None or str(val).strip() in ('', 'None', 'null', '0')


def _price_null_rate(rows: list[dict]) -> float:
    if not rows:
        return 0.0
    null = sum(1 for r in rows if _null(r.get('price')))
    return round(100 * null / len(rows))


def _get_threshold(source: str, ltype: str) -> int:
    return PRICE_NULL_FAIL.get((source, ltype), PRICE_NULL_FAIL.get(('DEFAULT', ltype), 60))


# ---------------------------------------------------------------------------
# Check suites
# ---------------------------------------------------------------------------

def check_listing_counts(rows: list[dict]) -> list[tuple[str, str]]:
    """Every source must have at least MIN_LISTINGS active listings."""
    results = []
    from collections import Counter
    by_source = Counter(r['source'] for r in rows)

    for src, minimum in sorted(MIN_LISTINGS.items()):
        count = by_source.get(src, 0)
        if count == 0:
            results.append(('FAIL', f'[{src}]  0 active listings — scraper may have failed entirely'))
        elif count < minimum:
            results.append(('WARN', f'[{src}]  {count} listings (expected ≥ {minimum})'))
        else:
            results.append(('PASS', f'[{src}]  {count} listings'))

    return results


def check_price_nulls(rows: list[dict]) -> list[tuple[str, str]]:
    """Price null rate per source × listing_type must be within threshold."""
    results = []
    sources = sorted(set(r['source'] for r in rows))

    for src in sources:
        for lt in ('sale', 'rent'):
            subset = [r for r in rows if r['source'] == src and r.get('listing_type') == lt]
            if not subset:
                continue
            pct       = _price_null_rate(subset)
            threshold = _get_threshold(src, lt)
            msg = f'[{src}][{lt}]  null price: {pct}%  ({sum(1 for r in subset if _null(r.get("price")))}/{len(subset)})  threshold:{threshold}%'
            if pct > threshold:
                results.append(('FAIL', msg))
            elif pct > threshold * 0.75:
                results.append(('WARN', msg))
            else:
                results.append(('PASS', msg))

    return results


def check_price_status(rows: list[dict]) -> list[tuple[str, str]]:
    """extraction_failed should be low. Flag any source with >15% extraction_failed."""
    from collections import Counter
    results = []
    sources = sorted(set(r['source'] for r in rows))

    for src in sources:
        for lt in ('sale', 'rent'):
            subset = [r for r in rows if r['source'] == src and r.get('listing_type') == lt]
            if not subset:
                continue
            ctr   = Counter(r.get('price_status', 'unknown') for r in subset)
            fail  = ctr.get('extraction_failed', 0)
            poa   = ctr.get('poa', 0)
            unk   = ctr.get('unknown', 0)
            ok    = ctr.get('ok', 0)
            total = len(subset)
            fail_pct = round(100 * fail / total)
            detail = f'ok:{ok}  poa:{poa}  fail:{fail}({fail_pct}%)  unk:{unk}'
            if fail_pct > 20:
                results.append(('FAIL', f'[{src}][{lt}]  extraction_failed rate {fail_pct}%  — {detail}'))
            elif fail_pct > 10:
                results.append(('WARN', f'[{src}][{lt}]  extraction_failed rate {fail_pct}%  — {detail}'))
            else:
                results.append(('PASS', f'[{src}][{lt}]  price_status ok  — {detail}'))

    return results


def check_suburb_null(rows: list[dict], stage: str) -> list[tuple[str, str]]:
    """suburb_normalised null rate — stricter after fix_suburbs."""
    results = []
    threshold = SUBURB_NULL_FAIL_POST_SUBURB if stage == 'post-suburb' else SUBURB_NULL_FAIL_POST_SCRAPE
    sources = sorted(set(r['source'] for r in rows))

    for src in sources:
        subset  = [r for r in rows if r['source'] == src]
        null_s  = sum(1 for r in subset if _null(r.get('suburb_normalised')))
        pct     = round(100 * null_s / len(subset)) if subset else 0
        msg     = f'[{src}]  suburb_normalised null: {pct}%  ({null_s}/{len(subset)})'
        if pct > threshold:
            results.append(('FAIL', msg))
        elif pct > threshold * 0.5:
            results.append(('WARN', msg))
        else:
            results.append(('PASS', msg))

    return results


def check_images(rows: list[dict]) -> list[tuple[str, str]]:
    """Flag sources with high missing image rates."""
    results = []
    sources = sorted(set(r['source'] for r in rows))

    for src in sources:
        subset  = [r for r in rows if r['source'] == src]
        no_img  = sum(1 for r in subset if _null(r.get('image_url')))
        pct     = round(100 * no_img / len(subset)) if subset else 0
        msg     = f'[{src}]  missing images: {pct}%  ({no_img}/{len(subset)})'
        if pct > IMAGE_NULL_FAIL:
            results.append(('FAIL', msg))
        elif pct > IMAGE_NULL_WARN:
            results.append(('WARN', msg))
        else:
            results.append(('PASS', msg))

    return results


def check_price_anomalies(rows: list[dict]) -> list[tuple[str, str]]:
    """Flag listings with prices outside expected ranges."""
    results  = []
    anomalies: list[str] = []

    for r in rows:
        raw = r.get('price')
        if _null(raw):
            continue
        try:
            p = int(float(raw))
        except (ValueError, TypeError):
            continue

        lt     = r.get('listing_type', '')
        src    = r.get('source', '')
        suburb = (r.get('suburb_normalised') or r.get('suburb') or '').strip()
        title  = (r.get('title') or '')[:40]

        if lt == 'rent':
            if p < RENT_MIN:
                anomalies.append(f'  RENT too low   R{p:,}  [{src}]  {suburb}  "{title}"')
            elif p > RENT_MAX:
                anomalies.append(f'  RENT too high  R{p:,}  [{src}]  {suburb}  "{title}"')
        elif lt == 'sale':
            if p < SALE_MIN:
                anomalies.append(f'  SALE too low   R{p:,}  [{src}]  {suburb}  "{title}"')
            elif p > SALE_MAX:
                anomalies.append(f'  SALE too high  R{p:,}  [{src}]  {suburb}  "{title}"')

    if anomalies:
        results.append(('WARN', f'{len(anomalies)} price anomalies found:'))
        for a in anomalies[:40]:
            results.append(('INFO', a))
        if len(anomalies) > 40:
            results.append(('INFO', f'  ... and {len(anomalies) - 40} more'))
    else:
        results.append(('PASS', 'No price anomalies (outside R500–R200K rent / R100K–R200M sale)'))

    return results


def check_junk_records(rows: list[dict]) -> list[tuple[str, str]]:
    """Catch obvious junk: 'test' suburbs, placeholder titles, etc."""
    results  = []
    junk: list[str] = []

    junk_patterns = ('test', 'placeholder', 'sample', 'dummy', 'delete me', 'n/a')

    for r in rows:
        suburb = (r.get('suburb') or '').lower().strip()
        title  = (r.get('title') or '').lower().strip()
        src    = r.get('source', '')
        lid    = r.get('listing_id', '')

        if any(p in suburb for p in junk_patterns):
            junk.append(f'  junk suburb="{r["suburb"]}"  [{src}]  id:{lid}')
        elif any(p in title for p in ('test listing', 'test property')):
            junk.append(f'  junk title="{r["title"][:50]}"  [{src}]  id:{lid}')

    if junk:
        results.append(('FAIL', f'{len(junk)} junk records found (deactivate or delete):'))
        for j in junk:
            results.append(('INFO', j))
    else:
        results.append(('PASS', 'No junk records found'))

    return results


def check_size_anomalies(rows: list[dict]) -> list[tuple[str, str]]:
    """
    Apartments and townhouses with size_m2 > 400 are suspect (erf/plot size leak).
    Houses with size_m2 > 2000 are suspect.
    """
    results  = []
    suspects: list[str] = []

    caps = {'apartment': 400, 'townhouse': 600, 'house': 2000}

    for r in rows:
        ptype = (r.get('property_type') or '').lower()
        cap   = caps.get(ptype)
        if cap is None:
            continue
        raw = r.get('size_m2')
        if _null(raw):
            continue
        try:
            s = float(raw)
        except (ValueError, TypeError):
            continue
        if s > cap:
            src    = r.get('source', '')
            suburb = (r.get('suburb_normalised') or r.get('suburb') or '').strip()
            suspects.append(f'  {src:<18} {ptype:<12} {s:>7.0f} m²  {suburb}')

    if suspects:
        results.append(('WARN', f'{len(suspects)} size_m2 values exceed type cap (likely ERF/plot, not floor area):'))
        for s in suspects[:30]:
            results.append(('INFO', s))
        if len(suspects) > 30:
            results.append(('INFO', f'  ... and {len(suspects) - 30} more'))
    else:
        results.append(('PASS', 'No size_m2 anomalies'))

    return results


def check_duplicate_urls(rows: list[dict]) -> list[tuple[str, str]]:
    """listing_url should be unique across active listings."""
    from collections import Counter
    urls = [r.get('listing_url', '') for r in rows if r.get('listing_url')]
    ctr  = Counter(urls)
    dupes = [(u, c) for u, c in ctr.items() if c > 1]

    if dupes:
        return [('FAIL', f'{len(dupes)} duplicate listing_urls found (upsert keying issue):')]  + [
            ('INFO', f'  count:{c}  {u[:80]}') for u, c in sorted(dupes, key=lambda x: -x[1])[:10]
        ]
    return [('PASS', 'No duplicate listing_urls')]


def check_source_coverage(rows: list[dict]) -> list[tuple[str, str]]:
    """All 19 expected sources must be present."""
    expected = {
        'pamgolding', 'sothebys', 'tyson', 'rawson', 'seeff', 'remax',
        'quay1', 'chaseveritt', 'greeff', 'jawitz', 'engelvolkers',
        'century21', 'harcourts', 'lionlead', 'abc_international',
        'justproperty', 'dgproperties', 'duarte', 'exprealty',
    }
    found   = set(r['source'] for r in rows)
    missing = expected - found
    extra   = found - expected

    results = []
    if missing:
        results.append(('FAIL', f'Missing sources (0 listings): {", ".join(sorted(missing))}'))
    else:
        results.append(('PASS', f'All 19 sources present'))
    if extra:
        results.append(('WARN', f'Unexpected sources found: {", ".join(sorted(extra))}'))
    return results


# ---------------------------------------------------------------------------
# Stage-specific known issues note
# ---------------------------------------------------------------------------

KNOWN_ISSUES = """
  Known issues in current dataset (tracked — not blocking):
    - exprealty rent price extraction_failed ~66%  (CSS/API selector bug)
    - exprealty suburb_normalised missing ~32%     (geocoding gap)
    - sothebys/rawson/tyson rent null price ~35-45% (selector investigation needed)
    - duarte ~37% missing images                   (scraper not capturing image)
    - 68 rent listings >R200K/month                (likely commercial, classify or filter)
    - duarte 'test' record (R56,651, suburb=test)  (delete manually)
"""


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_checks(stage: str, city: str = 'cape-town') -> None:
    print()
    print('═' * 70)
    print(f'  HOMELENS DATA QUALITY VALIDATOR  —  stage: {stage.upper()}')
    print(f'  {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}')
    print('═' * 70)
    print()
    print(f'  City: {city}')
    print('  Fetching active listings from Supabase...')

    rows = _fetch_active_listings(city)
    print(f'  Loaded {len(rows):,} active listings.\n')

    if not rows:
        print(_fail('No active listings returned — nothing to validate.'))
        sys.exit(1)

    # Build check list based on stage
    sections: list[tuple[str, list[tuple[str, str]]]] = [
        ('1. Source coverage (all 19 agencies present)',
            check_source_coverage(rows)),
        ('2. Listing counts per source',
            check_listing_counts(rows)),
        ('3. Price null rates by source × type',
            check_price_nulls(rows)),
        ('4. Price status breakdown (extraction_failed rate)',
            check_price_status(rows)),
        ('5. Suburb_normalised null rates' + (' (post-fix_suburbs: ≤5%)' if stage == 'post-suburb' else ' (pre-fix_suburbs)'),
            check_suburb_null(rows, stage)),
        ('6. Image coverage per source',
            check_images(rows)),
        ('7. Price anomalies (out-of-range values)',
            check_price_anomalies(rows)),
        ('8. Junk records (test/placeholder suburbs or titles)',
            check_junk_records(rows)),
        ('9. Size m² anomalies (ERF/plot size leaking as floor area)',
            check_size_anomalies(rows)),
        ('10. Duplicate listing_urls',
            check_duplicate_urls(rows)),
    ]

    fail_count = 0
    warn_count = 0

    for section_title, checks in sections:
        print(f'  {BOLD}── {section_title}{RESET}')
        for level, msg in checks:
            if level == 'PASS':
                print(f'    {_pass(msg)}')
            elif level == 'WARN':
                print(f'    {_warn(msg)}')
                warn_count += 1
            elif level == 'FAIL':
                print(f'    {_fail(msg)}')
                fail_count += 1
            else:  # INFO
                print(f'    {_info(msg)}')
        print()

    # Known issues note
    print(f'  {BOLD}── Known issues (tracked, not blocking){RESET}')
    print(KNOWN_ISSUES)

    # Summary
    print('═' * 70)
    if fail_count == 0 and warn_count == 0:
        print(f'  {GREEN}{BOLD}ALL CHECKS PASSED{RESET}  ({len(rows):,} active listings)')
    elif fail_count == 0:
        print(f'  {YELLOW}{BOLD}{warn_count} WARNING(S){RESET}, 0 failures  ({len(rows):,} listings)')
        print('  Safe to proceed — review warnings.')
    else:
        print(f'  {RED}{BOLD}{fail_count} FAILURE(S){RESET}, {warn_count} warning(s)  ({len(rows):,} listings)')
        print('  Investigate failures before running engine pipeline.')
    print('═' * 70)
    print()

    if stage == 'post-suburb':
        print('  Next steps (if all checks pass):')
        print('    python scripts/run_engine.py')
        print('    python scripts/compute_investment.py')
        print('    python scripts/generate_explanations.py')
        print()
    else:
        print('  Next step:')
        print('    python scripts/fix_suburbs.py')
        print('    python scripts/validate_listings.py --stage post-suburb')
        print()

    sys.exit(1 if fail_count > 0 else 0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Homelens listing data validator')
    parser.add_argument(
        '--stage',
        choices=['post-scrape', 'post-suburb'],
        default='post-scrape',
        help='post-scrape: run after run_all_scrapers.py. post-suburb: run after fix_suburbs.py.',
    )
    parser.add_argument(
        '--city', default='cape-town',
        help="city slug to validate, or 'all' (default: cape-town; thresholds are Cape Town-sized)",
    )
    args = parser.parse_args()
    run_checks(args.stage, args.city)
