import json

from agent_surf.store import Store


def test_tables_created(tmp_path):
    with Store(tmp_path / "sub" / "surf.db") as s:
        names = {r[0] for r in s.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"maps", "seen", "items"} <= names


def test_map_versions():
    s = Store(":memory:")
    assert s.current_map("x", "search") is None
    assert s.next_version("x", "search") == 1
    s.add_map("x", "search", 1, "/m/x.search.v1.json", "sha256-a")
    s.add_map("x", "search", 2, "/m/x.search.v2.json", "sha256-b")
    s.add_map("reddit", "post", 1, "/m/reddit.post.v1.json", None)
    cur = s.current_map("x", "search")
    assert cur["version"] == 2 and cur["path"].endswith("v2.json")
    assert s.next_version("x", "search") == 3
    listed = [(r["site"], r["page_type"], r["version"]) for r in s.list_maps()]
    assert listed == [("reddit", "post", 1), ("x", "search", 2)]


def test_seen_unique_and_items():
    s = Store(":memory:")
    assert s.add_new_item("x", "search", "1", {"text": "a"}) is True
    assert s.add_new_item("x", "search", "1", {"text": "a"}) is False
    assert s.is_seen("x", "search", "1")
    assert not s.is_seen("x", "home", "1")
    assert s.mark_seen("x", "home", "1") is True
    rows = s.conn.execute("SELECT * FROM items").fetchall()
    assert len(rows) == 1 and json.loads(rows[0]["data_json"]) == {"text": "a"}
