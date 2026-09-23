"""
Homelens — Priority scraper orchestrator (batch 2).

Runs three agencies sequentially:
  chaseveritt, dgproperties, engelvolkers

These fill Atlantic Seaboard and Southern Suburbs data gaps:
  Chas Everitt:   constantia, claremont, southern-suburbs, tableview
  DG Properties:  atlantic-seaboard, sea-point, de-waterkant, camps-bay
  Engel & Völkers: sea-point, gardens, vredehoek, heldervue, atlantic-seaboard

For each scraper:
  - Scrapes all pages (sale + rent)
  - Captures image_url
  - Upserts to listings table
  - Marks inactive listings not seen in this run
  - Logs to scrape_runs table

Usage:
    python scripts/run_priority_scrapers_2.py

Rate limits:
  chaseveritt:  4s page load wait   5-7s between pages (shared platform)
  dgproperties: 4s page load wait   5-7s between pages (shared platform)
  engelvolkers: 5s page load wait   single-page results (no pagination)

Run the engine pipeline afterwards:
  python scripts/run_engine.py
  python scripts/compute_investment.py
  python scripts/generate_explanations.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('homelens.run_priority_2')


# ---------------------------------------------------------------------------
# Env check
# ---------------------------------------------------------------------------

def _check_env() -> None:
    missing = [k for k in ('SUPABASE_URL', 'SUPABASE_KEY') if not os.environ.get(k)]
    if missing:
        logger.error('Missing environment variables: %s', ', '.join(missing))
        sys.exit(1)


# ---------------------------------------------------------------------------
# Per-scraper runner
# ---------------------------------------------------------------------------

async def run_scraper(
    source: str,
    scraper_fn: Callable[[], AsyncGenerator[dict, None]],
) -> dict:
    from scraper.database import (
        log_scrape_run, mark_inactive_listings,
        update_scrape_run, upsert_listing_by_url,
    )

    now    = datetime.now(timezone.utc).isoformat()
    run_id = None
    try:
        run_id = log_scrape_run({'source': source, 'status': 'running', 'started_at': now})
    except Exception as e:
        logger.warning('[%s] Could not log scrape run: %s', source, e)

    stats = {
        'source':     source,
        'total':      0,
        'new':        0,
        'updated':    0,
        'unchanged':  0,
        'errors':     0,
        'with_image': 0,
        'with_size':  0,
        'sale':       0,
        'rent':       0,
    }
    active_ids: list[str] = []
    t0 = time.time()

    try:
        async for listing in scraper_fn():
            lid    = listing.get('listing_id', '')
            price  = listing.get('price') or 0
            beds   = listing.get('bedrooms') or '?'
            suburb = (listing.get('suburb_normalised') or '').replace('-', ' ').title()
            size   = listing.get('size_m2')
            ltype  = listing.get('listing_type', 'sale')
            has_img  = bool(listing.get('image_url'))
            has_size = size is not None

            try:
                result = upsert_listing_by_url(listing)
                active_ids.append(lid)
                stats['total'] += 1
                stats[ltype]   += 1
                if has_img:
                    stats['with_image'] += 1
                if has_size:
                    stats['with_size'] += 1

                if result == 'new':
                    stats['new'] += 1
                    flag = '+'
                elif result == 'updated':
                    stats['updated'] += 1
                    flag = '~'
                else:
                    stats['unchanged'] += 1
                    flag = '='

                size_str = f'{size:.0f}m²' if size else '—'
                img_tag  = '✓' if has_img else '✗'
                print(
                    f'  [{source}] [{flag}] {suburb} {beds}bed'
                    f'  R{price:,} — {size_str} — image {img_tag}'
                )

            except Exception as e:
                logger.error('[%s] DB write failed for %s: %s', source, lid, e)
                stats['errors'] += 1

    except Exception as e:
        logger.error('[%s] Scraper failed: %s', source, e, exc_info=True)
        stats['errors'] += 1
        if run_id is not None:
            try:
                update_scrape_run(run_id, {'status': 'failed'})
            except Exception:
                pass
        return stats

    if active_ids:
        try:
            mark_inactive_listings(source, active_ids)
        except Exception as e:
            logger.warning('[%s] Could not mark inactive: %s', source, e)

    elapsed = round(time.time() - t0, 1)
    stats['elapsed'] = elapsed

    if run_id is not None:
        try:
            update_scrape_run(run_id, {
                'status':           'completed',
                'completed_at':     datetime.now(timezone.utc).isoformat(),
                'listings_found':   stats['total'],
                'listings_new':     stats['new'],
                'listings_updated': stats['updated'],
                'errors':           stats['errors'],
            })
        except Exception as e:
            logger.warning('[%s] Could not update scrape run: %s', source, e)

    tot      = stats['total']
    img_pct  = round(100 * stats['with_image'] / tot) if tot else 0
    size_pct = round(100 * stats['with_size']  / tot) if tot else 0

    print()
    print(f'  ── {source.upper()} complete ──')
    print(f'     {tot:,} listings  |  +{stats["new"]} new  ~{stats["updated"]} updated  ={stats["unchanged"]} unchanged  ✗{stats["errors"]} errors')
    print(f'     sale: {stats["sale"]}  rent: {stats["rent"]}')
    print(f'     {stats["with_image"]}/{tot} with image ({img_pct}%)  |  {stats["with_size"]}/{tot} with size ({size_pct}%)  |  {elapsed}s')
    print()

    return stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    _check_env()

    from scraper.chaseveritt  import run_chaseveritt_scraper
    from scraper.dgproperties  import run_dgproperties_scraper
    from scraper.engelvolkers  import run_engelvolkers_scraper

    scrapers: list[tuple[str, Callable]] = [
        ('chaseveritt',  run_chaseveritt_scraper),
        ('dgproperties', run_dgproperties_scraper),
        ('engelvolkers', run_engelvolkers_scraper),
    ]

    print()
    print('═' * 65)
    print('  HOMELENS — PRIORITY SCRAPERS (BATCH 2)')
    print(f'  Started: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}')
    print('  Sources: chaseveritt, dgproperties, engelvolkers')
    print('═' * 65)
    print()

    t_global  = time.time()
    all_stats: list[dict] = []

    for source, scraper_fn in scrapers:
        print()
        print('━' * 65)
        print(f'  Scraping: {source.upper()}')
        print('━' * 65)

        stats = await run_scraper(source, scraper_fn)
        all_stats.append(stats)

    # ── Final summary ────────────────────────────────────────────────────────
    elapsed_total = round(time.time() - t_global, 1)
    grand_total   = sum(s['total']      for s in all_stats)
    grand_new     = sum(s['new']        for s in all_stats)
    grand_updated = sum(s['updated']    for s in all_stats)
    grand_errors  = sum(s['errors']     for s in all_stats)
    grand_images  = sum(s['with_image'] for s in all_stats)
    grand_sizes   = sum(s['with_size']  for s in all_stats)

    print()
    print('═' * 65)
    print('  PRIORITY SCRAPERS BATCH 2 — COMPLETE')
    print('═' * 65)
    print()
    for s in all_stats:
        src     = s['source']
        tot     = s['total']
        img     = s['with_image']
        sz      = s['with_size']
        img_pct = round(100 * img / tot) if tot else 0
        sz_pct  = round(100 * sz  / tot) if tot else 0
        elapsed = s.get('elapsed', 0)
        print(f'  {src:<14}  {tot:>4,} listings')
        print(f'                 sale: {s["sale"]}  rent: {s["rent"]}')
        print(f'                 +{s["new"]} new  ~{s["updated"]} updated  ✗{s["errors"]} errors')
        print(f'                 {img}/{tot} images ({img_pct}%)  |  {sz}/{tot} with size ({sz_pct}%)  |  {elapsed}s')
        print()
    print(f'  {"─"*61}')
    print(f'  Total new    : {grand_new:,}')
    print(f'  Total updated: {grand_updated:,}')
    print(f'  Total errors : {grand_errors:,}')
    print(f'  Total time   : {elapsed_total}s')
    print('═' * 65)
    print()
    print('  Next steps:')
    print('    python scripts/run_engine.py')
    print('    python scripts/compute_investment.py')
    print('    python scripts/generate_explanations.py')
    print()


if __name__ == '__main__':
    asyncio.run(main())
