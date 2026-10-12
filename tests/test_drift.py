"""Layout drift is advisory and meaningful: content changes never warn, a
structural change warns after DRIFT_RUNS consecutive runs, a health drop warns
at once. Drift never blocks a run."""

import json
import logging
import sqlite3

import pytest

from agent_surf import learner, runner, sitemap, sites
from agent_surf.store import Store

from test_learner import FakeClient, fenced, model_map

DRIFT_SITE = sites.Site("drifttest", ("feed.test",), {"feed": "https://feed.test/drift/{handle}"})
DRIFT_MAP = {
    "site": "drifttest", "page_type": "feed", "version": 1, "source": "dom",
    "dom": {"item": "article[data-id]", "id_attr": "data-id", "fields": {"text": ".text"}},
    "required_fields": ["text"],
    "scroll": {"max_scrolls": 0, "delay_s": 1.0, "stop_after_seen": 50},
    "limits": {"max_items": 100},
}


@pytest.fixture(autouse=True)
def drift_site():
    sites.register_site(DRIFT_SITE)
    yield
    sites.unregister_site(DRIFT_SITE.name)


@pytest.fixture
def learned(store, har_page, tmp_path):
    """DRIFT_MAP saved with the base page's fingerprint and a dry-run count of 4."""
    page = har_page("drift.har", DRIFT_SITE)
    page.goto("https://feed.test/drift/base")
    m = dict(DRIFT_MAP, fingerprint=sitemap.fingerprint(page.aria_snapshot()))
    store.add_map("drifttest", "feed", 1, sitemap.save_map(tmp_path, m), m["fingerprint"], dry_run_items=4)
    return m


def run(store, har_page, variant, caplog):
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="agent_surf.runner"):
        result = runner.run(store, har_page("drift.har", DRIFT_SITE), "drifttest", "feed", handle=variant)
    return result, [r.getMessage() for r in caplog.records]


def test_content_changes_never_warn(store, har_page, learned, caplog):
    for variant in ["base", "text", "count", "sidebar", "text", "count", "sidebar"]:
        result, warnings = run(store, har_page, variant, caplog)
        assert result.drift_count == 0, variant
        assert not result.drift and warnings == [], (variant, warnings)


def test_healthy_structural_change_rebaselines_after_threshold(store, har_page, learned, caplog):
    """Fix 31: a layout change with healthy extraction moves the baseline once
    the streak reaches DRIFT_RUNS, instead of warning forever."""
    for n in (1, 2):
        result, warnings = run(store, har_page, "redesign", caplog)
        assert result.drift_count == n and not result.drift and warnings == []
        assert result.stop_reason == "max_scrolls"  # never blocks the run
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="agent_surf.runner"):
        result = runner.run(store, har_page("drift.har", DRIFT_SITE), "drifttest", "feed", handle="redesign")
    assert result.rebaselined and not result.drift and result.drift_count == 0
    assert "layout changed but extraction is healthy; baseline updated" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    row = store.current_map("drifttest", "feed")
    assert row["fingerprint"] == result.fingerprint and row["drift_count"] == 0
    # The new layout is now the baseline: no streak at all ...
    result, warnings = run(store, har_page, "redesign", caplog)
    assert result.drift_count == 0 and warnings == []
    # ... and the old one counts as a change.
    result, warnings = run(store, har_page, "base", caplog)
    assert result.drift_count == 1 and warnings == []


def test_structural_change_with_health_problem_warns_and_keeps_baseline(store, har_page, learned, caplog):
    before = store.current_map("drifttest", "feed")["fingerprint"]
    for variant in ("redesign", "redesign-sparse", "redesign"):
        result, warnings = run(store, har_page, variant, caplog)
    assert result.drift and result.drift_count == runner.DRIFT_RUNS and not result.rebaselined
    assert any("layout drift" in w and "3 runs in a row" in w and "health problem" in w for w in warnings)
    row = store.current_map("drifttest", "feed")
    assert row["fingerprint"] == before and row["drift_count"] == 3
    assert row["last_fingerprint"] == result.fingerprint


def test_interrupted_streak_does_not_warn(store, har_page, learned, caplog):
    for variant in ["redesign", "redesign", "base", "redesign", "redesign"]:
        result, warnings = run(store, har_page, variant, caplog)
        assert warnings == [], variant


@pytest.mark.parametrize("variant,expected", [
    ("sparse", "first pass found 1 item(s), the learner's dry run found 4"),
    ("fieldless", "only 1 of 4 item(s) have all required fields"),
])
def test_health_drop_warns_immediately(store, har_page, learned, caplog, variant, expected):
    result, warnings = run(store, har_page, variant, caplog)
    assert result.drift and result.drift_count == 0
    assert any("extraction health dropped" in w and expected in w for w in warnings), warnings
    assert len(result.items) == 1  # still returned: drift is advisory


