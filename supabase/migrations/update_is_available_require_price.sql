-- Redefine is_available so a listing without a price is never "available".
--
--   is_available = is_active AND listing_status = 'live' AND price > 0
--
-- POA ("price on application") and extraction_failed listings have no price, so
-- they cannot be compared, scored or shown. Doing it here means they drop out
-- automatically — even though the scrapers set is_active = true each time they
-- see the listing — without having to re-run fix_suburbs.py after every scrape.
--
-- Postgres cannot change a stored generated column's expression in place on all
-- versions, so drop and re-add it (nothing depends on it yet). Run in the
-- Supabase SQL editor. Safe to re-run.

DROP INDEX IF EXISTS listings_is_available_idx;
ALTER TABLE listings DROP COLUMN IF EXISTS is_available;

ALTER TABLE listings
  ADD COLUMN is_available boolean
  GENERATED ALWAYS AS (
    COALESCE(is_active, false)
    AND COALESCE(listing_status, 'live') = 'live'
    AND COALESCE(price, 0) > 0
  ) STORED;

CREATE INDEX listings_is_available_idx
  ON listings (is_available)
  WHERE is_available;
