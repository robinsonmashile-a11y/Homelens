"""Regions: each city is scraped across all of its platform "city" pages (sandton, milnerton ...)."""
import asyncio

import pytest

from scraper import cities
from scraper.cities import (CITY_REGIONS, REGION_TO_CITY, SCRAPE_TARGETS, city_index, city_url,
                            platform_suburb_from_url, region_guard)


def _target(region):
    return next(t for t in SCRAPE_TARGETS if t['platform_path'] == region)


def test_every_region_belongs_to_exactly_one_city():
    flat = [r for rs in CITY_REGIONS.values() for r in rs]
    assert len(flat) == len(set(flat))
    assert REGION_TO_CITY['sandton'] == 'johannesburg'
    assert REGION_TO_CITY['milnerton'] == 'cape-town'


def test_other_metros_are_not_regions():
    for other in ('pretoria', 'centurion', 'edenvale', 'germiston', 'boksburg', 'benoni', 'stellenbosch'):
        assert other not in REGION_TO_CITY


def test_targets_stamp_the_city_slug_not_the_region():
    assert _target('sandton')['slug'] == 'johannesburg'
    assert _target('bellville')['slug'] == 'cape-town'
    assert {t['slug'] for t in SCRAPE_TARGETS} == {'cape-town', 'johannesburg'}


def test_city_url_retargets_regions():
    base = 'https://x.co.za/results/residential/to-let/cape-town/'
    assert city_url(base, _target('cape-town')) == base
    assert city_url(base, _target('milnerton')) == 'https://x.co.za/results/residential/to-let/milnerton/'
    assert city_url(base, _target('sandton')) == 'https://x.co.za/results/residential/to-let/sandton/'
    prov = 'https://x/results/residential/for-sale/western-cape/cape-town/'
    assert city_url(prov, _target('randburg')) == 'https://x/results/residential/for-sale/gauteng/randburg/'


def test_city_index_and_suburb_parse_understand_regions():
    parts = ['results', 'residential', 'for-sale', 'sandton', 'bryanston', 'house', '1']
    assert city_index(parts) == 3
    assert platform_suburb_from_url('https://x/results/residential/to-let/sandton/bryanston/house/1/') \
        == ('johannesburg', 'bryanston')
    assert platform_suburb_from_url('https://x/results/residential/for-sale/milnerton/table-view/house/1/') \
        == ('cape-town', 'table-view')


async def _gen(urls):
    for u in urls:
        yield {'listing_url': u}


def _run(urls, region):
    async def go():
        return [x async for x in region_guard(_gen(urls), _target(region))]
    return asyncio.run(go())


def test_guard_keeps_only_listings_inside_the_region():
    ok = 'https://x/results/residential/for-sale/sandton/bryanston/house/1/'
    other = 'https://x/results/residential/for-sale/pretoria/hatfield/house/2/'
    assert [l['listing_url'] for l in _run([ok, other, ok], 'sandton')] == [ok, ok]


def test_guard_abandons_a_region_that_returns_only_the_national_list():
    foreign = [f'https://x/results/residential/for-sale/pretoria/a/house/{i}/' for i in range(500)]
    seen = []

    async def go():
        async def counting():
            for u in foreign:
                seen.append(u)
                yield {'listing_url': u}
        return [x async for x in region_guard(counting(), _target('sandton'))]

    assert asyncio.run(go()) == []
    assert len(seen) <= cities.REGION_PROBE + 1


@pytest.mark.parametrize('site', ['seeff', 'jawitz', 'tyson', 'greeff', 'dgproperties', 'century21',
                                  'chaseveritt', 'sothebys', 'harcourts', 'justproperty', 'lionlead',
                                  'abc_international'])
def test_platform_scrapers_use_regions_and_the_guard(site):
    import importlib
    import inspect
    src = inspect.getsource(importlib.import_module(f'scraper.{site}'))
    assert 'for city in SCRAPE_TARGETS' in src and f'region_guard(_run_{site}_city(city), city)' in src
