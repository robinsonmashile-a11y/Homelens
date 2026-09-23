"""
Homelens — Daily Analytics Report.

Reads yesterday's user_events from Supabase and prints a summary.
Requires SUPABASE_URL and SUPABASE_SERVICE_KEY in .env
(service key needed to read the private user_events table).

Usage:
    python scripts/analytics_report.py
    python scripts/analytics_report.py --date 2024-12-01
    python scripts/analytics_report.py --days 7
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


def _get_client():
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=_ROOT / '.env', override=True)
    from supabase import create_client

    url = os.getenv('SUPABASE_URL', '')
    # Use service key so we can read the private user_events table
    key = os.getenv('SUPABASE_SERVICE_KEY') or os.getenv('SUPABASE_KEY', '')
    if not url or not key:
        raise RuntimeError('SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env')
    return create_client(url, key)


def _fetch_events(client, date_str: str) -> list[dict]:
    """Fetch all events for a single calendar day (UTC)."""
    start = f'{date_str}T00:00:00+00:00'
    end   = f'{date_str}T23:59:59+00:00'

    all_rows: list[dict] = []
    offset, batch = 0, 1000
    while True:
        resp = (
            client.table('user_events')
            .select('*')
            .gte('occurred_at', start)
            .lte('occurred_at', end)
            .range(offset, offset + batch - 1)
            .execute()
        )
        rows = resp.data or []
        all_rows.extend(rows)
        if len(rows) < batch:
            break
        offset += batch

    return all_rows


def _pct(n: int, total: int) -> str:
    if total == 0:
        return '—%'
    return f'{n / total * 100:.0f}%'


def _bar(n: int, max_n: int, width: int = 20) -> str:
    if max_n == 0:
        return ''
    filled = round(n / max_n * width)
    return '█' * filled + '░' * (width - filled)


def print_report(events: list[dict], date_str: str) -> None:
    div = '═' * 56

    print(f'\n{div}')
    print(f'  HOMELENS DAILY ANALYTICS — {date_str}')
    print(div)

    if not events:
        print('  No events recorded for this date.')
        print(div + '\n')
        return

    # ── Top-line ─────────────────────────────────────────────
    sessions = {e['session_id'] for e in events}
    print(f'\n  Unique sessions (visitors) : {len(sessions):,}')
    print(f'  Total events               : {len(events):,}')
    print(f'  Events per session         : {len(events)/max(len(sessions),1):.1f}')

    # ── Page views ───────────────────────────────────────────
    page_views = [e for e in events if e['event_type'] == 'page_view']
    if page_views:
        pages = Counter(e['page'] for e in page_views if e.get('page'))
        print(f'\n  Page views ({len(page_views):,} total)')
        print('  ' + '─' * 40)
        max_pv = max(pages.values(), default=1)
        for page, count in pages.most_common():
            print(f'  {(page or "—"):<20} {count:>4}  {_bar(count, max_pv, 12)}')

    # ── Tab usage ────────────────────────────────────────────
    tab_switches = [e for e in events if e['event_type'] == 'tab_switch']
    if tab_switches:
        tabs = Counter(e.get('metadata', {}).get('tab') or e.get('page') for e in tab_switches)
        print(f'\n  Tab switches ({len(tab_switches):,} total)')
        for tab, n in tabs.most_common():
            print(f'  {(tab or "—"):<30} {n}')

    # ── Renter flow ──────────────────────────────────────────
    rent_checks = [e for e in events if e['event_type'] == 'rent_check_submitted']
    if rent_checks:
        print(f'\n  ── Renter Flow ──────────────────────────────')
        print(f'  Rent checks submitted : {len(rent_checks):,}')
        suburbs = Counter(e.get('suburb') or e.get('input_suburb') for e in rent_checks)
        print(f'  Most checked suburbs:')
        for sub, n in suburbs.most_common(5):
            print(f'    {(sub or "—"):<28} {n}')

        results = [e for e in events if e['event_type'] in ('rent_result_above', 'rent_result_below')]
        above   = sum(1 for e in results if e['event_type'] == 'rent_result_above')
        below   = sum(1 for e in results if e['event_type'] == 'rent_result_below')
        if results:
            print(f'  Results: above median={above}, below median={below}')

    # ── Buyer flow ───────────────────────────────────────────
    buyer_views  = [e for e in events if e['event_type'] == 'buyer_listing_viewed']
    buyer_filter = [e for e in events if e['event_type'] == 'buyer_filter_applied']
    if buyer_views or buyer_filter:
        print(f'\n  ── Buyer Flow ───────────────────────────────')
        print(f'  Listing cards viewed : {len(buyer_views):,}')
        print(f'  Filters applied      : {len(buyer_filter):,}')

    # ── Investor flow ────────────────────────────────────────
    inv_views     = [e for e in events if e['event_type'] == 'investor_listing_viewed']
    inv_breakdowns = [e for e in events if e['event_type'] == 'investor_breakdown_opened']
    if inv_views or inv_breakdowns:
        print(f'\n  ── Investor Flow ────────────────────────────')
        print(f'  Investor listings viewed   : {len(inv_views):,}')
        print(f'  Breakdowns opened          : {len(inv_breakdowns):,}')

    # ── Key conversion events ────────────────────────────────
    listing_opens   = [e for e in events if e['event_type'] == 'listing_opened']
    agency_clicks   = [e for e in events if e['event_type'] == 'listing_agency_clicked']
    waitlist_subs   = [e for e in events if e['event_type'] == 'waitlist_submitted']
    alert_subs      = [e for e in events if e['event_type'] == 'alert_submitted']

    print(f'\n  ── Conversions ──────────────────────────────')
    print(f'  Listings opened        : {len(listing_opens):,}')
    print(f'  Agency clicks (leads)  : {len(agency_clicks):,}')
    if listing_opens:
        rate = len(agency_clicks) / len(listing_opens) * 100
        print(f'  Lead conversion rate   : {rate:.1f}%')
    print(f'  Alert signups          : {len(alert_subs):,}')
    print(f'  Waitlist signups       : {len(waitlist_subs):,}')

    if agency_clicks:
        print(f'\n  Top clicked listings:')
        clicked = Counter(e.get('listing_id') for e in agency_clicks if e.get('listing_id'))
        for lid, n in clicked.most_common(5):
            sub = next((e.get('suburb') for e in agency_clicks if e.get('listing_id') == lid), '—')
            print(f'    {lid:<32} {sub:<20} ×{n}')

    # ── Engagement ───────────────────────────────────────────
    s25  = sum(1 for e in events if e['event_type'] == 'scroll_25')
    s50  = sum(1 for e in events if e['event_type'] == 'scroll_50')
    s75  = sum(1 for e in events if e['event_type'] == 'scroll_75')
    s100 = sum(1 for e in events if e['event_type'] == 'scroll_100')
    t30  = sum(1 for e in events if e['event_type'] == 'time_30s')
    t120 = sum(1 for e in events if e['event_type'] == 'time_120s')
    t300 = sum(1 for e in events if e['event_type'] == 'time_300s')

    total_sessions = len(sessions)
    print(f'\n  ── Engagement ───────────────────────────────')
    print(f'  Scroll depth reached:')
    print(f'    25%  {s25:>5}  ({_pct(s25,  total_sessions)} of sessions)')
    print(f'    50%  {s50:>5}  ({_pct(s50,  total_sessions)} of sessions)')
    print(f'    75%  {s75:>5}  ({_pct(s75,  total_sessions)} of sessions)')
    print(f'    100% {s100:>5}  ({_pct(s100, total_sessions)} of sessions)')
    print(f'  Time on page:')
    print(f'    30s   {t30:>5}  sessions')
    print(f'    2min  {t120:>5}  sessions')
    print(f'    5min  {t300:>5}  sessions')

    # ── Device split ─────────────────────────────────────────
    devices = Counter(e.get('device') for e in events if e.get('device'))
    if devices:
        print(f'\n  ── Devices ──────────────────────────────────')
        for device, n in devices.most_common():
            print(f'  {device:<10} {n:>5}  {_pct(n, len(events))}')

    # ── Popular suburbs (across all events) ─────────────────
    suburb_events = [e for e in events if e.get('suburb')]
    if suburb_events:
        suburbs = Counter(e['suburb'] for e in suburb_events)
        print(f'\n  ── Most Active Suburbs ──────────────────────')
        max_s = max(suburbs.values(), default=1)
        for sub, n in suburbs.most_common(8):
            print(f'  {sub:<28} {n:>4}  {_bar(n, max_s, 10)}')

    print(f'\n{div}\n')


def run_report(date_str: str | None = None, days: int = 1) -> None:
    client = _get_client()

    if days > 1:
        # Multi-day summary
        base = datetime.now(timezone.utc).date()
        for i in range(days - 1, -1, -1):
            d = (base - timedelta(days=i)).strftime('%Y-%m-%d')
            events = _fetch_events(client, d)
            print_report(events, d)
        return

    if not date_str:
        date_str = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d')

    events = _fetch_events(client, date_str)
    print_report(events, date_str)


if __name__ == '__main__':
    import logging
    logging.basicConfig(level=logging.WARNING)

    parser = argparse.ArgumentParser(description='Homelens daily analytics report')
    parser.add_argument('--date', help='Date to report on (YYYY-MM-DD), default = yesterday')
    parser.add_argument('--days', type=int, default=1,
                        help='Number of recent days to report (overrides --date)')
    args = parser.parse_args()

    run_report(date_str=args.date, days=args.days)
