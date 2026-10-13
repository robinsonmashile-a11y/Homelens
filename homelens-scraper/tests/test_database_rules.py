"""
Step 4 write rules, exercised through the real upsert functions in
scraper/database.py against an in-memory stand-in for the Supabase client.

  * status / status_source are written on every run
  * a live -> under_offer / sold change updates the status and sets
    status_changed_at (any other change is stamped too; no change, no stamp)
  * a stored levy / rates figure is NEVER replaced by null when a run finds nothing
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scraper import database


class _Query:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self._filters, self._op, self._payload, self._range = [], 'select', None, None

    def select(self, *_a, **_k): self._op = 'select'; return self
    def insert(self, row): self._op, self._payload = 'insert', row; return self
    def update(self, row): self._op, self._payload = 'update', row; return self
    def eq(self, col, val): self._filters.append(('eq', col, val)); return self
    def gte(self, col, val): self._filters.append(('gte', col, val)); return self
    def range(self, lo, hi): self._range = (lo, hi); return self

    def _match(self, row):
        for op, col, val in self._filters:
            v = row.get(col)
            if op == 'eq' and v != val:
                return False
            if op == 'gte' and (v is None or v < val):
                return False
        return True

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self._op == 'insert':
            rows.append(dict(self._payload))
            return type('R', (), {'data': [self._payload]})()
        hits = [r for r in rows if self._match(r)]
        if self._op == 'update':
            for r in hits:
                r.update(self._payload)
            return type('R', (), {'data': hits})()
        if self._range:
            hits = hits[self._range[0]:self._range[1] + 1]
        return type('R', (), {'data': [dict(r) for r in hits]})()


class FakeClient:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {}

    def table(self, name):
        return _Query(self, name)

    def listing(self, url='https://x/1'):
        return next(r for r in self.tables['listings'] if r['listing_url'] == url)


@pytest.fixture
def db(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(database, 'get_client', lambda: client)
    return client


def scraped(**over):
    base = {
        'listing_id': 'a1', 'listing_url': 'https://x/1', 'source': 'greeff',
        'listing_type': 'sale', 'title': 'Flat', 'price': 2_000_000, 'price_status': 'ok',
        'is_active': True, 'listing_status': 'live', 'status_source': 'default',
    }
    base.update(over)
    return base


def run(listing):
    return database.upsert_listing_by_url(dict(listing))


# ── status ────────────────────────────────────────────────────────────────────

def test_new_listing_stores_status_and_costs(db):
    run(scraped(levy_monthly=2267.0, rates_monthly=1000.0, costs_source='site',
                costs_raw_snippet='Monthly Levy: R2,267', costs_checked_at='2026-09-23T06:00:00+00:00'))
    row = db.listing()
    assert row['listing_status'] == 'live' and row['status_source'] == 'default'
    assert (row['levy_monthly'], row['rates_monthly'], row['costs_source']) == (2267.0, 1000.0, 'site')
    assert 'status_changed_at' not in row            # live on first sight: nothing changed


def test_new_listing_first_seen_under_offer_is_stamped(db):
    run(scraped(listing_status='under_offer', status_source='site'))
    assert db.listing()['status_changed_at']


@pytest.mark.parametrize('new', ['under_offer', 'sold'])
def test_live_to_under_offer_or_sold_updates_and_stamps(db, new):
    run(scraped())
    assert 'status_changed_at' not in db.listing()
    run(scraped(listing_status=new, status_source='site'))
    row = db.listing()
    assert row['listing_status'] == new and row['status_source'] == 'site'
    assert row['status_changed_at']


def test_unchanged_status_does_not_move_the_timestamp(db):
    run(scraped(listing_status='under_offer', status_source='site'))
    first = db.listing()['status_changed_at']
    run(scraped(listing_status='under_offer', status_source='site'))
    assert db.listing()['status_changed_at'] == first


def test_under_offer_to_sold_and_back_to_live_are_stamped(db):
    run(scraped(listing_status='under_offer', status_source='site'))
    t1 = db.listing()['status_changed_at']
    run(scraped(listing_status='sold', status_source='site'))
    t2 = db.listing()['status_changed_at']
    assert t2 > t1 and db.listing()['listing_status'] == 'sold'
    run(scraped(listing_status='live', status_source='default'))     # deal fell through
    assert db.listing()['listing_status'] == 'live'
    assert db.listing()['status_changed_at'] > t2


def test_status_is_written_every_run(db):
    run(scraped(status_source='default'))
    run(scraped(status_source='fallback'))
    assert db.listing()['status_source'] == 'fallback'


# ── costs: never overwrite a real number with null ───────────────────────────

def _seed_costs(db):
    run(scraped(levy_monthly=2267.0, rates_monthly=1000.0, costs_source='site',
                costs_raw_snippet='Monthly Levy: R2,267 | Monthly Rates: R1,000',
                costs_checked_at='2026-09-01T06:00:00+00:00'))


def test_run_that_finds_nothing_keeps_the_old_levy_and_rates(db):
    _seed_costs(db)
    run(scraped(levy_monthly=None, rates_monthly=None, levies_rates_combined_monthly=None,
                costs_source=None, costs_raw_snippet=None))
    row = db.listing()
    assert (row['levy_monthly'], row['rates_monthly']) == (2267.0, 1000.0)
    assert row['costs_source'] == 'site'
    assert row['costs_raw_snippet'] == 'Monthly Levy: R2,267 | Monthly Rates: R1,000'


def test_run_without_any_cost_keys_keeps_them_too(db):
    _seed_costs(db)                    # e.g. a listing skipped this run (already checked)
    run(scraped())
    assert (db.listing()['levy_monthly'], db.listing()['rates_monthly']) == (2267.0, 1000.0)


def test_a_new_real_number_replaces_the_old_one(db):
    _seed_costs(db)
    run(scraped(levy_monthly=2400.0, costs_source='fallback', costs_raw_snippet='Levies: R2 400 pm'))
    row = db.listing()
    assert row['levy_monthly'] == 2400.0
    assert row['rates_monthly'] == 1000.0            # not found this time -> kept
    assert row['costs_source'] == 'fallback'


def test_only_the_missing_field_is_kept(db):
    _seed_costs(db)
    run(scraped(rates_monthly=1100.0, costs_source='site', costs_raw_snippet='Monthly Rates: R1,100'))
    row = db.listing()
    assert (row['levy_monthly'], row['rates_monthly']) == (2267.0, 1100.0)


def test_costs_checked_at_is_recorded_even_when_nothing_was_found(db):
    run(scraped())
    run(scraped(costs_checked_at='2026-09-23T06:00:00+00:00'))
    assert db.listing()['costs_checked_at'] == '2026-09-23T06:00:00+00:00'


def test_combined_figure_is_stored_and_kept(db):
    run(scraped(levies_rates_combined_monthly=3267.0, costs_source='fallback'))
    run(scraped())
    assert db.listing()['levies_rates_combined_monthly'] == 3267.0
    assert db.listing().get('levy_monthly') is None


def test_upsert_by_id_follows_the_same_rules(db):
    database.upsert_listing(dict(scraped(levy_monthly=2267.0, costs_source='site')))
    database.upsert_listing(dict(scraped(listing_status='sold', status_source='site')))
    row = db.tables['listings'][0]
    assert row['levy_monthly'] == 2267.0 and row['listing_status'] == 'sold' and row['status_changed_at']


# ── which listings still need a page visit ───────────────────────────────────

def test_get_costs_checked_ids_respects_source_and_age(db):
    now = datetime.now(timezone.utc)
    ago = lambda d: (now - timedelta(days=d)).isoformat()
    db.tables['listings'] = [
        {'listing_id': 'fresh', 'source': 'greeff', 'costs_checked_at': ago(2)},
        {'listing_id': 'stale', 'source': 'greeff', 'costs_checked_at': ago(45)},
        {'listing_id': 'never', 'source': 'greeff', 'costs_checked_at': None},
        {'listing_id': 'other', 'source': 'tyson', 'costs_checked_at': ago(1)},
    ]
    assert database.get_costs_checked_ids('greeff', 30) == {'fresh'}


def test_get_costs_checked_ids_pages_past_1000_rows(db):
    ts = datetime.now(timezone.utc).isoformat()
    db.tables['listings'] = [{'listing_id': f'id{i}', 'source': 'greeff', 'costs_checked_at': ts}
                             for i in range(2350)]
    assert len(database.get_costs_checked_ids('greeff', 30)) == 2350