def test_run_right_after_learn_does_not_warn(store, har_page, tmp_path, caplog):
    """The validation symptom: drift warned on the run straight after learn."""
    learner.learn(store, har_page("feed.har"), "feedtest", "home", client=FakeClient(fenced(model_map())),
                  model="claude-sonnet-5-5", maps_dir=tmp_path / "maps")
    row = store.current_map("feedtest", "home")
    assert row["dry_run_items"] == 5  # counted before the learner's scroll, like a first pass
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="agent_surf.runner"):
        result = runner.run(store, har_page("feed.har"), "feedtest", "home")
    assert result.fingerprint == row["fingerprint"]
    assert not result.drift and caplog.records == []


OLD_SCHEMA = """
CREATE TABLE maps (site TEXT NOT NULL, page_type TEXT NOT NULL, version INTEGER NOT NULL,
    path TEXT NOT NULL, fingerprint TEXT, created_at TEXT NOT NULL,
    PRIMARY KEY (site, page_type, version));
CREATE TABLE seen (site TEXT NOT NULL, page_type TEXT NOT NULL, item_id TEXT NOT NULL,
    first_seen TEXT NOT NULL, UNIQUE (site, page_type, item_id));
CREATE TABLE items (site TEXT NOT NULL, page_type TEXT NOT NULL, item_id TEXT NOT NULL,
    data_json TEXT NOT NULL, captured_at TEXT NOT NULL);
"""


def test_existing_db_migrates_and_old_fingerprint_is_rebaselined(har_page, tmp_path, caplog):
    db = tmp_path / "surf.db"
    path = sitemap.save_map(tmp_path, dict(DRIFT_MAP, fingerprint="sha256-from-v1-scheme"))
    conn = sqlite3.connect(db)
    conn.executescript(OLD_SCHEMA)
    conn.execute("INSERT INTO maps VALUES ('drifttest', 'feed', 1, ?, 'sha256-from-v1-scheme', 'then')",
                 (str(path),))
    conn.execute("INSERT INTO seen VALUES ('drifttest', 'feed', 'd1', 'then')")
    conn.commit()
    conn.close()

    with Store(db) as store:
        cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(maps)")}
        assert {"dry_run_items", "last_fingerprint", "drift_count"} <= cols
        row = store.current_map("drifttest", "feed")
        assert row["path"] == str(path) and row["drift_count"] == 0 and row["dry_run_items"] is None
        assert store.is_seen("drifttest", "feed", "d1")

        for _ in range(4):  # an old-scheme fingerprint is replaced, never counted as drift
            result, warnings = run(store, har_page, "base", caplog)
            assert warnings == [] and result.drift_count == 0
        assert store.current_map("drifttest", "feed")["fingerprint"] == result.fingerprint
    with Store(db):  # reopening an already-migrated DB is a no-op
        pass


def test_rebaseline_command(tmp_path, monkeypatch, capsys, har_page, caplog):
    """Fix 31: maps rebaseline stores the current page's fingerprint; one tab, no model."""
    from agent_surf import cli
    from test_cli import FakeSession

    home = tmp_path / "home"
    monkeypatch.setenv("AGENT_SURF_HOME", str(home))
    path = sitemap.save_map(home / "maps", dict(DRIFT_MAP, fingerprint="v2-sha256-stale"))
    with Store(home / "surf.db") as s:
        s.add_map("drifttest", "feed", 1, path, "v2-sha256-stale", dry_run_items=4)
        s.update_drift("drifttest", "feed", 1, last_fingerprint="x", drift_count=8)
    opened = []

    class Session(FakeSession):
        def new_page(self, site):
            opened.append(site.name)
            return har_page("drift.har", DRIFT_SITE)

    monkeypatch.setattr(cli, "BrowserSession", Session(None))
    monkeypatch.setattr(cli, "make_client", lambda cfg: pytest.fail("no model client"))
    assert cli.main(["maps", "rebaseline", "drifttest", "feed", "--handle", "redesign", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["fingerprint"].startswith(sitemap.FINGERPRINT_PREFIX) and out["items_on_page"] == 4
    assert opened == ["drifttest"]
    with Store(home / "surf.db") as s:
        row = s.current_map("drifttest", "feed")
        assert row["fingerprint"] == out["fingerprint"] and row["drift_count"] == 0
        result = runner.run(s, har_page("drift.har", DRIFT_SITE), "drifttest", "feed", handle="redesign")
        assert result.drift_count == 0
    # a page the map extracts nothing from is never baselined
    with Store(home / "surf.db") as s:
        empty = har_page("drift.har", DRIFT_SITE)
        empty._page.route("**/drift/empty", lambda r: r.fulfill(
            status=200, content_type="text/html", body="<main><h1>Feed</h1></main>"))
        with pytest.raises(runner.MapBroken, match="not rebaselined"):
            runner.rebaseline(s, empty, "drifttest", "feed", handle="empty")
        assert s.current_map("drifttest", "feed")["fingerprint"] == out["fingerprint"]
