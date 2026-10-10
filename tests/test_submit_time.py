"""Fix 19: the dispatch log records the real submit moment; caps read only the
last 24 h."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from agent_surf import action_learner, actionmap, executor, outbox, publisher, runner
from agent_surf.publisher import PublishRefused

from composekit import FakeBackend, good_map
from test_learner import FakeClient

T0 = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(executor, "STEP_TIMEOUT_S", 2.0)
    monkeypatch.setattr(runner, "FIRST_PASS_TIMEOUT_S", 5.0)
    monkeypatch.setattr(publisher, "CONFIRM_TIMEOUT_S", 3.0)
    monkeypatch.setattr(publisher, "READBACK_TIMEOUT_S", 3.0)


def plain(**changes):
    m = good_map(**changes)
    m["start"]["url_template"] = "https://compose.test/compose-plain"
    return m


def test_slow_relearn_spacing_counts_from_the_submit(store, compose, tmp_path, monkeypatch):
    offset = [0.0]
    real_mono = publisher._monotonic
    monkeypatch.setattr(publisher, "_monotonic", lambda: real_mono() + offset[0])
    real_learn = action_learner.learn_action

    def slow_learn(*a, **kw):
        offset[0] += 600  # the relearn takes ten minutes
        return real_learn(*a, **kw)

    monkeypatch.setattr(action_learner, "learn_action", slow_learn)
    broken = plain()
    broken["steps"][1]["target"] = {"css": "#gone"}
    actionmap.save_action_map(store, tmp_path / "maps", broken)
    backend = FakeBackend()
    ids = []
    for text in ("first", "second"):
        qid = outbox.add(store, "composetest", "post", {"text": text})
        outbox.approve(store, qid)
        ids.append(qid)
    result = publisher.publish(store, lambda site: compose(backend=backend)[0], ids[0],
                               client=FakeClient(json.dumps(plain())), model="m", maps_dir=tmp_path / "maps", now=T0)
    assert result.status == "published"
    (submitted,) = store.conn.execute("SELECT submitted_at FROM dispatch_log WHERE queue_id = ?", (ids[0],)).fetchone()
    at = datetime.fromisoformat(submitted)
    assert T0 + timedelta(seconds=600) <= at < T0 + timedelta(seconds=660)
    # 620 s after the publish STARTED but only ~20 s after the submit: spacing (30 s) holds it back.
    with pytest.raises(PublishRefused, match="min spacing"):
        publisher.publish(store, lambda site: compose(backend=backend)[0], ids[1], now=T0 + timedelta(seconds=620))


def test_caps_only_count_the_last_day(store):
    limits = {"per_hour": 1, "per_day": 1, "min_spacing_s": 30}
    for days in (2, 5):
        store.conn.execute("INSERT INTO dispatch_log (queue_id, site, action, dispatched_at, submitted_at)"
                           " VALUES (1, 'x', 'post', ?, ?)", ((T0 - timedelta(days=days)).isoformat(),) * 2)
    assert publisher.caps_problem(store, "x", "post", limits, T0) is None
    store.conn.execute("INSERT INTO dispatch_log (queue_id, site, action, dispatched_at, submitted_at)"
                       " VALUES (1, 'x', 'post', ?, ?)", ((T0 - timedelta(hours=3)).isoformat(timespec="seconds"),) * 2)
    assert "per-day" in publisher.caps_problem(store, "x", "post", limits, T0)
    plan = " ".join(r[3] for r in store.conn.execute(
        "EXPLAIN QUERY PLAN SELECT submitted_at FROM dispatch_log WHERE site = 'x' AND action = 'post'"
        " AND submitted_at >= '2026'"))
    assert "dispatch_log_caps" in plan
