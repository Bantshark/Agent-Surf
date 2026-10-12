"""Fix 29: tell an empty result page from a broken map.

A search with no matches, an empty inbox or a page that shows nothing is not
a reason to relearn a map (or to pay for one). Two signals:

- the site's own "no results" markers (sites.NO_RESULTS: URL, title,
  accessibility text, data-testid);
- structure without content: a ``main`` landmark is present but holds no item
  and fewer than MIN_CONTENT content nodes. Without a ``main`` landmark the
  structure is missing, and that stays a broken map for the runner.

Pure reads; nothing here clicks, types or calls a model.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

from agent_surf import sitemap, sites

MIN_CONTENT = 3   # fewer content nodes than this (and no item) is empty
CONTENT_ROLES = frozenset({"text", "paragraph", "link", "img", "figure", "blockquote", "cell",
                           "listitem", "article", "row", "treeitem"})
OUTSIDE_RESULTS = frozenset({"banner", "navigation", "complementary", "contentinfo"})


def _site_name(page: Any) -> str:
    site = getattr(page, "site", None)
    return getattr(site, "name", site) or ""


def marker_reason(page: Any, snapshot: str | None = None) -> str | None:
    """The site's own "no results" marker, if the page shows one."""
    markers = sites.no_results_markers(_site_name(page))
    if not (markers.paths or markers.titles or markers.texts or markers.testids):
        return None
    try:
        path = urllib.parse.urlsplit(page.url or "").path.lower()
    except ValueError:
        path = ""
    for p in markers.paths:
        if path.startswith(p.lower()):
            return f"URL path starts with {p}"
    title = (page.title() or "").lower()
    for t in markers.titles:
        if t.lower() in title:
            return f"page title contains {t!r}"
    if markers.texts:
        snap = (page.aria_snapshot() if snapshot is None else snapshot).lower()
        for t in markers.texts:
            if t.lower() in snap:
                return f"page says {t!r}"
    for tid in markers.testids:
        if page.count(f"[data-testid={json.dumps(tid)}]"):
            return f"empty-state element {tid!r} is on the page"
    return None


def _main_subtree(roles: list[tuple[int, str]]) -> list[tuple[int, str]] | None:
    for i, (indent, role) in enumerate(roles):
        if role == "main":
            out = []
            for ind, r in roles[i + 1:]:
                if ind <= indent:
                    break
                out.append((ind, r))
            return out
    return None


def _results_nodes(roles: list[tuple[int, str]]) -> list[str]:
    """Roles outside banner/navigation/complementary/contentinfo subtrees."""
    out, skip_below = [], None
    for indent, role in roles:
        if skip_below is not None and indent > skip_below:
            continue
        skip_below = None
        if role in OUTSIDE_RESULTS:
            skip_below = indent
            continue
        out.append(role)
    return out


def emptiness(snapshot: str) -> tuple[bool, bool]:
    """(empty, has_main): no item node and fewer than MIN_CONTENT content
    nodes in the results area (``main`` if present, else the page minus its
    banner/navigation/sidebars)."""
    roles = sitemap._aria_roles(snapshot)
    main = _main_subtree(roles)
    scope = _results_nodes(main if main is not None else roles)
    if any(r in sitemap.ITEM_ROLES for r in scope):
        return False, main is not None
    content = sum(1 for r in scope if r in CONTENT_ROLES)
    return content < MIN_CONTENT, main is not None


def nothing_to_learn(page: Any) -> str | None:
    """Why a page cannot be learned from (no results / empty), or None."""
    snap = page.aria_snapshot()
    reason = marker_reason(page, snap)
    if reason:
        return f"the page shows no results ({reason})"
    empty, _ = emptiness(snap)
    if empty:
        return "the page shows no items or content"
    return None
