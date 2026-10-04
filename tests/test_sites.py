import pytest

from agent_surf import sites
from agent_surf.sites import DomainRefused, build_url, check_url

INITIAL = {
    "x": {"home", "search", "profile"},
    "reddit": {"subreddit", "post", "search"},
    "instagram": {"profile", "feed"},
    "facebook": {"page", "feed"},
    "linkedin": {"profile", "feed", "jobs"},
}


def test_initial_registry():
    for name, page_types in INITIAL.items():
        assert set(sites.get_site(name).page_types) == page_types


def test_build_url_fills_and_encodes():
    assert build_url("x", "search", query="agent surf") == "https://x.com/search?q=agent%20surf&f=live"
    assert build_url("x", "profile", handle="someone") == "https://x.com/someone"
    assert build_url("reddit", "subreddit", handle="python") == "https://www.reddit.com/r/python/new/"
    assert build_url("linkedin", "feed") == "https://www.linkedin.com/feed/"


def test_placeholder_cannot_escape_path():
    url = build_url("x", "profile", handle="../../evil.com/x")
    assert url.startswith("https://x.com/") and "/" not in url[len("https://x.com/"):]
    url = build_url("x", "profile", handle="@evil.com")
    check_url("x", url)


def test_missing_and_extra_placeholders():
    with pytest.raises(ValueError):
        build_url("x", "search")
    with pytest.raises(ValueError):
        build_url("x", "home", query="q")
    with pytest.raises(ValueError):
        build_url("x", "search", query="  ")
    with pytest.raises(sites.UnknownSite):
        build_url("x", "jobs")
    with pytest.raises(sites.UnknownSite):
        build_url("myspace", "feed")


@pytest.mark.parametrize("url", [
    "https://x.com/home",
    "https://mobile.x.com/home",
    "https://X.COM/search?q=1",
])
def test_domain_lock_allows_own_domain(url):
    check_url("x", url)


@pytest.mark.parametrize("url", [
    "https://evil.com/",
    "https://x.com.evil.com/",
    "https://notx.com/",
    "https://x.com@evil.com/",
    "http://x.com/home",
    "javascript:alert(1)",
    "file:///etc/passwd",
    "https://reddit.com/",
    "",
])
def test_domain_lock_refuses_off_site(url):
    with pytest.raises(DomainRefused):
        check_url("x", url)


def test_register_rejects_off_domain_template():
    with pytest.raises(DomainRefused):
        sites.register_site(sites.Site("bad", ("feed.test",), {"home": "https://other.test/"}))
    assert "bad" not in sites.SITES
