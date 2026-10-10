"""Fix 26: unattended dispatch must reach the user. dispatch refuses to start
without Telegram unless --stderr-only; every publish result other than
"published" is notified (a cap refusal is retried, not notified); login and
challenge waits are notified; a message never carries more than 60 characters
of the post text."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent_surf import challenge, cli, dispatcher, outbox
from agent_surf.challenge import ChallengeTimeout, LoggedOutTimeout
from agent_surf.publisher import PublishRefused

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
LONG = "Launch day! " + "x" * 150 + " SECRET-TAIL"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SURF_HOME", str(tmp_path / "home"))
    for k in ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)

    def no_browser(*a, **k):
        raise AssertionError("must not connect to Chrome")

    monkeypatch.setattr(cli, "BrowserSession", no_browser)
    return tmp_path / "home"


def test_dispatch_refuses_without_telegram(home, capsys):
    assert cli.main(["dispatch", "--once"]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert "TELEGRAM_BOT_TOKEN" in err and "--stderr-only" in err


def test_dispatch_stderr_only_runs(home, capsys):
    assert cli.main(["dispatch", "--once", "--stderr-only", "--json"]) == 0


def test_dispatch_with_telegram_runs_and_never_prints_token(home, capsys, monkeypatch):
    token = "123456:SYNTHETIC-TOKEN-VALUE"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    assert cli.main(["dispatch", "--once", "--json"]) == 0
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err


def item(store, text=LONG):
    qid = outbox.add(store, "x", "post", {"text": text})
    outbox.approve(store, qid)
    return qid


def result(store, qid, status, error):
    outbox.transition(store, qid, "publishing")
    return SimpleNamespace(status=status, item=outbox.transition(store, qid, status, last_error=error))


def tick(store, publish_fn):
    notes = []
    dispatcher.set_last_tick(store, T0 - timedelta(seconds=30))
    report = dispatcher.tick(store, publish_fn, now=T0, notify=notes.append)
    return report, notes


@pytest.mark.parametrize("status", ["needs_attention", "failed"])
def test_not_published_results_are_notified(store, status):
    qid = item(store)
    report, notes = tick(store, lambda i: result(store, i, status, "read-back did not match"))
    assert len(notes) == 1
    note = notes[0]
    assert f"queue item {qid} (x post) {status}: read-back did not match" in note
    assert LONG[:60] in note and "SECRET-TAIL" not in note and LONG not in note


def test_cap_refusal_is_not_notified(store):
    qid = item(store)

    def capped(i):
        raise PublishRefused("per-hour cap reached (5 in the last hour)")

    report, notes = tick(store, capped)
    assert notes == [] and outbox.get(store, qid)["status"] == "approved"
    assert report.not_published == [(qid, "per-hour cap reached (5 in the last hour)")]


def test_refusal_that_needs_attention_is_notified(store):
    qid = item(store)

    def changed(i):
        outbox.transition(store, i, "needs_attention", last_error="content changed after approval")
        raise PublishRefused(f"queue item {i}: content changed after approval")

    report, notes = tick(store, changed)
    assert len(notes) == 1 and "needs_attention: content changed after approval" in notes[0]


def test_published_is_not_notified(store):
    item(store)
    report, notes = tick(store, lambda i: result(store, i, "published", None))
    assert notes == [] and report.published


@pytest.mark.parametrize("exc,word", [
    (LoggedOutTimeout("x", "URL path is /login", "x: still logged out after 600 s"), "logged out"),
    (ChallengeTimeout("challenge on x not cleared after 600 s"), "challenge"),
    (RuntimeError("boom " + LONG), "error"),
])
def test_waits_and_errors_are_notified(store, exc, word):
    qid = item(store)

    def raises(i):
        raise exc

    report, notes = tick(store, raises)
    assert len(notes) == 1 and f"queue item {qid} (x post)" in notes[0] and word in notes[0]
    assert "SECRET-TAIL" not in notes[0]


def test_reason_containing_the_full_text_is_cut():
    note = dispatcher.alert_text({"id": 7, "site": "x", "action": "post", "payload": {"text": LONG}},
                                 "needs_attention", f"mismatch: expected {LONG}")
    assert "SECRET-TAIL" not in note and note.count(LONG[:60]) == 2


def test_publish_guard_uses_the_dispatch_notifier():
    notes = []
    guard = challenge.make_guard(notify=notes.append)
    assert guard.notify == notes.append
