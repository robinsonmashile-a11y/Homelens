-- Homelens database schema
-- Paste this into the Supabase SQL Editor and click Run
-- supabase.com → your project → SQL Editor → New Query

-- ============================================================
-- TABLE: listings
-- One row per unique listing URL. Upserted on every scrape run.
-- ============================================================
CREATE TABLE IF NOT EXISTS listings (
    listing_id       TEXT PRIMARY KEY,         -- MD5 hash of the listing URL
    source           TEXT NOT NULL,            -- 'property24' | 'private_property' | 'gumtree' | 'facebook'
    listing_type     TEXT NOT NULL,            -- 'rent' | 'sale' | 'development'
    city             TEXT NOT NULL DEFAULT 'cape-town'
                     CHECK (city IN ('cape-town', 'johannesburg')),  -- suburb names repeat across cities
    suburb           TEXT NOT NULL,            -- normalised slug e.g. 'sea-point'
    price            INTEGER NOT NULL,         -- ZAR (monthly rent or asking price)
    bedrooms         SMALLINT,
    bathrooms        SMALLINT,
    size_m2          NUMERIC,                  -- NULL if not listed
    price_per_m2     NUMERIC,                  -- computed on insert/update
    has_solar        BOOLEAN DEFAULT FALSE,
    has_parking      BOOLEAN DEFAULT FALSE,
    first_seen       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    price_at_first   INTEGER NOT NULL,         -- original listing price at first_seen
    price_history    JSONB NOT NULL DEFAULT '[]'::JSONB,  -- [{price, date}, ...]
    days_on_market   INTEGER DEFAULT 0,        -- computed from first_seen
    is_active        BOOLEAN DEFAULT TRUE,
    raw_url          TEXT NOT NULL             -- full source listing URL
);

-- Index for suburb + type + bedrooms queries (baseline computation)
CREATE INDEX IF NOT EXISTS idx_listings_suburb_type_beds
    ON listings (suburb, listing_type, bedrooms)
    WHERE is_active = TRUE;

-- Index for signal computation queries
CREATE INDEX IF NOT EXISTS idx_listings_active_last_seen
    ON listings (last_seen DESC)
    WHERE is_active = TRUE;

-- ============================================================
-- TABLE: suburb_baselines
-- Recomputed daily after each scrape run.
-- Stores median price/m² per suburb × listing_type × bedrooms.
-- ============================================================
CREATE TABLE IF NOT EXISTS suburb_baselines (
    suburb           TEXT NOT NULL,
    listing_type     TEXT NOT NULL,            -- 'rent' | 'sale'
    bedrooms         SMALLINT NOT NULL,
    sample_size      INTEGER NOT NULL,         -- number of listings used
    median_price     INTEGER NOT NULL,         -- ZAR
    median_price_m2  NUMERIC,                  -- NULL if most listings lack size_m2
    p25_price        INTEGER NOT NULL,         -- 25th percentile
    p75_price        INTEGER NOT NULL,         -- 75th percentile
    avg_days_on_mkt  NUMERIC,
    computed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (suburb, listing_type, bedrooms)
);

-- ============================================================
-- TABLE: signals
-- One row per signal event. Never deleted — append only.
-- signal_type:
--   'overpriced_rental'  — rental > 10% above suburb median/m²
--   'undervalue_buy'     — sale  > 10% below suburb median/m²
--   'motivated_seller'   — 3+ price drops on same listing
--   'new_development'    — listing_type = 'development'
--   'price_drop'         — any single price reduction
-- ============================================================
CREATE TABLE IF NOT EXISTS signals (
    id               BIGSERIAL PRIMARY KEY,
    listing_id       TEXT NOT NULL REFERENCES listings (listing_id),
    signal_type      TEXT NOT NULL,
    discount_pct     NUMERIC,                  -- % above (positive) or below (negative) baseline
    baseline_price   INTEGER,                  -- fair value from suburb_baselines
    days_on_market   INTEGER,
    price_drop_count INTEGER DEFAULT 0,
    confidence       TEXT NOT NULL,            -- 'high' (20+) | 'medium' (10-19) | 'low' (5-9)
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index for listing-level signal lookups
CREATE INDEX IF NOT EXISTS idx_signals_listing_id
    ON signals (listing_id);

-- Index for time-ordered signal feed
CREATE INDEX IF NOT EXISTS idx_signals_created_at
    ON signals (created_at DESC);

-- ============================================================
-- Row Level Security
-- Enable RLS so the anon key can only SELECT (public read).
-- Service role key (server-side only) can INSERT/UPDATE.
-- ============================================================
ALTER TABLE listings         ENABLE ROW LEVEL SECURITY;
ALTER TABLE suburb_baselines ENABLE ROW LEVEL SECURITY;
ALTER TABLE signals          ENABLE ROW LEVEL SECURITY;

-- Public read access (used by Lovable frontend via anon key)
CREATE POLICY "public_read_listings"
    ON listings FOR SELECT TO anon USING (TRUE);

CREATE POLICY "public_read_baselines"
    ON suburb_baselines FOR SELECT TO anon USING (TRUE);

CREATE POLICY "public_read_signals"
    ON signals FOR SELECT TO anon USING (TRUE);

-- Service role has full access (used by the scraper via service_role key)
-- No explicit policy needed — service role bypasses RLS by default.
