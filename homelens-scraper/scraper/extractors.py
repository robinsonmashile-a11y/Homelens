"""
Shared fallback extractors for listing status and monthly property costs.

Every scraper parses these from its own site markup first (the status badge,
the levy / rates cells) and only calls these text-based fallbacks when the
site-specific parse returns nothing.

  extract_status_from_text(page_text, badge_texts=None)
      -> 'live' | 'under_offer' | 'sold'

  extract_costs_from_text(page_text, description_text)
      -> {levy_monthly, rates_monthly, levies_rates_combined_monthly,
          raw_snippet, matched_in}
"""
from __future__ import annotations

import re

# ── Status ────────────────────────────────────────────────────────────────────

# Longest phrases first so "sold stc" wins over "sold".
_STATUS_PHRASES: list[tuple[str, str]] = [
    ('sold stc',       'sold'),
    ('sold',           'sold'),
    ('under offer',    'under_offer'),
    ('offer pending',  'under_offer'),
    ('sale agreed',    'under_offer'),
    ('under contract', 'under_offer'),
]
_PHRASE_RE = '|'.join(re.escape(p).replace(r'\ ', r'\s+') for p, _ in _STATUS_PHRASES)

# A badge / standalone line must consist of the phrase alone (surrounding
# punctuation allowed). This is what keeps "Sold out", "sold in 3 days" and
# "Pet friendly, under offer soon!" from matching.
_STANDALONE_RE = re.compile(rf'^[\W_]*({_PHRASE_RE})[\W_]*$', re.I)

# Sold beats under offer when both appear (a later stage of the same sale).
_STATUS_RANK = {'live': 0, 'under_offer': 1, 'sold': 2}


def _classify_phrase(text: str) -> str | None:
    m = _STANDALONE_RE.match(_normalise_spaces(text).strip())
    if not m:
        return None
    phrase = re.sub(r'\s+', ' ', m.group(1).lower())
    for p, status in _STATUS_PHRASES:
        if phrase == p:
            return status
    return None


def extract_status_from_text(page_text: str | None,
                             badge_texts: list[str] | None = None) -> str:
    """
    Classify a listing as 'live', 'under_offer' or 'sold'.

    Accepts only:
      - a badge element whose whole text is one of the phrases, or
      - a line of page_text that consists of the phrase alone.
    Never matches inside a sentence ("sold out", "sold in 3 days").

    Pass the listing's own text (card or detail body), not site chrome —
    a navigation link reading just "Sold" would otherwise count.
    """
    best = 'live'
    candidates = list(badge_texts or [])
    candidates += (page_text or '').splitlines()
    for text in candidates:
        if not text:
            continue
        status = _classify_phrase(text)
        if status and _STATUS_RANK[status] > _STATUS_RANK[best]:
            best = status
    return best


# ── Costs ─────────────────────────────────────────────────────────────────────

LEVY_BOUNDS     = (150.0, 25_000.0)
RATES_BOUNDS    = (50.0, 20_000.0)
COMBINED_BOUNDS = (LEVY_BOUNDS[0] + RATES_BOUNDS[0], LEVY_BOUNDS[1] + RATES_BOUNDS[1])

WINDOW = 60           # chars after a label in which the amount must start
SNIPPET_LEN = 80

_AND = r'\s*(?:and|&|\+|/)\s*'
_RATES_WORDS = r'(?:municipal\s+|council\s+)?rates(?:' + _AND + r'(?:taxes|refuse))?'

_COMBINED_LABEL = re.compile(
    r'\b(?:monthly\s+)?(?:levies|levy)' + _AND + _RATES_WORDS + r'\b'
    r'|\b(?:monthly\s+)?' + _RATES_WORDS + _AND + r'(?:levies|levy)\b',
    re.I,
)
_LEVY_LABEL = re.compile(
    r'\b(?:sectional\s+title\s+levy|monthly\s+levy|body\s+corporate(?:\s+(?:levy|levies|fees?))?'
    r'|body\s+corp\.?|hoa\s+fees?|levies|levy)\b',
    re.I,
)
_RATES_LABEL = re.compile(
    r'\b(?:rates' + _AND + r'taxes|rates' + _AND + r'refuse|municipal\s+rates'
    r'|council\s+rates|monthly\s+rates|rates)\b',
    re.I,
)
# Anything that starts a different row/field ends the current label's window.
_STOP_LABEL = re.compile(
    _COMBINED_LABEL.pattern + '|' + _LEVY_LABEL.pattern + '|' + _RATES_LABEL.pattern
    + r'|\x00|\btotal\b|\bprice\b|\bdeposit\b|\bbedrooms?\b|\bbathrooms?\b|\berf\b|\bfloor\b',
    re.I,
)
_TOTAL_BEFORE = re.compile(r'\btotal\b[^\n]{0,20}$', re.I)

