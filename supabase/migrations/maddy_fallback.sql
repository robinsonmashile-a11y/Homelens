-- Migration: Maddy fallback scoring. Run in Supabase SQL Editor. Idempotent, additive.
--
-- listing_signals rows now say who scored them. The statistical engine writes
-- 'engine'; the Maddy fallback pass (scripts/run_maddy_fallback.py) writes 'maddy'
-- for active listings the engine could not score (no usable baseline).
-- Maddy rows leave every statistical field (gap_pct, gap_rands, z_score, medians ...)
-- NULL and carry comparison_method = 'maddy_fallback'.
-- ---------------------------------------------------------------------------

ALTER TABLE listing_signals ADD COLUMN IF NOT EXISTS scored_by         TEXT NOT NULL DEFAULT 'engine';
ALTER TABLE listing_signals ADD COLUMN IF NOT EXISTS maddy_reason      TEXT;
ALTER TABLE listing_signals ADD COLUMN IF NOT EXISTS maddy_what_it_is  TEXT;
ALTER TABLE listing_signals ADD COLUMN IF NOT EXISTS input_hash        TEXT;   -- Maddy cache key

CREATE INDEX IF NOT EXISTS listing_signals_scored_by ON listing_signals (scored_by);
