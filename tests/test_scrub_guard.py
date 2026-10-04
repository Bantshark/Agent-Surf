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


def problems_in_text(text):
    problems = []
    for rx, label in ((FORBIDDEN_WORDS, "cookie/authorization"), (HEADER_LINE, "token-shaped header line"),
                      (JSON_HEADER, "token-shaped header name"), (BEARER, "bearer token")):
        m = rx.search(text)
        if m:
            problems.append(f"{label}: {m.group(0)[:40]!r}")
    return problems


def test_fixtures_exist():
    files = [p for p in FIXTURES.rglob("*") if p.is_file()]
    assert files, "no fixtures found; the guard would pass vacuously"
    assert any(p.suffix == ".har" for p in files)


def test_scrub_guard():
    failures = {}
    for path in sorted(p for p in FIXTURES.rglob("*") if p.is_file()):
        text = path.read_text(errors="replace")
        problems = problems_in_text(text)
        if path.suffix == ".har":
            problems += problems_in_har(json.loads(text))
        if problems:
            failures[str(path.relative_to(FIXTURES))] = problems
    assert failures == {}
