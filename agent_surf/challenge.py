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
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from agent_surf import config, sites

log = logging.getLogger("agent_surf.challenge")

POLL_S = 5.0
TIMEOUT_S = 600.0
PATH_RE = re.compile(r"/(challenge|checkpoint|captcha)", re.I)
TITLE_MARKER = "just a moment"
IFRAME_MARKERS = ("challenges.cloudflare.com", "recaptcha", "hcaptcha")
# Fix 36: sites load invisible reCAPTCHA (Enterprise) anchors on every page
# (Reddit 256x60 hidden, LinkedIn 0x0). A captcha iframe is a challenge only if
# its element is visible and at least this big in both directions ...
MIN_CHALLENGE_FRAME_PX = 100
# ... or if it is a frame that only ever shows a challenge (the reCAPTCHA image
# challenge "bframe", hCaptcha, Cloudflare) and is visible at any size ...
CHALLENGE_ONLY_FRAME_RE = re.compile(r"/recaptcha/.*bframe|hcaptcha|challenges\.cloudflare\.com", re.I)
# ... or if it is the visible reCAPTCHA checkbox ("I'm not a robot", 304x78, or
# 164x144 compact): it needs a human click, so it is treated as a challenge.
# The invisible badge (256x60, size=invisible) is not.
RECAPTCHA_ANCHOR_RE = re.compile(r"/recaptcha/.*anchor", re.I)
CHECKBOX_MIN_W, CHECKBOX_MIN_H = 150, 70


@dataclass(frozen=True)
class FrameInfo:
    """A frame as the challenge check sees it: its URL, whether its element is
    visible (CSS, opacity, on-screen) and its size in CSS px."""
    url: str
    visible: bool = True
    width: float = 0.0
    height: float = 0.0
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


def logged_in_reason(page: Any, snapshot: str | None = None) -> str | None:
    """Fix 34: a positive logged-in marker (account menu / avatar) on the page."""
    markers = sites.logged_in_markers(_site_name(page))
    if markers.roles:
        snap = page.aria_snapshot() if snapshot is None else snapshot
        for role, name in markers.roles:
            if re.search(rf'^\s*- {re.escape(role)} "{re.escape(name)}"', snap, re.M):
                return f'{role} "{name}" is on the page'
    for tid in markers.testids:
        if page.count(f'[data-testid="{tid}"]'):
            return f"{tid} is on the page"
    return None


def login_state(page: Any) -> tuple[str, str]:
    """("logged_out" | "logged_in" | "unknown", reason). Unknown means no login
    wall and no account marker: never reported as logged in."""
    reason = logged_out_page(page)
    if reason:
        return "logged_out", reason
    reason = logged_in_reason(page)
    if reason:
        return "logged_in", reason
    return "unknown", "no login wall and no account marker found"


def ensure_logged_in(page: Any) -> None:
    """Raise LoggedOut (no waiting) if the page is a login wall. Used where a
    map would otherwise be called broken or a page sent to the model."""
    reason = logged_out_page(page)
    if reason:
        raise LoggedOut(_site_name(page), reason)


def _page_reason(url: str, title: str) -> str | None:
    try:
        path = urllib.parse.urlsplit(url or "").path
    except ValueError:
        path = ""
    m = PATH_RE.search(path)
    if m:
        return f"URL path contains {m.group(0)}"
    if TITLE_MARKER in (title or "").lower():
        return "page title is 'Just a moment'"
    return None


def _marker(frame_url: str) -> str | None:
    low = (frame_url or "").lower()
    return next((m for m in IFRAME_MARKERS if m in low), None)


def detect(url: str, title: str, frame_urls: list[str]) -> str | None:
    """Return a reason string if the page looks like a challenge, else None.
    URL-only frame rule (every captcha frame counts); kept for tests and for
    pages that cannot report frame visibility. Callers use detect_frames."""
    reason = _page_reason(url, title)
    if reason:
        return reason
    for frame in frame_urls:
        marker = _marker(frame)
        if marker:
            return f"{marker} iframe on page"
    return None


def frame_reason(frame: FrameInfo) -> str | None:
    """Fix 36: why this frame is a challenge, or None. Invisible, zero-size and
    off-screen frames never are."""
    marker = _marker(frame.url)
    if not marker or not frame.visible or frame.width <= 0 or frame.height <= 0:
        return None
    size = f"{int(frame.width)}x{int(frame.height)}"
    if CHALLENGE_ONLY_FRAME_RE.search(frame.url):
        return f"visible {marker} challenge frame ({size})"
    if frame.width >= MIN_CHALLENGE_FRAME_PX and frame.height >= MIN_CHALLENGE_FRAME_PX:
        return f"visible {marker} iframe ({size})"
    if (RECAPTCHA_ANCHOR_RE.search(frame.url) and "size=invisible" not in frame.url.lower()
            and frame.width >= CHECKBOX_MIN_W and frame.height >= CHECKBOX_MIN_H):
        return f"visible reCAPTCHA checkbox ({size})"
    return None


def detect_frames(url: str, title: str, frames: list[FrameInfo]) -> str | None:
    """Fix 36: like detect, but a captcha frame counts only per frame_reason."""
    reason = _page_reason(url, title)
    if reason:
        return reason
    for frame in frames:
        reason = frame_reason(frame)
        if reason:
            return reason
    return None


def detect_page(page: Any) -> str | None:
    if logged_out_page(page):  # e.g. LinkedIn's /checkpoint/lg is a login page
        return None
    if hasattr(page, "frame_infos"):   # re-read on every call: visibility can change
        frames = [FrameInfo(**f) if isinstance(f, dict) else f for f in page.frame_infos()]
        return detect_frames(page.url, page.title(), frames)
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
    failure = None
    try:
        with urlopen(req, timeout=10) as resp:
            ok = 200 <= getattr(resp, "status", 200) < 300
    except Exception as e:
        failure = type(e).__name__   # the exception (its message may hold the URL) goes no further
    if failure is not None:
        # Logged outside the except block (Fix 35): a failing log handler prints
        # the exception being handled, and that one carries the token in its URL.
        log.warning("Telegram notification failed (%s)", failure)
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
