"""
Shared fallback extractors (scraper/extractors.py).

Cases requested in the spec, using REAL saved pages wherever one exists:
  costs  label above value ("Monthly Levy" / "R2,800")     century21 detail
         Total row that must NOT be picked up               century21 detail
         description line with levies + rates               engelvolkers detail
         page with no cost info                             chaseveritt detail
  status under-offer badge                                   quay1 search card
         sold badge                                          tyson search card
         "sold out" in a description                         must stay 'live'
No saved page contains an annual ("p.a.") figure or a combined
"Levies & Rates: R3 267" line, so those two use the spec's own text.
"""
from __future__ import annotations

import pytest

from scraper.extractors import (
    classify_cost_label, extract_costs_from_text, extract_status_from_text, parse_money,
)


# ── costs ─────────────────────────────────────────────────────────────────────

def test_label_above_value_real_page(browser):
    # Jawitz renders the label on one line and the amount on the next.
    text = browser.body_text('jawitz_detail_2.html')
    assert 'Monthly Levy\nR3,569.77' in text
    found = extract_costs_from_text(text, None)
    assert found['levy_monthly'] == 3569.77
    assert found['rates_monthly'] == 2362.52
    assert found['levies_rates_combined_monthly'] is None
    assert found['matched_in'] == 'page'
    assert 'Monthly Levy' in found['raw_snippet'] and 'R3,569.77' in found['raw_snippet']


def test_label_and_value_on_one_line_real_page(browser):
    text = browser.body_text('century21_detail_2.html')
    assert 'Monthly Levy R2,800\nMonthly Rates R850' in text
    found = extract_costs_from_text(text, None)
    assert (found['levy_monthly'], found['rates_monthly']) == (2800, 850)


def test_total_row_is_never_used_real_page(browser):
    text = browser.body_text('century21_detail_2.html')
    assert 'Total R3,650' in text                   # the page really has a Total row
    found = extract_costs_from_text(text, None)
    assert 3650 not in (found['levy_monthly'], found['rates_monthly'],
                        found['levies_rates_combined_monthly'])
    # ... and a Total row on its own never becomes a levy or rates figure
    only_total = 'Monthly Levy\nTotal R3,650'
    assert extract_costs_from_text(only_total, None)['levy_monthly'] is None


def test_total_only_gives_nothing():
    assert extract_costs_from_text('Levy\n\nTotal\nR2 900', None)['levy_monthly'] is None
    assert extract_costs_from_text('Total levies and rates R5000', None)['levies_rates_combined_monthly'] is None


def test_transfer_calculator_rows_are_ignored_real_page(browser):
    # PropData pages carry a bond/transfer calculator: "Deeds Office Levy",
    # "Rates/Levy Clearance", "Pro-Rata Rates/Levies" — not the property's costs.
    # It is hidden (innerText omits it) but textContent includes it.
    text = browser.body_textcontent('jawitz_detail_1.html')
    assert 'Deeds Office Levy' in text and 'Pro-Rata Rates/Levies' in text
    found = extract_costs_from_text(text, None)
    assert found['levy_monthly'] is None            # page shows only Monthly Rates
    assert found['rates_monthly'] == 1600
    assert found['levies_rates_combined_monthly'] is None


def test_calculator_rows_never_leak_even_when_populated():
    text = ('Deeds Office Levy\nR 4 500\nRates/Levy Clearance\nR 3 000\n'
            'Pro-Rata Rates/Levies\nR 2 000\nMonthly Levy\nR3,000')
    found = extract_costs_from_text(text, None)
    assert found['levy_monthly'] == 3000 and found['rates_monthly'] is None
    assert found['levies_rates_combined_monthly'] is None


def test_description_line_real_page(browser):
    text = browser.body_text('engelvolkers_detail_1.html')
    found = extract_costs_from_text(text, None)
    assert (found['levy_monthly'], found['rates_monthly']) == (4692, 1280)
    assert 'Levies R4,692.00' in found['raw_snippet']


def test_description_only_is_tagged_description():
    found = extract_costs_from_text(None, 'Sunny flat. Levies: R2 267 pm, Rates: R1 000 pm')
    assert (found['levy_monthly'], found['rates_monthly']) == (2267, 1000)
    assert found['matched_in'] == 'description'


