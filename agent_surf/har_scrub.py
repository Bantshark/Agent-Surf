"""Strip cookies, auth material and response bodies from HAR files.

Removes Cookie, Set-Cookie, Authorization, x-csrf-token, x-guest-token and any
header whose name matches (?i)token|auth|session|csrf, from requests and
responses; drops ``cookies`` arrays; redacts query parameters whose name
matches the same pattern. Request bodies (``postData``) are redacted by key:
matching form params and JSON keys (the pattern above plus PII names such as
email, phone, password); a body that cannot be parsed but mentions a matching
name is replaced wholesale.

Fix 23: response bodies are dropped by default: every ``content.text`` becomes
"REDACTED (<n> bytes, <mime>)" and ``content.encoding`` is removed, as is the
data of every WebSocket message. ``keep_bodies=True`` keeps JSON and text
bodies with the same key redaction (binary bodies are still dropped). A
scrubbed HAR still shows page structure and URLs: commit one only if it is
synthetic. Values are never printed.
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
PII_RE = re.compile(r"(?i)email|phone|password|birth|address|dob|ssn")
REDACTED = "REDACTED"
TEXT_MIME = re.compile(r"(?i)json|^text/|javascript|xml|x-www-form-urlencoded")


def _key_sensitive(name: str) -> bool:
    """Body keys: credentials and personal data."""
    return bool(SENSITIVE_RE.search(name) or PII_RE.search(name))


_KEY_WORDS = r"token|auth|session|csrf|email|phone|password|birth|address|dob|ssn"
# key: value / key=value / "key": "value" in HTML, inline scripts or plain text
_TEXT_PAIR = re.compile(r"(?i)([\w-]*(?:" + _KEY_WORDS + r")[\w-]*)([\"']?\s*[:=]\s*[\"']?)([^\"'&\s<>,;}]+)")
# <input name="csrf_token" value="..."> / <meta name="session" content="...">
_ATTR_PAIR = re.compile(r"(?i)((?:name|id|property)\s*=\s*[\"'][^\"']*(?:" + _KEY_WORDS
                        + r")[^\"']*[\"'][^>]*?(?:value|content)\s*=\s*[\"'])([^\"']*)")


def _redact_text(text: str, stats: ScrubStats) -> str:
    """--keep-bodies for non-JSON text (HTML, scripts): redact values that follow
    a credential- or PII-named key; the rest of the text is kept."""
    def attr(m: re.Match) -> str:
        stats.params += 1
        return m.group(1) + REDACTED

    def pair(m: re.Match) -> str:
        stats.params += 1
        return m.group(1) + m.group(2) + REDACTED

    return _TEXT_PAIR.sub(pair, _ATTR_PAIR.sub(attr, text))


@dataclass
class ScrubStats:
    headers: int = 0
    cookies: int = 0
    params: int = 0
    bodies: int = 0


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


def _redact_json(obj: Any, stats: ScrubStats) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _key_sensitive(str(k)):
                out[k] = REDACTED
                stats.params += 1
            else:
                out[k] = _redact_json(v, stats)
        return out
    if isinstance(obj, list):
        return [_redact_json(v, stats) for v in obj]
    return obj


def _scrub_body(text: str, mime: str, stats: ScrubStats) -> str:
    mime = mime.lower()
    if "json" in mime:
        try:
            return json.dumps(_redact_json(json.loads(text), stats), ensure_ascii=False)
        except ValueError:
            pass
    elif "x-www-form-urlencoded" in mime:
        pairs = parse_qsl(text, keep_blank_values=True)
        if pairs:
            hits = sum(1 for k, _ in pairs if _key_sensitive(k))
            if not hits:
                return text
            stats.params += hits
            return urlencode([(k, REDACTED if _key_sensitive(k) else v) for k, v in pairs])
    if SENSITIVE_RE.search(text):
        stats.bodies += 1
        return REDACTED
    return text


def _scrub_post_data(post: dict, stats: ScrubStats) -> None:
    for p in post.get("params") or []:
        if isinstance(p, dict) and _key_sensitive(str(p.get("name", ""))):
            p["value"] = REDACTED
            stats.params += 1
    if isinstance(post.get("text"), str) and post["text"]:
        post["text"] = _scrub_body(post["text"], str(post.get("mimeType", "")), stats)


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
    if isinstance(msg.get("postData"), dict):
        _scrub_post_data(msg["postData"], stats)


def _dropped(n: int, mime: str) -> str:
    return f"{REDACTED} ({n} bytes, {mime or 'unknown'})"


def _scrub_content(content: dict, stats: ScrubStats, keep_bodies: bool) -> None:
    text = content.get("text")
    encoding = content.pop("encoding", None)
    if not isinstance(text, str) or not text:
        return
    mime = str(content.get("mimeType", ""))
    if keep_bodies and encoding is None and TEXT_MIME.search(mime):
        if "json" in mime.lower():
            try:
                content["text"] = json.dumps(_redact_json(json.loads(text), stats), ensure_ascii=False)
                return
            except ValueError:
                pass
        content["text"] = _redact_text(text, stats)
        return
    size = content.get("size")
    n = size if isinstance(size, int) and size >= 0 else len(text.encode("utf-8"))
    content["text"] = _dropped(n, mime)
    stats.bodies += 1


def _scrub_ws_messages(messages: list, stats: ScrubStats, keep_bodies: bool) -> None:
    for msg in messages:
        if not isinstance(msg, dict) or not isinstance(msg.get("data"), str):
            continue
        if keep_bodies and msg.get("opcode", 1) == 1:
            try:
                msg["data"] = json.dumps(_redact_json(json.loads(msg["data"]), stats), ensure_ascii=False)
            except ValueError:
                msg["data"] = _redact_text(msg["data"], stats)
        else:
            msg["data"] = _dropped(len(msg["data"].encode("utf-8")), "websocket")
            stats.bodies += 1


def scrub_har(har: dict, *, keep_bodies: bool = False) -> tuple[dict, ScrubStats]:
    out = copy.deepcopy(har)
    stats = ScrubStats()
    for entry in (out.get("log") or {}).get("entries") or []:
        for side in ("request", "response"):
            if isinstance(entry.get(side), dict):
                _scrub_message(entry[side], stats)
        content = (entry.get("response") or {}).get("content")
        if isinstance(content, dict):
            _scrub_content(content, stats, keep_bodies)
        if isinstance(entry.get("_webSocketMessages"), list):
            _scrub_ws_messages(entry["_webSocketMessages"], stats, keep_bodies)
    return out, stats


def scrub_file(src: str | Path, dst: str | Path, *, keep_bodies: bool = False) -> ScrubStats:
    har: Any = json.loads(Path(src).read_text(encoding="utf-8"))
    if not isinstance(har, dict) or "log" not in har:
        raise ValueError(f"{src} is not a HAR file")
    clean, stats = scrub_har(har, keep_bodies=keep_bodies)
    Path(dst).write_text(json.dumps(clean, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return stats
