"""CDP connection to the user's Chrome, JSON response capture, read-only page.

Agent Surf never launches Chrome with the user's real profile and never
handles credentials. It attaches over CDP to a Chrome the user started with a
dedicated profile (see ``chrome_command``) and opens its own tab there.

``ReadOnlyPage`` is the only page interface the runner, learner and challenge
code receive. It can navigate (domain-locked), scroll, wait, read, and click
"show more" style controls named by a map's ``click`` selectors, but only
those that pass ``safe_to_click``. It has no typing or form methods.
"""

from __future__ import annotations

import itertools
import json
import logging
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agent_surf import sites

log = logging.getLogger("agent_surf.browser")

DEFAULT_BUFFER_SIZE = 500


class BrowserError(RuntimeError):
    pass


def chrome_command(home: Path, cdp_url: str) -> str:
    port = urlsplit(cdp_url).port or 9222
    return f'chrome --remote-debugging-port={port} --user-data-dir="{home / "chrome-profile"}"'


def chrome_instructions(home: Path, cdp_url: str) -> str:
    return "\n".join([
        "Start Chrome with a dedicated Agent Surf profile:",
        "",
        "  " + chrome_command(home, cdp_url),
        "",
        "Use the binary name for your platform (google-chrome, chromium, or the full",
        "path to Chrome). Chrome 136+ ignores the debug port on the default profile,",
        "which is why a dedicated --user-data-dir is required.",
        "Log in to the sites you want inside that window yourself.",
        "",
        "WARNING: while it runs, the debugging port gives full control of that",
        "profile (cookies, logged-in sessions) to any local process.",
    ])


# ---------------------------------------------------------------------------
# Response capture

@dataclass(frozen=True)
class CapturedResponse:
    seq: int
    url: str
    method: str
    status: int
    data: Any


class ResponseBuffer:
    """Bounded buffer of parsed JSON responses, fed by ``page.on("response")``."""

    def __init__(self, maxlen: int = DEFAULT_BUFFER_SIZE):
        self._items: deque[CapturedResponse] = deque(maxlen=maxlen)
        self._seq = itertools.count(1)
        self.last_seq = 0
        self.activity = 0  # responses of any type seen; lets callers tell a quiet page

    def add(self, url: str, method: str, status: int, data: Any) -> None:
        seq = next(self._seq)
        self._items.append(CapturedResponse(seq, url, method, status, data))
        self.last_seq = seq

    def on_response(self, response: Any) -> None:
        """Response listener. Keeps JSON bodies; never raises."""
        self.activity += 1
        try:
            ctype = (response.headers.get("content-type") or "").lower()
            if "json" not in ctype:
                return
            try:
                data = response.json()
            except Exception:
                return
            self.add(response.url, response.request.method, response.status, data)
        except Exception:  # a listener must never break the page
            return

    def on_websocket(self, ws: Any) -> None:
        """WebSocket listener: keep JSON frames from this socket; never raises."""
        try:
            url = ws.url
            ws.on("framereceived", lambda payload: self.on_frame(url, payload))
        except Exception:
            return

    def on_frame(self, url: str, payload: Any) -> None:
        """A received frame (str or bytes). JSON-parseable text is kept as a
        CapturedResponse with method "WS"; anything else is ignored."""
        self.activity += 1
        try:
            text = payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else payload
            data = json.loads(text)
        except Exception:
            return
        if isinstance(data, (dict, list)):
            self.add(url, "WS", 101, data)

    def all(self) -> list[CapturedResponse]:
        return list(self._items)

    def since(self, seq: int) -> list[CapturedResponse]:
        return [r for r in self._items if r.seq > seq]

    def __len__(self) -> int:
        return len(self._items)


# ---------------------------------------------------------------------------
# Read-only page

# Reads text/attributes only. Selectors are passed as arguments, never
# interpolated into code. An invalid selector yields an empty result.
_DOM_EXTRACT_JS = """([itemSel, idAttr, fields]) => {
  let nodes;
  try { nodes = document.querySelectorAll(itemSel); } catch (e) { return []; }
  const out = [];
  for (const el of nodes) {
    const row = {id: el.getAttribute(idAttr), fields: {}};
    for (const [name, sel] of Object.entries(fields)) {
      let f = null;
      try { f = el.querySelector(sel); } catch (e) { f = null; }
      row.fields[name] = f ? ((f.innerText || f.textContent || "").trim()) : null;
    }
    out.push(row);
  }
  return out;
}"""


