from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from supabase import create_client, Client

logger = logging.getLogger('homelens.database')

# ── Size caps by property type ─────────────────────────────────────────────────
# Apartments over 400m² and houses over 2000m² are almost certainly using
# ERF/plot size rather than floor size.  Null these out to prevent bad
# price-per-m² calculations in the engine.

_SIZE_CAPS: dict[str, float] = {
    'apartment':  400.0,
    'townhouse':  600.0,
    'house':     2000.0,
}


_IMAGE_BLOCKLIST = ('logo', 'placeholder', 'default', 'avatar', 'icon', 'blank')


def validate_listing(listing: dict) -> dict:
    """
    Apply data-quality rules to a listing dict before upsert.

    Rules:
      - If size_m2 exceeds the type-specific cap, null it out along with
        price_per_m2.  This catches ERF/plot sizes being stored as floor sizes.
      - city defaults to 'cape-town' when the scraper did not set one.
      - image_url must be a valid http/https URL without placeholder keywords.
        Empty strings are converted to None.

    Modifies in-place and returns the same dict.
    """
    # Scrapers that only cover Cape Town (duarte, gumtree) don't set a city.
    listing.setdefault('city', 'cape-town')

    ptype  = (listing.get('property_type') or '').lower()
    size   = listing.get('size_m2')
    cap    = _SIZE_CAPS.get(ptype)

    if size is not None and cap is not None and size > cap:
        logger.warning(
            'validate_listing: %s size_m2=%.0f exceeds cap %.0f for %s — nulled',
            listing.get('listing_id', '?'), size, cap, ptype,
        )
        listing['size_m2']      = None
        listing['price_per_m2'] = None

    # Validate image_url — must be http/https and not a placeholder/logo
    image_url = listing.get('image_url')
    if image_url is not None:
        url = (image_url or '').strip()
        if not url.startswith(('http://', 'https://')) or any(
            kw in url.lower() for kw in _IMAGE_BLOCKLIST
        ):
            listing['image_url'] = None

    return listing


def get_client() -> Client:
    return create_client(
        os.environ['SUPABASE_URL'],
        os.environ['SUPABASE_KEY'],
    )


# ── Listing status + monthly costs ────────────────────────────────────────────

_COST_FIELDS = ('levy_monthly', 'rates_monthly', 'levies_rates_combined_monthly')


def _merge_status_and_costs(listing: dict, old: dict, update_data: dict, now: str) -> None:
    """
    Write rules for listing_status and the levy/rates fields on an existing row.

    Status: written on every run. When it differs from the stored value the
    change is recorded in status_changed_at (live -> under_offer/sold, and any
    other transition, e.g. under_offer -> live when a deal falls through).

    Costs: a figure is only ever written when this run found a real number.
    A run that finds nothing leaves the stored levy/rates untouched — a real
    number is never overwritten with null.
    """
    new_status = listing.get('listing_status')
    if new_status:
        update_data['listing_status'] = new_status
        if listing.get('status_source'):
            update_data['status_source'] = listing['status_source']
        if new_status != (old.get('listing_status') or 'live'):
            update_data['status_changed_at'] = now

    found_any = False
    for field in _COST_FIELDS:
        value = listing.get(field)
        if value is not None:
            update_data[field] = value
            found_any = True
    if found_any:
        update_data['costs_source'] = listing.get('costs_source')
        update_data['costs_raw_snippet'] = listing.get('costs_raw_snippet')
    if listing.get('costs_checked_at'):
        update_data['costs_checked_at'] = listing['costs_checked_at']


def get_costs_checked_ids(source: str, max_age_days: int = 30) -> set[str]:
    """
    listing_ids of `source` whose levy/rates were checked within max_age_days.
    The scrapers skip the per-listing page visit for these. Paginated because
    Supabase caps a single response at 1000 rows.
    """
    client = get_client()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    ids: set[str] = set()
    offset, page_size = 0, 1000
    while True:
        rows = (
            client.table('listings')
            .select('listing_id')
            .eq('source', source)
            .gte('costs_checked_at', cutoff)
            .range(offset, offset + page_size - 1)
            .execute()
        ).data or []
        ids.update(r['listing_id'] for r in rows)
        if len(rows) < page_size:
            return ids
        offset += page_size


