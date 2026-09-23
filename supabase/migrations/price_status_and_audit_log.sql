-- Migration: price_status column + scrape_audit_log + backup table
-- Run in Supabase SQL Editor.
-- Idempotent: uses IF NOT EXISTS / ADD COLUMN IF NOT EXISTS throughout.
-- ---------------------------------------------------------------------------

-- 1. price_status on listings
-- Distinguishes POA (legitimate listing state) from extraction failure
-- from genuinely missing data. price=NULL alone cannot make this distinction.
ALTER TABLE listings
    ADD COLUMN IF NOT EXISTS price_status TEXT DEFAULT 'unknown'
        CHECK (price_status IN ('ok', 'poa', 'extraction_failed', 'not_applicable', 'unknown'));

-- Index for auditing / filtering
CREATE INDEX IF NOT EXISTS idx_listings_price_status
    ON listings (price_status);

-- Backfill: mark existing rows with a non-null price as 'ok'.
-- Rows with NULL price stay 'unknown' — they will be reclassified
-- on the next scraper run.
UPDATE listings
   SET price_status = 'ok'
 WHERE price IS NOT NULL
   AND price_status = 'unknown';

-- ---------------------------------------------------------------------------
-- 2. Audit log — one row per scraper run × source × listing_type
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scrape_audit_log (
    id                              SERIAL PRIMARY KEY,
    run_id                          UUID NOT NULL,
    source                          TEXT NOT NULL,
    listing_type                    TEXT NOT NULL,          -- 'sale' | 'rent'
    run_started_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    scraper_code_version            TEXT,                  -- git commit hash
    root_cause_bucket               TEXT,                  -- 'A_selector' | 'B_parse' | 'C_poa' | 'D_timing' | 'unknown'
    listings_attempted              INT,
    listings_extracted              INT,
    price_ok_count                  INT,
    price_poa_count                 INT,
    price_extraction_failed_count   INT,
    price_unknown_count             INT,
    expected_count_manual           INT,
    expected_tolerance_pct          NUMERIC DEFAULT 10,
    within_tolerance                BOOLEAN,
    notes                           TEXT
);

CREATE INDEX IF NOT EXISTS idx_scrape_audit_log_source_type
    ON scrape_audit_log (source, listing_type, run_started_at DESC);

-- ---------------------------------------------------------------------------
-- 3. Price backup table — snapshot before bulk re-scrape
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS listings_price_backup AS
    SELECT
        listing_id,
        source,
        listing_type,
        price,
        price_status,
        suburb_normalised,
        is_active,
        last_scraped_at,
        now() AS backed_up_at
    FROM listings
    WHERE listing_type = 'rent'
    LIMIT 0;   -- creates structure without copying data; script will INSERT

-- The actual snapshot INSERT is run from scripts/backup_rent_prices.py
-- before any scraper re-run.
