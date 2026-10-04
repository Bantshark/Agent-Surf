import copy
import json
from types import SimpleNamespace

import pytest

from agent_surf import sitemap
from agent_surf.sitemap import extract, get_one, parse_path, validate_map

DOC = {
    "data": {
        "timeline": {
            "instructions": [
                {"entries": [{"entryId": "t-1", "content": {"text": "one"}},
                             {"entryId": "t-2", "content": {"text": "two"}}]},
                {"type": "other"},
                {"entries": [{"entryId": "t-3", "content": {"text": None}}]},
            ]
        }
    },
    "list": [[1, 2], [3]],
}

GOOD = {
    "site": "x",
    "page_type": "search",
    "version": 1,
    "source": "network",
    "network": {
        "url_regex": "SearchTimeline",
        "items_path": "data.search.timeline.instructions[*].entries[*]",
        "id_path": "entryId",
        "fields": {"text": "content.legacy.full_text", "author": "content.user.screen_name"},
    },
    "dom": {
        "item": "article[data-testid=tweet]",
        "id_attr": "aria-labelledby",
        "fields": {"text": "[data-testid=tweetText]"},
    },
    "required_fields": ["text"],
    "fingerprint": "sha256-abc",
    "scroll": {"max_scrolls": 30, "delay_s": 2.0, "stop_after_seen": 5},
    "limits": {"max_items": 200},
    "learned_at": "2026-10-04T00:00:00+00:00",
    "learned_by": "claude-sonnet-5-5",
}


# Path language

def test_path_nesting():
    assert extract(DOC, "data.timeline.instructions[0].entries[1].entryId") == ["t-2"]
    assert get_one(DOC, "data.timeline.instructions[0].entries[0].content.text") == "one"


def test_path_fanout():
    ids = extract(DOC, "data.timeline.instructions[*].entries[*].entryId")
    assert ids == ["t-1", "t-2", "t-3"]
    assert extract(DOC, "list[*][*]") == [1, 2, 3]
    assert extract([{"a": 1}, {"a": 2}], "[*].a") == [1, 2]


def test_path_missing_keys_never_raise():
    assert extract(DOC, "data.nope.deeper") == []
    assert extract(DOC, "data.timeline.instructions[9].entries") == []
    assert extract(DOC, "list.key") == []
    assert extract(DOC, "data[0]") == []
    assert extract("scalar", "a.b[*]") == []
    assert extract(None, "a") == []
    assert get_one(DOC, "data.timeline.instructions[2].entries[0].content.text") is None


@pytest.mark.parametrize("bad", ["", " ", ".a", "a.", "a..b", "a[", "a[x]", "a[-1]", "a[0]b"])
def test_path_syntax_errors(bad):
    with pytest.raises(sitemap.PathError):
        parse_path(bad)


# validate_map

def test_good_map_passes():
    assert validate_map(GOOD) == []


def test_minimal_dom_map_passes():
    m = {k: copy.deepcopy(v) for k, v in GOOD.items()
         if k not in ("network", "version", "fingerprint", "learned_at", "learned_by")}
    m["source"] = "dom"
    assert validate_map(m) == []


def _bad(mutate):
    m = copy.deepcopy(GOOD)
    mutate(m)
    return validate_map(m)


