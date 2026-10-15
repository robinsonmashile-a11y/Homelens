"""CostEnricher skip rules, status resolution, eXp API mapping, run report."""
from __future__ import annotations

import asyncio
import json

import pytest

from scraper import enrich, exprealty
from scraper.enrich import CostEnricher, apply_costs, resolve_status
from tests.conftest import FIXTURES



# ── resolve_status ────────────────────────────────────────────────────────────

def test_site_badge_wins_and_is_tagged_site():
    assert resolve_status(['New', 'Under offer'], 'card text', {}) == ('under_offer', 'site')
    assert resolve_status(['Sold'], '', {}) == ('sold', 'site')


def test_site_specific_wording_via_status_map():
    assert resolve_status(['Offer Made'], '', {'status_map': {'offer made': 'under_offer'}}) == ('under_offer', 'site')
    assert resolve_status(['Offer Made'], '', {}) == ('live', 'default')      # unmapped wording is not guessed


def test_fallback_on_card_text_is_tagged_fallback():
    assert resolve_status([], 'Flat\nSOLD\nR1 000 000', {}) == ('sold', 'fallback')


def test_nothing_found_is_live_default():
    assert resolve_status(['New', 'Sole Mandate'], 'Flat\nR1 000 000', {}) == ('live', 'default')


def test_sold_out_in_a_real_card_stays_live(browser):
    # take a real card, add a marketing sentence, keep its real badges
    from scraper import quay1
    html = (FIXTURES / 'quay1_search.html').read_text(encoding='utf-8')
    page = browser.load(html)
    browser.run(page.evaluate("""() => {
        const p = document.createElement('p');
        p.textContent = 'Our last three units sold out in 3 days!';
        [...document.querySelectorAll('a[href^="/for-sale/"]')]
            .find(a => a.querySelector('p.font-serif')).appendChild(p);
    }"""))
    cards = browser.run(quay1._extract_cards(page, 'sale'))
    first = cards[0]
    assert first['listing_status'] == 'live'      # marketing text must not flip status


# ── CostEnricher ──────────────────────────────────────────────────────────────

class StubPage:
    def __init__(self):
        self.visited = []

    async def goto(self, url, **_):
        self.visited.append(url)


def make_enricher(monkeypatch, checked=(), fail_on=None, costs=None):
    e = CostEnricher('greeff', owner=None, selectors={})
    e.page = StubPage()
    e.checked = set(checked)

    async def fake_read(page, sel):
        if fail_on and page.visited[-1] == fail_on:
            raise RuntimeError('timeout')
        return costs or {'levy_monthly': 2267.0, 'rates_monthly': None,
                         'levies_rates_combined_monthly': None,
                         'costs_source': 'site', 'costs_raw_snippet': 'Monthly Levy: R2,267'}

    monkeypatch.setattr(enrich, 'read_costs_from_page', fake_read)
    monkeypatch.setattr(enrich, 'DETAIL_SETTLE_SEC', 0)
    monkeypatch.setattr(enrich, 'DETAIL_DELAY', (0, 0))
    return e


def listing(**over):
    base = {'listing_id': 'a1', 'listing_url': 'https://x/1', 'listing_type': 'sale'}
    base.update(over)
    return base


def test_new_sale_listing_is_visited_and_gets_costs(monkeypatch):
    e = make_enricher(monkeypatch)
    out = asyncio.run(e.enrich(listing()))
    assert e.page.visited == ['https://x/1']
    assert out['levy_monthly'] == 2267.0 and out['costs_source'] == 'site'
    assert out['costs_checked_at']


def test_rentals_are_never_visited_for_costs(monkeypatch):
    e = make_enricher(monkeypatch)
    out = asyncio.run(e.enrich(listing(listing_type='rent')))
    assert e.page.visited == [] and 'levy_monthly' not in out


def test_recently_checked_listing_is_skipped(monkeypatch):
    e = make_enricher(monkeypatch, checked={'a1'})
    out = asyncio.run(e.enrich(listing()))
    assert e.page.visited == [] and 'levy_monthly' not in out


def test_failed_visit_is_not_marked_checked_so_it_is_retried(monkeypatch):
    e = make_enricher(monkeypatch, fail_on='https://x/1')
    out = asyncio.run(e.enrich(listing()))
    assert 'costs_checked_at' not in out and 'levy_monthly' not in out
    assert e.visited == 0


def test_visit_that_finds_nothing_is_still_marked_checked(monkeypatch):
    empty = {'levy_monthly': None, 'rates_monthly': None, 'levies_rates_combined_monthly': None,
             'costs_source': None, 'costs_raw_snippet': None}
    e = make_enricher(monkeypatch, costs=empty)
    out = asyncio.run(e.enrich(listing()))
    assert out['costs_checked_at'] and out['levy_monthly'] is None      # not re-visited for 30 days


def test_apply_costs_stamps_checked_at():
    d = {}
    apply_costs(d, {'levy_monthly': 1.0})
    assert d['levy_monthly'] == 1.0 and d['costs_checked_at'] and d['rates_monthly'] is None


# ── eXp (API, no browser) ─────────────────────────────────────────────────────

def _records():
    recs = json.loads((FIXTURES / 'exprealty_api.json').read_text())
    for r in recs:
        r['address'] = 'Sea Point, Cape Town, Western Cape'   # fixture was captured nation-wide
    return recs


