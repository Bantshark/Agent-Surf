import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_surf import browser, sites
from agent_surf.browser import BrowserError, BrowserSession, ReadOnlyPage, ResponseBuffer

SITE = sites.Site("feedtest", ("feed.test",), {"home": "https://feed.test/"})


class FakeRequest:
    method = "GET"


class FakeResponse:
    def __init__(self, ctype, body=None, url="https://feed.test/api", raise_json=False, raise_headers=False):
        self._ctype, self._body, self.url = ctype, body, url
        self._raise_json, self._raise_headers = raise_json, raise_headers
        self.request, self.status = FakeRequest(), 200

    @property
    def headers(self):
        if self._raise_headers:
            raise RuntimeError("target closed")
        return {"content-type": self._ctype}

    def json(self):
        if self._raise_json:
            raise ValueError("bad json")
        return self._body


def test_buffer_keeps_json_only():
    buf = ResponseBuffer()
    buf.on_response(FakeResponse("application/json; charset=utf-8", {"a": 1}))
    buf.on_response(FakeResponse("text/html", "<html>"))
    buf.on_response(FakeResponse("application/json", raise_json=True))
    buf.on_response(FakeResponse("application/json", raise_headers=True))
    assert [(r.url, r.method, r.status, r.data) for r in buf.all()] == [
        ("https://feed.test/api", "GET", 200, {"a": 1})]


def test_buffer_bounded_and_since():
    buf = ResponseBuffer(maxlen=3)
    for i in range(5):
        buf.add(f"u{i}", "GET", 200, i)
    assert [r.data for r in buf.all()] == [2, 3, 4]
    assert [r.data for r in buf.since(3)] == [3, 4]
    assert buf.last_seq == 5


def test_chrome_command_uses_dedicated_profile(tmp_path):
    cmd = browser.chrome_command(tmp_path, "http://127.0.0.1:9222")
    assert "--remote-debugging-port=9222" in cmd
    assert f"--user-data-dir=\"{tmp_path / 'chrome-profile'}\"" in cmd
    text = browser.chrome_instructions(tmp_path, "http://127.0.0.1:9222")
    assert "full control" in text


class FakePage:
    def __init__(self, url="about:blank"):
        self.url = url
        self.gotos = []
        self.redirect_to = None

    def on(self, event, cb):
        pass

    def goto(self, url, **kw):
        self.gotos.append(url)
        self.url = self.redirect_to or url


def test_goto_refuses_off_site_without_navigating():
    fp = FakePage()
    page = ReadOnlyPage(fp, SITE)
    with pytest.raises(sites.DomainRefused):
        page.goto("https://evil.test/")
    assert fp.gotos == []
    page.goto("https://feed.test/a")
    assert fp.gotos == ["https://feed.test/a"]


def test_goto_detects_off_site_redirect():
    fp = FakePage()
    fp.redirect_to = "https://login.other.test/"
    with pytest.raises(sites.DomainRefused):
        ReadOnlyPage(fp, SITE).goto("https://feed.test/a")


def test_read_only_surface():
    public = {n for n in dir(ReadOnlyPage) if not n.startswith("_")}
    assert public == {"goto", "scroll", "wait", "expand", "check_domain", "url", "title",
                      "frame_urls", "aria_snapshot", "dom_items", "close"}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_connect_failure_is_clear():
    with pytest.raises(BrowserError, match="agent-surf chrome"):
        with BrowserSession(f"http://127.0.0.1:{_free_port()}"):
            pass


def _chromium_path():
    """Browser for the CDP tests. AGENT_SURF_TEST_CHROME (an existing file, e.g.
    Brave or Edge) overrides Playwright's bundled Chromium, which may not launch
    on some Windows machines (WinError 14001)."""
    override = os.environ.get("AGENT_SURF_TEST_CHROME")
    if override and os.path.isfile(override):
        return override
    # In a child process: a bare start/stop of Playwright prints asyncio noise.
    code = "from playwright.sync_api import sync_playwright\n" \
           "with sync_playwright() as p: print(p.chromium.executable_path)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def test_test_chrome_override(monkeypatch, tmp_path):
    fake = tmp_path / "brave.exe"
    fake.write_text("")
    monkeypatch.setenv("AGENT_SURF_TEST_CHROME", str(fake))
    assert _chromium_path() == str(fake)
    monkeypatch.setenv("AGENT_SURF_TEST_CHROME", str(tmp_path / "missing.exe"))
    assert _chromium_path() != str(tmp_path / "missing.exe") and os.path.isfile(_chromium_path())
    monkeypatch.delenv("AGENT_SURF_TEST_CHROME")
    assert os.path.isfile(_chromium_path())


def test_cdp_session_and_dom_extraction(tmp_path):
    port = _free_port()
    proc = subprocess.Popen([
        _chromium_path(), "--headless=new", f"--remote-debugging-port={port}",
        f"--user-data-dir={tmp_path / 'profile'}", "--no-first-run", "--no-sandbox", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.2)
        with BrowserSession(f"http://127.0.0.1:{port}") as session:
            page = session.new_page(SITE)
            page._page.set_content(
                "<main><article data-id='a1'><p class='t'>Hello</p></article>"
                "<article data-id='a2'><p class='t'> World </p></article>"
                "<article><span>no text</span></article></main>")
            items = page.dom_items({"item": "article", "id_attr": "data-id", "fields": {"text": ".t"}})
            assert items == [
                {"item_id": "a1", "fields": {"text": "Hello"}},
                {"item_id": "a2", "fields": {"text": "World"}},
                {"item_id": None, "fields": {"text": None}},
            ]
            assert page.dom_items({"item": "<<bad", "id_attr": "x", "fields": {"t": "p"}}) == []
            assert "article" in page.aria_snapshot()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
