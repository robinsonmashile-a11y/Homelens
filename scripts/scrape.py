"""
Homelens — main scraper entry point.

Usage
-----
    # Scrape all Phase 1 suburbs and write to Supabase:
    python scripts/scrape.py

    # Scrape a single suburb only:
    python scripts/scrape.py --suburb sea-point

    # Dry run — scrape but print results instead of writing to DB:
    python scripts/scrape.py --dry-run

    # Show raw debug output in a visible browser window:
    python scripts/scrape.py --no-headless --suburb sea-point
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter

from dotenv import load_dotenv

# Load .env before importing anything that reads env vars
load_dotenv()

from homelens.db.client import ListingsDB
from homelens.models.listing import Listing
from homelens.scrapers.property24 import Property24Scraper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("homelens.scrape")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Homelens property scraper")
    parser.add_argument(
        "--suburb",
        default=None,
        help="Single suburb slug to scrape (e.g. sea-point). Omit to scrape all.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=10,
        help="Maximum result pages to fetch per suburb (default: 10)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scrape and print results but do not write to Supabase",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run browser in visible (non-headless) mode for debugging",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    scraper = Property24Scraper(headless=not args.no_headless)
    db = None if args.dry_run else ListingsDB()

    # ── Scrape ────────────────────────────────────────────────────────────
    if args.suburb:
        logger.info("Scraping single suburb: %s", args.suburb)
        listings = scraper.scrape_suburb(args.suburb, max_pages=args.max_pages)
    else:
        logger.info("Scraping all Phase 1 suburbs")
        listings = scraper.scrape_all_suburbs(max_pages_per_suburb=args.max_pages)

    logger.info("Total listings scraped: %d", len(listings))

    if not listings:
        logger.warning(
            "No listings returned. If this is unexpected:\n"
            "  1. Run with --no-headless --suburb sea-point to watch the browser\n"
            "  2. Check debug/ folder for saved HTML snapshots\n"
            "  3. Update selectors in homelens/scrapers/property24.py"
        )
        sys.exit(0)

    # ── Dry run — print and exit ──────────────────────────────────────────
    if args.dry_run:
        _print_summary(listings)
        sys.exit(0)

    # ── Write to Supabase ─────────────────────────────────────────────────
    outcomes: Counter = Counter()
    seen_by_suburb: dict[str, list[str]] = {}

    for listing in listings:
        result = db.upsert_listing(listing)
        outcomes[result] += 1
        seen_by_suburb.setdefault(listing.suburb, []).append(listing.listing_id)

    # Mark any listing NOT seen in this run as inactive
    for suburb, active_ids in seen_by_suburb.items():
        db.mark_stale_inactive(suburb, active_ids)

    logger.info(
        "Done. inserted=%d updated=%d unchanged=%d",
        outcomes["inserted"],
        outcomes["updated"],
        outcomes["unchanged"],
    )


def _print_summary(listings: list[Listing]) -> None:
    """Pretty-print listings for dry-run inspection."""
    by_suburb: dict[str, list[Listing]] = {}
    for l in listings:
        by_suburb.setdefault(l.suburb, []).append(l)

    for suburb, items in sorted(by_suburb.items()):
        prices = [i.price for i in items]
        print(f"\n{'─'*60}")
        print(f"  {suburb.upper()}  ({len(items)} listings)")
        print(f"  Price range: R{min(prices):,} – R{max(prices):,}")
        print(f"  Median:      R{sorted(prices)[len(prices)//2]:,}")
        print()
        for i in sorted(items, key=lambda x: x.price)[:5]:
            beds = f"{i.bedrooms}BR" if i.bedrooms else "?BR"
            size = f"{i.size_m2}m²" if i.size_m2 else "?m²"
            ppm2 = f"R{i.price_per_m2:.0f}/m²" if i.price_per_m2 else ""
            print(
                f"  R{i.price:>7,}  {beds}  {size:>8}  {ppm2:<12}  {i.raw_url[:60]}"
            )
        if len(items) > 5:
            print(f"  ... and {len(items) - 5} more")


if __name__ == "__main__":
    main()
