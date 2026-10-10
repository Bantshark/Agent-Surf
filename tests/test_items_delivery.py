"""Fix 21: an item marked seen is never lost. Every run/inbox invocation stamps
its items with a run_id; delivered_at is set only after stdout was written and
flushed; `items` brings back what was stored (e.g. --undelivered)."""

import io
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

from agent_surf import cli, retention
from agent_surf.store import Store, new_run_id

from test_cli import FakeSession, save_map
from test_runner import make_map

IDS = [f"p{i}" for i in range(1, 16)]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SURF_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    return tmp_path / "home"


class BrokenStdout(io.StringIO):
    def write(self, s):
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        raise BrokenPipeError(32, "Broken pipe")


def run_id_from(err):
    found = re.findall(r"run_id=(\S+)", err)
    assert found, err
    return found[0]


def items_json(capsys, *args):
    assert cli.main(["items", "--json", *args]) == 0
    return json.loads(capsys.readouterr().out)


def test_run_id_format():
    rid = new_run_id()
    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{6}", rid)
    assert new_run_id() != rid


def test_stdout_failure_leaves_items_recoverable(home, capsys, monkeypatch, har_page):
    save_map(home, make_map())
    monkeypatch.setattr(cli, "BrowserSession", FakeSession(har_page))
    monkeypatch.setattr(sys, "stdout", BrokenStdout())
    assert cli.main(["run", "feedtest", "home", "--json"]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    rid = run_id_from(err)
    assert "15 item(s) from run_id=" in err and f"items --run {rid}" in err
    monkeypatch.undo()
    monkeypatch.setenv("AGENT_SURF_HOME", str(home))
    monkeypatch.setattr(cli, "BrowserSession", FakeSession(har_page))
    # the ids are seen now, so a new run prints nothing ...
    assert cli.main(["run", "feedtest", "home", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    # ... but nothing is lost
    got = items_json(capsys, "--undelivered")
    assert [i["item_id"] for i in got] == IDS
    assert got[0]["site"] == "feedtest" and got[0]["page_type"] == "home" and "text" in got[0]
    assert [i["item_id"] for i in items_json(capsys, "--run", rid)] == IDS
    # still undelivered until asked
    assert len(items_json(capsys, "--undelivered")) == 15
    assert len(items_json(capsys, "--undelivered", "--mark-delivered")) == 15
    assert items_json(capsys, "--undelivered") == []


@pytest.mark.parametrize("as_json", [True, False])
def test_successful_run_marks_delivered(home, capsys, monkeypatch, har_page, as_json):
    save_map(home, make_map())
    monkeypatch.setattr(cli, "BrowserSession", FakeSession(har_page))
    assert cli.main(["run", "feedtest", "home"] + (["--json"] if as_json else [])) == 0
    captured = capsys.readouterr()
    rid = run_id_from(captured.err)
    if as_json:
        assert isinstance(json.loads(captured.out), list)   # run --json stays a plain array
    assert items_json(capsys, "--undelivered") == []
    with Store(home / "surf.db") as s:
        rows = s.select_items(run_id=rid)
        assert len(rows) == 15 and all(r["delivered_at"] for r in rows)


def test_items_failure_with_mark_delivered_keeps_them(home, capsys, monkeypatch):
    with Store(home / "surf.db") as s:
        s.run_id = "r1"
        s.add_new_item("x", "search", "1", {"site": "x", "page_type": "search", "item_id": "1"})
    monkeypatch.setattr(sys, "stdout", BrokenStdout())
    assert cli.main(["items", "--undelivered", "--mark-delivered", "--json"]) == cli.EXIT_ERROR
    monkeypatch.undo()
    monkeypatch.setenv("AGENT_SURF_HOME", str(home))
    capsys.readouterr()
    assert [i["item_id"] for i in items_json(capsys, "--undelivered")] == ["1"]


def test_run_id_scoping_and_filters(home, capsys):
    with Store(home / "surf.db") as s:
        for rid, site, ids in (("A", "x", "123"), ("B", "x", "45"), ("C", "reddit", "6")):
            s.run_id = rid
            for i in ids:
                s.add_new_item(site, "search", i, {"site": site, "page_type": "search", "item_id": i})
        s.mark_delivered(run_id="A")
        assert s.mark_delivered(run_id="A") == 0          # already delivered
    ids = lambda *a: [i["item_id"] for i in items_json(capsys, *a)]
    assert ids("--run", "A") == ["1", "2", "3"]
    assert ids("--run", "B") == ["4", "5"]
    assert ids("--undelivered") == ["4", "5", "6"]
    assert ids("--undelivered", "--site", "reddit") == ["6"]
    assert ids("--site", "x", "--page-type", "search") == ["1", "2", "3", "4", "5"]
    assert ids("--site", "x", "--page-type", "home") == []
    assert ids() == ["1", "2", "3", "4", "5", "6"]
    assert ids("--since", "2000-01-01T00:00:00Z") == ["1", "2", "3", "4", "5", "6"]
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert ids("--since", future) == []
    # text output
    assert cli.main(["items", "--run", "B"]) == 0
    assert capsys.readouterr().out.splitlines()[0].startswith("4\t")
    with pytest.raises(SystemExit):
        cli.main(["items", "--since", "yesterday"])
    with pytest.raises(SystemExit):
        cli.main(["items", "--run", "A", "--undelivered"])


def test_since_normalises_to_utc():
    assert cli._since("2026-10-10T14:00:00+02:00") == "2026-10-10T12:00:00+00:00"
    assert cli._since("2026-10-10T12:00:00") == "2026-10-10T12:00:00+00:00"
    assert cli._since("2026-10-10T12:00:00Z") == "2026-10-10T12:00:00+00:00"


def test_inbox_gets_run_id_and_delivers(home, capsys, monkeypatch, har_page):
    from agent_surf import sites

    save_map(home, make_map())
    monkeypatch.setitem(sites.INBOX_PAGE_TYPES, "feedtest", {"home"})
    monkeypatch.setattr(cli, "BrowserSession", FakeSession(har_page))
    assert cli.main(["inbox", "feedtest", "home", "--json"]) == 0
    captured = capsys.readouterr()
    rid = run_id_from(captured.err)
    assert [i["item_id"] for i in json.loads(captured.out)] == IDS
    assert [i["item_id"] for i in items_json(capsys, "--run", rid)] == IDS
    assert items_json(capsys, "--undelivered") == []


def test_youtube_gets_run_id(home, capsys, monkeypatch):
    import yt_dlp

    from test_youtube import VIDEO, FakeYDL

    monkeypatch.setattr(yt_dlp, "YoutubeDL", lambda opts: FakeYDL(opts, VIDEO))
    assert cli.main(["youtube", "https://www.youtube.com/watch?v=abc", "--json"]) == 0
    rid = run_id_from(capsys.readouterr().err)
    assert len(items_json(capsys, "--run", rid)) == 1
    assert items_json(capsys, "--undelivered") == []


def test_migration_adds_columns_and_keeps_old_items(tmp_path):
    db = tmp_path / "surf.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE items (site TEXT NOT NULL, page_type TEXT NOT NULL, item_id TEXT NOT NULL,
                            data_json TEXT NOT NULL, captured_at TEXT NOT NULL);
        INSERT INTO items VALUES ('x', 'search', 'old', '{"item_id": "old"}', '2026-01-01T00:00:00+00:00');
    """)
    conn.commit()
    conn.close()
    with Store(db) as s:
        cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(items)")}
        assert {"run_id", "delivered_at"} <= cols
        (row,) = s.select_items()
        assert row["item_id"] == "old" and row["run_id"] is None
        assert row["delivered_at"] == "2026-01-01T00:00:00+00:00"   # printed back then
        assert s.select_items(undelivered=True) == []
    with Store(db) as s:   # reopening is a no-op
        assert len(s.select_items()) == 1


def test_purge_still_works_with_run_columns(store):
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat(timespec="seconds")
    store.run_id = "R"
    store.add_new_item("x", "search", "a", {"item_id": "a"})
    store.add_new_item("x", "search", "b", {"item_id": "b"})
    store.conn.execute("UPDATE items SET captured_at = ? WHERE item_id = 'a'", (old,))
    store.conn.commit()
    assert retention.purge_items(store, 90) == 1
    assert [r["item_id"] for r in store.select_items(run_id="R")] == ["b"]
    assert store.is_seen("x", "search", "a")


def test_add_new_item_is_one_transaction(store, monkeypatch):
    """A failure storing the item must not leave the id marked seen."""
    def boom(*a, **k):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(store, "_insert_item", boom)
    with pytest.raises(sqlite3.OperationalError):
        store.add_new_item("x", "search", "z", {})
    assert not store.is_seen("x", "search", "z")
