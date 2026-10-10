"""v2 publish with receipts.

Only an approved queue item whose content_hash still matches is published,
within the action map's caps. Nothing is marked published at dispatch: the
item becomes ``published`` only with a receipt, i.e. the platform's own post id
from the create response (with no errors), and a read-back of the permalink
showing the approved text. Anything less is ``needs_attention`` with evidence.
A submit whose outcome is unknown is never retried; the account is checked
through the reading side instead.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from agent_surf import actionmap, outbox, runner, sitemap, sites
from agent_surf.executor import (ActionPage, Refused, StepFailed, SubmitUnknown, Submitter,
                                 normalize_text)
from agent_surf.store import Store

log = logging.getLogger("agent_surf.publisher")

CONFIRM_TIMEOUT_S = 20.0
READBACK_TIMEOUT_S = 15.0

OpenPage = Callable[[sites.Site], ActionPage]


class PublishRefused(RuntimeError):
    """Not published and not attempted (status, content, map or caps)."""


@dataclass
class PublishResult:
    item: dict
    notes: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return self.item["status"]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Caps

def caps_problem(store: Store, site: str, action: str, limits: dict, now: datetime) -> str | None:
    """Why publishing now would break the map's caps, or None. Every submit
    counts, whatever its outcome."""
    rows = store.conn.execute(
        "SELECT submitted_at FROM dispatch_log WHERE site = ? AND action = ? AND submitted_at IS NOT NULL",
        (site, action)).fetchall()
    times = sorted(datetime.fromisoformat(r["submitted_at"]) for r in rows)
    if times:
        gap = (now - times[-1]).total_seconds()
        if gap < limits["min_spacing_s"]:
            return f"min spacing: last {action} on {site} was {int(gap)} s ago (min {limits['min_spacing_s']} s)"
    hour = sum(1 for t in times if now - t < timedelta(hours=1))
    if hour >= limits["per_hour"]:
        return f"per-hour cap reached ({hour}/{limits['per_hour']}) for {site} {action}"
    day = sum(1 for t in times if now - t < timedelta(days=1))
    if day >= limits["per_day"]:
        return f"per-day cap reached ({day}/{limits['per_day']}) for {site} {action}"
    return None


# ---------------------------------------------------------------------------
# Receipts

def _await_network_confirm(page: ActionPage, net: dict, since_seq: int) -> tuple[str | None, str | None]:
    """(post_id, error) from the create response; (None, None) if none arrived.
    Raises SubmitUnknown if the tab leaves the site while waiting."""
    rx = re.compile(net["url_regex"])
    method = net.get("method", "POST")
    deadline = time.monotonic() + CONFIRM_TIMEOUT_S
    while True:
        if not sites.is_allowed_url(page.site, page.raw.url):
            raise SubmitUnknown(f"the tab left the site after submit ({page.raw.url[:200]})")
        for r in page.buffer.since(since_seq):
            if r.method == method and rx.search(r.url):
                errors = sitemap.get_one(r.data, net.get("error_path", "errors"))
                if not sitemap.is_missing(errors):
                    return None, f"platform returned errors (HTTP {r.status}): {str(errors)[:300]}"
                post_id = sitemap.normalize_id(sitemap.get_one(r.data, net["id_path"]))
                if post_id is None:
                    return None, f"create response (HTTP {r.status}) has no id at {net['id_path']}"
                return post_id, None
        if time.monotonic() >= deadline:
            return None, None
        page.raw.wait_for_timeout(250)


def _id_from_permalink(template: str, url: str) -> str | None:
    pattern = re.escape(template).replace(re.escape("{id}"), r"([A-Za-z0-9_-]+)")
    m = re.search(pattern, url)
    return m.group(1) if m else None


def _await_dom_confirm(page: ActionPage, amap: dict) -> tuple[str | None, str | None]:
    if not sites.is_allowed_url(page.site, page.raw.url):
        raise SubmitUnknown(f"the tab left the site after submit ({page.raw.url[:200]})")
    dom = amap["confirm"]["dom"]
    r = actionmap.resolve_target(page.raw, dom["target"], timeout_s=CONFIRM_TIMEOUT_S)
    if r is None:
        return None, None
    href = r.locator.evaluate("e => (e.closest('a[href]') || e.querySelector('a[href]') || {}).href || ''")
    post_id = _id_from_permalink(amap["permalink_template"], href or "")
    if post_id is None:
        return None, "confirmation shown on the page but no post id found in it"
    return post_id, None


def read_back(page: ActionPage, permalink: str, payload: dict) -> bool:
    """Open the permalink (on-site only) and check it shows the approved text."""
    page.goto(permalink)
    want = normalize_text(payload.get("text") or "")
    deadline = time.monotonic() + READBACK_TIMEOUT_S
    while True:
        try:
            shown = normalize_text(page.raw.locator("body").inner_text())
        except Exception:
            shown = ""
        if (want and want in shown) or (not want and shown):
            return True
        if time.monotonic() >= deadline:
            return False
        page.raw.wait_for_timeout(300)


def lookup_problem(store: Store, amap: dict) -> str | None:
    """Why an unknown outcome cannot be checked on the account, or None."""
    lookup = amap.get("lookup")
    site, action = amap["site"], amap["action"]
    if not lookup:
        return (f"no lookup configured for the {site} {action} action map (set one: agent-surf actions "
                f"set-lookup {site} {action} --page-type profile --handle <you>)")
    if store.current_map(site, lookup["page_type"]) is None:
        arg = (f" --handle {lookup['handle']}" if lookup.get("handle") else
               f" --query {lookup['query']}" if lookup.get("query") else "")
        return (f"the lookup page {site} {lookup['page_type']} has no reading map "
                f"(learn: agent-surf learn {site} {lookup['page_type']}{arg})")
    return None


TIME_FIELDS = ("created_at", "timestamp", "time", "date")
LOOKUP_SLACK_S = 120


def parse_time_value(value: Any) -> datetime | None:
    """ISO 8601, epoch seconds/milliseconds, RFC 2822 or X's legacy created_at
    ('Wed Oct 10 20:19:24 +0000 2026'); None if it does not parse."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        secs = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(secs, timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    if v.isdigit():
        return parse_time_value(int(v))
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        return datetime.strptime(v, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(v)
    except (TypeError, ValueError, IndexError):
        return None


def receipt_post_ids(store: Store) -> set[str]:
    out = set()
    for (receipt,) in store.conn.execute("SELECT receipt_json FROM queue WHERE receipt_json IS NOT NULL"):
        pid = json.loads(receipt).get("post_id")
        if pid:
            out.add(str(pid))
    return out


def lookup_post(store: Store, open_page: OpenPage, amap: dict, payload: dict,
                submitted_at: datetime | None = None) -> tuple[str, str] | None:
    """Unknown outcome: look for the post on the account through the reading
    side (the map's lookup page and its reading map). Fix 17: a candidate with
    the same text is accepted only if (a) its id is not already in a receipt,
    and (b) when the reading map extracts a time field (created_at, timestamp,
    time, date), it parses and is >= submitted_at - 120 s. Of the rest, the
    newest wins (by time, else page order). Returns (item id, rule) or None."""
    lookup = amap.get("lookup")
    text = normalize_text(payload.get("text") or "")
    if not lookup or not text or lookup_problem(store, amap):
        return None
    taken = receipt_post_ids(store)
    row = store.current_map(amap["site"], lookup["page_type"])
    m = sitemap.load_map(row["path"])
    site = sites.get_site(amap["site"])
    page = open_page(site).read
    url = sites.build_url(site.name, lookup["page_type"], query=lookup.get("query"), handle=lookup.get("handle"))
    since = page.buffer.last_seq
    page.goto(url)
    runner.wait_for_content(m, page, since, lambda p: None)
    for source in [m["source"]] + [s for s in sitemap.SOURCES if s != m["source"] and s in m]:
        responses = page.buffer.since(since) if source in sitemap.STREAM_SOURCES else None
        time_keys = [k for k in TIME_FIELDS if k in m[source]["fields"]]
        candidates: list[tuple[datetime | None, int, str, str]] = []
        for order, it in enumerate(runner.extract_items(m, source, page, responses)):
            if not it["item_id"] or not any(isinstance(v, str) and normalize_text(v) == text
                                            for k, v in it["fields"].items() if k not in TIME_FIELDS):
                continue
            if str(it["item_id"]) in taken:
                continue  # rule (a): an older post that already has a receipt
            when = None
            if time_keys:
                when = next((t for t in (parse_time_value(it["fields"].get(k)) for k in time_keys) if t), None)
                if when is None or (submitted_at is not None
                                    and when < submitted_at - timedelta(seconds=LOOKUP_SLACK_S)):
                    continue  # rule (b): no parseable time, or older than the submit
                rule = (f"same text, id not in any receipt, {time_keys[0]} >= submit - {LOOKUP_SLACK_S} s"
                        if submitted_at else "same text, id not in any receipt (submit time unknown)")
            else:
                rule = "same text, id not in any receipt (no time field)"
            candidates.append((when, order, str(it["item_id"]), rule))
        if candidates:
            timed = [c for c in candidates if c[0] is not None]
            best = max(timed, key=lambda c: c[0]) if timed else min(candidates, key=lambda c: c[1])
            return best[2], best[3] + ("; newest of several" if len(candidates) > 1 else "")
    return None


# ---------------------------------------------------------------------------
# Publish

class _Log:
    def __init__(self, store: Store, item: dict, now: datetime):
        self.store = store
        with store.conn:
            cur = store.conn.execute(
                "INSERT INTO dispatch_log (queue_id, site, action, dispatched_at) VALUES (?, ?, ?, ?)",
                (item["id"], item["site"], item["action"], _iso(now)))
        self.id = cur.lastrowid

    def submitted(self, now: datetime) -> None:
        with self.store.conn:
            self.store.conn.execute("UPDATE dispatch_log SET submitted_at = ? WHERE id = ?", (_iso(now), self.id))

    def completed(self, outcome: str) -> None:
        with self.store.conn:
            self.store.conn.execute("UPDATE dispatch_log SET completed_at = ?, outcome = ? WHERE id = ?",
                                    (_iso(datetime.now(timezone.utc)), outcome, self.id))


def _finish(store: Store, item_id: int, status: str, logrow: _Log | None, *, error: str | None = None,
            receipt: dict | None = None, notes: list[str] | None = None) -> PublishResult:
    item = outbox.transition(store, item_id, status, last_error=error, receipt=receipt)
    if logrow:
        logrow.completed(status if not error else f"{status}: {error[:200]}")
    level = logging.INFO if status == "published" else logging.WARNING
    log.log(level, "queue item %s -> %s%s", item_id, status, f" ({error})" if error else "")
    return PublishResult(item, notes or [])


def _submitted_at(store: Store, item_id: int, logrow: "_Log | None") -> datetime | None:
    """When this item was last submitted (the dispatch log)."""
    if logrow is not None:
        row = store.conn.execute("SELECT submitted_at FROM dispatch_log WHERE id = ?", (logrow.id,)).fetchone()
    else:
        row = store.conn.execute("SELECT submitted_at FROM dispatch_log WHERE queue_id = ? AND submitted_at"
                                 " IS NOT NULL ORDER BY id DESC LIMIT 1", (item_id,)).fetchone()
    return datetime.fromisoformat(row["submitted_at"]) if row and row["submitted_at"] else None


def resolve_unknown(store: Store, open_page: OpenPage, item: dict, amap: dict, logrow: _Log | None,
                    reason: str) -> PublishResult:
    """Outcome after submit unknown: check the account; never resubmit."""
    problem = lookup_problem(store, amap)
    if problem:
        return _finish(store, item["id"], "needs_attention", logrow,
                       error=f"outcome unknown after submit ({reason}); could not check the account: "
                             f"{problem}; not retried - check the account before re-approving")
    try:
        found = lookup_post(store, open_page, amap, item["payload"], _submitted_at(store, item["id"], logrow))
    except Exception as e:  # the lookup itself must not cause a retry
        found = None
        reason += f"; lookup failed ({type(e).__name__})"
    if found:
        post_id, rule = found
        receipt = {"post_id": post_id, "permalink": amap["permalink_template"].format(id=post_id),
                   "confirmed_at": _iso(datetime.now(timezone.utc)), "via": "account lookup", "match": rule}
        return _finish(store, item["id"], "published", logrow, receipt=receipt)
    return _finish(store, item["id"], "needs_attention", logrow,
                   error=f"outcome unknown after submit ({reason}); not found on the account; "
                         "not retried - check the account before re-approving")


def _discard_quietly(sub: Submitter) -> None:
    """Before-submit failure: close the composer so no draft is left behind."""
    try:
        sub.discard()
    except Exception:
        pass


def publish(store: Store, open_page: OpenPage, item_id: int, *, client: Any = None, model: str = "",
            maps_dir: Any = None, guard: Any = None, now: datetime | None = None) -> PublishResult:
    now = _now(now)
    item = outbox.get(store, item_id)
    if item["status"] != "approved":
        raise PublishRefused(f"queue item {item_id} is {item['status']}; only approved items are published")
    problem = outbox.verify_approved(item)
    if problem:
        outbox.transition(store, item_id, "needs_attention", last_error=problem)
        raise PublishRefused(f"queue item {item_id}: {problem}")
    amap = actionmap.current_action_map(store, item["site"], item["action"])
    if amap is None:
        raise PublishRefused(f"no action map for {item['site']} {item['action']}; "
                             f"run: agent-surf learn-action {item['site']} {item['action']}")
    cap = caps_problem(store, item["site"], item["action"], amap["limits"], now)
    if cap:
        raise PublishRefused(cap)

    site = sites.get_site(item["site"])
    payload = outbox.publish_payload(item)   # approved text + the approved media snapshots
    outbox.transition(store, item_id, "publishing", count_attempt=True)
    logrow = _Log(store, item, now)
    notes: list[str] = []
    healed = False
    while True:
        page = open_page(site)
        sub = Submitter(page, amap, payload, guard)
        try:
            sub.start()
            sub.run_steps()
            since = sub.submit()
            logrow.submitted(now)
            notes += sub.notes
            break
        except (Refused, sites.DomainRefused) as e:
            _discard_quietly(sub)
            return _finish(store, item_id, "needs_attention", logrow, error=f"refused: {e}", notes=sub.notes)
        except SubmitUnknown as e:
            logrow.submitted(now)
            return resolve_unknown(store, open_page, item, amap, logrow, str(e))
        except StepFailed as e:
            _discard_quietly(sub)
            if healed or client is None:
                return _finish(store, item_id, "needs_attention", logrow,
                               error=f"step failed before submit (nothing published): {e}"
                                     + ("; already relearned once" if healed else ""), notes=sub.notes)
            healed = True
            log.warning("step failed before submit (%s); relearning the action map once", e)
            from agent_surf import action_learner
            try:
                amap = action_learner.learn_action(
                    store, open_page(site), item["site"], item["action"], client=client, model=model,
                    maps_dir=maps_dir, target_url=payload.get("target_url"),
                    thread_url=payload.get("thread_url"), old_map=amap, broken_reason=str(e), guard=guard,
                    start_url=None if amap["start"]["url_template"] in actionmap.URL_PLACEHOLDERS
                    else amap["start"]["url_template"])
            except Exception as le:
                return _finish(store, item_id, "needs_attention", logrow,
                               error=f"step failed before submit ({e}); relearn failed: {le}")

    # Receipt: the platform's own id, then a read-back of the permalink.
    try:
        net = amap["confirm"].get("network")
        if net:
            post_id, error = _await_network_confirm(page, net, since)
            via = "network"
        else:
            post_id, error = _await_dom_confirm(page, amap)
            via = "dom"
    except SubmitUnknown as e:
        return resolve_unknown(store, open_page, item, amap, logrow, str(e))
    except Exception as e:
        return resolve_unknown(store, open_page, item, amap, logrow, f"confirm failed ({type(e).__name__})")
    if error:
        return _finish(store, item_id, "needs_attention", logrow, error=f"not published: {error}", notes=notes)
    if post_id is None:
        return resolve_unknown(store, open_page, item, amap, logrow,
                               f"no {via} confirmation; page after submit: {sub.page_changes()}")
    receipt = {"post_id": post_id, "permalink": amap["permalink_template"].format(id=post_id),
               "confirmed_at": _iso(datetime.now(timezone.utc)), "via": via}
    try:
        matched = read_back(page, receipt["permalink"], payload)
    except Exception as e:
        matched = False
        notes.append(f"read-back failed ({type(e).__name__})")
    if not matched:
        return _finish(store, item_id, "needs_attention", logrow, receipt=dict(receipt, read_back=False),
                       error=f"post {post_id} created but its permalink does not show the approved text",
                       notes=notes)
    return _finish(store, item_id, "published", logrow, receipt=dict(receipt, read_back=True), notes=notes)


def recover_publishing(store: Store, open_page: OpenPage) -> list[PublishResult]:
    """After a crash: items left in 'publishing' have an unknown outcome."""
    out = []
    for item in outbox.list_items(store, "publishing"):
        amap = actionmap.current_action_map(store, item["site"], item["action"])
        if amap is None:
            out.append(_finish(store, item["id"], "needs_attention", None,
                               error="interrupted while publishing; no action map to check the account"))
        else:
            out.append(resolve_unknown(store, open_page, item, amap, None, "interrupted while publishing"))
    return out
