from __future__ import annotations

"""
fix_suburbs.py
==============
Runs AFTER all scrapers and BEFORE compute_baselines.py.

Fixes listings where suburb_normalised is NULL or blank by extracting
the suburb from the listing title (and raw suburb field as a fallback).
Listings that still have no suburb after extraction are deactivated —
they cannot be scored without a suburb.
"""

import os
import re

from dotenv import load_dotenv
from supabase import create_client

# ── Step 1: Cape Town suburb dictionary ──────────────────────────────────────

CAPE_TOWN_SUBURBS = {
    # Atlantic Seaboard
    'sea point': 'sea-point',
    'green point': 'green-point',
    'mouille point': 'mouille-point',
    'three anchor bay': 'three-anchor-bay',
    'bantry bay': 'bantry-bay',
    'clifton': 'clifton',
    'camps bay': 'camps-bay',
    'bakoven': 'bakoven',
    'llandudno': 'llandudno',
    'fresnaye': 'fresnaye',

    # City Bowl
    'de waterkant': 'de-waterkant',
    'bo kaap': 'bo-kaap',
    'bo-kaap': 'bo-kaap',
    'gardens': 'gardens',
    'oranjezicht': 'oranjezicht',
    'tamboerskloof': 'tamboerskloof',
    'vredehoek': 'vredehoek',
    'higgovale': 'higgovale',
    'signal hill': 'signal-hill',

    # CBD / Foreshore
    'cape town city centre': 'cape-town-city-centre',
    'city centre': 'cape-town-city-centre',
    'cape town cbd': 'cape-town-city-centre',
    'foreshore': 'foreshore',
    'waterfront': 'waterfront',
    'granger bay': 'granger-bay',
    'de ville': 'de-ville',

    # Inner City / East
    'woodstock': 'woodstock',
    'observatory': 'observatory',
    'salt river': 'salt-river',
    'maitland': 'maitland',
    'paarden eiland': 'paarden-eiland',
    'paarden-eiland': 'paarden-eiland',
    'zonnebloem': 'zonnebloem',
    'district six': 'district-six',

    # Southern Suburbs
    'claremont': 'claremont',
    'newlands': 'newlands',
    'rondebosch': 'rondebosch',
    'kenilworth': 'kenilworth',
    'wynberg': 'wynberg',
    'plumstead': 'plumstead',
    'diep river': 'diep-river',
    'diep-river': 'diep-river',
    'bergvliet': 'bergvliet',
    'tokai': 'tokai',
    'meadowridge': 'meadowridge',
    'constantia': 'constantia',
    'bishopscourt': 'bishopscourt',
    'heathfield': 'heathfield',
    'ottery': 'ottery',
    'wetton': 'wetton',
    'lansdowne': 'lansdowne',
    'crawford': 'crawford',
    'athlone': 'athlone',
    'rondebosch east': 'rondebosch-east',
    'mowbray': 'mowbray',
    'rosebank': 'rosebank',
    'pinelands': 'pinelands',
    'thornton': 'thornton',
    'goodwood': 'goodwood',
    'parow': 'parow',

    # False Bay / South Peninsula
    'muizenberg': 'muizenberg',
    'kalk bay': 'kalk-bay',
    'kalk-bay': 'kalk-bay',
    'st james': 'st-james',
    'fish hoek': 'fish-hoek',
    'fish-hoek': 'fish-hoek',
    'simons town': 'simons-town',
    'simonstown': 'simons-town',
    'glencairn': 'glencairn',
    'scarborough': 'scarborough',
    'misty cliffs': 'misty-cliffs',
    'kommetjie': 'kommetjie',
    'noordhoek': 'noordhoek',
    'ocean view': 'ocean-view',
    'sun valley': 'sun-valley',
    'lakeside': 'lakeside',
    'marina da gama': 'marina-da-gama',
    'zeekoevlei': 'zeekoevlei',

    # Hout Bay / West
    'hout bay': 'hout-bay',
    'hout-bay': 'hout-bay',

    # Northern Suburbs
    'bellville': 'bellville',
    'durbanville': 'durbanville',
    'brackenfell': 'brackenfell',
    'kraaifontein': 'kraaifontein',
    'kuils river': 'kuils-river',
    'kuilsriver': 'kuils-river',
    'blue downs': 'blue-downs',
    'kleinvlei': 'kleinvlei',
    'eerste river': 'eerste-river',
    'strand': 'strand',
    'somerset west': 'somerset-west',
    'gordons bay': 'gordons-bay',
    "gordon's bay": 'gordons-bay',

    # West Coast / Blouberg
    'milnerton': 'milnerton',
    'tableview': 'tableview',
    'table view': 'tableview',
    'blouberg': 'blouberg',
    'bloubergstrand': 'bloubergstrand',
    'big bay': 'big-bay',
    'big-bay': 'big-bay',
    'parklands': 'parklands',
    'sunningdale': 'sunningdale',
    'sunset beach': 'sunset-beach',
    'melkbosstrand': 'melkbosstrand',
    'atlantis': 'atlantis',

    # Cape Flats
    'mitchells plain': 'mitchells-plain',
    "mitchell's plain": 'mitchells-plain',
    'mitchellsplain': 'mitchells-plain',
    'khayelitsha': 'khayelitsha',
    'gugulethu': 'gugulethu',
    'langa': 'langa',
    'nyanga': 'nyanga',
    'philippi': 'philippi',
    'hanover park': 'hanover-park',
    'mannenberg': 'mannenberg',
    'bridgetown': 'bridgetown',
    'grassy park': 'grassy-park',
    'lavender hill': 'lavender-hill',
    'pelican park': 'pelican-park',
    'retreat': 'retreat',
    'steenberg': 'steenberg',
    'capricorn': 'capricorn',
    'strandfontein': 'strandfontein',
    'belhar': 'belhar',
    'delft': 'delft',
    'bellville south': 'bellville-south',
    'rylands': 'rylands',
    'surrey estate': 'surrey-estate',
    'fairways': 'fairways',
    'southfield': 'southfield',
    'schaapkraal': 'schaapkraal',
    'kirstenhof': 'kirstenhof',
    'pelikan park': 'pelican-park',

    # Century City / Canal Walk area
    'century city': 'century-city',
    'century-city': 'century-city',

    # Atlantic Beach / West Coast
    'atlantic beach': 'atlantic-beach',
    'atlantic-beach': 'atlantic-beach',
    'melkbos': 'melkbosstrand',

    # Durbanville / Northern Suburbs extras
    'panorama': 'panorama',
    'parow north': 'parow-north',
    'parow central': 'parow-central',
    'burgundy estate': 'burgundy-estate',
    'welgemoed': 'welgemoed',
    'sonstraal': 'sonstraal-heights',
    'sonstraal heights': 'sonstraal-heights',
    'vierlanden': 'vierlanden',
    'pinehurst': 'pinehurst',
    'kenridge': 'kenridge',
    'loevenstein': 'loevenstein',
    'oude westhof': 'oude-westhof',
    'protea valley': 'protea-valley',
    'protea heights': 'protea-heights',
    'mikro park': 'mikro-park',
    'de bos': 'de-bos',
    'penhill': 'penhill',
    'montana': 'montana',
    'joostenberg vlakte': 'joostenberg-vlakte',
    'fisantekraal': 'fisantekraal',
    'klipheuwel': 'klipheuwel',
    'raithby': 'raithby',
    'klapmuts': 'klapmuts',
    'simondium': 'simondium',
    'vrykyk': 'vrykyk',
    'vredelust': 'vredelust',
    'stellenberg': 'stellenberg',
    'boston': 'boston',
    'oakdale': 'oakdale',
    'oostersee': 'oostersee',
    'unibell': 'unibell',
    'stikland': 'stikland',
    'morningstar': 'morning-star',
    'morning star': 'morning-star',
    'monte vista': 'monte-vista',
    'vasco': 'vasco-estate',
    'greenway rise': 'greenway-rise',
    'edgemead': 'edgemead',
    'richwood': 'richwood',
    'welgelegen': 'welgelegen',
    'plattekloof': 'plattekloof',
    'plattekloof glen': 'plattekloof',

    # Blouberg sub-areas
    'west beach': 'west-beach',
    'blouberg sands': 'blouberg-sands',
    'bloubergrant': 'bloubergrant',
    'blouberg rise': 'blouberg-rise',
    'flamingo vlei': 'flamingo-vlei',
    'waves edge': 'waves-edge',
    'aurora': 'aurora',

    # West Coast / Milnerton extras
    'milnerton rural': 'milnerton-rural',
    'lagoon beach': 'lagoon-beach',
    'parklands north': 'parklands-north',

    # Somerset West / Strand / Helderberg area
    'heldervue': 'heldervue',
    'paardevlei': 'paardevlei',
    'helderberg': 'helderberg',
    'croydon': 'croydon',
    'croydon olive estate': 'croydon',

    # Cape Flats extras
    'manenberg': 'manenberg',
    'portlands': 'portlands',
    'heideveld': 'heideveld',
    'bishop lavis': 'bishop-lavis',
    'vanguard': 'vanguard',
    'gatesville': 'gatesville',
    'kewtown': 'kewtown',
    'newfields': 'newfields',
    'belmont park': 'belmont-park',
    'new woodlands': 'new-woodlands',
    'matroosfontein': 'matroosfontein',
    'wallacedene': 'wallacedene',
    'lentegeur': 'lentegeur',
    'tafelsig': 'tafelsig',
    'eastridge': 'eastridge',
    'beacon valley': 'beacon-valley',
    'woodlands': 'woodlands',

    # South Peninsula extras
    'glencairn heights': 'glencairn-heights',
    'seaforth': 'seaforth',
    'smitswinkelbaai': 'smitswinkelbaai',
    'capri': 'capri',
    'crofters valley': 'crofters-valley',
    'chapmans peak': 'chapmans-peak',
    'slangkop': 'slangkop',
    'clovelly': 'clovelly',

    # Constantia sub-areas
    'belle constantia': 'belle-constantia',
    'constantia heights': 'constantia-heights',
    'high constantia': 'high-constantia',
    'kreupelbosch': 'kreupelbosch',

    # Claremont / Wynberg / Kenilworth / Rondebosch sub-areas
    'claremont upper': 'claremont-upper',
    'harfield village': 'harfield-village',
    'claremont village': 'claremont-village',
    'wynberg upper': 'wynberg-upper',
    'kenilworth upper': 'kenilworth-upper',
    'rondebosch village': 'rondebosch-village',

    # Durbanville sub-areas
    'durbanville central': 'durbanville-central',
    'durbanville hills': 'durbanville-hills',

    # Waterfront / City Bowl aliases
    'city bowl': 'cape-town-city-centre',
    'cape town central': 'cape-town-city-centre',

    # Hout Bay sub-areas
    'hangberg': 'hangberg',

    # Kuils River sub-areas
    'sarepta': 'kuils-river',
    'highbury': 'kuils-river',
    'annandale': 'kuils-river',
    'camelot': 'kuils-river',
    'rouxville': 'kuils-river',
    'zevenwacht': 'kuils-river',
    'saxenburg': 'kuils-river',
    'blackheath': 'kuils-river',
    'haasendal': 'kuils-river',

    # Other
    'de tijger': 'de-tijger',
    'welcome estate': 'welcome-estate',
    'pelican heights': 'pelican-heights',
    'norwood': 'norwood',
    'riverton': 'riverton',
    'sunningdale': 'sunningdale',
}

