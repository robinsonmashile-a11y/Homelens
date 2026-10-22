"""
Detail-page extraction (scraper/detail.py + scraper/detail_page.py).

Pure-parser cases, then the shared reader over REAL saved pages in
tests/fixtures/. Expected numbers were read off the pages themselves
(plain-text search of each saved page), not produced by the code under test.
"""
from __future__ import annotations

import importlib

import pytest

from scraper.detail import DETAIL_FIELDS, parse_detail_fields
from scraper.detail_page import apply_details, read_detail_from_page


# ── parser: pure ──────────────────────────────────────────────────────────────

def test_every_field_is_present_and_none_when_nothing_found():
    out = parse_detail_fields([], {}, None, None)
    assert set(out) == set(DETAIL_FIELDS) and all(v is None for v in out.values())


def test_label_rows_and_money_normalisation():
    out = parse_detail_fields([('Floor size', '79.00 m²'), ('Land size', '4,523.00 m²'),
                               ('Monthly Levy', 'R 6,220.88'), ('Monthly Rates', 'R2 119.63')], {}, None)
    assert (out['size_m2'], out['erf_size']) == (79.0, 4523.0)
    assert (out['levy_monthly'], out['rates_monthly']) == (6220.88, 2119.63)


def test_erf_is_never_used_as_floor_size():
    out = parse_detail_fields([('Land size', '600.00 m²')], {}, None, 'Land size 600.00 m²')
    assert out['size_m2'] is None and out['erf_size'] == 600.0


def test_vague_size_equal_to_erf_is_the_plot_not_the_floor():
    out = parse_detail_fields([], {}, None, 'Size (sqm):523\nLand size: 523m2')
    assert out['size_m2'] is None and out['erf_size'] == 523.0


def test_sectional_unit_with_equal_land_and_floor_size_keeps_both():
    out = parse_detail_fields([], {}, None, 'Land size 50.00 m²\nFloor size 50.00 m²')
    assert out['size_m2'] == 50.0 and out['erf_size'] == 50.0


@pytest.mark.parametrize('text,expected', [
    ('2 bedrooms2 bathroomsBuilding 81 m² (on 103 m² Erf)', 81.0),    # glued card text
    ('~65 m² Living area', 65.0),
    ('Unit size: 87sqm, Garage; 41sqm.', 87.0),
    ('an impressive 405m² floor area', 405.0),
])
def test_floor_size_text_patterns(text, expected):
    assert parse_detail_fields([], {}, None, text)['size_m2'] == expected


def test_parking_chips_are_summed():
    out = parse_detail_fields([], {'Exterior': ['4 Garages', '15 Parkings', 'Pool']}, None)
    assert out['parking'] == 19


def test_flags_are_true_only_when_stated_never_inferred_false():
    none = parse_detail_fields([], {}, 'A lovely home near the beach.')
    assert none['pet_friendly'] is None and none['is_furnished'] is None
    yes = parse_detail_fields([], {'Exterior': ['Pet Friendly'], 'Interior': ['Furnished']}, None)
    assert yes['pet_friendly'] is True and yes['is_furnished'] is True
    no = parse_detail_fields([], {'Interior': ['Unfurnished']}, 'No pets allowed.')
    assert no['pet_friendly'] is False and no['is_furnished'] is False


def test_security_and_views_from_description():
    out = parse_detail_fields([], {}, 'Secure complex with 24-hour security and sea views from the lounge.')
    assert '24-hour security' in out['security'].lower() and 'sea views' in out['views'].lower()


# ── shared reader over real pages ─────────────────────────────────────────────

def _read(browser, site, n):
    sel = importlib.import_module(f'scraper.{site}').SELECTORS
    page = browser.with_fixture(f'{site}_detail_{n}.html')
    return browser.run(read_detail_from_page(page, sel))


SITES = ['abc_international', 'century21', 'chaseveritt', 'dgproperties', 'duarte', 'engelvolkers',
         'greeff', 'gumtree', 'harcourts', 'jawitz', 'justproperty', 'lionlead', 'quay1',
         'rawson', 'remax', 'seeff', 'sothebys', 'tyson']


@pytest.mark.parametrize('site', SITES)
def test_description_found_on_every_saved_page(browser, site):
    for n in (1, 2, 3):
        d = _read(browser, site, n)['description']
        assert d and len(d) >= 40, f'{site} detail_{n}: no description'


# (site, detail_n) -> floor size as printed on the page
FLOOR = {
    ('greeff', 1): 79.0, ('greeff', 2): 118.0, ('greeff', 3): 71.0,
    ('chaseveritt', 1): 270.0, ('chaseveritt', 3): 100.0,
    ('harcourts', 3): 195.0, ('seeff', 1): 146.0, ('seeff', 3): 167.0,
    ('sothebys', 1): 75.0, ('justproperty', 1): 50.0, ('justproperty', 2): 59.0,
    ('rawson', 1): 430.0, ('engelvolkers', 1): 65.0, ('engelvolkers', 2): 108.0,
}
# Pages that show only a land/erf size: floor size must stay None.
LAND_ONLY = [('harcourts', 1), ('harcourts', 2), ('sothebys', 2), ('sothebys', 3), ('rawson', 2), ('rawson', 3)]


@pytest.mark.parametrize('key,expected', FLOOR.items())
def test_floor_size_on_real_pages(browser, key, expected):
    assert _read(browser, *key)['size_m2'] == expected


@pytest.mark.parametrize('key', LAND_ONLY)
def test_land_only_pages_leave_floor_size_null(browser, key):
    out = _read(browser, *key)
    assert out['size_m2'] is None and out['erf_size']


def test_greeff_chips_parking_security_views(browser):
    out = _read(browser, 'greeff', 1)
    assert out['parking'] == 1 and out['is_furnished'] is True
    assert 'security' in out['security'].lower() and 'sea' in out['views'].lower()
    assert out['levy_monthly'] == 6220.88


def test_apply_details_keeps_card_values_and_longer_description():
    card = {'size_m2': 60.0, 'bedrooms': 3, 'description': 'x' * 300}
    apply_details(card, {'size_m2': 79.0, 'bedrooms': 2, 'bathrooms': 1.5, 'description': 'short but long enough ' * 3,
                         'parking': 2, 'levy_monthly': 999.0})
    assert card['size_m2'] == 60.0 and card['bedrooms'] == 3 and card['bathrooms'] == 1.5
    assert len(card['description']) == 300 and card['parking'] == 2
    assert 'levy_monthly' not in card and card['details_checked_at']


# ── RE/MAX: the page payload is an escaped JSON string ────────────────────────

@pytest.mark.parametrize('n,floor,erf,garages', [(1, 88.0, None, 1), (2, 124.0, 444.0, 1), (3, 74.0, 4222.0, None)])
def test_remax_payload_size_erf_parking(browser, monkeypatch, n, floor, erf, garages):
    from scraper import remax
    monkeypatch.setattr(remax.asyncio, 'sleep', lambda *_: _noop())
    page = browser.with_fixture(f'remax_detail_{n}.html')
    out = browser.run(remax._extract_detail(page, 'rent'))
    assert out.get('size_m2') == floor and out.get('erf_size') == erf and out.get('parking') == garages


async def _noop():
    return None
