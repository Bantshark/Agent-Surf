"""Fix 30: a dead tab or a sleeping machine fails cleanly, and runs are
bounded. A closed tab / browser is a clean BrowserClosed (exit 1, no
traceback), never a broken map or a model call; publishing before the submit
click leaves the item approved, after it takes the unknown-outcome path.
Wall-clock budgets and sleep detection stop runs with what was captured."""

import json

import pytest

from agent_surf import (action_learner, actionmap, budget, cli, executor, learner, outbox, publisher,
                        runner)
from agent_surf.browser import CLOSED_MESSAGE, BrowserClosed, is_closed_error

from composekit import FakeBackend, good_map
from test_cli import FakeSession, save_map
from test_learner import FakeClient, fenced, model_map
from test_runner import ALL_IDS, make_map, save


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(executor, "STEP_TIMEOUT_S", 3.0)
    monkeypatch.setattr(publisher, "CONFIRM_TIMEOUT_S", 3.0)
    monkeypatch.setattr(publisher, "READBACK_TIMEOUT_S", 3.0)
    monkeypatch.setattr(runner, "FIRST_PASS_TIMEOUT_S", 5.0)


class TargetClosedError(Exception):
    """Stands in for playwright._impl._errors.TargetClosedError (matched by name)."""


def test_is_closed_error():
    assert is_closed_error(TargetClosedError("x"))
    assert is_closed_error(RuntimeError("Page.wait_for_timeout: Target page, context or browser has been closed"))
    assert is_closed_error(BrowserClosed())
    assert not is_closed_error(RuntimeError("Timeout 3000ms exceeded"))


def closing_guard(after):
    """A guard that closes the tab on its ``after``-th call (as a crash or sleep would)."""
    calls = [0]

    def guard(page):
        calls[0] += 1
        if calls[0] == after:
            page._page.close()

    return guard


def test_reads_on_a_closed_tab_raise_browser_closed(har_page):
    page = har_page("feed.har")
    page.goto("https://feed.test/")
    page._page.close()
    for read in (page.aria_snapshot, page.title, lambda: page.count("p"),
                 lambda: page.dom_items({"item": "article", "id_attr": "id", "fields": {"t": "p"}})):
        with pytest.raises(BrowserClosed):
            read()


@pytest.mark.parametrize("after", [1, 3])
def test_run_on_a_closed_tab_is_clean_and_never_heals(store, har_page, tmp_path, after):
    save(store, tmp_path, make_map())
    client = FakeClient(fenced(model_map()))
    with pytest.raises(BrowserClosed):
        learner.run_with_heal(store, har_page("feed.har"), "feedtest", "home", client=client, model="m",
                              maps_dir=tmp_path, guard=closing_guard(after))
    assert client.calls == []


def test_learn_on_a_closed_tab_never_calls_the_model(store, har_page, tmp_path):
    client = FakeClient(fenced(model_map()))
    with pytest.raises(BrowserClosed):
        learner.learn(store, har_page("feed.har"), "feedtest", "home", client=client, model="m",
                      maps_dir=tmp_path, guard=closing_guard(1))
    assert client.calls == [] and store.current_map("feedtest", "home") is None


def plain_map():
    m = good_map()
    m["start"] = dict(m["start"], url_template="https://compose.test/compose-plain")
    return m


def test_learn_action_rehearsal_on_a_closed_tab(store, compose, tmp_path):
    page, backend = compose()
    reply = "```json\n" + json.dumps(plain_map()) + "\n```"
    client = FakeClient(reply)
    guard_calls = [0]

    def guard(p):   # close once the model has answered (rehearsal steps run the guard)
        guard_calls[0] += 1
        if client.calls and guard_calls[0] > 4:
            page.raw.close()

    with pytest.raises(BrowserClosed):
        action_learner.learn_action(store, page, "composetest", "post", client=client, model="m",
                                    maps_dir=tmp_path, start_url="https://compose.test/compose-plain",
                                    guard=guard)
    assert actionmap.current_action_map(store, "composetest", "post") is None