def test_page_text_tried_before_description():
    found = extract_costs_from_text('Monthly Levy R1 000', 'Levies: R2 267 pm')
    assert found['levy_monthly'] == 1000 and found['matched_in'] == 'page'


def test_no_cost_info_real_page(browser):
    found = extract_costs_from_text(browser.body_text('chaseveritt_detail_1.html'), None)
    assert found['levy_monthly'] is None and found['rates_monthly'] is None
    assert found['levies_rates_combined_monthly'] is None
    assert found['raw_snippet'] is None and found['matched_in'] is None


def test_annual_amount_is_divided_by_12():
    found = extract_costs_from_text('Rates: R12 000 per annum', None)
    assert found['rates_monthly'] == 1000
    assert extract_costs_from_text('Levy R24,000 p.a.', None)['levy_monthly'] == 2000
    assert extract_costs_from_text('Rates R9 600 annually', None)['rates_monthly'] == 800


def test_explicit_monthly_beats_a_later_yearly():
    found = extract_costs_from_text('Levies: R2 267 pm, escalating yearly', None)
    assert found['levy_monthly'] == 2267


def test_combined_figure_is_not_split():
    found = extract_costs_from_text('Levies & Rates: R3 267', None)
    assert found['levies_rates_combined_monthly'] == 3267
    assert found['levy_monthly'] is None and found['rates_monthly'] is None
    for wording in ('Levies and Rates R3 267', 'Levy + Rates: R3,267', 'Rates & Levies R3267'):
        got = extract_costs_from_text(wording, None)
        assert got['levies_rates_combined_monthly'] == 3267, wording
        assert got['levy_monthly'] is None and got['rates_monthly'] is None, wording


@pytest.mark.parametrize('text, expected', [
    ('Monthly Levy: R1,000', 1000),
    ('Monthly Levy: R1 000', 1000),
    ('Monthly Levy: R 1000.00', 1000),
    ('Monthly Levy: 1000', 1000),
    ('Monthly Levy: R1 000', 1000),          # non-breaking space
    ('Monthly Levy: R1 000', 1000),          # thin space
    ('Monthly Levy: R1 000', 1000),          # narrow no-break space
    ('Monthly Levy: R 1 500,50 per month', 1500.5),
])
def test_amount_formats(text, expected):
    assert extract_costs_from_text(text, None)['levy_monthly'] == expected


@pytest.mark.parametrize('label', [
    'levy', 'Levies', 'Monthly Levy', 'Body Corporate', 'body corp', 'HOA fee', 'Sectional Title Levy',
])
def test_levy_labels(label):
    assert extract_costs_from_text(f'{label}: R2 000', None)['levy_monthly'] == 2000


@pytest.mark.parametrize('label', [
    'Rates', 'Monthly Rates', 'Municipal Rates', 'Rates and Taxes', 'Rates & Taxes',
    'Council Rates', 'Rates and Refuse',
])
def test_rates_labels(label):
    assert extract_costs_from_text(f'{label}: R900', None)['rates_monthly'] == 900


def test_amount_must_be_within_60_chars_of_label():
    assert extract_costs_from_text('Levy ' + 'x' * 70 + ' R2 000', None)['levy_monthly'] is None
    assert extract_costs_from_text('Levy ' + 'x' * 40 + ' R2 000', None)['levy_monthly'] == 2000


@pytest.mark.parametrize('text, key', [
    ('Levy: R100', 'levy_monthly'),            # below R150
    ('Levy: R30 000', 'levy_monthly'),         # above R25,000
    ('Rates: R40', 'rates_monthly'),           # below R50
    ('Rates: R25 000', 'rates_monthly'),       # above R20,000
    ('Levies & Rates: R100', 'levies_rates_combined_monthly'),
])
def test_out_of_bounds_discarded(text, key):
    assert extract_costs_from_text(text, None)[key] is None


def test_bounds_edges_are_kept():
    assert extract_costs_from_text('Levy: R150', None)['levy_monthly'] == 150
    assert extract_costs_from_text('Levy: R25,000', None)['levy_monthly'] == 25000
    assert extract_costs_from_text('Rates: R50', None)['rates_monthly'] == 50
    assert extract_costs_from_text('Rates: R20 000', None)['rates_monthly'] == 20000


def test_year_is_not_an_amount():
    found = extract_costs_from_text('Levy: 1000\nRates 2023 valuation R 850 pm', None)
    assert (found['levy_monthly'], found['rates_monthly']) == (1000, 850)