# Pre-sort by name length descending so longer matches win over substrings.
_SORTED_SUBURBS = sorted(
    CAPE_TOWN_SUBURBS.items(),
    key=lambda x: len(x[0]),
    reverse=True,
)

# ── Step 2: Extraction function ───────────────────────────────────────────────

_PATTERNS = [
    r'\bin\s+([a-z\s]+?)(?:\s+for\s+sale|\s+to\s+let|\s+for\s+rent|\s*$)',
    r'\bin\s+([a-z\s]+?)(?:\s*,|\s*\-|\s*$)',
]


def find_suburb_in_text(text: str) -> str | None:
    """Return a suburb slug for *text*, or None if no CT suburb is found."""
    if not text:
        return None

    text_lower = text.lower().strip()

    # Longest-match direct lookup
    for suburb_name, suburb_slug in _SORTED_SUBURBS:
        if suburb_name in text_lower:
            return suburb_slug

    # Regex "in [Suburb]" patterns
    for pattern in _PATTERNS:
        match = re.search(pattern, text_lower)
        if match:
            candidate = match.group(1).strip()
            if candidate in CAPE_TOWN_SUBURBS:
                return CAPE_TOWN_SUBURBS[candidate]

    return None


# ── Step 3: Main script ───────────────────────────────────────────────────────