BAD_CASES = {
    "unknown top-level key": lambda m: m.update(actions=[{"type": "type"}]),
    "missing required key": lambda m: m.pop("scroll"),
    "bad site name": lambda m: m.update(site="../etc"),
    "bad source": lambda m: m.update(source="both"),
    "source block absent": lambda m: m.pop("network"),
    "regex does not compile": lambda m: m["network"].update(url_regex="(unclosed"),
    "empty regex": lambda m: m["network"].update(url_regex=""),
    "bad items_path": lambda m: m["network"].update(items_path="a..b"),
    "bad id_path": lambda m: m["network"].update(id_path=""),
    "bad field path": lambda m: m["network"]["fields"].update(text="x["),
    "unknown network key": lambda m: m["network"].update(click="button"),
    "empty item css": lambda m: m["dom"].update(item="  "),
    "empty field css": lambda m: m["dom"]["fields"].update(text=""),
    "empty id_attr": lambda m: m["dom"].update(id_attr=""),
    "reserved field": lambda m: m["network"]["fields"].update(item_id="entryId"),
    "required not a field": lambda m: m.update(required_fields=["title"]),
    "max_scrolls over ceiling": lambda m: m["scroll"].update(max_scrolls=101),
    "max_items over ceiling": lambda m: m["limits"].update(max_items=1001),
    "delay too short": lambda m: m["scroll"].update(delay_s=0.5),
    "stop_after_seen zero": lambda m: m["scroll"].update(stop_after_seen=0),
    "bool is not int": lambda m: m["scroll"].update(max_scrolls=True),
    "bad version": lambda m: m.update(version=0),
}


@pytest.mark.parametrize("name", sorted(BAD_CASES))
def test_bad_maps_report_problem(name):
    problems = _bad(BAD_CASES[name])
    assert len(problems) == 1, (name, problems)


def test_ceilings_inclusive():
    def edge(m):
        m["scroll"].update(max_scrolls=100, delay_s=1.0)
        m["limits"].update(max_items=1000)
    assert _bad(edge) == []


def test_non_dict():
    assert validate_map([]) == ["map must be a JSON object"]


# Items

def test_items_from_responses():
    net = {"url_regex": "/api/feed", "items_path": "data.items[*]", "id_path": "id",
           "fields": {"text": "body", "author": "user.name"}}
    resps = [
        SimpleNamespace(url="https://feed.test/api/feed?page=1",
                        data={"data": {"items": [{"id": 7, "body": "hi", "user": {"name": "a"}},
                                                 {"body": "no id"}]}}),
        SimpleNamespace(url="https://feed.test/api/other", data={"data": {"items": [{"id": 9}]}}),
    ]
    items = sitemap.items_from_responses(net, resps)
    assert items == [
        {"item_id": "7", "fields": {"text": "hi", "author": "a"}},
        {"item_id": None, "fields": {"text": "no id", "author": None}},
    ]
    assert sitemap.has_required(items[0]["fields"], ["text"])
    assert not sitemap.has_required({"text": "  "}, ["text"])


# Fingerprint

SNAP_A = """- main:
  - heading "Feed" [level=1]
  - article:
    - paragraph: first post
    - link "alice":
      - /url: https://feed.test/u/alice
  - article:
    - paragraph: second post
    - link "bob":
      - /url: https://feed.test/u/bob
"""

SNAP_B = """- main:
  - heading "Another title" [level=1]
  - article:
    - paragraph: something else entirely
    - link "carol":
      - /url: https://feed.test/u/carol
"""

SNAP_C = """- main:
  - heading "Feed" [level=1]
  - article:
    - paragraph: first post
  - article:
    - paragraph: second post
"""


def test_fingerprint_ignores_text_and_counts():
    assert sitemap.fingerprint(SNAP_A) == sitemap.fingerprint(SNAP_B)
    assert sitemap.fingerprint(SNAP_A).startswith("sha256-")


def test_fingerprint_detects_structure_change():
    assert sitemap.fingerprint(SNAP_A) != sitemap.fingerprint(SNAP_C)


# Files

def test_save_and_load(tmp_path):
    path = sitemap.save_map(tmp_path, GOOD)
    assert path.name == "x.search.v1.json"
    assert sitemap.load_map(path) == GOOD


def test_load_rejects_invalid(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"site": "x"}))
    with pytest.raises(sitemap.MapError):
        sitemap.load_map(p)
    with pytest.raises(sitemap.MapError):
        sitemap.save_map(tmp_path, {"site": "x"})