# Fix 25: an element's accessible name, in order: aria-labelledby (the
# referenced elements' text, joined), aria-label, <label>s / alt / title,
# innerText, value (then descendant img alt, textContent). A text field is
# named by its labels, title or placeholder, never by what was typed into it.
# Returns the name and every non-empty source, so a denylist can check them
# all. Pure read.
ACCESSIBLE_NAME_FN = r"""function (e) {
  const t = s => (s == null ? '' : String(s)).replace(/\s+/g, ' ').trim();
  const sources = [];
  const add = s => { s = t(s); if (s) sources.push(s); return s; };
  let name = '';
  const ids = t(e.getAttribute('aria-labelledby')).split(' ').filter(Boolean);
  if (ids.length) {
    const root = e.getRootNode();
    const doc = e.ownerDocument;
    name = add(ids.map(id => {
      const r = (root && root.getElementById ? root.getElementById(id) : null) || doc.getElementById(id);
      return r ? t(r.getAttribute('aria-label') || r.innerText || r.textContent) : '';
    }).filter(Boolean).join(' '));
  }
  const aria = add(e.getAttribute('aria-label'));
  const labels = add(e.labels ? Array.from(e.labels).map(l => t(l.innerText || l.textContent)).join(' ') : '');
  const alt = add(e.getAttribute('alt'));
  const title = add(e.getAttribute('title'));
  const tag = e.tagName.toLowerCase();
  const field = e.isContentEditable || tag === 'textarea' || e.getAttribute('role') === 'textbox' ||
    (tag === 'input' && !['button', 'submit', 'reset', 'image'].includes((e.type || '').toLowerCase()));
  if (field) {
    name = name || aria || labels || alt || title || add(e.getAttribute('placeholder'));
    return {name: name.slice(0, 200), sources: sources.map(s => s.slice(0, 200))};
  }
  const inner = add(e.innerText);
  const value = add(typeof e.value === 'string' ? e.value : '');
  const imgs = add(Array.from(e.querySelectorAll('img[alt]')).map(i => i.getAttribute('alt')).join(' '));
  const text = add(e.textContent);
  name = name || aria || labels || alt || title || inner || value || imgs || text;
  return {name: name.slice(0, 200), sources: sources.map(s => s.slice(0, 200))};
}"""

# Facts about elements matching a stored click selector. Pure read.
_CLICK_CANDIDATES_JS = """([sel, limit]) => {
  const accName = """ + ACCESSIBLE_NAME_FN + """;
  let nodes;
  try { nodes = document.querySelectorAll(sel); } catch (e) { return []; }
  const out = [];
  for (let i = 0; i < nodes.length && out.length < limit; i++) {
    const el = nodes[i];
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    const tag = el.tagName.toLowerCase();
    const link = el.closest('a[href]');
    const acc = accName(el);
    out.push({
      index: i,
      tag: tag,
      type: (el.getAttribute('type') || '').toLowerCase(),
      text: (el.innerText || '').trim().slice(0, 200),
      label: acc.name,
      names: acc.sources,
      href: link ? link.getAttribute('href') : null,
      inForm: !!el.closest('form'),
      inDialog: !!el.closest('dialog,[role=dialog],[role=alertdialog],[aria-modal=true]'),
      editable: el.isContentEditable || ['input', 'textarea', 'select', 'option'].includes(tag),
      disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
      visible: r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none',
    });
  }
  return out;
}"""

CLICK_ALLOW = re.compile(r"(?i)\b(show|see|view|load|read|expand|more|older)\b")
CLICK_DENY = re.compile(
    r"(?i)\b(like|unlike|love|react|follow|unfollow|subscribe|unsubscribe|post|send|reply|"
    r"comment|repost|retweet|quote|share|join|connect|invite|message|buy|order|pay|donate|"
    r"vote|upvote|downvote|award|save|bookmark|report|block|mute|hide|delete|remove|edit|"
    r"sign|log|login|logout|register|submit|apply|accept|allow|confirm|install|download|"
    r"less|account|password|email|phone|verify|with|translate|open|app)\b")
MAX_CLICK_TEXT = 40
MAX_CLICKS_PER_PASS = 10
CANDIDATES_PER_SELECTOR = 20


