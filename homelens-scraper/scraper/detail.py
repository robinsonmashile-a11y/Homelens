"""
Detail-page field extraction shared by the scrapers (pure functions, no browser,
no database).

  parse_detail_fields(rows, blocks, description, page_text) -> dict

`rows`   [(label, value)] from the site's label/value table
         (Greeff: .listing-details-info-detail)
`blocks` {heading: [chip text, ...]} from the feature chips
         (Greeff: .features-block > span + .features-icons span)

Every key is always present in the result; a field that was not found is None.
log_missing() reports which fields were None for which listing.
"""
from __future__ import annotations

import logging
import re

from .extractors import parse_money

logger = logging.getLogger('homelens.detail')

DETAIL_FIELDS = (
    'description', 'size_m2', 'bedrooms', 'bathrooms', 'parking',
    'levy_monthly', 'rates_monthly', 'levy_includes',
    'erf_size', 'building_name',
    'pet_friendly', 'is_furnished', 'security', 'views',
)

# Fields whose absence is worth a per-listing log line (extras are often legitimately absent).
LOG_EXPECTED = ('description', 'size_m2', 'bedrooms', 'bathrooms', 'parking')

_NUM = r'(\d[\d\s,]*(?:\.\d+)?)'

_FLOOR_LABELS = {'floor size', 'floor area', 'building size', 'building area', 'internal size',
                 'internal area', 'unit size', 'house size', 'floor space', 'size of unit',
                 'floor size (m²)', 'floor area (m²)', 'floor size m²'}
_ERF_LABELS = {'erf size', 'erf', 'stand size', 'land size', 'plot size', 'land area',
               'erf area', 'stand area', 'erf size (m²)', 'land size (m²)'}


_A = r'~?\s*(\d[\d\s,]*(?:\.\d+)?)\s*(?:m²|m2|sqm|sq\.?\s?m)'
# Floor/living area, never plot/erf. Order = most specific first.
_FLOOR_TEXT = [
    re.compile(r'\b(?:floor|internal|building|unit|living|house)\s*(?:size|area|space)?\s*(?:\(sqm\))?[\s:\-]*' + _A, re.I),
    re.compile(_A + r'\s*(?:living\s+area|floor\s+(?:area|size|space)|interior|under\s+roof)', re.I),
    re.compile(r'\bsize\s*\(sqm\)\s*:?\s*(\d[\d,.]*)()', re.I),
]
_ERF_TEXT = [
    re.compile(r'\b(?:erf|stand|land|plot)\s*(?:size|area|surface)?[\s:\-]*' + _A, re.I),
    re.compile(_A + r'\s*(?:plot|erf|land|stand)\b', re.I),
]


def _split_glued(text: str | None) -> str:
    """'2 bathroomsBuilding 81 m²' -> '2 bathrooms Building 81 m²' (some sites render cards without spaces)."""
    text = (text or '').replace('\xa0', ' ')
    text = re.sub(r'(?<=\d)\s*m2\b', ' m²', text, flags=re.I)   # keep the unit intact: '523m2' -> '523 m²'
    text = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', text)
    return re.sub(r'(?<=[a-z])(?=\d)', ' ', text)


def _first_area(patterns, text: str) -> float | None:
    for pat in patterns:
        for m in pat.finditer(text):
            try:
                val = float(m.group(1).replace(' ', '').replace(',', ''))
            except ValueError:
                continue
            if 10 <= val <= 100_000:
                return val
    return None


def _yes_no(value: str) -> bool | None:
    v = (value or '').strip().lower()
    if re.match(r'^(yes|allowed|y|true|pets? (?:allowed|ok|welcome))', v):
        return True
    if re.match(r'^(no|not allowed|n|false|no pets)', v):
        return False
    return None


def _num(text: str) -> float | None:
    m = re.search(_NUM, (text or '').replace('\xa0', ' '))
    if not m:
        return None
    try:
        return float(m.group(1).replace(' ', '').replace(',', ''))
    except ValueError:
        return None


def _area(text: str) -> float | None:
    m = re.search(_NUM + r'\s*(?:m²|m2|sqm|sq\.?\s?m)', (text or '').replace('\xa0', ' '), re.I)
    if not m:
        return None
    try:
        return float(m.group(1).replace(' ', '').replace(',', ''))
    except ValueError:
        return None


