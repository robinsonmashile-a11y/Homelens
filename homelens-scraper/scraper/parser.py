from __future__ import annotations

import json
import logging

import anthropic

logger = logging.getLogger(__name__)

# Reuse a single client for the lifetime of the process
_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


async def parse_listing_description(description: str, title: str, price: int) -> dict:
    """
    Use Claude to extract structured property data from a free-text listing description.
    Returns a dict with property attributes; returns {} on any failure so the scraper
    can continue without crashing.
    """
    if not description and not title:
        return {}

    prompt = f"""Extract structured property data from this South African property listing.

Title: {title}
Price: R{price}
Description: {description}

Extract and return ONLY a JSON object with these fields:
{{
    "bedrooms": <integer or null>,
    "bathrooms": <float or null — use 0.5 for half baths>,
    "size_m2": <number or null — look for m², sqm, square metres>,
    "has_solar": <boolean — true if solar panels, inverter, solar geyser mentioned>,
    "has_parking": <boolean — true if parking, garage, carport mentioned>,
    "has_garden": <boolean — true if garden, yard, braai area mentioned>,
    "pet_friendly": <boolean or null — null if not mentioned>,
    "suburb_mentioned": <string or null — suburb name if clearly stated>,
    "listing_type_confirmed": <"rent" or "sale" — based on context>,
    "key_features": <array of strings — max 5 notable features>
}}

Return ONLY the JSON object. No explanation. No markdown. No backticks."""

    try:
        client = _get_client()
        response = client.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=500,
            messages=[{'role': 'user', 'content': prompt}],
        )
        raw = response.content[0].text.strip()
        # Strip accidental markdown fences
        if raw.startswith('```'):
            raw = raw.strip('`').lstrip('json').strip()
        return json.loads(raw)

    except json.JSONDecodeError as e:
        logger.warning('Parser JSON decode error: %s', e)
        return {}
    except Exception as e:
        logger.warning('Parser error: %s', e)
        return {}
