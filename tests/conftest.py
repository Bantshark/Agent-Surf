"""Shared fixtures: an offline headless Chromium serving synthetic HARs.

Nothing here touches the network: every request is answered from a HAR under
tests/fixtures/ and anything not in the HAR is aborted.
"""

from pathlib import Path

import pytest

from agent_surf import sites
from agent_surf.browser import ReadOnlyPage
from agent_surf.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
FEED_SITE = sites.Site("feedtest", ("feed.test",), {"home": "https://feed.test/"})


@pytest.fixture(autouse=True)
def feed_site():
    sites.register_site(FEED_SITE)
    yield FEED_SITE
    sites.unregister_site(FEED_SITE.name)


@pytest.fixture(scope="module")
def chromium():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def har_page(chromium):
    """Factory: har_page("feed.har") -> ReadOnlyPage on the feedtest site."""
    contexts = []

    def make(har_name: str, site: sites.Site = FEED_SITE) -> ReadOnlyPage:
        ctx = chromium.new_context(viewport={"width": 1000, "height": 700})
        ctx.route_from_har(FIXTURES / har_name, not_found="abort")
        contexts.append(ctx)
        return ReadOnlyPage(ctx.new_page(), site)

    yield make
    for ctx in contexts:
        ctx.close()


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()