def approved(store):
    qid = outbox.add(store, "composetest", "post", {"text": "hello"})
    outbox.approve(store, qid)
    return qid


def test_publish_closed_before_submit_leaves_item_approved(store, compose, tmp_path):
    actionmap.save_action_map(store, tmp_path / "maps", plain_map())
    backend = FakeBackend()
    qid = approved(store)
    client = FakeClient()
    with pytest.raises(BrowserClosed):
        publisher.publish(store, lambda site: compose(backend=backend)[0], qid, client=client, model="m",
                          maps_dir=tmp_path, guard=closing_guard(3))
    item = outbox.get(store, qid)
    assert item["status"] == "approved" and "nothing submitted" in item["last_error"]
    assert outbox.verify_approved(item) is None
    assert backend.create_calls == 0 and client.calls == []


def test_publish_open_tab_fails_leaves_item_approved(store, tmp_path):
    actionmap.save_action_map(store, tmp_path / "maps", plain_map())
    qid = approved(store)

    def open_page(site):
        raise TargetClosedError("Browser.new_page: Target page, context or browser has been closed")

    with pytest.raises(BrowserClosed):
        publisher.publish(store, open_page, qid, maps_dir=tmp_path)
    assert outbox.get(store, qid)["status"] == "approved"


def test_publish_closed_after_submit_takes_the_unknown_path(store, compose, tmp_path, monkeypatch):
    actionmap.save_action_map(store, tmp_path / "maps", plain_map())
    backend = FakeBackend()
    qid = approved(store)
    pages = []

    def open_page(site):
        pages.append(compose(backend=backend)[0])
        return pages[-1]

    real = publisher._await_network_confirm

    def confirm_then_die(page, net, since):
        page.raw.close()
        return real(page, net, since)

    monkeypatch.setattr(publisher, "_await_network_confirm", confirm_then_die)
    result = publisher.publish(store, open_page, qid, maps_dir=tmp_path)
    assert backend.create_calls == 1                       # never resubmitted
    assert result.status in ("needs_attention", "published")
    assert result.status != "approved" and outbox.get(store, qid)["status"] != "approved"


def test_cli_closed_tab_exit_1_without_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_SURF_HOME", str(tmp_path / "home"))
    save_map(tmp_path / "home", make_map())

    def dies(*a, **k):
        raise TargetClosedError("Page.wait_for_timeout: Target page, context or browser has been closed")

    monkeypatch.setattr(learner, "run_with_heal", dies)
    monkeypatch.setattr(cli, "BrowserSession", FakeSession(lambda har: None))
    assert cli.main(["run", "feedtest", "home"]) == 1
    err = capsys.readouterr().err
    assert CLOSED_MESSAGE in err and "Traceback" not in err


# -- budgets and sleep -----------------------------------------------------

class Clocks:
    def __init__(self):
        self.mono, self.wall = 1000.0, 5000.0

    def advance(self, mono, wall=None):
        self.mono += mono
        self.wall += mono if wall is None else wall


def ticking_guard(clocks, *, per_call, start_after=0, wall=None):
    calls = [0]

    def guard(page):
        calls[0] += 1
        if calls[0] > start_after:
            clocks.advance(per_call, wall)

    return guard


def test_run_budget_returns_what_was_captured(store, har_page, tmp_path):
    save(store, tmp_path, make_map(scroll={"max_scrolls": 6, "delay_s": 1.0, "stop_after_seen": 50}))
    clocks = Clocks()
    with budget.limit("run", 100, clock=lambda: clocks.mono, wall=lambda: clocks.wall):
        # the first pass is free; each later guard call (after a scroll) spends 60 s
        result = runner.run(store, har_page("feed.har"), "feedtest", "home",
                            guard=ticking_guard(clocks, per_call=60, start_after=3))
    assert result.stop_reason == "time_budget"
    assert 0 < len(result.items) < len(ALL_IDS)
    rows = store.select_items()
    assert [r["item_id"] for r in rows] == [i["item_id"] for i in result.items]   # stored: recoverable


