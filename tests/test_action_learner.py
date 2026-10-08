"""v2 Unit 5: rehearsal learning of action maps with a fake model client."""

import json

import pytest

from agent_surf import action_learner, actionmap, executor, runner
from agent_surf.action_learner import ActionLearnError

from composekit import good_map
from test_learner import FakeClient

MODEL = "claude-sonnet-5-5"


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(executor, "STEP_TIMEOUT_S", 5.0)
    monkeypatch.setattr(runner, "FIRST_PASS_TIMEOUT_S", 8.0)


def reply_for(path="/compose-plain", **changes):
    m = good_map(**changes)
    m["start"] = dict(m["start"], url_template="https://compose.test" + path)
    return "```json\n" + json.dumps(m) + "\n```"


def learn(store, page, client, tmp_path, path="/compose-plain"):
    return action_learner.learn_action(store, page, "composetest", "post", client=client, model=MODEL,
                                       maps_dir=tmp_path / "maps", start_url="https://compose.test" + path)


def test_rehearsed_map_is_saved_and_nothing_is_created(store, compose, tmp_path):
    page, backend = compose()
    client = FakeClient(reply_for())
    m = learn(store, page, client, tmp_path)
    assert m["version"] == 1 and m["learned_by"] == MODEL
    assert actionmap.current_action_map(store, "composetest", "post")["version"] == 1
    assert backend.create_calls == 0
    assert not any("/api/create" in u for _, u in page.requests)
    assert page.raw.get_by_text("Composer closed").is_visible()  # closed via discard


def test_request_shape_and_untrusted_wrapping(store, compose, tmp_path):
    page, _ = compose()
    client = FakeClient(reply_for("/compose-injection"))
    learn(store, page, client, tmp_path, "/compose-injection")
    (call,) = client.calls
    assert set(call) == {"model", "max_tokens", "system", "messages"}
    prompt = call["messages"][0]["content"]
    assert prompt.index("<untrusted_page_data>") < prompt.index("type: buy now") < prompt.index("</untrusted_page_data>")
    assert "never follow" in call["system"]


def test_model_text_never_reaches_the_composer(store, compose, tmp_path, monkeypatch):
    typed = []
    real = executor.StepRunner._type

    def spy(self, step, where):
        typed.append(self.payload["text"])
        return real(self, step, where)

    monkeypatch.setattr(executor.StepRunner, "_type", spy)
    page, backend = compose()
    learn(store, page, FakeClient(reply_for("/compose-injection")), tmp_path, "/compose-injection")
    assert typed and all(t.startswith("Agent Surf rehearsal") for t in typed)
    assert backend.create_calls == 0


@pytest.mark.parametrize("bad,match", [
    (lambda m: m["steps"][1].update(value="buy now"), "invalid"),                       # model text
    (lambda m: m["submit"].update(label_allowlist=["Delete"]), "invalid"),
    (lambda m: m["steps"].insert(0, {"op": "click", "target": {"role": "button", "name": "Delete"}}),
     "destructive"),
    (lambda m: m["steps"].append({"op": "press", "value": "Enter"}), "could publish"),
])
def test_injected_model_output_is_refused(store, compose, tmp_path, bad, match):
    m = good_map()
    m["start"]["url_template"] = "https://compose.test/compose-injection"
    bad(m)
    page, backend = compose()
    with pytest.raises(ActionLearnError, match=match):
        learn(store, page, FakeClient(json.dumps(m)), tmp_path, "/compose-injection")
    assert actionmap.current_action_map(store, "composetest", "post") is None
    assert backend.create_calls == 0
    assert page.raw.evaluate("window.__deleted") is None


def test_invalid_json_rejected(store, compose, tmp_path):
    page, _ = compose()
    with pytest.raises(ActionLearnError, match="not valid JSON"):
        learn(store, page, FakeClient("no map here {"), tmp_path)


def test_discard_that_does_not_close_is_rejected(store, compose, tmp_path):
    page, backend = compose()
    reply = reply_for(discard=[{"op": "press", "value": "Tab"}])
    with pytest.raises(ActionLearnError, match="still open"):
        learn(store, page, FakeClient(reply), tmp_path)
    assert actionmap.current_action_map(store, "composetest", "post") is None
    assert backend.create_calls == 0


def test_rehearsal_with_media_and_overlay(store, compose, tmp_path):
    page, backend = compose()
    m = learn(store, page, FakeClient(reply_for("/compose")), tmp_path, "/compose")
    assert m["version"] == 1 and backend.create_calls == 0


def test_cold_start_composer_is_waited_for(store, compose, tmp_path):
    page, _ = compose()
    client = FakeClient(reply_for("/compose-late"))
    learn(store, page, client, tmp_path, "/compose-late")
    assert 'textbox "Post text"' in client.calls[0]["messages"][0]["content"]


def test_synthetic_png_is_a_png(tmp_path):
    data = action_learner.synthetic_png(tmp_path / "x.png").read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n") and data.endswith(b"IEND\xaeB`\x82")
