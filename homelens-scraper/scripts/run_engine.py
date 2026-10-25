"""
Homelens Insight Engine v4 — entry point.

Run order:
  1. Populate property_type on listings (keyword classification from title)
  2. Compute suburb baselines (two-track + sufficiency gate)
  3. Load suburb attractiveness scores (if table exists)
  4. Compute listing signals (sufficient baselines only)
  5. Print sufficiency report + signal summary

Usage:
    python scripts/run_engine.py

Prerequisites (run ONCE in Supabase SQL editor):
  - listings.property_type column
  - suburb_baselines table (v4 schema with has_sufficient_data + sufficient_track)
  - listing_signals table  (v4 schema with z_score + property_type)
  - suburb_coverage view
  Migration SQL is printed by engine.py on schema failure.
"""
from __future__ import annotations

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
logger = logging.getLogger('homelens.engine')


def _check_env() -> None:
    required = ['SUPABASE_URL', 'SUPABASE_KEY']
    missing  = [k for k in required if not os.environ.get(k)]
    if missing:
        logger.error('Missing environment variables: %s', ', '.join(missing))
        sys.exit(1)


if __name__ == '__main__':
    _check_env()

    from scraper.engine import run_engine

    print()
    print('━' * 60)
    print('  Homelens Insight Engine v4')
    print('━' * 60)

    t_start = time.time()

    try:
        pt_counts, baselines, signal_stats = run_engine()
    except SystemExit:
        raise
    except Exception as e:
        logger.error('Engine failed: %s', e, exc_info=True)
        sys.exit(1)

    elapsed = round(time.time() - t_start, 1)

    sufficient   = sum(1 for b in baselines.values() if b.get('has_sufficient_data'))
    insufficient = len(baselines) - sufficient
    suff_pct     = round(100 * sufficient / len(baselines)) if baselines else 0

    print()
    print('━' * 60)
    print(f"  Property types classified : {sum(pt_counts.values()):>6,} listings")
    print(f"  Baseline groups total     : {len(baselines):>6,}")
    print(f"    Sufficient              : {sufficient:>6,}  ({suff_pct}%)")
    print(f"    Insufficient            : {insufficient:>6,}  ({100 - suff_pct}%)")
    print(f"  Signals computed          : {signal_stats['total']:>6,}")
    print(f"    Track 1 (price/m²)     : {signal_stats['track1']:>6,}")
    print(f"    Track 2 (price only)   : {signal_stats['track2']:>6,}")
    print(f"    Skipped (no baseline)  : {signal_stats['no_baseline']:>6,}")
    print(f"    Skipped (insufficient) : {signal_stats['insufficient']:>6,}")
    print(f"    Skipped (track gap)    : {signal_stats['no_track']:>6,}")
    print(f"  Time elapsed              : {elapsed}s")
    print('━' * 60)
    print()

    # ── Fresh deals: new listings the engine just scored as a deal (read-only) ──
    try:
        from scraper.reports import fresh_deals_report
        fresh_deals_report()
    except Exception as e:
        logger.warning('Could not build fresh-deals report: %s', e)
