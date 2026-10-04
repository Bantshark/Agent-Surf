"""Deterministic replay of a site map. Zero model calls.

The runner navigates to the templated URL, extracts items (network buffer
first, DOM as fallback), emits only ids not seen before, and stops on
``stop_after_seen`` consecutive already-seen items, ``max_scrolls`` or
``max_items``. If the first pass finds nothing usable it raises ``MapBroken``;
deciding whether to relearn is the caller's job.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from agent_surf import sitemap, sites
from agent_surf.browser import ReadOnlyPage
from agent_surf.store import Store

log = logging.getLogger("agent_surf.runner")

Guard = Callable[[ReadOnlyPage], None]


DRIFT_RUNS = 3            # warn when the fingerprint differs this many runs in a row
HEALTH_MIN_ITEMS = 0.5    # first pass below this share of the dry-run count -> warn
HEALTH_MIN_COMPLETE = 0.8 # below this share of items with all required fields -> warn


class NoMap(RuntimeError):
    pass


class MapBroken(RuntimeError):
    def __init__(self, reason: str, map_: dict | None = None):
        super().__init__(reason)
        self.reason = reason
        self.map = map_


@dataclass
class RunResult:
    items: list[dict] = field(default_factory=list)
    source: str | None = None
    scrolls: int = 0
    clicks: int = 0
    stop_reason: str = ""
    fingerprint: str | None = None
    drift_count: int = 0      # consecutive runs whose fingerprint differs from the learned one
    drift: bool = False       # a drift or health warning was logged this run (advisory only)


def load_current_map(store: Store, site: str, page_type: str) -> dict:
    row = store.current_map(site, page_type)
    if row is None:
        raise NoMap(f"no map for {site} {page_type}; run: agent-surf learn {site} {page_type}")
    return sitemap.load_map(row["path"])


def extract_items(m: dict, source: str, page: ReadOnlyPage, responses: list | None = None) -> list[dict]:
    """Raw items from one source. ``responses`` defaults to the whole buffer."""
    if source == "network":
        if responses is None:
            responses = page.buffer.all()
        return sitemap.items_from_responses(m["network"], responses)
    return page.dom_items(m["dom"])


def required_for(m: dict, source: str) -> list[str]:
    fields = m[source]["fields"]
    return [r for r in m["required_fields"] if r in fields]


def track_drift(store: Store, m: dict, fp: str) -> int:
    """Update the per-map mismatch streak and return it. A stored fingerprint
    from an older scheme is replaced by this run's (no warning)."""
    row = store.map_row(m["site"], m["page_type"], m["version"]) if "version" in m else None
    if row is None:
        return 0
    baseline = row["fingerprint"]
    if not baseline or not baseline.startswith(sitemap.FINGERPRINT_PREFIX):
        store.update_drift(m["site"], m["page_type"], m["version"], last_fingerprint=fp,
                           drift_count=0, baseline=fp)
        return 0
    count = 0 if fp == baseline else (row["drift_count"] or 0) + 1
    store.update_drift(m["site"], m["page_type"], m["version"], last_fingerprint=fp, drift_count=count)
    return count


def health_problems(batch: list[dict], required: list[str], dry_run_items: int | None) -> list[str]:
    ids = {it["item_id"] for it in batch if it["item_id"]}
    complete = {it["item_id"] for it in batch
                if it["item_id"] and sitemap.has_required(it["fields"], required)}
    problems = []
    if dry_run_items and len(ids) < HEALTH_MIN_ITEMS * dry_run_items:
        problems.append(f"first pass found {len(ids)} item(s), the learner's dry run found {dry_run_items}")
    if ids and len(complete) < HEALTH_MIN_COMPLETE * len(ids):
        problems.append(f"only {len(complete)} of {len(ids)} item(s) have all required fields")
    return problems


def run(store: Store, page: ReadOnlyPage, site: str, page_type: str, *,
        query: str | None = None, handle: str | None = None, guard: Guard | None = None) -> RunResult:
    m = load_current_map(store, site, page_type)
    url = sites.build_url(site, page_type, query=query, handle=handle)
    return replay(m, page, store, url, guard=guard)


