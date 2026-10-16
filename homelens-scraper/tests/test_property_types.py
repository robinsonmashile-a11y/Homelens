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
