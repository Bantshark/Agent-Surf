"""Site map schema, validation, path language and fingerprint.

A map tells the runner where items live on one (site, page_type): in captured
JSON responses (``network``) and/or in the DOM (``dom``). Maps are data only;
nothing in a map is ever executed or turned into an action other than the
runner's fixed read-only whitelist.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

ALLOWED_KEYS = {
    "site", "page_type", "version", "source", "network", "dom", "required_fields",
    "fingerprint", "scroll", "limits", "learned_at", "learned_by",
}
REQUIRED_KEYS = {"site", "page_type", "source", "required_fields", "scroll", "limits"}
NETWORK_KEYS = {"url_regex", "items_path", "id_path", "fields"}
DOM_KEYS = {"item", "id_attr", "fields"}
SCROLL_KEYS = {"max_scrolls", "delay_s", "stop_after_seen"}
LIMITS_KEYS = {"max_items"}
SOURCES = ("network", "dom")
# Names the runner adds to every output item; a field may not shadow them.
RESERVED_FIELDS = {"site", "page_type", "item_id"}

MAX_SCROLLS_CEILING = 100
MAX_ITEMS_CEILING = 1000
MIN_DELAY_S = 1.0

_NAME_RE = re.compile(r"^[a-z0-9_]+$")


class PathError(ValueError):
    pass


class MapError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Path language: dot keys, [N] index, [*] fan-out.

STAR = object()
_KEY_RE = re.compile(r"[^.\[\]]+")


def parse_path(path: str) -> tuple:
    if not isinstance(path, str) or not path.strip():
        raise PathError("empty path")
    steps: list = []
    i, n = 0, len(path)
    while i < n:
        c = path[i]
        if c == "[":
            j = path.find("]", i)
            if j == -1:
                raise PathError(f"unclosed '[' in {path!r}")
            inner = path[i + 1:j]
            if inner == "*":
                steps.append(STAR)
            elif inner.isdigit():
                steps.append(int(inner))
            else:
                raise PathError(f"bad index [{inner}] in {path!r}")
            i = j + 1
            continue
        if steps:
            if c != ".":
                raise PathError(f"expected '.' or '[' at {i} in {path!r}")
            i += 1
        m = _KEY_RE.match(path, i)
        if not m:
            raise PathError(f"expected a key at {i} in {path!r}")
        steps.append(m.group(0))
        i = m.end()
    return tuple(steps)


def extract(obj: Any, path: str) -> list:
    """All values at ``path``. Missing keys and out-of-range indexes yield nothing."""
    values = [obj]
    for step in parse_path(path):
        nxt: list = []
        for v in values:
            if step is STAR:
                if isinstance(v, list):
                    nxt.extend(v)
                elif isinstance(v, dict):
                    nxt.extend(v.values())
            elif isinstance(step, int):
                if isinstance(v, list) and step < len(v):
                    nxt.append(v[step])
            elif isinstance(v, dict) and step in v:
                nxt.append(v[step])
        values = nxt
    return values


def get_one(obj: Any, path: str) -> Any:
    """First non-null value at ``path``, or None."""
    for v in extract(obj, path):
        if v is not None:
            return v
    return None


# ---------------------------------------------------------------------------
# Validation

def _check_keys(block: dict, allowed: set, where: str, problems: list[str]) -> None:
    for k in sorted(set(block) - allowed):
        problems.append(f"unknown key {where}.{k}" if where else f"unknown key {k}")


def _check_path(value: Any, where: str, problems: list[str]) -> None:
    try:
        parse_path(value)
    except PathError as e:
        problems.append(f"{where}: {e}")


def _check_css(value: Any, where: str, problems: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{where}: CSS selector is empty")


def _check_fields(fields: Any, where: str, check, problems: list[str]) -> None:
    if not isinstance(fields, dict) or not fields:
        problems.append(f"{where}: must be a non-empty object")
        return
    for name, value in fields.items():
        if not isinstance(name, str) or not name:
            problems.append(f"{where}: field names must be non-empty strings")
        elif name in RESERVED_FIELDS:
            problems.append(f"{where}.{name}: reserved field name")
        check(value, f"{where}.{name}", problems)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_map(m: Any) -> list[str]:
    """Return a list of problems; an empty list means the map is valid."""
    if not isinstance(m, dict):
        return ["map must be a JSON object"]
    problems: list[str] = []
    _check_keys(m, ALLOWED_KEYS, "", problems)
    for k in sorted(REQUIRED_KEYS - set(m)):
        problems.append(f"missing key {k}")

    for k in ("site", "page_type"):
        if k in m and not (isinstance(m[k], str) and _NAME_RE.match(m[k])):
            problems.append(f"{k}: must match [a-z0-9_]+")
    if "version" in m and not (_is_int(m["version"]) and m["version"] >= 1):
        problems.append("version: must be a positive integer")
    for k in ("fingerprint", "learned_at", "learned_by"):
        if k in m and not isinstance(m[k], str):
            problems.append(f"{k}: must be a string")

    source = m.get("source")
    if "source" in m and source not in SOURCES:
        problems.append("source: must be 'network' or 'dom'")
    elif source in SOURCES and source not in m:
        problems.append(f"source is {source!r} but there is no {source} block")

    net = m.get("network")
    if "network" in m:
        if not isinstance(net, dict):
            problems.append("network: must be an object")
        else:
            _check_keys(net, NETWORK_KEYS, "network", problems)
            for k in sorted(NETWORK_KEYS - set(net)):
                problems.append(f"network: missing key {k}")
            rx = net.get("url_regex")
            if "url_regex" in net:
                if not isinstance(rx, str) or not rx:
                    problems.append("network.url_regex: must be a non-empty string")
                else:
                    try:
                        re.compile(rx)
                    except re.error as e:
                        problems.append(f"network.url_regex: does not compile ({e})")
            for k in ("items_path", "id_path"):
                if k in net:
                    _check_path(net[k], f"network.{k}", problems)
            if "fields" in net:
                _check_fields(net["fields"], "network.fields", _check_path, problems)

    dom = m.get("dom")
    if "dom" in m:
        if not isinstance(dom, dict):
            problems.append("dom: must be an object")
        else:
            _check_keys(dom, DOM_KEYS, "dom", problems)
            for k in sorted(DOM_KEYS - set(dom)):
                problems.append(f"dom: missing key {k}")
            if "item" in dom:
                _check_css(dom["item"], "dom.item", problems)
            if "id_attr" in dom and not (isinstance(dom["id_attr"], str) and dom["id_attr"].strip()):
                problems.append("dom.id_attr: must be a non-empty attribute name")
            if "fields" in dom:
                _check_fields(dom["fields"], "dom.fields", _check_css, problems)

    req = m.get("required_fields")
    if "required_fields" in m:
        if not isinstance(req, list) or not all(isinstance(r, str) and r for r in req):
            problems.append("required_fields: must be a list of field names")
        elif source in SOURCES and isinstance(m.get(source), dict):
            fields = m[source].get("fields")
            if isinstance(fields, dict):
                for r in req:
                    if r not in fields:
                        problems.append(f"required_fields: {r!r} is not a {source} field")

    scroll = m.get("scroll")
    if "scroll" in m:
        if not isinstance(scroll, dict):
            problems.append("scroll: must be an object")
        else:
            _check_keys(scroll, SCROLL_KEYS, "scroll", problems)
            for k in sorted(SCROLL_KEYS - set(scroll)):
                problems.append(f"scroll: missing key {k}")
            ms = scroll.get("max_scrolls")
            if "max_scrolls" in scroll and not (_is_int(ms) and 0 <= ms <= MAX_SCROLLS_CEILING):
                problems.append(f"scroll.max_scrolls: must be an integer 0..{MAX_SCROLLS_CEILING}")
            d = scroll.get("delay_s")
            if "delay_s" in scroll and not (_is_num(d) and d >= MIN_DELAY_S):
                problems.append(f"scroll.delay_s: must be a number >= {MIN_DELAY_S}")
            sas = scroll.get("stop_after_seen")
            if "stop_after_seen" in scroll and not (_is_int(sas) and sas >= 1):
                problems.append("scroll.stop_after_seen: must be an integer >= 1")

    limits = m.get("limits")
    if "limits" in m:
        if not isinstance(limits, dict):
            problems.append("limits: must be an object")
        else:
            _check_keys(limits, LIMITS_KEYS, "limits", problems)
            mi = limits.get("max_items")
            if not (_is_int(mi) and 1 <= mi <= MAX_ITEMS_CEILING):
                problems.append(f"limits.max_items: must be an integer 1..{MAX_ITEMS_CEILING}")
    return problems


# ---------------------------------------------------------------------------
# Items

def normalize_id(raw: Any) -> str | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        return str(raw)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return not value
    return False


def has_required(fields: dict, required: Iterable[str]) -> bool:
    return all(not is_missing(fields.get(r)) for r in required)


def items_from_json(network: dict, data: Any) -> list[dict]:
    """Raw items ``{"item_id", "fields"}`` from one parsed JSON body."""
    out = []
    for node in extract(data, network["items_path"]):
        item_id = normalize_id(get_one(node, network["id_path"]))
        fields = {name: get_one(node, path) for name, path in network["fields"].items()}
        out.append({"item_id": item_id, "fields": fields})
    return out


def items_from_responses(network: dict, responses: Iterable[Any]) -> list[dict]:
    """Raw items from captured responses whose URL matches ``url_regex``.

    ``responses`` are objects with ``url`` and ``data`` attributes
    (see ``browser.CapturedResponse``).
    """
    rx = re.compile(network["url_regex"])
    out: list[dict] = []
    for r in responses:
        if rx.search(r.url):
            out.extend(items_from_json(network, r.data))
    return out


# ---------------------------------------------------------------------------
# Fingerprint: roles and nesting of the aria snapshot, all names/text stripped.

_ARIA_LINE = re.compile(r"^(\s*)-\s+(.*)$")
_ROLE = re.compile(r"[A-Za-z][\w-]*")


def _aria_roles(snapshot: str) -> list[tuple[int, str]]:
    out = []
    for line in snapshot.splitlines():
        m = _ARIA_LINE.match(line)
        if not m:
            continue
        indent, rest = len(m.group(1)), m.group(2)
        if rest.startswith("/"):  # properties such as /url carry text, not structure
            continue
        if rest.startswith('"'):
            out.append((indent, "text"))
            continue
        r = _ROLE.match(rest)
        if r:
            out.append((indent, r.group(0)))
    return out


def skeleton(snapshot: str) -> str:
    """Canonical structure string. Repeated sibling subtrees collapse to one,
    so feeds with different item counts produce the same skeleton."""
    root: dict = {"role": "root", "children": [], "indent": -1}
    stack = [root]
    for indent, role in _aria_roles(snapshot):
        while stack[-1]["indent"] >= indent:
            stack.pop()
        node = {"role": role, "children": [], "indent": indent}
        stack[-1]["children"].append(node)
        stack.append(node)

    def ser(node: dict) -> str:
        kids = sorted({ser(c) for c in node["children"]})
        return node["role"] + ("(" + ",".join(kids) + ")" if kids else "")

    return ser(root)


def fingerprint(snapshot: str) -> str:
    return "sha256-" + hashlib.sha256(skeleton(snapshot).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Files: <maps_dir>/<site>.<page_type>.v<N>.json

def map_filename(site: str, page_type: str, version: int) -> str:
    return f"{site}.{page_type}.v{version}.json"


def save_map(maps_dir: str | Path, m: dict) -> Path:
    problems = validate_map(m)
    if problems:
        raise MapError("; ".join(problems))
    if "version" not in m:
        raise MapError("version is required to save a map")
    maps_dir = Path(maps_dir)
    maps_dir.mkdir(parents=True, exist_ok=True)
    path = maps_dir / map_filename(m["site"], m["page_type"], m["version"])
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)
    return path


def load_map(path: str | Path) -> dict:
    try:
        m = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise MapError(f"cannot read map {path}: {e}") from e
    problems = validate_map(m)
    if problems:
        raise MapError(f"invalid map {path}: " + "; ".join(problems))
    return m
