-- Homelens — User Events (Analytics)
-- Run in: Supabase Dashboard → SQL Editor → New Query → Run
-- POPIA: no personal data stored. Session IDs are random UUIDs (reset every 24h).
--
-- After running this SQL:
--   1. Go to Settings → API → "Reload schema cache"
--   2. Deploy the track-event Edge Function

-- ============================================================
-- TABLE: user_events
-- Append-only analytics log. Never contains personal data.
-- Written by the track-event Edge Function (service role).
-- No public read policy — private analytics only.
-- ============================================================

CREATE TABLE IF NOT EXISTS user_events (
  id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Session identification (random UUID, resets every 24h — not a person)
  session_id             TEXT NOT NULL,

  -- Event classification
  event_type             TEXT NOT NULL,
  event_category         TEXT NOT NULL,

  -- Page and listing context
  page                   TEXT,
  suburb                 TEXT,
  listing_id             TEXT,
  listing_type           TEXT,

  -- Property context (numeric signals, not personal data)
  bedrooms               INTEGER,
  price                  INTEGER,
  homelens_score         INTEGER,
  investment_score       INTEGER,
  verdict                TEXT,

  -- User input values (what they typed/selected — not who they are)
  input_rent             INTEGER,
  input_suburb           TEXT,
  input_price            INTEGER,

  -- Engagement signals
  scroll_depth_pct       INTEGER,
  time_on_page_seconds   INTEGER,
  filter_type            TEXT,
  filter_value           TEXT,
  sort_by                TEXT,

  -- Technical context (no IP stored — POPIA compliant)
  device                 TEXT,
  screen_width           INTEGER,
  referrer               TEXT,

  -- Catch-all for any extra structured data
  metadata               JSONB,

  occurred_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Indexes for dashboard queries ────────────────────────────
CREATE INDEX IF NOT EXISTS idx_events_session
  ON user_events(session_id);

CREATE INDEX IF NOT EXISTS idx_events_type
  ON user_events(event_type);

CREATE INDEX IF NOT EXISTS idx_events_occurred
  ON user_events(occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_events_suburb
  ON user_events(suburb);

CREATE INDEX IF NOT EXISTS idx_events_listing
  ON user_events(listing_id);

-- ── Row Level Security ───────────────────────────────────────
-- No public read or write.
-- Only service role (Edge Function) can insert.
ALTER TABLE user_events ENABLE ROW LEVEL SECURITY;

-- No policies = anon key cannot read or write.
-- Service role bypasses RLS automatically.
