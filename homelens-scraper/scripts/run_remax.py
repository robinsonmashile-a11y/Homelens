"""
Homelens RE/MAX scraper entry point.

Usage:
    python scripts/run_remax.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(override=True)

from scraper.database import (
    log_scrape_run,
    mark_inactive_listings,
    update_scrape_run,
    upsert_listing_by_url,
)
from scraper.remax import run_remax_scraper

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('homelens.remax')


def _check_env() -> None:
    required = ['SUPABASE_URL', 'SUPABASE_KEY', 'ANTHROPIC_API_KEY']
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        logger.error('Missing environment variables: %s', ', '.join(missing))
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    _check_env()

    logger.info('=== Homelens RE/MAX Scraper starting ===')
    started_at = datetime.now(timezone.utc).isoformat()

    run_id = None
    try:
        run_id = log_scrape_run({'source': 'remax', 'started_at': started_at, 'status': 'running'})
        logger.info('Scrape run logged — id=%s', run_id)
    except Exception as e:
        logger.warning('Could not log scrape run: %s', e)

    stats = {
        'listings_found': 0,
        'listings_new': 0,
        'listings_updated': 0,
        'listings_unchanged': 0,
        'errors': 0,
    }
    active_listing_ids: list[str] = []
    listing_counter = 0

    try:
        async for listing in run_remax_scraper():
            stats['listings_found'] += 1
            listing_counter += 1
            listing_id  = listing.get('listing_id', 'unknown')
            listing_url = listing.get('listing_url', '')
            title       = listing.get('title', '')[:60]
            price       = listing.get('price')
            price_str   = f"R{price:,}" if price else 'R?'

            # Live progress line
            print(f"  Scraped listing {listing_counter} — {title} — {price_str}")

            try:
                # Strip fields not in DB schema before writing
                for field in ('date_posted',):
                    listing.pop(field, None)

                # Store raw snapshot
                listing['raw_data'] = {k: v for k, v in listing.items() if k not in ('raw_data', 'price_history')}

                result = upsert_listing_by_url(listing)
                active_listing_ids.append(listing_id)

                if result == 'new':
                    stats['listings_new'] += 1
                    logger.info('[NEW]       %s — %s', listing_id, title)
                elif result == 'updated':
                    stats['listings_updated'] += 1
                    logger.info('[UPDATED]   %s — price changed', listing_id)
                else:
                    stats['listings_unchanged'] += 1
                    logger.debug('[UNCHANGED] %s', listing_id)

            except Exception as e:
                stats['errors'] += 1
                logger.error('Error saving listing %s (%s): %s', listing_id, listing_url, e)

    except Exception as e:
        logger.error('Fatal scraper error: %s', e)
        stats['errors'] += 1

    # Mark listings not seen in this run as inactive
    if active_listing_ids:
        try:
            mark_inactive_listings('remax', active_listing_ids)
            logger.info('Marked unseen RE/MAX listings inactive (%d active kept)', len(active_listing_ids))
        except Exception as e:
            logger.warning('Could not mark inactive: %s', e)

    # Finalise scrape run record
    completed_at = datetime.now(timezone.utc).isoformat()
    if run_id is not None:
        try:
            update_scrape_run(run_id, {
                'completed_at':      completed_at,
                'listings_found':    stats['listings_found'],
                'listings_new':      stats['listings_new'],
                'listings_updated':  stats['listings_updated'],
                'errors':            stats['errors'],
                'status':            'completed',
            })
        except Exception as e:
            logger.warning('Could not update scrape run: %s', e)

    # Final summary
    print()
    logger.info('=== RE/MAX Scrape Complete ===')
    logger.info('  Found     : %d', stats['listings_found'])
    logger.info('  New       : %d', stats['listings_new'])
    logger.info('  Updated   : %d', stats['listings_updated'])
    logger.info('  Unchanged : %d', stats['listings_unchanged'])
    logger.info('  Errors    : %d', stats['errors'])
    logger.info('  Completed : %s', completed_at)


if __name__ == '__main__':
    asyncio.run(main())
