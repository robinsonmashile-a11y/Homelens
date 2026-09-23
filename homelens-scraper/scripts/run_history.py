"""
Homelens — Run history storage.

Saves every pipeline run to dashboard/run_history.json so the
local dashboard can display it without a server.

Also fetches live stats from Supabase for the dashboard.

Usage:
  from scripts.run_history import save_run_results, get_live_stats
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger('homelens.run_history')

_ROOT         = Path(__file__).resolve().parent.parent
HISTORY_FILE  = _ROOT / 'dashboard' / 'run_history.json'
MAX_HISTORY   = 60   # 2 runs/day × 30 days


def save_run_results(results: list[dict], trigger: str = 'scheduled') -> None:
    """
    Append a completed pipeline run to run_history.json.

    Each run record:
      run_id        — YYYYMMDD_HHMMSS
      started_at    — ISO timestamp
      trigger       — 'scheduled' | 'manual'
      overall_status — 'success' | 'failed'
      total_duration — seconds (sum of all steps)
      steps         — list of step dicts
    """
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)

    history: list[dict] = []
    if HISTORY_FILE.exists():
        try:
            history = json.loads(HISTORY_FILE.read_text())
        except Exception:
            history = []

    total_duration = sum(r.get('duration', 0) for r in results)
    overall = 'success' if all(r.get('status') == 'success' for r in results) else 'failed'

    run: dict[str, Any] = {
        'run_id':         datetime.now().strftime('%Y%m%d_%H%M%S'),
        'started_at':     datetime.now(timezone.utc).isoformat(),
        'trigger':        trigger,
        'overall_status': overall,
        'total_duration': total_duration,
        'steps':          results,
    }

    history.insert(0, run)
    history = history[:MAX_HISTORY]

    HISTORY_FILE.write_text(json.dumps(history, indent=2))
    logger.info('Run history saved → %s (%d runs stored)', HISTORY_FILE, len(history))


def get_live_stats() -> dict:
    """
    Pull live counts from Supabase for the dashboard.
    Returns a dict that gets written to dashboard/live_stats.json.
    """
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=_ROOT / '.env', override=True)
    from supabase import create_client

    url = os.getenv('SUPABASE_URL', '')
    key = os.getenv('SUPABASE_KEY', '')
    if not url or not key:
        return {}

    try:
        client = create_client(url, key)

        # Total active listings
        total_resp = (
            client.table('listings')
            .select('listing_id', count='exact')
            .eq('is_active', True)
            .execute()
        )
        total_listings = total_resp.count or 0

        # Listings with investment scores
        scored_resp = (
            client.table('listing_investment')
            .select('listing_id', count='exact')
            .execute()
        )
        scored = scored_resp.count or 0

        # Listings by source
        src_resp = (
            client.table('listings')
            .select('source')
            .eq('is_active', True)
            .execute()
        )
        source_counts: dict[str, int] = {}
        for row in (src_resp.data or []):
            s = row.get('source') or 'unknown'
            source_counts[s] = source_counts.get(s, 0) + 1

        # Recent scrape runs
        runs_resp = (
            client.table('scrape_runs')
            .select('source,status,started_at,listings_found,listings_new,errors')
            .order('started_at', desc=True)
            .limit(30)
            .execute()
        )
        recent_runs = runs_resp.data or []

        stats = {
            'generated_at':   datetime.now(timezone.utc).isoformat(),
            'total_listings': total_listings,
            'scored':         scored,
            'source_counts':  source_counts,
            'recent_runs':    recent_runs,
        }

        stats_file = _ROOT / 'dashboard' / 'live_stats.json'
        stats_file.write_text(json.dumps(stats, indent=2))
        logger.info('Live stats written → %s', stats_file)
        return stats

    except Exception as e:
        logger.error('Failed to fetch live stats: %s', e)
        return {}
