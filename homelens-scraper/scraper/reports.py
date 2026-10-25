"""
Run summaries — read-only against the database (the only write is the report file).

    from scraper.reports import new_listings_report, fresh_deals_report

    new_listings_report(since=run_started_at)    # end of the scrape job
    fresh_deals_report()                         # end of the engine job

new_listings_report   listings first seen since the last run: count, a table
                      (suburb, price, size, beds, source, age) and counts by
                      suburb and by source.
fresh_deals_report    those new listings that the engine scored as a deal:
                      verdict in DEAL_VERDICTS, with the gap vs market.

"New" means listings.first_seen >= `since` (first_seen is set once, on insert,
so a listing is new exactly once). `since` is, in order of precedence:
  1. the argument (a datetime; the scrape job passes its own start time)
  2. the timestamp of the previous report of the same kind in reports/
  3. 24 hours ago

Each call prints the report and saves reports/<kind>_<YYYYmmdd_HHMMSS>.txt and
.csv (UTC). Both return a dict: {count, rows, path, since}. They never raise
into the calling job: errors are logged and an empty result is returned.

Verdicts: the engine's slugs are strong_deal / deal / fair / above / overpriced
('deal' is labelled "Below market"); 'below_market' is accepted too, should the
engine ever emit it.
"""
from __future__ import annotations

import csv
import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger('homelens.reports')

REPORT_DIR = Path(__file__).resolve().parent.parent / 'reports'
DEFAULT_WINDOW = timedelta(hours=24)
DEAL_VERDICTS = ('strong_deal', 'deal', 'below_market')
_PAGE = 1000
_IN_CHUNK = 100                      # ids per .in_() call (keeps the request URL short)

_LISTING_COLS = ('listing_id,source,listing_type,suburb_normalised,suburb,price,size_m2,'
                 'bedrooms,listing_url,first_seen')
_STAMP = '%Y%m%d_%H%M%S'


# ── time window ───────────────────────────────────────────────────────────────

def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None


def _last_report_time(kind: str, report_dir: Path) -> datetime | None:
    stamps = []
    for p in report_dir.glob(f'{kind}_*.txt'):
        m = re.fullmatch(rf'{kind}_(\d{{8}}_\d{{6}})\.txt', p.name)
        if m:
            stamps.append(datetime.strptime(m.group(1), _STAMP).replace(tzinfo=timezone.utc))
    return max(stamps) if stamps else None


def resolve_since(kind: str, since: datetime | None, report_dir: Path, now: datetime) -> datetime:
    if since is not None:
        return since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    return _last_report_time(kind, report_dir) or (now - DEFAULT_WINDOW)


# ── data access (select only) ────────────────────────────────────────────────

def _client():
    from .database import get_client
    return get_client()


def fetch_new_listings(since: datetime, client=None) -> list[dict]:
    client = client or _client()
    rows: list[dict] = []
    offset = 0
    while True:
        page = (
            client.table('listings')
            .select(_LISTING_COLS)
            .gte('first_seen', since.isoformat())
            .eq('is_active', True)
            .order('first_seen', desc=True)
            .range(offset, offset + _PAGE - 1)
            .execute()
        ).data or []
        rows += page
        if len(page) < _PAGE:
            return rows
        offset += _PAGE


def fetch_signals(listing_ids: list[str], client=None) -> dict[str, dict]:
    client = client or _client()
    out: dict[str, dict] = {}
    for i in range(0, len(listing_ids), _IN_CHUNK):
        chunk = listing_ids[i:i + _IN_CHUNK]
        data = (
            client.table('listing_signals')
            .select('listing_id,verdict,verdict_label,gap_pct,gap_rands,confidence,z_score')
            .in_('listing_id', chunk)
            .execute()
        ).data or []
        out.update({r['listing_id']: r for r in data})
    return out


# ── formatting ────────────────────────────────────────────────────────────────

