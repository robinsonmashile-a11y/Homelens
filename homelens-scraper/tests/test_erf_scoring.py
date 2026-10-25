"""Houses: sale houses with a usable erf are scored on price per ERF m2 within a plot-size band."""
from __future__ import annotations

import pytest

from scraper import engine
from tests.test_engine_cities import FakeDB


def _house(i, price, erf, size=None, suburb='bergvliet', beds=3, city='cape-town', ltype='sale', ptype='house'):
    return {'listing_id': f'h{i}', 'city': city, 'suburb_normalised': suburb, 'listing_type': ltype,
            'bedrooms': beds, 'property_type': ptype, 'price': price, 'size_m2': size, 'erf_size': erf,
            'price_per_m2': round(price / size, 2) if size else None,
            'is_active': True, 'is_luxury': False, 'is_outlier': False, 'title': 't'}


def _db(rows):
    db = FakeDB(rows)
    db.tables['suburb_erf_baselines'] = []
    return db


def _score(db, city='cape-town'):
    base = engine.compute_suburb_baselines(db, city, store_city=True)
    erf = engine.compute_erf_baselines(db, city)
    engine.compute_listing_signals(db, base, {}, city=city, erf_baselines=erf)
    return erf, {s['listing_id']: s for s in db.tables['listing_signals']}


def _pool(n=8, erf=600, start=0, size=None, **kw):
    return [_house(start + i, (erf * 20_000) + i * 100_000, erf, size, **kw) for i in range(n)]


@pytest.mark.parametrize('erf,band', [(149, None), (150, '0-400'), (399, '0-400'), (400, '400-700'),
                                      (1199, '700-1200'), (2499, '1200-2500'), (4000, '2500-4000'),
                                      (4001, None), (None, None)])
def test_erf_bands(erf, band):
    assert engine._erf_band(erf) == band


def test_house_without_floor_size_is_scored_on_erf_within_its_band():
    rows = _pool(8, erf=600) + [_house(50, 6_000_000, 600)]            # R10k/erf m2 vs ~R20k median
    erf, sig = _score(_db(rows))
    s = sig['h50']
    assert s['comparison_method'] == engine.ERF_METHOD
    assert s['gap_pct'] < -40 and s['verdict'] in ('deal', 'strong_deal')
    assert s['deal_badge'] == 'excellent_deal'                         # medium+ confidence, valid erf


def test_big_plots_are_not_compared_with_small_plots():
    small = _pool(8, erf=500)                                          # R20k per m2
    big = [_house(100 + i, 2_000 * 20_000 // 4 + i * 1_000_000, 2000) for i in range(8)]   # ~R10k per m2
    erf, sig = _score(_db(small + big))
    assert {'bergvliet:400-700', 'bergvliet:1200-2500'} <= set(erf)
    assert abs(sig['h103']['gap_pct']) < 25                            # normal for its own band


def test_thin_band_falls_back_to_the_old_scoring():
    rows = _pool(8, erf=600, size=150) + [_house(60, 3_000_000, 1500, size=150)]   # only one 1200-2500 house
    erf, sig = _score(_db(rows))
    assert sig['h60']['comparison_method'] != engine.ERF_METHOD


def test_only_houses_for_sale_use_erf():
    rows = _pool(8, erf=600, ptype='townhouse') + _pool(8, erf=600, start=20, ltype='rent')
    erf, sig = _score(_db(rows))
    assert erf == {} and all(s['comparison_method'] != engine.ERF_METHOD for s in sig.values())


def test_oversized_plots_are_not_house_comparables():
    rows = _pool(8, erf=6_000)
    erf, sig = _score(_db(rows))
    assert erf == {}


def test_plot_and_floor_disagreement_drops_confidence_and_blocks_excellent():
    # 8 normal houses (erf 600 / floor 150) and a house on a big-for-band plot that is cheap per erf m2
    # but dear per floor m2.
    rows = [_house(i, 12_000_000 + i * 200_000, 600, 150) for i in range(8)]
    rows.append(_house(70, 6_000_000, 600, 60))        # erf: R10k (-50%); floor: R100k/m2 vs ~R86k (+16%)
    erf, sig = _score(_db(rows))
    s = sig['h70']
    assert s['comparison_method'] == engine.ERF_METHOD
    assert s['confidence'] == 'low' and s['deal_badge'] == 'good_deal'


def test_schema_not_migrated_leaves_houses_as_before():
    class NoTable(FakeDB):
        def table(self, name):
            if name == 'suburb_erf_baselines':
                raise Exception('relation "suburb_erf_baselines" does not exist')
            return super().table(name)
    db = NoTable(_pool(8, erf=600, size=150))
    assert engine.compute_erf_baselines(db, 'cape-town') == {}


def test_contradictory_floor_and_plot_sizes_are_not_used_for_erf():
    assert engine._erf_pm2(2_000_000, 400, 700) is None            # floor 1.75x the plot
    assert engine._erf_pm2(2_000_000, 400, 500) is not None        # two storeys on a small plot is fine
    assert engine._erf_pm2(2_000_000, 400, None) is not None
