"""
City isolation in the engine: Cape Town and Johannesburg are scored in separate
passes and can never be compared with each other. Uses an in-memory fake of the
Supabase client (select / eq / neq / filter / in_ / range / delete / upsert).
"""
from __future__ import annotations

import copy

import pytest

from scraper import engine


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self.mode, self.filters, self.window, self.payload, self.conflict = 'select', [], None, None, None

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self.filters.append(lambda r: r.get(col) == val)
        return self

    def neq(self, col, val):
        self.filters.append(lambda r: r.get(col) != val)
        return self

    def filter(self, col, op, val):
        assert (op, val) == ('not.is', 'null')
        self.filters.append(lambda r: r.get(col) is not None)
        return self

    def in_(self, col, vals):
        self.filters.append(lambda r: r.get(col) in vals)
        return self

    def limit(self, *_a):
        return self

    def range(self, a, b):
        self.window = (a, b)
        return self

    def delete(self):
        self.mode = 'delete'
        return self

    def update(self, payload):
        self.mode, self.payload = 'update', payload
        return self

    def upsert(self, payload, on_conflict=None):
        self.mode, self.payload, self.conflict = 'upsert', payload, on_conflict
        return self

    def execute(self):
        rows = self.db.tables[self.table]
        if self.mode == 'upsert':
            for new in (self.payload if isinstance(self.payload, list) else [self.payload]):
                keys = self.conflict.split(',')
                for old in rows:
                    if all(old.get(k) == new.get(k) for k in keys):
                        old.update(new)
                        break
                else:
                    rows.append(dict(new))
            return _Result([])
        hit = [r for r in rows if all(f(r) for f in self.filters)]
        if self.mode == 'delete':
            self.db.tables[self.table] = [r for r in rows if r not in hit]
            return _Result([])
        if self.mode == 'update':
            for r in hit:
                r.update(self.payload)
            return _Result([])
        if self.window:
            hit = hit[self.window[0]:self.window[1] + 1]
        return _Result(copy.deepcopy(hit))


class FakeDB:
    def __init__(self, listings, has_city_column=True):
        self.tables = {'listings': listings, 'suburb_baselines': [], 'listing_signals': [],
                       'suburb_attractiveness': []}
        self.has_city_column = has_city_column

    def table(self, name):
        if name == 'suburb_baselines' and not self.has_city_column:
            return _NoCityTable(self, name)
        return _Query(self, name)


class _NoCityTable(_Query):
    """suburb_baselines before engine_city.sql: any mention of `city` is a schema error."""
    def select(self, cols='*', *_a, **_k):
        if 'city' in cols:
            raise Exception('PGRST204 Could not find the city column of suburb_baselines')
        return self

    def upsert(self, payload, on_conflict=None):
        assert 'city' not in (payload if isinstance(payload, dict) else {}), 'city written before migration'
        return super().upsert(payload, on_conflict)


def _listing(i, city, suburb, price, size, beds=2, ltype='sale'):
    return {'listing_id': f'{city[:2]}{i}', 'city': city, 'suburb_normalised': suburb,
            'listing_type': ltype, 'bedrooms': beds, 'property_type': 'apartment',
            'price': price, 'size_m2': size, 'price_per_m2': round(price / size, 2),
            'is_active': True, 'is_luxury': False, 'is_outlier': False, 'title': 't'}


def _market():
    """'rosebank' exists in BOTH cities at very different price levels."""
    rows = []
    for i in range(8):                                     # Cape Town: ~R90k/m2
        rows.append(_listing(i, 'cape-town', 'rosebank', 4_500_000 + i * 100_000, 50))
    for i in range(8):                                     # Johannesburg: ~R20k/m2
        rows.append(_listing(i, 'johannesburg', 'rosebank', 1_000_000 + i * 20_000, 50))
    rows.append(_listing(90, 'johannesburg', 'rosebank', 600_000, 50))      # JHB bargain
    return rows


def _median_pm2(db, city):
    rows = [b for b in db.tables['suburb_baselines'] if b.get('city') == city and b['bedrooms'] == 2]
    assert len(rows) == 1
    return rows[0]['median_price_per_m2']


def test_baselines_never_mix_cities_even_for_the_same_suburb_name():
    db = FakeDB(_market())
    engine.compute_suburb_baselines(db, 'cape-town', store_city=True)
    engine.compute_suburb_baselines(db, 'johannesburg', store_city=True)
    assert len(db.tables['suburb_baselines']) == 2
    assert 85_000 < _median_pm2(db, 'cape-town') < 100_000
    assert 18_000 < _median_pm2(db, 'johannesburg') < 24_000


