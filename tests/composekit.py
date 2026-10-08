"""Shared pieces for the v2 tests: a known-good action map for the synthetic
https://compose.test site."""

import copy

GOOD_MAP = {
    "site": "composetest",
    "action": "post",
    "start": {"url_template": "https://compose.test/compose", "requires": ["text"]},
    "steps": [
        {"op": "click", "target": {"role": "textbox", "name": "Post text"}},
        {"op": "type", "target": {"role": "textbox", "name": "Post text", "testid": "editor"}, "value": "{text}"},
        {"op": "attach", "target": {"css": "input[type=file]"}, "value": "{media}",
         "preview": {"role": "img", "name": "Uploaded thumbnail"}},
    ],
    "submit": {"target": {"role": "button", "name": "Post", "testid": "submit"}, "label_allowlist": ["Post"]},
    "discard": [
        {"op": "click", "target": {"role": "button", "name": "Close"}},
        {"op": "click", "target": {"role": "button", "name": "Discard"}},
        {"op": "wait_for", "target": {"testid": "editor"}, "state": "hidden"},
    ],
    "confirm": {"network": {"url_regex": "/api/create", "method": "POST",
                            "id_path": "data.create.result.id", "error_path": "errors"}},
    "permalink_template": "https://compose.test/post/{id}",
    "limits": {"per_hour": 10, "per_day": 50, "min_spacing_s": 30},
    "lookup": {"page_type": "profile"},
}


def good_map(**changes):
    m = copy.deepcopy(GOOD_MAP)
    for k, v in changes.items():
        if v is None:
            m.pop(k, None)
        else:
            m[k] = v
    return m
