"""v2 Unit 1: attach modes and tab hygiene."""

import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

from agent_surf import config, sites
from agent_surf.browser import BrowserError, BrowserSession, cdp_endpoint, read_devtools_active_port

from test_browser import _chromium_path


def write_port_file(d, text):
    (d / "DevToolsActivePort").write_text(text)
    return d


def test_devtools_active_port_parsing(tmp_path):
    write_port_file(tmp_path, "9333\n/devtools/browser/0b1c-synthetic\n")
    assert read_devtools_active_port(tmp_path) == "ws://127.0.0.1:9333/devtools/browser/0b1c-synthetic"
    write_port_file(tmp_path, "  9334  \r\n/devtools/browser/x\r\n\r\n")  # Windows line endings
    assert read_devtools_active_port(tmp_path) == "ws://127.0.0.1:9334/devtools/browser/x"


@pytest.mark.parametrize("text", ["", "9333\n", "port\n/devtools/browser/x\n", "9333\ndevtools/browser/x\n",
                                  "0\n/devtools/browser/x\n", "70000\n/devtools/browser/x\n"])
def test_devtools_active_port_malformed(tmp_path, text):
    write_port_file(tmp_path, text)
    with pytest.raises(BrowserError):
        read_devtools_active_port(tmp_path)


def test_devtools_active_port_missing_is_clear(tmp_path):
    with pytest.raises(BrowserError, match="chrome://inspect"):
        read_devtools_active_port(tmp_path)


def test_cdp_endpoint_modes(tmp_path):
    assert cdp_endpoint(config.load({})) == "http://127.0.0.1:9222"
    with pytest.raises(BrowserError, match="AGENT_SURF_PROFILE_DIR"):
        cdp_endpoint(config.load({"AGENT_SURF_ATTACH": "devtools-active-port"}))
    write_port_file(tmp_path, "9444\n/devtools/browser/abc\n")
    cfg = config.load({"AGENT_SURF_ATTACH": "devtools-active-port", "AGENT_SURF_PROFILE_DIR": str(tmp_path)})
    assert cdp_endpoint(cfg) == "ws://127.0.0.1:9444/devtools/browser/abc"
    with pytest.raises(BrowserError, match="unknown"):
        cdp_endpoint(config.load({"AGENT_SURF_ATTACH": "magic"}))


USER_TAB = "data:text/html,<title>User tab</title><p>the user's own tab</p>"


@pytest.fixture
def chrome_port_zero(tmp_path):
    """Chromium started with --remote-debugging-port=0 writes DevToolsActivePort
    into its profile, the same file Chrome's chrome://inspect toggle writes."""
    profile = tmp_path / "profile"
    env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    proc = subprocess.Popen([
        _chromium_path(), "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={profile}",
        "--no-first-run", "--no-sandbox", "--no-proxy-server", USER_TAB,
    ], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 20
    while time.time() < deadline and not (profile / "DevToolsActivePort").exists():
        time.sleep(0.2)
    time.sleep(0.5)
    yield profile
    proc.terminate()
    proc.wait(timeout=10)


def tab_urls(endpoint):
    with BrowserSession(endpoint) as s:
        return [p.url for p in s.context.pages]


def test_devtools_mode_attaches_and_leaves_user_tabs_alone(chrome_port_zero):
    cfg = config.load({"AGENT_SURF_ATTACH": "devtools-active-port", "AGENT_SURF_PROFILE_DIR": str(chrome_port_zero)})
    endpoint = cdp_endpoint(cfg)
    assert endpoint.startswith("ws://127.0.0.1:")
    before = tab_urls(endpoint)
    assert USER_TAB in before

    site = sites.Site("blanktest", ("feed.test",), {})
    with BrowserSession(endpoint) as s:
        page = s.new_page(site)
        raw = s.open_tab()
        raw.set_content("<p>agent surf tab</p>")
        assert len(s.context.pages) == len(before) + 2
        assert page.url == "about:blank"
    assert tab_urls(endpoint) == before  # ours closed, the user's untouched