def _clean(text: str | None) -> str | None:
    text = re.sub(r'[ \t\xa0]+', ' ', text or '')
    text = re.sub(r'\n\s*\n+', '\n', text).strip()
    return text or None


def parse_detail_fields(rows: list[tuple[str, str]],
                        blocks: dict[str, list[str]],
                        description: str | None,
                        page_text: str | None = None) -> dict:
    out: dict = {k: None for k in DETAIL_FIELDS}
    out['description'] = _clean(description)

    # ── label/value table ────────────────────────────────────────────────
    for label, value in rows:
        key = ' '.join((label or '').lower().replace('\xa0', ' ').split()).rstrip(':').strip()
        if key in _FLOOR_LABELS and out['size_m2'] is None:
            out['size_m2'] = _area(value)
        elif key in _ERF_LABELS and out['erf_size'] is None:
            out['erf_size'] = _area(value)
        elif key in ('monthly levy', 'levy', 'levies', 'sectional title levy', 'body corporate levy') \
                and out['levy_monthly'] is None:
            out['levy_monthly'] = parse_money(value)
        elif key in ('monthly rates', 'rates', 'rates & taxes', 'rates and taxes', 'municipal rates') \
                and out['rates_monthly'] is None:
            out['rates_monthly'] = parse_money(value)
        elif key in ('bedrooms', 'beds', 'bedroom') and out['bedrooms'] is None and _num(value) is not None:
            out['bedrooms'] = int(_num(value))
        elif key in ('bathrooms', 'baths', 'bathroom') and out['bathrooms'] is None:
            out['bathrooms'] = _num(value)
        elif key in ('parking', 'parking bays', 'garages', 'garage', 'parking spaces') \
                and out['parking'] is None and _num(value) is not None:
            out['parking'] = int(_num(value))
        elif key in ('pets', 'pets allowed', 'pet friendly') and out['pet_friendly'] is None:
            out['pet_friendly'] = _yes_no(value)
        elif key in ('furnished', 'furnishing') and out['is_furnished'] is None:
            if re.search(r'unfurnished|^no$', value.strip().lower()):
                out['is_furnished'] = False
            elif re.search(r'furnished|^yes$', value.strip().lower()):
                out['is_furnished'] = True
        elif key in ('building', 'building name', 'complex', 'complex name', 'development') \
                and out['building_name'] is None and 2 < len(value.strip()) < 80:
            out['building_name'] = value.strip()

    text = _split_glued(page_text)
    if out['erf_size'] is None and text:
        out['erf_size'] = _first_area(_ERF_TEXT, text)
    if out['size_m2'] is None and text:
        out['size_m2'] = _first_area(_FLOOR_TEXT[:2], text) or _first_area(_FLOOR_TEXT[:2], description or '')
        if out['size_m2'] is None:
            # Vague "Size (sqm): 523": the same number as the erf size means it is the plot.
            vague = _first_area(_FLOOR_TEXT[2:], text)
            if vague is not None and vague != out['erf_size']:
                out['size_m2'] = vague

    # ── feature chips ────────────────────────────────────────────────────
    chips = [c.strip() for items in blocks.values() for c in items if c and c.strip()]
    lower = [c.lower() for c in chips]

    for heading, items in blocks.items():
        for chip in items:
            c = chip.lower()
            if out['size_m2'] is None and re.match(r'floor\s+(?:size|area)', c):
                out['size_m2'] = _area(chip)
            if out['erf_size'] is None and re.match(r'(?:erf|stand|land|plot)\s+size', c):
                out['erf_size'] = _area(chip)

    for c, raw in zip(lower, chips):
        if out['bedrooms'] is None and re.search(r'\bbedrooms?\b', c) and not c.startswith('main'):
            out['bedrooms'] = int(_num(raw)) if _num(raw) is not None else None
        elif out['bathrooms'] is None and re.search(r'\bbathrooms?\b', c):
            out['bathrooms'] = _num(raw)
        elif re.match(r'\d+\s+(?:parking|garage|carport|covered)', c):
            out['parking'] = (out['parking'] or 0) + int(_num(raw))   # "4 Garages" + "15 Parkings"

    if out['parking'] is None and text:
        m = re.search(r'\b(\d{1,2})\s*(?:garages?|parking(?:\s+(?:bays?|spaces?))?|carports?)\b', text, re.I)
        if m:
            out['parking'] = int(m.group(1))
    if out['parking'] is None:
        # "1 Dedicated parking bay" / "2 secure parking bays" in the description
        m = re.search(r'\b(\d+|one|two|three|four)\s+(?:\w+\s+){0,2}(?:parking|garage|carport)', (description or ''), re.I)
        if m:
            words = {'one': 1, 'two': 2, 'three': 3, 'four': 4}
            tok = m.group(1).lower()
            out['parking'] = words.get(tok) or int(tok)

    haystack = ' | '.join(lower)
    desc_lc = (description or '').lower()

    # Chips are authoritative: only claim True when stated, never infer False.
    if re.search(r'\bpets?\b.*\ballowed\b|\bpet[\s-]?friendly\b|\bpets?\s+(?:ok|welcome)\b', haystack + ' | ' + desc_lc) \
            and not re.search(r'\bno\s+pets\b', haystack + ' ' + desc_lc):
        out['pet_friendly'] = True
    elif re.search(r'\bno\s+pets\b|\bpets?\s+not\s+allowed\b', haystack + ' ' + desc_lc):
        out['pet_friendly'] = False

    if re.search(r'(?<!un)(?<!semi-)\bfurnished\b|\bsemi[\s-]?furnished\b', haystack) \
            and not re.search(r'\bunfurnished\b', haystack):
        out['is_furnished'] = True
    elif re.search(r'\bunfurnished\b', haystack):
        out['is_furnished'] = False
    elif re.search(r'\bsemi[\s-]?furnished\b|\bfully\s+furnished\b', desc_lc):
        out['is_furnished'] = True

    sec = [raw for c, raw in zip(lower, chips)
           if re.search(r'security|alarm|electric fenc|cctv|access control|24[\s-]?hour', c)]
    sec += [m.group(0).strip() for m in re.finditer(
        r'24[\s-]?hour\s+(?:security|access control|guard)|security\s+(?:complex|estate|gate|guard|system|village)'
        r'|electric(?:\s+perimeter)?\s+fenc\w*|\bcctv\b|controlled access|access control|alarm system|\bboomed\b',
        desc_lc)]
    if sec:
        out['security'] = ', '.join(dict.fromkeys(sec))

    views = [raw for c, raw in zip(lower, chips) if re.search(r'\bviews?\b', c) and c != 'scenery / views']
    for pat in (r'(?:sea|ocean|mountain|city|harbour|table mountain|lion.s head|bay)\b[^.,;]{0,25}\bviews?\b',):
        views += re.findall(pat, desc_lc)
    sea_chip = [raw for c, raw in zip(lower, chips) if c in ('sea', 'mountain', 'city', 'ocean', 'harbour')]
    views += [f'{s} view' for s in sea_chip]
    if views:
        out['views'] = ', '.join(dict.fromkeys(v.strip() for v in views))[:200]

    m = re.search(r'levy\s+(?:includes?|covers?|inclusive of)\s*[:\-]?\s*([^.\n]{3,120})', desc_lc)
    if m:
        out['levy_includes'] = m.group(1).strip()

    m = re.search(r'\b(?:in|within)\s+the\s+(?:sought-after\s+|popular\s+|exclusive\s+)?'
                  r'([A-Z][\w\'’&-]+(?:\s+[A-Z0-9][\w\'’&-]*){0,3})\s+'
                  r'(?:development|complex|building|estate|residence|apartments?|tower)',
                  description or '')
    if m:
        out['building_name'] = m.group(1).strip()

    return out


def log_missing(source: str, listing_id: str, fields: dict, expected: tuple = DETAIL_FIELDS) -> list[str]:
    missing = [k for k in expected if fields.get(k) is None]
    if missing:
        logger.info('MISSING %s %s: %s', source, listing_id, ', '.join(missing))
    return missing
