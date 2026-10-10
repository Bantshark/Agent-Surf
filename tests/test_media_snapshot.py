"""Fix 13: media is snapshotted at approval; publish uploads only the snapshot."""

import json
import os
import stat
from datetime import datetime, timezone

import pytest

from agent_surf import actionmap, executor, outbox, publisher, runner
from agent_surf.publisher import PublishRefused

from composekit import FakeBackend, good_map

PNG_A = b"\x89PNG\r\n\x1a\n approved bytes"
PNG_B = b"\x89PNG\r\n\x1a\n edited after approval"
NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(executor, "STEP_TIMEOUT_S", 3.0)
    monkeypatch.setattr(runner, "FIRST_PASS_TIMEOUT_S", 5.0)
    monkeypatch.setattr(publisher, "CONFIRM_TIMEOUT_S", 3.0)
    monkeypatch.setattr(publisher, "READBACK_TIMEOUT_S", 3.0)


@pytest.fixture
def approved(store, tmp_path):
    original = tmp_path / "photo.png"
    original.write_bytes(PNG_A)
    qid = outbox.add(store, "composetest", "post", {"text": "with a photo", "media": [str(original)]})
    return original, qid, outbox.approve(store, qid)


def test_snapshot_taken_with_private_permissions(store, approved):
    original, qid, item = approved
    (snap,) = item["media_snapshot"]
    assert snap == str(store.media_dir / str(qid) / "0.png")
    assert open(snap, "rb").read() == PNG_A
    assert item["payload"]["media"] == [str(original)]  # originals kept for display
    assert item["content_hash"] == outbox.content_hash(item["payload"], [snap])
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(os.path.dirname(snap)).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(snap).st_mode) == 0o600


def test_editing_the_original_does_not_change_the_upload(store, compose, tmp_path, approved, monkeypatch):
    original, qid, item = approved
    original.write_bytes(PNG_B)
    uploaded = []
    real = executor.StepRunner._attach

    def spy(self, step, where):
        uploaded.extend((p, open(p, "rb").read()) for p in self.payload.get("media") or [])
        return real(self, step, where)

    monkeypatch.setattr(executor.StepRunner, "_attach", spy)
    m = good_map()
    m["start"]["url_template"] = "https://compose.test/compose-plain"
    actionmap.save_action_map(store, tmp_path / "maps", m)
    backend = FakeBackend()
    result = publisher.publish(store, lambda site: compose(backend=backend)[0], qid, now=NOW)
    assert result.status == "published"
    assert uploaded == [(item["media_snapshot"][0], PNG_A)]
    assert not (store.media_dir / str(qid)).exists()                 # removed after the receipt
    assert outbox.get(store, qid)["media_snapshot"] is None


def test_editing_the_snapshot_makes_publish_refuse(store, approved):
    original, qid, item = approved
    open(item["media_snapshot"][0], "wb").write(PNG_B)
    with pytest.raises(PublishRefused, match="content changed"):
        publisher.publish(store, lambda site: None, qid, now=NOW)
    assert outbox.get(store, qid)["status"] == "needs_attention"


def test_snapshot_removed_on_reject(store, approved):
    original, qid, item = approved
    outbox.reject(store, qid)
    assert not os.path.exists(item["media_snapshot"][0]) and original.exists()


def test_reapproval_resnapshots(store, approved):
    original, qid, item = approved
    outbox.transition(store, qid, "needs_attention", last_error="human review")
    original.write_bytes(PNG_B)  # the human chose a new version
    again = outbox.approve(store, qid)
    assert open(again["media_snapshot"][0], "rb").read() == PNG_B
    assert again["content_hash"] != item["content_hash"]
    assert outbox.verify_approved(again) is None


def test_item_approved_before_snapshots_still_verifies(store, approved):
    """Approved by an older version: no snapshot, hash over the original files."""
    original, qid, item = approved
    store.conn.execute("UPDATE queue SET media_snapshot_json = NULL, content_hash = ? WHERE id = ?",
                       (outbox.content_hash(item["payload"]), qid))
    legacy = outbox.get(store, qid)
    assert outbox.publish_payload(legacy)["media"] == [str(original)]
    assert outbox.verify_approved(legacy) is None


def test_snapshot_column_added_to_existing_db(tmp_path):
    import sqlite3
    from agent_surf.store import Store
    db = tmp_path / "surf.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE queue (id INTEGER PRIMARY KEY AUTOINCREMENT, site TEXT NOT NULL, action TEXT NOT NULL,"
                 " payload_json TEXT NOT NULL, content_hash TEXT, status TEXT NOT NULL, scheduled_at TEXT,"
                 " missed_policy TEXT NOT NULL DEFAULT 'ask', approved_at TEXT, attempts INTEGER NOT NULL DEFAULT 0,"
                 " last_error TEXT, receipt_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.execute("INSERT INTO queue (site, action, payload_json, status, created_at, updated_at)"
                 " VALUES ('x', 'post', ?, 'draft', 'then', 'then')", (json.dumps({"text": "kept"}),))
    conn.commit()
    conn.close()
    with Store(db) as s:
        assert outbox.get(s, 1)["payload"] == {"text": "kept"} and outbox.get(s, 1)["media_snapshot"] is None
        assert s.media_dir == tmp_path / "media"
