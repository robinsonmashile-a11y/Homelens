"""City support: URL re-targeting, suburb parsing per city, eXp Johannesburg filtering."""
import pytest

from scraper.cities import CITIES, city_index, city_url
from scraper import exprealty

CPT = next(c for c in CITIES if c['slug'] == 'cape-town')
JNB = next(c for c in CITIES if c['slug'] == 'johannesburg')


def test_cape_town_urls_unchanged():
    for u in ('https://x.co.za/results/residential/for-sale/cape-town/',
              'https://x.co.za/results/residential/for-sale/western-cape/cape-town/',
              'https://www.remax.co.za/property-for-sale-south-africa/western-cape/cape-town'):
        assert city_url(u, CPT) == u


def test_johannesburg_urls():
    assert city_url('https://x.co.za/results/residential/to-let/cape-town/', JNB) == \
        'https://x.co.za/results/residential/to-let/johannesburg/'
    assert city_url('https://rawson.co.za/property/for-sale/cape-town', JNB) == \
        'https://rawson.co.za/property/for-sale/johannesburg'
    # Sites that already carry the province keep that convention
    assert city_url('https://x/results/residential/for-sale/western-cape/cape-town/', JNB) == \
        'https://x/results/residential/for-sale/gauteng/johannesburg/'


def test_city_index():
    assert city_index(['results', 'residential', 'for-sale', 'johannesburg', 'melrose']) == 3
    assert city_index(['results', 'residential', 'for-sale', 'cape-town', 'sea-point']) == 3
    assert city_index(['results', 'residential', 'for-sale', 'grabouw', 'grabouw']) is None


@pytest.mark.parametrize('mod,url,expected', [
    ('jawitz', '/results/residential/for-sale/johannesburg/melrose/house/1/', 'melrose'),
    ('jawitz', '/results/residential/for-sale/cape-town/sea-point/flat/1/', 'sea-point'),
    ('tyson', '/results/residential/for-sale/johannesburg/sandhurst/house/1/', 'sandhurst'),
    ('dgproperties', '/results/residential/for-sale/johannesburg/rosebank/apartment/1/', 'rosebank'),
    ('chaseveritt', '/results/residential/for-sale/johannesburg/bryanston/house/1/', 'bryanston'),
    ('harcourts', '/results/residential/for-sale/johannesburg/fourways/house/1/', 'fourways'),
])
def test_platform_suburb_parsers(mod, url, expected):
    import importlib
    m = importlib.import_module(f'scraper.{mod}')
    fn = getattr(m, '_extract_suburb_from_url')
    assert fn('https://x.co.za' + url) == expected


def test_exp_johannesburg_keeps_metro_towns_only():
    assert exprealty._suburb_from_city_field('Fourways Gardens, Sandton') == 'fourways-gardens'
    assert exprealty._suburb_from_city_field('Jeppestown, Johannesburg') == 'jeppestown'
    assert exprealty._suburb_from_city_field('Solheim, Germiston') is None      # Ekurhuleni
    assert exprealty._suburb_from_city_field('Zwartkop, Centurion') is None     # Tshwane
    assert exprealty._suburb_from_city_field('') is None


def test_exp_johannesburg_record_skips_cape_town_whitelist():
    rec = {'id': 'abc', 'price': 2_500_000, 'bedrooms': 3, 'bathrooms': 2,
           'city': 'Bryanston, Sandton', 'address': None, 'title': 'House', 'description': ''}
    out = exprealty._map_record(rec, 'sale', 'johannesburg')
    assert out and out['suburb_normalised'] == 'bryanston'
    assert exprealty._map_record(rec, 'sale', 'cape-town') is None  # not a CT suburb
