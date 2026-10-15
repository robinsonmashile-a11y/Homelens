-- missed_runs: consecutive complete scrape runs in which a listing was not seen.
-- The scrapers only set is_active = false once this reaches 2, so a single
-- timeout / partial page / glitch no longer hides a live listing.
-- Reset to 0 whenever the listing is seen again. Safe to re-run.

ALTER TABLE listings
  ADD COLUMN IF NOT EXISTS missed_runs integer NOT NULL DEFAULT 0;