def upsert_listing(listing: dict) -> str:
    """
    Insert new listing or update existing one.
    Returns 'new', 'updated', or 'unchanged'.
    """
    listing = validate_listing(listing)
    client = get_client()

    existing = (
        client.table('listings')
        .select('listing_id, price, price_history, first_seen, listing_status')
        .eq('listing_id', listing['listing_id'])
        .execute()
    )

    now = datetime.now(timezone.utc).isoformat()

    if not existing.data:
        listing['price_at_first'] = listing.get('price')
        listing['price_history'] = [{'price': listing.get('price'), 'date': now}]
        listing['first_seen'] = now
        listing['last_seen'] = now
        listing['created_at'] = now
        listing['updated_at'] = now
        listing['last_scraped_at'] = now
        if listing.get('listing_status') not in (None, 'live'):
            listing['status_changed_at'] = now
        client.table('listings').insert(listing).execute()
        return 'new'

    old = existing.data[0]
    update_data = {
        'last_seen':       now,
        'is_active':       True,
        'updated_at':      now,
        'last_scraped_at': now,
    }

    # Always propagate image_url if the scraper found one
    if listing.get('image_url'):
        update_data['image_url'] = listing['image_url']

    _merge_status_and_costs(listing, old, update_data, now)

    # Always propagate price_status when the scraper has a definitive answer.
    # 'unknown' means the selector found nothing (transient); don't overwrite a
    # better status from a previous run with a transient unknown.
    new_status = listing.get('price_status')
    if new_status and new_status != 'unknown':
        update_data['price_status'] = new_status

    new_price = listing.get('price')
    old_price  = old.get('price')

    # Case 1: new scrape found a real price that differs from what's stored
    if new_price and new_price != old_price:
        history = old.get('price_history') or []
        history.append({'price': new_price, 'date': now})
        update_data['price'] = new_price
        update_data['price_history'] = history

        first_seen_str = old.get('first_seen', now)
        try:
            first_seen_dt = datetime.fromisoformat(first_seen_str.replace('Z', '+00:00'))
            days = (datetime.now(timezone.utc) - first_seen_dt).days
            update_data['days_on_market'] = days
        except (ValueError, AttributeError):
            pass

        client.table('listings').update(update_data).eq('listing_id', old['listing_id']).execute()
        return 'updated'

    # Case 2: listing became POA — clear stored price so it can't corrupt baselines
    if new_status == 'poa' and old_price is not None:
        update_data['price'] = None
        client.table('listings').update(update_data).eq('listing_id', old['listing_id']).execute()
        return 'updated'

    client.table('listings').update(update_data).eq('listing_id', old['listing_id']).execute()
    return 'unchanged'


def upsert_listing_by_url(listing: dict) -> str:
    """
    Insert or update a listing keyed on listing_url (used for RE/MAX and
    other sources where the URL is the most stable unique identifier).
    Returns 'new', 'updated', or 'unchanged'.
    """
    listing = validate_listing(listing)
    client = get_client()

    existing = (
        client.table('listings')
        .select('listing_id, price, price_history, first_seen, listing_status')
        .eq('listing_url', listing['listing_url'])
        .execute()
    )

    now = datetime.now(timezone.utc).isoformat()

    if not existing.data:
        listing['price_at_first'] = listing.get('price')
        listing['price_history'] = [{'price': listing.get('price'), 'date': now}]
        listing['first_seen'] = now
        listing['last_seen'] = now
        listing['created_at'] = now
        listing['updated_at'] = now
        listing['last_scraped_at'] = now
        if listing.get('listing_status') not in (None, 'live'):
            listing['status_changed_at'] = now
        client.table('listings').insert(listing).execute()
        return 'new'

    old = existing.data[0]
    update_data = {
        'last_seen':       now,
        'is_active':       True,
        'updated_at':      now,
        'last_scraped_at': now,
    }

    # Always propagate image_url if the scraper found one
    if listing.get('image_url'):
        update_data['image_url'] = listing['image_url']

    _merge_status_and_costs(listing, old, update_data, now)

    new_status = listing.get('price_status')
    if new_status and new_status != 'unknown':
        update_data['price_status'] = new_status

    new_price = listing.get('price')
    old_price  = old.get('price')

    if new_price and new_price != old_price:
        history = old.get('price_history') or []
        history.append({'price': new_price, 'date': now})
        update_data['price'] = new_price
        update_data['price_history'] = history

        first_seen_str = old.get('first_seen', now)
        try:
            first_seen_dt = datetime.fromisoformat(first_seen_str.replace('Z', '+00:00'))
            days = (datetime.now(timezone.utc) - first_seen_dt).days
            update_data['days_on_market'] = days
        except (ValueError, AttributeError):
            pass

        client.table('listings').update(update_data).eq('listing_id', old['listing_id']).execute()
        return 'updated'

    if new_status == 'poa' and old_price is not None:
        update_data['price'] = None
        client.table('listings').update(update_data).eq('listing_id', old['listing_id']).execute()
        return 'updated'

    client.table('listings').update(update_data).eq('listing_id', old['listing_id']).execute()
    return 'unchanged'


