"""
Homelens — Full rescrape orchestrator (19 agencies).

Runs all scrapers sequentially to avoid IP blocks. For each scraper:
  - Scrapes all pages (sale + rent)
  - Upserts to listings table with price_status tracking
  - Marks inactive listings not seen in this run
  - Logs to scrape_runs table
  - Writes price-status breakdown to scrape_audit_log
  - Prints per-listing progress and per-scraper completion block

Usage:
    cd homelens-scraper
    python scripts/run_all_scrapers.py

Prerequisites:
    SUPABASE_URL and SUPABASE_KEY must be set (or in .env)
    SQL migration price_status_and_audit_log.sql must have been applied

Run the engine pipeline afterwards:
    python scripts/run_engine.py
    python scripts/compute_investment.py
    python scripts/generate_explanations.py

Agencies (19):
    pamgolding, sothebys, tyson, rawson, seeff, remax, quay1,
    chaseveritt, greeff, jawitz, engelvolkers, century21, harcourts,
    lionlead, abc_international, justproperty, dgproperties, duarte,
    exprealty

Legend (per-listing output):
    [+] new   [~] updated   [=] unchanged
    [S] sale  [R] rent
    [CPT] Cape Town  [JNB] Johannesburg
    price marker: R=captured  P=POA  !=extraction failed  ?=unknown
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import uuid
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
logger = logging.getLogger('homelens.run_all')

SCRAPER_VERSION = '2025-07-07'

_PRICE_MARKER = {
    'ok':                'R',
    'poa':               'P',
    'extraction_failed': '!',
    'unknown':           '?',
}


# ---------------------------------------------------------------------------
# Env check
# ---------------------------------------------------------------------------

def _check_env() -> None:
    required = ['SUPABASE_URL', 'SUPABASE_KEY']
    missing  = [k for k in required if not os.environ.get(k)]
    if missing:
        logger.error('Missing environment variables: %s', ', '.join(missing))
        sys.exit(1)


def _check_schema() -> None:
    """Abort before scraping if listing_status / levy columns are missing —
    every insert would otherwise fail on the unknown columns."""
    from scraper.database import get_client
    try:
        get_client().table('listings').select(
            'listing_status,status_changed_at,levy_monthly,rates_monthly,'
            'levies_rates_combined_monthly,costs_source,status_source,'
            'costs_raw_snippet,costs_checked_at,city'
        ).limit(1).execute()
    except Exception as e:
        logger.error('listings table is missing the status/costs/city columns (%s). '
                     'Run supabase/migrations/listing_status_and_costs.sql and add_city_column.sql in the '
                     'Supabase SQL editor, then re-run.', e)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Per-scraper runner
# ---------------------------------------------------------------------------

def _empty_type_stats() -> dict:
    return {
        'total': 0, 'new': 0, 'updated': 0, 'unchanged': 0,
        'price_ok': 0, 'price_poa': 0, 'price_fail': 0, 'price_unk': 0,
    }


async def run_scraper(
    source: str,
    scraper_fn: Callable[[], AsyncGenerator[dict, None]],
    run_id: str,
    run_started_at: str,
    scraper_num: int,
    total_scrapers: int,
) -> dict:
    """
    Run one scraper generator, upsert all listings, write audit log.
    Returns a stats dict used in the final summary table.
    """
    from scraper.database import (
        log_scrape_run,
        mark_inactive_listings,
        update_scrape_run,
        upsert_listing_by_url,
        write_audit_log,
    )

    now = datetime.now(timezone.utc).isoformat()

    db_run_id = None
    try:
        db_run_id = log_scrape_run({
            'source':     source,
            'status':     'running',
            'started_at': now,
        })
    except Exception as e:
        logger.warning('[%s] Could not create scrape_runs row: %s', source, e)

    stats = {
        'source':     source,
        'sale':       _empty_type_stats(),
        'rent':       _empty_type_stats(),
        'errors':     0,
        'with_image': 0,
        'total':      0,
        'elapsed':    0.0,
        'failed':     False,
    }

    # Separate collections for audit log per listing_type
    listings_by_type: dict[str, list[dict]] = {'sale': [], 'rent': []}
    active_ids: list[str] = []

    t0 = time.time()

    try:
        async for listing in scraper_fn():
            lid       = listing.get('listing_id', '')
            price     = listing.get('price') or 0
            beds      = listing.get('bedrooms') or '?'
            suburb    = (listing.get('suburb_normalised') or '').replace('-', ' ').title()
            city_tag  = {'cape-town': 'CPT', 'johannesburg': 'JNB'}.get(listing.get('city') or 'cape-town', '???')
            ltype     = listing.get('listing_type') or 'sale'
            ltype     = ltype if ltype in ('sale', 'rent') else 'sale'
            pstatus   = listing.get('price_status') or 'unknown'
            has_image = bool(listing.get('image_url'))

            pmarker  = _PRICE_MARKER.get(pstatus, '?')
            img_tag  = '✓' if has_image else '·'
            type_key = ltype[0].upper()  # S or R

            try:
                result = upsert_listing_by_url(listing)
                active_ids.append(lid)

                stats['total']      += 1
                type_stats           = stats[ltype]
                type_stats['total'] += 1

                if pstatus == 'ok':
                    type_stats['price_ok']   += 1
                elif pstatus == 'poa':
                    type_stats['price_poa']  += 1
                elif pstatus == 'extraction_failed':
                    type_stats['price_fail'] += 1
                else:
                    type_stats['price_unk']  += 1

                if has_image:
                    stats['with_image'] += 1

                if result == 'new':
                    type_stats['new'] += 1
                    flag = '+'
                elif result == 'updated':
                    type_stats['updated'] += 1
                    flag = '~'
                else:
                    type_stats['unchanged'] += 1
                    flag = '='

                listings_by_type[ltype].append(listing)

                title     = (listing.get('title') or 'Listing')[:35]
                price_str = f'R{price:,}' if price else pstatus
                print(
                    f'  [{source}] [{flag}][{type_key}][{pmarker}][{city_tag}]'
                    f'  {suburb:<18} {str(beds):<4}bed  {price_str:<14}'
                    f'  img:{img_tag}  {title}'
                )

            except Exception as e:
                logger.error('[%s] DB write failed for %s: %s', source, lid, e)
                stats['errors'] += 1

    except Exception as e:
        logger.error('[%s] Scraper failed: %s', source, e, exc_info=True)
        stats['errors'] += 1
        stats['failed'] = True
        if db_run_id is not None:
            try:
                update_scrape_run(db_run_id, {'status': 'failed'})
            except Exception:
                pass
        return stats

    # Mark listings not seen in this run as inactive
    if active_ids:
        try:
            mark_inactive_listings(source, active_ids)
        except Exception as e:
            logger.warning('[%s] Could not mark inactive listings: %s', source, e)

    elapsed = round(time.time() - t0, 1)
    stats['elapsed'] = elapsed

    # Write audit log rows — one per listing_type with listings
    for ltype, ltype_listings in listings_by_type.items():
        if ltype_listings:
            try:
                write_audit_log(
                    run_id=run_id,
                    source=source,
                    listing_type=ltype,
                    listings=ltype_listings,
                    scraper_code_version=SCRAPER_VERSION,
                    run_started_at=run_started_at,
                )
            except Exception as e:
                logger.warning('[%s] write_audit_log failed for %s: %s', source, ltype, e)

    # Finalise scrape_runs row
    new_total = stats['sale']['new'] + stats['rent']['new']
    upd_total = stats['sale']['updated'] + stats['rent']['updated']
    if db_run_id is not None:
        try:
            update_scrape_run(db_run_id, {
                'status':           'completed',
                'completed_at':     datetime.now(timezone.utc).isoformat(),
                'listings_found':   stats['total'],
                'listings_new':     new_total,
                'listings_updated': upd_total,
                'errors':           stats['errors'],
            })
        except Exception as e:
            logger.warning('[%s] Could not finalise scrape_runs row: %s', source, e)

    # Per-scraper completion block
    tot   = stats['total']
    img   = stats['with_image']
    ipct  = round(100 * img / tot) if tot else 0
    st    = stats['sale']
    rt    = stats['rent']

    def _ok_pct(type_stats: dict) -> str:
        t = type_stats['total']
        return f"{round(100 * type_stats['price_ok'] / t)}%" if t else 'n/a'

    print()
    print(f'  ── {source.upper()}  ({scraper_num}/{total_scrapers}) ──────────────────────────────────')
    print(f'     Total : {tot:>5,}  |  +{new_total} new  ~{upd_total} updated  ✗{stats["errors"]} errors  |  {elapsed}s')
    print(f'     SALE  : {st["total"]:>5}  price ok:{st["price_ok"]:>4}  poa:{st["price_poa"]:>3}  fail:{st["price_fail"]:>3}  ({_ok_pct(st)} capture)')
    print(f'     RENT  : {rt["total"]:>5}  price ok:{rt["price_ok"]:>4}  poa:{rt["price_poa"]:>3}  fail:{rt["price_fail"]:>3}  ({_ok_pct(rt)} capture)')
    print(f'     Images: {img}/{tot} ({ipct}%)')
    print()

    return stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _parse_only(argv: list[str]) -> list[str]:
    """--only rawson,seeff  (or --only=rawson). Empty list = run all."""
    for i, arg in enumerate(argv):
        if arg == '--only' and i + 1 < len(argv):
            return [x.strip() for x in argv[i + 1].split(',') if x.strip()]
        if arg.startswith('--only='):
            return [x.strip() for x in arg.split('=', 1)[1].split(',') if x.strip()]
    return []


async def main() -> None:
    _check_env()
    _check_schema()

    from scraper.pamgolding        import run_pamgolding_scraper
    from scraper.sothebys          import run_sothebys_scraper
    from scraper.tyson             import run_tyson_scraper
    from scraper.rawson            import run_rawson_scraper
    from scraper.seeff             import run_seeff_scraper
    from scraper.remax             import run_remax_scraper
    from scraper.quay1             import run_quay1_scraper
    from scraper.chaseveritt       import run_chaseveritt_scraper
    from scraper.greeff            import run_greeff_scraper
    from scraper.jawitz            import run_jawitz_scraper
    from scraper.engelvolkers      import run_engelvolkers_scraper
    from scraper.century21         import run_century21_scraper
    from scraper.harcourts         import run_harcourts_scraper
    from scraper.lionlead          import run_lionlead_scraper
    from scraper.abc_international import run_abc_international_scraper
    from scraper.justproperty      import run_justproperty_scraper
    from scraper.dgproperties      import run_dgproperties_scraper
    from scraper.duarte            import run_duarte_scraper
    from scraper.exprealty         import run_exprealty_scraper

    scrapers: list[tuple[str, Callable]] = [
        ('pamgolding',        run_pamgolding_scraper),
        ('sothebys',          run_sothebys_scraper),
        ('tyson',             run_tyson_scraper),
        ('rawson',            run_rawson_scraper),
        ('seeff',             run_seeff_scraper),
        ('remax',             run_remax_scraper),
        ('quay1',             run_quay1_scraper),
        ('chaseveritt',       run_chaseveritt_scraper),
        ('greeff',            run_greeff_scraper),
        ('jawitz',            run_jawitz_scraper),
        ('engelvolkers',      run_engelvolkers_scraper),
        ('century21',         run_century21_scraper),
        ('harcourts',         run_harcourts_scraper),
        ('lionlead',          run_lionlead_scraper),
        ('abc_international', run_abc_international_scraper),
        ('justproperty',      run_justproperty_scraper),
        ('dgproperties',      run_dgproperties_scraper),
        ('duarte',            run_duarte_scraper),
        ('exprealty',         run_exprealty_scraper),
    ]

    only = _parse_only(sys.argv[1:])
    if only:
        known = {name for name, _ in scrapers}
        unknown = [o for o in only if o not in known]
        if unknown:
            logger.error('Unknown scraper(s): %s. Known: %s', ', '.join(unknown), ', '.join(sorted(known)))
            sys.exit(2)
        scrapers = [(name, fn) for name, fn in scrapers if name in only]

    run_id         = str(uuid.uuid4())
    run_started_at = datetime.now(timezone.utc).isoformat()
    n              = len(scrapers)

    print()
    print('═' * 72)
    print(f'  HOMELENS — RESCRAPE  ({n} agenc{"y" if n == 1 else "ies"})')
    print(f'  Started : {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}')
    print(f'  Run ID  : {run_id}')
    print('═' * 72)

    t_global  = time.time()
    all_stats: list[dict] = []

    for i, (source, scraper_fn) in enumerate(scrapers, start=1):
        print()
        print('━' * 72)
        print(f'  [{i}/{n}]  {source.upper()}')
        print('━' * 72)

        stats = await run_scraper(
            source, scraper_fn, run_id, run_started_at, i, n,
        )
        all_stats.append(stats)

    # ── Final audit summary ────────────────────────────────────────────────
    elapsed_total = round(time.time() - t_global, 1)

    grand_total     = sum(s['total']      for s in all_stats)
    grand_new       = sum(s['sale']['new'] + s['rent']['new'] for s in all_stats)
    grand_updated   = sum(s['sale']['updated'] + s['rent']['updated'] for s in all_stats)
    grand_errors    = sum(s['errors']     for s in all_stats)
    grand_images    = sum(s['with_image'] for s in all_stats)

    grand_sale_tot  = sum(s['sale']['total']      for s in all_stats)
    grand_sale_ok   = sum(s['sale']['price_ok']   for s in all_stats)
    grand_sale_poa  = sum(s['sale']['price_poa']  for s in all_stats)
    grand_sale_fail = sum(s['sale']['price_fail'] for s in all_stats)

    grand_rent_tot  = sum(s['rent']['total']      for s in all_stats)
    grand_rent_ok   = sum(s['rent']['price_ok']   for s in all_stats)
    grand_rent_poa  = sum(s['rent']['price_poa']  for s in all_stats)
    grand_rent_fail = sum(s['rent']['price_fail'] for s in all_stats)

    img_pct  = round(100 * grand_images / grand_total)   if grand_total     else 0
    s_ok_pct = round(100 * grand_sale_ok / grand_sale_tot) if grand_sale_tot else 0
    r_ok_pct = round(100 * grand_rent_ok / grand_rent_tot) if grand_rent_tot else 0

    failed_sources = [s['source'] for s in all_stats if s.get('failed')]

    # Column widths: source(18) | sale listings+breakdown | rent listings+breakdown | img% | status
    HDR = f"  {'Source':<18}  {'Sale':>5} {'ok':>4} {'poa':>4} {'!':>4}  |  {'Rent':>5} {'ok':>4} {'poa':>4} {'!':>4}  {'img%':>5}  Status"
    SEP = '  ' + '─' * 76

    print()
    print('═' * 72)
    print('  AUDIT SUMMARY — price capture by agency')
    print('═' * 72)
    print()
    print(HDR)
    print(SEP)

    for s in all_stats:
        st     = s['sale']
        rt     = s['rent']
        tot    = s['total']
        img    = s['with_image']
        ipct   = round(100 * img / tot) if tot else 0
        status = 'FAILED' if s.get('failed') else 'ok'
        print(
            f"  {s['source']:<18}"
            f"  {st['total']:>5} {st['price_ok']:>4} {st['price_poa']:>4} {st['price_fail']:>4}"
            f"  |  {rt['total']:>5} {rt['price_ok']:>4} {rt['price_poa']:>4} {rt['price_fail']:>4}"
            f"  {ipct:>4}%  {status}"
        )

    print(SEP)
    print(
        f"  {'TOTAL':<18}"
        f"  {grand_sale_tot:>5} {grand_sale_ok:>4} {grand_sale_poa:>4} {grand_sale_fail:>4}"
        f"  |  {grand_rent_tot:>5} {grand_rent_ok:>4} {grand_rent_poa:>4} {grand_rent_fail:>4}"
        f"  {img_pct:>4}%"
    )
    print()
    print(f'  Price capture rate  —  SALE: {s_ok_pct}%   RENT: {r_ok_pct}%')
    print()
    print(f'  Total listings   : {grand_total:>8,}')
    print(f'  New this run     : {grand_new:>8,}')
    print(f'  Updated          : {grand_updated:>8,}')
    print(f'  DB errors        : {grand_errors:>8,}')
    print(f'  With image       : {grand_images:>8,}  ({img_pct}%)')
    print(f'  Time elapsed     : {elapsed_total}s')
    print()

    if failed_sources:
        print(f'  ⚠  FAILED scrapers : {", ".join(failed_sources)}')
        print()

    # ── Listing status + levies/rates coverage ────────────────────────────
    try:
        from run_report import print_run_report
        print_run_report(run_started_at)
    except Exception as e:
        logger.warning('Could not build status/costs report: %s', e)

    print(f'  Run ID : {run_id}')
    print('  (Audit rows written to scrape_audit_log)')
    print()
    print('  Next steps:')
    print('    python scripts/run_engine.py')
    print('    python scripts/compute_investment.py')
    print('    python scripts/generate_explanations.py')
    print()
    print('═' * 72)
    print()

    if failed_sources:
        sys.exit(1)


if __name__ == '__main__':
    asyncio.run(main())
