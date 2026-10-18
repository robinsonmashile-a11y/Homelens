import pytest

from scraper.property_types import classify_property_type, looks_non_residential


@pytest.mark.parametrize('text,expected', [
    ('2 Bedroom Cluster For Sale in Wynberg Upper', 'townhouse'),
    ('Sectional Title For Sale in Bantry Bay', 'apartment'),
    ('6 Bedroom Freestanding For Sale in Walmer Estate', 'house'),
    ('Brand New 4 Bedroom Double Storey Home', 'house'),
    ('Farm in Philadelphia For Sale', 'other'),
    ('Vacant land Sea Point', 'other'),
    ('Highland Estate house', 'house'),        # "land" inside a word is not land
    ('2 Bedroom Apartment near the Wine Farm', 'apartment'),
    ('SAGEWOOD', 'other'),
])
def test_classify(text, expected):
    assert classify_property_type(text) == expected


def test_looks_non_residential():
    assert looks_non_residential('7,633m² Farm For Sale')
    assert not looks_non_residential('SAGEWOOD')


def test_validate_listing_nulls_overflow_price():
    from scraper.database import validate_listing
    out = validate_listing({'listing_id': 'x', 'price': 2_999_999_999, 'price_per_m2': 5})
    assert out['price'] is None and out['price_status'] == 'extraction_failed'
    ok = validate_listing({'listing_id': 'y', 'price': 5_000_000})
    assert ok['price'] == 5_000_000
