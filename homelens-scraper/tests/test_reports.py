"""Run summaries: new listings + fresh deals (scraper/reports.py). Fake client, no network."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scraper import reports

NOW = datetime.now(timezone.utc)


def iso(**delta):
    return (NOW - timedelta(**delta)).isoformat()


LISTINGS = [
    {'listing_id': 'a', 'source': 'greeff', 'listing_type': 'sale', 'suburb_normalised': 'sea-point',
     'price': 3_500_000, 'size_m2': 70.0, 'bedrooms': 2, 'listing_url': 'u/a', 'first_seen': iso(hours=2), 'is_active': True},
    {'listing_id': 'b', 'source': 'seeff', 'listing_type': 'sale', 'suburb_normalised': 'sea-point',
     'price': 2_000_000, 'size_m2': None, 'bedrooms': 1, 'listing_url': 'u/b', 'first_seen': iso(hours=5), 'is_active': True},
    {'listing_id': 'c', 'source': 'remax', 'listing_type': 'rent', 'suburb_normalised': 'gardens',
     'price': 18_000, 'size_m2': 55.0, 'bedrooms': None, 'listing_url': 'u/c', 'first_seen': iso(minutes=30), 'is_active': True},
    {'listing_id': 'old', 'source': 'greeff', 'listing_type': 'sale', 'suburb_normalised': 'gardens',
     'price': 1, 'size_m2': 1.0, 'bedrooms': 1, 'listing_url': 'u/o', 'first_seen': iso(days=9), 'is_active': True},
    {'listing_id': 'gone', 'source': 'greeff', 'listing_type': 'sale', 'suburb_normalised': 'gardens',
     'price': 1, 'size_m2': 1.0, 'bedrooms': 1, 'listing_url': 'u/g', 'first_seen': iso(hours=1), 'is_active': False},
]
SIGNALS = [
    {'listing_id': 'a', 'verdict': 'deal', 'verdict_label': 'Below market', 'gap_pct': -12.5, 'gap_rands': -500000, 'confidence': 'high', 'z_score': -0.8},
    {'listing_id': 'b', 'verdict': 'strong_deal', 'verdict_label': 'x', 'gap_pct': -30.0, 'gap_rands': -900000, 'confidence': 'low', 'z_score': -2.0},
    {'listing_id': 'c', 'verdict': 'fair', 'verdict_label': 'x', 'gap_pct': 1.0, 'gap_rands': 100, 'confidence': 'high', 'z_score': 0.1},
]


class Q:
    def __init__(self, rows):
        self.rows, self._range = rows, None

    def select(self, *_): return self
    def order(self, *_a, **_k): return self
    def eq(self, col, val): self.rows = [r for r in self.rows if r.get(col) == val]; return self
    def gte(self, col, val): self.rows = [r for r in self.rows if r.get(col) >= val]; return self
    def in_(self, col, vals): self.rows = [r for r in self.rows if r.get(col) in vals]; return self
    def range(self, a, b): self.rows = self.rows[a:b + 1]; return self
    def execute(self):
        class R: pass
        r = R(); r.data = self.rows; return r

    def __getattr__(self, name):
        if name in ('insert', 'update', 'upsert', 'delete'):
            raise AssertionError(f'report tried to write: {name}')
        raise AttributeError(name)


class FakeClient:
    def table(self, name):
        return Q(list({'listings': LISTINGS, 'listing_signals': SIGNALS}[name]))


def test_new_listings_counts_table_and_files(tmp_path):
    out = reports.new_listings_report(since=NOW - timedelta(days=1), report_dir=tmp_path, quiet=True, client=FakeClient())
    assert out['count'] == 3                                    # old + inactive excluded
    text = out['path'].read_text()
    assert 'NEW LISTINGS — 3 added' in text
    assert 'By source' in text and 'greeff' in text and 'sea-point' in text
    assert 'R3 500 000' in text and '70 m²' in text and '30m' in text
    assert out['path'].with_suffix('.csv').exists()


def test_fresh_deals_only_deal_verdicts_sorted_by_gap(tmp_path):
    out = reports.fresh_deals_report(since=NOW - timedelta(days=1), report_dir=tmp_path, quiet=True, client=FakeClient())
    assert [d['listing_id'] for d in out['rows']] == ['b', 'a']          # biggest discount first; 'c' is fair
    text = out['path'].read_text()
    assert '2 of 3 new listings' in text and '-30.0%' in text and 'strong_deal' in text


def test_since_defaults_to_previous_report_then_24h(tmp_path):
    first = reports.new_listings_report(report_dir=tmp_path, quiet=True, client=FakeClient())
    assert first['since'] <= NOW - timedelta(hours=23)                  # no previous report: 24h window
    for f in tmp_path.iterdir():
        f.unlink()                                                      # forget the report the first call saved
    stamp = (NOW - timedelta(hours=3)).strftime(reports._STAMP)
    (tmp_path / f'new_listings_{stamp}.txt').write_text('x')
    second = reports.new_listings_report(report_dir=tmp_path, quiet=True, client=FakeClient())
    assert second['count'] == 2 and abs((NOW - timedelta(hours=3) - second['since']).total_seconds()) < 2


def test_empty_result_still_reports(tmp_path):
    out = reports.fresh_deals_report(since=NOW + timedelta(days=1), report_dir=tmp_path, quiet=True, client=FakeClient())
    assert out['count'] == 0 and 'No new deals' in out['path'].read_text()


def test_errors_never_raise_into_the_job(tmp_path):
    class Boom:
        def table(self, *_): raise RuntimeError('db down')
    out = reports.new_listings_report(since=NOW, report_dir=tmp_path, quiet=True, client=Boom())
    assert out['count'] == 0 and out['path'] is None
