-- Migration: house baselines on price per ERF (plot) m2, per suburb x plot-size band.
-- Run in Supabase SQL Editor. Idempotent and purely additive: no existing table
-- or constraint changes, so an engine run from an older branch is unaffected
-- (it never touches this table). Until this is applied the new engine scores
-- houses exactly as before.
-- The table is wiped and rebuilt per city on every engine run.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS suburb_erf_baselines (
  id                        SERIAL PRIMARY KEY,
  city                      TEXT NOT NULL,
  suburb_normalised         TEXT NOT NULL,
  listing_type              TEXT NOT NULL DEFAULT 'sale',
  erf_band                  TEXT NOT NULL,            -- e.g. '400-700'
  sample_size               INTEGER NOT NULL,
  median_price_per_erf_m2   NUMERIC,
  mad_price_per_erf_m2      NUMERIC,
  p25_price_per_erf_m2      NUMERIC,
  p75_price_per_erf_m2      NUMERIC,
  market_consistency        TEXT,
  has_sufficient_data       BOOLEAN NOT NULL DEFAULT FALSE,
  computed_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS suburb_erf_baselines_key
    ON suburb_erf_baselines (city, suburb_normalised, listing_type, erf_band);