def _suburb(r: dict) -> str:
    return (r.get('suburb_normalised') or r.get('suburb') or 'unknown').strip() or 'unknown'


def _money(v) -> str:
    return f'R{int(v):,}'.replace(',', ' ') if v else '-'


def _size(v) -> str:
    return f'{v:g} m²' if v else '-'


def _age(first_seen, now: datetime) -> str:
    ts = _parse_ts(first_seen)
    if not ts:
        return '-'
    mins = max(0, int((now - ts).total_seconds() // 60))
    if mins < 60:
        return f'{mins}m'
    if mins < 60 * 48:
        return f'{mins // 60}h'
    return f'{mins // 1440}d'


def _table(headers: list[str], rows: list[list[str]], right: set[int] = frozenset()) -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    def line(cells):
        return '  '.join(c.rjust(widths[i]) if i in right else c.ljust(widths[i]) for i, c in enumerate(cells))
    return '\n'.join([line(headers), line(['-' * w for w in widths]), *(line(r) for r in rows)])


def _counts(title: str, counter: Counter, top: int | None = None) -> str:
    items = counter.most_common(top)
    return f'{title}\n' + '\n'.join(f'  {k:<28}{v:>5}' for k, v in items)


def format_new_listings(rows: list[dict], since: datetime, now: datetime) -> str:
    head = [f'NEW LISTINGS — {len(rows)} added since {since:%Y-%m-%d %H:%M} UTC '
            f'(report {now:%Y-%m-%d %H:%M} UTC)']
    if not rows:
        return head[0] + '\n\nNothing new.'
    table = _table(
        ['suburb', 'price', 'size', 'beds', 'source', 'type', 'age'],
        [[_suburb(r), _money(r.get('price')), _size(r.get('size_m2')),
          str(r['bedrooms']) if r.get('bedrooms') is not None else '-',
          r.get('source') or '-', r.get('listing_type') or '-', _age(r.get('first_seen'), now)]
         for r in rows],
        right={1, 2, 3, 6},
    )
    return '\n\n'.join([
        head[0],
        _counts('By source', Counter(r.get('source') or 'unknown' for r in rows)),
        _counts('By suburb (top 25)', Counter(_suburb(r) for r in rows), top=25),
        table,
        'age = time since the listing was first scraped.',
    ])


def format_fresh_deals(deals: list[dict], new_count: int, scored: int, since: datetime, now: datetime) -> str:
    head = (f'FRESH DEALS — {len(deals)} of {new_count} new listings scored as a deal '
            f'({scored} scored by the engine) since {since:%Y-%m-%d %H:%M} UTC '
            f'(report {now:%Y-%m-%d %H:%M} UTC)')
    if not deals:
        return head + '\n\nNo new deals this run.'
    table = _table(
        ['suburb', 'price', 'size', 'verdict', 'gap vs market', 'conf.', 'source'],
        [[_suburb(d), _money(d.get('price')), _size(d.get('size_m2')), d['verdict'],
          _gap(d), d.get('confidence') or '-', d.get('source') or '-'] for d in deals],
        right={1, 2, 4},
    )
    return '\n\n'.join([
        head,
        _counts('By verdict', Counter(d['verdict'] for d in deals)),
        _counts('By suburb', Counter(_suburb(d) for d in deals), top=25),
        table,
        'gap vs market: negative = priced below the suburb median for comparable listings. '
        'Sorted by largest discount first. Low-confidence rows come from thin baselines.',
    ])


def _gap(d: dict) -> str:
    pct, rands = d.get('gap_pct'), d.get('gap_rands')
    if pct is None and rands is None:
        return '-'
    parts = []
    if pct is not None:
        parts.append(f'{float(pct):+.1f}%')
    if rands is not None:
        parts.append(f'{"-" if rands < 0 else "+"}R{abs(int(rands)):,}'.replace(',', ' '))
    return ' / '.join(parts)


# ── saving ────────────────────────────────────────────────────────────────────

def _save(kind: str, text: str, csv_rows: list[dict], report_dir: Path, now: datetime) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    stem = report_dir / f'{kind}_{now:{_STAMP}}'
    path = stem.with_suffix('.txt')
    path.write_text(text + '\n', encoding='utf-8')
    if csv_rows:
        with stem.with_suffix('.csv').open('w', newline='', encoding='utf-8') as fh:
            w = csv.DictWriter(fh, fieldnames=list(csv_rows[0]))
            w.writeheader()
            w.writerows(csv_rows)
    return path


# ── public API ────────────────────────────────────────────────────────────────

def new_listings_report(since: datetime | None = None, report_dir: Path | str | None = None,
                        quiet: bool = False, client=None) -> dict:
    """Listings added since the last run. Read-only; saves reports/new_listings_<ts>.txt/.csv."""
    kind, now = 'new_listings', datetime.now(timezone.utc)
    report_dir = Path(report_dir) if report_dir else REPORT_DIR
    try:
        since = resolve_since(kind, since, report_dir, now)
        rows = fetch_new_listings(since, client)
        text = format_new_listings(rows, since, now)
        csv_rows = [{
            'suburb': _suburb(r), 'price': r.get('price'), 'size_m2': r.get('size_m2'),
            'bedrooms': r.get('bedrooms'), 'source': r.get('source'), 'listing_type': r.get('listing_type'),
            'first_seen': r.get('first_seen'), 'listing_id': r.get('listing_id'), 'url': r.get('listing_url'),
        } for r in rows]
        path = _save(kind, text, csv_rows, report_dir, now)
        if not quiet:
            print('\n' + text + f'\n\nsaved {path}\n')
        return {'count': len(rows), 'rows': rows, 'path': path, 'since': since}
    except Exception as e:                                   # a report must never break the job
        logger.error('new_listings_report failed: %s', e, exc_info=True)
        return {'count': 0, 'rows': [], 'path': None, 'since': since}


def fresh_deals_report(since: datetime | None = None, report_dir: Path | str | None = None,
                       quiet: bool = False, client=None) -> dict:
    """New listings the engine scored as a deal. Read-only; saves reports/fresh_deals_<ts>.txt/.csv."""
    kind, now = 'fresh_deals', datetime.now(timezone.utc)
    report_dir = Path(report_dir) if report_dir else REPORT_DIR
    try:
        since = resolve_since(kind, since, report_dir, now)
        new_rows = fetch_new_listings(since, client)
        signals = fetch_signals([r['listing_id'] for r in new_rows], client) if new_rows else {}
        deals = []
        for r in new_rows:
            sig = signals.get(r['listing_id'])
            if sig and sig.get('verdict') in DEAL_VERDICTS:
                deals.append({**r, **{k: sig.get(k) for k in ('verdict', 'verdict_label', 'gap_pct',
                                                              'gap_rands', 'confidence', 'z_score')}})
        deals.sort(key=lambda d: (d.get('gap_pct') is None, float(d['gap_pct']) if d.get('gap_pct') is not None else 0.0))
        text = format_fresh_deals(deals, len(new_rows), len(signals), since, now)
        csv_rows = [{
            'suburb': _suburb(d), 'price': d.get('price'), 'size_m2': d.get('size_m2'), 'verdict': d['verdict'],
            'gap_pct': d.get('gap_pct'), 'gap_rands': d.get('gap_rands'), 'confidence': d.get('confidence'),
            'source': d.get('source'), 'listing_type': d.get('listing_type'),
            'listing_id': d.get('listing_id'), 'url': d.get('listing_url'),
        } for d in deals]
        path = _save(kind, text, csv_rows, report_dir, now)
        if not quiet:
            print('\n' + text + f'\n\nsaved {path}\n')
        return {'count': len(deals), 'rows': deals, 'path': path, 'since': since}
    except Exception as e:
        logger.error('fresh_deals_report failed: %s', e, exc_info=True)
        return {'count': 0, 'rows': [], 'path': None, 'since': since}
