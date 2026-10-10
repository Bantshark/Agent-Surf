"""Fix 14: purge old items and debug folders, auto-prune debug folders,
loopback-only CDP."""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from agent_surf import cli, config, outbox, retention
from agent_surf.browser import BrowserError, BrowserSession, cdp_endpoint
from agent_surf.store import Store

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


def add_item(store, item_id, days_ago):
    store.conn.execute("INSERT INTO items VALUES ('x', 'search', ?, '{}', ?)",
                       (item_id, (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")))
    store.mark_seen("x", "search", item_id)


def test_purge_items_threshold_and_dry_run(store):
    for i, age in enumerate([1, 89, 91, 400]):
        add_item(store, f"i{i}", age)
    outbox.add(store, "x", "post", {"text": "kept"})
    store.add_map("x", "search", 1, "/m.json", None)
    assert retention.purge_items(store, 90, now=NOW, dry_run=True) == 2
    assert store.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 4
    assert retention.purge_items(store, 90, now=NOW) == 2
    assert [r[0] for r in store.conn.execute("SELECT item_id FROM items ORDER BY item_id")] == ["i0", "i1"]
    assert all(store.is_seen("x", "search", f"i{i}") for i in range(4))   # seen ids kept
    assert len(outbox.list_items(store)) == 1 and store.current_map("x", "search") is not None


def make_debug(root, n, age_days):
    p = root / f"2026-{n:03d}-x-post"
    p.mkdir(parents=True)
    (p / "error.txt").write_text("e")
    t = (NOW - timedelta(days=age_days)).timestamp()
    os.utime(p, (t, t))
    return p


def test_purge_debug_threshold_and_dry_run(tmp_path):
    old, new = make_debug(tmp_path, 1, 30), make_debug(tmp_path, 2, 3)
    assert retention.purge_debug(tmp_path, 14, now=NOW, dry_run=True) == [old] and old.exists()
    assert retention.purge_debug(tmp_path, 14, now=NOW) == [old]
    assert not old.exists() and new.exists()


def test_prune_keeps_newest_20(tmp_path):
    folders = [make_debug(tmp_path, i, 40 - i) for i in range(25)]
    removed = retention.prune_debug(tmp_path)
    assert removed == folders[:5]
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(p.name for p in folders[5:])
    assert retention.prune_debug(tmp_path / "missing") == []


def test_cli_purge_and_auto_prune(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("AGENT_SURF_HOME", str(home))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with Store(home / "surf.db") as s:
        add_item(s, "old", 200)
        add_item(s, "new", 1)
    for i in range(25):
        make_debug(home / "debug", i, 1 + i * 0.01)
    assert cli.main(["purge", "--dry-run", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True and out["items"] >= 1 and out["debug_folders"] == []
    # learn-action prunes debug folders to the newest 20 even when it then stops (no API key)
    assert cli.main(["learn-action", "x", "post"]) == 1
    assert len(list((home / "debug").iterdir())) == 20


@pytest.mark.parametrize("url", ["http://127.0.0.1:9222", "http://localhost:9222", "http://[::1]:9222",
                                 "ws://127.0.0.1:9333/devtools/browser/x"])
def test_loopback_cdp_allowed(url):
    assert cdp_endpoint(config.load({"AGENT_SURF_CDP_URL": url})) == url


@pytest.mark.parametrize("url", ["http://192.168.1.5:9222", "http://example.test:9222", "http://0.0.0.0:9222",
                                 "ws://10.0.0.2:9333/devtools/browser/x"])
def test_non_loopback_cdp_refused_unless_allowed(url, tmp_path):
    with pytest.raises(BrowserError, match="AGENT_SURF_ALLOW_REMOTE_CDP=1"):
        cdp_endpoint(config.load({"AGENT_SURF_CDP_URL": url}))
    assert cdp_endpoint(config.load({"AGENT_SURF_CDP_URL": url, "AGENT_SURF_ALLOW_REMOTE_CDP": "1"})) == url
    with pytest.raises(BrowserError):
        BrowserSession(url)


def test_devtools_mode_is_loopback_checked_too(tmp_path):
    (tmp_path / "DevToolsActivePort").write_text("9444\n/devtools/browser/abc\n")
    cfg = config.load({"AGENT_SURF_ATTACH": "devtools-active-port", "AGENT_SURF_PROFILE_DIR": str(tmp_path)})
    assert cdp_endpoint(cfg).startswith("ws://127.0.0.1:9444")
