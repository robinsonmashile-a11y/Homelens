"""
Maddy fallback scoring — runs right after the engine (scripts/run_engine.py).

Scores the ACTIVE listings the engine could not (no row in listing_signals) using Maddy
(data/maddy_prompt.txt), writing scored_by='maddy', comparison_method='maddy_fallback' rows.
The engine stays primary: it replaces a Maddy row whenever it can score the listing itself.
Unchanged listings are skipped (input_hash), so each nightly run only pays for new/changed ones.

Usage:
    python scripts/run_maddy_fallback.py --sample 30 --dry-run   # look first, writes nothing
    python scripts/run_maddy_fallback.py --limit 200             # score at most 200, write them
    python scripts/run_maddy_fallback.py                         # everything outstanding
    python scripts/run_maddy_fallback.py --city cape-town

Needs ANTHROPIC_API_KEY, SUPABASE_URL, SUPABASE_KEY and (to write) the columns added by
supabase/migrations/maddy_fallback.sql. Without that migration it is always a dry run.
A failure here never fails the pipeline: it logs and exits 0 (the engine's scores are already saved).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(override=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger('homelens.maddy')

REPORT_DIR = Path(__file__).resolve().parent.parent / 'reports'


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--sample', type=int, help='score a small mixed sample (for a first look)')
    ap.add_argument('--limit', type=int, help='score at most N listings this run')
    ap.add_argument('--city', choices=('cape-town', 'johannesburg'))
    ap.add_argument('--dry-run', action='store_true', help='call Maddy but write nothing')
    args = ap.parse_args()

    try:
        import anthropic
        from scraper.database import get_client
        from scraper.maddy import format_report, run_fallback
        rep = run_fallback(get_client(), anthropic.Anthropic(), limit=args.limit, sample=args.sample,
                           city=args.city, dry_run=args.dry_run)
    except Exception as e:                                  # never break the pipeline
        logger.error('Maddy fallback failed: %s', e, exc_info=True)
        return 0

    print('\n' + format_report(rep) + '\n')
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"maddy_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}{'_dry' if rep['dry_run'] else ''}.json"
    path.write_text(json.dumps(rep, indent=1, default=str), encoding='utf-8')
    print(f'saved {path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
