"""Fix 11: the executor re-checks the site's domain after every op and around
submit; off-site after submit is an unknown outcome (never resubmitted)."""

from datetime import datetime, timezone

import pytest

from agent_surf import actionmap, executor, outbox, publisher, runner, sites
from agent_surf.executor import StepRunner
from agent_surf.publisher import PublishRefused

from composekit import FakeBackend
from test_composer_scope import modal_map

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)
EVIL_CLICK = {"op": "click", "target": {"role": "link", "name": "Add photo"}}


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(executor, "STEP_TIMEOUT_S", 3.0)
    monkeypatch.setattr(runner, "FIRST_PASS_TIMEOUT_S", 5.0)
    monkeypatch.setattr(publisher, "CONFIRM_TIMEOUT_S", 3.0)
    monkeypatch.setattr(publisher, "READBACK_TIMEOUT_S", 3.0)


def evil_link_map():
    m = modal_map("/compose-evil-link")
    m["steps"].insert(3, EVIL_CLICK)  # after typing; "Add photo" is an allowed label
    assert actionmap.validate_action_map(m) == []
    return m


def test_click_that_leaves_the_site_is_refused(compose):
    page, _ = compose()
    run = StepRunner(page, evil_link_map(), {"text": "hello"})
    run.start()
    with pytest.raises(sites.DomainRefused, match="refusing to navigate outside compose.test"):
        run.run_steps()


def publish_with(store, compose, tmp_path, m, backend):
    actionmap.save_action_map(store, tmp_path / "maps", m)
    qid = outbox.add(store, "composetest", "post", {"text": "hello"})
    outbox.approve(store, qid)
    return qid, publisher.publish(store, lambda site: compose(backend=backend)[0], qid, now=NOW)


def test_off_site_before_submit_needs_attention_nothing_published(store, compose, tmp_path):
    backend = FakeBackend()
    qid, result = publish_with(store, compose, tmp_path, evil_link_map(), backend)
    assert result.status == "needs_attention" and "refusing to navigate outside" in result.item["last_error"]
    assert backend.create_calls == 0
    log = store.conn.execute("SELECT submitted_at FROM dispatch_log WHERE queue_id = ?", (qid,)).fetchone()
    assert log["submitted_at"] is None  # never reached submit


def test_off_site_after_submit_is_an_unknown_outcome(store, compose, tmp_path):
    backend = FakeBackend()
    qid, result = publish_with(store, compose, tmp_path, modal_map("/compose-evil-submit"), backend)
    assert result.status == "needs_attention"
    assert "outcome unknown" in result.item["last_error"] and "left the site" in result.item["last_error"]
    with pytest.raises(PublishRefused):  # never resubmitted
        publisher.publish(store, lambda site: compose(backend=backend)[0], qid, now=NOW)
    log = store.conn.execute("SELECT submitted_at FROM dispatch_log WHERE queue_id = ?", (qid,)).fetchone()
    assert log["submitted_at"] is not None
