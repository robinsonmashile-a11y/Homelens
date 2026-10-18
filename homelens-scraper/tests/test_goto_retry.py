import asyncio

import pytest

from scraper import utils
from scraper.utils import goto_with_retry


class FakePage:
    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = 0

    async def goto(self, url, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TimeoutError('slow')


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def fast(_):
        return None
    monkeypatch.setattr(asyncio, 'sleep', fast)


def test_retries_then_succeeds():
    page = FakePage(fail_times=2)
    asyncio.run(goto_with_retry(page, 'http://x'))
    assert page.calls == 3


def test_raises_after_all_attempts():
    page = FakePage(fail_times=5)
    with pytest.raises(TimeoutError):
        asyncio.run(goto_with_retry(page, 'http://x'))
    assert page.calls == 3
