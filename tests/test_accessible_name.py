"""Fix 25: accessible names follow aria-labelledby, aria-label, labels/alt/title,
innerText, value; every label check (click allowlists, DESTRUCTIVE, submit
allowlist, the reading side's safe clicks) uses them."""

import pytest

from agent_surf import browser, executor
from agent_surf.executor import Refused, StepRunner, Submitter, accessible_name

from composekit import good_map
from conftest import COMPOSE_SITE, FEED_SITE

HTML = """<!doctype html><html><body>
<span id="a">Delete</span><span id="b"> the   post </span><span id="hidden" hidden>Hidden name</span>
<button id="by" aria-labelledby="a b" aria-label="Close">Post</button>
<button id="byhidden" aria-labelledby="hidden">Visible</button>
<button id="bymissing" aria-labelledby="nope" aria-label="Fallback">Text</button>
<button id="aria" aria-label="Close dialog">X</button>
<label for="inp">Search posts</label><input id="inp" title="tooltip">
<input id="img" type="image" alt="Send now" src="data:,">
<button id="titled" title="More options"></button>
<button id="inner">Show more</button>
<input id="val" type="button" value="Post">
<button id="imgalt"><img alt="Reply" src="data:,"></button>
</body></html>"""


@pytest.fixture
def page(chromium):
    ctx = chromium.new_context()
    p = ctx.new_page()
    p.set_content(HTML)
    yield p
    ctx.close()


@pytest.mark.parametrize("el,name", [
    ("by", "Delete the post"),            # aria-labelledby: referenced texts joined, beats aria-label/text
    ("byhidden", "Hidden name"),          # a hidden referenced element still names it
    ("bymissing", "Fallback"),            # unknown id: next source
    ("aria", "Close dialog"),             # aria-label beats innerText
    ("inp", "Search posts"),              # <label for>
    ("img", "Send now"),                  # alt
    ("titled", "More options"),           # title
    ("inner", "Show more"),               # innerText
    ("val", "Post"),                      # value
    ("imgalt", "Reply"),                  # descendant img alt
])
def test_each_name_source(page, el, name):
    assert accessible_name(page.locator("#" + el))[0] == name


def test_sources_list_every_name(page):
    name, sources = accessible_name(page.locator("#by"))
    assert name == "Delete the post"
    assert {"Delete the post", "Close", "Post"} <= set(sources)


def runner_on(page, cls=StepRunner, **changes):
    from agent_surf.executor import ActionPage

    m = good_map(**changes)
    return cls(ActionPage(page, COMPOSE_SITE), m, {"text": "x"})


def test_click_check_refuses_destructive_name_from_labelledby(page):
    run = runner_on(page)
    with pytest.raises(Refused, match="Delete the post.*destructive"):
        run._check_click_allowed(page.locator("#by"), 0, ("Post", "Close"))


def test_click_check_uses_resolved_name_for_allowlist(page):
    run = runner_on(page)
    run._check_click_allowed(page.locator("#aria"), 0, ("Close dialog",))   # allowed by aria-label
    with pytest.raises(Refused, match="not in the click allowlist"):
        run._check_click_allowed(page.locator("#aria"), 0, ("X",))           # innerText is not the name


def test_click_check_could_publish_sees_every_source(page):
    run = runner_on(page)
    run.composing = True
    page.set_content('<span id="c">Close</span><button id="b" aria-labelledby="c">Post</button>')
    with pytest.raises(Refused, match="could publish"):
        run._check_click_allowed(page.locator("#b"), 0, ("Close",))


def test_submit_allowlist_uses_labelledby(page):
    page.set_content('<span id="s">Schedule</span><button id="b" aria-labelledby="s">Post</button>')
    run = runner_on(page, Submitter, submit={"target": {"role": "button", "name": "Schedule"},
                                             "label_allowlist": ["Post"]}, composer=None)
    with pytest.raises(Refused, match="'Schedule' is not in the allowlist"):
        run._checked_submit()


def test_submit_refuses_destructive_labelledby(page):
    page.set_content('<span id="d">Delete</span><button id="b" aria-labelledby="d">Post</button>')
    run = runner_on(page, Submitter, submit={"target": {"role": "button", "name": "Delete"},
                                             "label_allowlist": ["Post"]}, composer=None)
    with pytest.raises(Refused, match="destructive"):
        run._checked_submit()


def test_submit_allowed_name_via_labelledby(page):
    page.set_content('<span id="p">Post</span><button id="b" aria-labelledby="p">Send it</button>')
    run = runner_on(page, Submitter, submit={"target": {"role": "button", "name": "Post"},
                                             "label_allowlist": ["Post"]}, composer=None)
    assert run._checked_submit()[1] == "Post"


def test_reading_side_safe_click_checks_labelledby(page):
    page.set_content('<span id="u">Unfollow</span>'
                     '<button class="more" aria-labelledby="u">Show more</button>'
                     '<button class="more">Show more</button>')
    cands = page.evaluate(browser._CLICK_CANDIDATES_JS, ["button.more", 5])
    assert cands[0]["label"] == "Unfollow" and not browser.safe_to_click(cands[0])
    assert cands[1]["label"] == "Show more" and browser.safe_to_click(cands[1])


def test_text_fields_are_named_by_labels_not_by_what_was_typed(page):
    page.set_content('<div id="ed" role="textbox" contenteditable="true" aria-label="Post text">'
                     'please delete this and unfollow them</div>'
                     '<textarea id="ta" placeholder="Write a comment">Delete everything</textarea>')
    name, sources = accessible_name(page.locator("#ed"))
    assert name == "Post text" and sources == ["Post text"]
    assert accessible_name(page.locator("#ta")) == ("Write a comment", ["Write a comment"])
    run = runner_on(page)
    run.composing = True
    run._check_click_allowed(page.locator("#ed"), 0, ("Post text",))   # typed text never blocks the box
