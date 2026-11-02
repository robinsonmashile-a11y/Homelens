"""
Cities the scrapers cover.

Every scraper loops over SCRAPE_TARGETS and stamps `city` (the city slug) on each listing it
yields; that value is stored in listings.city.

`platform_path` is the URL segment on the shared /results/residential/ agency
platform (jawitz, seeff, tyson, ...). Verified against jawitz: listing URLs are
/results/residential/for-sale/johannesburg/{suburb}/{type}/{id}/. The province
form (gauteng/johannesburg) is NOT a location filter there and returns the
national list, so it must not be used.

The platform files each city's suburbs under several "city" pages (regions): the northern
suburbs of Johannesburg sit under /sandton/, /randburg/ ... not /johannesburg/; Milnerton,
Blouberg, Bellville ... sit beside /cape-town/. Scraping only the home page misses roughly
half the market, so scrapers visit every region of a city (the municipal boundary: City of
Johannesburg / City of Cape Town) and stamp the city's own slug on each listing. Ekurhuleni
(Edenvale, Germiston ...) and Tshwane (Pretoria, Centurion) are other metros and stay out.
"""
from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

CITIES: list[dict] = [
    {'slug': 'cape-town',    'name': 'Cape Town',    'platform_path': 'cape-town',
     'province': 'western-cape'},
    {'slug': 'johannesburg', 'name': 'Johannesburg', 'platform_path': 'johannesburg',
     'province': 'gauteng'},
]

CITY_SLUGS = tuple(c['slug'] for c in CITIES)   # always all cities (URL parsing)

CITY_REGIONS: dict[str, tuple[str, ...]] = {
    'cape-town': ('cape-town', 'milnerton', 'blouberg', 'bellville', 'durbanville', 'parow',
                  'brackenfell', 'kuils-river', 'goodwood', 'blue-downs', 'mitchells-plain',
                  'hout-bay', 'kommetjie', 'fish-hoek', 'simons-town', 'somerset-west',
                  'strand', 'gordons-bay'),
    'johannesburg': ('johannesburg', 'sandton', 'randburg', 'roodepoort', 'midrand', 'soweto'),
}
REGION_TO_CITY: dict[str, str] = {r: slug for slug, rs in CITY_REGIONS.items() for r in rs}
REGION_SLUGS = tuple(REGION_TO_CITY)

# Optional filter, e.g. HOMELENS_CITIES=cape-town — lets CI run one city per job.
_only = {c.strip() for c in os.environ.get('HOMELENS_CITIES', '').split(',') if c.strip()}
if _only:
    CITIES = [c for c in CITIES if c['slug'] in _only]

# Cities this process is responsible for when HOMELENS_CITIES narrows the run
# (None = all). mark_inactive must be limited to these, or a per-city CI job
# would deactivate every other city's listings.
SCOPED_CITY_SLUGS: list[str] | None = [c['slug'] for c in CITIES] if _only else None

# What the scrapers loop over: one entry per region. `slug` is the CITY slug stamped on the
# listing; `platform_path` is the region's URL segment.
SCRAPE_TARGETS: list[dict] = [
    {**c, 'platform_path': region,
     'name': c['name'] if region == c['platform_path'] else f"{c['name']} / {region}"}
    for c in CITIES for region in CITY_REGIONS[c['slug']]
]


def platform_suburb_from_url(url: str) -> tuple[str | None, str | None]:
    """
    Parse /results/residential/{for-sale|to-rent|to-let}/[province/]{city}/{suburb}/{type}/{id}/
    Returns (city_slug, suburb_slug); either may be None. A region (sandton, milnerton ...)
    maps to its city.
    """
    parts = [p for p in urlparse(url).path.split('/') if p]
    for region in REGION_SLUGS:
        if region in parts:
            i = parts.index(region)
            suburb = parts[i + 1] if len(parts) > i + 3 else None
            return REGION_TO_CITY[region], suburb
    return None, None


def city_url(base: str, city: dict) -> str:
    """
    Re-target a Cape Town listing-search URL at `city` (a CITIES or SCRAPE_TARGETS entry).
    The Cape Town home region is returned untouched. A site that already puts the province in
    its Cape Town URL ('/western-cape/cape-town') gets the same form for the new region
    ('/gauteng/sandton'); otherwise '/cape-town' becomes the region's platform_path.
    """
    if city['platform_path'] == 'cape-town':
        return base
    if '/western-cape/cape-town' in base:
        return base.replace('/western-cape/cape-town',
                            f"/{city['province']}/{city['platform_path']}", 1)
    if '/cape-town' in base:
        return base.replace('/cape-town', '/' + city['platform_path'], 1)
    raise ValueError(f'No Cape Town segment to replace in {base!r}')


def city_index(parts: list[str]) -> int | None:
    """Index of the city/region segment in a split URL path, or None."""
    return next((i for i, p in enumerate(parts) if p in REGION_SLUGS), None)


# A region page that does not exist can silently return the national list. Every listing's
# URL carries its region, so anything else is dropped, and a region that yields nothing but
# foreign listings is abandoned instead of paged through.
REGION_PROBE = 40


async def region_guard(listings, target: dict):
    """Pass through only listings whose URL is inside `target`'s region."""
    region = target['platform_path']
    inside = foreign = 0
    try:
        async for listing in listings:
            parts = [p for p in urlparse(listing.get('listing_url') or '').path.split('/') if p]
            if region in parts:
                inside += 1
                yield listing
                continue
            foreign += 1
            if not inside and foreign >= REGION_PROBE:
                logger.warning('Region %s returned only foreign listings — skipping it', region)
                break
    finally:
        await listings.aclose()