MAX_STALE_FRACTION = 0.25     # never deactivate more than this share of a source in one run
MIN_ACTIVE_FOR_BREAKER = 40   # ...once a source is big enough for the ratio to mean anything


def mark_inactive_listings(source: str, active_ids: list, cities: list | None = None) -> None:
    """
    Mark listings not seen in this scrape run as inactive.

    Computes the stale set locally (DB active IDs for this source, minus
    today's active_ids) and deactivates only that subset, in small batches.
    This avoids passing the full active_ids list into a single .not_.in_()
    filter, which silently fails once a source has enough listings to push
    the request past PostgREST/Supabase's URL length limits (observed on
    Tyson at ~500+ active listings — the old version stopped deactivating
    anything for that source without raising a visible error).
    """
    if not active_ids:
        return
    client = get_client()

    # When a run covers only some cities (per-city CI jobs), only those cities'
    # listings may be deactivated — the rest were simply not scraped.
    # PostgREST caps a response at 1000 rows, so page through. Without this,
    # sources with >1000 active listings (pamgolding, exprealty) never had
    # their older listings deactivated — they became ghosts.
    db_active_ids: set[str] = set()
    page_size, offset = 1000, 0
    while True:
        query = (
            client.table('listings')
            .select('listing_id')
            .eq('source', source)
            .eq('is_active', True)
            .order('listing_id')
            .range(offset, offset + page_size - 1)
        )
        if cities:
            query = query.in_('city', cities)
        rows = query.execute().data
        db_active_ids.update(row['listing_id'] for row in rows)
        if len(rows) < page_size:
            break
        offset += page_size
    stale_ids = db_active_ids - set(active_ids)

    # Circuit breaker: a run that saw far fewer listings than we hold as active
    # is almost certainly incomplete (page timeout, block, site change), not a
    # genuine mass delisting. Deactivating then hides live listings.
    if (len(db_active_ids) >= MIN_ACTIVE_FOR_BREAKER
            and len(stale_ids) > MAX_STALE_FRACTION * len(db_active_ids)
            and not os.environ.get('FORCE_MARK_INACTIVE')):
        logger.error(
            'mark_inactive_listings: %s — REFUSING to deactivate %d of %d active '
            '(>%d%%): run looks incomplete. Set FORCE_MARK_INACTIVE=1 to override.',
            source, len(stale_ids), len(db_active_ids), int(MAX_STALE_FRACTION * 100),
        )
        return

    if not stale_ids:
        logger.info('mark_inactive_listings: %s — nothing stale', source)
        return

    stale_list = list(stale_ids)
    batch = 200
    deactivated = 0
    for i in range(0, len(stale_list), batch):
        chunk = stale_list[i:i + batch]
        try:
            (
                client.table('listings')
                .update({'is_active': False})
                .eq('source', source)
                .in_('listing_id', chunk)
                .execute()
            )
            deactivated += len(chunk)
        except Exception as e:
            logger.warning(
                'mark_inactive_listings: %s — batch %d failed (%d ids): %s',
                source, i // batch, len(chunk), e,
            )

    logger.info(
        'mark_inactive_listings: %s — deactivated %d of %d stale listings',
        source, deactivated, len(stale_list),
    )


