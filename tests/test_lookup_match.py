"""Fix 17: the account lookup never attaches an older post with the same text."""

from datetime import datetime, timedelta, timezone

import pytest

from agent_surf import actionmap, executor, outbox, publisher, runner, sitemap
from agent_surf.publisher import parse_time_value

from composekit import FakeBackend, good_map

TEXT = "hello world"


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(executor, "STEP_TIMEOUT_S", 3.0)
    monkeypatch.setattr(runner, "FIRST_PASS_TIMEOUT_S", 5.0)
    monkeypatch.setattr(publisher, "CONFIRM_TIMEOUT_S", 2.0)
    monkeypatch.setattr(publisher, "READBACK_TIMEOUT_S", 3.0)


def profile_map(with_time):
    fields = {"text": ".text", **({"created_at": "time.created"} if with_time else {})}
    return {"site": "composetest", "page_type": "profile", "version": 1, "source": "dom",
            "dom": {"item": "article[data-post-id]", "id_attr": "data-post-id", "fields": fields},
            "required_fields": ["text"], "scroll": {"max_scrolls": 0, "delay_s": 1.0, "stop_after_seen": 5},
            "limits": {"max_items": 50}}


def ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def run(store, compose, tmp_path, backend, *, with_time, receipted=()):
    m = good_map()
    m["start"]["url_template"] = "https://compose.test/compose-plain"
    actionmap.save_action_map(store, tmp_path / "maps", m)
    store.add_map("composetest", "profile", 1, sitemap.save_map(tmp_path / "maps", profile_map(with_time)), None)
    for pid in receipted:  # earlier items already published with these ids
        old = outbox.add(store, "composetest", "post", {"text": TEXT})
        outbox.approve(store, old)
        outbox.transition(store, old, "publishing")
        outbox.transition(store, old, "published", receipt={"post_id": pid})
    qid = outbox.add(store, "composetest", "post", {"text": TEXT})
    outbox.approve(store, qid)
    return publisher.publish(store, lambda site: compose(backend=backend)[0], qid, now=datetime.now(timezone.utc))


@pytest.mark.parametrize("with_time", [True, False])
def test_older_post_with_a_receipt_is_ignored(store, compose, tmp_path, with_time):
    backend = FakeBackend(mode="drop")
    backend.posts["900"], backend.times["900"] = TEXT, ago(3600)
    result = run(store, compose, tmp_path, backend, with_time=with_time, receipted=["900"])
    assert result.status == "published"
    assert result.item["receipt"]["post_id"] == "1000" and result.item["receipt"]["via"] == "account lookup"
    assert "id not in any receipt" in result.item["receipt"]["match"]


def test_only_the_old_receipted_post_means_not_found(store, compose, tmp_path):
    backend = FakeBackend(mode="lost")
    backend.posts["900"], backend.times["900"] = TEXT, ago(3600)
    result = run(store, compose, tmp_path, backend, with_time=True, receipted=["900"])
    assert result.status == "needs_attention" and "not found on the account" in result.item["last_error"]


def test_newest_of_two_new_matches_is_chosen(store, compose, tmp_path):
    backend = FakeBackend(mode="drop")
    backend.posts["950"], backend.times["950"] = TEXT, ago(60)   # within the 120 s slack
    result = run(store, compose, tmp_path, backend, with_time=True)
    r = result.item["receipt"]
    assert r["post_id"] == "1000" and "created_at >= submit - 120 s" in r["match"] and "newest" in r["match"]


def test_old_post_without_receipt_rejected_by_time(store, compose, tmp_path):
    backend = FakeBackend(mode="lost")
    backend.posts["900"], backend.times["900"] = TEXT, ago(3600)  # same text, an hour old, no receipt
    result = run(store, compose, tmp_path, backend, with_time=True)
    assert result.status == "needs_attention"


def test_no_time_field_only_rule_a_applies(store, compose, tmp_path):
    backend = FakeBackend(mode="lost")
    backend.posts["900"] = TEXT  # same text, not receipted, nothing tells its age
    result = run(store, compose, tmp_path, backend, with_time=False)
    assert result.status == "published" and result.item["receipt"]["post_id"] == "900"
    assert result.item["receipt"]["match"] == "same text, id not in any receipt (no time field)"


@pytest.mark.parametrize("value,expected", [
    ("2026-10-10T12:00:00Z", datetime(2026, 10, 10, 12, tzinfo=timezone.utc)),
    ("2026-10-10T12:00:00", datetime(2026, 10, 10, 12, tzinfo=timezone.utc)),
    ("Sat Oct 10 12:00:00 +0000 2026", datetime(2026, 10, 10, 12, tzinfo=timezone.utc)),
    ("Sat, 10 Oct 2026 12:00:00 +0000", datetime(2026, 10, 10, 12, tzinfo=timezone.utc)),
    (1791633600, datetime(2026, 10, 10, 12, tzinfo=timezone.utc)),
    ("1791633600000", datetime(2026, 10, 10, 12, tzinfo=timezone.utc)),
    ("3 hours ago", None), ("", None), (None, None),
])
def test_time_parsing(value, expected):
    assert parse_time_value(value) == expected
