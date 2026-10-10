"""v2 action maps: how to drive one composer (site, action), learned once.

An action map is data only. It names targets (role+name, testid or css) and a
fixed set of ops; it can never carry text to type or files to attach, only
the placeholders that the executor fills from an approved queue item.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_surf import sitemap, sites
from agent_surf.outbox import ACTIONS, PAYLOAD_KEYS
from agent_surf.store import Store, now_iso

# Final submit labels allowed per action, enforced in code. A map may narrow
# these, never widen them.
SUBMIT_LABELS = {
    "post": ("Post", "Tweet", "Share", "Publish"),
    "reply": ("Reply", "Post"),
    "dm": ("Send",),
    "comment": ("Comment", "Post", "Reply"),
}
# Fix 10: labels (accessible names) a map may click, checked in code before
# every click. Steps: controls that open, focus or expand the composer or attach
# media. Discard/dismiss steps (which may act outside the composer, e.g. on a
# "Discard post?" confirm): controls that close things without publishing.
# Compared case-insensitively with whitespace collapsed. "Delete" stays
# forbidden (executor.DESTRUCTIVE applies everywhere); "More options" is not
# allowed.
COMPOSER_CLICK_LABELS = (
    "Post text", "Add photos or video", "Add photo", "Add media", "Photo/video", "Media", "Image",
    "Start a post", "Create post", "Create", "New post", "Write a comment", "Add a comment",
    "Reply", "Comment", "Message", "Write a message", "Show more",
)
CLICK_LABELS = {action: COMPOSER_CLICK_LABELS for action in ("post", "reply", "comment", "dm")}
DISCARD_CLICK_LABELS = ("Discard", "Discard post", "Don't save", "Close", "Cancel", "Not now", "Dismiss",
                        "Got it", "OK")


def norm_label(label: str) -> str:
    return re.sub(r"\s+", " ", (label or "").replace("\u2019", "'")).strip().casefold()


def click_allowed(label: str, allowlist: tuple) -> bool:
    return bool(norm_label(label)) and norm_label(label) in {norm_label(a) for a in allowlist}


ALLOWED_KEYS = {"site", "action", "version", "start", "steps", "submit", "discard", "dismiss",
                "confirm", "permalink_template", "limits", "lookup", "learned_at", "learned_by",
                "composer"}
REQUIRED_KEYS = {"site", "action", "start", "steps", "submit", "discard", "confirm",
                 "permalink_template", "limits"}
STEP_KEYS = {"op", "target", "value", "state", "preview"}
TARGET_KEYS = {"role", "name", "testid", "css"}
STEP_OPS = ("navigate", "click", "type", "attach", "press", "wait_for")
DISCARD_OPS = ("click", "press", "wait_for")
ALL_OPS = STEP_OPS + ("submit", "discard")   # the whole whitelist; submit/discard have their own blocks
PRESS_KEYS = ("Enter", "Escape", "Tab")
STATES = ("visible", "enabled", "hidden")
PLACEHOLDERS = {"type": ("{text}",), "attach": ("{media}",), "navigate": ("{target_url}", "{thread_url}")}
URL_PLACEHOLDERS = ("{target_url}", "{thread_url}")
MAX_PER_DAY = 200
MIN_SPACING_S = 30
_NAME_RE = re.compile(r"^[a-z0-9_]+$")


class ActionMapError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Validation

def _check_target(t: Any, where: str, problems: list[str]) -> None:
    if not isinstance(t, dict) or not t:
        problems.append(f"{where}: target must be a non-empty object")
        return
    for k in sorted(set(t) - TARGET_KEYS):
        problems.append(f"{where}: unknown target key {k}")
    for k, v in t.items():
        if k in TARGET_KEYS and (not isinstance(v, str) or not v.strip()):
            problems.append(f"{where}.{k}: must be a non-empty string")
    if not any(isinstance(t.get(k), str) and t[k].strip() for k in ("role", "testid", "css")):
        problems.append(f"{where}: target needs role, testid or css")
    if "name" in t and "role" not in t:
        problems.append(f"{where}: name needs a role")


def _check_steps(steps: Any, where: str, ops: tuple, problems: list[str], needs: set) -> None:
    if not isinstance(steps, list) or not steps:
        problems.append(f"{where}: must be a non-empty list")
        return
    for i, step in enumerate(steps):
        w = f"{where}[{i}]"
        if not isinstance(step, dict):
            problems.append(f"{w}: must be an object")
            continue
        for k in sorted(set(step) - STEP_KEYS):
            problems.append(f"{w}: unknown key {k}")
        op = step.get("op")
        if op not in ops:
            problems.append(f"{w}: op {op!r} is not allowed here (allowed: {', '.join(ops)})")
            continue
        value = step.get("value")
        if op in PLACEHOLDERS:
            if value not in PLACEHOLDERS[op]:
                problems.append(f"{w}: {op} value must be one of {', '.join(PLACEHOLDERS[op])}")
            else:
                needs.add(value.strip("{}"))
        elif op == "press":
            if value not in PRESS_KEYS:
                problems.append(f"{w}: press value must be one of {', '.join(PRESS_KEYS)}")
        elif "value" in step:
            problems.append(f"{w}: {op} takes no value")
        if op == "navigate":
            if "target" in step:
                problems.append(f"{w}: navigate takes no target")
        elif op == "press":
            if "target" in step:
                _check_target(step["target"], w, problems)
        else:
            _check_target(step.get("target"), w, problems)
        if "state" in step and (op != "wait_for" or step["state"] not in STATES):
            problems.append(f"{w}: state must be one of {', '.join(STATES)} on wait_for")
        if "preview" in step:
            if op != "attach":
                problems.append(f"{w}: preview is only for attach")
            else:
                _check_target(step["preview"], f"{w}.preview", problems)


def _check_url(template: Any, site: sites.Site | None, where: str, problems: list[str],
               needs: set | None = None, placeholder: str | None = None) -> None:
    if not isinstance(template, str) or not template:
        problems.append(f"{where}: must be a non-empty string")
        return
    if needs is not None and template in URL_PLACEHOLDERS:
        needs.add(template.strip("{}"))  # checked against the site's domains at run time
        return
    probe = template.replace(placeholder, "123") if placeholder else template
    if "{" in probe or "}" in probe:
        problems.append(f"{where}: unexpected placeholder")
        return
    if site is not None and not sites.is_allowed_url(site, probe):
        problems.append(f"{where}: {template!r} is outside {', '.join(site.domains)}")


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def validate_action_map(m: Any) -> list[str]:
    """Problems with an action map; an empty list means valid."""
    if not isinstance(m, dict):
        return ["action map must be a JSON object"]
    problems: list[str] = []
    for k in sorted(set(m) - ALLOWED_KEYS):
        problems.append(f"unknown key {k}")
    for k in sorted(REQUIRED_KEYS - set(m)):
        problems.append(f"missing key {k}")

    site = None
    if "site" in m:
        if not (isinstance(m["site"], str) and _NAME_RE.match(m["site"])):
            problems.append("site: must match [a-z0-9_]+")
        else:
            site = sites.SITES.get(m["site"])
            if site is None:
                problems.append(f"site: unknown site {m['site']!r}")
    action = m.get("action")
    if "action" in m and action not in ACTIONS:
        problems.append(f"action: must be one of {', '.join(ACTIONS)}")
    if "version" in m and not (_is_int(m["version"]) and m["version"] >= 1):
        problems.append("version: must be a positive integer")
    for k in ("learned_at", "learned_by"):
        if k in m and not isinstance(m[k], str):
            problems.append(f"{k}: must be a string")

    needs: set = set()
    start = m.get("start")
    requires: list = []
    if "start" in m:
        if not isinstance(start, dict):
            problems.append("start: must be an object")
        else:
            for k in sorted(set(start) - {"url_template", "requires"}):
                problems.append(f"start: unknown key {k}")
            _check_url(start.get("url_template"), site, "start.url_template", problems, needs)
            requires = start.get("requires", [])
            if not isinstance(requires, list) or not all(r in PAYLOAD_KEYS for r in requires):
                problems.append(f"start.requires: must be a list of {', '.join(PAYLOAD_KEYS)}")
                requires = []
    if "composer" in m:
        comp = m["composer"]
        if not isinstance(comp, dict) or set(comp) != {"target"}:
            problems.append("composer: must be an object with only a target")
        else:
            _check_target(comp["target"], "composer", problems)
    if "steps" in m:
        _check_steps(m["steps"], "steps", STEP_OPS, problems, needs)
    if "discard" in m:
        _check_steps(m["discard"], "discard", DISCARD_OPS, problems, set())
    if "dismiss" in m:
        _check_steps(m["dismiss"], "dismiss", ("click", "press"), problems, set())
    for block, allow in (("steps", CLICK_LABELS.get(action, COMPOSER_CLICK_LABELS)),
                         ("discard", DISCARD_CLICK_LABELS), ("dismiss", DISCARD_CLICK_LABELS)):
        for i, step in enumerate(m.get(block) or []):
            if not isinstance(step, dict) or step.get("op") not in ("click", "attach"):
                continue
            name = (step.get("target") or {}).get("name") if isinstance(step.get("target"), dict) else None
            if isinstance(name, str) and not click_allowed(name, allow):
                problems.append(f"{block}[{i}]: may not click {name!r} (allowed: {', '.join(allow)})")
    for need in sorted(needs - {"media"} - set(requires)):
        problems.append(f"start.requires: must include {need} (used by the map)")
    if site is not None and action in ACTIONS and isinstance(m.get("steps"), list):
        has_attach = any(isinstance(s, dict) and s.get("op") == "attach" for s in m["steps"])
        if sites.media_required(site.name, action):
            if "media" not in requires:
                problems.append(f"start.requires: must include media ({site.name} {action} needs media)")
            if not has_attach:
                problems.append(f"steps: needs an attach step ({site.name} {action} needs media)")
        elif "media" in requires:
            problems.append(f"start.requires: media is optional for {site.name} {action}; "
                            "do not require it (attach steps are skipped when there is none)")

    submit = m.get("submit")
    if "submit" in m:
        if not isinstance(submit, dict):
            problems.append("submit: must be an object")
        else:
            for k in sorted(set(submit) - {"target", "label_allowlist"}):
                problems.append(f"submit: unknown key {k}")
            _check_target(submit.get("target"), "submit", problems)
            allow = submit.get("label_allowlist")
            if not isinstance(allow, list) or not allow or not all(isinstance(a, str) for a in allow):
                problems.append("submit.label_allowlist: must be a non-empty list of labels")
            elif action in SUBMIT_LABELS and not set(allow) <= set(SUBMIT_LABELS[action]):
                problems.append(f"submit.label_allowlist: must be a subset of "
                                f"{', '.join(SUBMIT_LABELS[action])} for {action}")

    confirm = m.get("confirm")
    if "confirm" in m:
        if not isinstance(confirm, dict) or not ({"network", "dom"} & set(confirm)):
            problems.append("confirm: needs a network and/or dom block")
        else:
            for k in sorted(set(confirm) - {"network", "dom"}):
                problems.append(f"confirm: unknown key {k}")
            net = confirm.get("network")
            if "network" in confirm:
                if not isinstance(net, dict):
                    problems.append("confirm.network: must be an object")
                else:
                    for k in sorted(set(net) - {"url_regex", "method", "id_path", "error_path"}):
                        problems.append(f"confirm.network: unknown key {k}")
                    try:
                        re.compile(net.get("url_regex") or "")
                        if not net.get("url_regex"):
                            problems.append("confirm.network.url_regex: must be non-empty")
                    except (re.error, TypeError) as e:
                        problems.append(f"confirm.network.url_regex: does not compile ({e})")
                    if net.get("method", "POST") not in ("POST", "PUT", "PATCH"):
                        problems.append("confirm.network.method: must be POST, PUT or PATCH")
                    for k in ("id_path", "error_path"):
                        try:
                            sitemap.parse_path(net.get(k, "" if k == "id_path" else "errors"))
                        except sitemap.PathError as e:
                            problems.append(f"confirm.network.{k}: {e}")
            dom = confirm.get("dom")
            if "dom" in confirm:
                if not isinstance(dom, dict):
                    problems.append("confirm.dom: must be an object")
                else:
                    for k in sorted(set(dom) - {"target", "state"}):
                        problems.append(f"confirm.dom: unknown key {k}")
                    _check_target(dom.get("target"), "confirm.dom", problems)
                    if dom.get("state", "visible") not in ("visible", "hidden"):
                        problems.append("confirm.dom.state: must be visible or hidden")

    if "permalink_template" in m:
        pt = m["permalink_template"]
        if not isinstance(pt, str) or "{id}" not in pt:
            problems.append("permalink_template: must contain {id}")
        else:
            _check_url(pt, site, "permalink_template", problems, placeholder="{id}")

    limits = m.get("limits")
    if "limits" in m:
        if not isinstance(limits, dict):
            problems.append("limits: must be an object")
        else:
            for k in sorted(set(limits) - {"per_hour", "per_day", "min_spacing_s"}):
                problems.append(f"limits: unknown key {k}")
            pd, ph, sp = limits.get("per_day"), limits.get("per_hour"), limits.get("min_spacing_s")
            if not (_is_int(pd) and 1 <= pd <= MAX_PER_DAY):
                problems.append(f"limits.per_day: must be an integer 1..{MAX_PER_DAY}")
            if not (_is_int(ph) and ph >= 1 and (not _is_int(pd) or ph <= pd)):
                problems.append("limits.per_hour: must be an integer 1..per_day")
            if not (_is_int(sp) and sp >= MIN_SPACING_S):
                problems.append(f"limits.min_spacing_s: must be an integer >= {MIN_SPACING_S}")

    lookup = m.get("lookup")
    if "lookup" in m:
        if not isinstance(lookup, dict) or not isinstance(lookup.get("page_type"), str):
            problems.append("lookup: must be an object with a page_type")
        else:
            for k in sorted(set(lookup) - {"page_type", "handle", "query"}):
                problems.append(f"lookup: unknown key {k}")
            if site is not None and lookup["page_type"] not in site.page_types:
                problems.append(f"lookup.page_type: {site.name} has no page type {lookup['page_type']!r}")
            elif site is not None:
                needed = sites.placeholders(site.page_types[lookup["page_type"]])
                for k in ("handle", "query"):
                    if k in needed and not lookup.get(k):
                        problems.append(f"lookup: {site.name} {lookup['page_type']} needs {k}")
                    if k not in needed and lookup.get(k):
                        problems.append(f"lookup: {site.name} {lookup['page_type']} does not take {k}")
    return problems


# ---------------------------------------------------------------------------
# Files and versions: <maps_dir>/<site>.action-<action>.v<N>.json

def action_map_filename(site: str, action: str, version: int) -> str:
    return f"{site}.action-{action}.v{version}.json"


def next_version(store: Store, site: str, action: str) -> int:
    row = store.conn.execute("SELECT MAX(version) AS v FROM action_maps WHERE site = ? AND action = ?",
                             (site, action)).fetchone()
    return (row["v"] or 0) + 1


def save_action_map(store: Store, maps_dir: str | Path, m: dict) -> Path:
    m = dict(m, version=next_version(store, m.get("site", ""), m.get("action", "")))
    problems = validate_action_map(m)
    if problems:
        raise ActionMapError("; ".join(problems))
    maps_dir = Path(maps_dir)
    maps_dir.mkdir(parents=True, exist_ok=True)
    path = maps_dir / action_map_filename(m["site"], m["action"], m["version"])
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)
    with store.conn:
        store.conn.execute("INSERT INTO action_maps (site, action, version, path, created_at)"
                           " VALUES (?, ?, ?, ?, ?)", (m["site"], m["action"], m["version"], str(path), now_iso()))
    return path


def load_action_map(path: str | Path) -> dict:
    try:
        m = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise ActionMapError(f"cannot read action map {path}: {e}") from e
    problems = validate_action_map(m)
    if problems:
        raise ActionMapError(f"invalid action map {path}: " + "; ".join(problems))
    return m


def current_action_map(store: Store, site: str, action: str) -> dict | None:
    row = store.conn.execute("SELECT path FROM action_maps WHERE site = ? AND action = ?"
                             " ORDER BY version DESC LIMIT 1", (site, action)).fetchone()
    if row is None:
        return None
    try:
        return load_action_map(row["path"])
    except ActionMapError as e:
        raise ActionMapError(f"{e}. Relearn it: agent-surf learn-action {site} {action}") from None


# ---------------------------------------------------------------------------
# Target resolution: role+name, then testid, then css; first visible match wins.

@dataclass
class Resolved:
    locator: Any
    how: str   # "role", "testid" or "css"


def _candidates(page: Any, target: dict) -> list[tuple[str, Any]]:
    out = []
    if target.get("role"):
        kw = {"name": target["name"], "exact": True} if target.get("name") else {}
        out.append(("role", page.get_by_role(target["role"], **kw)))
    if target.get("testid"):
        out.append(("testid", page.get_by_test_id(target["testid"])))
    if target.get("css"):
        out.append(("css", page.locator("css=" + target["css"])))
    return out


def _is_file_input(loc: Any) -> bool:
    try:
        return loc.evaluate("e => e.tagName === 'INPUT' && e.type === 'file'")
    except Exception:
        return False


def candidates(page: Any, target: dict, *, allow_hidden_file_input: bool = False,
               max_per_strategy: int = 20):
    """Visible matches in resolution order (role+name, testid, css), as Resolved."""
    for how, loc in _candidates(page, target):
        try:
            n = min(loc.count(), max_per_strategy)
        except Exception:
            continue
        for i in range(n):
            el = loc.nth(i)
            try:
                if el.is_visible() or (allow_hidden_file_input and _is_file_input(el)):
                    yield Resolved(el, how)
            except Exception:
                continue


def try_resolve(page: Any, target: dict, *, allow_hidden_file_input: bool = False,
                max_per_strategy: int = 20) -> Resolved | None:
    return next(candidates(page, target, allow_hidden_file_input=allow_hidden_file_input,
                           max_per_strategy=max_per_strategy), None)


def resolve_target(page: Any, target: dict, *, timeout_s: float = 0.0, poll_s: float = 0.25,
                   allow_hidden_file_input: bool = False) -> Resolved | None:
    """First visible match, trying role+name, testid, css in that order. Waits up
    to ``timeout_s`` for one to appear. None if nothing matched."""
    deadline = time.monotonic() + timeout_s
    while True:
        r = try_resolve(page, target, allow_hidden_file_input=allow_hidden_file_input)
        if r is not None or time.monotonic() >= deadline:
            return r
        page.wait_for_timeout(poll_s * 1000)
