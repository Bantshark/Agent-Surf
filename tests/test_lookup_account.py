"""Fix 16: unknown-outcome recovery works when the account is known: account
handles, auto-filled lookups, set-lookup as a new version, explicit messages."""

import json
from datetime import datetime, timezone

import pytest

from agent_surf import accounts, action_learner, actionmap, cli, executor, outbox, publisher, runner, sitemap
from agent_surf.store import Store

from composekit import FakeBackend, good_map
from test_learner import FakeClient

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)
PROFILE_MAP = {
    "site": "composetest", "page_type": "profile", "version": 1, "source": "dom",
    "dom": {"item": "article[data-post-id]", "id_attr": "data-post-id", "fields": {"text": ".text"}},
    "required_fields": ["text"], "scroll": {"max_scrolls": 0, "delay_s": 1.0, "stop_after_seen": 5},
    "limits": {"max_items": 50},
}


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(executor, "STEP_TIMEOUT_S", 3.0)
    monkeypatch.setattr(runner, "FIRST_PASS_TIMEOUT_S", 5.0)
    monkeypatch.setattr(publisher, "CONFIRM_TIMEOUT_S", 2.0)
    monkeypatch.setattr(publisher, "READBACK_TIMEOUT_S", 3.0)


def plain(**changes):
    m = good_map(**changes)
    m["start"] = dict(m["start"], url_template="https://compose.test/compose-plain")
    return m


def test_account_handles(store):
    assert accounts.get_handle(store, "x") is None
    assert accounts.set_handle(store, "x", " @me_123 ") == "me_123"
    assert accounts.set_handle(store, "x", "other") == "other"
    assert [a["handle"] for a in accounts.list_accounts(store)] == ["other"]
    for bad in ("", "https://x.com/me", "two words", "a/b"):
        with pytest.raises(ValueError):
            accounts.set_handle(store, "x", bad)
    with pytest.raises(ValueError):
        accounts.set_handle(store, "myspace", "me")


def learn(store, page, m, tmp_path):
    reply = json.dumps({k: v for k, v in m.items() if k != "lookup"})
    return action_learner.learn_action(store, page, "composetest", "post", client=FakeClient(reply), model="m",
                                       maps_dir=tmp_path / "maps", start_url="https://compose.test/compose-plain")


def test_lookup_auto_filled_when_handle_set(store, compose, tmp_path, caplog):
    page, _ = compose()
    m = learn(store, page, plain(), tmp_path)
    assert "lookup" not in m
    assert "no working lookup: no lookup configured" in caplog.text
    accounts.set_handle(store, "composetest", "me")
    page, _ = compose()
    m = learn(store, page, plain(), tmp_path)
    assert m["lookup"] == {"page_type": "profile", "handle": "me"} and m["version"] == 2
    assert "has no reading map (learn: agent-surf learn composetest profile --handle me)" in caplog.text


def test_dm_maps_never_get_a_lookup(store):
    accounts.set_handle(store, "composetest", "me")
    m = {"site": "composetest", "action": "dm", "lookup": {"page_type": "profile", "handle": "me"}}
    action_learner.fill_lookup(store, m)
    assert "lookup" not in m


def test_lookup_validation_needs_the_placeholder():
    assert any("needs handle" in p for p in actionmap.validate_action_map(plain(lookup={"page_type": "profile"})))
    assert any("does not take query" in p for p in actionmap.validate_action_map(
        plain(lookup={"page_type": "profile", "handle": "me", "query": "q"})))


def test_set_lookup_writes_a_new_validated_version(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("AGENT_SURF_HOME", str(home))
    with Store(home / "surf.db") as s:
        actionmap.save_action_map(s, home / "maps", plain(lookup=None))
    assert cli.main(["actions", "set-lookup", "composetest", "post", "--page-type", "profile",
                     "--handle", "me", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["version"] == 2 and out["lookup"] == {"page_type": "profile", "handle": "me"}
    assert (home / "maps" / "composetest.action-post.v1.json").exists()  # never edited in place
    assert "lookup" not in json.loads((home / "maps" / "composetest.action-post.v1.json").read_text())
    assert cli.main(["actions", "set-lookup", "composetest", "post", "--page-type", "timeline"]) == 1
    assert cli.main(["actions", "set-lookup", "composetest", "post", "--page-type", "profile"]) == 1  # needs handle
    with Store(home / "surf.db") as s:
        assert actionmap.current_action_map(s, "composetest", "post")["version"] == 2
    assert cli.main(["account", "set", "composetest", "--handle", "@me", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"site": "composetest", "handle": "me"}
    assert cli.main(["account", "show", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["handle"] == "me"


def unknown_outcome(store, compose, tmp_path, *, profile_map):
    backend = FakeBackend(mode="drop")  # created, response lost
    actionmap.save_action_map(store, tmp_path / "maps", plain())
    if profile_map:
        store.add_map("composetest", "profile", 1, sitemap.save_map(tmp_path / "maps", PROFILE_MAP), None)
    qid = outbox.add(store, "composetest", "post", {"text": "did it go out?"})
    outbox.approve(store, qid)
    return backend, publisher.publish(store, lambda site: compose(backend=backend)[0], qid, now=NOW)


def test_missing_reading_map_is_explicit(store, compose, tmp_path):
    backend, result = unknown_outcome(store, compose, tmp_path, profile_map=False)
    assert result.status == "needs_attention"
    assert "learn: agent-surf learn composetest profile --handle me" in result.item["last_error"]
    assert backend.create_calls == 1


def test_unknown_outcome_found_via_account_lookup(store, compose, tmp_path):
    backend, result = unknown_outcome(store, compose, tmp_path, profile_map=True)
    assert result.status == "published" and result.item["receipt"]["via"] == "account lookup"
    assert result.item["receipt"]["post_id"] == "1000" and backend.create_calls == 1


def test_recover_reports_missing_lookup(store, tmp_path):
    actionmap.save_action_map(store, tmp_path / "maps", plain(lookup=None))
    qid = outbox.add(store, "composetest", "post", {"text": "x"})
    outbox.approve(store, qid)
    outbox.transition(store, qid, "publishing")
    (result,) = publisher.recover_publishing(store, lambda site: None)
    assert result.status == "needs_attention" and "actions set-lookup composetest post" in result.item["last_error"]