def safe_to_click(c: dict) -> bool:
    """Whether a click candidate is a harmless "show more" style control."""
    if not c.get("visible") or c.get("disabled") or c.get("editable"):
        return False
    if c.get("inForm") or c.get("inDialog") or c.get("type") in ("submit", "reset", "file"):
        return False
    href = c.get("href")
    if href is not None and href.strip() not in ("", "#") and not href.strip().lower().startswith(
            ("#", "javascript:void")):
        return False  # real links navigate; v1 never follows them by clicking
    names = [n for n in (c.get("text") or "", c.get("label") or "") if n]
    every = names + [n for n in c.get("names") or [] if n]   # Fix 25: every name source
    if not names or any(len(n) > MAX_CLICK_TEXT for n in every):
        return False
    if any(CLICK_DENY.search(n) for n in every):
        return False
    return any(CLICK_ALLOW.search(n) for n in names)


class ReadOnlyPage:
    def __init__(self, page: Any, site: sites.Site, buffer: ResponseBuffer | None = None):
        self._page = page
        self.site = site
        self.buffer = buffer if buffer is not None else ResponseBuffer()
        self.last_url: str | None = None   # reopened after a login wall (challenge.wait_until_logged_in)
        page.on("response", self.buffer.on_response)
        page.on("websocket", self.buffer.on_websocket)

    # actions: navigate (domain-locked), scroll, wait

    def goto(self, url: str) -> None:
        sites.check_url(self.site, url)
        self.last_url = url
        self._page.goto(url, wait_until="domcontentloaded")
        self.check_domain()

    def scroll(self) -> None:
        height = self._page.evaluate("() => window.innerHeight") or 800
        self._page.mouse.wheel(0, int(height * 0.9))

    def wait(self, seconds: float) -> None:
        self._page.wait_for_timeout(seconds * 1000)

    def expand(self, selectors: list[str], max_clicks: int = MAX_CLICKS_PER_PASS) -> tuple[int, bool]:
        """Click safe "show more" controls matching stored selectors.

        Returns (clicks, navigated). Stops at the first click that changes the
        URL; raises DomainRefused if that took the tab off the site.
        """
        clicks = 0
        start_url = self._page.url
        for sel in selectors:
            try:
                candidates = self._page.evaluate(_CLICK_CANDIDATES_JS, [sel, CANDIDATES_PER_SELECTOR])
            except Exception as e:
                log.warning("click candidates failed: %s", type(e).__name__)
                continue
            for c in candidates:
                if clicks >= max_clicks:
                    return clicks, False
                if not safe_to_click(c):
                    continue
                loc = self._page.locator("css=" + sel).nth(c["index"])
                try:
                    if loc.inner_text(timeout=1000).strip()[:200] != c["text"]:
                        continue  # the DOM moved under us; re-check next pass
                    loc.click(timeout=2000)
                except Exception:
                    continue
                clicks += 1
                if self._page.url != start_url:
                    self.check_domain()
                    return clicks, True
        return clicks, False

    # reads

    def check_domain(self) -> None:
        """Raise DomainRefused if a redirect took the tab off the site."""
        sites.check_url(self.site, self._page.url)

    @property
    def url(self) -> str:
        return self._page.url

    def title(self) -> str:
        try:
            return self._page.title()
        except Exception:
            return ""

    def frame_urls(self) -> list[str]:
        """URLs of loaded frames plus src of every iframe element (even unloaded)."""
        urls = [f.url for f in self._page.frames]
        try:
            urls += self._page.evaluate(
                "() => Array.from(document.querySelectorAll('iframe'), f => f.src || '')")
        except Exception:
            pass
        return urls

    def aria_snapshot(self) -> str:
        try:
            return self._page.locator("body").aria_snapshot()
        except Exception as e:
            log.warning("aria snapshot failed: %s", type(e).__name__)
            return ""

    def dom_items(self, dom: dict) -> list[dict]:
        """Raw items ``{"item_id", "fields"}`` for a map's dom block."""
        from agent_surf.sitemap import normalize_id

        try:
            rows = self._page.evaluate(_DOM_EXTRACT_JS, [dom["item"], dom["id_attr"], dom["fields"]])
        except Exception as e:
            log.warning("DOM extraction failed: %s", type(e).__name__)
            return []
        return [{"item_id": normalize_id(r.get("id")), "fields": r.get("fields") or {}} for r in rows]

    def close(self) -> None:
        try:
            self._page.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Attach modes

