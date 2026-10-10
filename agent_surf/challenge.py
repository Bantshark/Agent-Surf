"""CAPTCHA / checkpoint detection, one human ping, then wait.

Agent Surf never interacts with a challenge. It detects one, tells the human
once (Telegram if configured, always stderr), polls every 5 s until the
challenge is gone, and gives up after 10 minutes.

Fix 22: a login wall (sites.LOGGED_OUT markers) is handled the same way but
separately: it is never a broken map, never relearned or self-healed, and never
shown to the model. The human logs in in the Agent Surf browser window; the
page that was being opened is opened again; after 10 minutes LoggedOutTimeout
(CLI exit 7).
"""

from __future__ import annotations

import logging
import re
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping

from agent_surf import config, sites

log = logging.getLogger("agent_surf.challenge")

POLL_S = 5.0
TIMEOUT_S = 600.0
PATH_RE = re.compile(r"/(challenge|checkpoint|captcha)", re.I)
TITLE_MARKER = "just a moment"
IFRAME_MARKERS = ("challenges.cloudflare.com", "recaptcha", "hcaptcha")
TELEGRAM_API = "https://api.telegram.org"


class ChallengeTimeout(RuntimeError):
    pass


class LoggedOut(RuntimeError):
    """The page is a login wall. Raised where no wait is possible."""

    def __init__(self, site: str, reason: str, message: str | None = None):
        super().__init__(message or f"{site}: logged out ({reason}); log in to {site} in the "
                                    "Agent Surf browser window and run again")
        self.site = site
        self.reason = reason


class LoggedOutTimeout(LoggedOut):
    pass


def _site_name(page: Any) -> str:
    site = getattr(page, "site", None)
    return getattr(site, "name", site) if site is not None else ""


def detect_logged_out(site: str, url: str, title: str) -> str | None:
    """Return a reason if the page is the site's login wall, else None."""
    markers = sites.logged_out_markers(site)
    try:
        path = urllib.parse.urlsplit(url or "").path or "/"
    except ValueError:
        path = "/"
    path = path.lower()
    for p in markers.paths:
        if path == p or path.rstrip("/") == p or path.startswith(p.rstrip("/") + "/"):
            return f"URL path is {p}"
    low = (title or "").strip().lower()
    for t in markers.titles:
        if low.startswith(t):
            return f"page title starts with {t!r}"
    return None


def logged_out_page(page: Any) -> str | None:
    try:
        return detect_logged_out(_site_name(page), page.url, page.title())
    except Exception:  # a closed or navigating page is not a login wall
        return None


def ensure_logged_in(page: Any) -> None:
    """Raise LoggedOut (no waiting) if the page is a login wall. Used where a
    map would otherwise be called broken or a page sent to the model."""
    reason = logged_out_page(page)
    if reason:
        raise LoggedOut(_site_name(page), reason)


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
    if logged_out_page(page):  # e.g. LinkedIn's /checkpoint/lg is a login page
        return None
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


def wait_until_logged_in(page: Any, *, notify: Callable[[str], None], poll_s: float = POLL_S,
                         timeout_s: float = TIMEOUT_S, sleep: Callable[[float], None] | None = None,
                         clock: Callable[[], float] = time.monotonic) -> bool:
    """If the page is a login wall: notify once, poll until it is gone, then
    open the page that was being opened again (``page.last_url``). Returns True
    if it waited. Never touches the login form."""
    reason = logged_out_page(page)
    if not reason:
        return False
    site = _site_name(page) or "the site"
    sleep = sleep or page.wait
    notify(f"log in to {site} in the Agent Surf browser window ({reason}); the run continues "
           f"once you are logged in (gives up after {int(timeout_s // 60)} min).")
    deadline = clock() + timeout_s
    while True:
        sleep(poll_s)
        if not logged_out_page(page):
            log.info("logged in to %s; continuing", site)
            target = getattr(page, "last_url", None)
            if target and page.url != target:
                page.goto(target)
            return True
        if clock() >= deadline:
            raise LoggedOutTimeout(site, reason, f"{site}: still logged out after {int(timeout_s)} s "
                                                 f"({reason}); log in and run again")


class Guard:
    """Called on every page check: waits out a login wall, then a challenge.
    ``wait_logged_in`` lets callers that cannot continue in place (the
    publisher) wait and start over."""

    def __init__(self, notify: Callable[[str], None], **kw: Any):
        self.notify = notify
        self.kw = kw

    def __call__(self, page: Any) -> None:
        wait_until_logged_in(page, notify=self.notify, **self.kw)
        wait_until_clear(page, notify=self.notify, **self.kw)

    def wait_logged_in(self, page: Any) -> bool:
        return wait_until_logged_in(page, notify=self.notify, **self.kw)


def make_guard(env: Mapping[str, str] | None = None, *, notify: Callable[[str], None] | None = None,
               **kw: Any) -> Guard:
    return Guard(notify or make_notifier(env), **kw)
