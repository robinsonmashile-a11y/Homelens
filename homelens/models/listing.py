"""
Listing data model.

Represents one rental or for-sale property listing scraped from a source.
listing_id is a stable MD5 hash of the raw URL so the same listing
is always idempotent across scrape runs.

POPIA note: we deliberately exclude agent name, phone, and email fields.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Listing:
    raw_url: str
    source: str           # 'property24' | 'private_property' | 'gumtree' | 'facebook'
    listing_type: str     # 'rent' | 'sale' | 'development'
    suburb: str           # normalised slug e.g. 'sea-point'
    price: int            # ZAR

    bedrooms: Optional[int] = None
    bathrooms: Optional[int] = None
    size_m2: Optional[float] = None
    has_solar: bool = False
    has_parking: bool = False

    @property
    def listing_id(self) -> str:
        """Stable identifier — MD5 of the canonical listing URL."""
        return hashlib.md5(self.raw_url.encode("utf-8")).hexdigest()

    @property
    def price_per_m2(self) -> Optional[float]:
        if self.size_m2 and self.size_m2 > 0:
            return round(self.price / self.size_m2, 2)
        return None

    def to_insert_record(self) -> dict:
        """Dict ready to pass to supabase.table('listings').insert()."""
        now = _now_iso()
        return {
            "listing_id": self.listing_id,
            "source": self.source,
            "listing_type": self.listing_type,
            "suburb": self.suburb,
            "price": self.price,
            "bedrooms": self.bedrooms,
            "bathrooms": self.bathrooms,
            "size_m2": self.size_m2,
            "price_per_m2": self.price_per_m2,
            "has_solar": self.has_solar,
            "has_parking": self.has_parking,
            "first_seen": now,
            "last_seen": now,
            "price_at_first": self.price,
            "price_history": [{"price": self.price, "date": now}],
            "days_on_market": 0,
            "is_active": True,
            "raw_url": self.raw_url,
        }

    def to_update_record(self, existing_price: int, existing_history: list) -> dict:
        """
        Dict for updating an existing listing on a subsequent scrape.
        Appends to price_history only when price has changed.
        """
        now = _now_iso()
        history = list(existing_history)  # copy
        if self.price != existing_price:
            history.append({"price": self.price, "date": now})

        return {
            "price": self.price,
            "price_per_m2": self.price_per_m2,
            "last_seen": now,
            "price_history": history,
            "is_active": True,
        }
