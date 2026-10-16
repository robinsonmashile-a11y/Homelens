"""
Property-type classification from listing text (title, or a site's own type
label such as Quay1's "Sectional Title · Camps Bay").

Shared by the engine (populate_property_types) and scrapers that have a type
label on the card. Returns 'apartment' | 'townhouse' | 'house' | 'other';
'other' means farms, land, commercial or text we could not place.
"""
from __future__ import annotations

import re

# Order matters: townhouse before house ("townhouse" contains "house").
PROPERTY_RULES: list[tuple[str, list[str]]] = [
    ('apartment', ['apartment', 'flat', 'penthouse', 'studio', 'sectional title',
                   'bachelor', 'loft']),
    ('townhouse', ['townhouse', 'duplex', 'triplex', 'cluster']),
    ('house',     ['house', 'freehold', 'villa', 'cottage', 'freestanding',
                   'free-standing', 'free standing', 'detached', 'family home',
                   'double storey', 'double-storey', 'single storey',
                   'single-storey', 'home']),
]

# Words that mean the listing is not a home, even if it has bedrooms
# ("Farm in Philadelphia", "Guest Lodge", "Vacant land").
_NON_RESIDENTIAL_RE = re.compile(
    r'\b(farm|vacant|land|plot|erf|commercial|industrial|office|retail|warehouse|'
    r'smallholding|parking|garage|shop|business|hotel|lodge|storage|development site)\b',
    re.I)
_RESIDENTIAL_WINS = ('apartment', 'flat', 'penthouse', 'townhouse', 'sectional title')


def classify_property_type(text: str | None) -> str:
    t = (text or '').lower()
    if _NON_RESIDENTIAL_RE.search(t) and not any(k in t for k in _RESIDENTIAL_WINS):
        return 'other'                       # farms, land, commercial win over "home"
    for ptype, keywords in PROPERTY_RULES:
        if any(kw in t for kw in keywords):
            return ptype
    return 'other'


def looks_non_residential(title: str | None) -> bool:
    return bool(_NON_RESIDENTIAL_RE.search(title or ''))
