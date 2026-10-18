"""
Homelens — Investment Analysis Computation.

For every active non-luxury sale listing, computes:
  - Estimated rental income (suburb baselines → estimated from yield)
  - Suburb-specific capital growth rate (tier-based, research-backed)
  - Suburb-specific vacancy rate
  - Gross and net yield
  - Itemised monthly costs (rates, levies, insurance, maintenance)
  - Monthly cashflow before bond
  - 5-year projections with annualised return
  - Transfer/acquisition costs (SARS 2024 transfer duty)
  - Investment score (yield 40% + price score 35% + attractiveness 25%)

Results are upserted into listing_investment.

Usage:
    python scripts/compute_investment.py

Prerequisites (run ONCE in Supabase SQL editor if table is missing):
  -- Migration SQL is printed automatically if the table is missing.
"""
from __future__ import annotations

import logging
import math
import os
import re
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(override=True)

from scraper.database import get_client

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('homelens.investment')

# ---------------------------------------------------------------------------
# Migration SQL
# ---------------------------------------------------------------------------

MIGRATION_SQL = """\
-- Run this ONCE in the Supabase SQL editor if listing_investment doesn't exist.

CREATE TABLE IF NOT EXISTS listing_investment (
  listing_id                   TEXT PRIMARY KEY,

  -- Rental income
  estimated_monthly_rent       INTEGER,
  rental_sample_size           INTEGER,
  rental_confidence            TEXT,
  rental_source                TEXT,        -- 'comparable_rentals' | 'rental_model' | 'no_rental_data' | null

  -- Rental cashflow components
  gross_rent                   INTEGER,
  vacancy_deduction            INTEGER,
  management_deduction         INTEGER,
  net_rental_income            INTEGER,

  -- Yield
  gross_yield_pct              NUMERIC,
  net_yield_pct                NUMERIC,
  yield_verdict                TEXT,
  yield_label                  TEXT,

  -- Monthly cost estimates
  est_monthly_rates            INTEGER,
  est_monthly_levies_min       INTEGER,
  est_monthly_levies_max       INTEGER,
  est_monthly_insurance        INTEGER,
  est_monthly_maintenance      INTEGER,
  monthly_rates                INTEGER,
  monthly_levies_min           INTEGER,
  monthly_levies_max           INTEGER,
  monthly_insurance            INTEGER,
  monthly_maintenance          INTEGER,
  monthly_costs_excl_bond      INTEGER,

  -- Cashflow
  monthly_cashflow_before_bond INTEGER,

  -- Growth + vacancy assumptions
  growth_rate_pct              NUMERIC,
  growth_rate_source           TEXT,
  vacancy_rate_pct             NUMERIC,

  -- Scores
  investment_score             INTEGER,
  market_momentum              TEXT,

  -- 5-year projections
  year_1_value                 INTEGER,
  year_2_value                 INTEGER,
  year_3_value                 INTEGER,
  year_4_value                 INTEGER,
  year_5_value                 INTEGER,
  capital_gain_5yr             INTEGER,
  rental_income_5yr            INTEGER,
  cumulative_net_rent_5yr      INTEGER,
  total_5yr_return             INTEGER,
  annualised_return_pct        NUMERIC,
  growth_assumption_pct        NUMERIC,

  -- Transfer costs
  transfer_duty                INTEGER,
  transfer_costs               INTEGER,
  bond_registration            INTEGER,
  total_acquisition_cost       INTEGER,

  computed_at                  TIMESTAMPTZ DEFAULT NOW()
);

-- If the table already exists, add any missing columns:
ALTER TABLE listing_investment
  ADD COLUMN IF NOT EXISTS rental_source                TEXT,
  ADD COLUMN IF NOT EXISTS gross_rent                   INTEGER,
  ADD COLUMN IF NOT EXISTS vacancy_deduction            INTEGER,
  ADD COLUMN IF NOT EXISTS management_deduction         INTEGER,
  ADD COLUMN IF NOT EXISTS net_rental_income            INTEGER,
  ADD COLUMN IF NOT EXISTS monthly_rates                INTEGER,
  ADD COLUMN IF NOT EXISTS monthly_levies_min           INTEGER,
  ADD COLUMN IF NOT EXISTS monthly_levies_max           INTEGER,
  ADD COLUMN IF NOT EXISTS monthly_insurance            INTEGER,
  ADD COLUMN IF NOT EXISTS monthly_maintenance          INTEGER,
  ADD COLUMN IF NOT EXISTS monthly_costs_excl_bond      INTEGER,
  ADD COLUMN IF NOT EXISTS growth_rate_pct              NUMERIC,
  ADD COLUMN IF NOT EXISTS growth_rate_source           TEXT,
  ADD COLUMN IF NOT EXISTS vacancy_rate_pct             NUMERIC,
  ADD COLUMN IF NOT EXISTS year_2_value                 INTEGER,
  ADD COLUMN IF NOT EXISTS year_4_value                 INTEGER,
  ADD COLUMN IF NOT EXISTS cumulative_net_rent_5yr      INTEGER,
  ADD COLUMN IF NOT EXISTS annualised_return_pct        NUMERIC,
  ADD COLUMN IF NOT EXISTS monthly_bond                 INTEGER,
  ADD COLUMN IF NOT EXISTS cashflow_after_bond          INTEGER,
  ADD COLUMN IF NOT EXISTS cashflow_score               INTEGER;
"""

# ---------------------------------------------------------------------------
# Suburb tier lookup sets
# ---------------------------------------------------------------------------

_ATLANTIC_SEABOARD: frozenset[str] = frozenset({
    'clifton', 'bantry-bay', 'camps-bay',
    'fresnaye', 'bakoven', 'llandudno',
    'mouille-point', 'three-anchor-bay',
})

# ── v5 financial constants ───────────────────────────────────────────────────
PRIME_RATE            = 0.1025    # SA prime lending rate, Jan 2026. UPDATE on repo moves.
CT_RATE_IN_RAND       = 0.007159  # CoCT residential rates tariff 2025/26 (annual)
MUNICIPAL_VALUE_RATIO = 0.80      # municipal valuation proxy vs asking price

# ── v5 rent model constants ──────────────────────────────────────────────────
# Multiplicative hedonic model: log(rent) = suburb_level + bed_eff + type_eff
# + size_adj. Fitted by robust median polish on the full rental corpus
# (active + inactive listings). Holdout-validated on group medians:
# 65% within ±20%, median error 14%; naive suburb-median benchmark: 22%.
RENT_MODEL_GAMMA      = 0.5              # rent ~ size^0.5 within bedroom class
RENT_MODEL_MIN_RENT   = 3_000            # corpus filter: kill per-night junk
RENT_MODEL_MAX_RENT   = 350_000
RENT_MODEL_BEDS       = ('0', '1', '2', '3', '4')  # 5+ = flatshare pollution
# 80%-coverage ranges, empirically calibrated by cross-validation
# (82% of true market medians landed inside the medium range on holdout):
RENT_RANGE_MEDIUM     = (0.74, 1.36)     # suburb has >= RENT_MODEL_GOOD_N rentals
RENT_RANGE_LOW        = (0.65, 1.55)     # suburb has 1..RENT_MODEL_GOOD_N-1
RENT_MODEL_GOOD_N     = 5
RENT_MAX_IMPLIED_YIELD = 15.0            # loose guard vs price/data errors (%)

