"""
Per-site checks against REAL saved pages (tests/fixtures/, captured
2026-09-23 with scripts/capture_fixtures.py).

Expected values come from an independent oracle: plain regexes over the raw
HTML, written separately from the scraper code, so a selector that silently
stops matching is caught rather than mirrored.

For each site:
  * costs  — the site's own selectors (SELECTORS block) on three real listing
             pages, incl. pages with no costs and pages with levy only /
             rates only. costs_source must be 'site' (or 'fallback' for the
             two sites whose costs only exist in free text).
  * costs  — the shared text fallback on the same pages' text must agree
             (proves the fallback works on real text, and that the site
             selectors are not the only thing producing the number).
  * status — the scraper's real _extract_cards on the real search page: how
             many cards come back under_offer / sold.
"""
from __future__ import annotations

import importlib
from collections import Counter

import pytest

from scraper.enrich import read_costs_from_page
from scraper.extractors import extract_costs_from_text

# {site: {detail_n: {'levy': x, 'rates': y}}}  — from the oracle regexes.
COSTS = {
    'abc_international': {1: {}, 2: {'levy': 3440.0, 'rates': 10500.0}, 3: {}},
    'century21':    {1: {'levy': 3000.0}, 2: {'levy': 2800.0, 'rates': 850.0}, 3: {'levy': 2000.0, 'rates': 500.0}},
    'chaseveritt':  {1: {}, 2: {'rates': 5444.0}, 3: {'levy': 4900.0, 'rates': 1800.0}},
    'dgproperties': {1: {'levy': 2913.0, 'rates': 2532.0}, 2: {'levy': 2418.0, 'rates': 2375.0}, 3: {}},
    'greeff':       {1: {'levy': 6220.88}, 2: {'levy': 7233.5, 'rates': 2119.63}, 3: {'levy': 3700.0}},
    'harcourts':    {1: {}, 2: {}, 3: {'rates': 1800.0}},
    'jawitz':       {1: {'rates': 1600.0}, 2: {'levy': 3569.77, 'rates': 2362.52}, 3: {'rates': 2521.0}},
    'justproperty': {1: {'levy': 2395.96, 'rates': 905.48}, 2: {'rates': 1595.98}, 3: {'levy': 5195.72, 'rates': 1000.0}},
    # lionlead 2: the page shows Monthly Levy R38,000 on a R31,995,000 apartment —
    # real, but above the spec's R25,000 levy cap, so it is discarded (see
    # test_levy_above_the_cap_is_discarded_real_page). Rates R14,800 is in bounds.
    'lionlead':     {1: {'rates': 8820.0}, 2: {'rates': 14800.0}, 3: {}},
    'pamgolding':   {1: {}, 2: {'rates': 4986.0, 'levy': 4851.0}, 3: {'rates': 14304.0}},
    'quay1':        {1: {'levy': 1544.0, 'rates': 300.0}, 2: {'levy': 1291.0, 'rates': 663.0}, 3: {'rates': 6100.0}},
    'rawson':       {1: {'rates': 6800.0}, 2: {}, 3: {'rates': 800.0}},
    'seeff':        {1: {'rates': 817.0}, 2: {'levy': 672.28, 'rates': 532.27}, 3: {'rates': 2400.0}},
    'sothebys':     {1: {'rates': 2643.57}, 2: {'levy': 6000.0}, 3: {'levy': 6000.0}},
    'tyson':        {1: {'levy': 2800.0, 'rates': 950.0}, 2: {'levy': 3700.0, 'rates': 860.0}, 3: {'rates': 1177.0}},
}
# Sites whose levy/rates exist only as free text (SELECTORS['text_only']).
TEXT_ONLY_COSTS = {
    'engelvolkers': {1: {'levy': 4692.0, 'rates': 1280.0}, 2: {}, 3: {}},
    'duarte':       {1: {}, 2: {}, 3: {}},
    # gumtree 3: real seller text "Rates And Taxes: 1800 / Levies: 2280" (bare numbers)
    'gumtree':      {1: {}, 2: {}, 3: {'levy': 2280.0, 'rates': 1800.0}},
}
# RE/MAX keeps them in the page's JSON payload (its own helper, not CSS rows).
REMAX_COSTS = {1: {}, 2: {'rates': 1273.47998}, 3: {'levy': 2325.320068, 'rates': 574.52002}}

# (under_offer, sold) cards expected back from the scraper's _extract_cards.
# Oracle = badge text counted in the raw HTML; a scraper's own filters (Pam
# Golding's Cape-Town suburb check, Duarte's validation …) may drop cards, so
# 'dropped' documents the difference instead of hiding it.
STATUS = {
    'century21':    (1, 0), 'chaseveritt': (2, 0), 'dgproperties': (1, 0), 'greeff': (4, 0),
    'harcourts':    (3, 0), 'justproperty': (3, 2), 'quay1': (3, 0), 'sothebys': (3, 0),
    'tyson':        (0, 1), 'seeff': (2, 0),
    'jawitz':       (1, 0), 'lionlead': (3, 0), 'abc_international': (0, 0),
    'rawson':       (0, 1), 'engelvolkers': (0, 0), 'gumtree': (0, 0),
}


def _module(site):
    return importlib.import_module(f'scraper.{site}')


def _expected(d: dict) -> tuple:
    return d.get('levy'), d.get('rates')


