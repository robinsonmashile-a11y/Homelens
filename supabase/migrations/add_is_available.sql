-- is_available: one column answering "can a buyer still get this listing?"
--
--   is_active       = seen on the agency site in the latest successful scrape
--   listing_status  = live | under_offer | sold   (from the site badge / text)
--   is_available    = is_active AND listing_status = 'live' AND price > 0
--                     (no price = POA / extraction failed: cannot be compared or shown)
--
-- Generated, so it can never disagree with the two columns it derives from and
-- needs no scraper changes. is_active / listing_status stay as they are: "sold"
-- and "disappeared" are different outcomes, and inactive rows are still used as
-- price / rent reference data.
--
-- Run in the Supabase SQL editor. Safe to re-run.

ALTER TABLE listings
  ADD COLUMN IF NOT EXISTS is_available boolean
  GENERATED ALWAYS AS (
    COALESCE(is_active, false)
    AND COALESCE(listing_status, 'live') = 'live'
    AND COALESCE(price, 0) > 0
  ) STORED;

CREATE INDEX IF NOT EXISTS listings_is_available_idx
  ON listings (is_available)
  WHERE is_available;
