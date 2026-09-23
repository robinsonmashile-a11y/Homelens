"""
Homelens — Century 21 scraper runner.

Usage:
    python scripts/run_century21.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('homelens.run_century21')


def _check_env() -> None:
    missing = [k for k in ('SUPABASE_URL', 'SUPABASE_KEY') if not os.environ.get(k)]
    if missing:
        logger.error('Missing env vars: %s', ', '.join(missing))
        sys.exit(1)


async def main() -> None:
    _check_env()

    from scraper.database import (
        log_scrape_run, mark_inactive_listings,
        update_scrape_run, upsert_listing_by_url,
    )
    from scraper.century21 import run_century21_scraper

    source  = 'century21'
    now     = datetime.now(timezone.utc).isoformat()
    run_id  = None
    try:
        run_id = log_scrape_run({'source': source, 'status': 'running', 'started_at': now})
    except Exception as e:
        logger.warning('Could not log scrape run: %s', e)

    stats = {'total': 0, 'new': 0, 'updated': 0, 'unchanged': 0, 'errors': 0, 'with_image': 0, 'with_size': 0}
    active_ids: list[str] = []
    t0 = time.time()

    print()
    print('═' * 65)
    print('  CENTURY 21 — SCRAPE')
    print(f'  Started: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}')
    print('═' * 65)
    print()

    try:
        async for listing in run_century21_scraper():
            lid    = listing.get('listing_id', '')
            price  = listing.get('price') or 0
            beds   = listing.get('bedrooms') or '?'
            suburb = (listing.get('suburb_normalised') or '').replace('-', ' ').title()
            size   = listing.get('size_m2')
            has_img  = bool(listing.get('image_url'))
            has_size = size is not None

            try:
                result = upsert_listing_by_url(listing)
                active_ids.append(lid)
                stats['total'] += 1
                if has_img:
                    stats['with_image'] += 1
                if has_size:
                    stats['with_size'] += 1

                flag = '+' if result == 'new' else ('~' if result == 'updated' else '=')
                if result == 'new':
                    stats['new'] += 1
                elif result == 'updated':
                    stats['updated'] += 1
                else:
                    stats['unchanged'] += 1

                size_str = f'{size:.0f}m²' if size else '—'
                img_tag  = '✓' if has_img else '✗'
                print(f'  [{source}] [{flag}] {suburb} {beds}bed  R{price:,} — {size_str} — image {img_tag}')

            except Exception as e:
                logger.error('DB write failed for %s: %s', lid, e)
                stats['errors'] += 1

    except Exception as e:
        logger.error('Scraper failed: %s', e, exc_info=True)
        stats['errors'] += 1
        if run_id:
            try:
                update_scrape_run(run_id, {'status': 'failed'})
            except Exception:
                pass
        return

    if active_ids:
        try:
            mark_inactive_listings(source, active_ids)
        except Exception as e:
            logger.warning('Could not mark inactive: %s', e)

    elapsed  = round(time.time() - t0, 1)
    tot      = stats['total']
    img_pct  = round(100 * stats['with_image'] / tot) if tot else 0
    size_pct = round(100 * stats['with_size']  / tot) if tot else 0

    if run_id:
        try:
            update_scrape_run(run_id, {
                'status':           'completed',
                'completed_at':     datetime.now(timezone.utc).isoformat(),
                'listings_found':   tot,
                'listings_new':     stats['new'],
                'listings_updated': stats['updated'],
                'errors':           stats['errors'],
            })
        except Exception as e:
            logger.warning('Could not update scrape run: %s', e)

    print()
    print('═' * 65)
    print('  CENTURY 21 — COMPLETE')
    print('═' * 65)
    print(f'  Total      : {tot:,}')
    print(f'  New        : {stats["new"]:,}')
    print(f'  Updated    : {stats["updated"]:,}')
    print(f'  Unchanged  : {stats["unchanged"]:,}')
    print(f'  Errors     : {stats["errors"]:,}')
    print(f'  With image : {stats["with_image"]:,}  ({img_pct}%)')
    print(f'  With size  : {stats["with_size"]:,}  ({size_pct}%)')
    print(f'  Time       : {elapsed}s')
    print('═' * 65)
    print()


if __name__ == '__main__':
    asyncio.run(main())
