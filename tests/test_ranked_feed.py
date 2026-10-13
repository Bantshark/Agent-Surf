"""Fix 38: algorithmic feeds stop early. A feed whose map extracts a time
field stops once stop_after_older consecutive items are older than the newest
item the previous successful run of the same page delivered."""

import random
from datetime import datetime, timedelta, timezone

import pytest

from agent_surf import runner, sitemap
from agent_surf.store import Store

from test_runner import save

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def dom_map(page_type="home", **changes):
    m = {"site": "feedtest", "page_type": page_type, "version": 1, "source": "dom",
         "dom": {"item": "article[data-id]", "id_attr": "data-id",
                 "fields": {"text": ".text", "created_at": ".time"}},
         "required_fields": ["text"],
         "scroll": {"max_scrolls": 2, "delay_s": 1.0, "stop_after_seen": 3},
         "limits": {"max_items": 200}}
    m.update(changes)
    assert sitemap.validate_map(m) == []
    return m


def html(posts):
    arts = "".join(f'<article data-id="{pid}"><p class="text">post {pid}</p>'
                   f'<span class="time">{t.isoformat().replace("+00:00", "Z")}</span></article>'
                   for pid, t in posts)
    return f"<!doctype html><html><head><title>Feed</title></head><body><main>{arts}</main></body></html>"


def serve(page, pattern, pages):
    """Each load of ``pattern`` serves the next HTML in ``pages``."""
    queue = list(pages)

    def handle(route):
        route.fulfill(status=200, content_type="text/html; charset=utf-8", body=queue.pop(0))

    page._page.route(pattern, handle)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def test_shuffled_feed_stops_on_the_second_run(store, har_page, tmp_path):
    rng = random.Random(7)
    first = [(f"a{i}", at(-i)) for i in range(30)]                    # newest: T0
    second = [(f"n{i}", at(10 - i)) for i in range(3)]                 # 3 newer than T0 ...
    older = [(f"b{i}", at(-100 - i)) for i in range(27)]               # ... then never-seen older ones
    rng.shuffle(older)
    page = har_page("feed.har")
    serve(page, "https://feed.test/", [html(first), html(second + older)])
    save(store, tmp_path, dom_map())

    r1 = runner.run(store, page, "feedtest", "home")
    assert len(r1.items) == 30 and r1.stop_reason == "max_scrolls"   # nothing to compare with yet
    assert store.high_water("feedtest", "home", "https://feed.test/") == T0.timestamp()

    r2 = runner.run(store, page, "feedtest", "home")
    assert r2.stop_reason == "older_than_last_run" and r2.scrolls == 0
    # the 3 newer items plus the 5 older ones that tripped the stop (unseen, so delivered)
    assert [it["item_id"] for it in r2.items][:3] == ["n0", "n1", "n2"] and len(r2.items) == 8
    assert store.high_water("feedtest", "home", "https://feed.test/") == at(10).timestamp()


def test_chronological_feed_unchanged(store, har_page, tmp_path):
    base = [(f"c{i}", at(-i)) for i in range(10)]
    page = har_page("feed.har")
    serve(page, "https://feed.test/", [html(base), html([("c-new", at(5))] + base)])
    save(store, tmp_path, dom_map())
    runner.run(store, page, "feedtest", "home")
    r2 = runner.run(store, page, "feedtest", "home")
    assert [it["item_id"] for it in r2.items] == ["c-new"]
    assert r2.stop_reason == "stop_after_seen"   # seen ids stop it first, as before


def test_high_water_mark_per_query(store, har_page, tmp_path):
    page = har_page("feed.har")
    serve(page, "https://feed.test/search/*", [html([("qa1", at(1)), ("qa2", at(0))]),
                                               html([("qb1", at(-50)), ("qb2", at(-60))])])
    save(store, tmp_path, dom_map("search"))
    runner.run(store, page, "feedtest", "search", query="alpha")
    runner.run(store, page, "feedtest", "search", query="beta")
    assert store.high_water("feedtest", "search", "https://feed.test/search/alpha") == at(1).timestamp()
    assert store.high_water("feedtest", "search", "https://feed.test/search/beta") == at(-50).timestamp()
    store.set_high_water("feedtest", "search", "https://feed.test/search/alpha", at(-500).timestamp())
    assert store.high_water("feedtest", "search", "https://feed.test/search/alpha") == at(1).timestamp()


def test_ranked_map_caps_scrolls(store, har_page, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "RANKED_MAX_SCROLLS", 1)
    page = har_page("feed.har")
    serve(page, "https://feed.test/", [html([(f"r{i}", at(-i)) for i in range(5)])])
    save(store, tmp_path, dom_map(ranked=True, scroll={"max_scrolls": 4, "delay_s": 1.0,
                                                      "stop_after_seen": 3, "stop_after_older": 2}))
    r = runner.run(store, page, "feedtest", "home")
    assert r.stop_reason == "max_scrolls" and r.scrolls == 1


def test_budget_stopped_run_keeps_the_old_mark(store, har_page, tmp_path, monkeypatch):
    from agent_surf import budget

    page = har_page("feed.har")
    serve(page, "https://feed.test/", [html([("t1", at(0))])])
    save(store, tmp_path, dom_map())
    monkeypatch.setattr(page, "scroll", lambda: (_ for _ in ()).throw(budget.BudgetExceeded("spent")))
    r = runner.run(store, page, "feedtest", "home")
    assert r.stop_reason == "time_budget" and [it["item_id"] for it in r.items] == ["t1"]
    assert store.high_water("feedtest", "home", "https://feed.test/") is None


@pytest.mark.parametrize("value,expected", [
    ("2026-10-01T12:00:00Z", T0),
    ("2026-10-01T12:00:00.000Z", T0),
    ("2026-10-01T14:00:00+02:00", T0),
    ("2026-10-01T12:00:00", T0),                       # no zone: UTC
    ("Thu Oct 01 12:00:00 +0000 2026", T0),           # X legacy created_at
    ("Thu, 01 Oct 2026 12:00:00 GMT", T0),            # RFC 2822
    (int(T0.timestamp()), T0),
    (int(T0.timestamp() * 1000), T0),                 # milliseconds
    (str(int(T0.timestamp())), T0),
    ("5 minutes ago", None), ("", None), (None, None), (True, None), (42, None), ("Oct", None),
])
def test_parse_time(value, expected):
    got = runner.parse_time(value)
    assert got == (expected.timestamp() if expected else None)


def test_validation():
    bad = dict(dom_map(), ranked="yes")
    assert "ranked: must be true or false" in sitemap.validate_map(bad)
    bad = dict(dom_map(), scroll={"max_scrolls": 2, "delay_s": 1.0, "stop_after_seen": 3, "stop_after_older": 0})
    assert any("stop_after_older" in p for p in sitemap.validate_map(bad))
    bad = dict(dom_map(), scroll={"max_scrolls": 2, "delay_s": 1.0, "stop_after_seen": 3, "stop_older": 2})
    assert "unknown key scroll.stop_older" in sitemap.validate_map(bad)


def test_migration_is_additive(tmp_path):
    with Store(tmp_path / "s.db") as s:
        s.set_high_water("x", "home", "https://x.com/home", 1.0e9)
    with Store(tmp_path / "s.db") as s:   # reopening keeps the table and the mark
        assert s.high_water("x", "home", "https://x.com/home") == 1.0e9