def _got(costs: dict) -> tuple:
    return costs['levy_monthly'], costs['rates_monthly']


def _approx(a, b):
    if a is None or b is None:
        return a is b
    return abs(a - b) < 0.01


# ── costs: the site's own selectors ───────────────────────────────────────────

CASES = [(s, n) for s, d in {**COSTS, **TEXT_ONLY_COSTS}.items() for n in d]


@pytest.mark.parametrize('site, n', CASES)
def test_site_selectors_read_costs(browser, site, n):
    expected = {**COSTS, **TEXT_ONLY_COSTS}[site][n]
    page = browser.with_fixture(f'{site}_detail_{n}.html')
    costs = browser.run(read_costs_from_page(page, _module(site).SELECTORS))
    assert all(_approx(g, e) for g, e in zip(_got(costs), _expected(expected))), (_got(costs), expected)
    assert costs['levies_rates_combined_monthly'] is None
    if expected:
        want = 'fallback' if site in TEXT_ONLY_COSTS else 'site'
        assert costs['costs_source'] == want
        assert costs['costs_raw_snippet']
    else:
        assert costs['costs_source'] is None and costs['costs_raw_snippet'] is None


@pytest.mark.parametrize('n', [1, 2, 3])
def test_remax_reads_costs_from_its_json_payload(browser, n):
    from scraper import remax
    html = (__import__('tests.conftest', fromlist=['FIXTURES']).FIXTURES / f'remax_detail_{n}.html').read_text(encoding='utf-8')
    text = browser.body_text(f'remax_detail_{n}.html')
    costs = remax._read_costs(html, text, None)
    expected = REMAX_COSTS[n]
    assert all(_approx(g, e) for g, e in zip(_got(costs), _expected(expected)))
    assert costs['costs_source'] == ('site' if expected else None)


def test_remax_special_levies_are_not_the_levy(browser):
    # detail 3 has "levy": 2325.32 AND "special_levies": 2267.52 — only the first counts
    from scraper import remax
    from tests.conftest import FIXTURES
    html = (FIXTURES / 'remax_detail_3.html').read_text(encoding='utf-8')
    assert 'special_levies' in html and '2267.52' in html
    costs = remax._read_costs(html, '', None)
    assert abs(costs['levy_monthly'] - 2325.32) < 0.01


def test_levy_above_the_cap_is_discarded_real_page(browser):
    # Real value R38,000 > R25,000 cap: dropped by design (spec bounds), on
    # both the site-selector path and the text-fallback path.
    page = browser.with_fixture('lionlead_detail_2.html')
    assert 'R38,000' in browser.body_text('lionlead_detail_2.html')
    costs = browser.run(read_costs_from_page(page, _module('lionlead').SELECTORS))
    assert costs['levy_monthly'] is None and costs['rates_monthly'] == 14800
    found = extract_costs_from_text(browser.body_text('lionlead_detail_2.html'), None)
    assert found['levy_monthly'] is None and found['rates_monthly'] == 14800


def test_duarte_calculator_default_is_not_a_levy(browser):
    # Duarte's bond calculator prints "Monthly HOA Fees R250 00" on every page.
    # Without text_exclude the fallback reads it as a R250 levy.
    text = browser.body_text('duarte_detail_1.html')
    assert 'Monthly HOA Fees' in text                       # the trap is really there
    assert extract_costs_from_text(text, None)['levy_monthly'] == 250   # unprotected fallback falls for it
    page = browser.with_fixture('duarte_detail_1.html')
    costs = browser.run(read_costs_from_page(page, _module('duarte').SELECTORS))
    assert costs['levy_monthly'] is None                    # protected by text_exclude


# ── costs: the shared fallback agrees on the same real pages ──────────────────

@pytest.mark.parametrize('site, n', [(s, n) for s, d in COSTS.items() for n in d])
def test_fallback_agrees_with_site_selectors_on_real_text(browser, site, n):
    expected = COSTS[site][n]
    found = extract_costs_from_text(browser.body_text(f'{site}_detail_{n}.html'), None)
    assert all(_approx(g, e) for g, e in zip(_got(found), _expected(expected))), (site, n, found)
    assert found['levies_rates_combined_monthly'] is None


# ── status: real search pages through the scraper's real _extract_cards ──────

@pytest.mark.parametrize('site', sorted(STATUS))
def test_status_from_real_cards(browser, site):
    page = browser.with_fixture(f'{site}_search.html')
    cards = browser.run(_module(site)._extract_cards(page, 'sale'))
    assert cards, f'{site}: no cards extracted from the saved search page'
    counts = Counter(c['listing_status'] for c in cards)
    assert set(counts) <= {'live', 'under_offer', 'sold'}
    assert (counts['under_offer'], counts['sold']) == STATUS[site], counts
    assert all(c.get('status_source') in ('site', 'fallback', 'default') for c in cards)
    # cards gain exactly the two status keys — costs are added later, from the listing page
    assert all({'listing_status', 'status_source'} <= set(c) for c in cards)
    assert not any(k in c for c in cards for k in ('levy_monthly', 'rates_monthly', 'costs_source', 'costs'))
    # a non-live status must come from a real badge, not a guess
    assert all(c['status_source'] == 'site' for c in cards if c['listing_status'] != 'live')