def test_sleep_detected_stops_the_run(store, har_page, tmp_path, caplog):
    save(store, tmp_path, make_map())
    clocks = Clocks()
    with budget.limit("run", 900, clock=lambda: clocks.mono, wall=lambda: clocks.wall):
        result = runner.run(store, har_page("feed.har"), "feedtest", "home",
                            guard=ticking_guard(clocks, per_call=1, wall=7200, start_after=3))
    assert result.stop_reason == "machine_slept"
    assert "the machine slept during the run" in caplog.text


def test_learn_budget_fails_without_saving(store, har_page, tmp_path):
    clocks = Clocks()
    client = FakeClient(fenced(model_map()))
    with budget.limit("learn", 100, clock=lambda: clocks.mono, wall=lambda: clocks.wall):
        with pytest.raises(budget.BudgetExceeded):
            learner.learn(store, har_page("feed.har"), "feedtest", "home", client=client, model="m",
                          maps_dir=tmp_path, guard=ticking_guard(clocks, per_call=60))
    assert store.current_map("feedtest", "home") is None


def test_publish_budget_before_submit_leaves_item_approved(store, compose, tmp_path):
    actionmap.save_action_map(store, tmp_path / "maps", plain_map())
    backend = FakeBackend()
    qid = approved(store)
    clocks = Clocks()
    with budget.limit("publish", 100, clock=lambda: clocks.mono, wall=lambda: clocks.wall):
        with pytest.raises(budget.BudgetExceeded):
            publisher.publish(store, lambda site: compose(backend=backend)[0], qid, maps_dir=tmp_path,
                              guard=ticking_guard(clocks, per_call=60))
    item = outbox.get(store, qid)
    assert item["status"] == "approved" and "time budget" in item["last_error"]
    assert backend.create_calls == 0


def test_budget_after_submit_is_suspended(store, compose, tmp_path):
    """Once submitted, the outcome is resolved even if the budget runs out."""
    actionmap.save_action_map(store, tmp_path / "maps", plain_map())
    backend = FakeBackend()
    qid = approved(store)
    clocks = Clocks()
    with budget.limit("publish", 100, clock=lambda: clocks.mono, wall=lambda: clocks.wall):
        sub_guard = ticking_guard(clocks, per_call=10)
        result = publisher.publish(store, lambda site: compose(backend=backend)[0], qid, maps_dir=tmp_path,
                                   guard=sub_guard)
        clocks.advance(10_000)
    assert result.status == "published" and backend.create_calls == 1


def test_budget_unit_and_env(monkeypatch):
    assert budget.seconds_for("run", {}) == 900 and budget.seconds_for("learn", {}) == 1200
    assert budget.seconds_for("publish", {}) == 600
    assert budget.seconds_for("run", {"AGENT_SURF_MAX_RUN_S": "30"}) == 30
    assert budget.seconds_for("run", {"AGENT_SURF_MAX_RUN_S": "soon"}) == 900
    budget.check()   # no active budget: nothing
    clocks = Clocks()
    with budget.limit("run", 10, clock=lambda: clocks.mono, wall=lambda: clocks.wall) as b:
        budget.check()
        with budget.suspended():
            clocks.advance(20)
            budget.check()
        with pytest.raises(budget.BudgetExceeded, match="AGENT_SURF_MAX_RUN_S"):
            budget.check()
    budget.check()


def test_cli_learn_budget_exit_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_SURF_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SYNTHETIC")
    monkeypatch.setattr(cli, "make_client", lambda cfg: FakeClient())

    def learn(*a, **k):
        raise budget.BudgetExceeded("the learn time budget of 1200 s was spent")

    monkeypatch.setattr(learner, "learn", learn)
    monkeypatch.setattr(cli, "BrowserSession", FakeSession(lambda har: None))
    assert cli.main(["learn", "feedtest", "home"]) == 1
    assert "nothing was saved" in capsys.readouterr().err
