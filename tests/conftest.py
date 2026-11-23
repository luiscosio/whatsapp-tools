"""Keep the fixture suite independent of a browser or a linked account."""

import pytest

from wa import browser


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refuse():
        pytest.fail("A fixture test attempted to start Playwright")

    monkeypatch.setattr(browser, "sync_playwright", refuse)
