"""fix_suburbs: every city uses its own dictionary and policy; nothing is matched across cities."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import fix_suburbs as fs  # noqa: E402


# ── Cape Town: behaviour unchanged ────────────────────────────────────────────

@pytest.mark.parametrize('text', ['2 Bedroom Apartment For Sale in Sea Point', 'Gardens, Cape Town',
                                  'lovely flat in woodstock', 'nothing recognisable here'])
def test_cape_town_default_matches_the_explicit_city(text):
    assert fs.find_suburb_in_text(text) == fs.find_suburb_in_text(text, 'cape-town')


def test_cape_town_finds_its_suburbs_and_never_a_johannesburg_one():
    assert fs.find_suburb_in_text('Flat in Sea Point', 'cape-town') == 'sea-point'
    assert fs.find_suburb_in_text('House in Bryanston', 'cape-town') is None


# ── Johannesburg ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize('text,slug', [
    ('2 Bed Apartment Furnished For Rent in Illovo', 'illovo'),
    ('3 Bedroom House For Sale in Orange Grove', 'orange-grove'),
    ('Morden 1 bedroom Hyde Park house', 'hyde-park'),
    ('Fully Furnished Luxury Apartment To Rent at One Rosebank, Rosebank', 'rosebank'),
    ('34 Nephin Road, South Hills, Johannesburg', 'south-hills'),
    ('3 Bedroom house for sale in Lenasia Ext 10, Johannesburg', 'lenasia-ext-10'),
    ('Stunning Semi-Detached 4 Bedroom Home for sale in Bezuidenhout Valley', 'bezuidenhout-valley'),
])
def test_johannesburg_titles(text, slug):
    assert fs.find_suburb_in_text(text, 'johannesburg') == slug


def test_longest_name_wins_and_names_match_whole_words_only():
    assert fs.find_suburb_in_text('Flat in Bryanston East', 'johannesburg') == 'bryanston-east'
    assert fs.find_suburb_in_text('Flat in Crowns', 'johannesburg') != 'crown'      # 'crown' inside 'crowns'


def test_johannesburg_never_matches_a_cape_town_suburb():
    assert fs.find_suburb_in_text('Flat in Sea Point', 'johannesburg') is None


def test_unknown_city_is_never_guessed():
    assert fs.find_suburb_in_text('Flat in Sea Point', 'durban') is None


def test_same_name_resolves_inside_each_city_without_crossing():
    assert fs.find_suburb_in_text('Rosebank', 'johannesburg') == 'rosebank'
    assert fs.find_suburb_in_text('Rosebank', 'cape-town') == fs.find_suburb_in_text('Rosebank')


# ── policy and wrong-city detection ───────────────────────────────────────────

def test_johannesburg_policy_never_overwrites_or_hides():
    assert fs.CITY_POLICY['johannesburg'] == {'overwrite_suburb': False, 'deactivate_unresolved': False}
    assert fs.CITY_POLICY['cape-town'] == {'overwrite_suburb': True, 'deactivate_unresolved': True}


GREEFF = 'https://www.greeff.co.za/results/residential/for-sale/{}/{}/house/3436745/x/'


@pytest.mark.parametrize('source,town,city,expected', [
    ('greeff', 'hermanus', 'johannesburg', True),        # Western Cape town tagged Johannesburg
    ('greeff', 'cape-town', 'cape-town', False),
    ('seeff', 'johannesburg', 'johannesburg', False),
    ('dgproperties', 'hout-bay', 'johannesburg', True),
    ('quay1', 'woodstock', 'cape-town', False),          # not a platform-URL site: never flagged
])
def test_wrong_city_detection(source, town, city, expected):
    row = {'source': source, 'listing_url': GREEFF.format(town, 'x')}
    assert fs.wrong_city(row, city) is expected
