"""v2 action executor: replays an action map with plain code.

Publishing boundary, enforced here:
- The only text typed is the approved queue item's ``text``; the only files
  attached are its ``media``. Map values are placeholders, never content.
- Ops are a fixed whitelist. ``StepRunner`` (used for rehearsal) has no submit
  at all and blocks the map's create endpoint; only ``Submitter`` can submit,
  and only through a target whose label is in both the map's and the code's
  per-action allowlist.
- Once text is in the composer, no step may press Enter or click anything
  labelled like a submit button, so nothing can publish outside ``submit``.
- Destructive-looking controls (delete, block, follow, like, ...) are never
  clicked. Navigation stays on the site's domains.
- The challenge guard runs before navigation, after every step and before
  submit. A challenge is never clicked.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from typing import Any, Callable

from agent_surf import actionmap, sitemap, sites
from agent_surf.actionmap import PRESS_KEYS, SUBMIT_LABELS, Resolved, candidates, try_resolve
from agent_surf.browser import ReadOnlyPage

log = logging.getLogger("agent_surf.executor")

STEP_TIMEOUT_S = 15.0     # wait for a step's target; covers a cold-start composer
CLICK_TIMEOUT_MS = 3000
TEXT_SETTLE_S = 2.0       # how long a text-entry method gets to take effect
SUBMIT_WORDS = frozenset(label.lower() for labels in SUBMIT_LABELS.values() for label in labels)
DESTRUCTIVE = re.compile(
    r"(?i)\b(delete|remove|block|report|mute|unfollow|follow|like|unlike|repost|retweet|undo|"
    r"leave|archive|unsubscribe|subscribe|deactivate|log ?out|sign ?out)\b")

Guard = Callable[[Any], None]


class Refused(RuntimeError):
    """A policy refusal: never retried, never self-healed."""


class StepFailed(RuntimeError):
    """A step could not be completed (target missing, text not accepted...).
    Raised only before submit, so nothing was published."""

    def __init__(self, index: int | str, reason: str):
        super().__init__(f"step {index}: {reason}")
        self.index = index
        self.reason = reason


def normalize_text(text: str) -> str:
    """Compare composer/permalink text with the payload: NFC, no emoji variation
    selectors or zero-width characters, whitespace collapsed."""
    text = unicodedata.normalize("NFC", text or "")
    text = re.sub("[︎️​‌‍⁠]", "", text).replace(" ", " ")
    return re.sub(r"\s+", " ", text).strip()


class ActionPage:
    """A tab Agent Surf opened for one action. Reads go through ``read`` (a
    ReadOnlyPage on the same tab, so the response buffer and challenge guard
    work as on the reading side); requests are recorded for rehearsal checks."""

    def __init__(self, raw: Any, site: sites.Site):
        self.raw = raw
        self.site = site
        self.read = ReadOnlyPage(raw, site)
        self.requests: list[tuple[str, str]] = []
        raw.on("request", self._on_request)

    def _on_request(self, request: Any) -> None:
        try:
            self.requests.append((request.method, request.url))
        except Exception:
            pass

    @property
    def buffer(self):
        return self.read.buffer

    def goto(self, url: str) -> None:
        self.read.goto(url)  # domain-locked before and after


def _key(target: dict) -> str:
    return json.dumps(target, sort_keys=True)


def _alive(handle: Any, hidden_ok: bool = False) -> bool:
    """Is a pinned element still on the page (and visible, unless hidden_ok)?
    A detached element, or one from a page that navigated away, is not."""
    if handle is None:
        return False
    try:
        return bool(handle.evaluate("e => e.isConnected")) if hidden_ok else handle.is_visible()
    except Exception:
        return False


def _contains(container: Any, el: Any) -> bool:
    try:
        return bool(container.evaluate("(c, el) => c.contains(el)", el))
    except Exception:
        return False


def _label(loc: Any) -> str:
    try:
        return (loc.evaluate(
            "e => (e.getAttribute('aria-label') || e.innerText || e.value || e.textContent || '')")
            or "").strip()
    except Exception:
        return ""


class StepRunner:
    """Runs a map's start, steps and discard. Structurally cannot submit: no
    submit method, and the map's create endpoint is blocked on this tab."""

    allows_submit = False

    def __init__(self, page: ActionPage, amap: dict, payload: dict, guard: Guard | None = None,
                 *, step_timeout_s: float | None = None):
        self.page = page
        self.raw = page.raw
        self.map = amap
        self.payload = payload
        self.guard = guard or (lambda p: None)
        self.step_timeout_s = STEP_TIMEOUT_S if step_timeout_s is None else step_timeout_s
        self.composing = False     # text is in the composer: no Enter, no submit-labelled clicks
        self.notes: list[str] = []
        # Elements this run actually used, pinned for the rest of the run, so a
        # look-alike elsewhere on the page (e.g. a timeline's inline composer
        # with the same role and name) is never typed into or mistaken for it.
        self.pins: dict[str, Any] = {}
        self.composer: Any = None   # pinned composer container (map "composer")
        if not self.allows_submit:
            net = (amap.get("confirm") or {}).get("network")
            if net and net.get("url_regex"):
                rx = re.compile(net["url_regex"])
                self.raw.route(lambda url: bool(rx.search(url)), lambda route: route.abort())

    # -- helpers ------------------------------------------------------------

    def _guard(self) -> None:
        self.guard(self.page.read)

    # -- composer container and pinned targets ------------------------------

    def _composer_target(self) -> dict | None:
        return (self.map.get("composer") or {}).get("target")

    def _text_target(self) -> dict | None:
        return next((s["target"] for s in self.map.get("steps", []) if s.get("op") == "type"), None)

    def _pick_container(self) -> Any:
        """A visible composer container that holds the map's text box (any
        visible one if the map types nothing). None if there is none yet."""
        text = self._text_target()
        for c in candidates(self.raw, self._composer_target()):
            handle = c.locator.element_handle()
            if text is None or any(_contains(handle, t.locator.element_handle())
                                   for t in candidates(self.raw, text)):
                self.notes.append(f"composer container pinned (via {c.how})")
                return handle
        return None

    def _container(self, where: Any, *, wait: bool = True) -> Any:
        """The pinned composer container. Composer steps never run without it."""
        if self.composer is not None:
            if _alive(self.composer):
                return self.composer
            raise StepFailed(where, "the composer container is gone")
        deadline = time.monotonic() + (self.step_timeout_s if wait else 0)
        while True:
            self.composer = self._pick_container()
            if self.composer is not None:
                return self.composer
            if time.monotonic() >= deadline:
                raise StepFailed(where, f"composer container not found: {self._composer_target()}")
            self.raw.wait_for_timeout(250)

    def _live_container(self) -> Any:
        """The container if it is (or can now be) pinned and alive; never raises."""
        if self.composer is None and self._composer_target():
            self.composer = self._pick_container()
        return self.composer if _alive(self.composer) else None

    def _find(self, target: dict, scope: str, where: Any, file_input: bool) -> Resolved | None:
        """One look, no waiting. scope "composer": only inside the container
        (required when the map has one); "auto": inside the container first,
        then page-wide (e.g. a confirm dialog outside the composer)."""
        if self._composer_target():
            container = self._container(where, wait=False) if scope == "composer" else self._live_container()
            if container is not None:
                for c in candidates(self.raw, target, allow_hidden_file_input=file_input):
                    if _contains(container, c.locator.element_handle()):
                        return Resolved(c.locator, c.how + " in composer")
            if scope == "composer":
                return None
        return try_resolve(self.raw, target, allow_hidden_file_input=file_input)

    def _resolve(self, target: dict, where: Any, *, scope: str = "auto", file_input: bool = False,
                 wait: bool = True) -> Resolved:
        """Resolve and pin. A pinned element is reused while it is alive."""
        key = _key(target)
        deadline = time.monotonic() + (self.step_timeout_s if wait else 0)
        if scope == "composer" and self._composer_target():
            self._container(where, wait=wait)  # waits for it; StepFailed if missing or gone
        while True:
            pin = self.pins.get(key)
            if _alive(pin, hidden_ok=file_input):
                return Resolved(pin, "pinned")
            r = self._find(target, scope, where, file_input)
            if r is not None:
                handle = r.locator.element_handle()
                if not self._composer_target() or r.how.endswith("in composer"):
                    self.pins[key] = handle
                return Resolved(handle, r.how)
            if time.monotonic() >= deadline:
                inside = " in the composer" if scope == "composer" and self._composer_target() else ""
                raise StepFailed(where, f"target not found{inside}: {target}")
            self.raw.wait_for_timeout(250)

    def _gone(self, target: dict) -> bool:
        """For "hidden" waits: the element this run used is detached or hidden,
        or the composer container is gone. A different element elsewhere on the
        page with the same role/name does not count."""
        comp = self._composer_target()
        if comp and self.composer is not None and not _alive(self.composer):
            return True
        if comp and _key(target) == _key(comp) and self.composer is not None:
            return not _alive(self.composer)
        pin = self.pins.get(_key(target))
        if pin is not None:
            return not _alive(pin)
        if comp and self.composer is not None:  # never used: look only inside the live container
            return all(not _contains(self.composer, c.locator.element_handle())
                       for c in candidates(self.raw, target))
        return try_resolve(self.raw, target) is None  # never used anywhere (legacy page-wide)

    def composer_closed(self) -> bool:
        """After discard: the composer this run used is closed."""
        if self._composer_target():
            return self.composer is not None and not _alive(self.composer)
        for target in (self._text_target(), self.map["submit"]["target"]):
            if target is not None and _key(target) in self.pins:
                return not _alive(self.pins[_key(target)])
        target = self._text_target() or self.map["submit"]["target"]
        return try_resolve(self.raw, target) is None

    def _check_click_allowed(self, loc: Any, where: Any) -> None:
        label = _label(loc)
        if DESTRUCTIVE.search(label):
            raise Refused(f"step {where}: refusing to click {label!r} (destructive control)")
        if self.composing and label.lower() in SUBMIT_WORDS:
            raise Refused(f"step {where}: refusing to click {label!r} outside submit "
                          "(it could publish)")

    def _dismiss_overlay(self, where: Any) -> None:
        dismiss = self.map.get("dismiss")
        if dismiss:
            for i, step in enumerate(dismiss):
                if step.get("op") == "press":
                    self._press(step, f"dismiss[{i}]")
                else:
                    r = try_resolve(self.raw, step["target"])
                    if r is not None:
                        self._check_click_allowed(r.locator, f"dismiss[{i}]")
                        r.locator.click(timeout=CLICK_TIMEOUT_MS)
        else:
            self.raw.keyboard.press("Escape")

    def _click(self, target: dict, where: Any) -> None:
        r = self._resolve(target, where)
        self._check_click_allowed(r.locator, where)
        try:
            r.locator.click(timeout=CLICK_TIMEOUT_MS)
            return
        except Exception as e:
            msg = str(e)
            if "intercepts pointer events" not in msg:
                raise StepFailed(where, f"click failed ({type(e).__name__})") from None
            cover = re.search(r"(<[^>]{0,120}>)[^\n]*intercepts pointer events", msg)
            self.notes.append(f"step {where}: click intercepted by "
                              f"{cover.group(1) if cover else 'an overlay'}; dismissed and retried")
        self._dismiss_overlay(where)
        r = self._resolve(target, where)
        self._check_click_allowed(r.locator, where)
        try:
            r.locator.click(timeout=CLICK_TIMEOUT_MS)
        except Exception as e:
            raise StepFailed(where, f"click still blocked after dismissing overlay "
                                    f"({type(e).__name__})") from None

    def _press(self, step: dict, where: Any) -> None:
        key = step.get("value")
        if key not in PRESS_KEYS:
            raise Refused(f"step {where}: press {key!r} is not allowed")
        if key == "Enter" and self.composing:
            raise Refused(f"step {where}: refusing Enter after text was entered (it could publish)")
        if step.get("target"):
            self._resolve(step["target"], where).locator.focus()
        self.raw.keyboard.press(key)

    def _submit_ready(self) -> bool:
        try:
            r = self._resolve(self.map["submit"]["target"], "submit", scope="composer", wait=False)
            return r.locator.is_enabled()
        except Exception:
            return False

    def _text_taken(self, loc: Any, text: str) -> bool:
        deadline = time.monotonic() + TEXT_SETTLE_S
        want = normalize_text(text)
        while True:
            try:
                shown = normalize_text(loc.inner_text())
            except Exception:
                shown = None
            if shown == want and self._submit_ready():
                return True
            if time.monotonic() >= deadline:
                return False
            self.raw.wait_for_timeout(100)

    def _clear(self, loc: Any) -> None:
        loc.focus()
        self.raw.keyboard.press("ControlOrMeta+A")
        self.raw.keyboard.press("Delete")

    def _type(self, step: dict, where: Any) -> None:
        text = self.payload.get("text")
        if not text:
            raise StepFailed(where, "payload has no text")
        loc = self._resolve(step["target"], where, scope="composer").locator
        self.composing = True
        methods = (
            ("fill", lambda: loc.fill(text)),
            ("keyboard.type", lambda: (self._clear(loc), self.raw.keyboard.type(text))),
            ("insertText", lambda: (self._clear(loc), self.raw.keyboard.insert_text(text))),
        )
        for name, enter in methods:
            try:
                enter()
            except Exception as e:
                log.debug("text entry via %s failed: %s", name, type(e).__name__)
                continue
            if self._text_taken(loc, text):
                self.notes.append(f"step {where}: text entered via {name}")
                return
        raise StepFailed(where, "the composer did not accept the text")

    def _attach(self, step: dict, where: Any) -> None:
        files = self.payload.get("media") or []
        if not files:
            self.notes.append(f"step {where}: no media in this item; attach skipped")
            return  # optional: nothing to attach
        r = self._resolve(step["target"], where, scope="composer", file_input=True)
        is_input = r.locator.evaluate("e => e.tagName === 'INPUT' && e.type === 'file'")
        try:
            if is_input:
                r.locator.set_input_files(files)
            else:
                self._check_click_allowed(r.locator, where)
                with self.raw.expect_file_chooser(timeout=self.step_timeout_s * 1000) as chooser:
                    r.locator.click(timeout=CLICK_TIMEOUT_MS)
                chooser.value.set_files(files)
        except (Refused, StepFailed):
            raise
        except Exception as e:
            raise StepFailed(where, f"attach failed ({type(e).__name__})") from None
        if step.get("preview"):
            self._resolve(step["preview"], f"{where}.preview")
        self.notes.append(f"step {where}: attached {len(files)} file(s)")

    def _navigate(self, step: dict, where: Any) -> None:
        key = (step.get("value") or "").strip("{}")
        if step.get("value") not in actionmap.URL_PLACEHOLDERS or not self.payload.get(key):
            raise Refused(f"step {where}: navigate needs {step.get('value')} from the queue item")
        url = self.payload[key]
        sites.check_url(self.page.site, url)
        self._guard()
        self.page.goto(url)

    def _wait_for(self, step: dict, where: Any) -> None:
        state = step.get("state", "visible")
        if state == "hidden":
            deadline = time.monotonic() + self.step_timeout_s
            while not self._gone(step["target"]):
                if time.monotonic() >= deadline:
                    raise StepFailed(where, f"still visible: {step['target']}")
                self.raw.wait_for_timeout(200)
            return
        loc = self._resolve(step["target"], where).locator
        if state == "enabled":
            deadline = time.monotonic() + self.step_timeout_s
            while not loc.is_enabled():
                if time.monotonic() >= deadline:
                    raise StepFailed(where, f"never enabled: {step['target']}")
                self.raw.wait_for_timeout(200)

    def _run_step(self, step: dict, where: Any, allowed: tuple) -> None:
        op = step.get("op")
        if op not in allowed:
            raise Refused(f"step {where}: op {op!r} is not allowed")
        if op == "navigate":
            self._navigate(step, where)
        elif op == "click":
            self._click(step["target"], where)
        elif op == "type":
            if step.get("value") != "{text}":
                raise Refused(f"step {where}: type only takes the queue item's text")
            self._type(step, where)
        elif op == "attach":
            if step.get("value") != "{media}":
                raise Refused(f"step {where}: attach only takes the queue item's media")
            self._attach(step, where)
        elif op == "press":
            self._press(step, where)
        elif op == "wait_for":
            self._wait_for(step, where)
        self._guard()

    # -- public -------------------------------------------------------------

    def start(self) -> None:
        start = self.map["start"]
        for need in start.get("requires", []):
            if not self.payload.get(need):
                raise Refused(f"the queue item has no {need}, which this action map requires")
        template = start["url_template"]
        url = self.payload[template.strip("{}")] if template in actionmap.URL_PLACEHOLDERS else template
        sites.check_url(self.page.site, url)
        self._guard()
        self.page.goto(url)
        self._guard()

    def run_steps(self) -> None:
        for i, step in enumerate(self.map["steps"]):
            self._run_step(step, i, actionmap.STEP_OPS)

    def discard(self) -> None:
        for i, step in enumerate(self.map["discard"]):
            self._run_step(step, f"discard[{i}]", actionmap.DISCARD_OPS)

    def submit_target_ok(self) -> str:
        """Check (without clicking) that the submit target resolves, is enabled
        and carries an allowed label; return the label."""
        return self._checked_submit()[1]

    def _checked_submit(self) -> tuple[Resolved, str]:
        sub = self.map["submit"]
        r = self._resolve(sub["target"], "submit", scope="composer")
        label = _label(r.locator)
        allowed = set(sub.get("label_allowlist") or ()) & set(SUBMIT_LABELS.get(self.map["action"], ()))
        if label not in allowed:
            raise Refused(f"submit label {label!r} is not in the allowlist {sorted(allowed)}")
        if not r.locator.is_enabled():
            raise StepFailed("submit", "submit is not enabled")
        return r, label


