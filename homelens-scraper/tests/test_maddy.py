"""Maddy fallback: validation, safety caps, caching, precedence. No network, no real model."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from scraper import maddy
from tests.test_engine_cities import FakeDB

NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)
GOOD = {'homelens_score': 62, 'verdict': 'deal', 'verdict_label': 'Below market', 'deal_badge': 'good_deal',
        'confidence': 'medium', 'reason': 'Priced under what similar flats go for.', 'what_it_really_is': ''}


def _l(i, price=19_000, **kw):
    return {'listing_id': f'l{i}', 'city': 'cape-town', 'suburb': 'Camps Bay', 'suburb_normalised': 'camps-bay',
            'listing_type': 'rent', 'property_type': 'apartment', 'bedrooms': 1, 'bathrooms': 1, 'size_m2': None,
            'price': price, 'title': 't', 'description': 'Sea views', 'first_seen': '2026-10-20T00:00:00+00:00',
            'is_furnished': True, 'is_luxury': False, 'is_outlier': False, 'is_active': True, **kw}


def test_prompt_is_stored_unchanged_and_hash_tracks_it():
    text = maddy.load_prompt()
    assert text.startswith("You are Maddy, Homelens's property expert.")
    assert '"homelens_score"' in text and 'NEVER use high confidence' in text
    assert maddy.prompt_version(text) != maddy.prompt_version(text + ' ')


def test_parse_reply_tolerates_fences():
    assert maddy.parse_reply('```json\n' + json.dumps(GOOD) + '\n```')['verdict'] == 'deal'
    assert maddy.parse_reply('no json here') is None


def test_valid_reply_keeps_engine_vocabulary():
    clean, why = maddy.validate_reply(dict(GOOD))
    assert why is None and clean['verdict'] == 'deal' and clean['deal_badge'] == 'good_deal' and not clean['capped']


@pytest.mark.parametrize('patch', [{'verdict': 'great'}, {'deal_badge': 'amazing'}, {'homelens_score': 'x'},
                                   {'homelens_score': 140}, {'confidence': 'certain'}, {'reason': ''},
                                   {'verdict': 'strong_deal', 'deal_badge': 'overpriced'}])
def test_bad_replies_are_rejected_not_written(patch):
    clean, why = maddy.validate_reply({**GOOD, **patch})
    assert clean is None and why


def test_high_confidence_is_never_stored():
    clean, _ = maddy.validate_reply({**GOOD, 'confidence': 'high'})
    assert clean['confidence'] == 'medium'


def test_loudest_claims_are_capped():
    clean, _ = maddy.validate_reply({**GOOD, 'verdict': 'strong_deal', 'deal_badge': 'excellent_deal', 'homelens_score': 95})
    assert (clean['verdict'], clean['deal_badge'], clean['capped']) == ('deal', 'good_deal', True)
    assert clean['homelens_score'] <= maddy.SCORE_CAP_WHEN_CAPPED


def test_row_has_no_fabricated_statistics():
    clean, _ = maddy.validate_reply(dict(GOOD))
    row = maddy.build_signal_row(_l(1), clean, 'h', 'now')
    assert row['scored_by'] == 'maddy' and row['comparison_method'] == 'maddy_fallback'
    for k in ('gap_pct', 'gap_rands', 'gap_annual', 'z_score', 'median_price', 'median_price_per_m2', 'sample_size'):
        assert k not in row           # absent from the upsert = NULL


def test_hash_ignores_listing_age_but_not_price_or_prompt():
    v = maddy.prompt_version()
    a = _l(1)
    assert maddy.input_hash(a, v) == maddy.input_hash({**a, 'first_seen': '2026-01-01'}, v)
    assert maddy.input_hash(a, v) != maddy.input_hash({**a, 'price': 18_000}, v)
    assert maddy.input_hash(a, v) != maddy.input_hash(a, 'other')


def test_message_carries_the_facts_and_no_made_up_ones():
    msg = maddy.build_user_message(_l(1, bathrooms=None), NOW)
    assert 'City: Cape Town' in msg and 'R19 000 per month' in msg and 'Bathrooms: not stated' in msg
    assert 'Listed: 7 days ago' in msg and 'Floor size' not in msg


def test_work_selection_precedence_and_caching():
    v = maddy.prompt_version()
    ls = [_l(1), _l(2), _l(3), _l(4, price=None), _l(5, is_outlier=True), _l(6)]
    h6 = maddy.input_hash(ls[5], v)
    w = maddy.select_work(ls, engine_ids={'l1'}, maddy={'l2': 'stale', 'l6': h6}, version=v)
    assert w['engine'] == 1 and w['no_price'] == 1 and w['outlier'] == 1
    assert w['unchanged'] == 1                                    # l6: same hash, not re-sent
    assert {l['listing_id'] for l, _ in w['todo']} == {'l2', 'l3'}   # l2 changed, l3 new


def test_engine_row_wins_over_a_maddy_row_written_meanwhile():
    db = FakeDB([])
    db.tables['listing_signals'] = [{'listing_id': 'l1', 'scored_by': 'engine'},
                                    {'listing_id': 'l2', 'scored_by': 'maddy'}]
    assert maddy.drop_listings_with_engine_rows(db, ['l1', 'l2', 'l3']) == {'l1'}


class _Usage:
    input_tokens, output_tokens = 900, 120


class _Block:
    type = 'text'

    def __init__(self, t):
        self.text = t


class _Resp:
    usage, stop_reason = _Usage(), 'end_turn'

    def __init__(self, t):
        self.content = [_Block(t)]


class _AI:
    def __init__(self, reply):
        self.calls, self.reply = 0, reply
        self.messages = self

    def create(self, **kw):
        self.calls += 1
        assert kw['model'] == maddy.MODEL and 'You are Maddy' in kw['system']
        return _Resp(json.dumps(self.reply))


def _db(listings, signals=()):
    db = FakeDB(list(listings))
    db.tables['listing_signals'] = list(signals)
    return db


def test_full_pass_writes_only_unscored_and_is_idempotent():
    ls = [_l(1), _l(2), _l(3)]
    db = _db(ls, [{'listing_id': 'l1', 'scored_by': 'engine', 'input_hash': None}])
    ai = _AI(GOOD)
    rep = maddy.run_fallback(db, ai)
    assert (rep['engine_scored'], rep['maddy_scored'], rep['written']) == (1, 2, 2) and ai.calls == 2
    by = {r['listing_id']: r for r in db.tables['listing_signals']}
    assert by['l1']['scored_by'] == 'engine' and by['l2']['scored_by'] == 'maddy'
    assert by['l2']['city'] == ls[1]['city'] and by['l3']['city'] == ls[2]['city']
    again = maddy.run_fallback(db, ai)
    assert again['skipped_unchanged'] == 2 and again['maddy_scored'] == 0 and ai.calls == 2


def test_dry_run_calls_the_model_but_writes_nothing():
    db = _db([_l(1)])
    rep = maddy.run_fallback(db, _AI(GOOD), dry_run=True)
    assert rep['maddy_scored'] == 1 and rep['written'] == 0 and db.tables['listing_signals'] == []


def test_changed_listing_is_rescored_and_inactive_rows_are_removed():
    db = _db([_l(1)])
    ai = _AI(GOOD)
    maddy.run_fallback(db, ai)
    db.tables['listings'][0]['price'] = 15_000
    assert maddy.run_fallback(db, ai)['maddy_scored'] == 1
    db.tables['listings'].clear()
    maddy.run_fallback(db, ai)
    assert db.tables['listing_signals'] == []