# Run ONCE in Supabase SQL editor before first v5 run.
INVESTMENT_MIGRATION_SQL = """\
ALTER TABLE listing_investment ADD COLUMN IF NOT EXISTS rent_range_low  INTEGER;
ALTER TABLE listing_investment ADD COLUMN IF NOT EXISTS rent_range_high INTEGER;
"""

# v6 — run ONCE in Supabase SQL editor. is_reference marks dead-but-priced
# rentals kept as historical evidence for the rent model. They are NEVER shown
# to users (browse/verdict/recommendations use is_active only); fix_suburbs.py
# populates this flag and fetch_rental_corpus() is the only reader.
REFERENCE_MIGRATION_SQL = """\
ALTER TABLE listings ADD COLUMN IF NOT EXISTS is_reference BOOLEAN DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_listings_reference
    ON listings (listing_type, is_reference) WHERE is_reference = TRUE;
"""

# ── v7: short-term (Airbnb) potential ────────────────────────────────────────
# AirDNA gives monthly gross STR revenue per suburb+bed. We show BOTH long-term
# and short-term potential; the Investment Score stays long-term (consistent
# across all suburbs). When netted STR income beats netted long-term by a clear
# margin, we set str_better_value = TRUE for the design layer to surface.
#
# STR net model — costs are HIGHER than long-term (more work, more turnover).
# AirDNA revenue is ACTUAL earned revenue (already occupancy-adjusted), so we
# do NOT deduct vacancy on the STR side — that would double-count.
STR_MGMT_RATE        = 0.20     # short-let management (vs 9% long-term)
STR_CLEANING_PER_MO  = 2_000    # ~5 turnovers × ~R400 cleaning
STR_UTILITIES_PER_MO = 2_500    # wifi, electricity, water, consumables (host pays)
STR_BETTER_MARGIN    = 0.20     # flag fires only when STR net >= LT net × 1.20
STR_MIN_MONTHS       = 6        # need >= this many months for a usable average
STR_AVG_WINDOW       = 12       # average over the most recent N months (one cycle)

REFERENCE_MIGRATION_SQL_v7 = REFERENCE_MIGRATION_SQL  # alias kept for clarity

STR_MIGRATION_SQL = """\
ALTER TABLE listing_investment ADD COLUMN IF NOT EXISTS str_monthly_gross   INTEGER;
ALTER TABLE listing_investment ADD COLUMN IF NOT EXISTS str_monthly_net     INTEGER;
ALTER TABLE listing_investment ADD COLUMN IF NOT EXISTS str_vs_lt_pct       NUMERIC;
ALTER TABLE listing_investment ADD COLUMN IF NOT EXISTS str_better_value    BOOLEAN DEFAULT FALSE;
ALTER TABLE listing_investment ADD COLUMN IF NOT EXISTS str_data_available  BOOLEAN DEFAULT FALSE;
ALTER TABLE listing_investment ADD COLUMN IF NOT EXISTS str_confidence      TEXT;
"""

_CITY_BOWL_GENTRIFYING: frozenset[str] = frozenset({
    'woodstock', 'observatory', 'salt-river',
    'de-waterkant', 'bo-kaap', 'zonnebloem',
    'foreshore', 'cape-town-city-centre',
    'gardens', 'tamboerskloof', 'vredehoek',
    'oranjezicht', 'higgovale',
})

_ATLANTIC_ESTABLISHED: frozenset[str] = frozenset({
    'sea-point', 'green-point',
    'waterfront', 'three-anchor-bay',
})

_SOUTHERN_SUBURBS: frozenset[str] = frozenset({
    'claremont', 'newlands', 'rondebosch',
    'kenilworth', 'constantia', 'tokai',
    'plumstead', 'diep-river', 'wynberg',
    'bishopscourt', 'mowbray', 'pinelands', 'rosebank',
})

_BATCH_SIZE = 100


# ---------------------------------------------------------------------------
# Suburb-specific rate lookups
# ---------------------------------------------------------------------------

def get_growth_rate(suburb: str | None) -> tuple[float, str]:
    """
    Return (growth_rate, source_label) for a suburb slug.

    Tiers (priority order):
      Atlantic Seaboard premium:  9.0%
      City Bowl / gentrifying:    9.0%
      Atlantic Seaboard estab.:   8.5%
      Southern Suburbs:           7.0%
      Default Cape Town:          7.5%
    """
    s = (suburb or '').lower().strip()
    if s in _ATLANTIC_SEABOARD:
        return 0.09, 'Atlantic Seaboard historical avg'
    if s in _CITY_BOWL_GENTRIFYING:
        return 0.09, 'City Bowl/gentrifying avg'
    if s in _ATLANTIC_ESTABLISHED:
        return 0.085, 'Established Atlantic Seaboard'
    if s in _SOUTHERN_SUBURBS:
        return 0.07, 'Southern Suburbs historical avg'
    return 0.075, 'Cape Town general average'


def get_vacancy_rate(suburb: str | None) -> float:
    """Return decimal vacancy rate for a suburb."""
    s = (suburb or '').lower().strip()
    if s in _ATLANTIC_SEABOARD or s in _ATLANTIC_ESTABLISHED:
        return 0.05   # 5% — very low vacancy, high demand
    if s in _CITY_BOWL_GENTRIFYING:
        return 0.07   # 7%
    if s in _SOUTHERN_SUBURBS:
        return 0.08   # 8%
    return 0.083      # 8.3% default (≈ 1 month per year)


def _get_yield_estimate_rate(suburb: str | None) -> float:
    """
    Estimated gross yield rate for fallback rental estimation.
    Used when no suburb_baselines data exists.
    """
    s = (suburb or '').lower().strip()
    if s in _ATLANTIC_SEABOARD or s in _ATLANTIC_ESTABLISHED:
        return 0.045   # 4.5% — premium areas, lower yields
    if s in _CITY_BOWL_GENTRIFYING:
        return 0.065   # 6.5%
    if s in _SOUTHERN_SUBURBS:
        return 0.055   # 5.5%
    return 0.055       # 5.5% default


# ---------------------------------------------------------------------------
# Financial helpers
# ---------------------------------------------------------------------------

def calc_transfer_duty(price: int) -> int:
    """SARS transfer duty 2024 — exact bracket computation."""
    if price <= 1_100_000:
        return 0
    elif price <= 1_512_500:
        return round((price - 1_100_000) * 0.03)
    elif price <= 2_117_500:
        return round(12_375 + (price - 1_512_500) * 0.06)
    elif price <= 2_722_500:
        return round(48_675 + (price - 2_117_500) * 0.08)
    elif price <= 12_100_000:
        return round(97_075 + (price - 2_722_500) * 0.11)
    else:
        return round(1_128_600 + (price - 12_100_000) * 0.13)


def calc_bond_registration(price: int) -> int:
    if price < 1_000_000:
        return 15_000
    elif price < 2_000_000:
        return 22_000
    elif price < 5_000_000:
        return 28_000
    else:
        return 35_000


