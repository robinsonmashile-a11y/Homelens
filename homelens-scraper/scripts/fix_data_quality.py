"""
Homelens data-quality pass — run once (or on-demand) after scraping.

Task 1: Add is_luxury / is_outlier / size_source columns and populate them.
Task 2: Extract size_m2 from description text for listings that are missing it.

Usage:
    python scripts/fix_data_quality.py

Prerequisites:
    Run the migration SQL below in Supabase SQL editor ONCE before this script
    (the script will detect missing columns and print a reminder if needed).

MIGRATION SQL:
─────────────────────────────────────────────────────────────────────────────
ALTER TABLE listings ADD COLUMN IF NOT EXISTS is_luxury    BOOLEAN DEFAULT FALSE;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS is_outlier   BOOLEAN DEFAULT FALSE;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS size_source  TEXT    DEFAULT NULL;
─────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import logging
import re
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(override=True)

import os

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('homelens.fix_data_quality')

# ── Constants ─────────────────────────────────────────────────────────────────

LUXURY_PRICE        = 30_000_000
LUXURY_PM2          = 150_000
LUXURY_BEDROOMS     = 8
LUXURY_SIZE_M2      = 1_500

OUTLIER_STD_MULT    = 2.5
OUTLIER_MIN_SAMPLES = 3

SIZE_MIN = 15.0
SIZE_MAX = 800.0

SIZE_PATTERNS = [
    r'(\d+(?:\.\d+)?)\s*m²',
    r'(\d+(?:\.\d+)?)\s*m2',
    r'(\d+(?:\.\d+)?)\s*sqm',
    r'(\d+(?:\.\d+)?)\s*sq\s*m',
    r'(\d+(?:\.\d+)?)\s*square\s*met',
    r'(\d+(?:\.\d+)?)\s*square\s*m',
    r'floor\s*(?:area|size)[:\s]*(\d+(?:\.\d+)?)',
    r'living\s*(?:area|space)[:\s]*(\d+(?:\.\d+)?)',
    r'(\d+(?:\.\d+)?)\s*(?:square|sq)',
]

MIGRATION_SQL = """\
ALTER TABLE listings ADD COLUMN IF NOT EXISTS is_luxury    BOOLEAN DEFAULT FALSE;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS is_outlier   BOOLEAN DEFAULT FALSE;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS size_source  TEXT    DEFAULT NULL;
"""

UPDATE_BATCH = 100   # listing_ids per IN() update
FETCH_PAGE   = 1000  # rows per paginated GET

# ── Helpers ───────────────────────────────────────────────────────────────────

def _supabase():
    from scraper.database import get_client
    return get_client()


def _paginate(query_fn, page_size: int = FETCH_PAGE) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        batch = (query_fn(offset, page_size).data or [])
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def _check_columns(client) -> bool:
    """
    Return True if is_luxury / is_outlier / size_source columns exist.
    Return False (and log instructions) if they're missing.
    """
    try:
        client.table('listings') \
              .select('listing_id,is_luxury,is_outlier,size_source') \
              .limit(1) \
              .execute()
        return True
    except Exception as e:
        err = str(e).lower()
        if ('pgrst204' in err or '42703' in err or 'schema cache' in err
                or 'could not find' in err or 'does not exist' in err):
            print()
            print('  ⚠  Missing columns detected.')
            print('  Run this SQL in the Supabase SQL editor first, then re-run:')
            print()
            print('  ' + '─' * 65)
            for line in MIGRATION_SQL.strip().splitlines():
                print(f'  {line}')
            print('  ' + '─' * 65)
            print()
            return False
        raise   # unexpected error


def _batch_update(client, ids: list[str], payload: dict) -> None:
    """Update all listing_ids in ids with the given payload, in batches."""
    for i in range(0, len(ids), UPDATE_BATCH):
        batch = ids[i : i + UPDATE_BATCH]
        client.table('listings').update(payload).in_('listing_id', batch).execute()


# ── Task 1a — Mark luxury listings ───────────────────────────────────────────

def mark_luxury(client) -> int:
    """
    Set is_luxury = TRUE where ANY of:
      price > 30M  |  price_per_m2 > 150K  |  bedrooms > 8  |  size_m2 > 1500
    Returns number of listings flagged.
    """
    logger.info('[1a] Fetching all active listings to identify luxury...')

    rows = _paginate(lambda off, lim: (
        client.table('listings')
        .select('listing_id,price,price_per_m2,bedrooms,size_m2')
        .eq('is_active', True)
        .range(off, off + lim - 1)
        .execute()
    ))
    logger.info('  %d active listings fetched', len(rows))

    luxury_ids: list[str] = []
    for r in rows:
        price   = r.get('price')   or 0
        pm2     = r.get('price_per_m2') or 0
        beds    = r.get('bedrooms') or 0
        size    = r.get('size_m2')  or 0
        if (
            price > LUXURY_PRICE
            or pm2   > LUXURY_PM2
            or beds  > LUXURY_BEDROOMS
            or size  > LUXURY_SIZE_M2
        ):
            luxury_ids.append(r['listing_id'])

    if luxury_ids:
        logger.info('  Marking %d listings as luxury...', len(luxury_ids))
        _batch_update(client, luxury_ids, {'is_luxury': True})
    else:
        logger.info('  No luxury listings found.')

    return len(luxury_ids)


# ── Task 1b — Mark outlier listings ──────────────────────────────────────────

def mark_outliers(client) -> int:
    """
    Set is_outlier = TRUE for non-luxury listings where
    price_per_m2 > mean + 2.5 × std for their suburb+bedrooms+type group.
    Group must have at least 3 qualifying listings.
    Returns number flagged.
    """
    logger.info('[1b] Fetching non-luxury listings with price_per_m2 for outlier detection...')

    rows = _paginate(lambda off, lim: (
        client.table('listings')
        .select('listing_id,suburb_normalised,listing_type,bedrooms,price_per_m2')
        .eq('is_active', True)
        .eq('is_luxury', False)
        .filter('price_per_m2', 'not.is', 'null')
        .range(off, off + lim - 1)
        .execute()
    ))
    logger.info('  %d non-luxury priced listings fetched', len(rows))

    # Group by (suburb, bedrooms, listing_type)
    groups: dict[tuple, list[tuple[str, float]]] = defaultdict(list)
    for r in rows:
        key = (
            r.get('suburb_normalised') or 'unknown',
            r.get('bedrooms'),
            r.get('listing_type', 'sale'),
        )
        groups[key].append((r['listing_id'], float(r['price_per_m2'])))

    outlier_ids: list[str] = []
    groups_checked = 0

    for key, items in groups.items():
        if len(items) < OUTLIER_MIN_SAMPLES:
            continue
        groups_checked += 1
        pm2_vals  = [pm2 for _, pm2 in items]
        mean_pm2  = statistics.mean(pm2_vals)
        std_pm2   = statistics.stdev(pm2_vals) if len(pm2_vals) > 1 else 0.0
        threshold = mean_pm2 + OUTLIER_STD_MULT * std_pm2
        for lid, pm2 in items:
            if pm2 > threshold:
                outlier_ids.append(lid)

    logger.info(
        '  Groups checked: %d | Outliers found: %d',
        groups_checked, len(outlier_ids),
    )

    if outlier_ids:
        logger.info('  Marking %d listings as outliers...', len(outlier_ids))
        _batch_update(client, outlier_ids, {'is_outlier': True})
    else:
        logger.info('  No outliers found.')

    return len(outlier_ids)


# ── Task 1c — Summary query ───────────────────────────────────────────────────

def print_flag_summary(client) -> None:
    """Print is_luxury × is_outlier count breakdown."""
    rows = _paginate(lambda off, lim: (
        client.table('listings')
        .select('is_luxury,is_outlier')
        .range(off, off + lim - 1)
        .execute()
    ))

    counts: dict[tuple, int] = defaultdict(int)
    for r in rows:
        key = (bool(r.get('is_luxury')), bool(r.get('is_outlier')))
        counts[key] += 1

    print()
    print('  Flag summary (is_luxury × is_outlier):')
    print(f'  {"is_luxury":<10} {"is_outlier":<10} {"count":>8}')
    print('  ' + '─' * 32)
    for (luxury, outlier), cnt in sorted(counts.items()):
        print(f'  {str(luxury):<10} {str(outlier):<10} {cnt:>8,}')
    print()


# ── Task 2 — Extract size_m2 from description ────────────────────────────────

def _extract_size(description: str) -> float | None:
    """
    Apply SIZE_PATTERNS in order; return first valid match in [SIZE_MIN, SIZE_MAX].
    Returns None if nothing found.
    """
    for pattern in SIZE_PATTERNS:
        m = re.search(pattern, description, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1))
                if SIZE_MIN <= val <= SIZE_MAX:
                    return val
            except (ValueError, IndexError):
                continue
    return None


def extract_sizes(client) -> tuple[int, int, int, list[dict]]:
    """
    For active listings missing size_m2, attempt regex extraction from description.
    Updates size_m2, price_per_m2, and size_source='extracted' on matches.

    Returns (total_missing, extracted, still_missing, samples).
    """
    logger.info('[2] Fetching active listings missing size_m2...')

    rows = _paginate(lambda off, lim: (
        client.table('listings')
        .select('listing_id,description,price')
        .eq('is_active', True)
        .filter('size_m2', 'is', 'null')
        .filter('description', 'not.is', 'null')
        .range(off, off + lim - 1)
        .execute()
    ))

    total_missing = len(rows)
    logger.info('  %d listings missing size_m2 (with description)', total_missing)

    extracted = 0
    samples: list[dict] = []

    for r in rows:
        desc  = r.get('description') or ''
        price = r.get('price')

        size = _extract_size(desc)
        if size is None:
            continue

        update: dict = {
            'size_m2':     size,
            'size_source': 'extracted',
        }
        if price and size > 0:
            update['price_per_m2'] = round(price / size, 2)

        try:
            client.table('listings').update(update).eq(
                'listing_id', r['listing_id']
            ).execute()
            extracted += 1

            if len(samples) < 5:
                # Find the matching snippet for transparency
                snippet = desc[:200].replace('\n', ' ')
                samples.append({
                    'listing_id': r['listing_id'],
                    'size_m2':    size,
                    'snippet':    snippet,
                })
        except Exception as e:
            logger.warning('Size update failed (%s): %s', r.get('listing_id'), e)

    still_missing = total_missing - extracted
    return total_missing, extracted, still_missing, samples


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    required = ['SUPABASE_URL', 'SUPABASE_KEY']
    missing  = [k for k in required if not os.environ.get(k)]
    if missing:
        logger.error('Missing environment variables: %s', ', '.join(missing))
        sys.exit(1)

    client = _supabase()

    print()
    print('━' * 55)
    print('  Homelens Data Quality Fix')
    print('━' * 55)
    print()

    # ── Column check ──────────────────────────────────────────────────────────
    if not _check_columns(client):
        sys.exit(1)

    t_start = time.time()

    # ── Task 1 ────────────────────────────────────────────────────────────────
    print('  ── Task 1: Luxury & Outlier flags ──────────────────')
    n_luxury   = mark_luxury(client)
    n_outliers = mark_outliers(client)
    print_flag_summary(client)

    # ── Task 2 ────────────────────────────────────────────────────────────────
    print('  ── Task 2: Size extraction from descriptions ────────')
    total_miss, n_extracted, still_miss, samples = extract_sizes(client)

    # ── Results ───────────────────────────────────────────────────────────────
    elapsed = round(time.time() - t_start, 1)

    print()
    print('━' * 55)
    print(f'  Luxury flagged    : {n_luxury:>6,} listings')
    print(f'  Outliers flagged  : {n_outliers:>6,} listings')
    print(f'  Size missing (pre): {total_miss:>6,} listings')
    print(f'  Sizes extracted   : {n_extracted:>6,} listings')
    print(f'  Still missing     : {still_miss:>6,} listings')
    print(f'  Time elapsed      : {elapsed}s')
    print('━' * 55)

    if samples:
        print()
        print('  Sample extracted sizes (verify accuracy):')
        print()
        for i, s in enumerate(samples, 1):
            print(f'  [{i}] {s["listing_id"]}')
            print(f'      size_m2  = {s["size_m2"]}')
            print(f'      snippet  = "{s["snippet"][:120]}..."')
            print()

    print()


if __name__ == '__main__':
    main()
