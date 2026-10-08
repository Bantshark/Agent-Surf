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


# ---------------------------------------------------------------------------
# Fake compose.test backend, served through Playwright routes (offline).

import html
import json
import re


class FakeBackend:
    """mode: "ok" -> create returns an id; "errors" -> HTTP 200 with {"errors": [...]};
    "drop" -> the post is created server-side but the connection is lost."""

    def __init__(self, mode="ok"):
        self.mode = mode
        self.posts = {}            # id -> text, newest last
        self.create_calls = 0
        self.bodies = []           # create request bodies, in order
        self.next_id = 1000
        self.permalink_override = None

    def install(self, ctx):
        ctx.route(re.compile(r"^https://compose\.test/api/create"), self._create)
        ctx.route(re.compile(r"^https://compose\.test/post/\w+$"), self._permalink)
        ctx.route(re.compile(r"^https://compose\.test/profile$"), self._profile)

    def _create(self, route):
        if route.request.method != "POST":
            return route.fulfill(status=405, body="")
        self.create_calls += 1
        body = json.loads(route.request.post_data or "{}")
        self.bodies.append(body)
        if self.mode == "errors":
            return route.fulfill(status=200, json={"errors": [{"message": "Something went wrong"}]})
        pid = str(self.next_id)
        self.next_id += 1
        self.posts[pid] = body.get("text", "")
        if self.mode == "drop":
            return route.abort()
        route.fulfill(status=200, json={"data": {"create": {"result": {"id": pid}}}})

    @staticmethod
    def _article(pid, text):
        return (f'<article data-post-id="{pid}"><p class="text">{html.escape(text)}</p></article>')

    def _permalink(self, route):
        pid = route.request.url.rsplit("/", 1)[-1]
        text = self.permalink_override if self.permalink_override is not None else self.posts.get(pid)
        if text is None:
            return route.fulfill(status=404, content_type="text/html", body="<h1>Not found</h1>")
        route.fulfill(status=200, content_type="text/html; charset=utf-8",
                      body=f"<title>Post</title><main>{self._article(pid, text)}</main>")

    def _profile(self, route):
        arts = "".join(self._article(pid, t) for pid, t in reversed(list(self.posts.items())))
        route.fulfill(status=200, content_type="text/html; charset=utf-8",
                      body=f"<title>Profile</title><main><h1>My posts</h1>{arts}</main>")