def calc_monthly_costs(price: int, property_type: str) -> dict:
    """
    Estimate monthly holding costs.

    Rates:   City of Cape Town residential rate-in-rand 2025/26 = 0.007159
             of municipal value PER YEAR. Municipal value proxied at 80% of
             asking price. No R435k primary-residence rebate — these are
             investment properties, which don't qualify.
             (v5 FIX: previous constant 0.00006892/month understated rates
             ~9x, e.g. R110/mo instead of ~R950/mo on a R2m property.)
    Levies:  Apartment and townhouse price bands per spec
             Freehold houses → 0
    Insurance: 0.12% of 120% replacement value p.a. / 12
    Maintenance: 1% of property value p.a. / 12
    """
    ptype = (property_type or '').lower().strip()

    # ── Rates & taxes ─────────────────────────────────────────────────────
    rates = round(price * MUNICIPAL_VALUE_RATIO * CT_RATE_IN_RAND / 12)

    # ── Levies by property type and price band ────────────────────────────
    is_apartment = ptype in ('apartment', 'flat', 'studio', 'penthouse')
    is_townhouse = ptype in ('townhouse', 'duplex', 'cluster')

    if is_apartment:
        if price < 1_500_000:
            levies_min, levies_max = 600,   1_200
        elif price < 2_500_000:
            levies_min, levies_max = 1_000, 2_000
        elif price < 5_000_000:
            levies_min, levies_max = 1_800, 3_500
        else:
            levies_min, levies_max = 3_000, 8_000
    elif is_townhouse:
        if price < 2_000_000:
            levies_min, levies_max = 800,   1_500
        elif price < 4_000_000:
            levies_min, levies_max = 1_500, 2_500
        else:
            levies_min, levies_max = 2_500, 5_000
    else:
        # Freehold house — no sectional title levies
        levies_min, levies_max = 0, 0

    # ── Insurance: 0.12% of replacement value (120% of price) p.a. / 12 ──
    insurance = round(price * 1.2 * 0.0012 / 12)

    # ── Maintenance: 1% of property value p.a. / 12 ───────────────────────
    maintenance = round(price * 0.01 / 12)

    return {
        'rates':       rates,
        'levies_min':  levies_min,
        'levies_max':  levies_max,
        'insurance':   insurance,
        'maintenance': maintenance,
    }


def _rental_confidence(sample_size: int | None) -> str:
    if not sample_size:
        return 'unknown'
    if sample_size >= 20:
        return 'high'
    if sample_size >= 10:
        return 'medium'
    return 'low'


def _yield_verdict_label(gross_yield: float | None) -> tuple[str, str]:
    if gross_yield is None:
        return 'no_data', 'No Rental Data'
    if gross_yield >= 8:
        return 'strong', 'Strong Yield'
    if gross_yield >= 6:
        return 'good', 'Good Yield'
    if gross_yield >= 4:
        return 'average', 'Average Yield'
    if gross_yield >= 2:
        return 'low', 'Low Yield'
    return 'very_low', 'Very Low Yield'


def _yield_score(gross_yield: float | None) -> int:
    if gross_yield is None:
        return 50
    if gross_yield >= 8:
        return 100
    if gross_yield >= 6:
        return 80
    if gross_yield >= 4:
        return 55
    if gross_yield >= 2:
        return 30
    return 15


def _market_momentum(verdict: str | None) -> str:
    if verdict in ('strong_deal', 'deal'):
        return 'rising'
    if verdict == 'fair':
        return 'stable'
    if verdict in ('above', 'overpriced'):
        return 'softening'
    return 'unknown'


def calc_monthly_bond(price: int) -> int:
    """
    Standard 90% LTV bond repayment at PRIME_RATE over 20 years.
      deposit      = price × 10%
      loan         = price × 90%
      monthly_rate = PRIME_RATE / 12
      months       = 240
    (v5 FIX: was hardcoded 11.75% — the 2024 peak. Prime has been cut to
    ~10.25% as of Jan 2026; update PRIME_RATE when the repo rate moves.)
    """
    if not price:
        return 0
    loan         = price * 0.90
    monthly_rate = PRIME_RATE / 12
    months       = 240
    factor       = (1 + monthly_rate) ** months
    return round(loan * (monthly_rate * factor) / (factor - 1))


def _growth_score(growth_rate: float) -> int:
    """Map suburb annual growth rate to a 0–100 score."""
    pct = growth_rate * 100
    if pct >= 9.0:
        return 90
    if pct >= 8.5:
        return 75
    if pct >= 7.5:
        return 60
    return 45


