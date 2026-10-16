"""
Homelens — Pipeline scheduler.

Runs the full scrape + analysis pipeline twice daily at 07:00 and 17:00 SAST.
Writes a PID file so stop_homelens.sh can kill the process cleanly.

Pipeline steps (in order):
  1. run_all_scrapers.py         (remax, pamgolding, seeff, rawson, sothebys, tyson, quay1)
  2. run_priority_scrapers.py    (greeff, jawitz, century21)
  3. run_priority_scrapers_2.py  (chaseveritt, dgproperties, engelvolkers)
  4. run_batch5_scrapers.py      (harcourts, justproperty, duarte, exprealty, lionlead, abc_international)
  5. fix_suburbs.py
  6. run_engine.py
  7. compute_investment.py
  8. generate_explanations.py
  9. backup_db.py

Usage:
    python scripts/scheduler.py             # run on schedule (blocking)
    python scripts/scheduler.py --now       # run once immediately, then exit
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

PID_FILE = _ROOT / 'scheduler.pid'
LOG_FILE = _ROOT / 'logs' / 'scheduler.log'

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger('homelens.scheduler')

# ---------------------------------------------------------------------------
# Pipeline definition
# ---------------------------------------------------------------------------

SCRIPTS_DIR = _ROOT / 'scripts'

PIPELINE_STEPS = [
    ('Scrapers (batch 1)',  SCRIPTS_DIR / 'run_all_scrapers.py'),
    ('Scrapers (batch 2)',  SCRIPTS_DIR / 'run_priority_scrapers.py'),
    ('Scrapers (batch 3)',  SCRIPTS_DIR / 'run_priority_scrapers_2.py'),
    ('Scrapers (batch 4)',  SCRIPTS_DIR / 'run_batch5_scrapers.py'),
    ('Fix suburbs',        SCRIPTS_DIR / 'fix_suburbs.py'),
    ('Engine',             SCRIPTS_DIR / 'run_engine.py'),
    ('Investment scores',  SCRIPTS_DIR / 'compute_investment.py'),
    ('Explanations',       SCRIPTS_DIR / 'generate_explanations.py'),
    ('Backup',             SCRIPTS_DIR / 'backup_db.py'),
]


# ---------------------------------------------------------------------------
# Step runner
# ---------------------------------------------------------------------------

def _run_step(name: str, script: Path) -> dict:
    """
    Run a pipeline step as a subprocess.
    Returns a result dict compatible with run_history.save_run_results().
    """
    import time as _time
    t0 = _time.time()
    logger.info('── Starting step: %s (%s) ──', name, script.name)

    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            cwd=str(_ROOT),
        )
        elapsed  = round(_time.time() - t0, 1)
        success  = proc.returncode == 0
        status   = 'success' if success else 'failed'

        if not success:
            logger.error(
                'Step %s exited %d\nSTDOUT:\n%s\nSTDERR:\n%s',
                name, proc.returncode,
                proc.stdout[-3000:] if proc.stdout else '',
                proc.stderr[-3000:] if proc.stderr else '',
            )
        else:
            logger.info('Step %s completed in %.1fs', name, elapsed)

        return {
            'step':     name,
            'script':   script.name,
            'status':   status,
            'duration': elapsed,
            'returncode': proc.returncode,
            'stderr':   (proc.stderr or '')[-500:],
        }

    except Exception as e:
        elapsed = round(_time.time() - t0, 1)
        logger.error('Step %s raised exception: %s', name, e, exc_info=True)
        return {
            'step':     name,
            'script':   script.name,
            'status':   'failed',
            'duration': elapsed,
            'returncode': -1,
            'stderr':   str(e),
        }


# ---------------------------------------------------------------------------
# Full pipeline run
# ---------------------------------------------------------------------------

def run_pipeline(trigger: str = 'scheduled') -> None:
    """Execute all pipeline steps in sequence, send alerts on failure."""
    from scripts.run_history import save_run_results
    from scripts.alerts import send_alert

    logger.info('════ Pipeline started (trigger=%s) ════', trigger)
    results: list[dict] = []

    for name, script in PIPELINE_STEPS:
        if not script.exists():
            logger.warning('Script not found — skipping: %s', script)
            results.append({
                'step': name, 'script': script.name,
                'status': 'skipped', 'duration': 0,
                'returncode': None, 'stderr': 'script not found',
            })
            continue

        result = _run_step(name, script)
        results.append(result)

        if result['status'] == 'failed':
            send_alert(name, result.get('stderr', 'Unknown error'))

    save_run_results(results, trigger=trigger)

    ok     = sum(1 for r in results if r['status'] == 'success')
    failed = sum(1 for r in results if r['status'] == 'failed')
    total_s = sum(r.get('duration', 0) for r in results)
    logger.info(
        '════ Pipeline done — %d ok / %d failed — %.0fs total ════',
        ok, failed, total_s,
    )


# ---------------------------------------------------------------------------
# Scheduler loop
# ---------------------------------------------------------------------------

def _write_pid() -> None:
    PID_FILE.write_text(str(os.getpid()))
    logger.info('PID %d written to %s', os.getpid(), PID_FILE)


def _remove_pid() -> None:
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _handle_signal(signum, frame):
    logger.info('Received signal %d — shutting down', signum)
    _remove_pid()
    sys.exit(0)


def run_scheduler() -> None:
    """Block forever, firing the pipeline at 07:00 and 17:00 SAST."""
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.error('apscheduler not installed — run: pip install apscheduler')
        sys.exit(1)

    _write_pid()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT,  _handle_signal)

    scheduler = BlockingScheduler(timezone='Africa/Johannesburg')

    scheduler.add_job(
        run_pipeline,
        CronTrigger(hour=7, minute=0, timezone='Africa/Johannesburg'),
        id='morning',
        kwargs={'trigger': 'scheduled'},
        misfire_grace_time=300,
    )
    scheduler.add_job(
        run_pipeline,
        CronTrigger(hour=17, minute=0, timezone='Africa/Johannesburg'),
        id='evening',
        kwargs={'trigger': 'scheduled'},
        misfire_grace_time=300,
    )

    logger.info('Scheduler started — jobs: 07:00 SAST and 17:00 SAST')

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        _remove_pid()
        logger.info('Scheduler stopped')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Homelens pipeline scheduler')
    parser.add_argument(
        '--now', action='store_true',
        help='Run the pipeline once immediately and exit',
    )
    args = parser.parse_args()

    if args.now:
        run_pipeline(trigger='manual')
    else:
        run_scheduler()
