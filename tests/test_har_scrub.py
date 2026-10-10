import json

from agent_surf import har_scrub
from agent_surf.har_scrub import REDACTED, scrub_har

from test_scrub_guard import problems_in_har

SECRET = "SYNTHETIC-SECRET-VALUE"


def dirty_har():
    # Built in memory only; never written under tests/fixtures/.
    def h(name):
        return {"name": name, "value": SECRET}

    return {"log": {"version": "1.2", "entries": [{
        "request": {
            "method": "GET",
            "url": f"https://feed.test/api?q=ok&auth_token={SECRET}&sessionid={SECRET}",
            "headers": [h("Cookie"), h("Authorization"), h("X-Csrf-Token"), h("x-guest-token"),
                        h("X-Session-Id"), h("x-auth-hint"), {"name": "Accept", "value": "*/*"}],
            "cookies": [{"name": "sid", "value": SECRET}],
            "queryString": [{"name": "q", "value": "ok"}, {"name": "auth_token", "value": SECRET},
                            {"name": "sessionid", "value": SECRET}],
            "postData": {"mimeType": "application/json", "text": json.dumps({"token": SECRET}),
                         "params": [{"name": "csrf", "value": SECRET}]},
        },
        "response": {
            "status": 302,
            "headers": [h("Set-Cookie"), h("access-token"), {"name": "Content-Type", "value": "text/html"}],
            "cookies": [{"name": "sid", "value": SECRET}],
            "redirectURL": f"https://feed.test/next?csrf={SECRET}",
            "content": {"mimeType": "text/html", "text": "<p>ok</p>"},
        },
    }]}}


def test_scrub_removes_everything_sensitive():
    clean, stats = scrub_har(dirty_har())
    text = json.dumps(clean)
    assert SECRET not in text
    req, resp = clean["log"]["entries"][0]["request"], clean["log"]["entries"][0]["response"]
    assert [x["name"] for x in req["headers"]] == ["Accept"]
    assert [x["name"] for x in resp["headers"]] == ["Content-Type"]
    assert "cookies" not in req and "cookies" not in resp
    assert req["queryString"] == [{"name": "q", "value": "ok"}, {"name": "auth_token", "value": REDACTED},
                                  {"name": "sessionid", "value": REDACTED}]
    assert req["url"] == f"https://feed.test/api?q=ok&auth_token={REDACTED}&sessionid={REDACTED}"
    assert resp["redirectURL"] == f"https://feed.test/next?csrf={REDACTED}"
    assert (stats.headers, stats.cookies) == (8, 2)
    assert problems_in_har(clean) == []
    assert problems_in_har(dirty_har())  # the guard notices the dirty input


def test_scrub_request_bodies():
    def post(mime, text, params=None):
        har = {"log": {"entries": [{"request": {"method": "POST", "url": "https://feed.test/api",
                                                "headers": [], "postData": {"mimeType": mime, "text": text}}}]}}
        if params is not None:
            har["log"]["entries"][0]["request"]["postData"]["params"] = params
        clean, stats = scrub_har(har)
        return clean["log"]["entries"][0]["request"]["postData"], stats

    body, _ = post("application/json", json.dumps({"query": "q", "variables": {"csrf_token": SECRET, "n": 1}}))
    assert json.loads(body["text"]) == {"query": "q", "variables": {"csrf_token": REDACTED, "n": 1}}

    body, _ = post("application/x-www-form-urlencoded", f"q=ok&session={SECRET}",
                   [{"name": "q", "value": "ok"}, {"name": "session", "value": SECRET}])
    assert body["text"] == f"q=ok&session={REDACTED}"
    assert body["params"][1]["value"] == REDACTED

    body, stats = post("text/plain", f"x-auth={SECRET}")
    assert body["text"] == REDACTED and stats.bodies == 1

    body, _ = post("application/json", '{"query": "no secrets here"}')
    assert json.loads(body["text"]) == {"query": "no secrets here"}


def test_scrub_leaves_original_untouched():
    har = dirty_har()
    scrub_har(har)
    assert SECRET in json.dumps(har)


def test_scrub_file_roundtrip(tmp_path, chromium):
    src, dst = tmp_path / "in.har", tmp_path / "out.har"
    src.write_text(json.dumps(dirty_har()))
    har_scrub.scrub_file(src, dst)
    assert SECRET not in dst.read_text()

    # A synthetic fixture scrubbed with --keep-bodies still replays.
    fixture = tmp_path / "feed.har"
    har_scrub.scrub_file("tests/fixtures/feed.har", fixture, keep_bodies=True)
    ctx = chromium.new_context()
    ctx.route_from_har(fixture, not_found="abort")
    page = ctx.new_page()
    page.goto("https://feed.test/")
    assert page.title() == "Feed Test"
    ctx.close()


# Fix 23: response bodies are dropped unless --keep-bodies.

PII = {"email": "someone" + "@" + "example.invalid", "phone_number": "555 0100 000",
       "password": "hunter2-synthetic", "birthDate": "2000-01-01", "home_address": "1 Synthetic Way",
       "dob": "2000-01-01", "ssn": "000-00-0000"}