def read_devtools_active_port(profile_dir: str | Path) -> str:
    """CDP websocket URL from a profile's DevToolsActivePort file (line 1: port,
    line 2: browser websocket path). Chrome writes it when remote debugging is on
    for that profile, e.g. via chrome://inspect/#remote-debugging (Chrome 144+)."""
    path = Path(profile_dir) / "DevToolsActivePort"
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise BrowserError(
            f"{path} not found: turn on remote debugging for this profile "
            "(chrome://inspect/#remote-debugging in Chrome 144+) or use AGENT_SURF_CDP_URL") from None
    except OSError as e:
        raise BrowserError(f"cannot read {path}: {type(e).__name__}") from None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2 or not lines[0].isdigit() or not lines[1].startswith("/"):
        raise BrowserError(f"{path} is malformed (expected a port line and a /devtools/... path line)")
    port = int(lines[0])
    if not 1 <= port <= 65535:
        raise BrowserError(f"{path} has an invalid port")
    return f"ws://127.0.0.1:{port}{lines[1]}"


LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def check_loopback(endpoint: str, allow_remote: bool = False) -> str:
    """Fix 14: CDP gives full control of the browser profile, so only a loopback
    endpoint is used unless AGENT_SURF_ALLOW_REMOTE_CDP=1."""
    host = (urlsplit(endpoint).hostname or "").lower()
    if host not in LOOPBACK_HOSTS and not allow_remote:
        raise BrowserError(f"refusing CDP endpoint on {host or endpoint!r}: only 127.0.0.1, ::1 or localhost "
                           "are allowed (set AGENT_SURF_ALLOW_REMOTE_CDP=1 to override)")
    return endpoint


def cdp_endpoint(cfg: Any) -> str:
    """Where to attach: AGENT_SURF_CDP_URL (default mode ``cdp``) or, in the
    experimental ``devtools-active-port`` mode, the profile's DevToolsActivePort.
    Loopback hosts only (both modes) unless AGENT_SURF_ALLOW_REMOTE_CDP=1."""
    allow = getattr(cfg, "allow_remote_cdp", False)
    if cfg.attach == "cdp":
        return check_loopback(cfg.cdp_url, allow)
    if cfg.attach == "devtools-active-port":
        if cfg.profile_dir is None:
            raise BrowserError("AGENT_SURF_ATTACH=devtools-active-port needs AGENT_SURF_PROFILE_DIR")
        return check_loopback(read_devtools_active_port(cfg.profile_dir), allow)
    raise BrowserError(f"unknown AGENT_SURF_ATTACH {cfg.attach!r}; use cdp or devtools-active-port")


# ---------------------------------------------------------------------------
# CDP session

class BrowserSession:
    """Attach to the user's running Chrome over CDP and use its default context.

    Tab hygiene: every page Agent Surf uses is a new tab it opened itself, and
    each is closed on exit. The user's existing tabs are never navigated, read
    or closed."""

    def __init__(self, cdp_url: str):
        import os
        self.cdp_url = check_loopback(cdp_url, os.environ.get("AGENT_SURF_ALLOW_REMOTE_CDP") == "1")
        self._pw = None
        self._tabs: list[Any] = []

    def __enter__(self) -> "BrowserSession":
        from playwright.sync_api import Error as PlaywrightError, sync_playwright

        self._pw = sync_playwright().start()
        try:
            self.browser = self._pw.chromium.connect_over_cdp(self.cdp_url)
        except PlaywrightError as e:
            self._pw.stop()
            raise BrowserError(
                f"cannot connect to Chrome at {self.cdp_url}; start it first (agent-surf chrome)"
            ) from e
        if not self.browser.contexts:
            self._pw.stop()
            raise BrowserError("connected Chrome has no default context")
        self.context = self.browser.contexts[0]
        return self

    def open_tab(self) -> Any:
        """A new tab owned by this session (closed on exit)."""
        tab = self.context.new_page()
        self._tabs.append(tab)
        return tab

    def new_page(self, site: sites.Site) -> ReadOnlyPage:
        return ReadOnlyPage(self.open_tab(), site)

    def __exit__(self, *exc: object) -> None:
        for tab in self._tabs:
            try:
                tab.close()
            except Exception:
                pass
        if self._pw is not None:
            self._pw.stop()  # disconnects; the user's Chrome keeps running
