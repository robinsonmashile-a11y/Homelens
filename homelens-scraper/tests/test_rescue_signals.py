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
