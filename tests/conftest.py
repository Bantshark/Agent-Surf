"""Shared fixtures: an offline headless Chromium serving synthetic HARs.

Nothing here touches the network: every request is answered from a HAR under
tests/fixtures/ and anything not in the HAR is aborted.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _use_system_packages():
    """The preinstalled pytest may live in an isolated venv that cannot see the
    pinned packages installed for the system python3. Append (never prepend)
    that site-packages dir so pytest's own modules still win, and export it
    for test subprocesses."""
    try:
        import playwright  # noqa: F401
        return
    except ImportError:
        pass
    py = shutil.which("python3")
    if not py:
        return
    out = subprocess.run(
        [py, "-c", "import os, playwright; print(os.path.dirname(os.path.dirname(playwright.__file__)))"],
        capture_output=True, text=True)
    path = out.stdout.strip()
    if out.returncode == 0 and path:
        sys.path.append(path)
        os.environ["PYTHONPATH"] = os.pathsep.join(
            p for p in (os.environ.get("PYTHONPATH"), path) if p)


_use_system_packages()

import pytest  # noqa: E402

from agent_surf import sites  # noqa: E402
from agent_surf.browser import ReadOnlyPage  # noqa: E402
from agent_surf.store import Store  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
FEED_SITE = sites.Site("feedtest", ("feed.test",), {
    "home": "https://feed.test/",
    "more": "https://feed.test/more",
    "late": "https://feed.test/late",
    "latedom": "https://feed.test/late-dom",
    "never": "https://feed.test/never",
    "latechallenge": "https://feed.test/late-challenge",
    "latesidebar": "https://feed.test/late-sidebar",
    "staticdivs": "https://feed.test/static-divs",
})


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
