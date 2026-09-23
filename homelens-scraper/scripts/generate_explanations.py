"""
Homelens — "Why this score" explanation generator.

For every listing that has a signal but no explanation yet,
calls Claude API to generate plain-English market context
and writes the result to listing_explanations.

Usage:
    python scripts/generate_explanations.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(override=True)

import anthropic
from scraper.database import get_client

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('homelens.explanations')

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MODEL        = 'claude-sonnet-4-5'
MAX_TOKENS   = 700
BATCH_SIZE   = 5
BATCH_DELAY  = 2.0   # seconds between batches
MAX_RETRIES  = 3

# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_prompt(listing: dict, signal: dict) -> str:
    price        = listing.get('price') or 0
    bedrooms     = listing.get('bedrooms')
    size_m2      = listing.get('size_m2')
    title        = listing.get('title', 'Property listing')
    suburb       = listing.get('suburb_normalised', 'Cape Town')

    median_price   = signal.get('median_price', 0)
    gap_pct        = signal.get('gap_pct', 0)
    homelens_score = signal.get('homelens_score', 50)
    days_on_market = listing.get('days_on_market') or 'unknown'
    sample_size    = signal.get('sample_size', 0)
    confidence     = signal.get('confidence', 'low')

    direction = 'below' if gap_pct < 0 else 'above'
    abs_gap   = abs(round(gap_pct, 1))

    bed_str  = f'{bedrooms} bedroom{"s" if bedrooms != 1 else ""}' if bedrooms else 'unknown bedrooms'
    size_str = f'{size_m2}m²' if size_m2 else 'unknown size'

    return f"""You are a Cape Town property market expert writing for first-time buyers and investors.

Write a clear, honest, plain-English explanation about this property listing. No jargon. No hype. Just honest market context that helps someone decide whether to investigate further.

Property details:
- Address/Title: {title}
- Suburb: {suburb.replace('-', ' ').title()}
- Price: R{price:,}
- Bedrooms: {bed_str}
- Size: {size_str}
- Fair value (market median): R{median_price:,}
- Gap: {abs_gap}% {direction} market
- Homelens score: {homelens_score}/100
- Days listed: {days_on_market}
- Based on: {sample_size} comparable listings
- Confidence: {confidence}

Write four things:

1. DEAL_SUMMARY (one sentence, max 20 words):
A punchy honest summary of what this listing represents.
Example: "Priced R2.1m below comparable Oranjezicht sales — rare for this suburb."

2. NEIGHBOURHOOD_CONTEXT (2-3 sentences):
What makes this suburb attractive to buyers? What kind of people live there? What is the area known for? What has happened to prices historically in this area?
Be specific to Cape Town. Be honest — if an area is up-and-coming say so, if it is established say so.

3. EXPLANATION (3-4 sentences):
Why does this listing have this score? Explain the price gap in plain English. Reference the comparable sales if confidence is high. Be honest about uncertainty if confidence is low. End with what a buyer should do next.

4. CONFIDENCE_NOTE (1 sentence):
An honest note about how reliable this score is. If sample_size < 10 — be upfront that data is limited. If sample_size >= 20 — affirm the score is well-supported.

Return as JSON:
{{
  "deal_summary": "...",
  "neighbourhood_context": "...",
  "explanation": "...",
  "confidence_note": "..."
}}