def replay(m: dict, page: ReadOnlyPage, store: Store, url: str, *, guard: Guard | None = None) -> RunResult:
    guard = guard or (lambda p: None)
    site, page_type = m["site"], m["page_type"]
    max_scrolls = m["scroll"]["max_scrolls"]
    delay = m["scroll"]["delay_s"]
    stop_after_seen = m["scroll"]["stop_after_seen"]
    max_items = m["limits"]["max_items"]
    sources = [m["source"]] + [s for s in sitemap.SOURCES if s != m["source"] and s in m]

    result = RunResult()
    cursor = page.buffer.last_seq
    page.goto(url)
    guard(page)
    page.wait(delay)
    guard(page)

    click_selectors = list(m.get("click") or [])

    def expand() -> int:
        """Click the map's safe "show more" controls; off for the run after any navigation."""
        nonlocal click_selectors
        if not click_selectors:
            return 0
        clicks, navigated = page.expand(click_selectors)
        result.clicks += clicks
        if navigated:
            log.warning("a click changed the page URL; no more clicks this run")
            click_selectors = []
        return clicks

    if expand():
        page.wait(delay)
        guard(page)

    result.fingerprint = sitemap.fingerprint(page.aria_snapshot())
    result.drift_count = track_drift(store, m, result.fingerprint)
    if result.drift_count >= DRIFT_RUNS:
        result.drift = True
        log.warning("layout drift on %s %s: page structure has differed from when the map was "
                    "learned for %d runs in a row", site, page_type, result.drift_count)

    def pass_items(source: str) -> list[dict]:
        nonlocal cursor
        if source == "network":
            fresh = page.buffer.since(cursor)
            cursor = page.buffer.last_seq
            return extract_items(m, source, page, fresh)
        return extract_items(m, source, page)

    run_ids: set[str] = set()
    consecutive_seen = 0
    chosen: str | None = None
    required: list[str] = []

    for i in range(max_scrolls + 1):
        if chosen is None:
            batch: list[dict] = []
            for source in sources:
                batch = pass_items(source)
                if any(it["item_id"] for it in batch):
                    chosen = source
                    break
            if chosen is None:
                raise MapBroken("first pass found no items with ids", m)
            required = required_for(m, chosen)
            if not any(it["item_id"] and sitemap.has_required(it["fields"], required) for it in batch):
                raise MapBroken(f"every item misses a required field ({', '.join(required)})", m)
            result.source = chosen
            row = store.map_row(site, page_type, m["version"]) if "version" in m else None
            problems = health_problems(batch, required, row["dry_run_items"] if row else None)
            if problems:
                result.drift = True
                log.warning("extraction health dropped on %s %s: %s", site, page_type,
                            "; ".join(problems))
            if chosen != m["source"]:
                log.warning("%s source found nothing; using %s fallback", m["source"], chosen)
        else:
            batch = pass_items(chosen)

        for it in batch:
            item_id = it["item_id"]
            if not item_id or item_id in run_ids:
                continue
            if not sitemap.has_required(it["fields"], required):
                continue
            run_ids.add(item_id)
            out: dict[str, Any] = {"site": site, "page_type": page_type, "item_id": item_id, **it["fields"]}
            if store.add_new_item(site, page_type, item_id, out):
                result.items.append(out)
                consecutive_seen = 0
                if len(result.items) >= max_items:
                    result.stop_reason = "max_items"
                    break
            else:
                consecutive_seen += 1
                if consecutive_seen >= stop_after_seen:
                    result.stop_reason = "stop_after_seen"
                    break
        if result.stop_reason:
            break
        if i == max_scrolls:
            result.stop_reason = "max_scrolls"
            break
        page.scroll()
        result.scrolls += 1
        expand()
        page.wait(delay)
        guard(page)
        page.check_domain()

    log.info("%s %s: %d new item(s), stopped on %s after %d scroll(s)",
             site, page_type, len(result.items), result.stop_reason, result.scrolls)
    return result
