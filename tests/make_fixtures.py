"""Regenerate the synthetic HAR fixtures in tests/fixtures/.

Everything here is invented: the feed.test domain, posts and handles. No real
site was recorded. Run: python3 tests/make_fixtures.py
"""

import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
ORIGIN = "https://feed.test"
PAGES = 3
PER_PAGE = 5

FEED_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Feed Test</title>
<style>article { min-height: 300px; border-bottom: 1px solid #ccc; }</style></head>
<body><main id="feed"><h1>Synthetic feed</h1></main>
<script>
let page = 0, loading = false;
const feed = document.getElementById('feed');
async function load() {
  if (loading || page >= %(pages)d) return;
  loading = true;
  page += 1;
  const r = await fetch('/api/feed?page=' + page);
  const j = await r.json();
  for (const edge of j.data.feed.edges) {
    const n = edge.node;
    const a = document.createElement('article');
    a.setAttribute('data-post-id', n.id);
    a.innerHTML = '<p class="text"></p><a class="author" href="#"></a>';
    a.querySelector('.text').textContent = n.body.text;
    a.querySelector('.author').textContent = n.author.handle;
    feed.appendChild(a);
  }
  loading = false;
}
window.addEventListener('scroll', () => {
  if (window.innerHeight + window.scrollY >= document.body.scrollHeight - 200) load();
});
fetch('/api/config').then(r => r.json());
load();
</script></body></html>
""" % {"pages": PAGES}


# Same posts, but more are loaded only by a "Show more posts" button. Decoy
# controls count their clicks in window.__decoys; Agent Surf must never click them.
MORE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Feed Test</title>
<style>article { min-height: 300px; border-bottom: 1px solid #ccc; }</style></head>
<body><main id="feed"><h1>Synthetic feed</h1></main>
<div id="controls">
  <button class="load-more" type="button">Show more posts</button>
  <button class="nav-more" type="button">Show more</button>
  <button class="act" type="button">Like</button>
  <button class="act" type="button">Show less</button>
  <button class="act" type="button" style="display:none">Show more</button>
  <button class="act" type="button" disabled>Show more</button>
  <a class="act" href="/u/someone">Show profile</a>
  <form action="/more"><button class="act">Show more</button></form>
  <div role="dialog"><button class="act" type="button">See more</button></div>
</div>
<script>
window.__decoys = 0;
let page = 0, loading = false;
const feed = document.getElementById('feed');
const more = document.querySelector('.load-more');
for (const el of document.querySelectorAll('.act')) {
  el.addEventListener('click', (e) => { window.__decoys += 1; e.preventDefault(); });
}
document.querySelector('.nav-more').addEventListener('click', () => history.pushState(null, '', '/more?p=2'));
async function load() {
  if (loading || page >= %(pages)d) return;
  loading = true;
  page += 1;
  const r = await fetch('/api/feed?page=' + page);
  const j = await r.json();
  for (const edge of j.data.feed.edges) {
    const n = edge.node;
    const a = document.createElement('article');
    a.setAttribute('data-post-id', n.id);
    a.innerHTML = '<p class="text"></p><a class="author" href="#"></a>';
    a.querySelector('.text').textContent = n.body.text;
    a.querySelector('.author').textContent = n.author.handle;
    feed.appendChild(a);
  }
  if (page >= %(pages)d) more.remove();
  loading = false;
}
more.addEventListener('click', load);
load();
</script></body></html>
""" % {"pages": PAGES}


# Cold start: the page's own script requests the feed (or renders items) only
# after a delay, like a site app booting in a freshly started browser. HAR replay
# answers instantly, so the delay has to come from the page.
LATE_MS = 3000
LATE_HTML = FEED_HTML.replace("\nload();\n", "\nsetTimeout(load, %d);\n" % LATE_MS)
assert LATE_HTML != FEED_HTML

LATE_DOM_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Feed Test</title></head>
<body><main id="feed"><h1>Synthetic feed</h1></main>
<script>
setTimeout(() => {
  const feed = document.getElementById('feed');
  for (let i = 1; i <= 5; i++) {
    const a = document.createElement('article');
    a.setAttribute('data-post-id', 'p' + i);
    a.innerHTML = '<p class="text">Synthetic post ' + i + '</p><a class="author" href="#">user</a>';
    feed.appendChild(a);
  }
}, %(late)d);
</script></body></html>
""" % {"late": LATE_MS}

# The feed is never requested and no items ever render.
NEVER_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Feed Test</title></head>
<body><main id="feed"><h1>Synthetic feed</h1><p>Loading...</p></main></body></html>
"""

# A checkpoint appears while the app is still loading, is cleared (as if the
# human solved it), then the feed loads.
LATE_CHALLENGE_HTML = FEED_HTML.replace("\nload();\n", """
setTimeout(() => history.replaceState(null, '', '/checkpoint/verify'), 1500);
setTimeout(() => { history.replaceState(null, '', '/late-challenge'); load(); }, 3500);
""")
assert LATE_CHALLENGE_HTML != FEED_HTML


# Late feed, but a sidebar list is there from the start (5 list items).
LATE_SIDEBAR_HTML = LATE_HTML.replace(
    '<body><main id="feed">',
    '<body><aside><h2>Trending</h2><ul>'
    + "".join(f'<li><a href="#">topic {i}</a></li>' for i in range(1, 6))
    + '</ul></aside><main id="feed">')
assert LATE_SIDEBAR_HTML != LATE_HTML

# Loaded at once, items in plain divs (no article/listitem roles), no JSON.
STATIC_DIVS_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Feed Test</title></head>
<body><main><h1>Synthetic feed</h1>%s</main></body></html>
""" % "".join(f'<div class="post" data-post-id="p{i}"><span class="text">Synthetic post {i}</span></div>'
              for i in range(1, 6))

# Never shows items but is never quiet: the DOM keeps changing...
TICKING_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Feed Test</title></head>
<body><main><h1>Synthetic feed</h1><p id="t">Loading 0</p></main>
<script>let n = 0; setInterval(() => { document.getElementById('t').textContent = 'Loading ' + (++n); }, 300);</script>
</body></html>
"""
# ...or JSON keeps arriving.
POLLING_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Feed Test</title></head>
<body><main><h1>Synthetic feed</h1><p>Loading</p></main>
<script>setInterval(() => fetch('/api/config'), 300);</script>
</body></html>
"""


def feed_page(n):
    edges = []
    for k in range(PER_PAGE):
        i = (n - 1) * PER_PAGE + k + 1
        edges.append({"cursor": f"c{i}", "node": {
            "id": f"p{i}",
            "body": {"text": f"Synthetic post {i}"},
            "author": {"handle": f"user_{chr(96 + (i % 26 or 26))}"},
        }})
    return {"data": {"feed": {"edges": edges, "page": n}}}


def entry(url, mime, text, status=200):
    return {
        "startedDateTime": "2026-01-01T00:00:00.000Z",
        "time": 1,
        "request": {"method": "GET", "url": url, "httpVersion": "HTTP/1.1",
                    "headers": [], "queryString": [], "headersSize": -1, "bodySize": 0},
        "response": {"status": status, "statusText": "OK", "httpVersion": "HTTP/1.1",
                     "headers": [{"name": "Content-Type", "value": mime}],
                     "content": {"size": len(text), "mimeType": mime, "text": text},
                     "redirectURL": "", "headersSize": -1, "bodySize": len(text)},
        "cache": {}, "timings": {"send": 0, "wait": 1, "receive": 0},
    }


def har(entries):
    return {"log": {"version": "1.2", "creator": {"name": "agent-surf-fixtures", "version": "1"},
                    "pages": [], "entries": entries}}


def build_feed():
    entries = [entry(ORIGIN + "/", "text/html; charset=utf-8", FEED_HTML),
               entry(ORIGIN + "/more", "text/html; charset=utf-8", MORE_HTML),
               entry(ORIGIN + "/late", "text/html; charset=utf-8", LATE_HTML),
               entry(ORIGIN + "/late-dom", "text/html; charset=utf-8", LATE_DOM_HTML),
               entry(ORIGIN + "/never", "text/html; charset=utf-8", NEVER_HTML),
               entry(ORIGIN + "/late-challenge", "text/html; charset=utf-8", LATE_CHALLENGE_HTML),
               entry(ORIGIN + "/late-sidebar", "text/html; charset=utf-8", LATE_SIDEBAR_HTML),
               entry(ORIGIN + "/static-divs", "text/html; charset=utf-8", STATIC_DIVS_HTML),
               entry(ORIGIN + "/ticking", "text/html; charset=utf-8", TICKING_HTML),
               entry(ORIGIN + "/polling", "text/html; charset=utf-8", POLLING_HTML),
               entry(ORIGIN + "/api/config", "application/json",
                     json.dumps({"features": {"dark_mode": True}}))]
    for n in range(1, PAGES + 1):
        entries.append(entry(f"{ORIGIN}/api/feed?page={n}", "application/json", json.dumps(feed_page(n))))
    return har(entries)


def page(title, body):
    return (f"<!doctype html><html><head><meta charset=\"utf-8\"><title>{title}</title></head>"
            f"<body>{body}</body></html>")


CHALLENGE_PAGES = {
    "/clean": page("Feed Test", "<main><h1>Nothing to see</h1></main>"),
    "/checkpoint/start": page("Checkpoint", "<main><h1>Confirm it is you</h1></main>"),
    # Synthetic "solved by the human" case: the path changes after 1.5 s.
    "/checkpoint/clears": page("Checkpoint", "<main><h1>Confirm it is you</h1></main>"
                               "<script>setTimeout(() => history.replaceState(null, '', '/'), 1500)</script>"),
    "/wait": page("Just a moment...", "<main><h1>Checking your browser</h1></main>"),
    "/turnstile": page("Feed Test", '<iframe src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/x"></iframe>'),
    "/recaptcha": page("Feed Test", '<iframe src="https://www.google.com/recaptcha/api2/anchor?k=synthetic"></iframe>'),
    "/hcaptcha": page("Feed Test", '<iframe src="https://newassets.hcaptcha.com/captcha/v1/x/static/hcaptcha.html"></iframe>'),
}


def drift_page(posts, sidebar, *, landmarks=True):
    """Synthetic feed page. ``posts``: list of (id, text or None)."""
    arts = "".join(
        f'<article data-id="{pid}">' + (f'<p class="text">{text}</p>' if text else '<p>(no text)</p>')
        + '<button type="button">Reply</button></article>' for pid, text in posts)
    side = "".join(
        f'<section aria-label="{title}"><h2>{title}</h2><ul>'
        + "".join(f"<li><a href=\"#\">{t}</a></li>" for t in entries) + "</ul>" + extra + "</section>"
        for title, entries, extra in sidebar)
    if landmarks:
        body = (f'<main><section aria-label="Timeline"><h1>Feed</h1>{arts}</section></main>'
                f'<aside>{side}</aside>')
    else:  # redesign: no main landmark or timeline region, items straight in the body
        body = f"<div><h1>Feed</h1>{arts}</div><aside>{side}</aside>"
    return page("Drift Test", body)


TRENDS = ("Trending", ["topic one", "topic two", "topic three"], "")
DRIFT_PAGES = {
    "/drift/base": drift_page([(f"d{i}", f"Post {i}") for i in range(1, 5)], [TRENDS]),
    # Content-only changes: other text, more items, a different sidebar.
    "/drift/text": drift_page([(f"d{i}", f"Something else {i}") for i in range(5, 9)], [TRENDS]),
    "/drift/count": drift_page([(f"d{i}", f"Post {i}") for i in range(1, 10)], [TRENDS]),
    "/drift/sidebar": drift_page(
        [(f"d{i}", f"Post {i}") for i in range(1, 5)],
        [("Trending", ["a", "b", "c", "d", "e", "f"], '<img alt="chart" src="data:,">'),
         ("Who to follow", ["someone", "someone else"], '<button type="button">Show more</button>')]),
    # Real structural change: the main landmark and timeline region are gone.
    "/drift/redesign": drift_page([(f"d{i}", f"Post {i}") for i in range(1, 5)], [TRENDS], landmarks=False),
    # Health drops: far fewer items; most items missing the required field.
    "/drift/sparse": drift_page([("d1", "Post 1")], [TRENDS]),
    "/drift/fieldless": drift_page([("d1", "Post 1")] + [(f"d{i}", None) for i in range(2, 5)], [TRENDS]),
}


def build_drift():
    return har([entry(ORIGIN + path, "text/html; charset=utf-8", html) for path, html in DRIFT_PAGES.items()])


def build_challenges():
    return har([entry(ORIGIN + path, "text/html; charset=utf-8", html) for path, html in CHALLENGE_PAGES.items()])


def write(name, data):
    (FIXTURES / name).write_text(json.dumps(data, indent=1) + "\n")


if __name__ == "__main__":
    FIXTURES.mkdir(exist_ok=True)
    write("feed.har", build_feed())
    write("challenges.har", build_challenges())
    write("drift.har", build_drift())
