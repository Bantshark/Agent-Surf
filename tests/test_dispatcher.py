"""v2 Unit 7: dispatcher ticks, wake detection, missed policies, restart safety."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent_surf import actionmap, dispatcher, executor, outbox, publisher
from agent_surf.publisher import PublishRefused

from composekit import FakeBackend, good_map

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


class FakePublisher:
    def __init__(self, store, refuse=()):
        self.store, self.refuse, self.calls = store, set(refuse), []

    def __call__(self, qid):
        self.calls.append(qid)
        if qid in self.refuse:
            raise PublishRefused("per-hour cap reached")
        outbox.transition(self.store, qid, "publishing")
        return SimpleNamespace(status="published", item=outbox.transition(self.store, qid, "published",
                                                                            receipt={"post_id": str(qid)}))


def item(store, at=None, policy="ask", approve=True):
    qid = outbox.add(store, "x", "post", {"text": f"t{at}"}, scheduled_at=at and at.isoformat(),
                     missed_policy=policy)
    if approve:
        outbox.approve(store, qid)
    return qid


def tick(store, pub, now, notes=None):
    return dispatcher.tick(store, pub, now=now, notify=(notes.append if notes is not None else print))


def test_due_items_run_future_and_unapproved_wait(store):
    dispatcher.set_last_tick(store, T0 - timedelta(seconds=30))
    due = item(store, T0 - timedelta(seconds=5))
    unscheduled = item(store)
    later = item(store, T0 + timedelta(minutes=5))
    draft = item(store, T0 - timedelta(seconds=5), approve=False)
    pub = FakePublisher(store)
    report = tick(store, pub, T0)
    assert not report.resumed
    assert sorted(pub.calls) == sorted([due, unscheduled]) and sorted(report.published) == sorted(pub.calls)
    assert outbox.get(store, later)["status"] == "approved" and outbox.get(store, draft)["status"] == "draft"
    report = tick(store, pub, T0 + timedelta(minutes=5, seconds=10))
    assert report.published == [later]


@pytest.mark.parametrize("policy,status,published", [
    ("skip", "failed", False), ("run", "published", True), ("ask", "needs_attention", False)])
def test_wake_gap_applies_missed_policy(store, policy, status, published):
    dispatcher.set_last_tick(store, T0)
    missed = item(store, T0 + timedelta(minutes=10), policy=policy)
    on_time = item(store, T0 + timedelta(hours=2) - timedelta(seconds=10), policy="skip")
    pub, notes = FakePublisher(store), []
    report = tick(store, pub, T0 + timedelta(hours=2), notes)  # slept two hours
    assert report.resumed
    assert outbox.get(store, missed)["status"] == status
    assert (missed in pub.calls) is published
    assert outbox.get(store, on_time)["status"] == "published"  # due inside the last tick: not missed
    assert len(notes) == (1 if policy == "ask" else 0)
    if policy != "run":
        assert "missed" in outbox.get(store, missed)["last_error"]


def test_normal_ticks_never_mark_missed(store):
    pub = FakePublisher(store)
    dispatcher.set_last_tick(store, T0)
    qid = item(store, T0 + timedelta(seconds=20), policy="skip")
    assert not tick(store, pub, T0 + timedelta(seconds=30)).resumed
    assert outbox.get(store, qid)["status"] == "published"


def test_first_ever_tick_treats_overdue_items_as_missed(store):
    qid = item(store, T0 - timedelta(days=1), policy="skip")
    report = tick(store, FakePublisher(store), T0)
    assert report.resumed and outbox.get(store, qid)["status"] == "failed"


def test_cap_refusal_leaves_item_for_a_later_tick(store):
    dispatcher.set_last_tick(store, T0 - timedelta(seconds=30))
    qid = item(store, T0 - timedelta(seconds=1))
    pub = FakePublisher(store, refuse={qid})
    report = tick(store, pub, T0)
    assert report.not_published == [(qid, "per-hour cap reached")]
    assert outbox.get(store, qid)["status"] == "approved"
    pub.refuse.clear()
    assert tick(store, pub, T0 + timedelta(seconds=30)).published == [qid]


def test_state_survives_restart(store, tmp_path):
    from agent_surf.store import Store
    db = tmp_path / "surf.db"
    with Store(db) as s:
        dispatcher.run(s, FakePublisher(s), recover_fn=lambda: None, notify=print, once=True, now_fn=lambda: T0)
    with Store(db) as s:  # a new process
        assert dispatcher.get_last_tick(s) == T0
        qid = item(s, T0 + timedelta(minutes=1), policy="skip")
        report = dispatcher.tick(s, FakePublisher(s), now=T0 + timedelta(minutes=30), notify=print)
        assert report.resumed and outbox.get(s, qid)["status"] == "failed"


class Clock:
    """Wall and monotonic time that advance only when the loop sleeps."""

    def __init__(self, start=T0):
        self.wall, self.mono = start, 1000.0

    def now(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def sleep(self, s, *, suspended=0.0):
        self.wall += timedelta(seconds=s + suspended)
        self.mono += s  # POSIX: the monotonic clock stops while suspended


def test_run_loop_recovers_first_and_sleeps_between_ticks(store):
    order, sleeps, clock = [], [], Clock()

    def sleep(s):
        sleeps.append(s)
        clock.sleep(s)

    dispatcher.run(store, FakePublisher(store), recover_fn=lambda: order.append("recover"), notify=print,
                   now_fn=clock.now, monotonic_fn=clock.monotonic, sleep_fn=sleep,
                   on_tick=lambda r: order.append((r.at, r.resumed)), max_ticks=3)
    assert order[0] == "recover" and len(order) == 4
    assert [resumed for _, resumed in order[1:]] == [True, False, False]  # first tick ever, then normal
    assert sleeps == [30.0, 30.0]


def test_dispatch_publishes_through_the_real_publisher(store, compose, tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "STEP_TIMEOUT_S", 3.0)
    backend = FakeBackend()
    m = good_map()
    m["start"]["url_template"] = "https://compose.test/compose-plain"
    actionmap.save_action_map(store, tmp_path / "maps", m)
    qid = outbox.add(store, "composetest", "post", {"text": "scheduled hello"},
                     scheduled_at=(T0 - timedelta(seconds=5)).isoformat())
    outbox.approve(store, qid)
    dispatcher.set_last_tick(store, T0 - timedelta(seconds=30))

    def publish_fn(i):
        return publisher.publish(store, lambda site: compose(backend=backend)[0], i, now=T0)

    report = dispatcher.tick(store, publish_fn, now=T0, notify=print)
    assert report.published == [qid] and backend.create_calls == 1
    log = store.conn.execute("SELECT dispatched_at, completed_at FROM dispatch_log WHERE queue_id = ?",
                             (qid,)).fetchone()
    assert log["dispatched_at"] and log["completed_at"]


# Fix 15: a slow tick must not look like a sleep.

def test_slow_tick_does_not_mark_the_next_item_missed(store):
    clock = Clock()
    dispatcher.set_last_tick(store, T0 - timedelta(seconds=30))
    a = item(store, T0 - timedelta(seconds=1), policy="skip")
    b = item(store, T0 + timedelta(seconds=60), policy="skip")  # comes due during the slow tick

    def slow_publish(qid):
        clock.wall += timedelta(seconds=5 * dispatcher.TICK_S)  # e.g. a relearn: 150 s inside the tick
        return FakePublisher(store)(qid)

    report = dispatcher.tick(store, slow_publish, now=clock.now(), notify=print, clock=clock.now)
    assert report.published == [a]
    assert dispatcher.get_last_tick(store) == T0 + timedelta(seconds=150)  # the finish time
    clock.sleep(30)
    report = dispatcher.tick(store, FakePublisher(store), now=clock.now(), notify=print, clock=clock.now)
    assert not report.resumed and report.missed == [] and report.published == [b]


def test_genuine_gap_after_finish_is_resumed(store):
    clock = Clock()
    dispatcher.set_last_tick(store, T0, started=T0 - timedelta(seconds=150))
    qid = item(store, T0 + timedelta(minutes=5), policy="skip")
    report = dispatcher.tick(store, FakePublisher(store), now=T0 + timedelta(minutes=20), notify=print,
                             clock=clock.now)
    assert report.resumed and outbox.get(store, qid)["status"] == "failed"


def test_suspend_seen_by_wall_vs_monotonic_clock(store):
    clock = Clock()
    pub = FakePublisher(store)
    seen = []
    calls = {"n": 0}

    def sleep(s):
        calls["n"] += 1
        clock.sleep(s, suspended=3600 if calls["n"] == 1 else 0)

    dispatcher.run(store, pub, recover_fn=lambda: None, notify=print, now_fn=clock.now,
                   monotonic_fn=clock.monotonic, sleep_fn=sleep, on_tick=lambda r: seen.append(r.resumed),
                   max_ticks=3)
    assert seen == [True, True, False]


def test_legacy_last_tick_key_is_read_as_finish(store):
    store.conn.execute("INSERT INTO dispatcher_state (key, value) VALUES ('last_tick', ?)", (T0.isoformat(),))
    assert dispatcher.get_last_tick(store) == T0
    assert not dispatcher.tick(store, FakePublisher(store), now=T0 + timedelta(seconds=30), notify=print).resumed


def test_slept_flag_alone_marks_resumed(store):
    dispatcher.set_last_tick(store, T0)
    normal = dispatcher.tick(store, FakePublisher(store), now=T0 + timedelta(seconds=30), notify=print)
    assert not normal.resumed
    flagged = dispatcher.tick(store, FakePublisher(store), now=T0 + timedelta(seconds=60), notify=print,
                              slept=True)
    assert flagged.resumed
