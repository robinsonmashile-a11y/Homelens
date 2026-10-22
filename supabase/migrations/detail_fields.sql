-- Migration: detail-page fields captured by the scrapers.
-- Run in Supabase SQL Editor BEFORE the next scrape run.
-- Idempotent: ADD COLUMN IF NOT EXISTS throughout. Nothing is dropped or rewritten.
--
-- Already present (from earlier migrations / schema): description, size_m2,
-- bedrooms, bathrooms, pet_friendly, levy_monthly, rates_monthly.
-- (If the scrapers run before this migration, database.py silently skips the
-- new columns instead of failing — see _detail_columns_exist().)
-- ---------------------------------------------------------------------------

ALTER TABLE listings
    ADD COLUMN IF NOT EXISTS parking            smallint,      -- number of parking bays / garages (has_parking is only a default flag)
    ADD COLUMN IF NOT EXISTS erf_size           numeric,       -- land / stand size in m2 (never used as floor size)
    ADD COLUMN IF NOT EXISTS is_furnished       boolean,       -- NULL = not stated
    ADD COLUMN IF NOT EXISTS building_name      text,
    ADD COLUMN IF NOT EXISTS security           text,          -- e.g. '24-hour security, Security Gate'
    ADD COLUMN IF NOT EXISTS views              text,          -- e.g. 'Table Mountain, sea'
    ADD COLUMN IF NOT EXISTS levy_includes      text,          -- what the levy covers, when stated
    -- When the listing's own page was last opened for description/size/extras.
    -- Lets the scrapers skip listings already detailed (same idea as costs_checked_at).
    ADD COLUMN IF NOT EXISTS details_checked_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_listings_source_details_checked
    ON listings (source, details_checked_at);
