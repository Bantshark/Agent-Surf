"""Fix 10: action-map clicks are scoped to the composer and label-allowlisted."""

import json
from datetime import datetime, timezone

import pytest

from agent_surf import actionmap, executor, outbox, publisher, runner
from agent_surf.actionmap import validate_action_map
from agent_surf.executor import Refused, StepRunner

from composekit import FakeBackend
from test_composer_scope import DIALOG, TEXTBOX, modal_map
from test_learner import FakeClient

ADD = {"role": "button", "name": "Add photos or video"}


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(executor, "STEP_TIMEOUT_S", 3.0)
    monkeypatch.setattr(runner, "FIRST_PASS_TIMEOUT_S", 5.0)
    monkeypatch.setattr(publisher, "CONFIRM_TIMEOUT_S", 3.0)
    monkeypatch.setattr(publisher, "READBACK_TIMEOUT_S", 3.0)


def scoped(*extra_steps, at=2):
    m = modal_map("/compose-scoped")
    for i, step in enumerate(extra_steps):
        m["steps"].insert(at + i, step)
    return m


def test_click_steps_hit_only_the_composer_copy(compose):
    page, _ = compose()
    run = StepRunner(page, scoped({"op": "click", "target": ADD}), {"text": "hello"})
    run.start()
    run.run_steps()
    assert page.raw.evaluate("window.__twin") is None          # the timeline twin, first in the DOM
    assert page.raw.evaluate("window.__addPhoto") == 1         # the composer's own button


def test_validator_rejects_unlisted_click_names():
    m = scoped({"op": "click", "target": {"role": "button", "name": "Change who can reply"}})
    assert any("may not click 'Change who can reply'" in p for p in validate_action_map(m))
    m = scoped({"op": "click", "target": {"role": "button", "name": "More options"}})
    assert validate_action_map(m)
    m = modal_map("/compose-scoped")
    m["discard"].insert(0, {"op": "click", "target": {"role": "button", "name": "Schedule"}})
    assert any("discard[0]: may not click 'Schedule'" in p for p in validate_action_map(m))
    m = scoped({"op": "click", "target": {"role": "button", "name": "  add PHOTOS  or video "}})
    assert validate_action_map(m) == []  # case and whitespace insensitive


@pytest.mark.parametrize("target,match", [
    ({"css": "#audience"}, "'Change who can reply' \\(not in the click allowlist\\)"),
    ({"css": "#icon-btn"}, "no accessible name"),
])
def test_runtime_label_check_refuses_and_types_nothing(compose, target, match):
    page, _ = compose()
    m = scoped({"op": "click", "target": target}, at=1)  # before the type step
    assert validate_action_map(m) == []                   # no name: only checkable at run time
    run = StepRunner(page, m, {"text": "never typed"})
    run.start()
    with pytest.raises(Refused, match=match):
        run.run_steps()
    assert page.raw.evaluate("[window.__audience, window.__icon]") == [None, None]
    assert page.raw.locator("#editor").inner_text() == ""


def test_discard_can_click_the_page_level_confirm(compose):
    page, backend = compose()
    run = StepRunner(page, modal_map("/compose-scoped"), {"text": "draft"})
    run.start()
    run.run_steps()
    run.discard()  # Close in the composer, Discard in an alertdialog outside it
    assert run.composer_closed() and backend.create_calls == 0


def test_discard_labels_only(compose):
    page, _ = compose()
    m = modal_map("/compose-scoped")
    m["discard"] = [{"op": "click", "target": {"css": "#twin"}}]  # no name: runtime check
    run = StepRunner(page, m, {"text": "draft"})
    run.start()
    run.run_steps()
    with pytest.raises(Refused, match="not in the click allowlist"):
        run.discard()
    assert page.raw.evaluate("window.__twin") is None


def test_refused_click_is_never_self_healed(store, compose, tmp_path):
    m = scoped({"op": "click", "target": {"css": "#audience"}}, at=1)
    actionmap.save_action_map(store, tmp_path / "maps", m)
    qid = outbox.add(store, "composetest", "post", {"text": "hi"})
    outbox.approve(store, qid)
    client = FakeClient(json.dumps(modal_map("/compose-scoped")))
    backend = FakeBackend()
    result = publisher.publish(store, lambda site: compose(backend=backend)[0], qid, client=client, model="m",
                               maps_dir=tmp_path / "maps", now=datetime(2026, 10, 10, tzinfo=timezone.utc))
    assert result.status == "needs_attention" and "refused" in result.item["last_error"]
    assert client.calls == [] and backend.create_calls == 0
