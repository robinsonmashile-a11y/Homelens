-- Migration: add city to listings so Cape Town and Johannesburg can be told apart.
-- Run in Supabase SQL Editor BEFORE the next scrape run. Idempotent.
-- ---------------------------------------------------------------------------

ALTER TABLE listings
    ADD COLUMN IF NOT EXISTS city text;

-- Every listing scraped so far is Cape Town.
UPDATE listings SET city = 'cape-town' WHERE city IS NULL;

ALTER TABLE listings
    ALTER COLUMN city SET DEFAULT 'cape-town',
    ALTER COLUMN city SET NOT NULL;

ALTER TABLE listings
    DROP CONSTRAINT IF EXISTS listings_city_check;
ALTER TABLE listings
    ADD CONSTRAINT listings_city_check
    CHECK (city IN ('cape-town', 'johannesburg'));

CREATE INDEX IF NOT EXISTS idx_listings_city ON listings (city);
CREATE INDEX IF NOT EXISTS idx_listings_city_suburb ON listings (city, suburb_normalised);
