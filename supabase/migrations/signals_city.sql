-- Migration: give listing_signals its own city column.
-- Run in Supabase SQL Editor. Idempotent; safe to re-run.
--
-- listing_signals only carried suburb_normalised, and suburb names repeat across cities
-- (ferndale, sandown, morningside, oakdene ... exist in Johannesburg only), so filtering a
-- city's scores by suburb name leaked the other city's rows. city is copied from the listing
-- each signal belongs to (listings.city, same format: 'cape-town' / 'johannesburg').
-- The engine and Maddy now write it on every row; this backfills the existing rows.
-- Until this is applied both writers behave exactly as before (they check for the column).
-- ---------------------------------------------------------------------------

ALTER TABLE listing_signals ADD COLUMN IF NOT EXISTS city TEXT;

-- Backfill: city of the parent listing. Orphans (no matching listing) are left NULL.
UPDATE listing_signals ls
SET    city = l.city
FROM   listings l
WHERE  l.listing_id = ls.listing_id
  AND  ls.city IS DISTINCT FROM l.city;

CREATE INDEX IF NOT EXISTS listing_signals_city_idx ON listing_signals (city);

-- Verification (run these after):
--   SELECT city, count(*) FROM listing_signals GROUP BY city ORDER BY 2 DESC;      -- NULL row = orphans
--   SELECT city, count(*) FROM listing_signals WHERE suburb_normalised = 'ferndale' GROUP BY city;
--   SELECT city, count(*) FROM listing_signals WHERE suburb_normalised IN ('woodstock','sea-point') GROUP BY city;
