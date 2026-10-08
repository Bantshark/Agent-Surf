"""Fix 8: per-site media requirement, and rehearsing the text-only path."""

import json
from datetime import datetime, timezone

import pytest

from agent_surf import action_learner, actionmap, executor, outbox, publisher, runner, sites
from agent_surf.actionmap import ActionMapError, validate_action_map

from composekit import FakeBackend, good_map


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(executor, "STEP_TIMEOUT_S", 3.0)
    monkeypatch.setattr(publisher, "CONFIRM_TIMEOUT_S", 3.0)
    monkeypatch.setattr(publisher, "READBACK_TIMEOUT_S", 3.0)
    monkeypatch.setattr(runner, "FIRST_PASS_TIMEOUT_S", 5.0)


def site_map(site, requires, attach=True):
    url, link = {"x": ("https://x.com/compose/post", "https://x.com/i/status/{id}"),
                 "instagram": ("https://www.instagram.com/", "https://www.instagram.com/p/{id}/")}[site]
    m = good_map(site=site, start={"url_template": url, "requires": requires}, permalink_template=link)
    m.pop("lookup")
    if not attach:
        m["steps"] = [s for s in m["steps"] if s["op"] != "attach"]
    return m


def test_media_required_table():
    assert sites.media_required("instagram", "post") is True
    for site, action in [("x", "post"), ("facebook", "post"), ("linkedin", "post"), ("reddit", "post"),
                         ("x", "reply"), ("instagram", "comment"), ("linkedin", "dm"), ("myspace", "post")]:
        assert sites.media_required(site, action) is False


@pytest.mark.parametrize("site,requires,attach,expect", [
    ("x", ["text", "media"], True, "media is optional for x post"),       # the live X v1 map
    ("x", ["text"], True, None),
    ("x", ["text"], False, None),
    ("instagram", ["text"], True, "must include media"),
    ("instagram", ["text", "media"], False, "needs an attach step"),
    ("instagram", ["text", "media"], True, None),
])
def test_validator_media_rule(site, requires, attach, expect):
    problems = validate_action_map(site_map(site, requires, attach))
    if expect is None:
        assert problems == []
    else:
        assert any(expect in p for p in problems), problems


def test_prompt_states_the_media_rule():
    x = action_learner.build_action_prompt("x", "post", "https://x.com/compose/post", "", [])
    ig = action_learner.build_action_prompt("instagram", "post", "https://www.instagram.com/", "", [])
    assert "media: OPTIONAL" in x and "do NOT put" in x
    assert "media: REQUIRED" in ig


@pytest.fixture
def attach_calls(monkeypatch):
    calls = []
    real = executor.StepRunner._attach

    def spy(self, step, where):
        calls.append(list(self.payload.get("media") or []))
        return real(self, step, where)

    monkeypatch.setattr(executor.StepRunner, "_attach", spy)
    return calls


def plain(m):
    m["start"] = dict(m["start"], url_template="https://compose.test/compose-plain")
    return m


def test_rehearsal_runs_text_only_then_media(compose, attach_calls):
    page, backend = compose()
    notes = action_learner.rehearse(page, plain(good_map()), {}, lambda p: None)
    assert attach_calls[0] == [] and len(attach_calls[1]) == 1  # pass 1: no file chosen
    assert "pass 1 (text only): step 2: no media in this item; attach skipped" in notes
    assert "pass 1 (text only): submit target found and enabled: 'Post' (not clicked)" in notes
    assert "pass 2 (with media): step 2: attached 1 file(s)" in notes
    assert sum("submit target found and enabled" in n for n in notes) == 2
    assert backend.create_calls == 0 and not any("/api/create" in u for _, u in page.requests)


def test_no_attach_step_means_one_text_only_pass(compose, attach_calls):
    page, backend = compose()
    m = plain(good_map())
    m["steps"] = [s for s in m["steps"] if s["op"] != "attach"]
    notes = action_learner.rehearse(page, m, {}, lambda p: None)
    assert attach_calls == [] and all(n.startswith("pass 1 (text only)") for n in notes)


def test_media_required_rehearses_only_with_media(compose, attach_calls, monkeypatch):
    monkeypatch.setitem(sites.MEDIA_REQUIRED, ("composetest", "post"), True)
    page, backend = compose()
    m = plain(good_map(start={"url_template": "x", "requires": ["text", "media"]}))
    assert validate_action_map(m) == []
    notes = action_learner.rehearse(page, m, {}, lambda p: None)
    assert len(attach_calls) == 1 and len(attach_calls[0]) == 1
    assert all(n.startswith("media pass (media required)") for n in notes)
    assert backend.create_calls == 0


def test_text_only_pass_must_reach_submit(compose, monkeypatch):
    """A composer whose Post only enables with media cannot pass the text-only
    rehearsal, so media-optional maps are proven to post text alone."""
    monkeypatch.setattr(executor, "TEXT_SETTLE_S", 0.3)
    page, backend = compose()
    m = good_map()
    m["start"] = dict(m["start"], url_template="https://compose.test/compose-media-only")
    with pytest.raises(action_learner.ActionLearnError, match=r"pass 1 \(text only\)"):
        action_learner.rehearse(page, m, {}, lambda p: None)
    assert backend.create_calls == 0


def test_text_only_publish_with_optional_attach_step(store, compose, tmp_path):
    backend = FakeBackend()
    m = plain(good_map())
    assert any(s["op"] == "attach" for s in m["steps"])
    actionmap.save_action_map(store, tmp_path / "maps", m)
    qid = outbox.add(store, "composetest", "post", {"text": "hello world"})
    outbox.approve(store, qid)
    result = publisher.publish(store, lambda site: compose(backend=backend)[0], qid,
                               now=datetime(2026, 10, 8, tzinfo=timezone.utc))
    assert result.status == "published"
    assert result.item["receipt"]["post_id"] == "1000" and result.item["receipt"]["read_back"] is True
    assert backend.bodies == [{"text": "hello world", "media": 0}]


def test_existing_map_requiring_media_for_x_must_be_relearned(store, tmp_path):
    old = dict(site_map("x", ["text", "media"]), version=1)
    path = tmp_path / "x.action-post.v1.json"
    path.write_text(json.dumps(old))
    store.conn.execute("INSERT INTO action_maps VALUES ('x', 'post', 1, ?, 'then')", (str(path),))
    with pytest.raises(ActionMapError, match="media is optional for x post.*learn-action x post"):
        actionmap.current_action_map(store, "x", "post")
