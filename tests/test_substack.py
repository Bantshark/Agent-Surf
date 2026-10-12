"""Fix 33: Substack (reading and Notes) under the existing rules: subdomains
of substack.com, custom publication domains only when added, URL templates
and handle validation, logged-out markers, and Notes/comment action maps with
Substack labels."""

import json

import pytest

from agent_surf import actionmap, challenge, cli, executor, publisher, sites
from agent_surf.executor import Submitter
from agent_surf.store import Store

from composekit import FakeBackend, good_map


@pytest.fixture(autouse=True)
def restore_substack():
    """add-domain changes the registry; put the built-in Substack site back."""
    original = sites.get_site("substack")
    yield
    sites.SITES["substack"] = original


def allowed(url):
    return sites.is_allowed_url(sites.get_site("substack"), url)


@pytest.mark.parametrize("url", [
    "https://substack.com/notes", "https://astral.substack.com/p/x", "https://www.substack.com/",
    "https://a-b-c.substack.com/archive?sort=new",
])
def test_substack_domains_and_subdomains_allowed(url):
    assert allowed(url)


@pytest.mark.parametrize("url", [
    "https://substack.com.evil.test/notes", "https://evilsubstack.com/", "https://substack.co/",
    "http://substack.com/notes", "https://astral.substack.com.evil.test/p/x", "https://notsubstack.com/p/x",
])
def test_look_alikes_refused(url):
    assert not allowed(url)
    with pytest.raises(sites.DomainRefused):
        sites.check_url("substack", url)


def test_url_templates():
    b = sites.build_url
    assert b("substack", "publication", handle="astral") == "https://astral.substack.com/archive?sort=new"
    assert b("substack", "notes") == "https://substack.com/notes"
    assert b("substack", "search", query="fintech africa") == \
        "https://substack.com/search/fintech%20africa?searching=all_posts"
    assert b("substack", "profile", handle="someone") == "https://substack.com/@someone"
    assert b("substack", "notifications") == "https://substack.com/activity"
    assert "notifications" in sites.INBOX_PAGE_TYPES["substack"]
    assert sites.ACTION_START["substack"]["post"] == "https://substack.com/notes"
    assert not sites.media_required("substack", "post") and not sites.media_required("substack", "comment")
    assert sites.takes_query("substack", "search")


@pytest.mark.parametrize("handle", ["evil.test", "a/b", "-astral", "astral.substack.com.evil", "UPPER_case",
                                    "x" * 64])
def test_publication_handle_must_be_a_subdomain_label(handle):
    with pytest.raises(ValueError):
        sites.build_url("substack", "publication", handle=handle)


def test_post_handle_is_a_validated_url():
    url = "https://astral.substack.com/p/hello-world"
    assert sites.build_url("substack", "post", handle=url) == url
    for bad in ("https://evil.test/p/hello", "https://astral.substack.com/archive",
                "http://astral.substack.com/p/x", "https://user:pw@astral.substack.com/p/x",
                "https://astral.substack.com:8443/p/x", "astral.substack.com/p/x",
                "https://news.example.org/p/hello"):
        with pytest.raises((ValueError, sites.DomainRefused)):
            sites.build_url("substack", "post", handle=bad)


def test_custom_domain_validation():
    n = sites.normalize_custom_domain
    assert n("substack", "News.Example.ORG.") == "news.example.org"
    for bad in ("*.example.org", "example", "https://news.example.org", "news.example.org/p",
                "news.example.org:443", "10.0.0.1", "x.com", "www.reddit.com", "a..b", ""):
        with pytest.raises(ValueError):
            n("substack", bad)
    with pytest.raises(ValueError, match="does not take custom domains"):
        n("x", "news.example.org")


