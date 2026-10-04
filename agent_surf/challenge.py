"""CAPTCHA / checkpoint detection, one human ping, then wait.

Agent Surf never interacts with a challenge. It detects one, tells the human
once (Telegram if configured, always stderr), polls every 5 s until the
challenge is gone, and gives up after 10 minutes.
"""

from __future__ import annotations

import logging
import re
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping

from agent_surf import config

log = logging.getLogger("agent_surf.challenge")

POLL_S = 5.0
TIMEOUT_S = 600.0
PATH_RE = re.compile(r"/(challenge|checkpoint|captcha)", re.I)
TITLE_MARKER = "just a moment"
IFRAME_MARKERS = ("challenges.cloudflare.com", "recaptcha", "hcaptcha")
TELEGRAM_API = "https://api.telegram.org"


class ChallengeTimeout(RuntimeError):
    pass


def detect(url: str, title: str, frame_urls: list[str]) -> str | None:
    """Return a reason string if the page looks like a challenge, else None."""
    try:
        path = urllib.parse.urlsplit(url or "").path
    except ValueError:
        path = ""
    m = PATH_RE.search(path)
    if m:
        return f"URL path contains {m.group(0)}"
    if TITLE_MARKER in (title or "").lower():
        return "page title is 'Just a moment'"
    for frame in frame_urls:
        low = (frame or "").lower()
        for marker in IFRAME_MARKERS:
            if marker in low:
                return f"{marker} iframe on page"
    return None


def detect_page(page: Any) -> str | None:
    return detect(page.url, page.title(), page.frame_urls())


def send_telegram(text: str, token: str, chat_id: str, *,
                  urlopen: Callable[..., Any] = urllib.request.urlopen) -> bool:
    """Bot API sendMessage. The token is only placed in the request URL; it is
    never logged, and errors are reported by type only."""
    req = urllib.request.Request(
        f"{TELEGRAM_API}/bot{token}/sendMessage",
        data=urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode(),
        method="POST",
    )
    try:
        with urlopen(req, timeout=10) as resp:
            ok = 200 <= getattr(resp, "status", 200) < 300
    except Exception as e:
        log.warning("Telegram notification failed (%s)", type(e).__name__)
        return False
    if not ok:
        log.warning("Telegram notification failed (HTTP %s)", getattr(resp, "status", "?"))
    return ok


def make_notifier(env: Mapping[str, str] | None = None, *,
                  urlopen: Callable[..., Any] = urllib.request.urlopen) -> Callable[[str], None]:
    creds = config.telegram_credentials(env)

    def notify(text: str) -> None:
        print(f"agent-surf: {text}", file=sys.stderr, flush=True)
        if creds:
            send_telegram(text, *creds, urlopen=urlopen)

    return notify


def wait_until_clear(page: Any, *, notify: Callable[[str], None], poll_s: float = POLL_S,
                     timeout_s: float = TIMEOUT_S, sleep: Callable[[float], None] | None = None,
                     clock: Callable[[], float] = time.monotonic) -> None:
    reason = detect_page(page)
    if not reason:
        return
    sleep = sleep or page.wait
    site = getattr(getattr(page, "site", None), "name", "the site")
    notify(f"challenge detected on {site} ({reason}). Solve it yourself in the Agent Surf "
           f"Chrome window; the run continues when it is gone (gives up after "
           f"{int(timeout_s // 60)} min).")
    deadline = clock() + timeout_s
    while True:
        sleep(poll_s)
        if not detect_page(page):
            log.info("challenge cleared; continuing")
            return
        if clock() >= deadline:
            raise ChallengeTimeout(f"challenge on {site} not cleared after {int(timeout_s)} s")


def make_guard(env: Mapping[str, str] | None = None, **kw: Any) -> Callable[[Any], None]:
    notify = make_notifier(env)

    def guard(page: Any) -> None:
        wait_until_clear(page, notify=notify, **kw)

    return guard
