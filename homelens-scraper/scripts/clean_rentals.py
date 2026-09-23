"""
Homelens — Rental Data Cleaner (v1)

WHY THIS EXISTS
---------------
The rental feed is polluted with listings that are not residential long-term
rentals. Left in, they (a) appear on the consumer rental pages as absurd
results, and (b) poison suburb baselines so genuine homes get scored against
warehouses. This script flags them BEFORE the engine runs, so the engine only
ever sees real rental homes.

It does NOT delete anything. It sets `is_outlier = TRUE` (the flag the engine
already excludes from baselines) and records why in `exclusion_reason`, so
every decision is auditable and reversible.

RUN ORDER (updated):
    python scripts/fix_suburbs.py
    python scripts/clean_rentals.py        <-- NEW: this script
    python scripts/run_engine.py
    python scripts/compute_attractiveness.py
    python scripts/compute_investment.py
    python scripts/generate_explanations.py

MIGRATION (run ONCE in Supabase SQL editor):
    ALTER TABLE listings ADD COLUMN IF NOT EXISTS exclusion_reason TEXT;

USAGE
    python scripts/clean_rentals.py            # apply to database
    python scripts/clean_rentals.py --dry-run  # report only, change nothing
    python scripts/clean_rentals.py --csv path/to/listings.csv --dry-run
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('homelens.clean_rentals')

MIGRATION_SQL = """\
ALTER TABLE listings ADD COLUMN IF NOT EXISTS exclusion_reason TEXT;
"""

# ── Thresholds ───────────────────────────────────────────────────────────────
MIN_MONTHLY_RENT = 3_000     # below this is per-night/per-week/deposit/error
MAX_MONTHLY_RENT = 300_000   # above this is commercial or data error
MAX_BEDROOMS     = 12        # above this is a guesthouse/hotel, not a rental home
MAX_SIZE_M2      = 800       # residential ceiling; above = warehouse/plot size

# ── Classification patterns ──────────────────────────────────────────────────
# Commercial / industrial — the biggest contaminant.
RE_COMMERCIAL = re.compile(
    r'\b(warehouse|industrial|factory|showroom|business\s*park|workshop|'
    r'logistics|distribution\s*cent|mini.?factory|office\s*(space|park|to\s*let)|'
    r'retail\s*(space|unit|shop)|commercial\s*(space|property|unit|premises)|'
    r'shop\s*(to\s*let|for\s*rent)|yard\s*to\s*let|storage\s*(unit|facility)|'
    r'consulting\s*rooms?|medical\s*suite)\b', re.I)

# Short-stay / holiday lets — priced per night or per week, not per month.
RE_SHORT_STAY = re.compile(
    r'\b(per\s*night|nightly|per\s*day|daily\s*rental|daily\s*rate|per\s*week|'
    r'weekly\s*rate|short.?term\s*(let|rental|stay)|holiday\s*(let|home|rental)|'
    r'air\s?bnb|self.?catering|minimum\s*stay|seasonal\s*(let|rental))\b', re.I)

# Room / house shares — a room is not a dwelling; skews per-bedroom medians.
RE_ROOM_SHARE = re.compile(
    r'\b(room\s*in\s*a?\s*(shared|house|apartment|flat)|shared\s*'
    r'(house|apartment|flat|accommodation|living)|housemate|co.?living|'
    r'student\s*(room|accommodation|digs)|\bdigs\b|bed\s*in\s*a)\b', re.I)

# Parking bays, garages, lock-ups — not dwellings. TITLE ONLY, and the
# parking must be the SUBJECT of the listing ("Parking Bay To Let"), never a
# feature mentioned in passing ("2 Bed Apartment with garage").
RE_PARKING = re.compile(
    r'^(?:[^|]*\b)?(parking\s*(bay|space)s?|lock.?up\s*garages?|'
    r'garages?|carports?)\s*(to\s*let|for\s*rent|available)', re.I)

# Phrases so unambiguous they are safe to match in the DESCRIPTION as well.
# Deliberately narrow — anything that could describe a feature of a normal
# home belongs in the title-only patterns above.
RE_DESC_ONLY = re.compile(
    r'\b(price\s*per\s*night|rate\s*per\s*night|per\s*night\s*rate|'
    r'minimum\s*stay\s*of\s*\d|nightly\s*rate|zoned\s*(industrial|commercial)|'
    r'gla\s*of\s*\d|warehouse\s*space\s*of)\b', re.I)

REASONS = {
    'commercial':    'Commercial/industrial property, not a residential rental',
    'short_stay':    'Short-stay/holiday let — price is not a monthly rent',
    'room_share':    'Room or shared accommodation, not a whole dwelling',
    'parking':       'Parking/garage/storage, not a dwelling',
    'rent_too_low':  f'Rent below R{MIN_MONTHLY_RENT:,} — likely per-night/deposit/error',
    'rent_too_high': f'Rent above R{MAX_MONTHLY_RENT:,} — likely commercial or error',
    'too_many_beds': f'More than {MAX_BEDROOMS} bedrooms — guesthouse/commercial',
    'size_implausible': f'Size above {MAX_SIZE_M2}m² — warehouse or plot size, not floor area',
    'no_price':      'No usable price',
}


def _title(listing: dict) -> str:
    """
    Title only. The title declares WHAT the property is; descriptions merely
    mention features. Matching descriptions caused severe false positives —
    e.g. "1 Garage" in a normal flat's description triggered the parking rule,
    and "close to shops" triggered the commercial rule. Titles are the
    reliable signal.
    """
    return listing.get('title') or ''


def _desc(listing: dict) -> str:
    """Description, used ONLY for unambiguous phrases (see RE_DESC_ONLY)."""
    return (listing.get('description') or '')[:600]


def _num(v):
    try:
        return float(v) if v not in ('', None) else None
    except (TypeError, ValueError):
        return None


def classify(listing: dict) -> str | None:
    """
    Return an exclusion reason key, or None if the listing is a clean
    residential rental. Order matters: most specific/confident first.
    """
    title = _title(listing)
    desc  = _desc(listing)
    price = _num(listing.get('price'))
    beds  = _num(listing.get('bedrooms'))
    size  = _num(listing.get('size_m2'))

    # 1. Keyword classes — TITLE ONLY (see _title for why), plus a short list
    #    of unambiguous description phrases.
    if RE_COMMERCIAL.search(title):
        return 'commercial'
    if RE_SHORT_STAY.search(title):
        return 'short_stay'
    if RE_ROOM_SHARE.search(title):
        return 'room_share'
    if RE_PARKING.search(title):
        return 'parking'
    if RE_DESC_ONLY.search(desc):
        return 'short_stay' if 'night' in desc.lower() or 'stay' in desc.lower() else 'commercial'

    # 2. Price sanity
    if price is None or price <= 0:
        return 'no_price'
    if price < MIN_MONTHLY_RENT:
        return 'rent_too_low'
    if price > MAX_MONTHLY_RENT:
        return 'rent_too_high'

    # 3. Structural sanity
    if beds is not None and beds > MAX_BEDROOMS:
        return 'too_many_beds'
    # Oversized listings: only exclude when there are NO bedrooms — that
    # combination means commercial floorspace. A 4-bed house recorded at
    # 1,000m² is a real home with a plot size in the size field; the engine's
    # own SIZE_MIN/SIZE_MAX bounds already stop that value being used for
    # price-per-m², so the listing itself is still valid rental stock.
    if size is not None and size > MAX_SIZE_M2 and (beds is None or beds == 0):
        return 'size_implausible'

    # 4. Zero-bedroom guard: a genuine 0-bed is a studio APARTMENT. A 0-bed
    #    "house" or "other" is almost always commercial that dodged the
    #    keyword filter.
    ptype = (listing.get('property_type') or '').lower()
    if beds == 0 and ptype not in ('apartment', ''):
        return 'commercial'

    return None


# ── Database path ────────────────────────────────────────────────────────────

def _paginate(fetch, page: int = 1000):
    out, off = [], 0
    while True:
        res = fetch(off, page)
        rows = res.data or []
        out.extend(rows)
        if len(rows) < page:
            break
        off += page
    return out


def fetch_rentals(client) -> list[dict]:
    return _paginate(lambda off, lim: (
        client.table('listings')
        .select('listing_id,title,description,price,bedrooms,size_m2,'
                'property_type,suburb_normalised,is_active,is_outlier,'
                'exclusion_reason')
        .eq('listing_type', 'rent')
        .range(off, off + lim - 1)
        .execute()
    ))


def apply_flags(client, decisions: list[tuple[str, str | None]], batch: int = 200) -> int:
    """decisions = [(listing_id, reason_or_None), ...]"""
    updated = 0
    for i in range(0, len(decisions), batch):
        chunk = decisions[i:i + batch]
        for lid, reason in chunk:
            try:
                client.table('listings').update({
                    'is_outlier': reason is not None,
                    'exclusion_reason': reason,
                }).eq('listing_id', lid).execute()
                updated += 1
            except Exception as e:
                logger.warning('update failed for %s: %s', lid, e)
    return updated


def report(rows: list[dict], decisions: dict[str, str | None]) -> None:
    active = [r for r in rows if str(r.get('is_active')).lower() == 'true']
    flagged = {k: v for k, v in decisions.items() if v}
    active_flagged = [r for r in active if decisions.get(r['listing_id'])]

    print()
    print('=' * 68)
    print('  RENTAL CLEANING REPORT')
    print('=' * 68)
    print(f'  Rental listings examined : {len(rows):>6,}')
    print(f'    of which active        : {len(active):>6,}')
    print(f'  Flagged for exclusion    : {len(flagged):>6,}')
    print(f'    of which active        : {len(active_flagged):>6,}')
    print(f'  Clean active rentals     : {len(active) - len(active_flagged):>6,}')
    print()
    print('  Reasons (active listings):')
    counts = Counter(decisions[r['listing_id']] for r in active_flagged)
    for key, n in counts.most_common():
        print(f'    {key:<18} {n:>5,}   {REASONS.get(key, "")}')
    print('=' * 68)
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true',
                    help='report only; do not write to the database')
    ap.add_argument('--csv', help='classify a CSV export instead of the database')
    args = ap.parse_args()

    if args.csv:
        import csv as _csv
        rows = [r for r in _csv.DictReader(open(args.csv))
                if r.get('listing_type') == 'rent']
        logger.info('Loaded %d rental rows from %s', len(rows), args.csv)
    else:
        for key in ('SUPABASE_URL', 'SUPABASE_KEY'):
            if not os.environ.get(key):
                logger.error('Missing environment variable: %s', key)
                sys.exit(1)
        from scraper.database import get_client
        client = get_client()
        logger.info('Fetching rental listings…')
        rows = fetch_rentals(client)
        logger.info('  %d rental listings loaded', len(rows))

    decisions = {r['listing_id']: classify(r) for r in rows}
    report(rows, decisions)

    if args.csv or args.dry_run:
        logger.info('Dry run — no changes written.')
        return

    changed = [(lid, reason) for lid, reason in decisions.items()]
    logger.info('Writing flags for %d listings…', len(changed))
    n = apply_flags(client, changed)
    logger.info('  %d listings updated. Now re-run the engine.', n)


if __name__ == '__main__':
    main()
