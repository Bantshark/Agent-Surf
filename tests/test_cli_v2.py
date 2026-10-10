"""v2 Unit 9: CLI for the write layer (offline: the browser session is a
HAR-backed compose.test context)."""

import json
import os

import pytest

from agent_surf import actionmap, cli, executor, publisher
from agent_surf.store import Store

from composekit import FakeBackend, good_map


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SURF_HOME", str(tmp_path / "home"))
    for k in ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(executor, "STEP_TIMEOUT_S", 3.0)
    monkeypatch.setattr(publisher, "CONFIRM_TIMEOUT_S", 3.0)
    monkeypatch.setattr(publisher, "READBACK_TIMEOUT_S", 3.0)
    return tmp_path / "home"


@pytest.fixture
def session(chromium, monkeypatch):
    """Replaces BrowserSession with HAR-backed compose.test tabs."""
    from conftest import FIXTURES
    backend = FakeBackend()
    ctx = chromium.new_context(viewport={"width": 1000, "height": 800})
    ctx.route_from_har(FIXTURES / "compose.har", not_found="abort")
    backend.install(ctx)
    opened = []

    class Session:
        def __init__(self, endpoint):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            for t in opened:
                t.close()
            opened.clear()

        def open_tab(self):
            opened.append(ctx.new_page())
            return opened[-1]

    monkeypatch.setattr(cli, "BrowserSession", Session)
    yield backend
    ctx.close()


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def save_map(home):
    m = good_map()
    m["start"]["url_template"] = "https://compose.test/compose-plain"
    with Store(home / "surf.db") as s:
        actionmap.save_action_map(s, home / "maps", m)


def test_queue_workflow_and_publish(home, session, capsys, tmp_path):
    save_map(home)
    code, out, _ = run(capsys, "queue", "add", "composetest", "post", "--text", "Hello CLI \U0001f44b", "--json")
    assert code == 0
    qid = json.loads(out)["id"]
    code, out, _ = run(capsys, "queue", "list", "--status", "draft", "--json")
    assert [i["id"] for i in json.loads(out)] == [qid]
    code, out, err = run(capsys, "publish", str(qid), "--json")
    assert code == cli.EXIT_PUBLISH_REFUSED and json.loads(out)["status"] == "refused"
    code, out, _ = run(capsys, "queue", "approve", str(qid), "--json")
    assert json.loads(out)[0]["status"] == "approved" and json.loads(out)[0]["content_hash"]
    code, out, _ = run(capsys, "publish", str(qid), "--json")
    assert code == 0
    item = json.loads(out)
    assert item["status"] == "published" and item["receipt"]["post_id"] == "1000"
    assert session.create_calls == 1
    code, out, _ = run(capsys, "receipts", "--json")
    assert json.loads(out) == [{"id": qid, "site": "composetest", "action": "post",
                                **item["receipt"]}]
    code, out, _ = run(capsys, "publish", str(qid))
    assert code == cli.EXIT_PUBLISH_REFUSED and session.create_calls == 1


def test_publish_needs_attention_exit_code(home, session, capsys):
    session.mode = "errors"
    save_map(home)
    _, out, _ = run(capsys, "queue", "add", "composetest", "post", "--text", "x", "--json")
    qid = json.loads(out)["id"]
    run(capsys, "queue", "approve", str(qid))
    code, out, err = run(capsys, "publish", str(qid), "--json")
    assert code == cli.EXIT_NEEDS_ATTENTION and json.loads(out)["status"] == "needs_attention"
    assert "platform returned errors" in err


def test_approve_all_reject_show(home, capsys, tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n x")
    ids = [json.loads(run(capsys, "queue", "add", "x", "post", "--text", f"t{i}", "--json")[1])["id"]
           for i in range(2)]
    mid = json.loads(run(capsys, "queue", "add", "x", "reply", "--text", "r", "--media", str(img),
                         "--target", "https://x.com/a/status/1", "--at", "2026-11-01T09:00:00Z",
                         "--missed", "skip", "--json")[1])["id"]
    run(capsys, "queue", "reject", str(ids[0]))
    code, out, _ = run(capsys, "queue", "approve", "--all-drafts", "--json")
    assert sorted(i["id"] for i in json.loads(out)) == sorted([ids[1], mid])
    code, out, _ = run(capsys, "queue", "show", str(mid), "--json")
    shown = json.loads(out)
    assert shown["scheduled_at"] == "2026-11-01T09:00:00+00:00" and shown["missed_policy"] == "skip"
    assert shown["payload"]["media"] == [os.path.abspath(img)]
    code, out, _ = run(capsys, "queue", "list")
    assert len(out.strip().splitlines()) == 3
    code, _, err = run(capsys, "queue", "approve", str(ids[0]))
    assert code == 1 and "rejected" in err
    code, _, err = run(capsys, "queue", "add", "x", "reply", "--text", "r")
    assert code == 1 and "needs --target" in err


def test_dispatch_once_publishes_due_items(home, session, capsys):
    save_map(home)
    qid = json.loads(run(capsys, "queue", "add", "composetest", "post", "--text", "due now", "--json")[1])["id"]
    run(capsys, "queue", "approve", str(qid))
    code, out, _ = run(capsys, "dispatch", "--once", "--json")
    assert code == 0 and json.loads(out.strip().splitlines()[-1])["published"] == [qid]
    assert session.create_calls == 1


def test_learn_action_needs_key_and_inbox_page_check(home, capsys):
    code, _, err = run(capsys, "learn-action", "x", "post")
    assert code == 1 and "ANTHROPIC_API_KEY" in err
    code, _, err = run(capsys, "inbox", "x", "search")
    assert code == 1 and "not an inbox page" in err


def test_json_on_every_command(home, capsys, tmp_path):
    for argv in (["chrome"], ["maps", "list"], ["receipts"], ["queue", "list"]):
        code, out, _ = run(capsys, *argv, "--json")
        assert code == 0
        json.loads(out)
    src, dst = tmp_path / "in.har", tmp_path / "out.har"
    src.write_text(json.dumps({"log": {"entries": []}}))
    code, out, _ = run(capsys, "scrub", str(src), str(dst), "--json")
    assert json.loads(out) == {"headers": 0, "cookies": 0, "params": 0, "bodies": 0}
