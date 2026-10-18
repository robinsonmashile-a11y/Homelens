from __future__ import annotations

import hashlib
import re


def clean_personal_info(text: str) -> str:
    """Strip phone numbers, emails, and WhatsApp refs from text (POPIA compliance)."""
    if not text:
        return text
    # Remove WhatsApp mentions with numbers first (more specific, must come before plain phone)
    text = re.sub(
        r'(whatsapp|wa|call|sms|text)[\s:]*(\+27|0)[0-9\s\-]{8,12}',
        '[contact removed]',
        text,
        flags=re.IGNORECASE,
    )
    # Remove SA phone numbers
    text = re.sub(r'(\+27|0)[0-9\s\-]{8,12}', '[contact removed]', text)
    # Remove email addresses
    text = re.sub(
        r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',
        '[contact removed]',
        text,
    )
    return text


_POA_PATTERNS = (
    'poa',
    'price on application',
    'price on request',
    'on application',
    'on request',
    'contact agent',
    'contact us',
    'price on enquiry',
    'por',
    'neg',
    'negotiable',
    'enquire',
    'enquiry',
)


class ForeignCurrencyError(Exception):
    """A site showed prices in a non-rand currency (e.g. geo-localised USD).
    Scrapers must abort rather than store these as rands."""


_RAND_RE = re.compile(r'(?<![A-Za-z$])(R|ZAR)\s*\d')


def assert_rand_price(price_text: str) -> None:
    """Raise ForeignCurrencyError if price_text has digits but is not in rands.
    'POA' / empty text pass (no digits)."""
    if price_text and re.search(r'\d', price_text) and not _RAND_RE.search(price_text):
        raise ForeignCurrencyError(f'non-rand price text: {price_text!r}')


def parse_price(price_str: str) -> tuple[int | None, str]:
    """
    Parse a property price string and return (price, price_status).

    price_status values:
      'ok'                — price parsed to a positive integer
      'poa'               — Price On Application / Contact Agent (no numeric value;
                            this is a valid listing state, not an error)
      'extraction_failed' — a non-empty string was found but contained no digits
                            (selector found something unexpected)
      'unknown'           — input was None or empty (selector found nothing)

    Handles: "R 12,500 pm", "R12500", "R 12 500 per month", "R12,500/month",
             "ZAR 4,850,000", "R8 500", non-breaking spaces, commas, full stops
             as thousands separators.  Strips all non-digit chars after POA check.
    """
    if not price_str:
        return None, 'unknown'

    text = price_str.strip()
    text_lower = text.lower()

    # POA detection — check before digit extraction so "POA" doesn't silently
    # become None/'unknown'; we need to distinguish it from extraction failure.
    if any(p in text_lower for p in _POA_PATTERNS):
        return None, 'poa'

    digits = re.sub(r'[^\d]', '', text)
    if not digits:
        # Non-empty string with no digits (e.g. "Contact agent", "—", "TBC")
        return None, 'extraction_failed'

    try:
        return int(digits), 'ok'
    except (ValueError, OverflowError):
        return None, 'extraction_failed'


def normalise_price(price_str: str) -> int | None:
    """
    Backwards-compatible shim — returns price integer or None.
    Prefer parse_price() for new code so price_status is captured.
    """
    price, _ = parse_price(price_str)
    return price


