"""
Snapshot current rent listing prices to listings_price_backup before re-scraping.

Run this ONCE before running the fixed rent scrapers so you can diff
before/after price capture rates.  Safe to re-run — inserts a new
snapshot batch each time (distinguished by backed_up_at timestamp).

Usage:
  cd homelens-scraper
  python -m scripts.backup_rent_prices

Prerequisites:
  SUPABASE_URL and SUPABASE_KEY must be set in environment.
  The listings_price_backup table must exist (created by the migration SQL).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from scraper.database import get_client

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger('backup_rent_prices')

BATCH_SIZE = 1_000


def run_backup() -> None:
    client = get_client()
    now    = datetime.now(timezone.utc).isoformat()

    logger.info('Fetching rent listings for snapshot...')

    backed_up = 0
    offset    = 0

    while True:
        result = (
            client.table('listings')
            .select(
                'listing_id, source, listing_type, price, price_status, '
                'suburb_normalised, is_active, last_scraped_at'
            )
            .eq('listing_type', 'rent')
            .range(offset, offset + BATCH_SIZE - 1)
            .execute()
        )

        rows = result.data or []
        if not rows:
            break

        backup_rows = [
            {
                **row,
                'backed_up_at': now,
            }
            for row in rows
        ]

        client.table('listings_price_backup').insert(backup_rows).execute()
        backed_up += len(rows)
        logger.info('  ... backed up %d rows (total so far: %d)', len(rows), backed_up)

        if len(rows) < BATCH_SIZE:
            break
        offset += BATCH_SIZE

    logger.info('Backup complete: %d rent listings snapshotted at %s', backed_up, now)

    # Print a quick count by source + price_status
    logger.info('\nSnapshot breakdown by source × price_status:')
    summary = (
        client.table('listings_price_backup')
        .select('source, price_status, listing_id')
        .eq('backed_up_at', now)
        .execute()
    )

    from collections import Counter
    counts: Counter = Counter()
    for row in (summary.data or []):
        counts[(row['source'], row.get('price_status') or 'unknown')] += 1

    print(f"\n{'Source':<22} {'price_status':<20} Count")
    print('-' * 55)
    for (source, status), count in sorted(counts.items()):
        print(f"  {source:<20} {status:<20} {count}")


if __name__ == '__main__':
    run_backup()
