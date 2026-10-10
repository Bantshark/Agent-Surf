"""v2 Unit 2: the publishing queue."""

import json
import os
import sqlite3

import pytest

from agent_surf import outbox
from agent_surf.outbox import IllegalTransition, QueueError
from agent_surf.store import Store


@pytest.fixture
def img(tmp_path):
    p = tmp_path / "photo.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n synthetic image bytes")
    return p


def test_add_and_approve_records_hash(store, img):
    qid = outbox.add(store, "x", "post", {"text": "Hello \U0001f30d", "media": [str(img)]},
                     scheduled_at="2026-11-01T09:00:00+02:00", missed_policy="skip")
    item = outbox.get(store, qid)
    assert item["status"] == "draft" and item["content_hash"] is None
    assert item["scheduled_at"] == "2026-11-01T07:00:00+00:00" and item["missed_policy"] == "skip"
    assert item["payload"]["media"] == [str(img)]
    item = outbox.approve(store, qid)
    assert item["status"] == "approved" and item["approved_at"]
    assert item["content_hash"] == outbox.content_hash(item["payload"])
    assert outbox.verify_approved(item) is None


def test_hash_covers_payload_and_media_bytes(store, img):
    qid = outbox.add(store, "x", "post", {"text": "t", "media": [str(img)]})
    item = outbox.approve(store, qid)
    img.write_bytes(b"\x89PNG\r\n\x1a\n different bytes")   # the original: no longer matters (Fix 13)
    assert outbox.verify_approved(outbox.get(store, qid)) is None
    (snap,) = item["media_snapshot"]
    open(snap, "wb").write(b"\x89PNG\r\n\x1a\n tampered snapshot")
    assert "content_hash mismatch" in outbox.verify_approved(outbox.get(store, qid))
    open(snap, "wb").write(b"\x89PNG\r\n\x1a\n synthetic image bytes")
    assert outbox.verify_approved(outbox.get(store, qid)) is None
    payload = dict(item["payload"], text="edited after approval")
    store.conn.execute("UPDATE queue SET payload_json = ? WHERE id = ?", (json.dumps(payload), qid))
    assert "content_hash mismatch" in outbox.verify_approved(outbox.get(store, qid))


def test_media_must_exist_and_be_regular(store, img, tmp_path):
    with pytest.raises(QueueError, match="not found"):
        outbox.add(store, "x", "post", {"media": [str(tmp_path / "nope.png")]})
    with pytest.raises(QueueError, match="regular file"):
        outbox.add(store, "x", "post", {"media": [str(tmp_path)]})
    qid = outbox.add(store, "x", "post", {"media": [str(img)]})
    os.remove(img)
    with pytest.raises(QueueError, match="not found"):
        outbox.approve(store, qid)
    assert outbox.get(store, qid)["status"] == "draft"


def test_approved_then_media_removed_is_refused(store, img):
    qid = outbox.add(store, "x", "post", {"media": [str(img)]})
    item = outbox.approve(store, qid)
    os.remove(img)                       # the original: the snapshot is what gets published
    assert outbox.verify_approved(outbox.get(store, qid)) is None
    os.remove(item["media_snapshot"][0])
    assert "media changed" in outbox.verify_approved(outbox.get(store, qid))


@pytest.mark.parametrize("site,action,payload,match", [
    ("x", "post", {}, "text and/or media"),
    ("x", "post", {"text": "  "}, "non-empty"),
    ("x", "like", {"text": "a"}, "unknown action"),
    ("x", "post", {"text": "a", "extra": 1}, "unknown payload"),
    ("x", "reply", {"text": "a"}, "needs --target"),
    ("x", "dm", {"text": "a"}, "needs --thread"),
    ("x", "reply", {"text": "a", "target_url": "https://evil.test/status/1"}, "outside"),
    ("myspace", "post", {"text": "a"}, "unknown site"),
])
def test_add_validation(store, site, action, payload, match):
    with pytest.raises(ValueError, match=match):
        outbox.add(store, site, action, payload)


def test_status_transitions(store):
    qid = outbox.add(store, "x", "post", {"text": "a"})
    with pytest.raises(IllegalTransition):
        outbox.transition(store, qid, "publishing")  # draft cannot publish
    with pytest.raises(IllegalTransition):
        outbox.transition(store, qid, "published")
    outbox.approve(store, qid)
    outbox.transition(store, qid, "publishing", count_attempt=True)
    with pytest.raises(IllegalTransition):
        outbox.approve(store, qid)  # publishing is not approvable
    item = outbox.transition(store, qid, "published", receipt={"post_id": "1"})
    assert item["attempts"] == 1 and item["receipt"] == {"post_id": "1"}
    for status in outbox.STATUSES:
        with pytest.raises(IllegalTransition):
            outbox.transition(store, qid, status)  # published is terminal
    with pytest.raises(IllegalTransition):
        outbox.transition(store, qid, "bogus")


def test_reject_and_reapprove(store):
    a = outbox.add(store, "x", "post", {"text": "a"})
    outbox.reject(store, a)
    with pytest.raises(IllegalTransition):
        outbox.approve(store, a)
    b = outbox.add(store, "x", "post", {"text": "b"})
    outbox.approve(store, b)
    outbox.transition(store, b, "needs_attention", last_error="checked by a human")
    assert outbox.approve(store, b)["status"] == "approved"


def test_approve_all_drafts_and_list(store):
    ids = [outbox.add(store, "x", "post", {"text": f"t{i}"}) for i in range(3)]
    outbox.reject(store, ids[1])
    assert [i["id"] for i in outbox.approve_all_drafts(store)] == [ids[0], ids[2]]
    assert [i["id"] for i in outbox.list_items(store, "approved")] == [ids[0], ids[2]]
    with pytest.raises(QueueError):
        outbox.list_items(store, "nope")


def test_queue_table_added_to_existing_v1_db(tmp_path):
    db = tmp_path / "surf.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE seen (site TEXT, page_type TEXT, item_id TEXT, first_seen TEXT,"
                 " UNIQUE (site, page_type, item_id))")
    conn.execute("INSERT INTO seen VALUES ('x', 'search', '1', 'then')")
    conn.commit()
    conn.close()
    with Store(db) as s:
        assert s.is_seen("x", "search", "1")
        qid = outbox.add(s, "x", "post", {"text": "a"})
        assert outbox.get(s, qid)["status"] == "draft"
