"""Scripts that only have Cape Town data must never pick up Johannesburg rows."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
from tests.test_engine_cities import FakeDB  # noqa: E402

import compute_investment as ci  # noqa: E402


def _baseline(city, median, beds=2):
    return {'city': city, 'suburb_normalised': 'rosebank', 'listing_type': 'rent', 'bedrooms': beds,
            'property_type': 'apartment', 'median_price': median, 'sample_size_price': 10,
            'market_consistency': 'consistent', 'has_sufficient_data': True}


def test_rent_baselines_come_only_from_cape_town():
    db = FakeDB([])
    db.tables['suburb_baselines'] = [_baseline('cape-town', 22_000), _baseline('johannesburg', 11_000)]
    exact, fallback = ci.fetch_rent_baselines(db)
    assert exact[('rosebank', 2, 'apartment')]['median_price'] == 22_000
    assert fallback[('rosebank', 2)]['median_price'] == 22_000


def test_johannesburg_sale_listings_never_enter_the_investment_model():
    rows = [{'listing_id': 'ct1', 'city': 'cape-town', 'listing_type': 'sale', 'is_active': True},
            {'listing_id': 'jo1', 'city': 'johannesburg', 'listing_type': 'sale', 'is_active': True}]
    assert [r['listing_id'] for r in ci.fetch_sale_listings(FakeDB(rows), ['ct1', 'jo1'])] == ['ct1']


def test_explanations_are_generated_for_cape_town_only(monkeypatch):
    import generate_explanations as ge
    db = FakeDB([{'listing_id': 'ct1', 'city': 'cape-town', 'title': 't'},
                 {'listing_id': 'jo1', 'city': 'johannesburg', 'title': 't'}])
    sig = lambda i: {'listing_id': i, 'median_price': 1}                      # noqa: E731
    db.tables['listing_signals'] = [sig('ct1'), sig('jo1')]
    db.tables['listing_explanations'] = []
    assert [p['listing_id'] for p in ge.fetch_pending(db)] == ['ct1']
