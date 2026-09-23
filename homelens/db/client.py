"""
Supabase database client wrapper.

Uses the service_role key so it can write data (bypasses RLS).
The anon key is only for the Lovable frontend — never use it server-side.

Environment variables required:
    SUPABASE_URL          — from Supabase project settings → API
    SUPABASE_SERVICE_KEY  — from Supabase project settings → API → service_role (secret)
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from supabase import Client, create_client

from homelens.models.listing import Listing

logger = logging.getLogger(__name__)


def get_client() -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return create_client(url, key)


class ListingsDB:
    def __init__(self, client: Optional[Client] = None):
        self.db: Client = client or get_client()

    def upsert_listing(self, listing: Listing) -> str:
        """
        Insert a new listing or update an existing one.
        Returns 'inserted' | 'updated' | 'unchanged'.
        """
        # Check if listing already exists
        existing = (
            self.db.table("listings")
            .select("listing_id, price, price_history")
            .eq("listing_id", listing.listing_id)
            .maybe_single()
            .execute()
        )

        if existing.data is None:
            # New listing — insert
            record = listing.to_insert_record()
            self.db.table("listings").insert(record).execute()
            logger.info("Inserted new listing: %s (%s)", listing.suburb, listing.raw_url)
            return "inserted"

        # Existing listing — update last_seen, price, price_history
        existing_price = existing.data["price"]
        existing_history = existing.data["price_history"] or []
        update = listing.to_update_record(existing_price, existing_history)

        self.db.table("listings").update(update).eq(
            "listing_id", listing.listing_id
        ).execute()

        if listing.price != existing_price:
            logger.info(
                "Updated price for %s: R%s → R%s",
                listing.raw_url,
                existing_price,
                listing.price,
            )
            return "updated"

        return "unchanged"

    def mark_stale_inactive(self, suburb: str, active_ids: list[str]) -> int:
        """
        Any listing in this suburb that was NOT seen in the latest scrape
        is marked inactive (listing likely removed from the site).
        Returns number of listings deactivated.
        """
        if not active_ids:
            return 0

        result = (
            self.db.table("listings")
            .update({"is_active": False})
            .eq("suburb", suburb)
            .eq("is_active", True)
            .not_.in_("listing_id", active_ids)
            .execute()
        )
        count = len(result.data) if result.data else 0
        if count:
            logger.info("Marked %d stale listings inactive in %s", count, suburb)
        return count