def test_add_domain_persists_and_joins_the_domain_lock(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_SURF_HOME", str(tmp_path / "home"))
    url = "https://news.example.org/p/hello"
    assert not allowed(url)
    assert cli.main(["sites", "add-domain", "substack", "News.Example.org", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"site": "substack", "domain": "news.example.org",
                                                   "added": True}
    with Store(tmp_path / "home" / "surf.db") as s:
        assert [r["domain"] for r in s.site_domains("substack")] == ["news.example.org"]
    # a fresh process: the registry starts without it, the CLI loads it from the store
    sites.SITES["substack"] = sites.Site("substack", ("substack.com",),
                                         dict(sites.get_site("substack").page_types))
    assert not allowed(url)
    assert cli.main(["sites", "list", "--json"]) == 0
    rows = {r["site"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["substack"]["domains"] == ["substack.com", "news.example.org"]
    assert allowed(url) and sites.build_url("substack", "post", handle=url) == url
    assert not allowed("https://evil.example.org/p/hello")   # no wildcard
    assert cli.main(["sites", "add-domain", "substack", "*.example.org"]) == 1
    assert cli.main(["sites", "add-domain", "x", "news.example.org"]) == 1
    assert cli.main(["sites", "remove-domain", "substack", "news.example.org"]) == 0
    with Store(tmp_path / "home" / "surf.db") as s:
        assert s.site_domains() == []


@pytest.mark.parametrize("url,title", [
    ("https://substack.com/sign-in?redirect=%2Fnotes", ""),
    ("https://substack.com/account/login", ""),
    ("https://astral.substack.com/sign-in", ""),
    ("https://substack.com/", "Sign in - Substack"),
])
def test_logged_out_markers(url, title):
    assert challenge.detect_logged_out("substack", url, title)


def test_not_logged_out_on_normal_pages():
    assert challenge.detect_logged_out("substack", "https://substack.com/notes", "Notes") is None
    assert challenge.detect_logged_out("substack", "https://astral.substack.com/p/sign-in-tips", "x") is None


def substack_map(action="post", **changes):
    """A Notes-like action map, checked against the Substack site rules."""
    m = good_map(site="substack", action=action, lookup=None, **changes)
    m["start"] = {"url_template": "https://substack.com/notes" if action == "post" else "{target_url}",
                  "requires": ["text"] + (["target_url"] if action == "comment" else [])}
    m["steps"] = [{"op": "click", "target": {"role": "button", "name": "What’s on your mind?"}}] + m["steps"]
    m["confirm"] = {"network": {"url_regex": "/api/v1/comment/feed", "method": "POST", "id_path": "id"}}
    m["permalink_template"] = "https://substack.com/@me/note/c-{id}"
    return m


@pytest.mark.parametrize("action", ["post", "comment"])
def test_substack_action_maps_validate(action):
    m = dict(substack_map(action), version=1)
    if action == "comment":
        m["submit"] = {"target": {"role": "button", "name": "Post"}, "label_allowlist": ["Post", "Reply"]}
    assert actionmap.validate_action_map(m) == []


def test_substack_map_cannot_click_unknown_or_destructive_labels():
    m = dict(substack_map(), version=1)
    m["steps"][0] = {"op": "click", "target": {"role": "button", "name": "Send to everyone"}}
    assert actionmap.validate_action_map(m)
    m["steps"][0] = {"op": "click", "target": {"role": "button", "name": "Delete note"}}
    assert actionmap.validate_action_map(m)


def test_notes_style_prompt_on_compose_test(store, compose, tmp_path, monkeypatch):
    """The Substack labels drive a real composer: the prompt opens it, the text
    is typed, and only the allowlisted Post submits (offline, compose.test)."""
    monkeypatch.setattr(executor, "STEP_TIMEOUT_S", 3.0)
    monkeypatch.setattr(publisher, "CONFIRM_TIMEOUT_S", 3.0)
    monkeypatch.setattr(publisher, "READBACK_TIMEOUT_S", 3.0)
    m = good_map(lookup=None)
    m["start"] = dict(m["start"], url_template="https://compose.test/compose-notes")
    m["steps"] = [{"op": "click", "target": {"role": "button", "name": "What’s on your mind?"}}] + m["steps"]
    assert actionmap.validate_action_map(dict(m, version=1)) == []
    page, backend = compose()
    sub = Submitter(page, m, {"text": "A synthetic note"})
    sub.start()
    sub.run_steps()
    sub.submit()
    page.raw.wait_for_timeout(500)
    assert backend.create_calls == 1 and list(backend.posts.values()) == ["A synthetic note"]
