"""
eXp Realty South Africa — Cape Town listing scraper.

ARCHITECTURE: Calls eXp Realty's Supabase backend API directly instead of
scraping HTML. The site's search page loads listings by calling:

  https://ywzpnbmomlzkcbzzkaqr.supabase.co/rest/v1/listings
    ?select=*&country_code=eq.ZA&status=eq.1
    &listing_type=in.(1,4,5,6,9,7)      ← sale
    &city=ilike.%25Cape+Town%25
    &order=created_at.desc,id.asc
    &offset=0&limit=24

We replicate this request with offset-based pagination to retrieve all records.

Listing type codes (from observed data):
  1 = For Sale (standard)
  7 = Auction / other sale type
  4, 5, 6, 9 = other sale variants (included in site's "buy" filter)
  2 = To Let (standard rental)
  3, 8 = other rental variants

Record fields used:
  id           → listing_id = 'exprealty_{id}'
  title        → title
  description  → description
  price        → price (ZAR integer)
  bedrooms     → bedrooms
  bathrooms    → bathrooms
  square_feet  → size_m2 (field name is legacy; SA uses m², values are in m²)
  images[0].url→ image_url
  address      → suburb_normalised (suburb extracted from full address)
  listing_type → 'sale' (types 1,4,5,6,9,7) or 'rent' (all other types)

Detail URL: https://www.expsouthafrica.co.za/property/{id}

POPIA: no personal info stored (agent_* fields are excluded).
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import AsyncGenerator

from scraper.utils import (
    clean_personal_info,
    normalise_suburb,
    compute_price_per_m2,
    validate_image_url,
)
from scraper.cities import CITIES
from scraper.enrich import costs_from_texts, resolve_status
from scraper.detail import parse_detail_fields
from scraper.extractors import cost_in_bounds, parse_money

logger = logging.getLogger('homelens.exprealty')

# ═══════════════════════════════════════════════════════════════════════════
# SITE SELECTORS — listing status + monthly levy/rates.
# eXp is read from its JSON API, so "selectors" are API field names. Checked
# against tests/fixtures/exprealty_api.json and a survey of all 763 Cape Town
# records (re-capture: scripts/capture_fixtures.py exprealty).
# ═══════════════════════════════════════════════════════════════════════════
SELECTORS = {
    # `status` is "1" on every record the site currently returns. The scraper's
    # own filter also admits 8, 9, 10, 11 but none occur in the data, so their
    # meaning is unknown — unmapped codes fall through to the text fallback
    # rather than being guessed at.
    'status_field': 'status',
    'status_codes': {'1': 'live'},
    'status_map':   {},
    # `levy` and `rates` exist on every record but are null on all 763 today
    # (`additional_costs_monthly` too — not used, it is not levy/rates). They are
    # read first in case agents start filling them in; otherwise the levy/rates
    # typed into the description is picked up by the text fallback.
    'levy_field':   'levy',
    'rates_field':  'rates',
    'cost_rows':    None,
}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = 'https://www.expsouthafrica.co.za'

# Supabase anon key — public key embedded in the browser bundle.
# Expires 2035 (exp=2060225233).
_SUPABASE_HOST = 'ywzpnbmomlzkcbzzkaqr.supabase.co'
_SUPABASE_KEY  = (
    'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9'
    '.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inl3enBuYm1vbWx6a2NienprYXFyIiwicm9sZSI6'
    'ImFub24iLCJpYXQiOjE3NDQ2NDkyMzMsImV4cCI6MjA2MDIyNTIzM30'
    '.6b8PT7DMzY2jnRgglammdCpqsT6EKR1_Na2T7djGb9A'
)
_SUPABASE_BASE = f'https://{_SUPABASE_HOST}/rest/v1/listings_public'

# listing_type values that correspond to "For Sale" on the site
_SALE_TYPES = '(1,4,5,6,9,7)'

# Status codes the site now includes (was eq.1 only — site now sends a
# broader set; verified against live network capture 2026-07-30).
_STATUS_FILTER = 'in.(1,8,9,10,11)'

# Cape Town metro bounding box — matches what the site's search uses.
# Re-captured from browser network intercept 2026-07-30 (previous bounds
# were stale — eXp's site now sends a slightly different box).
_GEO_LAT_MIN = '-34.69713791925447'
_GEO_LAT_MAX = '-33.12904050654684'
_GEO_LON_MIN = '18.150688306249997'
_GEO_LON_MAX = '19.161430493749997'

# City of Johannesburg metro bounding box (Midrand in the north to Orange Farm
# in the south, Randburg/Roodepoort in the west to the Ekurhuleni border in the
# east). Approximate — eXp's own Johannesburg search box has not been captured.
# Pretoria (lat > -25.8) is deliberately outside it.
CITY_GEO_BOUNDS: dict[str, tuple[str, str, str, str]] = {
    #                 lat_min               lat_max               lon_min              lon_max
    'cape-town':    (_GEO_LAT_MIN,         _GEO_LAT_MAX,         _GEO_LON_MIN,        _GEO_LON_MAX),
    'johannesburg': ('-26.45',             '-25.85',             '27.70',             '28.35'),
}

PAGE_SIZE   = 24
MAX_RECORDS = {
    'sale': 900,  # ~766 sale listings in Cape Town metro; ceiling for safety
    'rent': 500,  # ~383 rent listings
}
REQUEST_DELAY = 1.0   # seconds between API calls

_MAX_BEDS = 10

# ---------------------------------------------------------------------------
# Cape Town suburb whitelist
# Only listings whose extracted suburb matches one of these slugs are kept.
# Prevents Joburg/PE/Durban listings (with null geo coordinates) slipping through.
# ---------------------------------------------------------------------------
_CT_SUBURBS: frozenset[str] = frozenset({
    # City Bowl & CBD
    'cape-town', 'city-bowl', 'bo-kaap', 'de-waterkant', 'foreshore',
    'gardens', 'oranjezicht', 'tamboerskloof', 'vredehoek', 'higgovale',
    'zonnebloem', 'woodstock', 'salt-river', 'observatory', 'maitland',
    # Atlantic Seaboard
    'sea-point', 'green-point', 'three-anchor-bay', 'mouille-point',
    'fresnaye', 'bantry-bay', 'clifton', 'camps-bay', 'bakoven',
    'llandudno', 'hout-bay', 'imizamo-yethu',
    # Southern Suburbs
    'claremont', 'newlands', 'rondebosch', 'mowbray', 'pinelands',
    'wynberg', 'plumstead', 'kenilworth', 'lansdowne', 'ottery',
    'bishop-court', 'bishopscourt', 'constantia', 'tokai', 'bergvliet',
    'meadowridge', 'high-constantia', 'meadowridge', 'retreat',
    'heathfield', 'diep-river', 'dieprivier', 'westlake', 'lakeside',
    'muizenberg', 'kalk-bay', 'st-james', 'fish-hoek', 'clovelly',
    'simon-town', 'simonstown', 'glencairn', 'sunnydale', 'ocean-view',
    'kommetjie', 'scarborough', 'noordhoek', 'capri', 'sunvalley',
    'sun-valley', 'marina-da-gama', 'marina-da-gama',
    # Northern Suburbs
    'bellville', 'tyger-valley', 'tygervalley', 'durbanville',
    'brackenfell', 'kraaifontein', 'kuilsrivier', 'kuils-river',
    'parow', 'goodwood', 'monte-vista', 'edgemead', 'bothasig',
    'parklands', 'blouberg', 'bloubergrand', 'bloubergsrand',
    'tableview', 'table-view', 'sunningdale', 'milnerton',
    'richmond-estate', 'richwood', 'welgemoed', 'de-tijger',
    'kenridge', 'pinehurst', 'eversdal', 'vierlanden', 'sonstraal',
    'sonstraal-heights', 'protea-valley', 'protea-heights',
    'uitzicht', 'die-boord', 'paradyskloof',
    # Helderberg
    'somerset-west', 'strand', 'gordon-bay', 'gordons-bay',
    'firgrove', 'morgenster', 'lourensford',
    # West Coast
    'melkbosstrand', 'big-bay', 'west-beach', 'wave-crest', 'waves-edge',
    'lagoon-beach', 'flamingo-vlei', 'sandown',
    # Cape Flats
    'mitchells-plain', 'mitchells-plain', 'athlone', 'hanover-park',
    'belhar', 'delft', 'blue-downs', 'khayelitsha', 'gugulethu',
    'langa', 'crossroads', 'philippi', 'grassy-park', 'lavender-hill',
    'lotus-river', 'steenberg', 'parkwood', 'pelican-park',
    'strandfontein', 'schaapkraal', 'happy-valley',
    # Winelands (within Cape metro)
    'stellenbosch',
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _api_headers() -> dict:
    return {
        'apikey':          _SUPABASE_KEY,
        'Authorization':   f'Bearer {_SUPABASE_KEY}',
        'Content-Type':    'application/json',
        'Prefer':          'count=exact',
        'Accept-Profile':  'public',
    }


def _fetch_page(offset: int, sale: bool, city_slug: str = 'cape-town') -> tuple[list[dict], int]:
    """
    Fetch one page of listings from Supabase.
    Returns (records, total_count).
    """
    if sale:
        lt_filter = f'listing_type=in.{_SALE_TYPES}'
    else:
        lt_filter = f'listing_type=not.in.{_SALE_TYPES}'

    # Geo bounds params are passed as raw query string (not URL-encoded through
    # urlencode) because PostgREST expects repeated param names for gte/lte.
    lat_min, lat_max, lon_min, lon_max = CITY_GEO_BOUNDS[city_slug]
    geo = (
        f'geo_lat=gte.{lat_min}&geo_lat=lte.{lat_max}'
        f'&geo_lon=gte.{lon_min}&geo_lon=lte.{lon_max}'
    )
    params = {
        'select':       '*',
        'country_code': 'eq.ZA',
        'status':       _STATUS_FILTER,
        'order':        'created_at.desc,id.asc',
        'offset':       str(offset),
        'limit':        str(PAGE_SIZE),
    }
    url = f'{_SUPABASE_BASE}?{lt_filter}&{geo}&{urllib.parse.urlencode(params)}'

    req = urllib.request.Request(url, headers=_api_headers())
    with urllib.request.urlopen(req, timeout=20) as resp:
        data  = json.loads(resp.read())
        cr    = resp.headers.get('Content-Range', '')
        # Content-Range: "0-23/168" → total = 168
        total = 0
        if '/' in cr:
            try:
                total = int(cr.split('/')[-1])
            except ValueError:
                pass
        return data, total


# eXp's `city` field is "Suburb, Town". Towns inside the City of Johannesburg
# metro; the bounding box also catches Tshwane (Centurion) and Ekurhuleni /
# West Rand towns (Germiston, Boksburg, Kempton Park, Edenvale, Krugersdorp, ...)
# which are other metros and are dropped.
_JHB_METRO_TOWNS: frozenset[str] = frozenset({
    'johannesburg', 'sandton', 'randburg', 'roodepoort', 'midrand', 'soweto',
})


def _suburb_from_city_field(city_field: str) -> str | None:
    """'Fourways Gardens, Sandton' -> 'fourways-gardens' (metro towns only)."""
    parts = [p.strip() for p in (city_field or '').split(',') if p.strip()]
    if len(parts) < 2 or parts[-1].lower() not in _JHB_METRO_TOWNS:
        return None
    return normalise_suburb(parts[0])


def _suburb_from_address(address: str) -> str | None:
    """
    Extract suburb from a full address string.

    Examples:
      "18, Bergvliet Vistas, 59 Main Road, Heathfield, Cape Town, Western Cape"
        → "heathfield"
      "Sea Point, Cape Town, Western Cape"
        → "sea-point"
    """
    if not address:
        return None

    skip_lower = {
        'cape town', 'western cape', 'south africa', 'johannesburg',
        'gauteng', 'kwazulu-natal', 'eastern cape', 'northern cape',
    }
    parts = [p.strip() for p in address.split(',')]

    for part in reversed(parts):
        lc = part.lower()
        if lc in skip_lower:
            continue
        if len(part) < 3:
            continue
        # Skip parts that are mostly numeric ("59 Main Service Road", "18")
        if re.match(r'^\d+', part) or re.search(r'\b\d{4,}\b', part):
            continue
        return normalise_suburb(part) if part else None
    return None


def _suburb_from_title(title: str) -> str | None:
    """
    Fallback suburb extraction from eXp Realty title strings.

    Patterns handled:
      "3 Bed Apartment in Sea Point"         → "sea-point"
      "Sea Point | Modern Apartment"         → "sea-point"
      "Camps Bay, 4 Bed House"               → "camps-bay"
      "4 Bed House to Rent - Bantry Bay"     → "bantry-bay"
    """
    if not title:
        return None

    # Pattern 1 + 2: "in {suburb}" with optional listing-type or punctuation suffix
    for _pat in (
        r'\bin\s+([A-Za-z\s]+?)(?:\s+for\s+sale|\s+to\s+let|\s+for\s+rent)?$',
        r'\bin\s+([A-Za-z\s]+?)(?:\s*[-–]\s*|\s*,|\s*$)',
    ):
        _m = re.search(_pat, title, re.I)
        if _m:
            result = normalise_suburb(_m.group(1).strip())
            if result and result in _CT_SUBURBS:
                return result

    # Pattern 2: first pipe/dash/comma segment that looks like a suburb name
    for part in re.split(r'[|,\-–]', title):
        part = part.strip()
        if not part or len(part) < 3:
            continue
        # Skip segments that contain digits followed by bed/bath/m²
        if re.search(r'\d+\s*(bed|bath|m²|m2)', part, re.I):
            continue
        # Skip generic words
        if re.match(r'^(to\s+rent|for\s+sale|property|apartment|house|flat|studio)', part, re.I):
            continue
        result = normalise_suburb(part)
        if result and result in _CT_SUBURBS:
            return result

    return None


def _parse_size(square_feet) -> float | None:
    """
    The `square_feet` DB column stores m² values (SA is metric, field name
    is a legacy artifact of the PropCon platform). Return as float m².
    """
    if square_feet is None:
        return None
    try:
        v = float(square_feet)
        return v if 5 < v < 50_000 else None
    except (TypeError, ValueError):
        return None


def _parse_price(price, listing_type: str = 'sale') -> tuple[int | None, str]:
    """Return (price, price_status) from a raw API price field.

    Plausible ranges (ZAR):
      sale — R 50 000 to R 500 000 000
      rent — R 1 000 to R 500 000 per month
    """
    if price is None:
        return None, 'unknown'
    try:
        v = int(price)
        if listing_type == 'rent':
            ok = 1_000 <= v <= 500_000
        else:
            ok = 50_000 <= v <= 500_000_000
        if ok:
            return v, 'ok'
        return None, 'extraction_failed'   # outside plausible range
    except (TypeError, ValueError):
        return None, 'extraction_failed'


def _parse_beds(bedrooms) -> int | None:
    if bedrooms is None:
        return None
    try:
        v = int(bedrooms)
        return v if 0 <= v <= _MAX_BEDS else None
    except (TypeError, ValueError):
        return None


def _parse_baths(bathrooms) -> int | None:
    if bathrooms is None:
        return None
    try:
        v = int(bathrooms)
        return v if 0 < v <= 20 else None
    except (TypeError, ValueError):
        return None


def _image_from_record(record: dict) -> str | None:
    """Extract first image URL from the `images` field (list of dicts)."""
    images = record.get('images')
    if not images or not isinstance(images, list):
        return None
    first = images[0]
    if isinstance(first, dict):
        raw = first.get('url') or first.get('original_url') or first.get('thumbnail_url')
        return validate_image_url(raw)
    return None


def _map_record(record: dict, listing_type: str, city_slug: str = 'cape-town') -> dict | None:
    """Map a Supabase record to the Homelens listing schema."""
    uid = record.get('id', '')
    if not uid:
        return None

    listing_id  = f'exprealty_{uid}'
    listing_url = f'{BASE_URL}/property/{uid}'

    price, price_status = _parse_price(record.get('price'), listing_type)
    bedrooms = _parse_beds(record.get('bedrooms'))
    baths    = _parse_baths(record.get('bathrooms'))
    size_m2  = _parse_size(record.get('square_feet'))
    image    = _image_from_record(record)

    title       = (record.get('title') or '').strip()
    description = (record.get('description') or '').strip()
    address     = record.get('address') or ''

    if city_slug == 'cape-town':
        suburb = _suburb_from_address(address)
        if not suburb:
            # Title fallback only knows Cape Town suburbs
            suburb = _suburb_from_title(title)
    else:
        # The address is mostly empty and, when set, ends in the broad area
        # ("Sandton"), so use the "Suburb, Town" city field instead.
        suburb = _suburb_from_city_field(record.get('city') or '')

    price_per_m2 = compute_price_per_m2(price, size_m2)

    extras = _read_detail_extras(record, description)
    if size_m2 is None and extras['size_m2'] is not None:
        size_m2 = extras['size_m2']
        price_per_m2 = compute_price_per_m2(price, size_m2)

    listing_status, status_source = _read_status(record, title, description)
    costs = _read_costs(record, description) if listing_type == 'sale' else {}

    # ── Cape Town suburb whitelist — discard non-CT and no-suburb listings ──
    if not suburb:
        logger.debug('Skipping listing %s — no suburb found (address=%r)', listing_id, address)
        return None
    # The geo bounding box already scopes other cities; the whitelist is Cape Town only.
    if city_slug == 'cape-town' and suburb not in _CT_SUBURBS:
        logger.debug('Skipping non-CT suburb: %s (listing %s)', suburb, listing_id)
        return None

    return {
        'listing_id':        listing_id,
        'listing_url':       listing_url,
        'source':            'exprealty',
        'listing_type':      listing_type,
        'title':             clean_personal_info(title[:250]) if title else None,
        'description':       clean_personal_info(description[:5000]) if description else None,
        'price':             price,
        'price_status':      price_status,
        'bedrooms':          bedrooms,
        'bathrooms':         baths,
        'size_m2':           size_m2,
        'suburb_normalised': suburb,
        'price_per_m2':      price_per_m2,
        'image_url':         image,
        'is_active':         True,
        'listing_status':    listing_status,
        'status_source':     status_source,
        **costs,
        **{k: extras[k] for k in _EXTRA_KEYS if extras[k] is not None},
        'details_checked_at': datetime.now(timezone.utc).isoformat(),
    }


_EXTRA_KEYS = ('parking', 'erf_size', 'is_furnished', 'building_name', 'security',
               'views', 'levy_includes', 'pet_friendly')


def _read_detail_extras(record: dict, description: str) -> dict:
    """
    Parking / erf / furnished / security / views … from the API record's own
    fields (plot_size_sqm, garage, parking_info, amenities) and the description.
    Anything the record doesn't state stays None.
    """
    amenities = record.get('amenities') or []
    if isinstance(amenities, str):
        amenities = [a.strip() for a in re.split(r'[,;|]', amenities) if a.strip()]
    fields = parse_detail_fields([], {'amenities': [str(a) for a in amenities]}, description, description)

    plot = _parse_size(record.get('plot_size_sqm'))
    if plot:
        fields['erf_size'] = plot

    for key in ('garage', 'parking_info'):
        raw = record.get(key)
        try:
            n = int(float(raw))
        except (TypeError, ValueError):
            n = None
        if n and n > 0:
            fields['parking'] = n
            break
    return fields


def _read_status(record: dict, title: str, description: str) -> tuple[str, str]:
    """API status code first; unknown codes fall back to the text of the listing."""
    code = str(record.get(SELECTORS['status_field']))
    mapped = SELECTORS['status_codes'].get(code)
    if mapped:
        return mapped, 'site'
    if code not in ('None', ''):
        logger.debug('Unmapped eXp status code %r on %s', code, record.get('id'))
    return resolve_status([], f'{title}\n{description}', SELECTORS)


def _api_amount(value, kind: str) -> float | None:
    if value in (None, ''):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        amount = parse_money(str(value))
    return cost_in_bounds(kind, amount)


def _read_costs(record: dict, description: str) -> dict:
    """Sale listings only. API levy/rates first, then the full description text."""
    levy  = _api_amount(record.get(SELECTORS['levy_field']), 'levy')
    rates = _api_amount(record.get(SELECTORS['rates_field']), 'rates')
    if levy is not None or rates is not None:
        costs = {
            'levy_monthly': levy,
            'rates_monthly': rates,
            'levies_rates_combined_monthly': None,
            'costs_source': 'site',
            'costs_raw_snippet': f'api levy={record.get(SELECTORS["levy_field"])} '
                                 f'rates={record.get(SELECTORS["rates_field"])}',
        }
    else:
        # Full description — the 1000-char cut stored on the listing would drop
        # levies typed at the end.
        costs = costs_from_texts(None, description)
    costs['costs_checked_at'] = datetime.now(timezone.utc).isoformat()
    return costs


# ---------------------------------------------------------------------------
# Main scraper generator
# ---------------------------------------------------------------------------

async def _run_exprealty_city(city: dict) -> AsyncGenerator[dict, None]:
    """
    Async generator — yields one listing dict at a time.

    Calls the Supabase REST API directly (no Playwright / browser needed).
    """
    seen_ids: set[str] = set()

    for lt_name, is_sale in [('sale', True), ('rent', False)]:
        max_records = MAX_RECORDS[lt_name]
        offset = 0
        page = 0
        total_reported = None

        logger.info('── Starting eXp Realty %s (max %d records) ──', lt_name, max_records)

        while offset < max_records:
            page += 1
            try:
                records, total = _fetch_page(offset, is_sale, city['slug'])
            except Exception as e:
                logger.warning('[%s] Page %d (offset=%d) failed: %s — stopping', lt_name, page, offset, e)
                break

            if total_reported is None and total:
                total_reported = total
                logger.info('[%s] API reports %d total records', lt_name, total)

            n_raw = len(records)
            logger.info('[%s] Page %d (offset=%d): %d records', lt_name, page, offset, n_raw)

            if n_raw == 0:
                logger.info('[%s] No more records — done', lt_name)
                break

            new_this_page = 0
            for rec in records:
                mapped = _map_record(rec, lt_name, city['slug'])
                if not mapped:
                    continue
                lid = mapped['listing_id']
                if lid in seen_ids:
                    continue
                seen_ids.add(lid)
                new_this_page += 1
                yield mapped

            logger.info('[%s] Page %d: %d new (total yielded so far: %d)', lt_name, page, new_this_page, len(seen_ids))

            if n_raw < PAGE_SIZE:
                logger.info('[%s] Last page reached (%d < %d)', lt_name, n_raw, PAGE_SIZE)
                break

            offset += PAGE_SIZE
            if offset < max_records:
                time.sleep(REQUEST_DELAY)


async def run_exprealty_scraper() -> AsyncGenerator[dict, None]:
    """Scrapes every city in scraper.cities.CITIES, tagging each listing with its city."""
    for city in CITIES:
        logger.info('══ exprealty — %s ══', city['name'])
        async for listing in _run_exprealty_city(city):
            listing['city'] = city['slug']
            yield listing
