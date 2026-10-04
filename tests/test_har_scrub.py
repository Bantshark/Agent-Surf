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

    # A clean fixture survives scrubbing unchanged and still replays.
    fixture = tmp_path / "feed.har"
    har_scrub.scrub_file("tests/fixtures/feed.har", fixture)
    assert json.loads(fixture.read_text()) == json.loads(open("tests/fixtures/feed.har").read())
    ctx = chromium.new_context()
    ctx.route_from_har(fixture, not_found="abort")
    page = ctx.new_page()
    page.goto("https://feed.test/")
    assert page.title() == "Feed Test"
    ctx.close()
