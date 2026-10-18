from scraper.engine import build_rescue_signal_rows


def _row(i, price, size, ptype='apartment', suburb='woodstock', beds=1):
    return {'listing_id': f'x{i}', 'suburb_normalised': suburb, 'listing_type': 'sale',
            'bedrooms': beds, 'property_type': ptype, 'price': price, 'size_m2': size,
            'price_per_m2': round(price / size, 2) if size else None, 'title': 't'}


def _pool():
    return [_row(i, 2_000_000 + i * 50_000, 50) for i in range(10)]


def test_cheap_listing_gets_guarded_deal():
    rows = _pool() + [_row(99, 1_000_000, 50, beds=None)]
    out = build_rescue_signal_rows(rows, set(), ['x99'], {'woodstock': 50}, 'now')
    assert len(out) == 1
    s = out[0]
    assert s['listing_id'] == 'x99' and s['confidence'] == 'low_blended'
    assert s['verdict'] in ('deal', 'fair')            # never strong_deal from a rescue
    assert s['homelens_score'] <= 55 and s['gap_pct'] < -30
    assert s['deal_badge'] == 'excellent_deal'


def test_skips_already_scored_missing_or_thin_pool():
    rows = _pool() + [_row(99, 1_000_000, 50)]
    assert build_rescue_signal_rows(rows, {'x99'}, ['x99'], {}, 'n') == []        # already scored
    assert build_rescue_signal_rows(rows, set(), ['nope'], {}, 'n') == []         # not active
    thin = [_row(i, 2_000_000, 50) for i in range(3)] + [_row(99, 1_000_000, 50)]
    assert build_rescue_signal_rows(thin, set(), ['x99'], {}, 'n') == []          # pool < 6


def test_str_potential_estimates():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
    from compute_investment import str_potential
    stats = {('bantry-bay', '1'): {'gross': 40000.0, 'months': 12, 'confidence': 'high'}}
    # studio -> 75% of the 1-bed figure, flagged estimated
    s = str_potential(stats, 'bantry-bay', 0, None)
    assert s['str_monthly_gross'] == 30000 and s['str_confidence'] == 'estimated'
    # real 1-bed -> unchanged, keeps AirDNA confidence
    s = str_potential(stats, 'bantry-bay', 1, None)
    assert s['str_monthly_gross'] == 40000 and s['str_confidence'] == 'high'
    # no bedrooms: size decides, apartments only
    assert str_potential(stats, 'bantry-bay', None, None, size_m2=50, property_type='apartment')['str_monthly_gross'] == 40000
    assert str_potential(stats, 'bantry-bay', None, None, size_m2=30, property_type='apartment')['str_monthly_gross'] == 30000
    assert str_potential(stats, 'bantry-bay', None, None, size_m2=50, property_type='house') is None
    assert str_potential(stats, 'bantry-bay', None, None) is None
    assert str_potential(stats, 'sea-point', 0, None) is None
