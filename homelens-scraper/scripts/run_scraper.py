"""
Homelens Gumtree scraper entry point.

Usage:
    python scripts/run_scraper.py
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure the project root is on sys.path so `scraper` package resolves
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(override=True)

from scraper.database import log_scrape_run, mark_inactive_listings, update_scrape_run, upsert_listing
from scraper.gumtree import run_scraper
from scraper.parser import parse_listing_description
from scraper.utils import compute_price_per_m2

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('homelens')


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

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

    logger.info('=== Homelens Gumtree Scraper starting ===')
    started_at = datetime.now(timezone.utc).isoformat()

    # Log scrape run start
    run_id: int | None = None
    try:
        run_id = log_scrape_run({
            'source': 'gumtree',
            'started_at': started_at,
            'status': 'running',
        })
        logger.info('Scrape run logged with id=%s', run_id)
    except Exception as e:
        logger.warning('Could not log scrape run start: %s', e)

    stats = {
        'listings_found': 0,
        'listings_new': 0,
        'listings_updated': 0,
        'listings_unchanged': 0,
        'errors': 0,
    }
    active_ids: list[str] = []

    try:
        async for listing in run_scraper():
            stats['listings_found'] += 1
            listing_id = listing.get('listing_id', 'unknown')

            try:
                # 1. Parse description with Claude
                parsed = await parse_listing_description(
                    description=listing.get('description') or listing.get('snippet', ''),
                    title=listing.get('title', ''),
                    price=listing.get('price') or 0,
                )

                # 2. Merge parsed attributes (don't overwrite already-extracted values)
                for key, value in parsed.items():
                    if key not in listing or listing[key] is None:
                        listing[key] = value

                # 3. Suburb from Claude if still missing
                if not listing.get('suburb_normalised') and parsed.get('suburb_mentioned'):
                    from scraper.utils import normalise_suburb
                    listing['suburb_normalised'] = normalise_suburb(parsed['suburb_mentioned'])

                # 4. Compute price per m²
                if listing.get('price') and listing.get('size_m2'):
                    listing['price_per_m2'] = compute_price_per_m2(
                        listing['price'], listing['size_m2']
                    )

                # 5. Remove fields not in DB schema
                for field in ('snippet', 'date_posted', 'listing_type_confirmed', 'suburb_mentioned', 'key_features'):
                    listing.pop(field, None)

                # 6. Store raw data snapshot before writing
                listing['raw_data'] = {
                    k: v for k, v in listing.items()
                    if k not in ('raw_data', 'price_history')
                }

                # 7. Upsert to Supabase
                result = upsert_listing(listing)
                active_ids.append(listing_id)

                if result == 'new':
                    stats['listings_new'] += 1
                    logger.info('[NEW]       %s — %s', listing_id, listing.get('title', '')[:60])
                elif result == 'updated':
                    stats['listings_updated'] += 1
                    logger.info('[UPDATED]   %s — price changed', listing_id)
                else:
                    stats['listings_unchanged'] += 1
                    logger.debug('[UNCHANGED] %s', listing_id)

            except Exception as e:
                stats['errors'] += 1
                logger.error('Error processing listing %s: %s', listing_id, e)

    except Exception as e:
        logger.error('Fatal scraper error: %s', e)
        stats['errors'] += 1

    # Mark unseen listings inactive
    if active_ids:
        try:
            mark_inactive_listings('gumtree', active_ids)
            logger.info('Marked unseen listings as inactive (kept %d active)', len(active_ids))
        except Exception as e:
            logger.warning('Could not mark inactive listings: %s', e)

    # Update scrape run record
    completed_at = datetime.now(timezone.utc).isoformat()
    if run_id is not None:
        try:
            update_scrape_run(run_id, {
                'completed_at': completed_at,
                'listings_found': stats['listings_found'],
                'listings_new': stats['listings_new'],
                'listings_updated': stats['listings_updated'],
                'errors': stats['errors'],
                'status': 'completed',
            })
        except Exception as e:
            logger.warning('Could not update scrape run: %s', e)

    # Summary
    logger.info('')
    logger.info('=== Scrape complete ===')
    logger.info('  Found     : %d', stats['listings_found'])
    logger.info('  New       : %d', stats['listings_new'])
    logger.info('  Updated   : %d', stats['listings_updated'])
    logger.info('  Unchanged : %d', stats['listings_unchanged'])
    logger.info('  Errors    : %d', stats['errors'])
    logger.info('  Completed : %s', completed_at)


if __name__ == '__main__':
    asyncio.run(main())
