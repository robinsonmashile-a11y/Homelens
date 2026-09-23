"""
Homelens — Rawson size backfill script.

Visits detail pages for existing Rawson listings that are missing size_m2,
and extracts Building Size, has_parking, has_garden, and full description.

Also updates price_per_m2 where size is successfully recovered.

Usage:
    python scripts/fix_rawson_sizes.py

Prerequisites:
    has_parking and has_garden columns must exist in the listings table.
    If they don't, run the migration SQL below in Supabase SQL editor first.

MIGRATION SQL:
─────────────────────────────────────────────────────────────────────────────
ALTER TABLE listings ADD COLUMN IF NOT EXISTS has_parking BOOLEAN DEFAULT NULL;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS has_garden  BOOLEAN DEFAULT NULL;
─────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
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
logger = logging.getLogger('homelens.fix_rawson_sizes')

# ── Constants ──────────────────────────────────────────────────────────────────

FETCH_PAGE      = 500    # rows per paginated GET
DETAIL_DELAY_LO = 4.0    # minimum seconds between detail-page requests
DETAIL_DELAY_HI = 8.0    # maximum seconds between detail-page requests

SIZE_MIN = 15.0
SIZE_MAX = 800.0

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)

MIGRATION_SQL = """\
ALTER TABLE listings ADD COLUMN IF NOT EXISTS has_parking BOOLEAN DEFAULT NULL;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS has_garden  BOOLEAN DEFAULT NULL;
"""

# ── Helpers ────────────────────────────────────────────────────────────────────

def _supabase():
    from scraper.database import get_client
    return get_client()


def _paginate(query_fn, page_size: int = FETCH_PAGE) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        batch = (query_fn(offset, page_size).data or [])
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def _check_columns(client) -> bool:
    """Return True if has_parking / has_garden columns exist."""
    try:
        client.table('listings') \
              .select('listing_id,has_parking,has_garden') \
              .limit(1) \
              .execute()
        return True
    except Exception as e:
        err = str(e).lower()
        if ('pgrst204' in err or '42703' in err or 'schema cache' in err
                or 'could not find' in err or 'does not exist' in err):
            print()
            print('  ⚠  Missing columns detected.')
            print('  Run this SQL in the Supabase SQL editor first, then re-run:')
            print()
            print('  ' + '─' * 70)
            for line in MIGRATION_SQL.strip().splitlines():
                print(f'  {line}')
            print('  ' + '─' * 70)
            print()
            return False
        raise


def _parse_building_size(label_text: str) -> float | None:
    m = re.search(r'Building Size\s*([\d,]+(?:\.\d+)?)\s*m', label_text, re.I)
    if not m:
        return None
    try:
        val = float(m.group(1).replace(',', ''))
    except ValueError:
        return None
    return val if SIZE_MIN <= val <= SIZE_MAX else None


# ── Detail page fetch ─────────────────────────────────────────────────────────

async def _fetch_detail(browser, url: str) -> dict:
    """
    Visit a Rawson detail page using a FRESH browser context per request
    to avoid Cloudflare session-based blocking.
    """
    defaults = {
        'size_m2':     None,
        'has_parking': False,
        'has_garden':  False,
        'description': None,
    }
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

        # Bail out early if Cloudflare blocked us
        title = await page.title()
        if 'cloudflare' in title.lower() or 'attention required' in title.lower():
            logger.warning('Cloudflare block on %s — skipping', url)
            return defaults

        size_m2     = None
        has_parking = False
        has_garden  = False

        feature_labels = await page.query_selector_all('.features__item .features__label')
        for lbl_el in feature_labels:
            text = (await lbl_el.inner_text()).strip()

            if size_m2 is None and 'Building Size' in text:
                size_m2 = _parse_building_size(text)

            low = text.lower()
            if 'garage' in low or 'parking' in low:
                has_parking = True
            if 'garden' in low:
                has_garden = True

        description = None
        desc_el = await page.query_selector('.content-block__synopsis--large')
        if desc_el:
            from scraper.utils import clean_personal_info
            raw = (await desc_el.inner_text()).strip()
            if raw:
                description = clean_personal_info(raw[:4000])

        return {
            'size_m2':     size_m2,
            'has_parking': has_parking,
            'has_garden':  has_garden,
            'description': description,
        }

    except Exception as e:
        logger.warning('Detail fetch error for %s: %s', url, e)
        return defaults
    finally:
        try:
            await context.close()
        except Exception:
            pass


# ── Main async logic ───────────────────────────────────────────────────────────

async def run_backfill(client) -> tuple[int, int, int]:
    """
    Fetch Rawson listings missing size_m2, visit their detail pages, and update.
    Returns (total_missing, recovered, still_missing).
    """
    from playwright.async_api import async_playwright

    logger.info('Fetching Rawson listings missing size_m2...')

    rows = _paginate(lambda off, lim: (
        client.table('listings')
        .select('listing_id,listing_url,price')
        .eq('source', 'rawson')
        .eq('is_active', True)
        .filter('size_m2', 'is', 'null')
        .filter('listing_url', 'not.is', 'null')
        .range(off, off + lim - 1)
        .execute()
    ))

    total_missing = len(rows)
    logger.info('  %d Rawson listings missing size_m2', total_missing)

    if not rows:
        return 0, 0, 0

    recovered     = 0
    still_missing = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage'],
        )

        for i, row in enumerate(rows, 1):
            lid   = row['listing_id']
            url   = row['listing_url']
            price = row.get('price')

            logger.info('[%d/%d] %s → %s', i, total_missing, lid, url)

            detail = await _fetch_detail(browser, url)

            update: dict = {
                'has_parking': detail['has_parking'],
                'has_garden':  detail['has_garden'],
            }
            if detail['description']:
                update['description'] = detail['description']
            if detail['size_m2']:
                update['size_m2'] = detail['size_m2']
                if price and detail['size_m2'] > 0:
                    update['price_per_m2'] = round(price / detail['size_m2'], 2)
                recovered += 1
                logger.info('  → size_m2=%.0f  parking=%s  garden=%s',
                            detail['size_m2'], detail['has_parking'], detail['has_garden'])
            else:
                still_missing += 1
                logger.info('  → No building size found  parking=%s  garden=%s',
                            detail['has_parking'], detail['has_garden'])

            try:
                client.table('listings').update(update).eq('listing_id', lid).execute()
            except Exception as e:
                logger.warning('DB update failed for %s: %s', lid, e)

            # Polite delay
            delay = random.uniform(DETAIL_DELAY_LO, DETAIL_DELAY_HI)
            await asyncio.sleep(delay)

        await browser.close()

    return total_missing, recovered, still_missing


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    required = ['SUPABASE_URL', 'SUPABASE_KEY']
    missing  = [k for k in required if not os.environ.get(k)]
    if missing:
        logger.error('Missing environment variables: %s', ', '.join(missing))
        sys.exit(1)

    client = _supabase()

    print()
    print('━' * 55)
    print('  Homelens — Rawson Size Backfill')
    print('━' * 55)
    print()

    if not _check_columns(client):
        sys.exit(1)

    t_start = time.time()

    total, recovered, still = asyncio.run(run_backfill(client))

    elapsed  = round(time.time() - t_start, 1)
    pct      = round(100 * recovered / total) if total else 0

    print()
    print('━' * 55)
    print(f'  Total missing   : {total:>6,}')
    print(f'  Sizes recovered : {recovered:>6,}  ({pct}%)')
    print(f'  Still missing   : {still:>6,}')
    print(f'  Time elapsed    : {elapsed}s')
    print('━' * 55)
    print()


if __name__ == '__main__':
    main()
