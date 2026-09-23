"""
Homelens — Image URL backfill script.

For every active listing where image_url IS NULL and listing_url exists:
  - Visit the listing_url with Playwright
  - Extract og:image meta tag
  - Update image_url in Supabase

Processes in batches of 20 with random 3-6 second delays.
Stops if more than 10 consecutive errors (site may be blocking).

Usage:
    python scripts/fix_images.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(override=True)

from scraper.database import get_client
from scraper.utils import validate_image_url

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('homelens.fix_images')

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BATCH_SIZE      = 20
DELAY_LO        = 3.0
DELAY_HI        = 6.0
MAX_CONSEC_ERRS = 10
PAGE_TIMEOUT    = 30_000   # ms

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)


# ---------------------------------------------------------------------------
# Image extraction
# ---------------------------------------------------------------------------

async def _fetch_og_image(browser, url: str) -> str | None:
    """
    Visit a listing detail page and return the og:image URL.
    Creates a fresh browser context per request to avoid session tracking.
    Returns None on any error or if no image found.
    """
    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={'width': 1280, 'height': 900},
        locale='en-ZA',
        timezone_id='Africa/Johannesburg',
    )
    page = await context.new_page()
    try:
        await page.goto(url, wait_until='domcontentloaded', timeout=PAGE_TIMEOUT)
        await asyncio.sleep(1.5)

        # Try gallery image first, fall back to og:image
        raw = await page.evaluate('''() => {
            const gallerySelectors = [
                ".swiper-slide img",
                ".gallery img",
                "div[class*=\'gallery\'] img",
                ".property-images img",
                ".listing-images img",
                ".card-image img"
            ];
            for (const sel of gallerySelectors) {
                const img = document.querySelector(sel);
                if (img && img.src && img.src.startsWith("http")) return img.src;
            }
            const meta = document.querySelector("meta[property=\'og:image\']");
            return meta ? meta.content : null;
        }''')
        return validate_image_url(raw)

    except Exception as e:
        logger.debug('og:image fetch failed for %s: %s', url, e)
        return None
    finally:
        try:
            await context.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_missing_images(client) -> list[dict]:
    """
    Return all active listings where image_url IS NULL and listing_url IS NOT NULL.
    Excludes remax — their detail pages block standalone Playwright visits via
    Cloudflare anti-bot; images are captured during the normal remax scrape instead.
    Paginate to handle large datasets.
    """
    results: list[dict] = []
    offset = 0
    page_size = 1000

    while True:
        resp = (
            client.table('listings')
            .select('listing_id,listing_url,source,suburb_normalised,bedrooms,price')
            .eq('is_active', True)
            .is_('image_url', 'null')
            .not_.is_('listing_url', 'null')
            .neq('source', 'remax')
            .range(offset, offset + page_size - 1)
            .execute()
        )
        batch = resp.data or []
        results.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size

    return results


def update_image_url(client, listing_id: str, image_url: str) -> None:
    client.table('listings').update(
        {'image_url': image_url}
    ).eq('listing_id', listing_id).execute()


# ---------------------------------------------------------------------------
# Env check
# ---------------------------------------------------------------------------

def _check_env() -> None:
    required = ['SUPABASE_URL', 'SUPABASE_KEY']
    missing  = [k for k in required if not os.environ.get(k)]
    if missing:
        logger.error('Missing environment variables: %s', ', '.join(missing))
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    _check_env()
    client = get_client()

    print()
    print('━' * 60)
    print('  Homelens — Image URL Backfill')
    print('━' * 60)

    # Fetch listings missing image_url
    logger.info('Fetching listings with missing image_url…')
    listings = fetch_missing_images(client)
    total = len(listings)

    if total == 0:
        print('  No listings missing image_url. Nothing to do.')
        print('━' * 60)
        return

    print(f'  Listings to process : {total:,}')
    print(f'  Batch size          : {BATCH_SIZE}')
    print(f'  Delay per request   : {DELAY_LO:.0f}-{DELAY_HI:.0f}s')
    print(f'  Max consec errors   : {MAX_CONSEC_ERRS}')
    print('━' * 60)
    print()

    t_start         = time.time()
    backfilled      = 0
    still_missing   = 0
    errors          = 0
    consec_errors   = 0

    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage'],
        )

        for i, listing in enumerate(listings, 1):
            lid     = listing['listing_id']
            url     = listing.get('listing_url', '')
            suburb  = (listing.get('suburb_normalised') or '').replace('-', ' ').title()
            beds    = listing.get('bedrooms') or '?'
            price   = listing.get('price') or 0
            source  = listing.get('source', '')

            if not url:
                print(f'  [{i:>5}/{total}] {lid} — no listing_url, skipped')
                still_missing += 1
                continue

            image_url = await _fetch_og_image(browser, url)

            if image_url:
                try:
                    update_image_url(client, lid, image_url)
                    print(f'  [{i:>5}/{total}] {source} {suburb} {beds}bed R{price:,} → {image_url[:60]}... ✓')
                    backfilled   += 1
                    consec_errors = 0
                except Exception as e:
                    logger.error('DB update failed for %s: %s', lid, e)
                    errors += 1
                    consec_errors += 1
            else:
                print(f'  [{i:>5}/{total}] {lid} ({source}) → no image found')
                still_missing += 1
                consec_errors += 1

            if consec_errors >= MAX_CONSEC_ERRS:
                print()
                print(f'  Stopped: {MAX_CONSEC_ERRS} consecutive failures — site may be blocking.')
                break

            # Random delay between requests
            await asyncio.sleep(random.uniform(DELAY_LO, DELAY_HI))

        await browser.close()

    elapsed = round(time.time() - t_start, 1)

    print()
    print('━' * 60)
    print(f'  Backfilled    : {backfilled:,}')
    print(f'  Still missing : {still_missing:,}')
    print(f'  Errors        : {errors:,}')
    print(f'  Time elapsed  : {elapsed}s')
    print('━' * 60)
    print()


if __name__ == '__main__':
    asyncio.run(main())
