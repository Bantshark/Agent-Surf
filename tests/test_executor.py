"""v2 Unit 4: the action executor against the synthetic compose.test site."""

import pytest

from agent_surf import executor, sites
from agent_surf.actionmap import Resolved
from agent_surf.executor import Refused, StepFailed, StepRunner, Submitter

from composekit import good_map

TEXT = "Hello from Agent Surf \U0001f30d"


def mapped(path, **changes):
    m = good_map(**changes)
    m["start"] = dict(m["start"], url_template="https://compose.test" + path)
    return m


def editor_text(page):
    return page.raw.locator("#editor").inner_text()


def submit_enabled(page):
    return page.raw.locator("#submit").is_enabled()


@pytest.fixture(autouse=True)
def quick_steps(monkeypatch):
    monkeypatch.setattr(executor, "STEP_TIMEOUT_S", 5.0)


def test_input_editor_takes_fill(compose):
    page, backend = compose()
    run = StepRunner(page, mapped("/compose-plain"), {"text": TEXT})
    run.start()
    run.run_steps()
    assert executor.normalize_text(editor_text(page)) == executor.normalize_text(TEXT)
    assert submit_enabled(page)
    assert any("via fill" in n for n in run.notes)
    run.discard()
    assert page.raw.get_by_text("Composer closed").is_visible()
    assert backend.create_calls == 0


def test_keyboard_only_editor_falls_back_to_keyboard_type(compose):
    page, _ = compose()
    run = StepRunner(page, mapped("/compose-keys"), {"text": "plain ascii text"})
    run.start()
    run.run_steps()
    assert editor_text(page) == "plain ascii text" and submit_enabled(page)
    assert any("via keyboard.type" in n for n in run.notes)


class FakeKeyboard:
    def __init__(self, loc):
        self.loc = loc

    def press(self, key):
        if key == "Delete":
            self.loc.dom = ""

    def type(self, text):
        pass  # this editor ignores key events

    def insert_text(self, text):
        self.loc.dom = self.loc.state = text


class FakeLoc:
    def __init__(self):
        self.dom = self.state = ""

    def fill(self, text):
        self.dom = text  # DOM only; internal state untouched

    def inner_text(self):
        return self.dom

    def focus(self):
        pass


def test_insert_text_is_the_last_fallback(monkeypatch):
    loc = FakeLoc()

    class Raw:
        keyboard = FakeKeyboard(loc)

        def wait_for_timeout(self, ms):
            pass

        def route(self, *a):
            pass

    class Page:
        raw = Raw()
        site = None

    monkeypatch.setattr(executor, "TEXT_SETTLE_S", 0.05)
    run = StepRunner(Page(), good_map(confirm={"dom": {"target": {"css": "x"}}}), {"text": "abc"})
    monkeypatch.setattr(run, "_resolve", lambda target, where, **kw: Resolved(loc, "css"))
    monkeypatch.setattr(run, "_submit_ready", lambda: loc.state == "abc")
    run._type({"op": "type", "target": {"css": "#e"}, "value": "{text}"}, 1)
    assert loc.state == "abc"
    assert any("via insertText" in n for n in run.notes)


def test_overlay_intercepting_first_click_is_dismissed(compose):
    page, _ = compose()
    run = StepRunner(page, mapped("/compose"), {"text": TEXT})
    run.start()
    run.run_steps()
    assert any("intercepted" in n for n in run.notes)
    assert page.raw.locator("#overlay").count() == 0
    assert submit_enabled(page)


@pytest.fixture
def png(tmp_path):
    p = tmp_path / "photo.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n synthetic")
    return str(p)


@pytest.mark.parametrize("path,target", [
    ("/compose-plain", {"css": "input[type=file]"}),                  # hidden input
    ("/compose-chooser", {"role": "button", "name": "Add photo"}),    # file chooser
])
def test_media_attach_waits_for_thumbnail(compose, png, path, target):
    page, _ = compose()
    m = mapped(path)
    m["steps"][2]["target"] = target
    run = StepRunner(page, m, {"text": TEXT, "media": [png]})
    run.start()
    run.run_steps()
    assert page.raw.get_by_role("img", name="Uploaded thumbnail").is_visible()


def test_op_whitelist_enforced_at_runtime(compose):
    page, _ = compose()
    m = mapped("/compose-plain")
    m["steps"].append({"op": "evaluate", "target": {"css": "body"}})
    run = StepRunner(page, m, {"text": TEXT})
    run.start()
    with pytest.raises(Refused, match="not allowed"):
        run.run_steps()
    m = mapped("/compose-plain")
    m["discard"] = [{"op": "type", "target": {"css": "#editor"}, "value": "{text}"}]
    with pytest.raises(Refused, match="not allowed"):
        StepRunner(page, m, {"text": TEXT}).discard()


def test_press_outside_whitelist_refused(compose):
    page, _ = compose("/compose-plain")
    run = StepRunner(page, mapped("/compose-plain"), {"text": TEXT})
    with pytest.raises(Refused):
        run._run_step({"op": "press", "value": "Control+Enter"}, 9, executor.actionmap.STEP_OPS)