def _cashflow_score_fn(cashflow_after_bond: int | None) -> int:
    """
    Map monthly cashflow after bond repayment to a 0–100 score.
    Positive cashflow rewarded; deeply negative heavily penalised.
    """
    if cashflow_after_bond is None:
        return 50
    cf = cashflow_after_bond
    if cf >= 5_000:
        return 100
    if cf >= 1_000:
        return 85
    if cf >= 0:
        return 70
    if cf >= -2_000:
        return 60
    if cf >= -5_000:
        return 45
    if cf >= -10_000:
        return 30
    return 15


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def compute_investment_row(
    listing: dict,
    rent_baseline: dict | None,
    signal: dict | None,
    attractiveness: int,
    rent_model: dict | None = None,
    str_stats: dict | None = None,
) -> dict:
    """Compute all investment fields for one listing. Returns a row dict for upsert."""
    price  = listing.get('price') or 0
    ptype  = (listing.get('property_type') or '').lower()
    suburb = listing.get('suburb_normalised')
    lid    = listing['listing_id']

    # ── Suburb-specific assumptions ────────────────────────────────────────
    growth_rate, growth_source = get_growth_rate(suburb)
    vacancy_rate               = get_vacancy_rate(suburb)

    # ── 1. Rental income — strict priority order ───────────────────────────
    # Hard reset every variable before every listing. Nothing carries over.
    estimated_monthly_rent = None
    rental_sample_size     = None
    rental_confidence_val  = None
    rental_source          = None
    rent_range_low         = None
    rent_range_high        = None

    # STEP 1: Real comparable rentals from suburb_baselines
    # (same suburb, bedrooms, property_type, has_sufficient_data=True)
    if rent_baseline:
        _rent = rent_baseline.get('median_price')
        _size = rent_baseline.get('sample_size_price')
        if _rent and price:
            # Sanity check: implied gross yield must be realistic.
            # Cap is suburb-tier-aware: 2× the expected yield rate for that
            # area. Atlantic Seaboard = 9% cap (4.5% × 2), gentrifying = 13%,
            # default = 11%. This rejects inflated suburb_baselines data (e.g.
            # a luxury penthouse skewing the Sea Point rental median to R62k)
            # and falls through to the rent model instead.
            _expected_yield = _get_yield_estimate_rate(suburb)
            _max_yield      = _expected_yield * 2 * 100   # e.g. 9.0 for AS
            _implied_yield  = (_rent * 12) / price * 100
            if _implied_yield <= _max_yield:
                estimated_monthly_rent = _rent
                rental_sample_size     = _size
                rental_source          = 'comparable_rentals'
                rental_confidence_val  = 'high' if (_size or 0) >= 10 else 'medium'
            # else: bad comparable — fall through to Step 2

    # STEP 2 (v5): hedonic rent model — suburb level × bedroom × type × size,
    # fitted from the full rental corpus. Replaces the old price×yield
    # fabrication, whose "gross yield" was the assumed rate echoed back.
    # Returns None for suburbs with zero rental evidence (Option B): the row
    # then carries rental_source='no_rental_data' and neutral yield/cashflow
    # scores instead of fabricated certainty.
    if not estimated_monthly_rent and price and rent_model:
        est = model_rent_estimate(
            rent_model, suburb, listing.get('bedrooms'),
            ptype, listing.get('size_m2'),
        )
        if est:
            _rent, _lo, _hi, _conf = est
            _implied_yield = (_rent * 12) / price * 100
            if _implied_yield <= RENT_MAX_IMPLIED_YIELD:
                estimated_monthly_rent = _rent
                rent_range_low         = _lo
                rent_range_high        = _hi
                rental_sample_size     = rent_model['sub_n'].get(suburb)
                rental_source          = 'rental_model'
                rental_confidence_val  = _conf
            # else: price or data error — honest null below

    # STEP 3 (v5): no evidence — say so. No fabrication.
    if not estimated_monthly_rent:
        rental_source = rental_source or 'no_rental_data'

    # ── 2. Rental cashflow breakdown ───────────────────────────────────────
    gross_rent           = estimated_monthly_rent
    vacancy_deduction    = None
    management_deduction = None
    net_rental_income    = None

    if gross_rent:
        vacancy_deduction    = round(gross_rent * vacancy_rate)
        effective_rent       = gross_rent - vacancy_deduction
        management_deduction = round(effective_rent * 0.09)
        net_rental_income    = effective_rent - management_deduction

    # ── 3. Monthly costs ───────────────────────────────────────────────────
    costs = calc_monthly_costs(price, ptype)
    monthly_costs_excl_bond = (
        costs['rates'] +
        costs['levies_min'] +
        costs['insurance'] +
        costs['maintenance']
    )

    # ── 4. Yields ──────────────────────────────────────────────────────────
    gross_yield_pct = None
    net_yield_pct   = None

    if gross_rent and price:
        gross_yield_pct = round((gross_rent * 12) / price * 100, 2)

    if net_rental_income is not None and price:
        net_annual = net_rental_income * 12 - monthly_costs_excl_bond * 12
        net_yield_pct = round(net_annual / price * 100, 2)

    yield_verdict, yield_label = _yield_verdict_label(gross_yield_pct)

    # ── 5. Cashflow before bond ────────────────────────────────────────────
    monthly_cashflow_before_bond = None
    if net_rental_income is not None:
        monthly_cashflow_before_bond = round(net_rental_income - monthly_costs_excl_bond)

    # ── 5b. Bond repayment + cashflow after bond ───────────────────────────
    monthly_bond        = calc_monthly_bond(price)
    cashflow_after_bond = None
    if monthly_cashflow_before_bond is not None:
        cashflow_after_bond = monthly_cashflow_before_bond - monthly_bond

    # ── 5c. Short-term (Airbnb) potential (v7) ─────────────────────────────
    # Shown alongside long-term. Compared NET-vs-NET: STR net income vs the
    # long-term net RENTAL income (before holding costs, which are identical
    # under either strategy and cancel out of the comparison). Does NOT affect
    # the investment score — that stays long-term and consistent everywhere.
    str_fields = {
        'str_monthly_gross':  None,
        'str_monthly_net':    None,
        'str_vs_lt_pct':      None,
        'str_better_value':   False,
        'str_data_available': False,
        'str_confidence':     None,
    }
    if str_stats:
        sp = str_potential(str_stats, suburb, listing.get('bedrooms'),
                           net_rental_income,
                           size_m2=listing.get('size_m2'),
                           property_type=listing.get('property_type'))
        if sp:
            str_fields.update(sp)

    # ── 6. Transfer costs ──────────────────────────────────────────────────
    transfer_duty  = calc_transfer_duty(price)
    transfer_costs = round(price * 0.012)
    bond_reg       = calc_bond_registration(price)
    total_acq_cost = price + transfer_duty + transfer_costs + bond_reg

    # ── 7. 5-year projections ──────────────────────────────────────────────
    g = growth_rate
    year_1_value     = round(price * (1 + g) ** 1)
    year_2_value     = round(price * (1 + g) ** 2)
    year_3_value     = round(price * (1 + g) ** 3)
    year_4_value     = round(price * (1 + g) ** 4)
    year_5_value     = round(price * (1 + g) ** 5)
    capital_gain_5yr = year_5_value - price

    cumulative_net_rent_5yr = 0
    if net_rental_income is not None:
        cumulative_net_rent_5yr = round(net_rental_income * 12 * 5)

    total_5yr_return = capital_gain_5yr + cumulative_net_rent_5yr

    annualised_return_pct = None
    if price > 0:
        annualised_return_pct = round(
            (((price + total_5yr_return) / price) ** (1 / 5) - 1) * 100, 2
        )

    # ── 8. Investment score ────────────────────────────────────────────────
    # Weights: price 20% | yield 30% | growth 20% | cashflow 30%
    y_score  = _yield_score(gross_yield_pct)
    p_score  = (signal.get('homelens_score') or 50) if signal else 50
    g_score  = _growth_score(growth_rate)
    cf_score = _cashflow_score_fn(cashflow_after_bond)
    invest_score = round(
        p_score  * 0.20 +
        y_score  * 0.30 +
        g_score  * 0.20 +
        cf_score * 0.30
    )

    # ── 9. Market momentum ─────────────────────────────────────────────────
    momentum = _market_momentum(signal.get('verdict') if signal else None)

    return {
        'listing_id':                   lid,

        # Rental income
        'estimated_monthly_rent':       estimated_monthly_rent,
        'rental_sample_size':           rental_sample_size,
        'rental_confidence':            rental_confidence_val,
        'rental_source':                rental_source,
        'rent_range_low':               rent_range_low,
        'rent_range_high':              rent_range_high,
        'str_monthly_gross':            str_fields['str_monthly_gross'],
        'str_monthly_net':              str_fields['str_monthly_net'],
        'str_vs_lt_pct':                str_fields['str_vs_lt_pct'],
        'str_better_value':             str_fields['str_better_value'],
        'str_data_available':           str_fields['str_data_available'],
        'str_confidence':               str_fields['str_confidence'],

        # Rental cashflow
        'gross_rent':                   gross_rent,
        'vacancy_deduction':            vacancy_deduction,
        'management_deduction':         management_deduction,
        'net_rental_income':            round(net_rental_income) if net_rental_income else None,

        # Yields
        'gross_yield_pct':              gross_yield_pct,
        'net_yield_pct':                net_yield_pct,
        'yield_verdict':                yield_verdict,
        'yield_label':                  yield_label,

        # Monthly costs (both old and new column names for compatibility)
        'est_monthly_rates':            costs['rates'],
        'est_monthly_levies_min':       costs['levies_min'],
        'est_monthly_levies_max':       costs['levies_max'],
        'est_monthly_insurance':        costs['insurance'],
        'est_monthly_maintenance':      costs['maintenance'],
        'monthly_rates':                costs['rates'],
        'monthly_levies_min':           costs['levies_min'],
        'monthly_levies_max':           costs['levies_max'],
        'monthly_insurance':            costs['insurance'],
        'monthly_maintenance':          costs['maintenance'],
        'monthly_costs_excl_bond':      monthly_costs_excl_bond,

        # Cashflow
        'monthly_cashflow_before_bond': monthly_cashflow_before_bond,
        'monthly_bond':                 monthly_bond,
        'cashflow_after_bond':          cashflow_after_bond,
        'cashflow_score':               cf_score,

        # Growth + vacancy
        'growth_rate_pct':              round(growth_rate * 100, 2),
        'growth_rate_source':           growth_source,
        'vacancy_rate_pct':             round(vacancy_rate * 100, 2),
        'growth_assumption_pct':        round(growth_rate * 100, 2),

        # Scores
        'investment_score':             invest_score,
        'market_momentum':              momentum,

        # 5-year projections
        'year_1_value':                 year_1_value,
        'year_2_value':                 year_2_value,
        'year_3_value':                 year_3_value,
        'year_4_value':                 year_4_value,
        'year_5_value':                 year_5_value,
        'capital_gain_5yr':             capital_gain_5yr,
        'rental_income_5yr':            cumulative_net_rent_5yr,
        'cumulative_net_rent_5yr':      cumulative_net_rent_5yr,
        'total_5yr_return':             total_5yr_return,
        'annualised_return_pct':        annualised_return_pct,

        # Transfer costs
        'transfer_duty':                transfer_duty,
        'transfer_costs':               transfer_costs,
        'bond_registration':            bond_reg,
        'total_acquisition_cost':       total_acq_cost,

        'computed_at':                  datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def _is_schema_error(e: Exception) -> bool:
    msg = str(e).lower()
    return any(kw in msg for kw in (
        'does not exist', '42p01', 'undefined table', 'relation',
        'pgrst205', 'could not find the table', 'schema cache',
    ))


def _paginate(fn, page_size: int = 1000) -> list[dict]:
    """Paginate a Supabase query. fn(offset, limit) must return a response."""
    results: list[dict] = []
    offset = 0
    while True:
        resp  = fn(offset, page_size)
        batch = resp.data or []
        results.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return results


def fetch_non_luxury_sale_signals(client) -> dict[str, dict]:
    """Return {listing_id: signal} for all non-luxury sale listings."""
    rows = _paginate(lambda off, lim: (
        client.table('listing_signals')
        .select('listing_id,homelens_score,verdict,is_luxury,listing_type')
        .eq('is_luxury', False)
        .eq('listing_type', 'sale')
        .range(off, off + lim - 1)
        .execute()
    ))
    return {r['listing_id']: r for r in rows}


def fetch_sale_listings(client, listing_ids: list[str]) -> list[dict]:
    """Fetch active sale listing rows for the given IDs."""
    results: list[dict] = []
    for i in range(0, len(listing_ids), 100):
        batch = listing_ids[i:i + 100]
        resp  = (
            client.table('listings')
            .select('listing_id,suburb_normalised,listing_type,price,'
                    'bedrooms,property_type,is_active,size_m2')
            .in_('listing_id', batch)
            .eq('listing_type', 'sale')
            .eq('is_active', True)
            .execute()
        )
        results.extend(resp.data or [])
    return results


# ---------------------------------------------------------------------------
# v5 Rent model — multiplicative hedonic, fitted at runtime from the corpus
# ---------------------------------------------------------------------------

REFERENCE_MAX_AGE_MONTHS = 12   # reference (inactive) rentals older than this
                                # are ignored — a safety valve for future data.
                                # Today nothing is older than ~3 months; this
                                # protects yields from staleness a year from now.


def _months_since(date_str: str | None, now: datetime) -> float | None:
    """Months between a listing's last_seen and now. None if unparseable."""
    if not date_str:
        return None
    s = re.sub(r'\+\d{2}(:?\d{2})?$', '', str(date_str).strip()).split('+')[0].strip()
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return (now - datetime.strptime(s, fmt)).days / 30.4
        except ValueError:
            continue
    return None


def fetch_rental_corpus(client) -> list[dict]:
    """
    Rental listings used as evidence for the rent model and yields.

    Includes:
      - ACTIVE rentals (live on the market), and
      - REFERENCE rentals (is_reference = TRUE): dead-but-priced listings whose
        historical rent is still real evidence, ONLY if last_seen is within
        REFERENCE_MAX_AGE_MONTHS.

    Reference listings are flagged by fix_suburbs.py. They are NEVER shown to
    users (that path uses is_active only) — this function is the one place they
    are read, purely to strengthen investment-side rent estimates. Cleaned of
    flatshare/warehouse pollution exactly like active listings.
    """
    now = datetime.utcnow()
    rows = _paginate(lambda off, lim: (
        client.table('listings')
        .select('suburb_normalised,bedrooms,property_type,price,size_m2,'
                'is_luxury,is_outlier,is_active,is_reference,last_seen')
        .eq('listing_type', 'rent')
        .eq('city', 'cape-town')   # rent model + CT rates; suburb names repeat across cities
        .range(off, off + lim - 1)
        .execute()
    ))
    corpus = []
    n_active = n_reference = n_ref_too_old = 0
    for r in rows:
        is_active    = bool(r.get('is_active'))
        is_reference = bool(r.get('is_reference'))
        # Only two kinds of rental count as evidence: live listings, or
        # explicitly-flagged reference listings within the recency window.
        if not is_active:
            if not is_reference:
                continue
            age = _months_since(r.get('last_seen'), now)
            if age is None or age > REFERENCE_MAX_AGE_MONTHS:
                n_ref_too_old += 1
                continue

        p = r.get('price')
        if not p or not (RENT_MODEL_MIN_RENT <= p <= RENT_MODEL_MAX_RENT):
            continue
        if r.get('is_luxury') or r.get('is_outlier'):
            continue
        sub = r.get('suburb_normalised')
        bed = str(r.get('bedrooms')) if r.get('bedrooms') is not None else None
        if not sub or bed not in RENT_MODEL_BEDS:
            continue
        pt = (r.get('property_type') or 'other').lower()
        if bed == '0' and pt != 'apartment':
            continue          # "0-bed house" = warehouse/commercial mislabel
        size = r.get('size_m2')
        size = float(size) if size and 15 <= float(size) <= 800 else None
        corpus.append(dict(sub=sub, bed=bed, pt=pt,
                           logr=math.log(p), size=size,
                           is_reference=(is_reference and not is_active)))
        if is_active:
            n_active += 1
        else:
            n_reference += 1

    logger.info('  rent corpus: %d active + %d reference (<= %d mo); '
                '%d reference skipped as too old',
                n_active, n_reference, REFERENCE_MAX_AGE_MONTHS, n_ref_too_old)
    return corpus


# ── v7: short-term rental (Airbnb) potential ─────────────────────────────────

def fetch_str_stats(client) -> dict:
    """
    Load AirDNA short-term stats and reduce to one gross monthly figure per
    (suburb, bedroom_count): the MEAN of the most recent STR_AVG_WINDOW months.

    Mean (not median) is deliberate here: an investor banks every month's
    income across the year, so the average month IS the annual-earnings basis.
    Median would discard the real money earned in peak months. (Contrast the
    engine's use of median for sale/rent baselines, where the job is outlier
    protection, not annualising a seasonal cycle.)

    Returns {(suburb, bed): {'gross': float, 'months': int, 'confidence': str}}.
    """
    try:
        rows = _paginate(lambda off, lim: (
            client.table('str_market_stats')
            .select('suburb_normalised,month,bedroom_count,avg_monthly_revenue')
            .order('month', desc=True)
            .range(off, off + lim - 1)
            .execute()
        ))
    except Exception as e:
        logger.warning('  STR stats unavailable (%s) — short-term panel disabled', e)
        return {}

    by_group: dict[tuple, list[tuple[str, float]]] = defaultdict(list)
    for r in rows:
        bed = str(r.get('bedroom_count'))
        if bed in ('all', '6+'):
            continue
        rev = r.get('avg_monthly_revenue')
        sub = r.get('suburb_normalised')
        mon = r.get('month')
        if not sub or rev is None or not mon:
            continue
        by_group[(sub, bed)].append((mon, float(rev)))

    out = {}
    for (sub, bed), series in by_group.items():
        series.sort(reverse=True)                 # newest first
        window = [rev for _m, rev in series[:STR_AVG_WINDOW]]
        if len(window) < STR_MIN_MONTHS:
            continue
        gross = sum(window) / len(window)          # MEAN across months
        conf = 'high' if len(window) >= STR_AVG_WINDOW else 'medium'
        out[(sub, bed)] = {'gross': gross, 'months': len(window), 'confidence': conf}
    logger.info('  STR stats: %d suburb+bed groups with >= %d months',
                len(out), STR_MIN_MONTHS)
    return out


def _str_net(gross: float) -> int:
    """Net monthly STR income. Vacancy NOT deducted — AirDNA revenue is already
    occupancy-adjusted (deducting again would double-count)."""
    mgmt = gross * STR_MGMT_RATE
    return round(gross - mgmt - STR_CLEANING_PER_MO - STR_UTILITIES_PER_MO)


STR_STUDIO_FACTOR = 0.75   # a studio earns ~75% of a 1-bedroom (no AirDNA studio series)
STR_STUDIO_MAX_M2 = 35     # no bedroom count: a flat this small is treated as a studio
STR_ONE_BED_MAX_M2 = 75    # ...up to this size as a 1-bedroom


def str_potential(str_stats: dict, suburb: str, bedrooms,
                  lt_net_monthly: float | None,
                  size_m2=None, property_type: str | None = None) -> dict | None:
    """
    Look up short-term potential for a listing and compare to long-term NET.
    Returns a dict for the STR panel, or None when no STR data for this
    suburb+bed (Option B honesty — no fabrication elsewhere).

    AirDNA has no studio series, and some listings carry no bedroom count, so
    two estimates are allowed, both marked str_confidence = 'estimated':
      * 0 bedrooms             -> 1-bedroom revenue x STR_STUDIO_FACTOR
      * no bedrooms, apartment -> by size: <= STR_STUDIO_MAX_M2 studio,
                                  <= STR_ONE_BED_MAX_M2 1-bedroom
    """
    bed = str(int(bedrooms)) if bedrooms not in (None, '') else None
    factor, estimated = 1.0, False
    if bed is None and property_type == 'apartment' and size_m2:
        if size_m2 <= STR_STUDIO_MAX_M2:
            bed, factor, estimated = '0', STR_STUDIO_FACTOR, True
        elif size_m2 <= STR_ONE_BED_MAX_M2:
            bed, estimated = '1', True
    if bed is None:
        return None
    lookup_bed = bed
    if bed == '0':
        lookup_bed, factor, estimated = '1', STR_STUDIO_FACTOR, True
    hit = str_stats.get((suburb, lookup_bed))
    if not hit:
        return None
    gross = hit['gross'] * factor
    net   = _str_net(gross)
    better = False
    vs_lt = None
    if lt_net_monthly and lt_net_monthly > 0:
        vs_lt = round((net - lt_net_monthly) / lt_net_monthly * 100, 1)
        better = net >= lt_net_monthly * (1 + STR_BETTER_MARGIN)
    return {
        'str_monthly_gross':  round(gross),
        'str_monthly_net':    net,
        'str_vs_lt_pct':      vs_lt,
        'str_better_value':   better,
        'str_data_available': True,
        'str_confidence':     'estimated' if estimated else hit['confidence'],
    }


def fit_rent_model(corpus: list[dict], iters: int = 15) -> dict | None:
    """
    Robust median polish on log(rent):
      suburb level (2-bed-apartment-equivalent) + bedroom effect + type effect
      + RENT_MODEL_GAMMA * (log size - median log size for that bed count).
    Returns model dict or None if the corpus is unusable.
    """
    if len(corpus) < 100:
        return None
    size_med: dict[str, float] = {}
    for bed in RENT_MODEL_BEDS:
        ss = [d['size'] for d in corpus if d['bed'] == bed and d['size']]
        if ss:
            size_med[bed] = statistics.median(ss)

    def _sadj(d):
        sm = size_med.get(d['bed'])
        if d['size'] and sm:
            return RENT_MODEL_GAMMA * (math.log(d['size']) - math.log(sm))
        return 0.0

    sub_e: dict = defaultdict(float)
    bed_e: dict = defaultdict(float)
    pt_e:  dict = defaultdict(float)
    for _ in range(iters):
        res = defaultdict(list)
        for d in corpus:
            res[d['sub']].append(d['logr'] - _sadj(d) - bed_e[d['bed']] - pt_e[d['pt']])
        for k, v in res.items():
            sub_e[k] = statistics.median(v)
        res = defaultdict(list)
        for d in corpus:
            res[d['bed']].append(d['logr'] - _sadj(d) - sub_e[d['sub']] - pt_e[d['pt']])
        for k, v in res.items():
            bed_e[k] = statistics.median(v)
        c = bed_e.get('2', 0.0)
        for k in bed_e:
            bed_e[k] -= c
        res = defaultdict(list)
        for d in corpus:
            res[d['pt']].append(d['logr'] - _sadj(d) - sub_e[d['sub']] - bed_e[d['bed']])
        for k, v in res.items():
            pt_e[k] = statistics.median(v)
        c = pt_e.get('apartment', 0.0)
        for k in pt_e:
            pt_e[k] -= c
        res = defaultdict(list)
        for d in corpus:
            res[d['sub']].append(d['logr'] - _sadj(d) - bed_e[d['bed']] - pt_e[d['pt']])
        for k, v in res.items():
            sub_e[k] = statistics.median(v)

    sub_n: dict = defaultdict(int)
    for d in corpus:
        sub_n[d['sub']] += 1
    return dict(sub_e=dict(sub_e), bed_e=dict(bed_e), pt_e=dict(pt_e),
                size_med=size_med, sub_n=dict(sub_n))


def model_rent_estimate(model: dict | None, suburb: str, bedrooms,
                        ptype: str, size_m2) -> tuple[int, int, int, str] | None:
    """
    Predict market rent for a property.
    Returns (rent, range_low, range_high, confidence) or None when the
    suburb has zero rental evidence (Option B: honest absence, no fabrication).
    """
    if not model or not suburb or suburb not in model['sub_e']:
        return None
    bed = str(bedrooms) if bedrooms is not None else None
    if bed not in RENT_MODEL_BEDS:
        return None       # 5+ bed / unknown: outside model support — honest null
    pt = (ptype or 'other').lower()
    if bed == '0' and pt != 'apartment':
        return None       # 0-bed non-apartment = farm/commercial — honest null
    adj = 0.0
    sm = model['size_med'].get(bed)
    if size_m2 and sm:
        try:
            s = float(size_m2)
            if 15 <= s <= 800:
                adj = RENT_MODEL_GAMMA * (math.log(s) - math.log(sm))
        except (TypeError, ValueError):
            pass
    log_rent = (model['sub_e'][suburb]
                + model['bed_e'].get(bed, 0.0)
                + model['pt_e'].get(pt, 0.0)
                + adj)
    rent = round(math.exp(log_rent))
    n = model['sub_n'].get(suburb, 0)
    lo_f, hi_f = RENT_RANGE_MEDIUM if n >= RENT_MODEL_GOOD_N else RENT_RANGE_LOW
    conf = 'medium' if n >= RENT_MODEL_GOOD_N else 'low'
    return rent, round(rent * lo_f), round(rent * hi_f), conf


def fetch_rent_baselines(client) -> tuple[dict, dict]:
    """
    Return two lookup dicts for sufficient rent baselines:
      exact:    (suburb_normalised, bedrooms, property_type) -> row
      fallback: (suburb_normalised, bedrooms)               -> first matching row
    """
    rows = _paginate(lambda off, lim: (
        client.table('suburb_baselines')
        .select('suburb_normalised,bedrooms,property_type,'
                'median_price,sample_size_price,market_consistency')
        .eq('listing_type', 'rent')
        .eq('has_sufficient_data', True)
        .range(off, off + lim - 1)
        .execute()
    ))

    exact:    dict = {}
    fallback: dict = {}
    for r in rows:
        key_e = (r['suburb_normalised'], r['bedrooms'], r['property_type'])
        key_f = (r['suburb_normalised'], r['bedrooms'])
        exact[key_e] = r
        if key_f not in fallback:
            fallback[key_f] = r

    return exact, fallback


def fetch_attractiveness(client) -> dict[str, int]:
    """Return {suburb_normalised: attractiveness_score}. Defaults to 50 if table missing."""
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
            if r.get('attractiveness_score') is not None
        }
    except Exception as e:
        if _is_schema_error(e):
            logger.info('suburb_attractiveness not found — using default score 50')
            return {}
        raise


