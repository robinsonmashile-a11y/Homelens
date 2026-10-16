"""
Homelens Insight Engine v4 — definitive rewrite.

Pipeline (run via scripts/run_engine.py):
  Step 1 — populate_property_types()      classify listings by title keywords
  Step 2 — compute_suburb_baselines()     two-track stats + sufficiency gate
  Step 3 — compute_listing_signals()      z-score scoring (sufficient baselines only)
  Step 4 — suburb_attractiveness lookup   read from table if it exists

Scoring uses a ROBUST z-score against group median/MAD (v4.1); two tracks:
  - Track 1: price_per_m2  (listings with valid size_m2 + price_per_m2)
  - Track 2: price_only    (all priced listings in group, fallback)

Robust z (v4.1 — replaces std-dev z):
  z = (x - median) / max(1.4826 * MAD, 0.10 * median), capped to +/-3.
  MAD (median absolute deviation) cannot be inflated by a single outlier the
  way the standard deviation can, so one R110k/month rental in a R28k suburb
  no longer masks genuine over/under-pricing of its neighbours. The 10%%-of-
  median floor prevents divide-by-zero on identical-price groups, and the
  +/-3 cap stops a single data error producing absurd scores. std_dev is
  still computed and stored for reference/audit, but no longer drives z.

Sufficiency gate:
  A baseline group is SUFFICIENT only when sample_size >= 5 AND a robust
  spread can be established (median present and > 0 on that track).

Blended baselines (v4.2):
  Track-1 groups with 2-4 sized listings blend their median with the same
  suburb's price-per-m2 across all bedroom counts (weight = n/(n+8)).
  Cross-suburb borrowing is deliberately NOT supported. Blended signals are
  score-COMPRESSED into a 15-50 band (ordering preserved, ceiling enforced),
  carry confidence='low_blended', and can never receive 'strong_deal' or
  'overpriced' verdicts. Backtested on Homelens
  data: blended error 8.5%% vs 9.8%% for the tiny local sample alone.
  Signals are only computed for listings backed by a sufficient baseline,
  on a track that covers that listing's data availability.

MIGRATION SQL — run ONCE in Supabase SQL editor before first run:
─────────────────────────────────────────────────────────────────────────────
  See MIGRATION_SQL constant below.
─────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import logging
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ── Migration SQL ─────────────────────────────────────────────────────────────

MIGRATION_SQL = """\
-- Step 1: property_type on listings
ALTER TABLE listings ADD COLUMN IF NOT EXISTS property_type TEXT DEFAULT 'other';

-- Step 2: rebuild suburb_baselines (v4 schema)
DROP TABLE IF EXISTS suburb_baselines CASCADE;
CREATE TABLE suburb_baselines (
  id                       SERIAL PRIMARY KEY,

  -- Group identity
  suburb_normalised        TEXT NOT NULL,
  listing_type             TEXT NOT NULL,
  bedrooms                 SMALLINT NOT NULL,
  property_type            TEXT NOT NULL,

  -- Sample sizes
  total_listings           INTEGER DEFAULT 0,
  listings_with_size       INTEGER DEFAULT 0,
  listings_without_size    INTEGER DEFAULT 0,

  -- Track 1: price per m2
  sample_size_pm2          INTEGER DEFAULT 0,
  median_price_per_m2      NUMERIC,
  std_dev_price_per_m2     NUMERIC,
  mad_price_per_m2         NUMERIC,
  p25_price_per_m2         NUMERIC,
  p75_price_per_m2         NUMERIC,
  cv_price_per_m2          NUMERIC,
  market_consistency       TEXT,

  -- Track 2: raw price
  sample_size_price        INTEGER DEFAULT 0,
  median_price             INTEGER,
  std_dev_price            NUMERIC,
  mad_price                NUMERIC,
  p25_price                INTEGER,
  p75_price                INTEGER,

  -- Sufficiency gate
  has_sufficient_data      BOOLEAN DEFAULT FALSE,
  sufficient_track         TEXT,
  insufficiency_reason     TEXT,

  computed_at              TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(suburb_normalised, listing_type, bedrooms, property_type)
);

-- Step 3: rebuild listing_signals
DROP TABLE IF EXISTS listing_signals CASCADE;
CREATE TABLE listing_signals (
  listing_id               TEXT PRIMARY KEY,
  suburb_normalised        TEXT,
  listing_type             TEXT,
  bedrooms                 SMALLINT,
  property_type            TEXT,

  comparison_method        TEXT,

  listing_price_per_m2     NUMERIC,
  median_price_per_m2      NUMERIC,
  std_dev_price_per_m2     NUMERIC,
  mad_price_per_m2         NUMERIC,

  listing_price            INTEGER,
  median_price             INTEGER,
  std_dev_price            NUMERIC,
  mad_price                NUMERIC,

  z_score                  NUMERIC,
  gap_pct                  NUMERIC,
  gap_rands                INTEGER,
  gap_annual               INTEGER,

  raw_score                INTEGER,
  sample_size_ceiling      INTEGER,
  attractiveness_score     INTEGER,
  final_price_score        INTEGER,
  homelens_score           INTEGER,

  verdict                  TEXT,
  verdict_label            TEXT,
  deal_badge               TEXT,

  confidence               TEXT,
  sample_size              INTEGER,
  market_consistency       TEXT,

  is_luxury                BOOLEAN DEFAULT FALSE,
  is_outlier               BOOLEAN DEFAULT FALSE,

  computed_at              TIMESTAMPTZ DEFAULT NOW()
);

-- Step 4: suburb_coverage view (for frontend)
CREATE OR REPLACE VIEW suburb_coverage AS
SELECT
  suburb_normalised,
  listing_type,
  COUNT(*)                                              AS total_groups,
  SUM(CASE WHEN has_sufficient_data THEN 1 ELSE 0 END) AS sufficient_groups,
  SUM(total_listings)                                   AS total_listings,
  SUM(sample_size_pm2)                                  AS listings_with_size,
  BOOL_OR(has_sufficient_data)                          AS any_sufficient,
  MAX(computed_at)                                      AS last_computed