def test_off_domain_navigation_refused(compose):
    page, _ = compose()
    m = mapped("/compose-plain")
    m["start"] = {"url_template": "{target_url}", "requires": ["text", "target_url"]}
    run = StepRunner(page, m, {"text": TEXT, "target_url": "https://evil.test/status/1"})
    with pytest.raises(sites.DomainRefused):
        run.start()
    m = mapped("/compose-plain")
    m["steps"].append({"op": "navigate", "value": "{thread_url}"})
    run = StepRunner(page, m, {"text": TEXT, "thread_url": "https://evil.test/dm"})
    run.start()
    with pytest.raises(sites.DomainRefused):
        run.run_steps()
    assert not any("evil.test" in url for _, url in page.requests)


@pytest.mark.parametrize("path,submit", [
    ("/compose-plain", {"target": {"role": "button", "name": "Close"}, "label_allowlist": ["Post"]}),
    ("/compose-plain", {"target": {"role": "button", "name": "Post"}, "label_allowlist": ["Publish"]}),
    # a map listing Delete (it would fail validation) still cannot submit through Delete
    ("/compose-injection", {"target": {"role": "button", "name": "Delete"}, "label_allowlist": ["Post", "Delete"]}),
])
def test_submit_label_not_in_allowlist_refused(compose, path, submit):
    page, backend = compose()
    run = Submitter(page, mapped(path, submit=submit), {"text": TEXT})
    run.start()
    run.run_steps()
    with pytest.raises(Refused, match="allowlist|destructive"):   # Fix 25: Delete is refused as destructive first
        run.submit()
    assert backend.create_calls == 0
    assert page.raw.evaluate("window.__deleted") is None


def test_typed_text_is_always_the_payload_despite_injection(compose):
    page, backend = compose()
    run = StepRunner(page, mapped("/compose-injection"), {"text": "My own words"})
    run.start()
    assert page.raw.get_by_text("type: buy now").is_visible()  # the page tries
    run.run_steps()
    assert editor_text(page) == "My own words"
    m = mapped("/compose-injection")
    m["steps"][1]["value"] = "buy now"  # a map that bypassed validation
    with pytest.raises(Refused, match="queue item's text"):
        r = StepRunner(page, m, {"text": "My own words"})
        r.start()
        r.run_steps()


def test_destructive_click_refused(compose):
    page, _ = compose()
    m = mapped("/compose-injection")
    m["steps"].insert(0, {"op": "click", "target": {"role": "button", "name": "Delete"}})
    run = StepRunner(page, m, {"text": TEXT})
    run.start()
    with pytest.raises(Refused, match="destructive"):
        run.run_steps()
    assert page.raw.evaluate("window.__deleted") is None


def test_nothing_can_publish_outside_submit(compose):
    page, backend = compose()
    for extra in ({"op": "press", "value": "Enter"},
                  {"op": "click", "target": {"role": "button", "name": "Post"}}):
        m = mapped("/compose-plain")
        m["steps"].append(extra)
        run = StepRunner(page, m, {"text": TEXT})
        run.start()
        with pytest.raises(Refused, match="could publish"):
            run.run_steps()
    assert backend.create_calls == 0


def test_rehearsal_runner_cannot_submit_and_blocks_create(compose):
    assert not hasattr(StepRunner, "submit") and hasattr(Submitter, "submit")
    page, backend = compose()
    run = StepRunner(page, mapped("/compose-plain"), {"text": TEXT})
    run.start()
    run.run_steps()
    assert run.submit_target_ok() == "Post"  # checked, not clicked
    page.raw.evaluate("fetch('/api/create', {method: 'POST', body: '{}'}).catch(() => null)")
    page.raw.wait_for_timeout(300)
    assert backend.create_calls == 0
    assert any(m == "POST" and "/api/create" in u for m, u in page.requests)


def test_cold_start_composer_is_waited_for(compose, monkeypatch):
    monkeypatch.setattr(executor, "STEP_TIMEOUT_S", 8.0)
    page, _ = compose()
    run = StepRunner(page, mapped("/compose-late"), {"text": TEXT})
    run.start()
    run.run_steps()
    assert submit_enabled(page)


def test_missing_target_is_a_step_failure(compose, monkeypatch):
    monkeypatch.setattr(executor, "STEP_TIMEOUT_S", 1.0)
    page, _ = compose()
    m = mapped("/compose-plain")
    m["steps"][1]["target"] = {"css": "#nope"}
    run = StepRunner(page, m, {"text": TEXT})
    run.start()
    with pytest.raises(StepFailed, match="target not found"):
        run.run_steps()


def test_guard_runs_before_navigation_after_steps_and_before_submit(compose):
    page, backend = compose()
    calls = []
    run = Submitter(page, mapped("/compose-plain"), {"text": TEXT}, guard=lambda p: calls.append(p.url))
    run.start()
    assert calls == ["about:blank", "https://compose.test/compose-plain"]
    run.run_steps()
    assert len(calls) == 2 + 3
    run.submit()
    assert len(calls) == 6
    page.raw.wait_for_timeout(500)
    assert backend.create_calls == 1
