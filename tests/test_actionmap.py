"""v2 Unit 3: action map schema, validation, versions and target resolution."""

import copy

import pytest

from agent_surf import actionmap
from agent_surf.actionmap import resolve_target, validate_action_map

from composekit import GOOD_MAP, good_map


def test_good_map_is_valid():
    assert validate_action_map(GOOD_MAP) == []


def _bad(mutate):
    m = copy.deepcopy(GOOD_MAP)
    mutate(m)
    return validate_action_map(m)


def step(**kw):
    return lambda m: m["steps"].append(kw)


BAD = {
    "unknown top key": lambda m: m.update(script="alert(1)"),
    "missing submit": lambda m: m.pop("submit"),
    "unknown site": lambda m: m.update(site="myspace"),
    "bad action": lambda m: m.update(action="like"),
    "unknown op": step(op="evaluate", target={"css": "body"}),
    "submit op in steps": step(op="submit", target={"css": "button"}),
    "discard op in steps": step(op="discard"),
    "unknown step key": lambda m: m["steps"][0].update(script="x"),
    "literal text to type": lambda m: m["steps"][1].update(value="buy now"),
    "placeholder mixed with text": lambda m: m["steps"][1].update(value="{text} buy now"),
    "wrong placeholder for type": lambda m: m["steps"][1].update(value="{media}"),
    "unknown placeholder": lambda m: m["steps"][2].update(value="{password}"),
    "press bad key": step(op="press", value="Control+Enter"),
    "empty target": lambda m: m["steps"][0].update(target={}),
    "blank css": lambda m: m["steps"][0].update(target={"css": "  "}),
    "name without role": lambda m: m["steps"][0].update(target={"name": "Post text"}),
    "unknown target key": lambda m: m["steps"][0].update(target={"xpath": "//div"}),
    "navigate to literal url": step(op="navigate", value="https://evil.test/"),
    "navigate with target": lambda m: (m["steps"].append({"op": "navigate", "value": "{target_url}",
                                                          "target": {"css": "a"}}),
                                       m["start"]["requires"].append("target_url")),
    "url_template off domain": lambda m: m["start"].update(url_template="https://evil.test/compose"),
    "url_template http": lambda m: m["start"].update(url_template="http://compose.test/compose"),
    "requires missing target_url": lambda m: m["start"].update(url_template="{target_url}"),
    "type without requires text": lambda m: m["start"].update(requires=[]),
    "wait_for bad state": step(op="wait_for", target={"css": "x"}, state="focused"),
    "preview on click": lambda m: m["steps"][0].update(preview={"css": "img"}),
    "discard with type": lambda m: m["discard"].append({"op": "type", "target": {"css": "x"}, "value": "{text}"}),
    "allowlist widened": lambda m: m["submit"].update(label_allowlist=["Post", "Delete"]),
    "allowlist from other action": lambda m: m["submit"].update(label_allowlist=["Send"]),
    "allowlist empty": lambda m: m["submit"].update(label_allowlist=[]),
    "confirm empty": lambda m: m.update(confirm={}),
    "confirm bad regex": lambda m: m["confirm"]["network"].update(url_regex="(oops"),
    "confirm GET": lambda m: m["confirm"]["network"].update(method="GET"),
    "confirm bad id_path": lambda m: m["confirm"]["network"].update(id_path="a..b"),
    "permalink no id": lambda m: m.update(permalink_template="https://compose.test/post/"),
    "permalink off domain": lambda m: m.update(permalink_template="https://evil.test/{id}"),
    "per_day over ceiling": lambda m: m["limits"].update(per_day=201),
    "per_hour over per_day": lambda m: m["limits"].update(per_hour=60),
    "spacing under 30": lambda m: m["limits"].update(min_spacing_s=29),
    "unknown limit": lambda m: m["limits"].update(per_minute=1),
    "lookup unknown page type": lambda m: m["lookup"].update(page_type="timeline"),
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_bad_action_maps_report_problems(name):
    assert _bad(BAD[name]), name


def test_reply_map_with_target_url_template_is_valid():
    m = good_map(action="reply", start={"url_template": "{target_url}", "requires": ["text", "target_url"]},
                 submit={"target": {"role": "button", "name": "Reply"}, "label_allowlist": ["Reply"]})
    assert validate_action_map(m) == []


def test_ops_whitelist_is_exactly_the_brief():
    assert set(actionmap.ALL_OPS) == {"navigate", "click", "type", "attach", "press", "wait_for",
                                      "submit", "discard"}
    assert actionmap.PRESS_KEYS == ("Enter", "Escape", "Tab")


def test_save_versions_and_load(store, tmp_path):
    p1 = actionmap.save_action_map(store, tmp_path, good_map(version=99))
    p2 = actionmap.save_action_map(store, tmp_path, good_map())
    assert (p1.name, p2.name) == ("composetest.action-post.v1.json", "composetest.action-post.v2.json")
    assert actionmap.current_action_map(store, "composetest", "post")["version"] == 2
    assert actionmap.current_action_map(store, "composetest", "reply") is None
    with pytest.raises(actionmap.ActionMapError):
        actionmap.save_action_map(store, tmp_path, good_map(steps=[]))


RESOLVE_HTML = """
<button data-testid="hidden-first" style="display:none">Post</button>
<button data-testid="go">Post</button>
<div class="only-css">css only</div>
<div class="two" style="display:none">first hidden</div><div class="two">second visible</div>
<input type="file" style="display:none">
"""


def test_target_resolution_order_and_visibility(chromium):
    page = chromium.new_page()
    page.set_content(RESOLVE_HTML)
    r = resolve_target(page, {"role": "button", "name": "Post", "testid": "go", "css": ".only-css"})
    assert r.how == "role" and r.locator.get_attribute("data-testid") == "go"  # hidden one skipped
    r = resolve_target(page, {"role": "button", "name": "Nope", "testid": "go"})
    assert r.how == "testid"
    r = resolve_target(page, {"role": "button", "name": "Nope", "testid": "nope", "css": ".only-css"})
    assert r.how == "css"
    assert resolve_target(page, {"css": ".two"}).locator.inner_text() == "second visible"
    assert resolve_target(page, {"css": ".missing"}, timeout_s=0.3) is None
    assert resolve_target(page, {"css": "input[type=file]"}) is None
    assert resolve_target(page, {"css": "input[type=file]"}, allow_hidden_file_input=True).how == "css"
    page.close()