Return JSON only. No markdown. No backticks."""


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def fetch_pending(client) -> list[dict]:
    """
    Return listings that have a signal but no explanation yet.
    Joins via Python since Supabase anon client doesn't support LEFT JOIN.
    """
    # All listing_ids that already have an explanation
    explained_result = client.table('listing_explanations').select('listing_id').execute()
    explained_ids    = {r['listing_id'] for r in (explained_result.data or [])}

    # All listing_ids that have a signal with a median_price
    signals_result = (
        client.table('listing_signals')
        .select('listing_id,suburb_normalised,listing_type,median_price,gap_pct,'
                'homelens_score,sample_size,confidence,verdict,z_score,'
                'median_price_per_m2,comparison_method')
        .filter('median_price', 'not.is', 'null')
        .execute()
    )
    signals = {
        r['listing_id']: r
        for r in (signals_result.data or [])
        if r['listing_id'] not in explained_ids
    }

    if not signals:
        return []

    # Fetch the actual listing details in pages of 100
    pending = []
    ids_list = list(signals.keys())

    for i in range(0, len(ids_list), 100):
        batch_ids = ids_list[i:i + 100]
        listings_result = (
            client.table('listings')
            .select('listing_id,title,suburb_normalised,price,bedrooms,bathrooms,'
                    'size_m2,price_per_m2,property_type,description,days_on_market')
            .in_('listing_id', batch_ids)
            .execute()
        )
        for listing in (listings_result.data or []):
            listing['_signal'] = signals[listing['listing_id']]
            pending.append(listing)

    return pending


def generate_explanation(ai_client: anthropic.Anthropic, listing: dict) -> dict | None:
    """
    Call Claude and return parsed JSON dict, or None on failure.
    Retries up to MAX_RETRIES times on API errors.
    """
    signal = listing['_signal']
    prompt = _build_prompt(listing, signal)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = ai_client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                messages=[{'role': 'user', 'content': prompt}],
            )
            raw = response.content[0].text.strip()

            # Strip accidental markdown fences
            if raw.startswith('```'):
                raw = raw.strip('`').lstrip('json').strip()

            parsed = json.loads(raw)

            # Validate required keys are present
            required = {'deal_summary', 'neighbourhood_context', 'explanation', 'confidence_note'}
            if not required.issubset(parsed.keys()):
                missing = required - parsed.keys()
                logger.warning('Missing keys %s for %s', missing, listing['listing_id'])
                return None

            return parsed

        except json.JSONDecodeError as e:
            logger.warning('JSON parse error attempt %d for %s: %s', attempt, listing['listing_id'], e)
            if attempt == MAX_RETRIES:
                return None
            time.sleep(attempt * 1.5)

        except anthropic.RateLimitError:
            logger.warning('Rate limit hit — waiting 30s (attempt %d)', attempt)
            time.sleep(30)

        except anthropic.APIError as e:
            # Billing errors are permanent — stop immediately, no point retrying.
            if hasattr(e, 'status_code') and e.status_code == 400:
                err_body = str(e).lower()
                if 'credit balance' in err_body or 'too low' in err_body or 'billing' in err_body:
                    logger.error('Anthropic billing error — credit balance too low. Add credits at console.anthropic.com/settings/billing')
                    raise SystemExit(1)
            logger.warning('API error attempt %d for %s: %s', attempt, listing['listing_id'], e)
            if attempt == MAX_RETRIES:
                return None
            time.sleep(attempt * 2)

    return None


def insert_explanation(client, listing_id: str, parsed: dict) -> None:
    from datetime import datetime, timezone
    client.table('listing_explanations').upsert(
        {
            'listing_id':             listing_id,
            'explanation':            parsed.get('explanation', ''),
            'neighbourhood_context':  parsed.get('neighbourhood_context', ''),
            'deal_summary':           parsed.get('deal_summary', ''),
            'confidence_note':        parsed.get('confidence_note', ''),
            'generated_at':           datetime.now(timezone.utc).isoformat(),
        },
        on_conflict='listing_id',
    ).execute()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    required = ['SUPABASE_URL', 'SUPABASE_KEY', 'ANTHROPIC_API_KEY']
    missing  = [k for k in required if not os.environ.get(k)]
    if missing:
        logger.error('Missing env vars: %s', ', '.join(missing))
        sys.exit(1)

    client    = get_client()
    ai_client = anthropic.Anthropic()

    print()
    print('━' * 55)
    print('  Homelens — Explanation Generator')
    print('━' * 55)

    pending = fetch_pending(client)
    total   = len(pending)

    if total == 0:
        print('  No pending listings — all explanations are up to date.')
        print('━' * 55)
        return

    print(f'  Pending listings : {total}')
    print(f'  Batch size       : {BATCH_SIZE}  (pause {BATCH_DELAY}s between batches)')
    print(f'  Estimated time   : ~{round(total * 3.5 / 60, 1)} min')
    print('━' * 55)
    print()

    generated = 0
    failed    = 0

    for i, listing in enumerate(pending, 1):
        result = generate_explanation(ai_client, listing)

        if result:
            try:
                insert_explanation(client, listing['listing_id'], result)
                suburb = listing.get('suburb_normalised', '').replace('-', ' ').title()
                title  = (listing.get('title') or 'Listing')[:50]
                print(f'  [{i:>4}/{total}] ✓  {title} ({suburb})')
                generated += 1
            except Exception as e:
                logger.error('DB write failed for %s: %s', listing['listing_id'], e)
                failed += 1
        else:
            suburb = listing.get('suburb_normalised', '')
            print(f'  [{i:>4}/{total}] ✗  {listing["listing_id"]} ({suburb}) — skipped')
            failed += 1

        # Pause between batches
        if i % BATCH_SIZE == 0 and i < total:
            time.sleep(BATCH_DELAY)

    print()
    print('━' * 55)
    print(f'  Generated : {generated}')
    print(f'  Failed    : {failed}')
    print(f'  Total     : {total}')
    print('━' * 55)
    print()


if __name__ == '__main__':
    main()
