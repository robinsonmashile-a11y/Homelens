"""
Cities the scrapers cover.

Every scraper loops over CITIES and stamps `city` (the slug) on each listing it
yields; that value is stored in listings.city.

`platform_path` is the URL segment on the shared /results/residential/ agency
platform (jawitz, seeff, tyson, ...). Verified against jawitz: listing URLs are
/results/residential/for-sale/johannesburg/{suburb}/{type}/{id}/. The province
form (gauteng/johannesburg) is NOT a location filter there and returns the
national list, so it must not be used.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

CITIES: list[dict] = [
    {'slug': 'cape-town',    'name': 'Cape Town',    'platform_path': 'cape-town',
     'province': 'western-cape'},
    {'slug': 'johannesburg', 'name': 'Johannesburg', 'platform_path': 'johannesburg',
     'province': 'gauteng'},
]

CITY_SLUGS = tuple(c['slug'] for c in CITIES)   # always all cities (URL parsing)

# Optional filter, e.g. HOMELENS_CITIES=cape-town — lets CI run one city per job.
_only = {c.strip() for c in os.environ.get('HOMELENS_CITIES', '').split(',') if c.strip()}
if _only:
    CITIES = [c for c in CITIES if c['slug'] in _only]


def platform_suburb_from_url(url: str) -> tuple[str | None, str | None]:
    """
    Parse /results/residential/{for-sale|to-rent|to-let}/[province/]{city}/{suburb}/{type}/{id}/
    Returns (city_slug, suburb_slug); either may be None.
    """
    parts = [p for p in urlparse(url).path.split('/') if p]
    for slug in CITY_SLUGS:
        if slug in parts:
            i = parts.index(slug)
            suburb = parts[i + 1] if len(parts) > i + 3 else None
            return slug, suburb
    return None, None


def city_url(base: str, city: dict) -> str:
    """
    Re-target a Cape Town listing-search URL at `city`. Cape Town URLs are
    returned untouched. A site that already puts the province in its Cape Town
    URL ('/western-cape/cape-town') gets the same form for the new city
    ('/gauteng/johannesburg'); otherwise '/cape-town' becomes the city's
    platform_path.
    """
    if city['slug'] == 'cape-town':
        return base
    if '/western-cape/cape-town' in base:
        return base.replace('/western-cape/cape-town',
                            f"/{city['province']}/{city['slug']}", 1)
    if '/cape-town' in base:
        return base.replace('/cape-town', '/' + city['platform_path'], 1)
    raise ValueError(f'No Cape Town segment to replace in {base!r}')


def city_index(parts: list[str]) -> int | None:
    """Index of the city segment in a split URL path, or None."""
    return next((i for i, p in enumerate(parts) if p in CITY_SLUGS), None)
