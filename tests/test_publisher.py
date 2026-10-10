"""v2 Unit 6: publish with receipts against the synthetic compose.test site."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from agent_surf import actionmap, executor, outbox, publisher, runner, sitemap
from agent_surf.publisher import PublishRefused

from composekit import FakeBackend, good_map
from test_learner import FakeClient

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
TEXT = "Shipping v2 today \U0001f680"


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(executor, "STEP_TIMEOUT_S", 3.0)
    monkeypatch.setattr(publisher, "CONFIRM_TIMEOUT_S", 3.0)
    monkeypatch.setattr(publisher, "READBACK_TIMEOUT_S", 3.0)
    monkeypatch.setattr(runner, "FIRST_PASS_TIMEOUT_S", 5.0)


def plain_map(**changes):
    m = good_map(**changes)
    m["start"] = dict(m["start"], url_template="https://compose.test/compose-plain")
    return m


@pytest.fixture
def site(store, compose, tmp_path):
    """(backend, open_page, save_map, add_item)"""
    backend = FakeBackend()

    def open_page(site):
        return compose(backend=backend)[0]

    def save_map(m=None):
        actionmap.save_action_map(store, tmp_path / "maps", m or plain_map())

    def add_item(text=TEXT, approve=True, **payload):
        qid = outbox.add(store, "composetest", "post", dict(payload, text=text))
        if approve:
            outbox.approve(store, qid)
        return qid

    return backend, open_page, save_map, add_item


def publish(store, open_page, qid, now=T0, **kw):
    return publisher.publish(store, open_page, qid, now=now, **kw)


def test_publish_with_network_receipt_and_readback(store, site):
    backend, open_page, save_map, add_item = site
    save_map()
    qid = add_item()
    result = publish(store, open_page, qid)
    assert result.status == "published"
    r = result.item["receipt"]
    assert r["post_id"] == "1000" and r["permalink"] == "https://compose.test/post/1000"
    assert r["via"] == "network" and r["read_back"] is True and r["confirmed_at"]
    assert backend.create_calls == 1 and backend.posts["1000"] == TEXT
    row = store.conn.execute("SELECT * FROM dispatch_log WHERE queue_id = ?", (qid,)).fetchone()
    assert row["dispatched_at"] and row["submitted_at"] and row["completed_at"] and row["outcome"] == "published"


@pytest.mark.parametrize("state", ["draft", "rejected"])
def test_refuses_unapproved(store, site, state):
    backend, open_page, save_map, add_item = site
    save_map()
    qid = add_item(approve=False)
    if state == "rejected":
        outbox.reject(store, qid)
    with pytest.raises(PublishRefused, match=f"is {state}"):
        publish(store, open_page, qid)
    assert backend.create_calls == 0


def test_refuses_when_payload_changed_after_approval(store, site):
    backend, open_page, save_map, add_item = site
    save_map()
    qid = add_item()
    payload = dict(outbox.get(store, qid)["payload"], text="swapped after approval")
    store.conn.execute("UPDATE queue SET payload_json = ? WHERE id = ?", (json.dumps(payload), qid))
    with pytest.raises(PublishRefused, match="content changed"):
        publish(store, open_page, qid)
    assert outbox.get(store, qid)["status"] == "needs_attention"
    assert backend.create_calls == 0


def test_refuses_when_media_changed_after_approval(store, site, tmp_path):
    backend, open_page, save_map, add_item = site
    save_map()
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n one")
    qid = add_item(media=[str(img)])
    open(outbox.get(store, qid)["media_snapshot"][0], "wb").write(b"\x89PNG\r\n\x1a\n two")
    with pytest.raises(PublishRefused, match="content changed"):
        publish(store, open_page, qid)
    assert outbox.get(store, qid)["status"] == "needs_attention"
    assert backend.create_calls == 0


def test_200_with_errors_is_not_published(store, site):
    backend, open_page, save_map, add_item = site
    backend.mode = "errors"
    save_map()
    qid = add_item()
    result = publish(store, open_page, qid)
    assert result.status == "needs_attention" and result.item["receipt"] is None
    assert "platform returned errors" in result.item["last_error"]


def test_dom_fallback_receipt(store, site):
    backend, open_page, save_map, add_item = site
    save_map(plain_map(confirm={"dom": {"target": {"role": "link", "name": "View post"}}}))
    result = publish(store, open_page, add_item())
    assert result.status == "published"
    assert result.item["receipt"]["via"] == "dom" and result.item["receipt"]["post_id"] == "1000"


def test_readback_mismatch_needs_attention(store, site):
    backend, open_page, save_map, add_item = site
    backend.permalink_override = "Something else entirely"
    save_map()
    result = publish(store, open_page, add_item())
    assert result.status == "needs_attention"
    assert result.item["receipt"]["read_back"] is False and result.item["receipt"]["post_id"] == "1000"
    assert "does not show the approved text" in result.item["last_error"]


PROFILE_MAP = {
    "site": "composetest", "page_type": "profile", "version": 1, "source": "dom",
    "dom": {"item": "article[data-post-id]", "id_attr": "data-post-id", "fields": {"text": ".text"}},
    "required_fields": ["text"], "scroll": {"max_scrolls": 0, "delay_s": 1.0, "stop_after_seen": 5},
    "limits": {"max_items": 50},
}


def save_profile_map(store, tmp_path):
    store.add_map("composetest", "profile", 1, sitemap.save_map(tmp_path / "maps", PROFILE_MAP), None)


def test_unknown_outcome_found_on_account_no_double_post(store, site, tmp_path):
    backend, open_page, save_map, add_item = site
    backend.mode = "drop"  # created server-side, response lost
    save_map()
    save_profile_map(store, tmp_path)
    qid = add_item()
    result = publish(store, open_page, qid)
    assert result.status == "published" and result.item["receipt"]["via"] == "account lookup"
    assert result.item["receipt"]["post_id"] == "1000"
    with pytest.raises(PublishRefused):
        publish(store, open_page, qid, now=T0 + timedelta(hours=1))
    assert backend.create_calls == 1


def test_unknown_outcome_not_found_is_never_retried(store, site):
    backend, open_page, save_map, add_item = site
    backend.mode = "drop"
    save_map()  # no reading map for the lookup page
    qid = add_item()
    result = publish(store, open_page, qid)
    assert result.status == "needs_attention" and "outcome unknown" in result.item["last_error"]
    with pytest.raises(PublishRefused):
        publish(store, open_page, qid, now=T0 + timedelta(hours=1))
    assert backend.create_calls == 1


def test_caps_and_min_spacing(store, site):
    backend, open_page, save_map, add_item = site
    save_map(plain_map(limits={"per_hour": 2, "per_day": 3, "min_spacing_s": 30}))
    a, b, c, d = (add_item(text=f"post {i}") for i in range(4))
    assert publish(store, open_page, a, now=T0).status == "published"
    with pytest.raises(PublishRefused, match="min spacing"):
        publish(store, open_page, b, now=T0 + timedelta(seconds=10))
    assert outbox.get(store, b)["status"] == "approved"  # untouched, can go later
    assert publish(store, open_page, b, now=T0 + timedelta(seconds=40)).status == "published"
    with pytest.raises(PublishRefused, match="per-hour"):
        publish(store, open_page, c, now=T0 + timedelta(minutes=5))
    assert publish(store, open_page, c, now=T0 + timedelta(hours=2)).status == "published"
    with pytest.raises(PublishRefused, match="per-day"):
        publish(store, open_page, d, now=T0 + timedelta(hours=4))
    assert backend.create_calls == 3


def test_self_heal_relearns_once_then_publishes(store, site, tmp_path):
    backend, open_page, save_map, add_item = site
    broken = plain_map()
    broken["steps"][1]["target"] = {"css": "#gone"}
    save_map(broken)
    client = FakeClient(json.dumps(plain_map()))
    result = publish(store, open_page, add_item(), client=client, model="m", maps_dir=tmp_path / "maps")
    assert result.status == "published"
    assert len(client.calls) == 1 and "stopped working" in client.calls[0]["messages"][0]["content"]
    assert actionmap.current_action_map(store, "composetest", "post")["version"] == 2
    assert backend.create_calls == 1


def test_self_heal_at_most_once(store, site, tmp_path):
    backend, open_page, save_map, add_item = site
    broken = plain_map()
    broken["steps"][1]["target"] = {"css": "#gone"}
    save_map(broken)
    client = FakeClient(json.dumps(broken), json.dumps(broken))
    result = publish(store, open_page, add_item(), client=client, model="m", maps_dir=tmp_path / "maps")
    assert result.status == "needs_attention" and "relearn failed" in result.item["last_error"]
    assert len(client.calls) == 1 and backend.create_calls == 0


def test_recover_interrupted_publish(store, site, tmp_path):
    backend, open_page, save_map, add_item = site
    save_map()
    save_profile_map(store, tmp_path)
    qid = add_item()
    outbox.transition(store, qid, "publishing")
    backend.posts["777"] = TEXT  # it did go out before the crash
    (result,) = publisher.recover_publishing(store, open_page)
    assert result.status == "published" and result.item["receipt"]["post_id"] == "777"
    assert backend.create_calls == 0