def upsert_rows(client, rows: list[dict]) -> None:
    """Upsert a batch; falls back to row-by-row on failure."""
    try:
        client.table('listing_investment').upsert(
            rows, on_conflict='listing_id'
        ).execute()
    except Exception as e:
        logger.warning('Batch upsert failed (%d rows) — trying one-by-one: %s', len(rows), e)
        for row in rows:
            try:
                client.table('listing_investment').upsert(
                    row, on_conflict='listing_id'
                ).execute()
            except Exception as e2:
                logger.error('Single upsert failed for %s: %s', row.get('listing_id'), e2)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def _fmt(v: int | None) -> str:
    return f'R{v:,}' if v is not None else 'N/A'


def _suburb_tier_label(suburb: str | None) -> str:
    s = (suburb or '').lower().strip()
    if s in _ATLANTIC_SEABOARD:
        return 'atlantic_seaboard'
    if s in _CITY_BOWL_GENTRIFYING:
        return 'city_bowl'
    if s in _ATLANTIC_ESTABLISHED:
        return 'atlantic_established'
    if s in _SOUTHERN_SUBURBS:
        return 'southern_suburbs'
    return 'default'


def print_summary(
    computed_rows: list[dict],
    listing_map: dict,
    old_scores: dict[str, int] | None = None,
) -> None:
    total = len(computed_rows)
    if not total:
        print('  No rows computed.')
        return

    # ── Growth rate tiers ──────────────────────────────────────────────────
    tier_counts: dict[str, int] = {
        'atlantic_seaboard':   0,
        'city_bowl':           0,
        'atlantic_established': 0,
        'southern_suburbs':    0,
        'default':             0,
    }
    for r in computed_rows:
        listing = listing_map.get(r['listing_id'], {})
        tier = _suburb_tier_label(listing.get('suburb_normalised'))
        tier_counts[tier] += 1

    # ── Rental sources ─────────────────────────────────────────────────────
    from_baseline = sum(1 for r in computed_rows if r.get('rental_source') == 'comparable_rentals')
    from_model    = sum(1 for r in computed_rows if r.get('rental_source') == 'rental_model')
    no_rent       = total - from_baseline - from_model
    bl_pct  = round(100 * from_baseline / total) if total else 0
    est_pct = round(100 * from_model    / total) if total else 0
    no_pct  = round(100 * no_rent       / total) if total else 0

    # ── Yield distribution ─────────────────────────────────────────────────
    strong = good = average = low_yield = no_data = 0
    for r in computed_rows:
        gy = r.get('gross_yield_pct')
        if gy is None:
            no_data += 1
        elif gy >= 8:
            strong += 1
        elif gy >= 6:
            good += 1
        elif gy >= 4:
            average += 1
        else:
            low_yield += 1

    # ── Top 5 ──────────────────────────────────────────────────────────────
    scored = [
        r for r in computed_rows
        if r.get('investment_score') and r.get('gross_yield_pct')
    ]
    top5 = sorted(scored, key=lambda r: r['investment_score'], reverse=True)[:5]

    print()
    print('═' * 65)
    print('  === INVESTMENT COMPUTATION ===')
    print('═' * 65)
    print()
    print(f'  Properties computed : {total:,}')
    print()
    print('  Growth rates applied:')
    print(f'    Atlantic Seaboard premium   (9.0%) : {tier_counts["atlantic_seaboard"]:,}')
    print(f'    City Bowl / gentrifying     (9.0%) : {tier_counts["city_bowl"]:,}')
    print(f'    Atlantic Seaboard estab.    (8.5%) : {tier_counts["atlantic_established"]:,}')
    print(f'    Southern Suburbs            (7.0%) : {tier_counts["southern_suburbs"]:,}')
    print(f'    Default Cape Town           (7.5%) : {tier_counts["default"]:,}')
    print()
    print('  Rental income sources:')
    print(f'    comparable_rentals       : {from_baseline:,} ({bl_pct}%)')
    print(f'    rental_model             : {from_model:,} ({est_pct}%)')
    print(f'    No data                  : {no_rent:,} ({no_pct}%)')
    print()
    print('  Yield distribution:')
    print(f'    Strong  (≥ 8%) : {strong}')
    print(f'    Good    (6-8%) : {good}')
    print(f'    Average (4-6%) : {average}')
    print(f'    Low     (< 4%) : {low_yield}')
    print(f'    No data        : {no_data}')

    if top5:
        print()
        print('  Top 5 by investment score:')
        for r in top5:
            listing  = listing_map.get(r['listing_id'], {})
            suburb   = (listing.get('suburb_normalised') or '').replace('-', ' ').title()
            beds     = listing.get('bedrooms') or '?'
            price    = listing.get('price') or 0
            gy       = r.get('gross_yield_pct') or 0
            score    = r.get('investment_score') or 0
            growth   = r.get('growth_rate_pct') or 0
            ret5     = r.get('total_5yr_return')
            ann      = r.get('annualised_return_pct')
            p_s      = r.get('_p_score', 0)
            y_s      = r.get('_y_score', 0)
            g_s      = r.get('_g_score', 0)
            cf_s     = r.get('cashflow_score', 0)
            print(f'    {suburb} {beds}bed {_fmt(price)}')
            print(f'      Yield: {gy:.1f}%  |  Score: {score}')
            print(f'      Growth: {growth:.1f}%  |  5yr return: {_fmt(ret5)}  |  Ann. return: {ann:.1f}%' if ann else
                  f'      Growth: {growth:.1f}%  |  5yr return: {_fmt(ret5)}')

    # ── Weighting update before/after comparison ───────────────────────────
    if old_scores:
        new_scores = {r['listing_id']: r.get('investment_score') for r in computed_rows}
        both = [(lid, old_scores[lid], new_scores[lid])
                for lid in old_scores if lid in new_scores
                and old_scores[lid] is not None and new_scores[lid] is not None]

        if both:
            old_avg = round(sum(o for _, o, _ in both) / len(both))
            new_avg = round(sum(n for _, _, n in both) / len(both))

            def _dist(scores):
                return {
                    'exceptional': sum(1 for s in scores if s >= 80),
                    'strong':      sum(1 for s in scores if 65 <= s < 80),
                    'moderate':    sum(1 for s in scores if 50 <= s < 65),
                    'cautious':    sum(1 for s in scores if 35 <= s < 50),
                    'high_risk':   sum(1 for s in scores if s < 35),
                }

            old_d = _dist([o for _, o, _ in both])
            new_d = _dist([n for _, _, n in both])

            # Sample 5 listings with biggest score change
            sample = sorted(both, key=lambda x: abs(x[2] - x[1]), reverse=True)[:5]

            print()
            print('  === WEIGHTING UPDATE RESULTS ===')
            print()
            print('  Sample listings (largest score change):')
            for lid, old_s, new_s in sample:
                listing = listing_map.get(lid, {})
                suburb  = (listing.get('suburb_normalised') or '').replace('-', ' ').title()
                beds    = listing.get('bedrooms') or '?'
                price   = listing.get('price') or 0
                row     = next((r for r in computed_rows if r['listing_id'] == lid), {})
                p_s     = (signals_ref.get(lid, {}).get('homelens_score') or 50) if hasattr(print_summary, '_signals') else 50
                y_s_v   = _yield_score(row.get('gross_yield_pct'))
                g_s_v   = _growth_score(row.get('growth_rate_pct', 7.5) / 100)
                cf_s_v  = row.get('cashflow_score', 50)
                p_s_v   = round(new_s - y_s_v * 0.30 - g_s_v * 0.20 - cf_s_v * 0.30) // 1
                print(f'    {suburb} {beds}bed {_fmt(price)}')
                print(f'      Old score: {old_s}  →  New score: {new_s}  (Δ {new_s - old_s:+d})')
                print(f'      Price:    {p_s_v:>3} × 20% = {p_s_v * 0.20:>4.0f}')
                print(f'      Yield:    {y_s_v:>3} × 30% = {y_s_v * 0.30:>4.0f}')
                print(f'      Growth:   {g_s_v:>3} × 20% = {g_s_v * 0.20:>4.0f}')
                print(f'      Cashflow: {cf_s_v:>3} × 30% = {cf_s_v * 0.30:>4.0f}')
            print()
            print('  Score distribution change:')
            print(f'    Old avg: {old_avg}   New avg: {new_avg}')
            print()
            print(f'    {"Band":<20}  {"Old":>5}  {"New":>5}')
            print(f'    {"Exceptional (80+)":<20}  {old_d["exceptional"]:>5}  {new_d["exceptional"]:>5}')
            print(f'    {"Strong (65-79)":<20}  {old_d["strong"]:>5}  {new_d["strong"]:>5}')
            print(f'    {"Moderate (50-64)":<20}  {old_d["moderate"]:>5}  {new_d["moderate"]:>5}')
            print(f'    {"Cautious (35-49)":<20}  {old_d["cautious"]:>5}  {new_d["cautious"]:>5}')
            print(f'    {"High risk (<35)":<20}  {old_d["high_risk"]:>5}  {new_d["high_risk"]:>5}')

    print()
    print('═' * 65)
    print()


