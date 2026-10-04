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
               entry(ORIGIN + "/api/config", "application/json",
                     json.dumps({"features": {"dark_mode": True}}))]
    for n in range(1, PAGES + 1):
        entries.append(entry(f"{ORIGIN}/api/feed?page={n}", "application/json", json.dumps(feed_page(n))))
    return har(entries)


def write(name, data):
    (FIXTURES / name).write_text(json.dumps(data, indent=1) + "\n")


if __name__ == "__main__":
    FIXTURES.mkdir(exist_ok=True)
    write("feed.har", build_feed())
