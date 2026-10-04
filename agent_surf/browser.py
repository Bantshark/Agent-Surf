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

    def add(self, url: str, method: str, status: int, data: Any) -> None:
        seq = next(self._seq)
        self._items.append(CapturedResponse(seq, url, method, status, data))
        self.last_seq = seq

    def on_response(self, response: Any) -> None:
        """Response listener. Keeps JSON bodies; never raises."""
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


# Facts about elements matching a stored click selector. Pure read.
_CLICK_CANDIDATES_JS = """([sel, limit]) => {
  let nodes;
  try { nodes = document.querySelectorAll(sel); } catch (e) { return []; }
  const out = [];
  for (let i = 0; i < nodes.length && out.length < limit; i++) {
    const el = nodes[i];
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    const tag = el.tagName.toLowerCase();
    const link = el.closest('a[href]');
    out.push({
      index: i,
      tag: tag,
      type: (el.getAttribute('type') || '').toLowerCase(),
      text: (el.innerText || '').trim().slice(0, 200),
      label: (el.getAttribute('aria-label') || '').trim().slice(0, 200),
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
    if not names or any(len(n) > MAX_CLICK_TEXT for n in names):
        return False
    if any(CLICK_DENY.search(n) for n in names):
        return False
    return any(CLICK_ALLOW.search(n) for n in names)


class ReadOnlyPage:
    def __init__(self, page: Any, site: sites.Site, buffer: ResponseBuffer | None = None):
        self._page = page
        self.site = site
        self.buffer = buffer if buffer is not None else ResponseBuffer()
        page.on("response", self.buffer.on_response)

    # actions: navigate (domain-locked), scroll, wait

    def goto(self, url: str) -> None:
        sites.check_url(self.site, url)
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
# CDP session

class BrowserSession:
    """Attach to the user's running Chrome over CDP and use its default context."""

    def __init__(self, cdp_url: str):
        self.cdp_url = cdp_url
        self._pw = None
        self._pages: list[ReadOnlyPage] = []

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

    def new_page(self, site: sites.Site) -> ReadOnlyPage:
        page = ReadOnlyPage(self.context.new_page(), site)
        self._pages.append(page)
        return page

    def __exit__(self, *exc: object) -> None:
        for p in self._pages:
            p.close()
        if self._pw is not None:
            self._pw.stop()  # disconnects; the user's Chrome keeps running
