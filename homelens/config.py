"""
Homelens — central configuration.

Suburb URL map: keys are normalised slugs stored in the DB,
values are the Property24 listing pages to scrape.

To find the correct URL for a suburb:
1. Go to property24.com → To Rent → search the suburb name
2. Copy the URL from the browser — it will look like:
   https://www.property24.com/to-rent/sea-point/cape-town/western-cape/8/
3. Paste it here.

The trailing number is Property24's internal location ID and differs per suburb.
"""

# Phase 1 suburbs — Cape Town focus
SUBURBS: dict[str, str] = {
    "sea-point": "https://www.property24.com/to-rent/sea-point/cape-town/western-cape/8/",
    "green-point": "https://www.property24.com/to-rent/green-point/cape-town/western-cape/8/",
    "observatory": "https://www.property24.com/to-rent/observatory/cape-town/western-cape/8/",
    "gardens": "https://www.property24.com/to-rent/gardens/cape-town/western-cape/8/",
    "woodstock": "https://www.property24.com/to-rent/woodstock/cape-town/western-cape/8/",
}

# Minimum comparables before a signal is created
SIGNAL_MIN_SAMPLE = 5

# How many % above/below baseline triggers a signal
OVERPRICED_THRESHOLD = 0.10   # +10%
UNDERVALUE_THRESHOLD = -0.10  # -10%

# Number of price drops that triggers a 'motivated_seller' signal
MOTIVATED_SELLER_DROPS = 3

# Baseline is computed from listings seen within this many days
BASELINE_WINDOW_DAYS = 90

# Random delay range between page loads (seconds) — stay polite
SCRAPE_DELAY_MIN = 3
SCRAPE_DELAY_MAX = 7