FROM suburb_baselines
GROUP BY suburb_normalised, listing_type
ORDER BY total_listings DESC;
"""

# v4.1 incremental migration — run ONCE in Supabase SQL editor on an EXISTING
# database (fresh installs get these columns from MIGRATION_SQL above).
MAD_MIGRATION_SQL = """\
ALTER TABLE suburb_baselines ADD COLUMN IF NOT EXISTS mad_price_per_m2 NUMERIC;
ALTER TABLE suburb_baselines ADD COLUMN IF NOT EXISTS mad_price        NUMERIC;
ALTER TABLE listing_signals  ADD COLUMN IF NOT EXISTS mad_price_per_m2 NUMERIC;
ALTER TABLE listing_signals  ADD COLUMN IF NOT EXISTS mad_price        NUMERIC;
"""

# v4.2 incremental migration — run ONCE in Supabase SQL editor.
BLEND_MIGRATION_SQL = """\
ALTER TABLE suburb_baselines ADD COLUMN IF NOT EXISTS pm2_blended_available       BOOLEAN DEFAULT FALSE;
ALTER TABLE suburb_baselines ADD COLUMN IF NOT EXISTS blended_median_price_per_m2 NUMERIC;
ALTER TABLE suburb_baselines ADD COLUMN IF NOT EXISTS blended_mad_price_per_m2    NUMERIC;
ALTER TABLE suburb_baselines ADD COLUMN IF NOT EXISTS pm2_parent_sample_size      INTEGER;
ALTER TABLE suburb_baselines ADD COLUMN IF NOT EXISTS pm2_blend_weight            NUMERIC;
ALTER TABLE listing_signals  ADD COLUMN IF NOT EXISTS baseline_method             TEXT DEFAULT 'direct';
ALTER TABLE listing_signals  ADD COLUMN IF NOT EXISTS parent_sample_size          INTEGER;
ALTER TABLE listing_signals  ADD COLUMN IF NOT EXISTS blend_weight                NUMERIC;
"""

# v4.3 incremental migration — run ONCE in Supabase SQL editor.
BADGE_MIGRATION_SQL = """\
ALTER TABLE listing_signals ADD COLUMN IF NOT EXISTS deal_badge TEXT;
"""

# ── Quality filters ───────────────────────────────────────────────────────────

SIZE_MIN   = 15     # m²   — below this is a data error
SIZE_MAX   = 800    # m²   — above this is likely ERF/plot size

# Per listing_type price-per-m² and price bounds
_PM2_BOUNDS: dict[str, tuple[float, float]] = {
    'sale': (2_000,    200_000),
    'rent': (10,       2_000),
}
_PRICE_BOUNDS: dict[str, tuple[int, int]] = {
    'sale': (100_000,  500_000_000),
    'rent': (1_000,    500_000),
}

SUFFICIENCY_MIN_N = 5   # minimum sample size for a track to be sufficient

# ── Robust z-score constants (v4.1) ──────────────────────────────────────────
MAD_TO_SIGMA        = 1.4826  # standard consistency constant: scales MAD to be
                              # comparable with a normal std dev. Do not change.
MIN_SPREAD_FRACTION = 0.10    # spread floor as fraction of median — guards
                              # against MAD=0 (identical prices) and absurdly
                              # tight spreads producing huge z values.
Z_CAP               = 3.0     # |z| ceiling — one data error can never produce
                              # an unbounded score or a R10m "gap".

# ── Within-suburb blended baselines (v4.2) ───────────────────────────────────
# Track-1 groups with only 2-4 sized listings get a BLENDED baseline: their own
# median mixed with the same suburb's price-per-m² across ALL bedroom counts.
# Backtested on Homelens data: median error 8.5% vs 9.8% for the tiny sample
# alone; cross-suburb ("area") blending tested WORSE (19.4%) and is deliberately
# not implemented. Blended signals carry hard guardrails (see BLENDED_* below).
BLEND_MIN_N          = 2      # smallest group eligible for blending
BLEND_MAX_N          = 4      # groups >= 5 use direct baselines as before
BLEND_K              = 8      # shrinkage constant: weight = n / (n + K)
BLEND_PARENT_MIN_N   = 5      # parent (same suburb+type, all beds) minimum size
BLENDED_SCORE_CEILING = 50    # blended signals can never score above this
BLENDED_CONFIDENCE   = 'low_blended'  # frontend badge: limited local data

# ── Property type classification ──────────────────────────────────────────────

from .property_types import classify_property_type as _classify_property_type  # noqa: E402


# ── Robust statistics (v4.1) ─────────────────────────────────────────────────

def _mad(values: list[float]) -> float:
    """Median absolute deviation: median of |x - median(values)|."""
    if not values:
        return 0.0
    m = statistics.median(values)
    return statistics.median([abs(v - m) for v in values])


def _robust_spread(median: float | None, mad: float | None) -> float | None:
    """Outlier-resistant stand-in for std dev. None if no median available."""
    if median is None or median <= 0:
        return None
    return max(MAD_TO_SIGMA * (mad or 0.0), MIN_SPREAD_FRACTION * median)


def _robust_z(x: float, median: float, mad: float) -> float | None:
    """Drop-in replacement for (x - median) / std_dev, capped to +/- Z_CAP."""
    spread = _robust_spread(median, mad)
    if not spread:
        return None
    z = (x - median) / spread
    return round(max(-Z_CAP, min(Z_CAP, z)), 3)


# ── Scoring tables ────────────────────────────────────────────────────────────

def _raw_score_from_z(z: float) -> int:
    if z <= -2.0: return 95
    if z <= -1.5: return 85
    if z <= -1.0: return 75
    if z <= -0.5: return 65
    if z <=  0.5: return 55
    if z <=  1.0: return 45
    if z <=  1.5: return 35
    if z <=  2.0: return 25
    return 15


def _ceiling_and_confidence(n: int) -> tuple[int | None, str]:
    if n >= 15: return 100, 'high'
    if n >= 10: return 85,  'medium-high'
    if n >=  7: return 70,  'medium'
    if n >=  5: return 55,  'low'
    if n >=  3: return 40,  'very_low'
    return None, 'insufficient'


def _verdict_from_z(z: float) -> tuple[str, str]:
    if z <= -1.5: return 'strong_deal', 'Significantly below market'
    if z <= -0.5: return 'deal',        'Below market'
    if z <=  0.5: return 'fair',        'Fair market price'
    if z <=  1.5: return 'above',       'Above market'
    return 'overpriced', 'Significantly overpriced'


# v4.2.1: hedged verdict labels — shown whenever the engine is working from
# thin evidence (blended baselines, or direct baselines with only 5-6
# listings). Slugs are unchanged so frontend colours/filters keep working;
# only the display text softens. Confident labels stay reserved for
# well-evidenced baselines, which makes them mean more.
_HEDGED_LABELS: dict[str, str] = {
    'strong_deal': 'Possibly well below market',
    'deal':        'Possibly below market',
    'fair':        'Near market average — limited data',
    'above':       'Possibly above market',
    'overpriced':  'Possibly well above market',
}


def _cap_blended_verdict(verdict: str, label: str) -> tuple[str, str]:
    """
    v4.2 guardrail: blended baselines (2-4 local listings + suburb parent) may
    never award the extreme verdicts. 'Steal of the week' and 'Significantly
    overpriced' remain reserved for real, sufficient baselines. Labels are
    always hedged (v4.2.1).
    """
    if verdict == 'strong_deal': verdict = 'deal'
    elif verdict == 'overpriced': verdict = 'above'
    return verdict, _HEDGED_LABELS.get(verdict, label)


def _blended_final_score(raw: int) -> int:
    """
    v4.2.1: COMPRESS the raw score into the 15..BLENDED_SCORE_CEILING band
    instead of clipping at the ceiling. A hard min(raw, 50) flattened every
    better-than-fair blended listing to exactly 50, so a deal at z=-0.9 and a
    fair listing at z=+0.2 scored identically and ranking was lost. Linear
    compression keeps the ceiling AND the ordering:
      raw 95 -> 50, 85 -> 46, 75 -> 41, 65 -> 37, 55 -> 33 (fair),
      raw 45 -> 28, 35 -> 24, 25 -> 19, 15 -> 15.
    """
    raw_min, raw_max = 15, 95            # bounds of _raw_score_from_z
    span = BLENDED_SCORE_CEILING - raw_min
    return round(raw_min + (raw - raw_min) * span / (raw_max - raw_min))


# ── v4.3: deal badge — the buyer-facing label, driven PURELY by gap % ─────────
# Independent of the Homelens score/rating. gap_pct is negative = below market.
# The badge answers "how good is this price?"; the Homelens rating answers "how
# much do we back it?" — two separate axes, shown side by side. A thin-data
# listing can legitimately carry 'excellent_deal' (Option A); its low rating
# beside the badge is what signals caution. Thresholds validated on real data:
# 25%/10% put ~15% of stock in Excellent (scarce enough to mean something).
BADGE_EXCELLENT = 25.0   # % below market for Excellent Deal
BADGE_GOOD      = 10.0   # % below market for Good Deal / above for Above Market
BADGE_OVERPRICED = 25.0  # % above market for Overpriced

def _deal_badge(gap_pct: float | None) -> str | None:
    """Buyer-facing badge from gap %. None only when gap is unavailable."""
    if gap_pct is None:
        return None
    if gap_pct <= -BADGE_EXCELLENT: return 'excellent_deal'
    if gap_pct <= -BADGE_GOOD:      return 'good_deal'
    if gap_pct <   BADGE_GOOD:      return 'fair'
    if gap_pct <   BADGE_OVERPRICED: return 'above_market'
    return 'overpriced'


# ── Shared helpers ────────────────────────────────────────────────────────────

def _supabase():
    from .database import get_client
    return get_client()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _paginate(query_fn, page_size: int = 1000) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        batch = (query_fn(offset, page_size).data or [])
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def _is_schema_error(e: Exception) -> bool:
    err = str(e).lower()
    return (
        'pgrst204' in err or '42703' in err or 'schema cache' in err
        or 'could not find' in err or 'does not exist' in err
    )


# ── Schema check ─────────────────────────────────────────────────────────────

def check_schema(client) -> bool:
    """
    Probe for required columns.  Returns True if schema is ready.
    On failure prints the migration SQL and returns False.
    """
    probes = [
        ('listings',         'listing_id,property_type'),
        ('suburb_baselines', 'suburb_normalised,property_type,has_sufficient_data,sufficient_track'),
        ('listing_signals',  'listing_id,z_score,property_type'),
    ]
    for table, cols in probes:
        try:
            client.table(table).select(cols).limit(1).execute()
        except Exception as e:
            if _is_schema_error(e):
                print()
                print(f'  ⚠  Schema not ready (missing columns in {table}).')
                print('  Run the migration SQL in Supabase SQL editor, then re-run:')
                print()
                print('  ' + '─' * 65)
                for line in MIGRATION_SQL.strip().splitlines():
                    print(f'  {line}')
                print('  ' + '─' * 65)
                print()
                return False
            raise
    return True


# ── Step 1 — Populate property_type ──────────────────────────────────────────

def populate_property_types(client) -> dict[str, int]:
    """
    Classify all listings where property_type IS NULL or 'other'.
    Batch-updates in groups of 100.
    Returns dict of {property_type: total_count}.
    """
    logger.info('[1/4] Populating property_type from title keywords...')

    rows = _paginate(lambda off, lim: (
        client.table('listings')
        .select('listing_id,title,property_type')
        .range(off, off + lim - 1)
        .execute()
    ))

    bucket: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        if r.get('property_type') not in (None, 'other'):
            continue
        ptype = _classify_property_type(r.get('title') or '')
        bucket[ptype].append(r['listing_id'])

    for ptype, ids in bucket.items():
        for i in range(0, len(ids), 100):
            batch = ids[i : i + 100]
            client.table('listings').update(
                {'property_type': ptype}
            ).in_('listing_id', batch).execute()

    all_rows = _paginate(lambda off, lim: (
        client.table('listings')
        .select('property_type')
        .range(off, off + lim - 1)
        .execute()
    ))
    final_counts: dict[str, int] = defaultdict(int)
    for r in all_rows:
        final_counts[r.get('property_type') or 'other'] += 1

    print('\n  property_type counts (all listings):')
    for ptype, cnt in sorted(final_counts.items(), key=lambda x: -x[1]):
        print(f'    {ptype:<12}: {cnt:,}')

    return dict(final_counts)


# ── Step 2 — Compute suburb baselines ────────────────────────────────────────

def compute_suburb_baselines(client) -> dict[str, dict]:
    """
    Two-track baselines per group (suburb × listing_type × bedrooms × property_type).

    Track 1 (price_per_m2): listings with valid size_m2 + price_per_m2,
                             filtered by listing-type-specific pm2 bounds.
    Track 2 (price):        all priced listings filtered by listing-type price bounds.

    Sufficiency gate (v4.1):
      - Track is sufficient when sample_size >= 5 AND a robust spread exists
        (median present and > 0; MAD may be 0 thanks to the spread floor)
      - has_sufficient_data = TRUE if any track is sufficient
      - Groups that don't pass are stored but flagged has_sufficient_data = FALSE

    Returns in-memory dict keyed by
    '{suburb}:{listing_type}:{bedrooms}:{property_type}'.
    """
    logger.info('[2/4] Computing suburb baselines...')

    def _fetch(off, lim):
        try:
            return (
                client.table('listings')
                .select('listing_id,suburb_normalised,listing_type,'
                        'bedrooms,property_type,price,size_m2,price_per_m2')
                .eq('is_active', True)
                .eq('city', 'cape-town')  # baselines are per-suburb; suburb names repeat across cities
                .eq('is_luxury', False)
                .eq('is_outlier', False)
                .filter('suburb_normalised', 'not.is', 'null')
                .filter('price', 'not.is', 'null')
                .filter('bedrooms', 'not.is', 'null')
                .range(off, off + lim - 1)
                .execute()
            )
        except Exception as e:
            if _is_schema_error(e):
                logger.warning('  is_luxury/is_outlier columns missing — '
                               'computing baselines without luxury/outlier filter')
                return (
                    client.table('listings')
                    .select('listing_id,suburb_normalised,listing_type,'
                            'bedrooms,property_type,price,size_m2,price_per_m2')
                    .eq('is_active', True)
                    .eq('city', 'cape-town')  # baselines are per-suburb; suburb names repeat across cities
                    .filter('suburb_normalised', 'not.is', 'null')
                    .filter('price', 'not.is', 'null')
                    .filter('bedrooms', 'not.is', 'null')
                    .range(off, off + lim - 1)
                    .execute()
                )
            raise

    rows = _paginate(_fetch)
    logger.info('  %d active priced listings fetched', len(rows))

    # Accumulate per-group data
    total_counter:     dict[tuple, int]        = defaultdict(int)
    with_size_counter: dict[tuple, int]        = defaultdict(int)
    pm2_groups:        dict[tuple, list[float]] = defaultdict(list)
    price_groups:      dict[tuple, list[int]]   = defaultdict(list)

    for r in rows:
        listing_type = r.get('listing_type') or 'sale'
        key = (
            r.get('suburb_normalised') or 'unknown',
            listing_type,
            r.get('bedrooms'),
            r.get('property_type') or 'other',
        )

        total_counter[key] += 1
        if r.get('size_m2') is not None:
            with_size_counter[key] += 1

        price     = r.get('price')
        size_m2   = r.get('size_m2')
        pm2       = r.get('price_per_m2')

        price_lo, price_hi = _PRICE_BOUNDS.get(listing_type, _PRICE_BOUNDS['sale'])
        pm2_lo,   pm2_hi  = _PM2_BOUNDS.get(listing_type,   _PM2_BOUNDS['sale'])

        if price is not None and price_lo <= price <= price_hi:
            price_groups[key].append(int(price))

        if (size_m2 is not None and SIZE_MIN <= size_m2 <= SIZE_MAX
                and pm2 is not None and pm2_lo <= pm2 <= pm2_hi):
            pm2_groups[key].append(float(pm2))

    # Clear existing baselines
    try:
        client.table('suburb_baselines') \
              .delete().neq('suburb_normalised', '____never____').execute()
        logger.info('  suburb_baselines cleared')
    except Exception as e:
        logger.warning('  Could not clear suburb_baselines: %s', e)

    baselines: dict[str, dict] = {}
    inserted   = 0
    all_keys   = set(total_counter)

    # v4.2: parent pools for blending — same suburb + listing_type +
    # property_type, ALL bedroom counts, price-per-m² only. Location stays
    # fixed; cross-suburb borrowing is deliberately not supported (backtested
    # worse than using the tiny local sample alone).
    parent_pm2: dict[tuple, list[float]] = defaultdict(list)
    for (sub, lt, _bed, pt), vals in pm2_groups.items():
        parent_pm2[(sub, lt, pt)].extend(vals)

    for key in all_keys:
        suburb, listing_type, bedrooms, property_type = key

        total_count = total_counter[key]
        with_size   = with_size_counter.get(key, 0)
        without_sz  = total_count - with_size

        pm2_vals   = pm2_groups.get(key, [])
        price_vals = price_groups.get(key, [])
        n_pm2      = len(pm2_vals)
        n_price    = len(price_vals)

        # ── Track 1 stats ─────────────────────────────────────────────────
        if n_pm2 >= 2:
            s_pm2      = sorted(pm2_vals)
            median_pm2 = round(statistics.median(pm2_vals), 2)
            try:
                std_pm2 = round(statistics.stdev(pm2_vals), 2)   # kept for audit
            except statistics.StatisticsError:
                std_pm2 = None
            mad_pm2    = round(_mad(pm2_vals), 2)
            spread_pm2 = _robust_spread(median_pm2, mad_pm2)
            p25_pm2    = round(s_pm2[n_pm2 // 4], 2)
            p75_pm2    = round(s_pm2[3 * n_pm2 // 4], 2)
            # v4.1: consistency now judged on the robust spread, so one outlier
            # can no longer flip a suburb from 'consistent' to 'volatile'.
            cv         = round(spread_pm2 / median_pm2, 4) if (spread_pm2 and median_pm2 > 0) else None
            if cv is None:
                consistency = None
            elif cv < 0.15:
                consistency = 'very_consistent'
            elif cv < 0.25:
                consistency = 'consistent'
            elif cv < 0.40:
                consistency = 'moderate'
            else:
                consistency = 'volatile'
        else:
            median_pm2 = std_pm2 = mad_pm2 = p25_pm2 = p75_pm2 = cv = consistency = None

        # ── Track 2 stats ─────────────────────────────────────────────────
        if n_price >= 2:
            s_price      = sorted(price_vals)
            median_price = int(statistics.median(price_vals))
            try:
                std_price = round(statistics.stdev(price_vals), 2)  # kept for audit
            except statistics.StatisticsError:
                std_price = None
            mad_price    = round(_mad([float(v) for v in price_vals]), 2)
            p25_price    = int(s_price[n_price // 4])
            p75_price    = int(s_price[3 * n_price // 4])
        else:
            median_price = std_price = mad_price = p25_price = p75_price = None

        # ── Sufficiency gate ──────────────────────────────────────────────
        # v4.1: sufficiency now requires a robust spread rather than std > 0.
        # The MIN_SPREAD_FRACTION floor means identical-price groups (MAD = 0,
        # e.g. new-development launch pricing) are now scoreable instead of
        # being rejected as 'insufficient_variance'.
        t1_sufficient = (n_pm2 >= SUFFICIENCY_MIN_N
                         and _robust_spread(median_pm2, mad_pm2) is not None)
        t2_sufficient = (n_price >= SUFFICIENCY_MIN_N
                         and _robust_spread(median_price, mad_price) is not None)

        # ── v4.2: within-suburb blended baseline (Track 1 only) ──────────
        # Eligible when the group itself is too small for a direct Track-1
        # baseline but has 2-4 sized listings, and the same suburb offers at
        # least BLEND_PARENT_MIN_N other sized listings across bedroom counts.
        blended_available  = False
        blended_median_pm2 = blended_mad_pm2 = blend_weight = None
        parent_n           = None
        if not t1_sufficient and BLEND_MIN_N <= n_pm2 <= BLEND_MAX_N:
            pool = list(parent_pm2.get((suburb, listing_type, property_type), []))
            for v in pm2_vals:              # exclude the group's own listings
                pool.remove(v)
            parent_n = len(pool)
            if parent_n >= BLEND_PARENT_MIN_N:
                w            = round(n_pm2 / (n_pm2 + BLEND_K), 4)
                parent_med   = statistics.median(pool)
                parent_mad   = _mad(pool)
                group_med    = statistics.median(pm2_vals)
                group_mad    = _mad(pm2_vals)
                blended_median_pm2 = round(w * group_med + (1 - w) * parent_med, 2)
                blended_mad_pm2    = round(w * group_mad + (1 - w) * parent_mad, 2)
                blend_weight       = w
                blended_available  = (_robust_spread(blended_median_pm2,
                                                     blended_mad_pm2) is not None)

        if t1_sufficient and t2_sufficient:
            has_sufficient    = True
            sufficient_track  = 'both'
            insuff_reason     = None
        elif t1_sufficient:
            has_sufficient    = True
            sufficient_track  = 'track1'
            insuff_reason     = None
        elif t2_sufficient:
            has_sufficient    = True
            sufficient_track  = 'track2'
            insuff_reason     = None
        elif blended_available:
            # v4.2: blended-only group — scoreable with guardrails
            has_sufficient    = True
            sufficient_track  = 'track1_blended'
            insuff_reason     = None
        else:
            has_sufficient   = False
            sufficient_track = 'none'
            if total_count < SUFFICIENCY_MIN_N:
                insuff_reason = 'too_few_listings'
            elif with_size == 0 and n_price >= SUFFICIENCY_MIN_N:
                insuff_reason = 'no_size_data'
            elif ((std_pm2 == 0 or std_pm2 is None)
                  and (std_price == 0 or std_price is None)):
                insuff_reason = 'insufficient_variance'
            else:
                insuff_reason = 'too_few_listings'

        key_str = f'{suburb}:{listing_type}:{bedrooms}:{property_type}'
        baselines[key_str] = {
            'suburb_normalised':      suburb,
            'listing_type':           listing_type,
            'bedrooms':               bedrooms,
            'property_type':          property_type,
            'total_listings':         total_count,
            'listings_with_size':     with_size,
            'listings_without_size':  without_sz,
            'sample_size_pm2':        n_pm2,
            'median_price_per_m2':    median_pm2,
            'std_dev_price_per_m2':   std_pm2,
            'mad_price_per_m2':       mad_pm2,
            'p25_price_per_m2':       p25_pm2,
            'p75_price_per_m2':       p75_pm2,
            'cv_price_per_m2':        cv,
            'market_consistency':     consistency,
            'sample_size_price':      n_price,
            'median_price':           median_price,
            'std_dev_price':          std_price,
            'mad_price':              mad_price,
            'p25_price':              p25_price,
            'p75_price':              p75_price,
            'has_sufficient_data':    has_sufficient,
            'sufficient_track':       sufficient_track,
            'insufficiency_reason':   insuff_reason,
            # v4.2 blended baseline fields
            'pm2_blended_available':        blended_available,
            'blended_median_price_per_m2':  blended_median_pm2,
            'blended_mad_price_per_m2':     blended_mad_pm2,
            'pm2_parent_sample_size':       parent_n,
            'pm2_blend_weight':             blend_weight,
        }

        row: dict[str, Any] = {**baselines[key_str], 'computed_at': _now()}
        try:
            client.table('suburb_baselines').upsert(
                row,
                on_conflict='suburb_normalised,listing_type,bedrooms,property_type',
            ).execute()
            inserted += 1
        except Exception as e:
            logger.warning('Baseline insert failed (%s): %s', key_str, e)

    n_sufficient = sum(1 for b in baselines.values() if b['has_sufficient_data'])
    logger.info(
        '  %d baseline groups stored (%d sufficient, %d insufficient)',
        inserted, n_sufficient, inserted - n_sufficient,
    )
    return baselines


# ── Step 3 helper — Attractiveness lookup ────────────────────────────────────

def _get_attractiveness_map(client) -> dict[str, int]:
    """
    Fetch suburb_attractiveness table.
    Returns {suburb_normalised: attractiveness_score}.
    Returns empty dict (default 50) if table doesn't exist.
    """
    try:
        rows = _paginate(lambda off, lim: (
            client.table('suburb_attractiveness')
            .select('suburb_normalised,attractiveness_score')
            .range(off, off + lim - 1)
            .execute()
        ))
        return {
            r['suburb_normalised']: int(r['attractiveness_score'])
            for r in rows
            if r.get('suburb_normalised') and r.get('attractiveness_score') is not None
        }
    except Exception as e:
        if _is_schema_error(e):
            logger.info('  suburb_attractiveness table not found — using default score 50')
            return {}
        raise


# ── Step 3 — Compute listing signals ─────────────────────────────────────────

def compute_listing_signals(
    client,
    baselines: dict[str, dict],
    attract_map: dict[str, int],
) -> dict[str, Any]:
    """
    For every active non-luxury non-outlier priced listing:
      1. Look up baseline (exact group match; bedrooms=None fallback)
      2. Require has_sufficient_data = TRUE
      3. Require sufficient_track covers the listing's data track
      4. Compute z_score, gap_pct, gap_rands
      5. Score: raw → ceiling → final_price_score → homelens_score
      6. Verdict from z_score
      7. Upsert to listing_signals

    Returns summary stats dict.
    """
    logger.info('[3/4] Computing listing signals...')

    def _fetch_listings(off, lim):
        try:
            return (
                client.table('listings')
                .select('listing_id,suburb_normalised,listing_type,bedrooms,'
                        'property_type,price,size_m2,price_per_m2,title')
                .eq('is_active', True)
                .eq('city', 'cape-town')  # baselines are per-suburb; suburb names repeat across cities
                .eq('is_luxury', False)
                .eq('is_outlier', False)
                .filter('price', 'not.is', 'null')
                .range(off, off + lim - 1)
                .execute()
            )
        except Exception as e:
            if _is_schema_error(e):
                return (
                    client.table('listings')
                    .select('listing_id,suburb_normalised,listing_type,bedrooms,'
                            'property_type,price,size_m2,price_per_m2,title')
                    .eq('is_active', True)
                    .eq('city', 'cape-town')  # baselines are per-suburb; suburb names repeat across cities
                    .filter('price', 'not.is', 'null')
                    .range(off, off + lim - 1)
                    .execute()
                )
            raise

    rows = _paginate(_fetch_listings)
    logger.info('  %d active priced listings to score', len(rows))

    # Clear existing signals
    try:
        client.table('listing_signals') \
              .delete().neq('listing_id', '____never____').execute()
        logger.info('  listing_signals cleared')
    except Exception as e:
        logger.warning('  Could not clear listing_signals: %s', e)

    now = _now()

    n_track1   = 0
    n_track2   = 0
    n_blended  = 0   # v4.2
    n_no_base  = 0
    n_insuff   = 0
    n_no_track = 0
    verdict_counts: dict[str, int] = defaultdict(int)
    top5: list[tuple[int, float, dict]] = []
    signal_rows: list[dict] = []

    for listing in rows:
        try:
            suburb        = listing.get('suburb_normalised') or 'unknown'
            listing_type  = listing.get('listing_type') or 'sale'
            bedrooms      = listing.get('bedrooms')
            property_type = listing.get('property_type') or 'other'
            price         = listing.get('price')
            size_m2       = listing.get('size_m2')
            pm2           = listing.get('price_per_m2')

            if not price:
                continue

            # ── Baseline lookup ───────────────────────────────────────────
            key_str  = f'{suburb}:{listing_type}:{bedrooms}:{property_type}'
            baseline = baselines.get(key_str)

            if baseline is None:
                fb_key   = f'{suburb}:{listing_type}:None:{property_type}'
                baseline = baselines.get(fb_key)

            if baseline is None:
                n_no_base += 1
                continue

            # ── Sufficiency gate ──────────────────────────────────────────
            if not baseline.get('has_sufficient_data'):
                n_insuff += 1
                continue

            sufficient_track = baseline.get('sufficient_track', 'none')

            # ── Determine listing track ───────────────────────────────────
            pm2_lo = _PM2_BOUNDS.get(listing_type, _PM2_BOUNDS['sale'])[0]
            listing_has_pm2 = (
                size_m2 is not None and size_m2 > SIZE_MIN
                and pm2 is not None and pm2 > pm2_lo
            )

            # v4.4: RENTALS are scored on raw monthly rand only, never per-m².
            # Rental size data is sparse (~12% coverage) and often unreliable
            # (plot vs floor, missing values), so mixing per-m² and raw-rand
            # within one suburb group produced nonsensical rankings — e.g. a
            # R15,000 flat out-scoring a R13,000 flat in the same building
            # because the pricier one happened to have a (large) size on file.
            # Forcing every rental down Track 2 makes all rentals in a group
            # comparable on the same ruler. Sales keep per-m² (77% coverage,
            # reliable sizes).
            force_raw_rand = (listing_type == 'rent')

            # Track 1 available? (v4.1: gated on robust spread, not std > 0)
            can_use_track1 = (
                not force_raw_rand
                and listing_has_pm2
                and sufficient_track in ('track1', 'both')
                and _robust_spread(baseline.get('median_price_per_m2'),
                                   baseline.get('mad_price_per_m2')) is not None
            )
            # Track 2 available?
            can_use_track2 = (
                sufficient_track in ('track2', 'both')
                and _robust_spread(baseline.get('median_price'),
                                   baseline.get('mad_price')) is not None
            )
            # v4.2 — blended Track 1 available? (lowest priority: a direct
            # baseline on either track always wins over a blended one)
            can_use_blended = (
                not force_raw_rand
                and listing_has_pm2
                and baseline.get('pm2_blended_available')
                and _robust_spread(baseline.get('blended_median_price_per_m2'),
                                   baseline.get('blended_mad_price_per_m2')) is not None
            )

            baseline_method    = 'direct'
            parent_sample_size = None
            blend_weight       = None

            if can_use_track1:
                comparison_method  = 'price_per_m2'
                med                = baseline['median_price_per_m2']
                mad                = baseline.get('mad_price_per_m2')
                z_score            = _robust_z(pm2, med, mad)   # v4.1 robust, capped +/-3
                gap_pct            = round((pm2 - med) / med * 100, 1)
                gap_rands          = int(price - round(med * size_m2))
                gap_annual         = gap_rands * 12
                sample_size        = baseline['sample_size_pm2']
                market_consistency = baseline.get('market_consistency')
            elif can_use_track2:
                comparison_method  = 'price_only'
                med                = baseline['median_price']
                mad                = baseline.get('mad_price')
                z_score            = _robust_z(price, med, mad)  # v4.1 robust, capped +/-3
                gap_pct            = round((price - med) / med * 100, 1)
                gap_rands          = int(price - med)
                gap_annual         = gap_rands * 12
                sample_size        = baseline['sample_size_price']
                market_consistency = baseline.get('market_consistency')
            elif can_use_blended:
                # v4.2 — blended within-suburb baseline, guarded
                comparison_method  = 'price_per_m2'
                baseline_method    = 'blended'
                med                = baseline['blended_median_price_per_m2']
                mad                = baseline.get('blended_mad_price_per_m2')
                z_score            = _robust_z(pm2, med, mad)
                gap_pct            = round((pm2 - med) / med * 100, 1)
                gap_rands          = int(price - round(med * size_m2))
                gap_annual         = gap_rands * 12
                sample_size        = baseline['sample_size_pm2']
                parent_sample_size = baseline.get('pm2_parent_sample_size')
                blend_weight       = baseline.get('pm2_blend_weight')
                market_consistency = None   # consistency is a direct-baseline stat
            else:
                n_no_track += 1
                continue

            if z_score is None:          # no robust spread derivable
                n_no_track += 1
                continue

            # ── Scoring ───────────────────────────────────────────────────
            if baseline_method == 'blended':
                # v4.2.1 guardrails: compressed score band + fixed low badge.
                # Compression (not clipping) keeps deal > fair > above ordering
                # among blended listings while the 50 ceiling still holds.
                confidence        = BLENDED_CONFIDENCE
                raw_score         = _raw_score_from_z(z_score)
                ceiling           = BLENDED_SCORE_CEILING
                final_price_score = _blended_final_score(raw_score)
            else:
                ceiling, confidence = _ceiling_and_confidence(sample_size)
                if ceiling is None:
                    continue
                raw_score         = _raw_score_from_z(z_score)
                final_price_score = min(raw_score, ceiling)

            attract_score     = attract_map.get(suburb, 50)
            homelens_score    = round(final_price_score * 0.65 + attract_score * 0.35)
            if sample_size < 7:
                homelens_score = min(homelens_score, 70)
            if sample_size < 5:
                homelens_score = min(homelens_score, 55)

            # ── Verdict ───────────────────────────────────────────────────
            verdict, verdict_label = _verdict_from_z(z_score)
            if baseline_method == 'blended':
                # v4.2 guardrail: no extreme verdicts from blended baselines;
                # labels always hedged
                verdict, verdict_label = _cap_blended_verdict(verdict, verdict_label)
            elif confidence == 'low':
                # v4.2.1: direct baselines with only 5-6 listings keep their
                # verdict slug but display a hedged label
                verdict_label = _HEDGED_LABELS.get(verdict, verdict_label)
            verdict_counts[verdict] += 1

            # ── Deal badge (v4.3) — independent of score, driven by gap % ──
            deal_badge = _deal_badge(gap_pct)
            sig_row: dict[str, Any] = {
                'listing_id':            listing['listing_id'],
                'suburb_normalised':     suburb,
                'listing_type':          listing_type,
                'bedrooms':              bedrooms,
                'property_type':         property_type,
                'comparison_method':     comparison_method,
                'listing_price_per_m2':  pm2,
                'median_price_per_m2':   baseline.get('median_price_per_m2'),
                'std_dev_price_per_m2':  baseline.get('std_dev_price_per_m2'),
                'mad_price_per_m2':      baseline.get('mad_price_per_m2'),
                'listing_price':         int(price),
                'median_price':          baseline.get('median_price'),
                'std_dev_price':         baseline.get('std_dev_price'),
                'mad_price':             baseline.get('mad_price'),
                'z_score':               z_score,
                'gap_pct':               gap_pct,
                'gap_rands':             gap_rands,
                'gap_annual':            gap_annual,
                'raw_score':             raw_score,
                'sample_size_ceiling':   ceiling,
                'attractiveness_score':  attract_score,
                'final_price_score':     final_price_score,
                'homelens_score':        homelens_score,
                'verdict':               verdict,
                'verdict_label':         verdict_label,
                'deal_badge':            deal_badge,
                'confidence':            confidence,
                'sample_size':           sample_size,
                'market_consistency':    market_consistency,
                'is_luxury':             False,
                'is_outlier':            False,
                'baseline_method':       baseline_method,
                'parent_sample_size':    parent_sample_size,
                'blend_weight':          blend_weight,
                'computed_at':           now,
            }
            signal_rows.append(sig_row)

            if baseline_method == 'blended':
                n_blended += 1
            elif comparison_method == 'price_per_m2':
                n_track1 += 1
            else:
                n_track2 += 1

            if len(top5) < 5 or homelens_score > top5[-1][0]:
                top5.append((homelens_score, z_score, {
                    'title':   listing.get('title', '')[:60],
                    'suburb':  suburb,
                    'score':   homelens_score,
                    'z_score': z_score,
                    'gap_pct': gap_pct,
                }))
                top5.sort(key=lambda x: (-x[0], x[1]))
                top5 = top5[:5]

        except Exception as e:
            logger.warning('Signal error (%s): %s', listing.get('listing_id'), e)

    # ── Batch upsert ──────────────────────────────────────────────────────────
    upserted   = 0
    batch_size = 100
    for i in range(0, len(signal_rows), batch_size):
        batch = signal_rows[i : i + batch_size]
        try:
            client.table('listing_signals').upsert(
                batch, on_conflict='listing_id',
            ).execute()
            upserted += len(batch)
        except Exception as e:
            logger.warning('Batch upsert failed (rows %d-%d): %s', i, i + len(batch), e)
            for row in batch:
                try:
                    client.table('listing_signals').upsert(
                        row, on_conflict='listing_id',
                    ).execute()
                    upserted += 1
                except Exception as e2:
                    logger.warning('Single upsert failed (%s): %s',
                                   row.get('listing_id'), e2)

    logger.info('  %d signals upserted', upserted)
    if n_blended:
        logger.info('  %d of these use v4.2 blended baselines '
                    '(capped at score %d, confidence=%s)',
                    n_blended, BLENDED_SCORE_CEILING, BLENDED_CONFIDENCE)

    return {
        'total':          upserted,
        'track1':         n_track1,
        'track2':         n_track2,
        'blended':        n_blended,   # v4.2
        'no_baseline':    n_no_base,
        'insufficient':   n_insuff,
        'no_track':       n_no_track,
        'verdict_counts': dict(verdict_counts),
        'top5':           [x[2] for x in top5],
    }


# ── Print helpers ─────────────────────────────────────────────────────────────

def _print_sufficiency_report(baselines: dict[str, dict]) -> None:
    sufficient   = {k: v for k, v in baselines.items() if v.get('has_sufficient_data')}
    insufficient = {k: v for k, v in baselines.items() if not v.get('has_sufficient_data')}

    print('\n' + '=' * 65)
    print('  SUBURB BASELINE SUFFICIENCY REPORT')
    print('=' * 65)

    print(f'\nSUFFICIENT (has_sufficient_data = TRUE)  [{len(sufficient)} groups]')
    hdr = (f'  {"suburb":<26} {"type":<5} {"beds":<5} {"prop":<11} '
           f'{"track":<8} {"pm2_n":<7} {"price_n":<8} median_pm2')
    print(hdr)
    print('  ' + '-' * 80)
    for _, b in sorted(sufficient.items(),
                       key=lambda x: (-x[1]['total_listings'], x[1]['suburb_normalised'])):
        suburb    = (b['suburb_normalised'] or '')[:25]
        ltype     = b['listing_type'][:4]
        beds      = str(b['bedrooms'])
        ptype     = (b['property_type'] or '')[:10]
        track     = b['sufficient_track']
        pm2_n     = b['sample_size_pm2']
        price_n   = b['sample_size_price']
        median    = b.get('median_price_per_m2')
        med_str   = f'R{median:,.0f}' if median else 'n/a'
        print(f'  {suburb:<26} {ltype:<5} {beds:<5} {ptype:<11} '
              f'{track:<8} {pm2_n:<7} {price_n:<8} {med_str}')

    print(f'\nINSUFFICIENT (has_sufficient_data = FALSE)  [{len(insufficient)} groups]')
    hdr2 = (f'  {"suburb":<26} {"type":<5} {"beds":<5} {"prop":<11} '
            f'{"reason":<26} {"total"}')
    print(hdr2)
    print('  ' + '-' * 80)
    for _, b in sorted(insufficient.items(),
                       key=lambda x: (-x[1]['total_listings'], x[1]['suburb_normalised'])):
        suburb = (b['suburb_normalised'] or '')[:25]
        ltype  = b['listing_type'][:4]
        beds   = str(b['bedrooms'])
        ptype  = (b['property_type'] or '')[:10]
        reason = b.get('insufficiency_reason') or 'unknown'
        total  = b.get('total_listings', 0)
        print(f'  {suburb:<26} {ltype:<5} {beds:<5} {ptype:<11} '
              f'{reason:<26} {total}')

    total_groups = len(baselines)
    suff_count   = len(sufficient)
    insuff_count = len(insufficient)
    suff_pct     = round(100 * suff_count / total_groups) if total_groups else 0

    print(f'\nSUMMARY:')
    print(f'  Total groups found  : {total_groups:,}')
    print(f'  Sufficient groups   : {suff_count:,} ({suff_pct}%)')
    print(f'  Insufficient groups : {insuff_count:,} ({100 - suff_pct}%)')

    suburb_suff: dict[str, int] = defaultdict(int)
    for b in sufficient.values():
        suburb_suff[b['suburb_normalised']] += 1

    if suburb_suff:
        print(f'\n  Sufficient by suburb:')
        for suburb, cnt in sorted(suburb_suff.items(), key=lambda x: -x[1])[:25]:
            print(f'    {suburb}: {cnt} sufficient group{"s" if cnt != 1 else ""}')

    print('=' * 65)


def _print_signal_summary(stats: dict[str, Any]) -> None:
    vc   = stats.get('verdict_counts', {})
    top5 = stats.get('top5', [])

    print('\n=== SIGNAL SUMMARY ===')
    print(f"  Total listings scored      : {stats['total']:,}")
    print(f"  Track 1 (price/m²)         : {stats['track1']:,}")
    print(f"  Track 2 (price only)       : {stats['track2']:,}")
    print(f"  Skipped (no baseline)      : {stats['no_baseline']:,}")
    print(f"  Skipped (insufficient)     : {stats['insufficient']:,}")
    print(f"  Skipped (track mismatch)   : {stats['no_track']:,}")

    print('\n  Score distribution:')
    order  = ['strong_deal', 'deal', 'fair', 'above', 'overpriced']
    labels = {
        'strong_deal': 'strong_deal  (z ≤ -1.5)',
        'deal':        'deal         (z ≤ -0.5)',
        'fair':        'fair                    ',
        'above':       'above        (z ≤  1.5)',
        'overpriced':  'overpriced   (z >  1.5)',
    }
    for v in order:
        cnt = vc.get(v, 0)
        print(f"    {labels[v]}: {cnt:,} listings")

    if top5:
        print('\n  Top 5 best-value listings:')
        for t in top5:
            print(f"    [{t['suburb']}] {t['title']}")
            print(f"      score={t['score']}  z={t['z_score']:+.2f}  "
                  f"gap={t['gap_pct']:+.1f}%")


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_engine() -> tuple[dict, dict[str, dict], dict[str, Any]]:
    """
    Full pipeline:
      Step 1 — populate property_type
      Step 2 — compute suburb baselines (with sufficiency gate)
      Step 3 — suburb attractiveness lookup
      Step 4 — compute listing signals (sufficient baselines only)

    Prints sufficiency report + signal summary.
    Returns (property_type_counts, baselines, signal_stats).
    """
    logger.info('=== Homelens Insight Engine v4 starting ===')

    client = _supabase()

    if not check_schema(client):
        sys.exit(1)

    # Step 1
    pt_counts = populate_property_types(client)

    # Step 2
    baselines = compute_suburb_baselines(client)

    # Step 3
    logger.info('[3/4] Loading suburb attractiveness scores...')
    attract_map = _get_attractiveness_map(client)
    logger.info('  %d suburbs with attractiveness scores', len(attract_map))

    # Step 4
    signal_stats = compute_listing_signals(client, baselines, attract_map)

    # Reports
    _print_sufficiency_report(baselines)
    _print_signal_summary(signal_stats)

    logger.info('=== Engine v4 complete ===')
    return pt_counts, baselines, signal_stats
