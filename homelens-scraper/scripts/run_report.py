"""
Per-site report on listing status and levy/rates capture.

Reads the listings the last run touched (last_scraped_at >= --since) straight
from the database, so listings whose costs were checked on an earlier run
(and skipped this time) still count with their stored values.

  python scripts/run_report.py                       # last 24h
  python scripts/run_report.py --since 2026-09-23T06:00:00+00:00

Also called at the end of scripts/run_all_scrapers.py.

Levy / rates percentages are over SALE listings only — rental levies and
rates are not shown on listings. Status counts cover every listing.

Flags:
  SELECTORS BROKEN  costs_source is mostly 'fallback' — the site's own
                    selectors stopped matching and the text fallback is
                    picking up the slack (or guessing).
  NO COSTS          many sale listings but not one levy/rates figure — either
                    the site shows none, or selectors AND fallback both fail.
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FALLBACK_SHARE_FLAG = 0.5   # flag when more than this share of costs came from fallback
MIN_COSTS_FOR_FLAG  = 5     # ...and at least this many listings have costs
MIN_SALES_NO_COSTS  = 20    # 'NO COSTS' needs at least this many sale listings

_COLUMNS = ('source,listing_type,listing_status,levy_monthly,rates_monthly,'
            'levies_rates_combined_monthly,costs_source')


def text_only_sources() -> set[str]:
    """
    Sources whose SELECTORS set 'text_only': True (levy/rates only ever appear
    in free text, e.g. Engel & Völkers descriptions). Their costs are always
    'fallback' by design, so the 'selectors broken' flag would be noise.
    """
    import importlib
    out: set[str] = set()
    for name in ALL_SOURCES:
        try:
            sel = importlib.import_module(f'scraper.{name}').SELECTORS
        except Exception:
            continue
        if sel.get('text_only'):
            out.add(name)
    return out


ALL_SOURCES = (
    'abc_international', 'century21', 'chaseveritt', 'dgproperties', 'duarte',
    'engelvolkers', 'exprealty', 'greeff', 'gumtree', 'harcourts', 'jawitz',
    'justproperty', 'lionlead', 'pamgolding', 'quay1', 'rawson', 'remax',
    'seeff', 'sothebys', 'tyson',
)


def summarise(rows: list[dict], text_only: set[str] | None = None) -> dict[str, dict]:
    """rows -> {source: stats}. Pure function (no DB, no printing)."""
    text_only = text_only or set()
    by_source: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_source[r.get('source') or 'unknown'].append(r)

    out: dict[str, dict] = {}
    for source, items in by_source.items():
        sales = [r for r in items if r.get('listing_type') == 'sale']
        with_levy = sum(1 for r in sales if r.get('levy_monthly') is not None)
        with_rates = sum(1 for r in sales if r.get('rates_monthly') is not None)
        with_combined = sum(1 for r in sales if r.get('levies_rates_combined_monthly') is not None)
        with_any = [r for r in sales if any(r.get(k) is not None for k in (
            'levy_monthly', 'rates_monthly', 'levies_rates_combined_monthly'))]
        sources = Counter(r.get('costs_source') or 'unknown' for r in with_any)
        n_costs = len(with_any)

        flags: list[str] = []
        if (source not in text_only and n_costs >= MIN_COSTS_FOR_FLAG
                and sources['fallback'] / n_costs > FALLBACK_SHARE_FLAG):
            flags.append('SELECTORS BROKEN — costs mostly from fallback')
        if len(sales) >= MIN_SALES_NO_COSTS and n_costs == 0:
            flags.append('NO COSTS — none of the sale listings has a levy/rates figure')

        out[source] = {
            'scraped': len(items),
            'sales': len(sales),
            'levy_pct': _pct(with_levy, len(sales)),
            'rates_pct': _pct(with_rates, len(sales)),
            'combined_pct': _pct(with_combined, len(sales)),
            'site_pct': _pct(sources['site'], n_costs),
            'fallback_pct': _pct(sources['fallback'], n_costs),
            'description_pct': _pct(sources['description'], n_costs),
            'with_costs': n_costs,
            'status': dict(Counter(r.get('listing_status') or 'live' for r in items)),
            'flags': flags,
        }
    return dict(sorted(out.items()))


def _pct(part: int, whole: int) -> int | None:
    return round(100 * part / whole) if whole else None


def _fmt(p: int | None) -> str:
    return f'{p}%' if p is not None else '–'


def format_report(summary: dict[str, dict]) -> str:
    hdr = (f"  {'Source':<18}{'Scraped':>8}{'Sale':>6}  {'Levy':>5}{'Rates':>6}{'Comb':>6}"
           f"  {'Site':>5}{'Fallbk':>7}{'Descr':>6}  {'Live':>5}{'UndrOf':>7}{'Sold':>5}")
    lines = ['', '═' * len(hdr), '  LISTING STATUS + LEVIES/RATES — by site', '═' * len(hdr),
             '  Levy/Rates/Comb are % of SALE listings; Site/Fallbk/Descr are % of listings with costs.',
             '', hdr, '  ' + '─' * (len(hdr) - 2)]
    flagged: list[tuple[str, str]] = []
    totals: Counter = Counter()
    for source, s in summary.items():
        st = s['status']
        lines.append(
            f"  {source:<18}{s['scraped']:>8}{s['sales']:>6}  {_fmt(s['levy_pct']):>5}"
            f"{_fmt(s['rates_pct']):>6}{_fmt(s['combined_pct']):>6}  {_fmt(s['site_pct']):>5}"
            f"{_fmt(s['fallback_pct']):>7}{_fmt(s['description_pct']):>6}  "
            f"{st.get('live', 0):>5}{st.get('under_offer', 0):>7}{st.get('sold', 0):>5}"
            + ('   ⚠' if s['flags'] else '')
        )
        for k, v in st.items():
            totals[k] += v
        flagged += [(source, f) for f in s['flags']]
    lines += ['  ' + '─' * (len(hdr) - 2),
              f"  {'TOTAL':<18}{sum(s['scraped'] for s in summary.values()):>8}"
              f"{sum(s['sales'] for s in summary.values()):>6}{'':>34}"
              f"{totals.get('live', 0):>5}{totals.get('under_offer', 0):>7}{totals.get('sold', 0):>5}", '']
    if flagged:
        lines.append('  ⚠  NEEDS ATTENTION')
        lines += [f'     {src:<18} {msg}' for src, msg in flagged]
        lines.append('     Re-capture pages with scripts/capture_fixtures.py <site> and update that '
                     "scraper's SELECTORS block.")
        lines.append('')
    return '\n'.join(lines)


def fetch_rows(since_iso: str) -> list[dict]:
    from scraper.database import get_client
    client = get_client()
    rows: list[dict] = []
    offset, page = 0, 1000
    while True:
        batch = (client.table('listings').select(_COLUMNS)
                 .gte('last_scraped_at', since_iso)
                 .range(offset, offset + page - 1).execute()).data or []
        rows += batch
        if len(batch) < page:
            return rows
        offset += page


def print_run_report(since_iso: str) -> dict[str, dict]:
    summary = summarise(fetch_rows(since_iso), text_only_sources())
    print(format_report(summary))
    return summary


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv(override=True)
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    if '--since' in sys.argv:
        since = sys.argv[sys.argv.index('--since') + 1]
    print_run_report(since)
