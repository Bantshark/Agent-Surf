"""v2 scheduler/dispatcher on the stdlib + SQLite.

Every TICK_S it publishes approved items that are due (scheduled_at <= now, or
no schedule), one at a time through the publisher, which enforces caps and
spacing. A gap between ticks longer than WAKE_FACTOR * TICK_S means the machine
slept or the dispatcher was stopped: items that came due during the gap are
"missed" and get their missed_policy (skip -> failed, run -> publish once,
ask -> needs_attention + a ping). The last tick is stored in SQLite, so this
survives restarts; items left 'publishing' by a crash are recovered (checked on
the account, never resubmitted) before the first tick.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from agent_surf import challenge, outbox
from agent_surf.publisher import PublishRefused
from agent_surf.store import Store

log = logging.getLogger("agent_surf.dispatcher")

TICK_S = 30.0
WAKE_FACTOR = 3


@dataclass
class TickReport:
    at: str
    resumed: bool = False
    missed: list[tuple[int, str]] = field(default_factory=list)      # (id, policy outcome)
    published: list[int] = field(default_factory=list)
    not_published: list[tuple[int, str]] = field(default_factory=list)  # (id, status or reason)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _get(store: Store, key: str) -> datetime | None:
    row = store.conn.execute("SELECT value FROM dispatcher_state WHERE key = ?", (key,)).fetchone()
    return datetime.fromisoformat(row["value"]) if row else None


def _set(store: Store, key: str, at: datetime) -> None:
    with store.conn:
        store.conn.execute("INSERT INTO dispatcher_state (key, value) VALUES (?, ?)"
                           " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, _iso(at)))


def get_last_tick(store: Store) -> datetime | None:
    """When the previous tick FINISHED (Fix 15). A database written before Fix 15
    only has 'last_tick' (a start time); it is read as the finish time."""
    return _get(store, "last_tick_finished") or _get(store, "last_tick")


def set_last_tick(store: Store, at: datetime, started: datetime | None = None) -> None:
    """Record a tick that started at ``started`` (default ``at``) and finished at ``at``."""
    _set(store, "last_tick_started", started or at)
    _set(store, "last_tick_finished", at)


SNIPPET_CHARS = 60   # Fix 26: the most of a post's text a notification ever carries


def alert_text(item: dict, status: str, reason: str) -> str:
    """Queue id, site, action, status and reason; at most the first 60
    characters of the post text, never all of it."""
    text = " ".join(str((item.get("payload") or {}).get("text") or "").split())
    snippet = text[:SNIPPET_CHARS] + ("…" if len(text) > SNIPPET_CHARS else "")
    reason = " ".join(str(reason).split())
    if len(text) > SNIPPET_CHARS and text in reason:
        reason = reason.replace(text, snippet)
    reason = reason[:200]
    return (f"queue item {item['id']} ({item['site']} {item['action']}) {status}: {reason}"
            + (f' ("{snippet}")' if snippet else ""))


def _scheduled(item: dict) -> datetime | None:
    return datetime.fromisoformat(item["scheduled_at"]) if item["scheduled_at"] else None


def tick(store: Store, publish_fn: Callable[[int], object], *, now: datetime,
         notify: Callable[[str], None], tick_s: float = TICK_S,
         clock: Callable[[], datetime] | None = None, slept: bool = False) -> TickReport:
    """One tick starting at ``now``. Sleep/stop detection uses the gap since the
    previous tick FINISHED (time spent inside a tick, e.g. a slow publish, never
    counts); ``slept`` reports a sleep seen by the run loop's wall-vs-monotonic
    check. ``clock`` gives the finish time (default: the real clock)."""
    report = TickReport(at=_iso(now))
    last = get_last_tick(store)
    resumed = slept or last is None or (now - last).total_seconds() > WAKE_FACTOR * tick_s
    approved = outbox.list_items(store, "approved")
    handled: set[int] = set()

    if resumed:
        report.resumed = True
        on_time_from = now - timedelta(seconds=tick_s)
        for item in approved:
            when = _scheduled(item)
            if when is None or when > on_time_from or (last is not None and when <= last):
                continue  # not due during the gap
            handled.add(item["id"])
            policy = item["missed_policy"]
            due = _iso(when)
            if policy == "skip":
                outbox.transition(store, item["id"], "failed",
                                  last_error=f"missed: due {due}, dispatcher resumed {report.at}")
                report.missed.append((item["id"], "skipped"))
            elif policy == "ask":
                outbox.transition(store, item["id"], "needs_attention",
                                  last_error=f"missed: due {due}, dispatcher resumed {report.at}; "
                                             "approve again to publish or reject")
                notify(f"queue item {item['id']} ({item['site']} {item['action']}) was due {due} and "
                       "missed while the dispatcher was not running; approve it again or reject it")
                report.missed.append((item["id"], "asked"))
            else:  # run: publish once now
                report.missed.append((item["id"], "run"))
                handled.discard(item["id"])

    for item in approved:
        if item["id"] in handled:
            continue
        when = _scheduled(item)
        if when is not None and when > now:
            continue
        # Fix 26: every outcome that needs the human reaches them. A refusal that
        # leaves the item approved (caps, spacing) is retried, not notified.
        try:
            result = publish_fn(item["id"])
        except PublishRefused as e:
            report.not_published.append((item["id"], str(e)))
            after = outbox.get(store, item["id"])
            if after["status"] in ("needs_attention", "failed"):
                notify(alert_text(after, after["status"], after["last_error"] or str(e)))
            continue
        except challenge.LoggedOut as e:   # Fix 22: the item stays approved; retried next tick
            report.not_published.append((item["id"], f"logged out: {e}"))
            notify(alert_text(item, "not published (logged out, stays approved)", str(e)))
            continue
        except challenge.ChallengeTimeout as e:
            report.not_published.append((item["id"], f"challenge: {e}"))
            notify(alert_text(item, "not published (challenge not cleared)", str(e)))
            continue
        except Exception as e:  # one bad item must not stop the dispatcher
            log.exception("publishing queue item %s failed", item["id"])
            report.not_published.append((item["id"], f"error: {type(e).__name__}"))
            notify(alert_text(item, "error", f"{type(e).__name__}; see `agent-surf queue show {item['id']}`"))
            continue
        status = getattr(result, "status", "?")
        if status == "published":
            report.published.append(item["id"])
        else:
            report.not_published.append((item["id"], status))
            after = getattr(result, "item", None) or outbox.get(store, item["id"])
            notify(alert_text(after, status, after.get("last_error") or status))
    finished = (clock or (lambda: datetime.now(timezone.utc)))()
    set_last_tick(store, max(finished, now), started=now)
    return report


def run(store: Store, publish_fn: Callable[[int], object], *, recover_fn: Callable[[], object],
        notify: Callable[[str], None], once: bool = False, tick_s: float = TICK_S,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
        on_tick: Callable[[TickReport], None] | None = None, max_ticks: int | None = None) -> None:
    """Tick every ``tick_s``. Across each inter-tick sleep the wall clock is
    compared with time.monotonic(): on POSIX the monotonic clock stops while the
    machine sleeps, so a large difference means a sleep even if the stored
    finish->start gap were misleading. (On Windows the monotonic clock keeps
    running in sleep; the finish->start gap covers it.)"""
    recover_fn()
    n = 0
    slept = False
    while True:
        report = tick(store, publish_fn, now=now_fn(), notify=notify, tick_s=tick_s, clock=now_fn,
                      slept=slept)
        if on_tick:
            on_tick(report)
        n += 1
        if once or (max_ticks is not None and n >= max_ticks):
            return
        wall0, mono0 = now_fn(), monotonic_fn()
        sleep_fn(tick_s)
        wall_gap = (now_fn() - wall0).total_seconds()
        slept = wall_gap - (monotonic_fn() - mono0) > WAKE_FACTOR * tick_s
