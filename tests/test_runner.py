import copy

import pytest

from agent_surf import runner, sitemap
from agent_surf.runner import MapBroken, NoMap

FEED_MAP = {
    "site": "feedtest",
    "page_type": "home",
    "version": 1,
    "source": "network",
    "network": {
        "url_regex": r"/api/feed\?",
        "items_path": "data.feed.edges[*].node",
        "id_path": "id",
        "fields": {"text": "body.text", "author": "author.handle"},
    },
    "dom": {
        "item": "article[data-post-id]",
        "id_attr": "data-post-id",
        "fields": {"text": ".text", "author": ".author"},
    },
    "required_fields": ["text"],
    "scroll": {"max_scrolls": 6, "delay_s": 1.0, "stop_after_seen": 3},
    "limits": {"max_items": 200},
}


def make_map(**changes):
    m = copy.deepcopy(FEED_MAP)
    for k, v in changes.items():
        if v is None:
            m.pop(k, None)
        else:
            m[k] = v
    assert sitemap.validate_map(m) == []
    return m


def save(store, tmp_path, m):
    path = sitemap.save_map(tmp_path, m)
    store.add_map(m["site"], m["page_type"], m["version"], path, m.get("fingerprint"))


def ids(result):
    return [it["item_id"] for it in result.items]


ALL_IDS = [f"p{i}" for i in range(1, 16)]


def test_refuses_without_map(store, har_page):
    with pytest.raises(NoMap, match="agent-surf learn feedtest home"):
        runner.run(store, har_page("feed.har"), "feedtest", "home")


def test_network_extracts_all_items(store, har_page, tmp_path):
    save(store, tmp_path, make_map())
    result = runner.run(store, har_page("feed.har"), "feedtest", "home")
    assert result.source == "network"
    assert ids(result) == ALL_IDS
    assert result.items[0] == {"site": "feedtest", "page_type": "home", "item_id": "p1",
                               "text": "Synthetic post 1", "author": "user_a"}
    assert result.stop_reason == "max_scrolls"


def test_second_run_returns_nothing_new(store, har_page, tmp_path):
    save(store, tmp_path, make_map())
    first = runner.run(store, har_page("feed.har"), "feedtest", "home")
    assert len(first.items) == 15
    second = runner.run(store, har_page("feed.har"), "feedtest", "home")
    assert second.items == []
    assert second.stop_reason == "stop_after_seen"
    assert second.scrolls == 0


def test_stops_after_n_seen(store, har_page, tmp_path):
    save(store, tmp_path, make_map())
    for i in range(6, 16):  # pages 2 and 3 already seen
        store.mark_seen("feedtest", "home", f"p{i}")
    result = runner.run(store, har_page("feed.har"), "feedtest", "home")
    assert ids(result) == ["p1", "p2", "p3", "p4", "p5"]
    assert result.stop_reason == "stop_after_seen"


def test_max_items(store, har_page, tmp_path):
    save(store, tmp_path, make_map(limits={"max_items": 4}))
    result = runner.run(store, har_page("feed.har"), "feedtest", "home")
    assert ids(result) == ["p1", "p2", "p3", "p4"]
    assert result.stop_reason == "max_items"


def test_dom_fallback_when_network_block_absent(store, har_page, tmp_path):
    save(store, tmp_path, make_map(source="dom", network=None))
    result = runner.run(store, har_page("feed.har"), "feedtest", "home")
    assert result.source == "dom"
    assert ids(result) == ALL_IDS
    assert result.items[2]["text"] == "Synthetic post 3"


def test_dom_fallback_when_network_finds_nothing(store, har_page, tmp_path):
    net = dict(FEED_MAP["network"], url_regex="/api/nothing-here")
    save(store, tmp_path, make_map(network=net))
    result = runner.run(store, har_page("feed.har"), "feedtest", "home")
    assert result.source == "dom"
    assert ids(result) == ALL_IDS


def test_map_broken_when_nothing_found(store, har_page, tmp_path):
    net = dict(FEED_MAP["network"], items_path="data.timeline[*]")
    save(store, tmp_path, make_map(network=net, dom=None))
    with pytest.raises(MapBroken, match="no items"):
        runner.run(store, har_page("feed.har"), "feedtest", "home")
    assert store.conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0


def test_map_broken_when_required_field_missing(store, har_page, tmp_path):
    net = dict(FEED_MAP["network"], fields={"text": "body.html", "author": "author.handle"})
    save(store, tmp_path, make_map(network=net, dom=None))
    with pytest.raises(MapBroken, match="required field"):
        runner.run(store, har_page("feed.har"), "feedtest", "home")


def test_fingerprint_drift_is_reported_not_blocking(store, har_page, tmp_path):
    save(store, tmp_path, make_map(fingerprint=sitemap.FINGERPRINT_PREFIX + "stale"))
    for _ in range(runner.DRIFT_RUNS - 1):
        assert runner.run(store, har_page("feed.har"), "feedtest", "home").drift is False
    result = runner.run(store, har_page("feed.har"), "feedtest", "home")
    assert result.drift is True
    assert result.stop_reason == "stop_after_seen"  # the run itself went ahead


def test_guard_called_after_navigation_and_each_scroll(store, har_page, tmp_path):
    save(store, tmp_path, make_map(scroll={"max_scrolls": 2, "delay_s": 1.0, "stop_after_seen": 3}))
    calls = []
    result = runner.run(store, har_page("feed.har"), "feedtest", "home", guard=lambda p: calls.append(p.url))
    assert result.scrolls == 2
    assert len(calls) == 2 + result.scrolls
    assert all(u.startswith("https://feed.test/") for u in calls)
