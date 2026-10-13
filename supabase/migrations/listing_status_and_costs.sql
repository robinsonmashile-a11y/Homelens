-- Migration: listing status (live / under offer / sold) + monthly levies & rates
-- Run in Supabase SQL Editor BEFORE the next scrape run.
-- Idempotent: ADD COLUMN IF NOT EXISTS throughout.
-- ---------------------------------------------------------------------------

ALTER TABLE listings
    ADD COLUMN IF NOT EXISTS listing_status                text DEFAULT 'live',
    ADD COLUMN IF NOT EXISTS status_changed_at             timestamptz,
    ADD COLUMN IF NOT EXISTS levy_monthly                  numeric,
    ADD COLUMN IF NOT EXISTS rates_monthly                 numeric,
    ADD COLUMN IF NOT EXISTS levies_rates_combined_monthly numeric,
    ADD COLUMN IF NOT EXISTS costs_source                  text,   -- 'site' | 'fallback' | 'description'
    ADD COLUMN IF NOT EXISTS status_source                 text,   -- 'site' | 'fallback' | 'default'
    ADD COLUMN IF NOT EXISTS costs_raw_snippet             text,
    -- Not in the original spec: when the listing page was last opened for
    -- levy/rates. Lets the scrapers skip listings already checked (most sale
    -- listings show no costs, so "costs IS NULL" alone would re-visit them
    -- every run).
    ADD COLUMN IF NOT EXISTS costs_checked_at              timestamptz;

-- Existing rows count as live (the column default only applies to new inserts
-- on some Postgres versions' ADD COLUMN paths, so make it explicit).
UPDATE listings SET listing_status = 'live' WHERE listing_status IS NULL;

ALTER TABLE listings
    DROP CONSTRAINT IF EXISTS listings_listing_status_check;
ALTER TABLE listings
    ADD CONSTRAINT listings_listing_status_check
    CHECK (listing_status IN ('live', 'under_offer', 'sold'));

CREATE INDEX IF NOT EXISTS idx_listings_status
    ON listings (listing_status);
CREATE INDEX IF NOT EXISTS idx_listings_source_costs_checked
    ON listings (source, costs_checked_at);
