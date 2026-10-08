"""v2 Unit 8: WebSocket capture, websocket reading maps, inbox page types,
aria-snapshot delta."""

import json
import os
import socket
import subprocess
import time

import pytest

from agent_surf import inbox, learner, runner, sitemap, sites
from agent_surf.browser import BrowserSession, ResponseBuffer

from test_browser import _chromium_path, _free_port
from test_learner import FakeClient
from wskit import InboxServer, make_cert

WS_MAP = {
    "site": "wsinbox", "page_type": "messages", "version": 1, "source": "websocket",
    "websocket": {"url_regex": r"/socket/inbox", "items_path": "data.threads[*]", "id_path": "id",
                  "fields": {"from": "from", "snippet": "snippet"}},
    "dom": {"item": "li[data-thread-id]", "id_attr": "data-thread-id", "fields": {"snippet": "li"}},
    "required_fields": ["snippet"],
    "scroll": {"max_scrolls": 0, "delay_s": 1.0, "stop_after_seen": 5},
    "limits": {"max_items": 50},
}


@pytest.fixture(scope="module")
def ws_env(tmp_path_factory):
    """Local TLS inbox server + Chromium (feed.test -> 127.0.0.1) attached over CDP."""
    tmp = tmp_path_factory.mktemp("ws")
    make_cert(tmp)
    server = InboxServer(tmp)
    cdp = _free_port()
    env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    proc = subprocess.Popen([
        _chromium_path(), "--headless=new", f"--remote-debugging-port={cdp}", f"--user-data-dir={tmp / 'p'}",
        "--no-first-run", "--no-sandbox", "--no-proxy-server", "--ignore-certificate-errors",
        "--host-resolver-rules=MAP feed.test 127.0.0.1", "about:blank"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", cdp), timeout=0.5).close()
            break
        except OSError:
            time.sleep(0.2)
    site = sites.Site("wsinbox", ("feed.test",), {"messages": f"https://feed.test:{server.port}/inbox"})
    yield server, f"http://127.0.0.1:{cdp}", site
    proc.terminate()
    proc.wait(timeout=10)
    server.close()


@pytest.fixture
def ws_site(ws_env, monkeypatch):
    server, endpoint, site = ws_env
    sites.register_site(site)
    monkeypatch.setitem(sites.INBOX_PAGE_TYPES, "wsinbox", {"messages"})
    yield server, endpoint, site
    sites.unregister_site(site.name)


def test_websocket_frames_captured_str_and_bytes(ws_site):
    server, endpoint, site = ws_site
    with BrowserSession(endpoint) as s:
        page = s.new_page(site)
        page.goto(site.page_types["messages"])
        page.wait(1.5)
        frames = [r for r in page.buffer.all() if r.method == "WS"]
    assert [f.data["data"]["threads"][0]["id"] for f in frames] == ["m1", "m3"]  # ping and non-UTF-8 dropped
    assert all(f.url.startswith("wss://feed.test:") and f.url.endswith("/socket/inbox") for f in frames)


def test_buffer_frame_handling_unit():
    buf = ResponseBuffer()
    buf.on_frame("wss://x/s", '{"a": 1}')
    buf.on_frame("wss://x/s", b'{"b": 2}')
    buf.on_frame("wss://x/s", "not json")
    buf.on_frame("wss://x/s", b"\xff\xfe")
    buf.on_frame("wss://x/s", "42")  # JSON, but not an object or list
    assert [(r.method, r.status, r.data) for r in buf.all()] == [("WS", 101, {"a": 1}), ("WS", 101, {"b": 2})]
    assert buf.activity == 5


def test_websocket_map_extracts_and_delta(ws_site, store, tmp_path):
    server, endpoint, site = ws_site
    assert sitemap.validate_map(WS_MAP) == []
    store.add_map("wsinbox", "messages", 1, sitemap.save_map(tmp_path, WS_MAP), None)
    with BrowserSession(endpoint) as s:
        result = inbox.run_inbox(store, s.new_page(site), "wsinbox", "messages", client=None, model="m",
                                 maps_dir=tmp_path)
    assert result.source == "websocket"
    assert [i["item_id"] for i in result.items] == ["m1", "m2", "m3", "m4"]
    assert result.items[0]["snippet"] == "Synthetic hello \U0001f44b"
    with BrowserSession(endpoint) as s:
        again = inbox.run_inbox(store, s.new_page(site), "wsinbox", "messages", client=None, model="m",
                                maps_dir=tmp_path)
    assert again.items == []


def test_learner_sees_websocket_frames(ws_site, store, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "FIRST_PASS_TIMEOUT_S", 4.0)
    server, endpoint, site = ws_site
    reply = json.dumps({k: v for k, v in WS_MAP.items() if k != "version"})
    client = FakeClient(reply)
    with BrowserSession(endpoint) as s:
        m = learner.learn(store, s.new_page(site), "wsinbox", "messages", client=client, model="m",
                          maps_dir=tmp_path / "maps")
    assert m["source"] == "websocket"
    prompt = client.calls[0]["messages"][0]["content"]
    assert '"kind": "websocket frame"' in prompt and "/socket/inbox" in prompt
    assert "websocket" in client.calls[0]["system"]


def test_inbox_refuses_non_inbox_pages(store, tmp_path):
    with pytest.raises(ValueError, match="not an inbox page"):
        inbox.run_inbox(store, None, "x", "search", client=None, model="m", maps_dir=tmp_path)


def test_websocket_block_validation():
    bad = dict(WS_MAP, websocket=dict(WS_MAP["websocket"], url_regex="(oops"))
    assert any("websocket.url_regex" in p for p in sitemap.validate_map(bad))
    assert any("no websocket block" in p for p in sitemap.validate_map({k: v for k, v in WS_MAP.items()
                                                                         if k != "websocket"}))


def test_aria_delta_returns_only_changed_nodes():
    before = """- main:
  - heading "Compose" [level=1]
  - textbox "Post text"
  - button "Post" [disabled]
"""
    after = """- main:
  - heading "Compose" [level=1]
  - status: Your post was sent.
  - link "View post":
    - /url: /post/1000
"""
    d = sitemap.aria_delta(before, after)
    assert d["added"] == ["main > status: Your post was sent.", 'main > link "View post"',
                          'main > link "View post" > /url: /post/1000']
    assert d["removed"] == ['main > textbox "Post text"', 'main > button "Post" [disabled]']
    assert sitemap.aria_delta(before, before) == {"added": [], "removed": []}
