"""
Homelens — Database backup.

Exports critical Supabase tables to timestamped CSV files in backups/.
Read-only — never modifies Supabase data.

Usage:
    python scripts/backup_db.py
    python scripts/backup_db.py --tables listings listing_investment
"""
from __future__ import annotations

import csv
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger('homelens.backup_db')

_ROOT       = Path(__file__).resolve().parent.parent
BACKUP_DIR  = _ROOT / 'backups'
MAX_BACKUPS = 30   # keep 30 daily backups (~1 month)

TABLES = [
    'listings',
    'listing_signals',
    'listing_investment',
    'suburb_baselines',
    'waitlist',
    'agent_enquiries',
]


def _get_client():
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=_ROOT / '.env', override=True)
    from supabase import create_client

    url = os.getenv('SUPABASE_URL', '')
    key = os.getenv('SUPABASE_KEY', '')
    if not url or not key:
        raise RuntimeError('SUPABASE_URL / SUPABASE_KEY not set in .env')
    return create_client(url, key)


def _export_table(client, table: str, ts: str) -> dict:
    """
    Export all rows from `table` to backups/<ts>/<table>.csv.
    Returns {'table': table, 'rows': int, 'path': str, 'status': 'ok'|'error', 'error': str|None}.
    """
    out_dir = BACKUP_DIR / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f'{table}.csv'

    try:
        # Fetch all rows in batches (PostgREST default limit is 1000)
        all_rows: list[dict] = []
        offset = 0
        batch  = 1000
        while True:
            resp = (
                client.table(table)
                .select('*')
                .range(offset, offset + batch - 1)
                .execute()
            )
            rows = resp.data or []
            all_rows.extend(rows)
            if len(rows) < batch:
                break
            offset += batch

        if not all_rows:
            # Write an empty file so we know the backup ran
            csv_path.write_text('')
            return {'table': table, 'rows': 0, 'path': str(csv_path), 'status': 'ok', 'error': None}

        fieldnames = list(all_rows[0].keys())
        with csv_path.open('w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_rows)

        logger.info('Backed up %s → %s (%d rows)', table, csv_path, len(all_rows))
        return {'table': table, 'rows': len(all_rows), 'path': str(csv_path), 'status': 'ok', 'error': None}

    except Exception as e:
        logger.error('Failed to backup %s: %s', table, e)
        return {'table': table, 'rows': 0, 'path': str(csv_path), 'status': 'error', 'error': str(e)}


def _prune_old_backups() -> None:
    """Delete oldest backup folders beyond MAX_BACKUPS."""
    if not BACKUP_DIR.exists():
        return
    folders = sorted(
        [d for d in BACKUP_DIR.iterdir() if d.is_dir()],
        key=lambda d: d.name,
    )
    to_delete = folders[:-MAX_BACKUPS] if len(folders) > MAX_BACKUPS else []
    for folder in to_delete:
        try:
            import shutil
            shutil.rmtree(folder)
            logger.info('Pruned old backup: %s', folder)
        except Exception as e:
            logger.warning('Could not prune %s: %s', folder, e)


def run_backup(tables: list[str] | None = None) -> list[dict]:
    """
    Export all (or specified) tables to backups/<timestamp>/.
    Returns list of result dicts (one per table).
    """
    tables = tables or TABLES
    ts     = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')

    logger.info('Starting backup — timestamp: %s, tables: %s', ts, tables)

    try:
        client = _get_client()
    except RuntimeError as e:
        logger.error('%s', e)
        return [{'table': t, 'rows': 0, 'path': '', 'status': 'error', 'error': str(e)} for t in tables]

    results = [_export_table(client, t, ts) for t in tables]
    _prune_old_backups()

    ok    = sum(1 for r in results if r['status'] == 'ok')
    total = sum(r['rows'] for r in results if r['status'] == 'ok')
    logger.info('Backup complete: %d/%d tables, %d total rows', ok, len(results), total)
    return results


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    sys.path.insert(0, str(_ROOT))

    parser = argparse.ArgumentParser(description='Backup Supabase tables to CSV')
    parser.add_argument(
        '--tables', nargs='+', metavar='TABLE',
        help=f'Tables to backup (default: {", ".join(TABLES)})',
    )
    args = parser.parse_args()

    results = run_backup(tables=args.tables)

    print()
    print('═' * 60)
    print('  DATABASE BACKUP')
    print('═' * 60)
    for r in results:
        status = 'OK' if r['status'] == 'ok' else 'FAIL'
        rows   = f"{r['rows']:,}" if r['status'] == 'ok' else r['error']
        print(f'  [{status}] {r["table"]:<25} {rows}')
    print('═' * 60)
