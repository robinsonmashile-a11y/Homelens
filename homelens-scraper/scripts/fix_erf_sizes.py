"""
Homelens — ERF size cleanup script.

Finds listings where size_m2 is suspiciously large for the property type
(almost certainly an ERF/plot size stored instead of floor size), then
visits each detail page to extract the correct floor size.

Targets:
  - Apartments with size_m2 > 300
  - Townhouses with size_m2 > 400
  - Houses with size_m2 > 1500  (optional — very large, likely ERF)

For each suspicious listing:
  1. Visit listing_url in Playwright
  2. Extract the correct floor size (source-aware)
  3. If floor size found and differs: update size_m2 + price_per_m2
  4. If no floor size found: null out size_m2 and price_per_m2
  5. Print: [source] URL  ERF=Xm² → Floor=Ym² ✓  (or → NULL)

Usage:
    python scripts/fix_erf_sizes.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('homelens.fix_erf_sizes')

# ── Thresholds ─────────────────────────────────────────────────────────────────
# Anything above these is suspicious for the property type
SUSPECT_THRESHOLDS: dict[str, float] = {
    'apartment':  300.0,
    'townhouse':  400.0,
    'house':     1500.0,
}

FLOOR_SIZE_MIN = 15.0
FLOOR_SIZE_MAX = 800.0   # reasonable max for a Cape Town floor area

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
)

FETCH_PAGE   = 500
DETAIL_DELAY = 3.0   # seconds between page loads


# ── Floor size extractors per source ──────────────────────────────────────────

async def _extract_floor_size_remax(page) -> float | None:
    """RE/MAX: parse floor_area from embedded Next.js JSON."""
    html = await page.content()
    m = re.search(r'"floor_area"\s*:\s*(\d+(?:\.\d+)?)', html)
    if m:
        try:
            val = float(m.group(1))
            return val if FLOOR_SIZE_MIN <= val <= FLOOR_SIZE_MAX else None
        except ValueError:
            pass
    return None


async def _extract_floor_size_rawson(page) -> float | None:
    """Rawson: find .features__label containing 'Building Size'."""
    labels = await page.query_selector_all('.features__item .features__label')
    for lbl in labels:
        text = (await lbl.inner_text()).strip()
        if 'Building Size' in text:
            m = re.search(r'Building Size\s*([\d,]+(?:\.\d+)?)\s*m', text, re.I)
            if m:
                try:
                    val = float(m.group(1).replace(',', ''))
                    return val if FLOOR_SIZE_MIN <= val <= FLOOR_SIZE_MAX else None
                except ValueError:
                    pass
    return None


async def _extract_floor_size_generic(page) -> float | None:
    """
    Generic: scan page for size values associated with floor/building labels.
    Explicitly skips any value adjacent to 'erf', 'land', 'stand', or 'plot'.
    """
    body = await page.evaluate('() => document.body.innerText')
    lines = body.splitlines()

    floor_keywords  = ('floor', 'building size', 'living area', 'floor area', 'habitable')
    skip_keywords   = ('erf', 'land area', 'stand size', 'plot', 'lot size', 'stand:')

    # Pass 1: look for a line that explicitly mentions floor/building
    for line in lines:
        ll = line.lower()
        if any(kw in ll for kw in skip_keywords):
            continue
        if any(kw in ll for kw in floor_keywords) and 'm' in ll:
            m = re.search(r'(\d+(?:[.,]\d+)?)\s*m[²2]?', line)
            if m:
                try:
                    val = float(m.group(1).replace(',', '.'))
                    if FLOOR_SIZE_MIN <= val <= FLOOR_SIZE_MAX:
                        return val
                except ValueError:
                    pass

    return None


async def _extract_floor_size(page, source: str) -> float | None:
    """Dispatch to the source-specific extractor."""
    try:
        if source == 'remax':
            return await _extract_floor_size_remax(page)
        elif source == 'rawson':
            return await _extract_floor_size_rawson(page)
        else:
            # pam_golding, seeff, tyson, sothebys, etc.
            return await _extract_floor_size_generic(page)
    except Exception as e:
        logger.warning('Floor size extraction error (%s): %s', source, e)
        return None


# ── Main async backfill ────────────────────────────────────────────────────────

async def run_fix(client) -> dict:
    from playwright.async_api import async_playwright

    stats = {
        'total':    0,
        'fixed':    0,
        'nulled':   0,
        'skipped':  0,
    }

    # Fetch suspicious listings
    all_rows: list[dict] = []
    for ptype, threshold in SUSPECT_THRESHOLDS.items():
        rows = []
        offset = 0
        while True:
            batch = (
                client.table('listings')
                .select('listing_id,listing_url,source,property_type,size_m2,price')
                .eq('is_active', True)
                .eq('property_type', ptype)
                .gt('size_m2', threshold)
                .filter('listing_url', 'not.is', 'null')
                .range(offset, offset + FETCH_PAGE - 1)
                .execute()
                .data or []
            )
            rows.extend(batch)
            if len(batch) < FETCH_PAGE:
                break
            offset += FETCH_PAGE

        logger.info('  %s with size_m2 > %.0f: %d listings', ptype, threshold, len(rows))
        all_rows.extend(rows)

    stats['total'] = len(all_rows)
    logger.info('Total suspicious listings: %d', stats['total'])

    if not all_rows:
        return stats

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage'],
        )

        for i, row in enumerate(all_rows, 1):
            lid     = row['listing_id']
            url     = row['listing_url']
            source  = row.get('source') or ''
            old_sz  = row.get('size_m2') or 0
            price   = row.get('price')

            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={'width': 1280, 'height': 900},
                locale='en-ZA',
                timezone_id='Africa/Johannesburg',
            )
            page = await context.new_page()

            try:
                await page.goto(url, wait_until='networkidle', timeout=60_000)
                await asyncio.sleep(2.0)

                title = await page.title()
                if 'cloudflare' in title.lower() or 'attention required' in title.lower():
                    logger.warning('[%d/%d] Cloudflare blocked — skipping %s',
                                   i, stats['total'], lid)
                    stats['skipped'] += 1
                    continue

                floor_size = await _extract_floor_size(page, source)

                if floor_size and abs(floor_size - old_sz) > 1:
                    # Found a valid, different floor size
                    update: dict = {'size_m2': floor_size}
                    if price:
                        update['price_per_m2'] = round(price / floor_size, 2)
                    else:
                        update['price_per_m2'] = None
                    client.table('listings').update(update).eq('listing_id', lid).execute()
                    print(f'  [{source}] ERF={old_sz:.0f}m² → Floor={floor_size:.0f}m² ✓  {url}')
                    stats['fixed'] += 1

                elif floor_size is None or floor_size == 0:
                    # No floor size found — null it out
                    client.table('listings').update(
                        {'size_m2': None, 'price_per_m2': None}
                    ).eq('listing_id', lid).execute()
                    print(f'  [{source}] ERF={old_sz:.0f}m² → NULL (no floor size found)  {url}')
                    stats['nulled'] += 1

                else:
                    # Found floor size but it matches — no change needed
                    print(f'  [{source}] size={floor_size:.0f}m² unchanged  {url}')
                    stats['skipped'] += 1

            except Exception as e:
                logger.warning('[%d/%d] Error for %s: %s', i, stats['total'], lid, e)
                stats['skipped'] += 1
            finally:
                try:
                    await context.close()
                except Exception:
                    pass

            await asyncio.sleep(DETAIL_DELAY)

        await browser.close()

    return stats


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    required = ['SUPABASE_URL', 'SUPABASE_KEY']
    missing  = [k for k in required if not os.environ.get(k)]
    if missing:
        logger.error('Missing env vars: %s', ', '.join(missing))
        sys.exit(1)

    from scraper.database import get_client
    client = get_client()

    print()
    print('━' * 60)
    print('  Homelens — ERF Size Cleanup')
    print('━' * 60)
    print()

    t_start = time.time()
    stats   = asyncio.run(run_fix(client))
    elapsed = round(time.time() - t_start, 1)

    print()
    print('━' * 60)
    print(f"  Suspicious listings found : {stats['total']:>6,}")
    print(f"  Floor size recovered      : {stats['fixed']:>6,}")
    print(f"  Nulled (no floor size)    : {stats['nulled']:>6,}")
    print(f"  Skipped                   : {stats['skipped']:>6,}")
    print(f"  Time elapsed              : {elapsed}s")
    print('━' * 60)
    print()


if __name__ == '__main__':
    main()