# Studios / bachelor flats / "0.5 bedroom" listings arrive with bedrooms
# scattered across 0, 1, 5 (a "0.5" mis-parse that dropped the "0."), or empty.
# A studio genuinely has zero separate bedrooms, so we normalise every detected
# studio to bedrooms = 0 and property_type = apartment. This (a) rescues studios
# stored as empty (currently unscorable), (b) pulls studios out of the 5-bed
# pool where the rent model was discarding them as flatshare pollution, and
# (c) gives studios one clean comparison group. "0-bed apartment" = studio;
# "0-bed house/other" = commercial (handled elsewhere).
_STUDIO_RE = re.compile(r'\bstudio\b|\b0\.5\s*bed|\bbachelor\b|\bhalf\s*bed', re.I)


def is_studio(title: str | None) -> bool:
    return bool(title and _STUDIO_RE.search(title))


# Only Cape Town has a suburb dictionary (and the engine / investment model are
# Cape Town only), so this script must not touch other cities' listings.
CITY = 'cape-town'


class _Rows:
    def __init__(self, data):
        self.data = data


def _paged(query, page_size: int = 1000) -> _Rows:
    """Run a select for CITY across all pages. (.limit(20000) is silently capped
    at 1000 rows by PostgREST, which made this script process a random slice.)"""
    rows: list[dict] = []
    offset = 0
    while True:
        chunk = (
            query.eq('city', CITY)
            .order('listing_id')
            .range(offset, offset + page_size - 1)
            .execute()
            .data
        )
        rows.extend(chunk)
        if len(chunk) < page_size:
            return _Rows(rows)
        offset += page_size


