"""
Homelens — Rawson scraper entry point (v2).

Scrapes Cape Town property listings from rawson.co.za and writes them to Supabase.
Now visits each listing detail page to extract size_m2, full description,
has_parking, and has_garden.

Logs each run to scrape_runs and marks stale listings inactive.

Usage:
    python scripts/run_rawson.py
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
logger = logging.getLogger('homelens.run_rawson')


def _check_env() -> None:
    required = ['SUPABASE_URL', 'SUPABASE_KEY']
    missing  = [k for k in required if not os.environ.get(k)]
    if missing:
        logger.error('Missing environment variables: %s', ', '.join(missing))
        sys.exit(1)


async def main() -> None:
    _check_env()

    from scraper.database import upsert_listing_by_url, log_scrape_run, update_scrape_run, mark_inactive_listings
    from scraper.rawson   import run_rawson_scraper

    print()
    print('━' * 55)
    print('  Homelens — Rawson Scraper')
    print('━' * 55)

    run_id = log_scrape_run({
        'source':     'rawson',
        'status':     'running',
        'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    })

    inserted     = 0
    updated      = 0
    skipped      = 0
    errors       = 0
    with_size    = 0
    with_parking = 0
    with_garden  = 0
    active_ids: list[str] = []

    t_start = time.time()

    try:
        async for listing in run_rawson_scraper():
            try:
                result = upsert_listing_by_url(listing)
                active_ids.append(listing['listing_id'])

                if listing.get('size_m2'):
                    with_size += 1
                if listing.get('has_parking'):
                    with_parking += 1
                if listing.get('has_garden'):
                    with_garden += 1

                if result == 'new':
                    inserted += 1
                    flag = '+'
                elif result == 'updated':
                    updated += 1
                    flag = '~'
                else:
                    skipped += 1
                    flag = '='

                suburb   = (listing.get('suburb_normalised') or '').replace('-', ' ').title()
                title    = (listing.get('title') or 'Listing')[:40]
                price    = listing.get('price') or 0
                size_str = f'{listing["size_m2"]:.0f}m²' if listing.get('size_m2') else 'no size'
                print(f'  [{flag}] {title} ({suburb})  R{price:,}  {size_str}')

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
        mark_inactive_listings('rawson', active_ids)

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

    size_pct    = round(100 * with_size    / total) if total else 0
    parking_pct = round(100 * with_parking / total) if total else 0
    garden_pct  = round(100 * with_garden  / total) if total else 0

    print()
    print('━' * 55)
    print(f'  New listings    : {inserted:>6,}')
    print(f'  Updated         : {updated:>6,}')
    print(f'  Unchanged       : {skipped:>6,}')
    print(f'  Errors          : {errors:>6,}')
    print(f'  Total active    : {total:>6,}')
    print(f'  With size_m2    : {with_size:>6,}  ({size_pct}%)')
    print(f'  With parking    : {with_parking:>6,}  ({parking_pct}%)')
    print(f'  With garden     : {with_garden:>6,}  ({garden_pct}%)')
    print(f'  Time elapsed    : {elapsed}s')
    print('━' * 55)
    print()


if __name__ == '__main__':
    asyncio.run(main())
