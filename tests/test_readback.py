"""Fix 18: read-back looks at the post's own element; read_back is true |
"page" | false."""

import json
from datetime import datetime, timezone

import pytest

from agent_surf import actionmap, cli, executor, outbox, publisher, runner
from agent_surf.actionmap import validate_action_map
from agent_surf.store import Store

from composekit import FakeBackend, good_map

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(executor, "STEP_TIMEOUT_S", 3.0)
    monkeypatch.setattr(runner, "FIRST_PASS_TIMEOUT_S", 5.0)
    monkeypatch.setattr(publisher, "CONFIRM_TIMEOUT_S", 3.0)
    monkeypatch.setattr(publisher, "READBACK_TIMEOUT_S", 1.5)


def publish(store, compose, tmp_path, mode, **map_changes):
    backend = FakeBackend()
    backend.permalink_mode = mode
    m = good_map(**map_changes)
    m["start"]["url_template"] = "https://compose.test/compose-plain"
    actionmap.save_action_map(store, tmp_path / "maps", m)
    qid = outbox.add(store, "composetest", "post", {"text": "Read me back"})
    outbox.approve(store, qid)
    return publisher.publish(store, lambda site: compose(backend=backend)[0], qid, now=NOW)


def test_post_article_with_id_link_reads_back_true(store, compose, tmp_path):
    result = publish(store, compose, tmp_path, "article")
    assert result.status == "published" and result.item["receipt"]["read_back"] is True


def test_text_only_in_a_sidebar_is_not_true(store, compose, tmp_path):
    result = publish(store, compose, tmp_path, "sidebar")
    assert result.status == "needs_attention" and result.item["receipt"]["read_back"] is False


def test_no_post_element_reads_back_page(store, compose, tmp_path):
    result = publish(store, compose, tmp_path, "noarticle")
    assert result.status == "published" and result.item["receipt"]["read_back"] == "page"


def test_readback_target_from_the_map(store, compose, tmp_path):
    result = publish(store, compose, tmp_path, "article", readback={"target": {"css": "article[data-post-id]"}})
    assert result.item["receipt"]["read_back"] is True
    m = good_map(readback={"target": {}})
    assert any("readback" in p for p in validate_action_map(m))
    assert any("readback" in p for p in validate_action_map(good_map(readback={"target": {"css": "a"}, "x": 1})))


def test_receipts_surface_page(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_SURF_HOME", str(tmp_path / "home"))
    with Store(tmp_path / "home" / "surf.db") as s:
        qid = outbox.add(s, "x", "post", {"text": "t"})
        outbox.approve(s, qid)
        outbox.transition(s, qid, "publishing")
        outbox.transition(s, qid, "published", receipt={"post_id": "1", "permalink": "https://x.com/i/status/1",
                                                        "confirmed_at": "now", "read_back": "page"})
    assert cli.main(["receipts"]) == 0
    assert "read_back=page" in capsys.readouterr().out
    assert cli.main(["receipts", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["read_back"] == "page"