def test_rebuilding_one_city_leaves_the_other_citys_baselines_alone():
    db = FakeDB(_market())
    engine.compute_suburb_baselines(db, 'cape-town', store_city=True)
    before = copy.deepcopy([b for b in db.tables['suburb_baselines'] if b['city'] == 'cape-town'])
    engine.compute_suburb_baselines(db, 'johannesburg', store_city=True)
    engine.compute_suburb_baselines(db, 'johannesburg', store_city=True)       # re-run
    after = [b for b in db.tables['suburb_baselines'] if b['city'] == 'cape-town']
    assert {b['median_price_per_m2'] for b in before} == {b['median_price_per_m2'] for b in after}
    assert len(db.tables['suburb_baselines']) == 2


def test_a_listing_is_scored_only_against_its_own_city():
    db = FakeDB(_market())
    for city in ('cape-town', 'johannesburg'):
        base = engine.compute_suburb_baselines(db, city, store_city=True)
        engine.compute_listing_signals(db, base, {}, city=city, clear=(city == 'cape-town'))
    sig = {s['listing_id']: s for s in db.tables['listing_signals']}
    assert sig['jo90']['gap_pct'] < -20            # R600k is far below JHB's own median...
    assert sig['jo90']['verdict'] in ('deal', 'strong_deal')
    assert abs(sig['jo3']['gap_pct']) < 15         # ...yet a normal JHB flat is not "cheap vs Cape Town"
    assert abs(sig['ca3']['gap_pct']) < 15         # and a normal CT flat is not "expensive vs Joburg"
    assert 'ca90' not in sig


def test_cape_town_scores_are_identical_with_or_without_johannesburg_data():
    only_ct = FakeDB([r for r in _market() if r['city'] == 'cape-town'])
    both = FakeDB(_market())

    def run(db):
        base = engine.compute_suburb_baselines(db, 'cape-town', store_city=True)
        engine.compute_listing_signals(db, base, {'rosebank': 80}, city='cape-town')
        return sorted((s['listing_id'], s['z_score'], s['gap_pct'], s['homelens_score'])
                      for s in db.tables['listing_signals'])

    assert run(only_ct) == run(both)


def test_default_city_is_cape_town_and_old_behaviour_is_unchanged():
    db = FakeDB(_market(), has_city_column=False)
    base = engine.compute_suburb_baselines(db)                    # no city args at all
    assert all('city' not in b for b in base.values())            # nothing city-shaped is stored
    stats = engine.compute_listing_signals(db, base, {})
    assert stats['total'] == 8                                    # the 8 Cape Town listings only


def _run_engine(db, monkeypatch):
    monkeypatch.setattr(engine, '_supabase', lambda: db)
    monkeypatch.setattr(engine, 'check_schema', lambda c: True)
    monkeypatch.setattr(engine, 'populate_property_types', lambda c: {'apartment': 1})
    monkeypatch.setattr(engine, '_load_approved_ids', lambda: [])
    return engine.run_engine()


def test_run_engine_scores_both_cities_and_keeps_attractiveness_cape_town_only(monkeypatch):
    db = FakeDB(_market())
    db.tables['suburb_attractiveness'] = [{'suburb_normalised': 'rosebank', 'attractiveness_score': 90}]
    _pt, baselines, stats = _run_engine(db, monkeypatch)
    sig = {s['listing_id']: s for s in db.tables['listing_signals']}
    assert stats['by_city']['cape-town']['total'] == 8 and stats['by_city']['johannesburg']['total'] == 9
    assert sig['ca3']['attractiveness_score'] == 90               # Cape Town data used for Cape Town
    assert sig['jo3']['attractiveness_score'] == 50               # never applied to Johannesburg
    assert {b['city'] for b in baselines.values()} == {'cape-town', 'johannesburg'}
    assert len(baselines) == len({k for k in baselines})          # merged report keys don't collide


def test_run_engine_scores_cape_town_only_until_the_migration_is_applied(monkeypatch):
    db = FakeDB(_market(), has_city_column=False)
    _pt, baselines, stats = _run_engine(db, monkeypatch)
    ids = {s['listing_id'] for s in db.tables['listing_signals']}
    assert ids and all(i.startswith('ca') for i in ids)
    assert 'johannesburg' not in stats.get('by_city', {})


@pytest.mark.parametrize('fn', ['compute_suburb_baselines', 'compute_listing_signals'])
def test_no_hardcoded_city_in_engine_queries(fn):
    import inspect
    assert "'cape-town'" not in inspect.getsource(getattr(engine, fn))
