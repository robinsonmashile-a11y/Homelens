"""
Browser-side reader for listing detail pages: description, floor/erf size,
beds/baths/parking and the extras (see scraper/detail.py for the parsing).

    fields = await read_detail_from_page(page, SELECTORS)
    apply_details(listing, fields)

Optional SELECTORS keys (a generic reader covers every site without them):
  info_rows / info_label / info_value
        CSS for the site's label/value rows (floor size, erf, levy ...).
        dt/dd pairs, two-cell table rows and "Label: value" lines are always read too.
  feature_blocks
        CSS for feature-chip blocks (a heading <span> + .features-icons spans).
        Feeds parking / furnished / security / views / pets. Defaults to the
        Greeff-platform blocks; other sites only get these from the description
        (scanning a whole page for chips would pick up filter menus).
  description            CSS for the description (already used by the cost fallback)
  description_fallbacks  extra CSS tried when `description` finds nothing
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from .detail import DETAIL_FIELDS, parse_detail_fields
from .utils import clean_personal_info

logger = logging.getLogger('homelens.detail_page')

MIN_DESCRIPTION_CHARS = 40
MAX_DESCRIPTION_CHARS = 5000

_GENERIC_DESCRIPTION_CSS = (
    '[itemprop="description"], .property-description, .listing-description, '
    '[class*="description"] p'
)

# Costs are owned by apply_costs (bounds + provenance); a card value is kept
# for these three when the search card already had one.
# The Greeff/Sothebys/Tyson/... platform's chip blocks; matches nothing elsewhere.
_PLATFORM_BLOCKS = '.listing-details-features .features-block'
_BOILERPLATE = re.compile(r'^take a look at this property|discover more about its features', re.I)
_COST_KEYS = ('levy_monthly', 'rates_monthly')
_FILL_ONLY = ('size_m2', 'bedrooms', 'bathrooms')

_JS_ROWS = r"""(cfg) => {
    const txt = e => (e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim();
    const rows = [];
    if (cfg.rows) document.querySelectorAll(cfg.rows).forEach(r => {
        const l = cfg.label ? r.querySelector(cfg.label) : null;
        const v = cfg.value ? r.querySelector(cfg.value) : null;
        if (l && v) rows.push([txt(l), txt(v)]);
    });
    document.querySelectorAll('dt').forEach(dt => {
        const dd = dt.nextElementSibling;
        if (dd && dd.tagName === 'DD') rows.push([txt(dt), txt(dd)]);
    });
    document.querySelectorAll('tr').forEach(tr => {
        if (tr.children.length === 2) rows.push([txt(tr.children[0]), txt(tr.children[1])]);
    });
    document.querySelectorAll('li, p, div, span').forEach(e => {
        if (e.children.length > 3) return;
        const t = txt(e);
        if (t.length > 60) return;
        const m = t.match(/^([A-Za-z][A-Za-z \/&()m²]{2,28}):\s*(.{1,40})$/);
        if (m) rows.push([m[1], m[2]]);
    });
    return rows;
}"""

_JS_BLOCKS = r"""(css) => {
    const out = {};
    document.querySelectorAll(css).forEach(b => {
        const h = ((b.querySelector(':scope > span') || {}).textContent || 'other').trim();
        out[h] = Array.from(b.querySelectorAll('.features-icons span'))
            .filter(s => !s.querySelector('span') || s.classList.contains('carports-text'))
            .map(s => s.textContent.replace(/\s+/g, ' ').trim());
    });
    return out;
}"""


async def _read_description(page, sel: dict) -> str | None:
    for css in (sel.get('description'), sel.get('description_fallbacks'), _GENERIC_DESCRIPTION_CSS):
        if not css:
            continue
        try:
            els = await page.query_selector_all(css)
            texts = []
            for el in els[:6]:
                t = ((await el.inner_text()) or '').strip()
                if t:
                    texts.append(t)
        except Exception:
            continue
        text = '\n'.join(dict.fromkeys(texts))
        if len(text) >= MIN_DESCRIPTION_CHARS:
            return text
    try:
        meta = await page.get_attribute('meta[property="og:description"]', 'content', timeout=1000)
    except Exception:
        meta = None
    meta = (meta or '').strip()
    if len(meta) < MIN_DESCRIPTION_CHARS or _BOILERPLATE.search(meta):
        return None
    return meta


async def read_detail_from_page(page, sel: dict) -> dict:
    """Never raises; every field not found is None."""
    rows: list = []
    blocks: dict = {}
    try:
        rows = await page.evaluate(_JS_ROWS, {
            'rows':  sel.get('info_rows')  or sel.get('cost_rows'),
            'label': sel.get('info_label') or sel.get('cost_label'),
            'value': sel.get('info_value') or sel.get('cost_value'),
        })
    except Exception as e:
        logger.debug('detail rows failed: %s', e)
    try:
        blocks = await page.evaluate(_JS_BLOCKS, sel.get('feature_blocks') or _PLATFORM_BLOCKS)
    except Exception as e:
        logger.debug('feature blocks failed: %s', e)
    try:
        description = await _read_description(page, sel)
    except Exception as e:
        logger.debug('description failed: %s', e)
        description = None
    try:
        page_text = await page.evaluate('() => document.body ? document.body.innerText : ""')
    except Exception:
        page_text = None
    return parse_detail_fields([tuple(r) for r in rows], blocks, description, page_text)


def apply_details(listing: dict, fields: dict) -> None:
    """
    Copy detail fields onto a listing. Size/beds/baths already read from the
    search card are kept. A field the page did not have is left unset (so the
    database layer never overwrites a stored value with null).
    """
    for key in DETAIL_FIELDS:
        value = fields.get(key)
        if value is None or key in _COST_KEYS:
            continue
        if key in _FILL_ONLY and listing.get(key) is not None:
            continue
        if key == 'description':
            value = clean_personal_info(value[:MAX_DESCRIPTION_CHARS])
            if len(listing.get('description') or '') >= len(value):
                continue   # keep a longer description already read from the card/API
        listing[key] = value
    listing['details_checked_at'] = datetime.now(timezone.utc).isoformat()
