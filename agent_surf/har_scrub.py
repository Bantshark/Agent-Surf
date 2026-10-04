"""Strip cookies and auth material from HAR files before they are shared.

Removes Cookie, Set-Cookie, Authorization, x-csrf-token, x-guest-token and any
header whose name matches (?i)token|auth|session|csrf, from requests and
responses; drops ``cookies`` arrays; redacts query parameters whose name
matches the same pattern. Values are never printed.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SENSITIVE_HEADERS = {"cookie", "set-cookie", "authorization", "x-csrf-token", "x-guest-token"}
SENSITIVE_RE = re.compile(r"(?i)token|auth|session|csrf")
REDACTED = "REDACTED"


@dataclass
class ScrubStats:
    headers: int = 0
    cookies: int = 0
    params: int = 0


def is_sensitive(name: str) -> bool:
    return name.lower() in SENSITIVE_HEADERS or bool(SENSITIVE_RE.search(name))


def scrub_url(url: str, stats: ScrubStats | None = None) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    hits = sum(1 for k, _ in pairs if SENSITIVE_RE.search(k))
    if not hits:
        return url
    if stats:
        stats.params += hits
    query = urlencode([(k, REDACTED if SENSITIVE_RE.search(k) else v) for k, v in pairs])
    return urlunsplit(parts._replace(query=query))


def _scrub_message(msg: dict, stats: ScrubStats) -> None:
    headers = msg.get("headers")
    if isinstance(headers, list):
        kept = [h for h in headers if not is_sensitive(str(h.get("name", "")))]
        stats.headers += len(headers) - len(kept)
        msg["headers"] = kept
    if "cookies" in msg:
        cookies = msg.pop("cookies")
        stats.cookies += len(cookies) if isinstance(cookies, list) else 1
    if isinstance(msg.get("url"), str):
        msg["url"] = scrub_url(msg["url"], stats)
    if isinstance(msg.get("redirectURL"), str):
        msg["redirectURL"] = scrub_url(msg["redirectURL"], stats)
    qs = msg.get("queryString")
    if isinstance(qs, list):
        for p in qs:
            if SENSITIVE_RE.search(str(p.get("name", ""))):
                p["value"] = REDACTED
                stats.params += 1


def scrub_har(har: dict) -> tuple[dict, ScrubStats]:
    out = copy.deepcopy(har)
    stats = ScrubStats()
    for entry in (out.get("log") or {}).get("entries") or []:
        for side in ("request", "response"):
            if isinstance(entry.get(side), dict):
                _scrub_message(entry[side], stats)
    return out, stats


def scrub_file(src: str | Path, dst: str | Path) -> ScrubStats:
    har: Any = json.loads(Path(src).read_text())
    if not isinstance(har, dict) or "log" not in har:
        raise ValueError(f"{src} is not a HAR file")
    clean, stats = scrub_har(har)
    Path(dst).write_text(json.dumps(clean, indent=1, ensure_ascii=False) + "\n")
    return stats
