"""
Glue between each scraper's SELECTORS block and the shared fallbacks in
extractors.py. Adds two fields to every listing:

  listing_status / status_source   'live' | 'under_offer' | 'sold'   — read from
      the search-result card on every run (no extra page load).
  levy_monthly / rates_monthly / levies_rates_combined_monthly /
  costs_source / costs_raw_snippet / costs_checked_at                 — read from
      the listing's own page, SALE listings only (rental levies/rates are the
      landlord's cost and are not shown).

Order for both fields: the site's own markup first, then the text fallback.
costs_source: 'site' | 'fallback' | 'description'.
status_source: 'site' | 'fallback' | 'default' (nothing on the card → 'live').

SELECTORS dict keys (one block at the top of each scraper):
  status_badge  CSS for elements inside a card whose text is a status label
  status_map    {lowercase badge text: 'under_offer' | 'sold'} for site wording
                beyond the shared phrases (e.g. RE/MAX "Offer Made")
  cost_rows     CSS for each label/value row on the listing page
  cost_label    CSS (relative to a row) for the label ("Monthly Levy")
  cost_value    CSS (relative to a row) for the value ("R2,267")
  description   CSS for the listing description (used by the description fallback)
  text_exclude  CSS for blocks to hide before the text fallback reads the page
                (e.g. a bond calculator whose default output looks like a levy)
  text_only     True when levy/rates only ever appear in free text (the run
                report then doesn't treat 'fallback' costs as a broken selector)
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime, timezone

from .extractors import (
    classify_cost_label,
    cost_in_bounds,
    extract_costs_from_text,
    extract_status_from_text,
    parse_money,
)

logger = logging.getLogger('homelens.enrich')

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)

DETAIL_SETTLE_SEC = 1.5          # let SSR/hydration finish after domcontentloaded
DETAIL_DELAY      = (2.0, 4.0)   # polite pause after each listing-page visit
DETAIL_TIMEOUT_MS = 45_000
COSTS_REFRESH_DAYS = 30          # re-check a listing's costs at most this often

# Wording sites use for a badge, beyond the phrases in extractors.py.
_BASE_STATUS_MAP = {
    'under offer': 'under_offer', 'offer pending': 'under_offer',
    'sale agreed': 'under_offer', 'under contract': 'under_offer',
    'sold': 'sold', 'sold stc': 'sold',
}
_RANK = {'live': 0, 'under_offer': 1, 'sold': 2}

_BLOCKED_ASSETS = '**/*.{png,jpg,jpeg,gif,webp,avif,svg,ico,woff,woff2,ttf,otf,mp4,webm}'

_EMPTY_COSTS = {
    'levy_monthly': None,
    'rates_monthly': None,
    'levies_rates_combined_monthly': None,
    'costs_source': None,
    'costs_raw_snippet': None,
}


# ── Status (search-result card) ──────────────────────────────────────────────

def resolve_status(badge_texts: list[str], text: str, sel: dict) -> tuple[str, str]:
    """
    Pure helper (no browser) → (listing_status, status_source).
    Site badge first, then the shared fallback on the card text.
    """
    status_map = {**_BASE_STATUS_MAP, **{k.lower(): v for k, v in (sel.get('status_map') or {}).items()}}
    site = 'live'
    for raw in badge_texts:
        key = ' '.join((raw or '').lower().split())
        found = status_map.get(key)
        if found and _RANK[found] > _RANK[site]:
            site = found
    if site != 'live':
        return site, 'site'

    fallback = extract_status_from_text(text, badge_texts)
    if fallback != 'live':
        return fallback, 'fallback'
    return 'live', 'default'


async def read_card_status(card, sel: dict) -> tuple[str, str]:
    """Read the status badge(s) on one result card. Never raises."""
    try:
        badge_texts: list[str] = []
        if sel.get('status_badge'):
            for el in await card.query_selector_all(sel['status_badge']):
                badge_texts.append((await el.inner_text()).strip())
        text = await card.inner_text()
        return resolve_status(badge_texts, text, sel)
    except Exception as e:
        logger.debug('status read failed: %s', e)
        return 'live', 'default'


# ── Costs (listing page) ─────────────────────────────────────────────────────

def _row_snippet(label: str, value_text: str) -> str:
    """'Monthly Levy: R2,267' — tidy even when the label and value share one element."""
    label_clean = ' '.join(label.replace('\xa0', ' ').split()).rstrip(':').strip()
    value_clean = ' '.join(value_text.replace('\xa0', ' ').replace(label, '', 1).split()).strip(': ')
    return (f'{label_clean}: {value_clean}' if value_clean else label_clean)[:80]


async def read_costs_from_page(page, sel: dict) -> dict:
    """
    Levy / rates from a listing page that is already loaded.
    1. the site's own label/value rows (sel cost_rows/cost_label/cost_value)
    2. the shared text fallback over the page text, then the description.
    """
    out = dict(_EMPTY_COSTS)

    if sel.get('cost_rows'):
        found: dict[str, float] = {}
        snippets: list[str] = []
        for row in await page.query_selector_all(sel['cost_rows']):
            label_el = await row.query_selector(sel['cost_label']) if sel.get('cost_label') else row
            value_el = await row.query_selector(sel['cost_value']) if sel.get('cost_value') else row
            if not label_el or not value_el:
                continue
            label = (await label_el.inner_text()).strip()
            value_text = (await value_el.inner_text()).strip()
            kind = classify_cost_label(label)
            if not kind or kind in found:
                continue
            amount = cost_in_bounds(kind, parse_money(value_text))
            if amount is None:
                continue
            found[kind] = amount
            snippets.append(_row_snippet(label, value_text))
        if found:
            out.update(
                levy_monthly=found.get('levy'),
                rates_monthly=found.get('rates'),
                levies_rates_combined_monthly=found.get('combined'),
                costs_source='site',
                costs_raw_snippet=' | '.join(snippets),
            )
            return out

    if sel.get('text_exclude'):
        # Hide blocks that are not the listing's own data (calculators whose
        # default output would read as a levy) before reading the page text.
        await page.evaluate(
            "css => document.querySelectorAll(css).forEach(e => { e.style.display = 'none'; })",
            sel['text_exclude'])
    page_text = await page.evaluate('() => document.body ? document.body.innerText : ""')
    description = ''
    if sel.get('description'):
        desc_el = await page.query_selector(sel['description'])
        if desc_el:
            description = (await desc_el.inner_text()).strip()
    return costs_from_texts(page_text, description, out)


def costs_from_texts(page_text: str | None, description: str | None,
                     base: dict | None = None) -> dict:
    """Fallback path used when the site markup gave nothing (also for API/JSON sites)."""
    out = dict(base or _EMPTY_COSTS)
    found = extract_costs_from_text(page_text, description)
    if found['matched_in']:
        out.update(
            levy_monthly=found['levy_monthly'],
            rates_monthly=found['rates_monthly'],
            levies_rates_combined_monthly=found['levies_rates_combined_monthly'],
            costs_source='fallback' if found['matched_in'] == 'page' else 'description',
            costs_raw_snippet=found['raw_snippet'],
        )
    return out


class CostEnricher:
    """
    Adds costs to sale listings by visiting their own page — but only listings
    that are new or whose costs were not checked in the last COSTS_REFRESH_DAYS.
    Existing values are never lost: the database layer keeps a stored figure
    when a run finds nothing (see scraper/database.py).

        enricher = CostEnricher('greeff', browser, SELECTORS)
        await enricher.start()
        ...
        await enricher.enrich(listing)     # mutates + returns the listing
        ...
        await enricher.close()

    `owner` is a Playwright Browser or BrowserContext.
    """

    def __init__(self, source: str, owner, selectors: dict):
        self.source = source
        self.owner = owner
        self.sel = selectors
        self.page = None
        self.checked: set[str] = set()
        self.visited = 0

    async def start(self) -> None:
        from .database import get_costs_checked_ids
        try:
            # RECHECK_COSTS=1 forces every sale listing to be visited again —
            # use it after fixing a site's selectors, so listings checked while
            # they were broken don't wait out the 30-day refresh.
            if not os.environ.get('RECHECK_COSTS'):
                self.checked = get_costs_checked_ids(self.source, COSTS_REFRESH_DAYS)
        except Exception as e:
            logger.warning('%s: could not load checked-costs ids (%s) — visiting all', self.source, e)
        owner = self.owner
        if hasattr(owner, 'new_context'):           # a Browser
            owner = await owner.new_context(
                user_agent=USER_AGENT, viewport={'width': 1280, 'height': 900},
                locale='en-ZA', timezone_id='Africa/Johannesburg',
            )
        self.page = await owner.new_page()
        await self.page.route(_BLOCKED_ASSETS, lambda route: route.abort())
        logger.info('%s: %d listings already have recently-checked costs', self.source, len(self.checked))

    async def enrich(self, listing: dict) -> dict:
        if listing.get('listing_type') != 'sale':
            return listing                          # levies/rates: sale listings only
        if listing.get('listing_id') in self.checked:
            return listing
        try:
            await self.page.goto(listing['listing_url'], wait_until='domcontentloaded',
                                 timeout=DETAIL_TIMEOUT_MS)
            await asyncio.sleep(DETAIL_SETTLE_SEC)
            costs = await read_costs_from_page(self.page, self.sel)
            apply_costs(listing, costs)
            self.visited += 1
        except Exception as e:
            # Not marked as checked → retried on the next run.
            logger.warning('%s: costs visit failed for %s: %s', self.source, listing.get('listing_url'), e)
        await asyncio.sleep(random.uniform(*DETAIL_DELAY))
        return listing

    async def close(self) -> None:
        logger.info('%s: visited %d listing pages for costs', self.source, self.visited)
        try:
            if self.page:
                await self.page.close()
        except Exception:
            pass


def apply_costs(listing: dict, costs: dict) -> None:
    """Copy a costs dict onto a listing and stamp costs_checked_at."""
    listing.update({k: costs.get(k) for k in _EMPTY_COSTS})
    listing['costs_checked_at'] = datetime.now(timezone.utc).isoformat()
