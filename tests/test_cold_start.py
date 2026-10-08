"""Cold start: a freshly started browser's app requests its feed late. The
runner and learner wait for content (bounded by FIRST_PASS_TIMEOUT_S) before a
first pass may declare the map broken."""

import pytest

from agent_surf import challenge, learner, runner
from agent_surf.runner import MapBroken

from test_learner import FakeClient, fenced, model_map
from test_runner import ids, make_map, save

FIVE = [f"p{i}" for i in range(1, 6)]


@pytest.fixture(autouse=True)
def short_timeout(monkeypatch):
    # The pages deliver content after ~3 s (3x delay_s); keep the ceiling short.
    monkeypatch.setattr(runner, "FIRST_PASS_TIMEOUT_S", 6.0)


def cold_map(page_type, **changes):
    m = make_map(scroll={"max_scrolls": 0, "delay_s": 1.0, "stop_after_seen": 3}, **changes)
    m["page_type"] = page_type
    return m


def test_late_feed_is_waited_for(store, har_page, tmp_path):
    save(store, tmp_path, cold_map("late"))
    result = runner.run(store, har_page("feed.har"), "feedtest", "late")
    assert result.source == "network"
    assert ids(result) == FIVE


def test_late_dom_is_waited_for(store, har_page, tmp_path):
    save(store, tmp_path, cold_map("latedom", source="dom", network=None))
    result = runner.run(store, har_page("feed.har"), "feedtest", "latedom")
    assert result.source == "dom"
    assert ids(result) == FIVE


def test_feed_never_arrives_breaks_after_timeout_and_heals_once(store, har_page, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "FIRST_PASS_TIMEOUT_S", 1.5)
    save(store, tmp_path, cold_map("never"))
    with pytest.raises(MapBroken, match="no items"):
        runner.run(store, har_page("feed.har"), "feedtest", "never")

    client = FakeClient(fenced(model_map()), fenced(model_map()))
    with pytest.raises(learner.HealFailed):
        learner.run_with_heal(store, har_page("feed.har"), "feedtest", "never", client=client,
                              model="claude-sonnet-5-5", maps_dir=tmp_path / "maps")
    assert len(client.calls) == 1


class FakeHTTPResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def test_challenge_during_wait_is_handled(store, har_page, tmp_path):
    sent = []

    def urlopen(req, timeout):
        sent.append(req.full_url.rsplit("/", 1)[-1])
        return FakeHTTPResponse()

    notify = challenge.make_notifier({"TELEGRAM_BOT_TOKEN": "000:synthetic", "TELEGRAM_CHAT_ID": "1"},
                                     urlopen=urlopen)
    guard_calls = []

    def guard(page):
        guard_calls.append(page.url)
        challenge.wait_until_clear(page, notify=notify, poll_s=0.5, timeout_s=20)

    save(store, tmp_path, cold_map("latechallenge"))
    result = runner.run(store, har_page("feed.har"), "feedtest", "latechallenge", guard=guard)
    assert sent == ["sendMessage"]  # one ping, Telegram mocked
    assert any("/checkpoint/" in u for u in guard_calls)
    assert ids(result) == FIVE



def test_learner_waits_for_late_feed(store, har_page, tmp_path):
    client = FakeClient(fenced(model_map()))
    m = learner.learn(store, har_page("feed.har"), "feedtest", "late", client=client,
                      model="claude-sonnet-5-5", maps_dir=tmp_path / "maps")
    assert m["version"] == 1
    row = store.current_map("feedtest", "late")
    assert row["dry_run_items"] == 5  # measured after the feed arrived, before scrolling
    assert "Synthetic post 1" in client.calls[0]["messages"][0]["content"]