class Submitter(StepRunner):
    """The only executor that can submit."""

    allows_submit = True

    def submit(self) -> int:
        """Click the allowlisted submit target. Returns the response-buffer
        sequence number from just before the click (for receipts)."""
        self._guard()
        r, label = self._checked_submit()
        self.aria_before_submit = self.page.read.aria_snapshot()  # evidence: see page_changes()
        seq = self.page.buffer.last_seq
        try:
            r.locator.click(timeout=CLICK_TIMEOUT_MS, trial=True)  # actionable? no click yet
        except Exception as e:
            raise StepFailed("submit", f"submit is not clickable ({type(e).__name__})") from None
        try:
            r.locator.click(timeout=CLICK_TIMEOUT_MS)
        except Exception as e:  # the click may or may not have landed
            raise SubmitUnknown(f"submit click raised {type(e).__name__}") from None
        self.notes.append(f"submit: clicked {label!r}")
        return seq


    def page_changes(self, limit: int = 5) -> str:
        """What changed on the page since submit (aria delta), for evidence."""
        before = getattr(self, "aria_before_submit", None)
        if before is None:
            return "no snapshot"
        delta = sitemap.aria_delta(before, self.page.read.aria_snapshot())
        shown = "; ".join(n[-120:] for n in delta["added"][:limit])
        return f"+{len(delta['added'])}/-{len(delta['removed'])} nodes" + (f" (added: {shown})" if shown else "")


class SubmitUnknown(RuntimeError):
    """Submit was attempted and its outcome is unknown. Never retried blindly."""
