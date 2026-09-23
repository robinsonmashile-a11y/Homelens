# Homelens Gumtree Scraper

Async Playwright scraper for Cape Town property listings from Gumtree South Africa.
Writes structured data to Supabase and runs on GitHub Actions every 6 hours.

---

## Project structure

```
homelens-scraper/
├── .env                          # Local secrets — never commit
├── .env.example                  # Template
├── .gitignore
├── requirements.txt
├── scraper/
│   ├── __init__.py
│   ├── gumtree.py                # Playwright scraper
│   ├── parser.py                 # Claude API description parser
│   ├── database.py               # Supabase read/write
│   └── utils.py                  # Price, suburb helpers + POPIA cleaning
├── scripts/
│   └── run_scraper.py            # Entry point
└── .github/
    └── workflows/
        └── scraper.yml           # GitHub Actions cron (every 6 hours)
```

---

## 1. Supabase setup

### Create tables

Run the following SQL in your Supabase project's **SQL Editor**:

```sql
-- Listings table
CREATE TABLE IF NOT EXISTS listings (
    listing_id TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'gumtree',
    listing_type TEXT NOT NULL CHECK (listing_type IN ('rent', 'sale')),
    suburb TEXT,
    suburb_normalised TEXT,
    price INTEGER,
    price_per_m2 NUMERIC,
    bedrooms SMALLINT,
    bathrooms NUMERIC,
    size_m2 NUMERIC,
    has_solar BOOLEAN DEFAULT FALSE,
    has_parking BOOLEAN DEFAULT FALSE,
    has_garden BOOLEAN DEFAULT FALSE,
    pet_friendly BOOLEAN,
    title TEXT,
    description TEXT,
    listing_url TEXT,
    price_history JSONB DEFAULT '[]'::jsonb,
    first_seen TIMESTAMPTZ DEFAULT NOW(),
    last_seen TIMESTAMPTZ DEFAULT NOW(),
    price_at_first INTEGER,
    days_on_market INTEGER,
    is_active BOOLEAN DEFAULT TRUE,
    raw_data JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_listings_suburb ON listings(suburb_normalised);
CREATE INDEX IF NOT EXISTS idx_listings_type ON listings(listing_type);
CREATE INDEX IF NOT EXISTS idx_listings_active ON listings(is_active);
CREATE INDEX IF NOT EXISTS idx_listings_price ON listings(price);

-- Suburb baselines table (computed daily)
CREATE TABLE IF NOT EXISTS suburb_baselines (
    id SERIAL PRIMARY KEY,
    suburb TEXT NOT NULL,
    listing_type TEXT NOT NULL,
    bedrooms SMALLINT,
    sample_size INTEGER,
    median_price INTEGER,
    median_price_m2 NUMERIC,
    p25_price INTEGER,
    p75_price INTEGER,
    avg_days_on_market NUMERIC,
    computed_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(suburb, listing_type, bedrooms)
);

-- Scrape runs log
CREATE TABLE IF NOT EXISTS scrape_runs (
    id SERIAL PRIMARY KEY,
    source TEXT,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    listings_found INTEGER DEFAULT 0,
    listings_new INTEGER DEFAULT 0,
    listings_updated INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running'
);
```

### Get your credentials

In your Supabase project go to **Settings → API** and copy:

| Variable | Where to find it |
|---|---|
| `SUPABASE_URL` | Project URL (e.g. `https://xxxx.supabase.co`) |
| `SUPABASE_KEY` | `anon` public key |

---

## 2. Local setup

```bash
# Clone / navigate to the repo
cd homelens-scraper

# Create virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright browser
playwright install chromium

# Copy and fill in credentials
cp .env.example .env
# Edit .env with your SUPABASE_URL, SUPABASE_KEY, ANTHROPIC_API_KEY
```

---

## 3. Run locally

```bash
python scripts/run_scraper.py
```

Output example:
```
2024-05-18 10:00:00 [INFO] homelens — === Homelens Gumtree Scraper starting ===
2024-05-18 10:00:01 [INFO] homelens — Scrape run logged with id=1
2024-05-18 10:00:05 [INFO] homelens.gumtree — Scraping rent page 1: https://www.gumtree.co.za/...
2024-05-18 10:00:12 [INFO] homelens — [NEW] 12345678 — 2 Bedroom Apartment Sea Point...
...
2024-05-18 10:45:00 [INFO] homelens — === Scrape complete ===
2024-05-18 10:45:00 [INFO] homelens —   Found     : 142
2024-05-18 10:45:00 [INFO] homelens —   New       : 38
2024-05-18 10:45:00 [INFO] homelens —   Updated   : 5
2024-05-18 10:45:00 [INFO] homelens —   Unchanged : 99
2024-05-18 10:45:00 [INFO] homelens —   Errors    : 0
```

---

## 4. GitHub Actions setup

### Add secrets to your repo

Go to **GitHub → repo → Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret name | Value |
|---|---|
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_KEY` | Your Supabase anon key |
| `ANTHROPIC_API_KEY` | Your Anthropic API key |

### Schedule

The workflow runs automatically at **06:00, 12:00, 18:00, 00:00 SAST** (UTC 04:00, 10:00, 16:00, 22:00).

You can also trigger it manually from **Actions → Homelens Gumtree Scraper → Run workflow**.

---

## POPIA compliance

The scraper never stores:
- Seller or landlord names
- Phone numbers (stripped via regex before DB write)
- Email addresses (stripped via regex before DB write)
- WhatsApp contact details (stripped via regex before DB write)

All contact information is replaced with `[contact removed]` in the stored description.

---

## Data schema notes

- `listing_id` — Gumtree's numeric listing ID extracted from the URL (primary key)
- `price_history` — JSONB array of `{price, date}` objects; appended on every price change
- `is_active` — set to `false` for listings not seen in the most recent scrape run
- `days_on_market` — computed from `first_seen` to the date of last price change
- `raw_data` — full snapshot of the scraped + parsed listing at time of last update