def cleanup_stale_signals(table: str, valid_listing_ids: set) -> None:
    """
    Remove rows from a derived table (listing_signals or listing_investment)
    whose listing_id is no longer in the current valid/eligible set — e.g.
    because the listing was deactivated, reclassified as an outlier, or
    dropped out of scope after signals/investment rows were already computed
    for it. Prevents stale rows leaking through to the app after upstream
    listings are deactivated post-hoc.

    Mirrors mark_inactive_listings: fetch existing IDs, diff locally, delete
    only the stale subset in small batches (avoids the same URL-length
    failure mode that broke mark_inactive_listings on large sources).
    """
    if not valid_listing_ids:
        logger.warning('cleanup_stale_signals: %s — valid_listing_ids is empty, '
                        'refusing to run (would delete everything)', table)
        return

    client = get_client()
    existing = client.table(table).select('listing_id').execute()
    db_ids = {row['listing_id'] for row in existing.data}
    stale_ids = db_ids - set(valid_listing_ids)

    if not stale_ids:
        logger.info('cleanup_stale_signals: %s — nothing stale', table)
        return

    stale_list = list(stale_ids)
    batch = 200
    removed = 0
    for i in range(0, len(stale_list), batch):
        chunk = stale_list[i:i + batch]
        try:
            client.table(table).delete().in_('listing_id', chunk).execute()
            removed += len(chunk)
        except Exception as e:
            logger.warning('cleanup_stale_signals: %s batch failed: %s', table, e)

    logger.info('cleanup_stale_signals: %s — removed %d of %d stale rows',
                table, removed, len(stale_list))


def log_scrape_run(run_data: dict) -> int:
    """Insert a scrape_runs row and return the new row id."""
    client = get_client()
    result = client.table('scrape_runs').insert(run_data).execute()
    return result.data[0]['id']


def update_scrape_run(run_id: int, data: dict) -> None:
    client = get_client()
    client.table('scrape_runs').update(data).eq('id', run_id).execute()


def write_audit_log(
    run_id: str,
    source: str,
    listing_type: str,
    listings: list[dict],
    *,
    root_cause_bucket: str | None = None,
    expected_count_manual: int | None = None,
    expected_tolerance_pct: float = 10.0,
    notes: str | None = None,
    scraper_code_version: str | None = None,
    run_started_at: str | None = None,
) -> None:
    """
    Insert one row into scrape_audit_log for (source, listing_type).

    Call once per scraper run after all listings have been yielded/collected.
    `listings` is the list of dicts that were passed to upsert_listing —
    each must contain a 'price_status' key (set by parse_price).

    The function counts price_status values from the listings list and
    computes within_tolerance against expected_count_manual if provided.
    """
    price_ok    = sum(1 for l in listings if l.get('price_status') == 'ok')
    price_poa   = sum(1 for l in listings if l.get('price_status') == 'poa')
    price_fail  = sum(1 for l in listings if l.get('price_status') == 'extraction_failed')
    price_unk   = sum(1 for l in listings if l.get('price_status') in ('unknown', None))
    extracted   = len(listings)

    within_tolerance = None
    if expected_count_manual is not None and extracted > 0:
        lo = expected_count_manual * (1 - expected_tolerance_pct / 100)
        hi = expected_count_manual * (1 + expected_tolerance_pct / 100)
        within_tolerance = lo <= extracted <= hi

    row = {
        'run_id':                           run_id,
        'source':                           source,
        'listing_type':                     listing_type,
        'run_started_at':                   run_started_at or datetime.now(timezone.utc).isoformat(),
        'scraper_code_version':             scraper_code_version,
        'root_cause_bucket':               root_cause_bucket,
        'listings_attempted':               extracted,
        'listings_extracted':               extracted,
        'price_ok_count':                   price_ok,
        'price_poa_count':                  price_poa,
        'price_extraction_failed_count':    price_fail,
        'price_unknown_count':              price_unk,
        'expected_count_manual':            expected_count_manual,
        'expected_tolerance_pct':           expected_tolerance_pct,
        'within_tolerance':                 within_tolerance,
        'notes':                            notes,
    }
    try:
        client = get_client()
        client.table('scrape_audit_log').insert(row).execute()
    except Exception as exc:
        logger.warning('write_audit_log failed for %s/%s: %s', source, listing_type, exc)
