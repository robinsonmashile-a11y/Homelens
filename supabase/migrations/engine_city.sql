-- Migration: make engine baselines city-aware so Johannesburg can be scored.
-- Run in Supabase SQL Editor BEFORE the next engine run. Idempotent.
--
-- Suburb names repeat across cities (rosebank, kensington, newlands, observatory,
-- kenilworth, sandown, sagewood), so a baseline is identified by city + suburb + ...
-- suburb_baselines is wiped and rebuilt on every engine run, so no history is lost.
-- Until this is applied the engine keeps scoring Cape Town only, exactly as before.
-- ---------------------------------------------------------------------------

ALTER TABLE suburb_baselines ADD COLUMN IF NOT EXISTS city TEXT NOT NULL DEFAULT 'cape-town';

-- Replace the old unique key (no city) with one that includes the city.
DO $$
DECLARE c text;
BEGIN
  FOR c IN
    SELECT conname FROM pg_constraint
    WHERE conrelid = 'suburb_baselines'::regclass AND contype = 'u'
  LOOP
    EXECUTE format('ALTER TABLE suburb_baselines DROP CONSTRAINT %I', c);
  END LOOP;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS suburb_baselines_city_key
    ON suburb_baselines (city, suburb_normalised, listing_type, bedrooms, property_type);

-- Coverage view, now per city. 'city' is added as the LAST column so existing
-- readers selecting the old columns by name keep working.
CREATE OR REPLACE VIEW suburb_coverage AS
SELECT
  suburb_normalised,
  listing_type,
  COUNT(*)                                              AS total_groups,
  SUM(CASE WHEN has_sufficient_data THEN 1 ELSE 0 END) AS sufficient_groups,
  SUM(total_listings)                                   AS total_listings,
  SUM(sample_size_pm2)                                  AS listings_with_size,
  BOOL_OR(has_sufficient_data)                          AS any_sufficient,
  MAX(computed_at)                                      AS last_computed,
  city
FROM suburb_baselines
GROUP BY city, suburb_normalised, listing_type
ORDER BY total_listings DESC;
