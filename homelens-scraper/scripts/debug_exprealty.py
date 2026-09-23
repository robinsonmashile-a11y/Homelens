"""
Debug script — captures exact Supabase API details from eXp Realty.
Runs full page load, intercepts the Supabase request, prints full API key
and tests the intercepted URL directly to get the real total count.

Usage:
    python3 scripts/debug_exprealty.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)

URL = (
    'https://www.expsouthafrica.co.za/search-properties'
    '?country=ZA&propertyType=any&maxPrice=999999999&city=Cape+Town&listingType=buy'
)

SUPABASE_HOST = 'ywzpnbmomlzkcbzzkaqr.supabase.co'


async def main() -> None:
    captured: dict = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=['--no-sandbox'])
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={'width': 1280, 'height': 900},
        )
        page = await context.new_page()

        async def on_request(request):
            if SUPABASE_HOST in request.url and '/rest/v1/listings' in request.url and 'offset=0' in request.url:
                if not captured:  # first one only
                    captured['url']     = request.url
                    captured['headers'] = dict(request.headers)

        page.on('request', on_request)

        print(f'Loading: {URL}\n')
        await page.goto(URL, wait_until='networkidle', timeout=60_000)
        await asyncio.sleep(5)
        await browser.close()

    if not captured:
        print('!! No Supabase request captured')
        return

    api_key = captured['headers'].get('apikey', '')
    auth    = captured['headers'].get('authorization', f'Bearer {api_key}')
    intercepted_url = captured['url']

    print(f'Full API key:\n  {api_key}\n')
    print(f'Intercepted URL:\n  {intercepted_url}\n')

    # Parse the intercepted URL to understand its filters
    parsed = urllib.parse.urlparse(intercepted_url)
    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    print('Intercepted filters:')
    for k, vs in params.items():
        for v in vs:
            print(f'  {k} = {v}')

    def api_get(url: str) -> tuple[dict | list, str]:
        """Make GET request, return (data, content_range)."""
        req = urllib.request.Request(url, headers={
            'apikey': api_key,
            'Authorization': auth,
            'Content-Type': 'application/json',
            'Prefer': 'count=exact',
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            cr = resp.headers.get('Content-Range', '')
            return data, cr

    # ── Test 1: exact intercepted URL (geo bounds filter) ──────────────────
    print('\n── Test 1: intercepted geo-bounds URL ──')
    # Swap limit=1 to just get count quickly
    url1 = intercepted_url.replace('&limit=24', '&limit=1').replace('&offset=0', '&offset=0')
    try:
        data, cr = api_get(url1)
        print(f'  Content-Range: {cr}   ← total count here')
        print(f'  Records returned: {len(data)}')
        if data:
            print(f'  First record keys: {list(data[0].keys())}')
    except Exception as e:
        print(f'  Error: {e}')

    # ── Test 2: city filter (what our test used) ───────────────────────────
    print('\n── Test 2: city=ilike.%Cape+Town% filter ──')
    base = f'https://{SUPABASE_HOST}/rest/v1/listings'
    url2 = (
        base
        + '?select=*&country_code=eq.ZA&status=eq.1'
        + '&listing_type=in.(1,4,5,6,9,7)'
        + '&city=ilike.%25Cape+Town%25'
        + '&order=created_at.desc,id.asc&offset=0&limit=1'
    )
    try:
        data, cr = api_get(url2)
        print(f'  Content-Range: {cr}   ← total count here')
    except Exception as e:
        print(f'  Error: {e}')

    # ── Test 3: no listing_type filter with geo bounds ─────────────────────
    print('\n── Test 3: geo-bounds, no listing_type filter ──')
    url3 = url1.replace('&listing_type=in.%281%2C4%2C5%2C6%2C9%2C7%29', '')
    try:
        data, cr = api_get(url3)
        print(f'  Content-Range: {cr}   ← total count here')
    except Exception as e:
        print(f'  Error: {e}')

    # ── Test 4: record field values ────────────────────────────────────────
    print('\n── Test 4: sample record fields ──')
    try:
        data, cr = api_get(url1.replace('&limit=1', '&limit=1'))
        if data:
            rec = data[0]
            print(f'  id:          {rec.get("id")}')
            print(f'  title:       {rec.get("title")}')
            print(f'  price:       {rec.get("price")}')
            print(f'  bedrooms:    {rec.get("bedrooms")}')
            print(f'  bathrooms:   {rec.get("bathrooms")}')
            print(f'  square_feet: {rec.get("square_feet")}')
            print(f'  address:     {rec.get("address")}')
            print(f'  listing_type:{rec.get("listing_type")}')
            print(f'  status:      {rec.get("status")}')
            print(f'  geo_lat:     {rec.get("geo_lat")}')
            print(f'  geo_lon:     {rec.get("geo_lon")}')
            # Print all keys
            print(f'  ALL keys:    {list(rec.keys())}')
    except Exception as e:
        print(f'  Error: {e}')

    print('\nDone.')


if __name__ == '__main__':
    asyncio.run(main())
