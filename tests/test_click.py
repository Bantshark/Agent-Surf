"""Stored click selectors: only safe "show more" controls are ever clicked."""

import pytest

from agent_surf import runner, sitemap
from agent_surf.browser import safe_to_click

from test_runner import ALL_IDS, ids, make_map, save

SAFE = {"visible": True, "disabled": False, "editable": False, "inForm": False, "inDialog": False,
        "type": "button", "href": None, "text": "Show more", "label": ""}


def cand(**kw):
    return dict(SAFE, **kw)


@pytest.mark.parametrize("c", [
    cand(), cand(text="Load more"), cand(text="View 3 replies"), cand(text="", label="Show more posts"),
    cand(tag="a", href="#"), cand(href="javascript:void(0)"), cand(text="Read more"),
])
def test_safe_candidates(c):
    assert safe_to_click(c)


@pytest.mark.parametrize("c", [
    cand(text="Like"), cand(text="Follow"), cand(text="Reply"), cand(text="Show less"),
    cand(text="Sign in to see more"), cand(text="Continue with Google"), cand(text="Add a comment"),
    cand(text="Share"), cand(text="Open app to see more"), cand(text="Post"),
    cand(text="Show more", label="Like this post"), cand(text=""), cand(text="Show more " * 10),
    cand(visible=False), cand(disabled=True), cand(editable=True), cand(inForm=True),
    cand(inDialog=True), cand(type="submit"), cand(href="/u/someone"),
    cand(href="https://feed.test/next"), cand(text="Submit"),
])
def test_unsafe_candidates(c):
    assert not safe_to_click(c)


def test_click_key_validation():
    m = make_map()
    for bad in ([], ["a"] * 6, [""], "button", [3]):
        m["click"] = bad
        assert sitemap.validate_map(m), bad
    m["click"] = ["button.load-more", ".see-more"]
    assert sitemap.validate_map(m) == []


def more_map(**kw):
    m = make_map(**kw)
    m["page_type"] = "more"
    return m


def test_runner_clicks_load_more(store, har_page, tmp_path):
    m = more_map(scroll={"max_scrolls": 3, "delay_s": 1.0, "stop_after_seen": 3})
    m["click"] = ["button.load-more"]
    save(store, tmp_path, m)
    page = har_page("feed.har")
    result = runner.run(store, page, "feedtest", "more")
    assert ids(result) == ALL_IDS
    assert result.clicks == 2
    assert page._page.evaluate("window.__decoys") == 0


def test_without_click_only_first_page(store, har_page, tmp_path):
    save(store, tmp_path, more_map(scroll={"max_scrolls": 2, "delay_s": 1.0, "stop_after_seen": 3}))
    result = runner.run(store, har_page("feed.har"), "feedtest", "more")
    assert ids(result) == ALL_IDS[:5]
    assert result.clicks == 0


def test_decoys_never_clicked(har_page):
    page = har_page("feed.har")
    page.goto("https://feed.test/more")
    page.wait(1.0)
    assert page.expand([".act", "#controls .act", "form button", "[role=dialog] button", "a"]) == (0, False)
    assert page._page.evaluate("window.__decoys") == 0
    assert page.url == "https://feed.test/more"


def test_click_that_changes_url_stops_clicking(store, har_page, tmp_path):
    page = har_page("feed.har")
    page.goto("https://feed.test/more")
    page.wait(1.0)
    assert page.expand([".nav-more", "button.load-more"]) == (1, True)
    assert page.url == "https://feed.test/more?p=2"


def test_runner_disables_clicks_after_navigation(store, har_page, tmp_path):
    m = more_map(scroll={"max_scrolls": 2, "delay_s": 1.0, "stop_after_seen": 3})
    m["click"] = [".nav-more", "button.load-more"]
    save(store, tmp_path, m)
    result = runner.run(store, har_page("feed.har"), "feedtest", "more")
    assert result.clicks == 1
    assert ids(result) == ALL_IDS[:5]
