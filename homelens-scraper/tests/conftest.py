"""
Shared test harness.

Site-selector tests load the REAL pages saved in tests/fixtures/ into Chromium
(JavaScript off — they are already-rendered DOM snapshots) and run the actual
scraper code against them. Re-capture a site with:
    python scripts/capture_fixtures.py <site>
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / 'tests' / 'fixtures'


def fixture_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding='utf-8')


class Browser:
    """One Chromium for the whole test session, driven from a private event loop."""

    def __init__(self):
        from playwright.async_api import async_playwright
        self.loop = asyncio.new_event_loop()
        self._pw = self.loop.run_until_complete(async_playwright().start())
        self._browser = self.loop.run_until_complete(self._pw.chromium.launch(headless=True))
        self._ctx = self.loop.run_until_complete(self._browser.new_context(java_script_enabled=False))
        self._page = self.loop.run_until_complete(self._ctx.new_page())
        self._text_cache: dict[str, str] = {}

    def run(self, coro):
        return self.loop.run_until_complete(coro)

    def load(self, html: str):
        """Load html into the shared page and return it."""
        self.run(self._page.set_content(html, wait_until='domcontentloaded'))
        return self._page

    def with_fixture(self, name: str):
        return self.load(fixture_html(name))

    def body_text(self, name: str) -> str:
        """document.body.innerText of a saved page — what the text fallback sees."""
        if name not in self._text_cache:
            page = self.with_fixture(name)
            self._text_cache[name] = self.run(page.evaluate('() => document.body.innerText'))
        return self._text_cache[name]

    def body_textcontent(self, name: str) -> str:
        """document.body.textContent — includes hidden elements (e.g. the
        bond/transfer calculator that innerText leaves out): the harder case."""
        key = 'tc:' + name
        if key not in self._text_cache:
            page = self.with_fixture(name)
            self._text_cache[key] = self.run(page.evaluate('() => document.body.textContent'))
        return self._text_cache[key]

    def close(self):
        self.run(self._browser.close())
        self.run(self._pw.stop())
        self.loop.close()


@pytest.fixture(scope='session')
def browser():
    b = Browser()
    yield b
    b.close()