SUBURB_MAP: dict[str, str] = {
    # Sea Point / Atlantic Seaboard
    'sea point': 'sea-point',
    'seapoint': 'sea-point',
    'sea-point': 'sea-point',
    'green point': 'green-point',
    'greenpoint': 'green-point',
    'green-point': 'green-point',
    'mouille point': 'mouille-point',
    'mouille-point': 'mouille-point',
    'moullie point': 'mouille-point',
    'three anchor bay': 'three-anchor-bay',
    'three-anchor-bay': 'three-anchor-bay',
    'bantry bay': 'bantry-bay',
    'bantry-bay': 'bantry-bay',
    'fresnaye': 'fresnaye',
    'clifton': 'clifton',
    'camps bay': 'camps-bay',
    'camps-bay': 'camps-bay',
    # City Bowl
    'gardens': 'gardens',
    'de waterkant': 'de-waterkant',
    'dewaterkant': 'de-waterkant',
    'de-waterkant': 'de-waterkant',
    'bo kaap': 'bo-kaap',
    'bo-kaap': 'bo-kaap',
    'bokaap': 'bo-kaap',
    'tamboerskloof': 'tamboerskloof',
    'oranjezicht': 'oranjezicht',
    'vredehoek': 'vredehoek',
    'higgovale': 'higgovale',
    'city bowl': 'city-bowl',
    'citybowl': 'city-bowl',
    'city-bowl': 'city-bowl',
    'city centre': 'city-bowl',
    'city center': 'city-bowl',
    'cape town city': 'city-bowl',
    'cape town centre': 'city-bowl',
    'cape town cbd': 'city-bowl',
    # Inner suburbs
    'woodstock': 'woodstock',
    'observatory': 'observatory',
    'obs': 'observatory',
    'salt river': 'salt-river',
    'saltriver': 'salt-river',
    'salt-river': 'salt-river',
    'mowbray': 'mowbray',
    'rondebosch': 'rondebosch',
    'newlands': 'newlands',
    'claremont': 'claremont',
    'wynberg': 'wynberg',
    # Northern suburbs
    'bellville': 'bellville',
    'parow': 'parow',
    'milnerton': 'milnerton',
    'tableview': 'tableview',
    'table view': 'tableview',
    'blouberg': 'blouberg',
    'bloubergstrand': 'blouberg',
    'goodwood': 'goodwood',
    'durbanville': 'durbanville',
    'brackenfell': 'brackenfell',
    'kraaifontein': 'kraaifontein',
    'edgemead': 'edgemead',
    'monte vista': 'monte-vista',
    'monte-vista': 'monte-vista',
    'pinelands': 'pinelands',
    'thornton': 'thornton',
    'century city': 'century-city',
    'century-city': 'century-city',
    # Southern suburbs
    'constantia': 'constantia',
    'bishopscourt': 'bishopscourt',
    'kenilworth': 'kenilworth',
    'tokai': 'tokai',
    'bergvliet': 'bergvliet',
    'meadowridge': 'meadowridge',
    'westlake': 'westlake',
    'diep river': 'diep-river',
    'diep-river': 'diep-river',
    'plumstead': 'plumstead',
    'retreat': 'retreat',
    # Southern Peninsula / False Bay
    'muizenberg': 'muizenberg',
    'kalk bay': 'kalk-bay',
    'kalk-bay': 'kalk-bay',
    'st james': 'st-james',
    'st-james': 'st-james',
    'fish hoek': 'fish-hoek',
    'fish-hoek': 'fish-hoek',
    'simons town': 'simons-town',
    'simon\'s town': 'simons-town',
    'simons-town': 'simons-town',
    'glencairn': 'glencairn',
    'kommetjie': 'kommetjie',
    'noordhoek': 'noordhoek',
    'scarborough': 'scarborough',
    'sun valley': 'sun-valley',
    'sun-valley': 'sun-valley',
    'ocean view': 'ocean-view',
    'smitswinkelbaai': 'smitswinkelbaai',
    # Hout Bay / Atlantic
    'hout bay': 'hout-bay',
    'hout-bay': 'hout-bay',
    'llandudno': 'llandudno',
    'imhoffs gift': 'imhoffs-gift',
    # Inner city / Bo-Kaap adjacent
    'zonnebloem': 'zonnebloem',
    'foreshore': 'foreshore',
    'walmer estate': 'walmer-estate',
    'walmer-estate': 'walmer-estate',
    'university estate': 'university-estate',
    'university-estate': 'university-estate',
    'crawford': 'crawford',
    'athlone': 'athlone',
    'pinelands': 'pinelands',
}


def normalise_suburb(raw: str) -> str | None:
    """Normalise suburb names to consistent slugs."""
    if not raw:
        return None
    cleaned = raw.lower().strip()
    return SUBURB_MAP.get(cleaned, cleaned.replace(' ', '-'))


def compute_price_per_m2(price: int, size_m2: float) -> float | None:
    if price and size_m2 and size_m2 > 0:
        return round(price / size_m2, 2)
    return None


_IMAGE_BLOCKLIST = ('logo', 'placeholder', 'default', 'avatar', 'icon', 'blank')


def validate_image_url(url: str | None) -> str | None:
    """
    Validate a property image URL before storing.
    Returns None if the URL is falsy, not http/https, or looks like a placeholder/logo.
    Never stores empty strings — always None or a valid absolute URL.
    """
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if not url.startswith(('http://', 'https://')):
        return None
    url_lc = url.lower()
    if any(kw in url_lc for kw in _IMAGE_BLOCKLIST):
        return None
    return url


def extract_listing_id_from_url(url: str) -> str | None:
    """
    Extract a unique listing ID from a property URL.
    Tries numeric ID first, then alphanumeric reference codes, then URL hash.
    """
    if not url:
        return None
    # Match trailing numeric ID: /123456 or /123456?...
    m = re.search(r'/(\d{5,})', url)
    if m:
        return m.group(1)
    # Match alphanumeric ref codes: ML12345, REF-12345, P12345
    m = re.search(r'/((?:ML|REF|P)[A-Z0-9\-]{4,})', url, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    # Fallback: deterministic hash of the URL
    return 'h' + hashlib.md5(url.encode()).hexdigest()[:12]


async def goto_with_retry(page, url: str, *, wait_until: str = 'domcontentloaded',
                          timeout: int = 60_000, attempts: int = 3) -> None:
    """
    page.goto with retries. A single slow load used to drop a whole results
    page (page 1 = the entire listing type for that city, for the whole run).
    Waits 5s then 15s between attempts; raises the last error if all fail.
    """
    import asyncio
    import logging
    last: Exception | None = None
    for i in range(attempts):
        try:
            await page.goto(url, wait_until=wait_until, timeout=timeout)
            return
        except Exception as e:                        # Playwright TimeoutError etc.
            last = e
            if i < attempts - 1:
                logging.getLogger('homelens.utils').warning(
                    'goto %s failed (attempt %d/%d): %s', url, i + 1, attempts, e)
                await asyncio.sleep(5 * (3 ** i))
    raise last  # type: ignore[misc]