def test_exp_real_records_map_status_and_costs():
    mapped = [exprealty._map_record(r, 'sale') for r in _records()]
    assert len(mapped) == 24 and all(mapped)
    assert {m['listing_status'] for m in mapped} == {'live'}
    assert {m['status_source'] for m in mapped} == {'site'}
    # the API's own levy/rates are null on every record; 2 descriptions carry costs
    assert sorted(m['costs_source'] for m in mapped if m['costs_source']) == ['description', 'description']
    with_costs = [m for m in mapped if m['costs_source']]
    for m in with_costs:
        assert m['levy_monthly'] or m['rates_monthly'] or m['levies_rates_combined_monthly']
        assert m['costs_raw_snippet']


def test_exp_api_fields_beat_the_description():
    rec = _records()[0]
    rec['levy'], rec['rates'] = 1500, 'R 700'
    rec['description'] = 'Levies: R9 999 pm'
    out = exprealty._map_record(rec, 'sale')
    assert (out['levy_monthly'], out['rates_monthly'], out['costs_source']) == (1500.0, 700.0, 'site')


def test_exp_levy_typed_at_the_end_of_a_long_description_is_found():
    rec = _records()[0]
    rec['description'] = 'Lovely home. ' * 120 + 'Levies: R2 267 pm, Rates: R1 000 pm'
    out = exprealty._map_record(rec, 'sale')
    assert (out['levy_monthly'], out['rates_monthly']) == (2267.0, 1000.0)
    assert len(out['description']) == 1000            # stored description is still cut


def test_exp_rentals_get_no_costs():
    out = exprealty._map_record(_records()[0], 'rent')
    assert 'levy_monthly' not in out and 'costs_checked_at' not in out
    assert out['listing_status'] == 'live'


def test_exp_unknown_status_code_falls_back_to_text_not_a_guess():
    rec = _records()[0]
    rec['status'], rec['title'] = '9', 'SOLD'
    assert exprealty._map_record(rec, 'sale')['listing_status'] == 'sold'
    rec['title'] = 'Nice house'
    out = exprealty._map_record(rec, 'sale')
    assert (out['listing_status'], out['status_source']) == ('live', 'default')


# ── run report ────────────────────────────────────────────────────────────────



def _report():
    import sys
    from tests.conftest import ROOT
    sys.path.insert(0, str(ROOT / 'scripts'))
    import run_report
    return run_report


def row(source, ltype='sale', status='live', levy=None, rates=None, comb=None, src=None):
    return {'source': source, 'listing_type': ltype, 'listing_status': status,
            'levy_monthly': levy, 'rates_monthly': rates,
            'levies_rates_combined_monthly': comb, 'costs_source': src}


def test_report_percentages_are_over_sale_listings_only():
    rr = _report()
    rows = ([row('greeff', levy=2000.0, rates=900.0, src='site')] * 6
            + [row('greeff', levy=2000.0, src='site')] * 2
            + [row('greeff')] * 2
            + [row('greeff', ltype='rent')] * 30)           # rentals must not dilute the %
    s = rr.summarise(rows)['greeff']
    assert (s['scraped'], s['sales']) == (40, 10)
    assert (s['levy_pct'], s['rates_pct']) == (80, 60)
    assert (s['site_pct'], s['fallback_pct']) == (100, 0)


def test_report_status_counts_cover_every_listing():
    rr = _report()
    rows = [row('a', status='live')] * 5 + [row('a', status='under_offer')] * 2 + [row('a', ltype='rent', status='sold')]
    assert rr.summarise(rows)['a']['status'] == {'live': 5, 'under_offer': 2, 'sold': 1}


def test_report_flags_a_site_whose_costs_are_mostly_fallback():
    rr = _report()
    rows = [row('greeff', levy=2000.0, src='fallback')] * 8 + [row('greeff', levy=2000.0, src='site')] * 2
    s = rr.summarise(rows)['greeff']
    assert s['fallback_pct'] == 80 and any('SELECTORS BROKEN' in f for f in s['flags'])
    assert 'greeff' in rr.format_report({'greeff': s}) and 'NEEDS ATTENTION' in rr.format_report({'greeff': s})


def test_report_does_not_flag_healthy_or_tiny_samples():
    rr = _report()
    healthy = [row('a', levy=2000.0, src='site')] * 9 + [row('a', levy=2000.0, src='fallback')]
    tiny = [row('b', levy=2000.0, src='fallback')] * 3          # < 5 with costs: too few to judge
    out = rr.summarise(healthy + tiny)
    assert out['a']['flags'] == [] and out['b']['flags'] == []


def test_report_exempts_text_only_sites():
    rr = _report()
    rows = [row('engelvolkers', levy=2000.0, src='fallback')] * 10
    assert rr.summarise(rows, text_only={'engelvolkers'})['engelvolkers']['flags'] == []
    assert rr.summarise(rows)['engelvolkers']['flags']          # ...but flagged if not exempt


def test_report_text_only_sources_come_from_the_selector_blocks():
    assert _report().text_only_sources() == {'engelvolkers', 'duarte', 'gumtree'}


def test_report_flags_a_site_with_no_costs_at_all():
    rr = _report()
    s = rr.summarise([row('x')] * 25)['x']
    assert any('NO COSTS' in f for f in s['flags'])
    assert rr.summarise([row('x')] * 5)['x']['flags'] == []     # too few sales to say


def test_row_snippets_are_tidy():
    assert enrich._row_snippet('Rates and Taxes:\xa0', 'R 4,986 per month') == 'Rates and Taxes: R 4,986 per month'
    assert enrich._row_snippet('Monthly Levy', 'Monthly Levy\nR3,569.77') == 'Monthly Levy: R3,569.77'
    assert enrich._row_snippet('Rates R6,800 per month', 'Rates R6,800 per month') == 'Rates R6,800 per month'
    assert enrich._row_snippet('Monthly Levy', 'R2,800') == 'Monthly Levy: R2,800'