@pytest.mark.parametrize('text', [
    'Exchange rates apply. Interest rates from 10.5%. Sam Levys Village.',
    'Prime rates R 5 000 000 bond',
])
def test_marketing_and_market_text_is_ignored(text):
    found = extract_costs_from_text(text, None)
    assert found['levy_monthly'] is None and found['rates_monthly'] is None


def test_raw_snippet_is_short_and_from_the_source_text():
    found = extract_costs_from_text('x ' * 300 + 'Monthly Levy: R2 267 pm ' + 'y ' * 300, None)
    assert found['raw_snippet'] == 'Monthly Levy: R2 267 pm'
    assert len(found['raw_snippet']) <= 240


@pytest.mark.parametrize('label, kind', [
    ('Monthly Levy', 'levy'), ('Levies:', 'levy'), ('Monthly Rates', 'rates'),
    ('Rates and Taxes: ', 'rates'), ('Levies & Rates', 'combined'),
    ('Total', None), ('Monthly Repayment', None), ('Deeds Office Levy', None),
    ('Rates/Levy Clearance', None), ('Floor size', None),
])
def test_classify_cost_label(label, kind):
    assert classify_cost_label(label) == kind


def test_parse_money():
    assert parse_money('R6,220.88') == 6220.88
    assert parse_money('R 4,986 per month') == 4986
    assert parse_money('R12 000 p.a.') == 1000
    assert parse_money('') is None and parse_money(None) is None


# ── status ────────────────────────────────────────────────────────────────────

def test_under_offer_badge_real_card(browser):   # greeff card (quay1 has no badges since its Oct 2026 rebuild)
    from tests.conftest import FIXTURES
    page = browser.load((FIXTURES / 'greeff_search.html').read_text(encoding='utf-8'))
    badges = browser.run(page.eval_on_selector_all(
        '.card-badges .card-badge', 'els => els.map(e => e.innerText.trim())'))
    assert 'Under offer' in badges                   # real badge on a real card
    assert extract_status_from_text('', badges) == 'under_offer'
    assert extract_status_from_text('', ['New']) == 'live'


def test_sold_badge_real_card(browser):
    from tests.conftest import FIXTURES
    page = browser.load((FIXTURES / 'tyson_search.html').read_text(encoding='utf-8'))
    badges = browser.run(page.eval_on_selector_all(
        '.card-badges .card-badge', 'els => els.map(e => e.innerText.trim())'))
    assert 'Sold' in badges
    assert extract_status_from_text('', badges) == 'sold'


@pytest.mark.parametrize('description', [
    'Priced to sell — the last unit sold out in days!',
    'Sold out in 3 days at the last launch. Act fast.',
    'Our previous listing sold in 3 days.',
    'Everything sold out and then some.',
    'Not under offer yet, come and view.',
])
def test_marketing_sentences_stay_live(description):
    assert extract_status_from_text(description) == 'live'
    assert extract_status_from_text('Lovely flat\n' + description + '\nR1 500 000') == 'live'
    assert extract_status_from_text('', [description]) == 'live'


@pytest.mark.parametrize('phrase, expected', [
    ('under offer', 'under_offer'), ('UNDER OFFER', 'under_offer'), ('Offer Pending', 'under_offer'),
    ('sale agreed', 'under_offer'), ('Under Contract', 'under_offer'),
    ('sold', 'sold'), ('SOLD', 'sold'), ('Sold STC', 'sold'), ('sold stc', 'sold'),
])
def test_all_phrases_as_badge_or_standalone_line(phrase, expected):
    assert extract_status_from_text('', [phrase]) == expected
    assert extract_status_from_text(f'3 Bed Apartment\n{phrase}\nR2 000 000') == expected
    assert extract_status_from_text(f'3 Bed Apartment\n  {phrase}!  \nR2 000 000') == expected


def test_default_is_live_and_sold_beats_under_offer():
    assert extract_status_from_text('') == 'live'
    assert extract_status_from_text(None) == 'live'
    assert extract_status_from_text('Under offer\nSold') == 'sold'


def test_word_boundaries():
    assert extract_status_from_text('', ['Sold-out']) == 'live'
    assert extract_status_from_text('', ['Resold']) == 'live'
    assert extract_status_from_text('', ['Unsold']) == 'live'