_NUM = r'\d{1,3}(?:[ ,]\d{3})+(?:[.,]\d{1,2})?(?!\d)|\d+(?:[.,]\d{1,2})?(?!\d)'
# "R 1 000" / "R1,000.00" anywhere in the window …
_RAND_AMOUNT = re.compile(r'\b(?:R|ZAR)\s?(' + _NUM + r')', re.I)
# … or a bare "1000" directly after the label ("Levy: 1000", "Monthly Levy\n2267").
_BARE_AMOUNT = re.compile(r'^[\s:=\-–—()]*(' + _NUM + r')(?![\d%])(?!\s*(?:m²|m2|sqm|%|bed|bath))', re.I)
_MONTHLY_AFTER = re.compile(r'^\s*(?:pm\b|p\.m\.|p/m\b|per\s+month\b|/\s?(?:month|mo)\b|monthly\b|a\s+month\b)', re.I)
_UNIT_AFTER = re.compile(
    r'^\s*(?:per\s+month|per\s+annum|per\s+year|p\.?\s?m\.?(?!\w)|p\.?\s?a\.?(?!\w)'
    r'|/\s?(?:month|mo|yr|year)|monthly|annually|yearly)', re.I)
_YEAR = re.compile(r'(?:19|20)\d\d')
_ANNUAL = re.compile(r'\bp\.?\s?a\.?(?!\w)|\bper\s+annum\b|\bannually\b|\byearly\b|\bper\s+year\b|/\s?(?:yr|year)\b', re.I)

_SPACE_CHARS = dict.fromkeys(map(ord, '\u00a0\u2007\u2009\u200a\u202f\u2002\u2003'), ' ')


def _normalise_spaces(text: str) -> str:
    return text.translate(_SPACE_CHARS)


def _parse_amount(raw: str) -> float | None:
    s = raw.replace(' ', '')
    # A trailing ",dd" / ".dd" is a decimal part; every other comma groups thousands.
    m = re.match(r'^(.*?)[.,](\d{1,2})$', s)
    whole, frac = (m.group(1), m.group(2)) if m else (s, '')
    whole = whole.replace(',', '')
    if not whole.isdigit():
        return None
    return float(f'{whole}.{frac}') if frac else float(whole)


def _snippet(text: str, start: int, end: int) -> str:
    return re.sub(r'[\s\x00]+', ' ', text[start:min(end, start + SNIPPET_LEN)]).strip()


_MASK = '\x00'   # masked text also ends a label's search window (see _STOP_LABEL)


