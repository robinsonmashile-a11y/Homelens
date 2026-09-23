"""
Rent-price diagnostic script.

For each scraper source, fetches the first page of RENT listings, extracts
the price element text from the first 5 cards, and classifies the source
into a root-cause bucket:

  A_selector  — price element not found at all (selector misses the node)
  B_parse     — price element found but text has no parseable digits after
                POA check (unexpected non-POA text like "Contact for price")
  C_poa       — price element found but text is POA / Contact Agent
  D_timing    — page loaded but cards were empty / price JS not yet rendered
  ok          — price extracted successfully

Results are printed as a table and written to
  scripts/diagnosis_results.json  (machine-readable for audit log)

Usage:
  cd homelens-scraper
  python -m scripts.diagnose_rent_prices

The script uses Playwright but does NOT write to the database.
It is safe to run repeatedly.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

from scraper.utils import parse_price

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger('diagnose')

# ---------------------------------------------------------------------------
# Source definitions — (source_name, rent_url, card_selector, price_selector)
# ---------------------------------------------------------------------------
SOURCES = [
    ('pamgolding',    'https://www.pamgolding.co.za/property-search/properties-to-rent-cape-town/3080',
     'article.results__item', '.results__item-price'),
    ('sothebys',      'https://www.sothebysrealty.co.za/results/residential/to-let/cape-town/',
     'a.property-card-sm', 'p.card-price'),
    ('tyson',         'https://www.tysonprop.co.za/results/residential/to-let/cape-town/',
     'a.property-card-sm', '.card-price'),
    ('rawson',        'https://rawson.co.za/property/to-rent/cape-town',
     'div.card', '.card__price'),
    ('seeff',         'https://www.seeff.com/results/residential/to-rent/cape-town/',
     'div.seeff-listing-card', '.card-price'),
    ('remax',         'https://www.remax.co.za/property-to-rent/cape-town/',
     'div.shadow-xl', 'div.font-bold.whitespace-nowrap'),
    ('quay1',         'https://www.quay1.co.za/results/residential/to-rent/cape-town/',
     'a.property-card-sm', '.card-price'),
    ('chaseveritt',   'https://www.chaseveritt.co.za/results/residential/to-rent/cape-town/',
     'a.property-card-sm', '.card-price'),
    ('greeff',        'https://www.greeff.co.za/results/residential/to-rent/cape-town/',
     'a.property-card-sm', '.card-price'),
    ('jawitz',        'https://www.jawitz.co.za/results/residential/to-rent/cape-town/',
     'div.property-list-listing', 'span.property-list-price-heading'),
    ('engelvolkers',  'https://www.engelvoelkers.com/za/en/properties/res/rent/real-estate/western-cape/cape-town/',
     'article[data-testid^="search-components_result-card"]',
     '[data-testid="search-components_result-card_price"]'),
    ('century21',     'https://www.century21.co.za/results/residential/to-rent/cape-town/',
     'a.property-card-sm', '.card-price'),
    ('harcourts',     'https://harcourts.net/ZA/results/residential/to-rent/cape-town/',
     'a.property-card-sm', '.card-price'),
    ('lionlead',      'https://www.lionlead.co.za/results/residential/to-rent/cape-town/',
     'div.property-list-listing', '.property-list-price-heading'),
    ('abc_international', 'https://www.abcinternational.co.za/results/residential/to-rent/cape-town/',
     'div.property-list-listing', '.property-list-price-heading'),
    ('justproperty',  'https://www.justproperty.co.za/results/residential/to-rent/cape-town/',
     'a.property-card-sm', '.card-price'),
    ('dgproperties',  'https://www.dgproperties.co.za/results/residential/to-rent/cape-town/',
     'a.property-card-sm', '.card-price'),
]

SAMPLES_PER_SOURCE = 5
PAGE_LOAD_WAIT     = 5.0
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)


def _classify(samples: list[dict]) -> str:
    """
    Given a list of {found: bool, text: str, price: int|None, status: str},
    return the dominant root-cause bucket for this source.
    """
    if not samples:
        return 'D_timing'

    not_found = sum(1 for s in samples if not s['found'])
    poa       = sum(1 for s in samples if s['status'] == 'poa')
    ok        = sum(1 for s in samples if s['status'] == 'ok')
    failed    = sum(1 for s in samples if s['status'] == 'extraction_failed')

    total = len(samples)

    if not_found / total >= 0.6:
        return 'A_selector'
    if ok / total >= 0.6:
        return 'ok'
    if poa / total >= 0.4:
        return 'C_poa'
    if failed / total >= 0.4:
        return 'B_parse'
    # Mixed — POA + ok
    return 'C_poa' if poa > ok else 'ok'


async def diagnose_source(
    browser,
    source: str,
    url: str,
    card_sel: str,
    price_sel: str,
) -> dict:
    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={'width': 1280, 'height': 900},
        locale='en-ZA',
        timezone_id='Africa/Johannesburg',
    )
    page = await context.new_page()
    samples = []
    error = None

    try:
        logger.info('  → %s: loading %s', source, url)
        await page.goto(url, wait_until='networkidle', timeout=60_000)
        await asyncio.sleep(PAGE_LOAD_WAIT)

        # Scroll to trigger lazy-loaded cards
        try:
            h = await page.evaluate('document.body.scrollHeight')
            for i in range(1, 6):
                await page.evaluate(f'window.scrollTo(0, {h // 5 * i})')
                await asyncio.sleep(0.3)
        except Exception:
            pass

        cards = await page.query_selector_all(card_sel)
        logger.info('    cards found: %d', len(cards))

        for card in cards[:SAMPLES_PER_SOURCE]:
            try:
                price_el   = await card.query_selector(price_sel)
                price_text = (await price_el.inner_text()).strip() if price_el else ''
                price, status = parse_price(price_text)
                samples.append({
                    'found':  price_el is not None,
                    'text':   price_text[:80],
                    'price':  price,
                    'status': status,
                })
            except Exception as exc:
                samples.append({'found': False, 'text': str(exc)[:80], 'price': None, 'status': 'unknown'})

    except Exception as exc:
        error = str(exc)
        logger.warning('    ERROR: %s', exc)
    finally:
        try:
            await context.close()
        except Exception:
            pass

    bucket = _classify(samples) if not error else 'D_timing'

    result = {
        'source':  source,
        'url':     url,
        'bucket':  bucket,
        'samples': samples,
        'error':   error,
        'run_at':  datetime.now(timezone.utc).isoformat(),
    }
    return result


async def main() -> None:
    all_results = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage'],
        )

        for source, url, card_sel, price_sel in SOURCES:
            result = await diagnose_source(browser, source, url, card_sel, price_sel)
            all_results.append(result)

            # Print summary row
            ok_n  = sum(1 for s in result['samples'] if s['status'] == 'ok')
            poa_n = sum(1 for s in result['samples'] if s['status'] == 'poa')
            nf_n  = sum(1 for s in result['samples'] if not s['found'])
            total = len(result['samples'])
            print(
                f"  {source:<20} bucket={result['bucket']:<12} "
                f"cards={total}  ok={ok_n}  poa={poa_n}  selector_miss={nf_n}"
            )
            if result['samples']:
                for s in result['samples']:
                    marker = '✓' if s['status'] == 'ok' else ('~' if s['status'] == 'poa' else '✗')
                    print(f"    {marker} [{s['status']:<18}] {s['text']!r}")
            if result['error']:
                print(f"    ERROR: {result['error']}")

        await browser.close()

    # Write machine-readable output
    out_path = Path(__file__).parent / 'diagnosis_results.json'
    with open(out_path, 'w') as f:
        json.dump(all_results, f, indent=2)
    logger.info('\nResults written to %s', out_path)

    # Print bucket summary table
    print('\n=== BUCKET SUMMARY ===')
    print(f"{'Source':<22} {'Bucket':<14} {'Recommended action'}")
    print('-' * 70)
    for r in all_results:
        action = {
            'ok':         'No fix needed',
            'A_selector': 'Fix CSS selector for rent card',
            'B_parse':    'Fix price text parser / add variant',
            'C_poa':      'POA is correct — price_status tracking is sufficient',
            'D_timing':   'Increase PAGE_LOAD_WAIT or add scroll trigger',
        }.get(r['bucket'], '?')
        print(f"  {r['source']:<20} {r['bucket']:<14} {action}")


if __name__ == '__main__':
    asyncio.run(main())
