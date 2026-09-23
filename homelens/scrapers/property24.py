"""
Property24 rental listing scraper using Playwright.

Scrapes one suburb at a time, handles pagination, and returns a list
of Listing objects. Deliberately excludes all personal information
(agent names, phone numbers, emails) for POPIA compliance.

Selector notes
--------------
Property24 uses a React-rendered frontend. Key selectors as of 2024:

  Listing tiles:        [data-testid="listing-result-item"]  (fallback: .p24_regularTile)
  Price text:           [data-testid="listing-result-price"] (fallback: .p24_price)
  Property features:    the icon+text rows for bed/bath/size live inside
                        [data-testid="listing-result-preview"] span elements.
  Detail href:          <a> wrapping the listing tile

If Property24 updates their HTML, run:
    python scripts/debug_selectors.py --suburb sea-point
to print a snapshot of the first page so you can update selectors here.
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Optional
from urllib.parse import urljoin

from playwright.sync_api import Browser, Page, Playwright, sync_playwright

from homelens.config import SCRAPE_DELAY_MIN, SCRAPE_DELAY_MAX, SUBURBS
from homelens.models.listing import Listing

logger = logging.getLogger(__name__)

BASE_URL = "https://www.property24.com"

# ── CSS / data-testid selectors ─────────────────────────────────────────────
# Primary selectors (data-testid — more stable than class names)
SEL_TILE = '[data-testid="listing-result-item"]'
SEL_PRICE = '[data-testid="listing-result-price"]'

# Fallback class-based selectors (update these if the above stop working)
SEL_TILE_FB = ".p24_regularTile, .p24_result"
SEL_PRICE_FB = ".p24_price"

# Pagination: "Next" button
SEL_NEXT = 'a[aria-label="Go to next page"], a.p24_pagerNext'

# Feature icon labels — Property24 renders these as aria-label or title attributes
# on SVG icons next to the feature value.
FEAT_BED_LABEL   = re.compile(r"bed(room)?s?", re.I)
FEAT_BATH_LABEL  = re.compile(r"bath(room)?s?", re.I)
FEAT_SIZE_LABEL  = re.compile(r"(floor\s*)?area|m.{0,3}²|sqm", re.I)
FEAT_SOLAR_TEXT  = re.compile(r"solar", re.I)
FEAT_PARK_TEXT   = re.compile(r"park(ing)?|garage|carport", re.I)


def _random_delay() -> None:
    """Polite delay between requests — mimic human browsing speed."""
    delay = random.uniform(SCRAPE_DELAY_MIN, SCRAPE_DELAY_MAX)
    logger.debug("Waiting %.1fs before next request", delay)
    time.sleep(delay)


def _parse_price(text: str) -> Optional[int]:
    """Extract integer ZAR value from a price string like 'R 13 500 pm'."""
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _parse_int(text: str) -> Optional[int]:
    match = re.search(r"\d+", text)
    return int(match.group()) if match else None


def _parse_float(text: str) -> Optional[float]:
    # Handles "75 m²" or "75.5m²"
    match = re.search(r"[\d]+\.?\d*", text.replace(",", "."))
    return float(match.group()) if match else None


class Property24Scraper:
    """
    Scrapes rental listings for one or more Cape Town suburbs from Property24.

    Usage
    -----
    scraper = Property24Scraper()
    listings = scraper.scrape_suburb("sea-point", max_pages=5)
    """

    def __init__(self, headless: bool = True):
        self.headless = headless

    def scrape_all_suburbs(self, max_pages_per_suburb: int = 10) -> list[Listing]:
        """Scrape every suburb defined in config.SUBURBS."""
        all_listings: list[Listing] = []
        with sync_playwright() as pw:
            browser = self._launch_browser(pw)
            try:
                for slug, url in SUBURBS.items():
                    logger.info("Scraping suburb: %s", slug)
                    listings = self._scrape_suburb_pages(
                        browser, slug, url, max_pages=max_pages_per_suburb
                    )
                    all_listings.extend(listings)
                    logger.info(
                        "Finished %s — %d listings collected", slug, len(listings)
                    )
                    _random_delay()
            finally:
                browser.close()
        return all_listings

    def scrape_suburb(self, slug: str, max_pages: int = 10) -> list[Listing]:
        """Scrape a single suburb by its slug key (e.g. 'sea-point')."""
        if slug not in SUBURBS:
            raise ValueError(
                f"Unknown suburb '{slug}'. Valid slugs: {list(SUBURBS.keys())}"
            )
        url = SUBURBS[slug]
        with sync_playwright() as pw:
            browser = self._launch_browser(pw)
            try:
                return self._scrape_suburb_pages(browser, slug, url, max_pages)
            finally:
                browser.close()

    # ── Private helpers ───────────────────────────────────────────────────

    def _launch_browser(self, pw: Playwright) -> Browser:
        return pw.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )

    def _new_page(self, browser: Browser) -> Page:
        """Create a new page with a realistic viewport and user-agent."""
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-ZA",
            timezone_id="Africa/Johannesburg",
        )
        # Remove webdriver flag
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        return ctx.new_page()

    def _scrape_suburb_pages(
        self, browser: Browser, slug: str, start_url: str, max_pages: int
    ) -> list[Listing]:
        listings: list[Listing] = []
        page = self._new_page(browser)
        current_url: Optional[str] = start_url
        page_num = 0

        try:
            while current_url and page_num < max_pages:
                page_num += 1
                logger.info("  Page %d: %s", page_num, current_url)
                page.goto(current_url, wait_until="domcontentloaded", timeout=30_000)

                # Wait for listing tiles — if none appear within 10s the suburb
                # page may be empty or the selector is wrong.
                try:
                    page.wait_for_selector(
                        f"{SEL_TILE}, {SEL_TILE_FB}", timeout=10_000
                    )
                except Exception:
                    logger.warning(
                        "No listing tiles found on page %d for %s — "
                        "may have hit a CAPTCHA or the suburb is empty.",
                        page_num, slug,
                    )
                    # Dump HTML for debugging
                    _dump_debug_html(page, slug, page_num)
                    break

                tiles = page.query_selector_all(f"{SEL_TILE}, {SEL_TILE_FB}")
                logger.info("  Found %d tiles", len(tiles))

                for tile in tiles:
                    listing = self._parse_tile(tile, slug)
                    if listing:
                        listings.append(listing)

                # Pagination — find the "Next" button
                next_el = page.query_selector(SEL_NEXT)
                if next_el:
                    href = next_el.get_attribute("href")
                    current_url = urljoin(BASE_URL, href) if href else None
                else:
                    current_url = None  # last page

                if current_url:
                    _random_delay()
        finally:
            page.close()

        return listings

    def _parse_tile(self, tile, suburb: str) -> Optional[Listing]:
        """Extract a Listing from a single listing tile element."""
        try:
            # ── URL ──────────────────────────────────────────────────────
            link_el = tile.query_selector("a[href*='/to-rent/'], a[href*='/property-for-sale/']")
            if not link_el:
                link_el = tile.query_selector("a")
            if not link_el:
                return None

            href = link_el.get_attribute("href") or ""
            if not href:
                return None
            raw_url = urljoin(BASE_URL, href)

            # Skip if not a listing detail page (e.g. promo banners)
            if "/to-rent/" not in raw_url and "/property-for-sale/" not in raw_url:
                return None

            # ── Price ─────────────────────────────────────────────────────
            price_el = (
                tile.query_selector(SEL_PRICE)
                or tile.query_selector(SEL_PRICE_FB)
            )
            if not price_el:
                return None
            price = _parse_price(price_el.inner_text())
            if not price or price < 1000:
                # Skip obviously wrong prices (< R1000 is not a real Cape Town rental)
                return None

            # ── Bedrooms / Bathrooms / Size ───────────────────────────────
            bedrooms, bathrooms, size_m2 = self._parse_features(tile)

            # ── Solar / Parking flags ─────────────────────────────────────
            full_text = tile.inner_text().lower()
            has_solar = bool(FEAT_SOLAR_TEXT.search(full_text))
            has_parking = bool(FEAT_PARK_TEXT.search(full_text))

            return Listing(
                raw_url=raw_url,
                source="property24",
                listing_type="rent",
                suburb=suburb,
                price=price,
                bedrooms=bedrooms,
                bathrooms=bathrooms,
                size_m2=size_m2,
                has_solar=has_solar,
                has_parking=has_parking,
            )

        except Exception as exc:
            logger.debug("Failed to parse tile: %s", exc)
            return None

    def _parse_features(self, tile) -> tuple[Optional[int], Optional[int], Optional[float]]:
        """
        Property24 renders features as a row of icon + text pairs.
        We identify each feature by the surrounding text / aria-label.

        Strategy:
        1. Look for a <ul> or <div> containing feature items.
        2. For each item, check the icon's aria-label or a nearby <span>.
        3. Match against bedroom / bathroom / size patterns.
        """
        bedrooms: Optional[int] = None
        bathrooms: Optional[int] = None
        size_m2: Optional[float] = None

        # Attempt 1: data-testid attributes on individual features
        for attr_val, pattern in [
            ("bedrooms", FEAT_BED_LABEL),
            ("bathrooms", FEAT_BATH_LABEL),
        ]:
            el = tile.query_selector(f'[data-testid*="{attr_val}"]')
            if el:
                val = _parse_int(el.inner_text())
                if attr_val == "bedrooms":
                    bedrooms = val
                else:
                    bathrooms = val

        size_el = tile.query_selector('[data-testid*="floor-size"], [data-testid*="erf-size"]')
        if size_el:
            size_m2 = _parse_float(size_el.inner_text())

        # Attempt 2: icon list items — scan all small text nodes
        if bedrooms is None or bathrooms is None or size_m2 is None:
            items = tile.query_selector_all(
                ".p24_featureDetails li, .p24_info li, "
                "[class*='feature'] li, [class*='Info'] li"
            )
            for item in items:
                text = item.inner_text().strip()
                icon = item.query_selector("svg, img, i, [class*='icon']")
                label = ""
                if icon:
                    label = (
                        icon.get_attribute("aria-label")
                        or icon.get_attribute("title")
                        or ""
                    )

                combined = f"{label} {text}"

                if bedrooms is None and FEAT_BED_LABEL.search(combined):
                    bedrooms = _parse_int(text)
                elif bathrooms is None and FEAT_BATH_LABEL.search(combined):
                    bathrooms = _parse_int(text)
                elif size_m2 is None and FEAT_SIZE_LABEL.search(combined):
                    size_m2 = _parse_float(text)

        # Attempt 3: Fall back to plain text scan of the tile
        # Property24 often renders "3 Beds  2 Baths  75 m²" as plain text
        if bedrooms is None or bathrooms is None or size_m2 is None:
            full_text = tile.inner_text()

            if bedrooms is None:
                m = re.search(r"(\d+)\s*(?:bed(?:room)?s?)", full_text, re.I)
                if m:
                    bedrooms = int(m.group(1))

            if bathrooms is None:
                m = re.search(r"(\d+)\s*(?:bath(?:room)?s?)", full_text, re.I)
                if m:
                    bathrooms = int(m.group(1))

            if size_m2 is None:
                m = re.search(r"([\d,]+\.?\d*)\s*m[²2]", full_text, re.I)
                if m:
                    size_m2 = _parse_float(m.group(1))

        return bedrooms, bathrooms, size_m2


def _dump_debug_html(page: Page, suburb: str, page_num: int) -> None:
    """Write page HTML to a debug file so you can inspect the structure."""
    import os

    os.makedirs("debug", exist_ok=True)
    path = f"debug/{suburb}_page{page_num}.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(page.content())
    logger.warning("Saved debug HTML to %s", path)