def _bulk_update(supabase, ids: list[str], payload: dict, batch_size: int = 200) -> int:
    """Apply payload to all listing_ids in batched .in_() calls."""
    total = 0
    for i in range(0, len(ids), batch_size):
        chunk = ids[i:i + batch_size]
        supabase.table('listings').update(payload).in_('listing_id', chunk).execute()
        total += len(chunk)
    return total


def run_suburb_fix() -> None:
    load_dotenv()
    supabase = create_client(
        os.environ['SUPABASE_URL'],
        os.environ['SUPABASE_KEY'],
    )

    print('=== SUBURB FIX STARTING ===\n')

    # ── Fetch all data first (read-only phase) ────────────────────────────────
    print('Fetching listings...')
    active_result = _paged(
        supabase.table('listings')
        .select('listing_id, source, title, suburb, suburb_normalised, '
                'bedrooms, property_type')
        .eq('is_active', True)
    )
    inactive_null = _paged(
        supabase.table('listings')
        .select('listing_id, source, title, suburb, suburb_normalised')
        .eq('is_active', False)
        .is_('suburb_normalised', 'null')
    )
    inactive_empty = _paged(
        supabase.table('listings')
        .select('listing_id, source, title, suburb, suburb_normalised')
        .eq('is_active', False)
        .eq('suburb_normalised', '')
    )
    no_price_null = _paged(
        supabase.table('listings')
        .select('listing_id')
        .eq('is_active', True)
        .is_('price', 'null')
    )
    no_price_zero = _paged(
        supabase.table('listings')
        .select('listing_id')
        .eq('is_active', True)
        .eq('price', 0)
    )
    # Listings with no current price but a known first-seen price — likely unavailable/sold/let
    price_gone_result = _paged(
        supabase.table('listings')
        .select('listing_id, source, title, suburb_normalised, listing_type, price_at_first, listing_url, price_status, last_seen')
        .is_('price', 'null')
        .not_.is_('price_at_first', 'null')
    )
    commercial = _paged(
        supabase.table('listings')
        .select('listing_id')
        .eq('is_active', True)
        .eq('property_type', 'other')
    )
    # Inactive listings that still LOOK complete (price+suburb+type ok) but were
    # deactivated. We do NOT reactivate them (that resurrects dead listings — the
    # Tyson bug). But inactive RENTALS are still valuable historical evidence for
    # the investment rent model, so we flag those as is_reference = TRUE. They
    # remain is_active = FALSE and never appear in user-facing results; only
    # compute_investment.py reads them, within a recency window.
    good_but_inactive_result = _paged(
        supabase.table('listings')
        .select('listing_id,listing_type')
        .eq('is_active', False)
        .eq('price_status', 'ok')
        .not_.is_('price', 'null')
        .not_.is_('suburb_normalised', 'null')
        .neq('suburb_normalised', '')
        .in_('property_type', ['apartment', 'house'])
    )

    active_listings   = active_result.data
    inactive_listings = inactive_null.data + inactive_empty.data
    no_price_ids      = list({r['listing_id'] for r in no_price_null.data + no_price_zero.data})
    commercial_ids    = [r['listing_id'] for r in commercial.data]
    price_gone        = price_gone_result.data
    good_but_inactive_ids = [r['listing_id'] for r in good_but_inactive_result.data]
    # Only inactive RENTALS become reference evidence (the rent model is rental-
    # based). Inactive sales are left untouched.
    reference_rental_ids = [r['listing_id'] for r in good_but_inactive_result.data
                            if r.get('listing_type') == 'rent']

    print(f'Active listings:                   {len(active_listings)}')
    print(f'Inactive no-suburb listings:       {len(inactive_listings)}')
    print(f'Active no-price listings:          {len(no_price_ids)}')
    print(f'Active commercial listings:        {len(commercial_ids)}')
    print(f'Price-gone listings:               {len(price_gone)}  (had price_at_first, now price=NULL)')
    print(f'Good-but-inactive listings:        {len(good_but_inactive_ids)}  (price+suburb+type ok, LEFT INACTIVE — not reactivated)\n')

    # ── Classify active listings (in-memory, no DB calls yet) ─────────────────
    suburb_updates: dict[str, list[str]] = {}   # slug -> [listing_ids] to update
    deactivate_ids: list[str] = []
    failed_labels:  list[str] = []
    skipped = 0

    for listing in active_listings:
        lid            = listing['listing_id']
        title          = listing.get('title') or ''
        raw_suburb     = listing.get('suburb') or ''
        current_suburb = listing.get('suburb_normalised') or ''
        source         = listing.get('source') or ''

        suburb_found = find_suburb_in_text(title) or find_suburb_in_text(raw_suburb)

        if suburb_found:
            if suburb_found != current_suburb:
                suburb_updates.setdefault(suburb_found, []).append(lid)
                print(f'  FIXING [{source}] {title[:50]!r} -> {suburb_found}')
            else:
                skipped += 1
        else:
            if not current_suburb:
                deactivate_ids.append(lid)
                failed_labels.append(f'[{source}] {title[:60]}')
            else:
                skipped += 1

    # ── Classify inactive no-suburb listings ──────────────────────────────────
    reactivate_updates: dict[str, list[str]] = {}  # slug -> [listing_ids]

    for listing in inactive_listings:
        lid        = listing['listing_id']
        title      = listing.get('title') or ''
        raw_suburb = listing.get('suburb') or ''
        source     = listing.get('source') or ''

        suburb_found = find_suburb_in_text(title) or find_suburb_in_text(raw_suburb)
        if suburb_found:
            reactivate_updates.setdefault(suburb_found, []).append(lid)
            print(f'  REACTIVATING [{source}] {title[:50]!r} -> {suburb_found}')

    # ── Apply all updates in bulk ─────────────────────────────────────────────
    print('\nApplying updates...')

    fixed = sum(len(v) for v in suburb_updates.values())
    for slug, ids in suburb_updates.items():
        _bulk_update(supabase, ids, {'suburb_normalised': slug})

    reactivated = sum(len(v) for v in reactivate_updates.values())
    for slug, ids in reactivate_updates.items():
        _bulk_update(supabase, ids, {'suburb_normalised': slug, 'is_active': True})

    deactivated          = _bulk_update(supabase, deactivate_ids, {'is_active': False})
    deactivated_no_price = _bulk_update(supabase, no_price_ids,   {'is_active': False})
    deactivated_commercial = _bulk_update(supabase, commercial_ids, {'is_active': False})

    price_gone_ids = [r['listing_id'] for r in price_gone if r.get('listing_id')]
    deactivated_price_gone = _bulk_update(supabase, price_gone_ids, {'is_active': False})

    # v6: normalise studios/bachelor/0.5-bed listings to bedrooms=0, apartment.
    # Only touch active listings whose stored bedrooms/type isn't already the
    # canonical (0, apartment), so we don't rewrite rows needlessly.
    studio_ids = [
        r['listing_id'] for r in active_listings
        if is_studio(r.get('title'))
        and not (str(r.get('bedrooms')) == '0'
                 and (r.get('property_type') or '') == 'apartment')
    ]
    studios_normalised = _bulk_update(
        supabase, studio_ids, {'bedrooms': 0, 'property_type': 'apartment'})

    # PATH 2 REMOVED: previously this reactivated "good-but-inactive" listings.
    # Manual testing showed ~3,000 of them are gone from the market but still hold
    # a stale price, so reactivating them revived dead listings. We now leave them
    # inactive and only report the count below.
    reactivated_good = 0  # intentionally not reactivating any

    # v6: flag inactive RENTALS as reference evidence. They STAY is_active=False
    # (never shown to users) but is_reference=True lets the investment rent model
    # use their historical price. compute_investment.py applies a recency window.
    referenced = _bulk_update(supabase, reference_rental_ids, {'is_reference': True})

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f'\n=== SUBURB FIX COMPLETE ===')
    print(f'Fixed suburb (active):   {fixed}')
    print(f'Reactivated:             {reactivated}  (inactive → suburb found → active)')
    print(f'Good-but-inactive LEFT OFF: {len(good_but_inactive_ids)}  (price+suburb+type ok but NOT reactivated — likely gone-but-stale-price)')
    print(f'Flagged as REFERENCE:    {referenced}  (inactive rentals → is_reference=TRUE for rent model; still hidden from users)')
    print(f'Studios normalised:      {studios_normalised}  (studio/0.5/bachelor → bedrooms=0, apartment)')
    print(f'Deactivated (no suburb): {deactivated}')
    print(f'Deactivated (no price):  {deactivated_no_price}')
    print(f'Deactivated (commercial):{deactivated_commercial}')
    print(f'Deactivated (price gone):{deactivated_price_gone}  (had price_at_first, now price=NULL)')
    print(f'Skipped:                 {skipped}')
    print(f'Total processed:         {len(active_listings) + len(inactive_listings)}')

    if failed_labels:
        print(f'\nCould not fix (deactivated):')
        for entry in failed_labels[:20]:
            print(f'  {entry}')
        if len(failed_labels) > 20:
            print(f'  ... and {len(failed_labels) - 20} more')

    # ── Price-gone report ─────────────────────────────────────────────────────
    # Listings where price IS NULL but price_at_first IS NOT NULL.
    # This indicates a property that was once priced but has since had its
    # price removed — typically because it became unavailable, was let/sold,
    # or its URL changed (e.g. pamgolding to-rent → let mutation).
    if price_gone:
        print(f'\n=== PRICE-GONE REPORT ({len(price_gone)} listings) ===')
        print(f'  These listings had a known price when first seen but now have no price.')
        print(f'  Possible causes: property let/sold, URL changed, page restructured.\n')
        print(f'  {"Source":<18} {"Type":<6} {"First Price":>12}  {"Suburb":<22} {"Status":<18} {"Last Seen":<20}  Title / URL')
        print(f'  {"-"*18} {"-"*6} {"-"*12}  {"-"*22} {"-"*18} {"-"*20}  ------')

        by_source: dict[str, list[dict]] = {}
        for row in price_gone:
            src = row.get('source') or 'unknown'
            by_source.setdefault(src, []).append(row)

        for src in sorted(by_source):
            rows = by_source[src]
            for row in rows:
                lid        = row.get('listing_id', '')
                ltype      = (row.get('listing_type') or 'sale')[:6]
                first_px   = row.get('price_at_first')
                suburb     = (row.get('suburb_normalised') or '—')[:22]
                status     = (row.get('price_status') or '—')[:18]
                last_seen  = (row.get('last_seen') or '—')[:20]
                title      = (row.get('title') or '')[:60]
                url        = row.get('listing_url') or ''
                first_px_str = f'R{first_px:,}' if first_px else '—'
                print(f'  {src:<18} {ltype:<6} {first_px_str:>12}  {suburb:<22} {status:<18} {last_seen:<20}  {title}')
                if url:
                    print(f'  {"":18} {"":6} {"":12}  {"":22} {"":18} {"":20}  {url}')

        print(f'\n  Summary by source:')
        for src in sorted(by_source):
            rows = by_source[src]
            total_first = sum(r.get('price_at_first') or 0 for r in rows)
            print(f'    {src:<20} {len(rows):>5} listing(s)  (total first-seen value: R{total_first:,})')
        print()


if __name__ == '__main__':
    run_suburb_fix()
