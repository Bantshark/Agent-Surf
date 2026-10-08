"""Fix 7: verify and use the composer the executor used, not a look-alike on the
page (e.g. a timeline's inline composer with the same accessible name)."""

import json
from datetime import datetime, timezone

import pytest

from agent_surf import action_learner, actionmap, executor, outbox, publisher, runner
from agent_surf.actionmap import try_resolve, validate_action_map
from agent_surf.executor import StepFailed, StepRunner

from composekit import FakeBackend, good_map
from test_learner import FakeClient

TEXTBOX = {"role": "textbox", "name": "Post text"}
DIALOG = {"role": "dialog", "name": "Compose post"}
TEXT = "Into the dialog only \U0001f3af"


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(executor, "STEP_TIMEOUT_S", 3.0)
    monkeypatch.setattr(runner, "FIRST_PASS_TIMEOUT_S", 5.0)
    monkeypatch.setattr(publisher, "CONFIRM_TIMEOUT_S", 3.0)
    monkeypatch.setattr(publisher, "READBACK_TIMEOUT_S", 3.0)


def modal_map(path="/compose-modal", composer=True):
    """Shaped like the live X v1 map; with composer=False it is that map exactly."""
    m = good_map()
    m["start"] = {"url_template": "https://compose.test" + path, "requires": ["text"]}
    m["steps"] = [
        {"op": "wait_for", "target": TEXTBOX, "state": "visible"},
        {"op": "click", "target": TEXTBOX},
        {"op": "type", "target": TEXTBOX, "value": "{text}"},
        {"op": "attach", "target": {"role": "button", "name": "Add photos or video"}, "value": "{media}",
         "preview": {"role": "img", "name": "Uploaded thumbnail"}},
        {"op": "wait_for", "target": {"role": "button", "name": "Post"}, "state": "enabled"},
    ]
    m["submit"] = {"target": {"role": "button", "name": "Post"}, "label_allowlist": ["Post"]}
    m["discard"] = [
        {"op": "click", "target": {"role": "button", "name": "Close"}},
        {"op": "click", "target": {"role": "button", "name": "Discard"}},
        {"op": "wait_for", "target": DIALOG if composer else TEXTBOX, "state": "hidden"},
    ]
    if composer:
        m["composer"] = {"target": DIALOG}
    assert validate_action_map(m) == []
    return m


def rehearse_once(page, m):
    run = StepRunner(page, m, {"text": "rehearsal text"})
    run.start()
    run.run_steps()
    run.discard()
    return run


@pytest.mark.parametrize("path,composer", [
    ("/compose-modal", True),           # look-alike visible behind the dialog
    ("/compose-modal-only", True),
    ("/compose-modal-only", False),     # a map learned before this change (no "composer")
])
def test_discard_check_ignores_the_inline_composer_behind(compose, path, composer):
    page, backend = compose()
    run = rehearse_once(page, modal_map(path, composer))
    assert page.raw.url == "https://compose.test/home-inline"
    # The old page-wide check: the inline composer has the same role and name.
    assert try_resolve(page.raw, TEXTBOX) is not None
    assert run.composer_closed()
    assert backend.create_calls == 0


@pytest.mark.parametrize("composer", [True, False])
def test_race_inline_composer_appears_after_modal_closes(compose, composer):
    page, _ = compose()
    run = rehearse_once(page, modal_map("/compose-modal-race", composer))
    page.raw.wait_for_timeout(800)
    assert try_resolve(page.raw, TEXTBOX) is not None  # it did appear
    assert run.composer_closed()


@pytest.mark.parametrize("path,composer", [("/compose-modal", True), ("/compose-modal-only", False)])
def test_rehearsal_passes_and_saves(store, compose, tmp_path, path, composer):
    page, backend = compose()
    m = modal_map(path, composer)
    saved = action_learner.learn_action(store, page, "composetest", "post", client=FakeClient(json.dumps(m)),
                                        model="m", maps_dir=tmp_path / "maps",
                                        start_url="https://compose.test" + path)
    assert saved["version"] == 1 and ("composer" in saved) is composer
    assert backend.create_calls == 0


def test_missing_container_fails_the_type_step_and_types_nothing(compose):
    page, _ = compose()
    m = modal_map()
    m["composer"] = {"target": {"role": "dialog", "name": "Not this one"}}
    run = StepRunner(page, m, {"text": TEXT})
    run.start()
    with pytest.raises(StepFailed, match="step 2: composer container not found"):
        run.run_steps()
    assert page.raw.locator("#editor").inner_text() == "" and page.raw.locator("#inline").inner_text() == ""


def test_container_gone_mid_run_is_a_step_failure(compose):
    page, _ = compose()
    run = StepRunner(page, modal_map(), {"text": TEXT})
    run.start()
    run.run_steps()
    page.raw.evaluate("document.getElementById('modal').remove()")
    with pytest.raises(StepFailed, match="composer container is gone"):
        run.submit_target_ok()


def test_publish_types_into_the_dialog_only(store, compose, tmp_path):
    backend = FakeBackend()
    actionmap.save_action_map(store, tmp_path / "maps", modal_map())
    qid = outbox.add(store, "composetest", "post", {"text": TEXT})
    outbox.approve(store, qid)
    pages = []

    def open_page(site):
        pages.append(compose(backend=backend)[0])
        return pages[-1]

    result = publisher.publish(store, open_page, qid, now=datetime(2026, 10, 8, tzinfo=timezone.utc))
    assert result.status == "published" and result.item["receipt"]["read_back"] is True
    assert backend.posts == {"1000": TEXT} and backend.create_calls == 1
    assert backend.bodies[0]["source"] == "dialog"  # not the look-alike inline composer behind it


def test_composer_key_validation():
    m = modal_map()
    for bad in ({}, {"target": {}}, {"target": DIALOG, "extra": 1}, "dialog", {"target": {"xpath": "//div"}}):
        m["composer"] = bad
        assert validate_action_map(m), bad
