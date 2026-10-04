"""End to end through the real CLI, the real CDP path and the real Anthropic SDK.

- Chromium is started as a separate process with a debugging port, exactly as
  `agent-surf chrome` tells a user to start Chrome, and the CLI attaches to it
  over CDP.
- https://feed.test is a local HTTPS server (throwaway self-signed cert made
  with openssl at test time) serving the synthetic feed.har content; Chromium
  maps feed.test to it with --host-resolver-rules. No live site is touched.
- The Messages API is a local fake reached through the real `anthropic` SDK via
  ANTHROPIC_BASE_URL, with a dummy key.
"""

import copy
import http.server
import json
import os
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from agent_surf import cli, sitemap
from agent_surf.store import Store

from test_browser import _chromium_path, _free_port
from test_runner import FEED_MAP, make_map

FIXTURES = Path(__file__).parent / "fixtures"
DUMMY_KEY = "test-dummy-key-not-a-secret"
MODEL = "claude-sonnet-5-5"


def serve(handler_cls, tls_dir=None):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    if tls_dir is not None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(tls_dir / "cert.pem", tls_dir / "key.pem")
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def har_site_handler(har_path):
    routes = {}
    for e in json.loads(har_path.read_text())["log"]["entries"]:
        u = urlsplit(e["request"]["url"])
        routes[u.path + ("?" + u.query if u.query else "")] = e["response"]

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            resp = routes.get(self.path)
            if resp is None:
                self.send_error(404)
                return
            body = resp["content"]["text"].encode()
            self.send_response(resp["status"])
            self.send_header("Content-Type", resp["content"]["mimeType"])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    return Handler


def fake_messages_api(replies, requests):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"path": self.path, "key_ok": self.headers.get("x-api-key") == DUMMY_KEY,
                             "body": body})
            out = json.dumps({
                "id": f"msg_{len(requests)}", "type": "message", "role": "assistant",
                "model": body["model"], "content": [{"type": "text", "text": replies.pop(0)}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    return Handler


@pytest.fixture
def chrome_on_feed(tmp_path):
    tls = tmp_path / "tls"
    tls.mkdir()
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-keyout", str(tls / "key.pem"), "-out", str(tls / "cert.pem"),
                    "-subj", "/CN=feed.test", "-addext", "subjectAltName=DNS:feed.test"],
                   check=True, capture_output=True)
    site = serve(har_site_handler(FIXTURES / "feed.har"), tls)
    cdp_port = _free_port()
    env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    proc = subprocess.Popen([
        _chromium_path(), "--headless=new", f"--remote-debugging-port={cdp_port}",
        f"--user-data-dir={tmp_path / 'chrome-profile'}", "--no-first-run", "--no-sandbox",
        "--no-proxy-server", "--ignore-certificate-errors",
        f"--host-resolver-rules=MAP feed.test:443 127.0.0.1:{site.server_address[1]}", "about:blank",
    ], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", cdp_port), timeout=0.5).close()
            break
        except OSError:
            time.sleep(0.2)
    yield f"http://127.0.0.1:{cdp_port}"
    proc.terminate()
    proc.wait(timeout=10)
    site.shutdown()


def model_reply():
    m = {k: v for k, v in copy.deepcopy(FEED_MAP).items() if k != "version"}
    m["scroll"] = {"max_scrolls": 15, "delay_s": 1.0, "stop_after_seen": 3}
    return "```json\n" + json.dumps(m) + "\n```"


def test_learn_run_delta_and_self_heal(chrome_on_feed, tmp_path, monkeypatch, capsys):
    replies, requests = [model_reply(), model_reply()], []
    api = serve(fake_messages_api(replies, requests))
    home = tmp_path / "home"
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AGENT_SURF_HOME", str(home))
    monkeypatch.setenv("AGENT_SURF_CDP_URL", chrome_on_feed)
    monkeypatch.setenv("AGENT_SURF_MODEL", MODEL)
    monkeypatch.setenv("ANTHROPIC_API_KEY", DUMMY_KEY)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{api.server_address[1]}")

    # learn: the real SDK sends the page to the (fake) API and the map is saved.
    assert cli.main(["learn", "feedtest", "home"]) == 0
    (req,) = requests
    assert req["path"] == "/v1/messages" and req["key_ok"]
    assert req["body"]["model"] == MODEL
    assert not {"temperature", "top_p", "top_k"} & set(req["body"])
    assert "Synthetic post 1" in req["body"]["messages"][0]["content"]
    assert "/api/feed?page=1" in req["body"]["messages"][0]["content"]
    capsys.readouterr()

    # run: zero model calls, all 15 items, then nothing new.
    assert cli.main(["run", "feedtest", "home", "--json"]) == 0
    items = json.loads(capsys.readouterr().out)
    assert [i["item_id"] for i in items] == [f"p{i}" for i in range(1, 16)]
    assert items[0]["text"] == "Synthetic post 1"
    assert cli.main(["run", "feedtest", "home", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert len(requests) == 1

    # A broken v2 map triggers exactly one relearn (v3), then the run continues.
    net = dict(FEED_MAP["network"], items_path="data.timeline[*]")
    broken = make_map(network=net, dom=None, version=2)
    with Store(home / "surf.db") as s:
        s.add_map("feedtest", "home", 2, sitemap.save_map(home / "maps", broken), None)
    assert cli.main(["run", "feedtest", "home", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert len(requests) == 2
    assert "stopped working" in requests[1]["body"]["messages"][0]["content"]
    with Store(home / "surf.db") as s:
        assert s.current_map("feedtest", "home")["version"] == 3

    assert cli.main(["maps", "list"]) == 0
    assert "feedtest\thome\tv3" in capsys.readouterr().out
    api.shutdown()