def _mask(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for s, e in spans:
        for i in range(s, e):
            if chars[i] != '\n':
                chars[i] = _MASK
    return ''.join(chars)


def _find_amount(text: str, label: re.Match,
                 bounds: tuple[float, float]) -> tuple[float, int] | None:
    """
    First in-bounds monthly amount starting within WINDOW chars of the label.
    Returns (monthly_value, index in text where the amount text ends).
    """
    after = label.end()
    window = text[after:after + WINDOW + 20]
    stop = _STOP_LABEL.search(window)
    if stop:
        window = window[:stop.start()]

    m = _BARE_AMOUNT.match(window)
    # "Rates 2023 valuation R 850 pm": a bare year is not an amount.
    if m and _YEAR.fullmatch(m.group(1)):
        m = _RAND_AMOUNT.search(window)
    m = m or _RAND_AMOUNT.search(window)
    if not m or m.start(1) > WINDOW:
        return None
    value = _parse_amount(m.group(1))
    if value is None:
        return None
    if not _MONTHLY_AFTER.match(window[m.end():]) and _ANNUAL.search(window[:m.end() + 20]):
        value = round(value / 12, 2)
    lo, hi = bounds
    if not lo <= value <= hi:
        return None
    unit = _UNIT_AFTER.match(window[m.end():])
    return value, after + m.end() + (unit.end() if unit else 0)


def _scan(label_re: re.Pattern, text: str, bounds) -> tuple[float | None, str | None]:
    for label in label_re.finditer(text):
        line_start = text.rfind('\n', 0, label.start()) + 1
        if _TOTAL_BEFORE.search(text[line_start:label.start()]):
            continue   # "Total levies …" row — never a levy or rates figure
        found = _find_amount(text, label, bounds)
        if found:
            value, end = found
            # label → end of the amount and its unit ("pm", "per annum", …)
            return value, _snippet(text, label.start(), end)
    return None, None


# Labels that contain "levy"/"rates" but are not the property's monthly costs:
# the bond/transfer calculator on PropData pages, market commentary, place names.
_NOISE = re.compile(
    r'deeds\s+office\s+levy|(?:rates|levy)\s*/\s*(?:levy|rates)\s+clearance|clearance\s+(?:levy|rates)'
    r'|pro-?\s?rata\s+rates(?:\s*/\s*levies)?|exchange\s+rates?|interest\s+rates?|prime\s+rates?'
    r'|sam\s+levys|rates\s+clearance|levy\s+clearance|special\s+levy|transfer\s+(?:duty|fees?)',
    re.I,
)


def is_cost_noise(label: str) -> bool:
    return bool(_NOISE.search(label or ''))


def classify_cost_label(label: str) -> str | None:
    """'levy' | 'rates' | 'combined' for a table/definition label, else None."""
    label = _normalise_spaces(label or '').strip()
    if not label or re.search(r'\btotal\b', label, re.I) or is_cost_noise(label):
        return None
    if _COMBINED_LABEL.search(label):
        return 'combined'
    if _RATES_LABEL.search(label) and not _LEVY_LABEL.search(label):
        return 'rates'
    if _LEVY_LABEL.search(label) and not _RATES_LABEL.search(label):
        return 'levy'
    return None


def cost_in_bounds(kind: str, value: float | None) -> float | None:
    if value is None:
        return None
    lo, hi = {'levy': LEVY_BOUNDS, 'rates': RATES_BOUNDS, 'combined': COMBINED_BOUNDS}[kind]
    return value if lo <= value <= hi else None


def _costs_from(text: str) -> dict:
    text = _mask(_normalise_spaces(text), [m.span() for m in _NOISE.finditer(_normalise_spaces(text))])
    combined, combined_snip = _scan(_COMBINED_LABEL, text, COMBINED_BOUNDS)

    # Hide combined labels so "Levies & Rates: R3 267" is never read as a levy.
    masked = _mask(text, [m.span() for m in _COMBINED_LABEL.finditer(text)])
    levy,  levy_snip  = _scan(_LEVY_LABEL,  masked, LEVY_BOUNDS)
    rates, rates_snip = _scan(_RATES_LABEL, masked, RATES_BOUNDS)

    snippets = [s for s in (levy_snip, rates_snip, combined_snip) if s]
    return {
        'levy_monthly':                  levy,
        'rates_monthly':                 rates,
        'levies_rates_combined_monthly': combined,
        'raw_snippet':                   ' | '.join(snippets)[:3 * SNIPPET_LEN] if snippets else None,
    }


def extract_costs_from_text(page_text: str | None, description_text: str | None) -> dict:
    """
    Find monthly levy / rates figures by label.

    Returns levy_monthly, rates_monthly, levies_rates_combined_monthly,
    raw_snippet (the text each number came from) and matched_in
    ('page' | 'description' | None). A combined "levies and rates" figure is
    never split; annual figures are divided by 12; "Total" rows are ignored;
    out-of-bounds amounts are discarded.
    """
    for text, where in ((page_text, 'page'), (description_text, 'description')):
        if not text:
            continue
        found = _costs_from(text)
        if any(found[k] is not None for k in
               ('levy_monthly', 'rates_monthly', 'levies_rates_combined_monthly')):
            found['matched_in'] = where
            return found
    return {
        'levy_monthly': None,
        'rates_monthly': None,
        'levies_rates_combined_monthly': None,
        'raw_snippet': None,
        'matched_in': None,
    }


def parse_money(text: str | None) -> float | None:
    """Parse a single cost cell ("R2 267", "R 1,000.00 p.a.") to a monthly amount."""
    if not text:
        return None
    text = _normalise_spaces(text)
    m = _RAND_AMOUNT.search(text) or re.search(r'(' + _NUM + r')', text)
    if not m:
        return None
    value = _parse_amount(m.group(1))
    if value is not None and _ANNUAL.search(text):
        value = round(value / 12, 2)
    return value
