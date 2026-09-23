"""
Homelens — Quay 1 scraper entry point.

Scrapes Cape Town property listings from quay1.co.za and writes them to Supabase.
Logs each run to scrape_runs and marks stale listings inactive.

Usage:
    python scripts/run_quay1.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('homelens.run_quay1')


def _check_env() -> None:
    required = ['SUPABASE_URL', 'SUPABASE_KEY']
    missing  = [k for k in required if not os.environ.get(k)]
    if missing:
        logger.error('Missing environment variables: %s', ', '.join(missing))
        sys.exit(1)


async def main() -> None:
    _check_env()

    from scraper.database import upsert_listing_by_url, log_scrape_run, update_scrape_run, mark_inactive_listings
    from scraper.quay1    import run_quay1_scraper

    print()
    print('━' * 55)
    print('  Homelens — Quay 1 Scraper')
    print('━' * 55)

    run_id = log_scrape_run({
        'source':     'quay1',
        'status':     'running',
        'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    })

    inserted   = 0
    updated    = 0
    skipped    = 0
    errors     = 0
    active_ids: list[str] = []

    t_start = time.time()

    try:
        async for listing in run_quay1_scraper():
            try:
                result = upsert_listing_by_url(listing)
                active_ids.append(listing['listing_id'])

                if result == 'new':
                    inserted += 1
                    flag = '+'
                elif result == 'updated':
                    updated += 1
                    flag = '~'
                else:
                    skipped += 1
                    flag = '='

                suburb = (listing.get('suburb_normalised') or '').replace('-', ' ').title()
                title  = (listing.get('title') or 'Listing')[:45]
                price  = listing.get('price') or 0
                print(f'  [{flag}] {title} ({suburb})  R{price:,}')

            except Exception as e:
                logger.error('DB write failed for %s: %s', listing.get('listing_id'), e)
                errors += 1

    except Exception as e:
        logger.error('Scraper failed: %s', e)
        try:
            update_scrape_run(run_id, {'status': 'failed'})
        except Exception:
            pass
        raise

    # Mark stale listings inactive
    if active_ids:
        mark_inactive_listings('quay1', active_ids)

    elapsed = round(time.time() - t_start, 1)
    total   = inserted + updated + skipped

    try:
        update_scrape_run(run_id, {
            'status':           'completed',
            'completed_at':     time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'listings_found':   total,
            'listings_new':     inserted,
            'listings_updated': updated,
            'errors':           errors,
        })
    except Exception as e:
        logger.warning('Could not update scrape run: %s', e)

    print()
    print('━' * 55)
    print(f'  New listings    : {inserted:>6,}')
    print(f'  Updated         : {updated:>6,}')
    print(f'  Unchanged       : {skipped:>6,}')
    print(f'  Errors          : {errors:>6,}')
    print(f'  Total active    : {total:>6,}')
    print(f'  Time elapsed    : {elapsed}s')
    print('━' * 55)
    print()


if __name__ == '__main__':
    asyncio.run(main())
