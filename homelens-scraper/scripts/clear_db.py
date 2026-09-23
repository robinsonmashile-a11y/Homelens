"""
Nuclear option — wipes all data from listings, suburb_baselines, and scrape_runs.

Usage:
    python scripts/clear_db.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from supabase import create_client
import os


def get_client():
    return create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_KEY'])


def count_rows(client, table: str) -> int:
    result = client.table(table).select('*', count='exact').execute()
    return result.count or 0


def main():
    client = get_client()

    # Count current rows
    print('\nCurrent row counts:')
    tables = ['listings', 'suburb_baselines', 'scrape_runs']
    counts = {}
    for table in tables:
        counts[table] = count_rows(client, table)
        print(f'  {table:25s}: {counts[table]:,} rows')

    if all(v == 0 for v in counts.values()):
        print('\nAll tables already empty. Nothing to do.')
        return

    print()
    confirm = input('Are you sure you want to delete all data? Type YES to confirm: ').strip()
    if confirm != 'YES':
        print('Aborted.')
        return

    print()
    for table in tables:
        if counts[table] == 0:
            print(f'  {table:25s}: already empty, skipped')
            continue
        # Delete all rows by filtering on a always-true condition
        client.table(table).delete().neq('id' if table != 'listings' else 'listing_id', '__never_match__').execute()
        after = count_rows(client, table)
        deleted = counts[table] - after
        print(f'  {table:25s}: deleted {deleted:,} rows ({after} remaining)')

    print('\nDone. All tables cleared.')


if __name__ == '__main__':
    main()