def body_har():
    user = dict(PII, name="Alice", session_id="SYNTHETIC-SESSION", posts=[{"id": "1", "text": "hi"}])
    return {"log": {"version": "1.2", "entries": [
        {"request": {"method": "GET", "url": "https://feed.test/api/user", "headers": []},
         "response": {"status": 200, "headers": [],
                      "content": {"mimeType": "application/json", "size": 999, "text": json.dumps(user)}}},
        {"request": {"method": "GET", "url": "https://feed.test/", "headers": []},
         "response": {"status": 200, "headers": [],
                      "content": {"mimeType": "text/html", "text": "<p>Hello Alice</p>"}}},
        {"request": {"method": "GET", "url": "https://feed.test/a.png", "headers": []},
         "response": {"status": 200, "headers": [],
                      "content": {"mimeType": "image/png", "size": 4, "text": "iVBORw==", "encoding": "base64"}}},
        {"request": {"method": "GET", "url": "wss://feed.test/ws", "headers": []},
         "response": {"status": 101, "headers": [], "content": {"mimeType": "x-unknown", "size": 0}},
         "_webSocketMessages": [{"type": "receive", "opcode": 1, "data": json.dumps({"email": PII["email"],
                                                                                     "text": "hi"})}]},
    ]}}


def test_default_drops_every_response_body():
    clean, stats = scrub_har(body_har())
    entries = clean["log"]["entries"]
    assert entries[0]["response"]["content"]["text"] == "REDACTED (999 bytes, application/json)"
    assert entries[1]["response"]["content"]["text"] == "REDACTED (18 bytes, text/html)"
    png = entries[2]["response"]["content"]
    assert png["text"] == "REDACTED (4 bytes, image/png)" and "encoding" not in png
    assert entries[3]["_webSocketMessages"][0]["data"].startswith("REDACTED (")
    text = json.dumps(clean)
    for v in PII.values():
        assert v not in text
    assert "Alice" not in text and "SYNTHETIC-SESSION" not in text
    assert stats.bodies == 4


def test_keep_bodies_redacts_pii_and_credential_keys():
    clean, stats = scrub_har(body_har(), keep_bodies=True)
    entries = clean["log"]["entries"]
    user = json.loads(entries[0]["response"]["content"]["text"])
    for k in PII:
        assert user[k] == REDACTED, k
    assert user["session_id"] == REDACTED
    assert user["name"] == "Alice" and user["posts"] == [{"id": "1", "text": "hi"}]
    assert entries[1]["response"]["content"]["text"] == "<p>Hello Alice</p>"      # text kept
    assert entries[2]["response"]["content"]["text"] == "REDACTED (4 bytes, image/png)"  # binary dropped
    ws = json.loads(entries[3]["_webSocketMessages"][0]["data"])
    assert ws == {"email": REDACTED, "text": "hi"}
    text = json.dumps(clean)
    for v in PII.values():
        assert v not in text


def test_request_bodies_redact_pii_keys():
    har = {"log": {"entries": [{"request": {
        "method": "POST", "url": "https://feed.test/login", "headers": [],
        "postData": {"mimeType": "application/x-www-form-urlencoded", "text": "user=a&password=hunter2",
                     "params": [{"name": "user", "value": "a"}, {"name": "password", "value": "hunter2"}]}}}]}}
    clean, _ = scrub_har(har)
    post = clean["log"]["entries"][0]["request"]["postData"]
    assert post["text"] == f"user=a&password={REDACTED}" and post["params"][1]["value"] == REDACTED


def test_cli_keep_bodies_flag(tmp_path):
    from agent_surf import cli

    src, dst = tmp_path / "in.har", tmp_path / "out.har"
    src.write_text(json.dumps(body_har()), encoding="utf-8")
    assert cli.main(["scrub", str(src), str(dst)]) == 0
    assert "Alice" not in dst.read_text(encoding="utf-8")
    assert cli.main(["scrub", str(src), str(dst), "--keep-bodies"]) == 0
    out = dst.read_text(encoding="utf-8")
    assert "Alice" in out and PII["email"] not in out


def test_keep_bodies_redacts_values_in_html():
    html = ('<meta name="csrf-token" content="SYNTH-CSRF"><input type="hidden" name="authenticity_token" '
            'value="SYNTH-AUTH"><script>window.cfg = {"email": "x' + '@' + 'example.invalid", "theme": "dark"}'
            '</script><p class="post">kept text</p>')
    har = {"log": {"entries": [{"request": {"method": "GET", "url": "https://feed.test/", "headers": []},
                                "response": {"status": 200, "headers": [],
                                             "content": {"mimeType": "text/html", "text": html}}}]}}
    clean, _ = scrub_har(har, keep_bodies=True)
    out = clean["log"]["entries"][0]["response"]["content"]["text"]
    assert "SYNTH-CSRF" not in out and "SYNTH-AUTH" not in out and "example.invalid" not in out
    assert "kept text" in out and '"theme": "dark"' in out
