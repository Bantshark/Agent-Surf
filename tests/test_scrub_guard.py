"""Scrub guard: nothing under tests/fixtures/ may carry cookies, auth headers or
token-shaped headers. This test must never be skipped."""

import json
import re
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
FORBIDDEN_WORDS = re.compile(r"(?i)cookie|authorization")
TOKENISH = re.compile(r"(?i)token|auth|session|csrf")
HEADER_LINE = re.compile(r"(?im)^\s*[\w-]*(token|auth|session|csrf)[\w-]*\s*:\s*\S")
JSON_HEADER = re.compile(r'(?i)"name"\s*:\s*"[^"]*(token|auth|session|csrf)[^"]*"')
BEARER = re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]{8,}")
# In a .har, a {"name": ..., "value": "REDACTED"} pair is what har_scrub leaves
# for a redacted query/body param. Only the token-shaped-name check skips these
# pairs; every other check still sees the full text, and problems_in_har still
# checks every header name and every param value structurally.
REDACTED_PAIR = re.compile(
    r'"name"\s*:\s*"[^"]*"\s*,\s*"value"\s*:\s*"REDACTED"'
    r'|"value"\s*:\s*"REDACTED"\s*,\s*"name"\s*:\s*"[^"]*"')


def problems_in_har(har):
    problems = []
    for i, entry in enumerate((har.get("log") or {}).get("entries") or []):
        for side in ("request", "response"):
            msg = entry.get(side) or {}
            for h in msg.get("headers") or []:
                name = str(h.get("name", ""))
                if name.lower() in {"cookie", "set-cookie", "authorization"} or TOKENISH.search(name):
                    problems.append(f"entry {i} {side} header {name}")
            if msg.get("cookies"):
                problems.append(f"entry {i} {side} has cookies")
            post = msg.get("postData") or {}
            for p in post.get("params") or []:
                if TOKENISH.search(str(p.get("name", ""))) and p.get("value") != "REDACTED":
                    problems.append(f"entry {i} {side} body param {p.get('name')}")
            for p in msg.get("queryString") or []:
                if TOKENISH.search(str(p.get("name", ""))) and p.get("value") != "REDACTED":
                    problems.append(f"entry {i} {side} query param {p.get('name')}")
    return problems


def problems_in_text(text, is_har=False):
    problems = []
    name_text = REDACTED_PAIR.sub("", text) if is_har else text
    for rx, label, body in ((FORBIDDEN_WORDS, "cookie/authorization", text),
                            (HEADER_LINE, "token-shaped header line", text),
                            (JSON_HEADER, "token-shaped header name", name_text),
                            (BEARER, "bearer token", text)):
        m = rx.search(body)
        if m:
            problems.append(f"{label}: {m.group(0)[:40]!r}")
    return problems


def test_fixtures_exist():
    files = [p for p in FIXTURES.rglob("*") if p.is_file()]
    assert files, "no fixtures found; the guard would pass vacuously"
    assert any(p.suffix == ".har" for p in files)


def guard_problems(text, is_har):
    """Everything the guard checks for one file's text."""
    problems = problems_in_text(text, is_har=is_har)
    if is_har:
        problems += problems_in_har(json.loads(text))
    return problems


def test_scrub_guard():
    failures = {}
    for path in sorted(p for p in FIXTURES.rglob("*") if p.is_file()):
        problems = guard_problems(path.read_text(errors="replace"), path.suffix == ".har")
        if problems:
            failures[str(path.relative_to(FIXTURES))] = problems
    assert failures == {}


# Guard behaviour on planted secrets. Built in memory only, never under fixtures/.
PLANTED = "PLANTED-SYNTHETIC-SECRET-0123456789"


def planted_har():
    return {"log": {"version": "1.2", "entries": [{
        "request": {
            "method": "POST",
            "url": f"https://feed.test/api?q=ok&auth_token={PLANTED}",
            "headers": [{"name": "Cookie", "value": f"sid={PLANTED}"},
                        {"name": "x-csrf-token", "value": PLANTED},
                        {"name": "Accept", "value": "*/*"}],
            "queryString": [{"name": "q", "value": "ok"}, {"name": "auth_token", "value": PLANTED}],
            "postData": {"mimeType": "text/plain", "text": f"Authorization: Bearer {PLANTED}"},
        },
        "response": {
            "status": 200,
            "headers": [{"name": "Content-Type", "value": "application/json"}],
            "cookies": [{"name": "sid", "value": PLANTED}],
            "content": {"mimeType": "application/json", "text": "{}"},
        },
    }]}}


def as_text(har):
    return json.dumps(har, indent=1)


def test_guard_flags_planted_secrets():
    problems = " | ".join(guard_problems(as_text(planted_har()), is_har=True))
    for expected in ("header Cookie", "header x-csrf-token", "query param auth_token",
                     "response has cookies", "bearer token", "cookie/authorization"):
        assert expected in problems, expected


def test_guard_passes_scrubbed_har():
    from agent_surf.har_scrub import scrub_har

    clean, _ = scrub_har(planted_har())
    text = as_text(clean)
    assert PLANTED not in text
    assert '"name": "auth_token"' in text and '"value": "REDACTED"' in text  # the case that used to fail
    assert guard_problems(text, is_har=True) == []


def test_guard_still_flags_token_name_with_real_value():
    from agent_surf.har_scrub import scrub_har

    clean, _ = scrub_har(planted_har())
    clean["log"]["entries"][0]["request"]["queryString"][1]["value"] = "not-redacted-value"
    problems = guard_problems(as_text(clean), is_har=True)
    assert any(p.startswith("token-shaped header name") for p in problems)
    assert any("query param auth_token" in p for p in problems)
    # A token-shaped header is flagged even if its value says REDACTED.
    clean["log"]["entries"][0]["request"]["headers"].append({"name": "x-csrf-token", "value": "REDACTED"})
    assert any("header x-csrf-token" in p for p in guard_problems(as_text(clean), is_har=True))
    # A name/value pair outside a .har is never excused.
    assert any(p.startswith("token-shaped header name")
               for p in problems_in_text('{"name": "auth_token", "value": "REDACTED"}'))