# ---------------------------------------------------------------------------
# Env check
# ---------------------------------------------------------------------------

def _check_env() -> None:
    missing = [k for k in ('SUPABASE_URL', 'SUPABASE_KEY') if not os.environ.get(k)]
    if missing:
        logger.error('Missing environment variables: %s', ', '.join(missing))
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    _check_env()
    client = get_client()

    print()
    print('━' * 65)
    print('  Homelens — Investment Analysis')
    print('━' * 65)

    t_start = time.time()

    # ── Check listing_investment table exists ──────────────────────────────
    try:
        client.table('listing_investment').select('listing_id').limit(1).execute()
    except Exception as e:
        if _is_schema_error(e):
            print()
            print('  listing_investment table does not exist.')
            print('  Run the following SQL in the Supabase SQL editor:')
            print()
            print(MIGRATION_SQL)
            sys.exit(1)
        raise

    # ── 1. Non-luxury sale signals ─────────────────────────────────────────
    logger.info('Fetching non-luxury sale signals…')
    signals = fetch_non_luxury_sale_signals(client)
    logger.info('  %d non-luxury sale signals found', len(signals))

    if not signals:
        print('  No non-luxury sale signals found. Run the engine first.')
        print('━' * 65)
        return

    # ── 2. Active sale listings ────────────────────────────────────────────
    logger.info('Fetching active sale listings…')
    listings    = fetch_sale_listings(client, list(signals.keys()))
    listing_map = {lst['listing_id']: lst for lst in listings}
    logger.info('  %d active sale listings loaded', len(listings))

    # ── 3. Sufficient rent baselines ───────────────────────────────────────
    logger.info('Fetching sufficient rent baselines…')
    rent_exact, rent_fallback = fetch_rent_baselines(client)
    logger.info(
        '  %d exact baseline groups, %d fallback groups loaded',
        len(rent_exact), len(rent_fallback),
    )

    # ── 3b. v5 rent model — fitted from the full rental corpus ────────────
    logger.info('Fetching rental corpus (active + inactive) for rent model…')
    corpus     = fetch_rental_corpus(client)
    rent_model = fit_rent_model(corpus)
    if rent_model:
        logger.info('  rent model fitted on %d rentals across %d suburbs',
                    len(corpus), len(rent_model['sub_e']))
    else:
        logger.warning('  rental corpus too small — model disabled, '
                       'no-comparable listings will show no_rental_data')

    # ── 3c. Short-term (Airbnb) stats (v7) ─────────────────────────────────
    logger.info('Fetching short-term (AirDNA) stats…')
    str_stats = fetch_str_stats(client)

    # ── 4. Suburb attractiveness ───────────────────────────────────────────
    attractiveness_map = fetch_attractiveness(client)

    # ── 4b. Snapshot existing scores for before/after comparison ──────────
    logger.info('Snapshotting existing investment scores…')
    old_scores: dict[str, int] = {}
    try:
        snap_rows = _paginate(lambda off, lim: (
            client.table('listing_investment')
            .select('listing_id,investment_score')
            .range(off, off + lim - 1)
            .execute()
        ))
        old_scores = {
            r['listing_id']: r['investment_score']
            for r in snap_rows
            if r.get('investment_score') is not None
        }
        logger.info('  %d existing scores snapshotted', len(old_scores))
    except Exception as e:
        logger.warning('Could not snapshot old scores: %s', e)

    # ── 5. Compute and upsert ──────────────────────────────────────────────
    logger.info('Computing investment metrics for %d listings…', len(listings))

    pending_upsert: list[dict] = []
    computed_rows:  list[dict] = []
    skipped = 0

    for listing in listings:
        lid    = listing['listing_id']
        suburb = listing.get('suburb_normalised')
        beds   = listing.get('bedrooms')
        ptype  = (listing.get('property_type') or '').lower()

        # Rent baseline: exact match preferred, then suburb+beds only
        rent_baseline = None
        if suburb and beds is not None:
            rent_baseline = (
                rent_exact.get((suburb, beds, ptype)) or
                rent_fallback.get((suburb, beds))
            )

        signal      = signals.get(lid)
        attract_val = attractiveness_map.get(suburb, 50)

        try:
            row = compute_investment_row(listing, rent_baseline, signal, attract_val,
                                         rent_model=rent_model, str_stats=str_stats)
            pending_upsert.append(row)
            computed_rows.append(row)
        except Exception as e:
            logger.error('Compute failed for %s: %s', lid, e)
            skipped += 1

        if len(pending_upsert) >= _BATCH_SIZE:
            upsert_rows(client, pending_upsert)
            pending_upsert = []

    if pending_upsert:
        upsert_rows(client, pending_upsert)

    elapsed = round(time.time() - t_start, 1)
    logger.info(
        'Done in %ss — computed: %d, skipped: %d',
        elapsed, len(computed_rows), skipped,
    )

    # ── 6. Summary ─────────────────────────────────────────────────────────
    print_summary(computed_rows, listing_map, old_scores=old_scores)


if __name__ == '__main__':
    main()
