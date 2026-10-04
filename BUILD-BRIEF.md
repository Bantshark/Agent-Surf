# Agent Surf — Build Brief

This is the authority for the BUILD phase. It was produced locally through the
RESEARCH and UNDERSTAND phases of a gated workflow. Build exactly this. Anything
not covered here is a **named gap**: stop and report it, do not improvise.

## What Agent Surf is

A local agent browser with interface memory. A large model learns a page once
and writes a **site map**. Deterministic code replays the map on every later
run with **zero model calls**. The model is called again only when the map
breaks (self-heal). Each run returns **only items not seen before**.

Targets: X, Reddit, Instagram, Facebook, LinkedIn (browser + site maps) and
YouTube (yt-dlp adapter, no map).

## Hard rules

1. **Dependencies: only the approved list below, at the exact pinned version.**
   A package that surfaces mid-build is a gap — report it, do not install it.
   Use the cloud image's preinstalled `pytest`; do not add test dependencies.
   No build backend (no setuptools/hatchling): run as `python -m agent_surf`.
2. **Never touch live social sites from the cloud.** All tests use synthetic
   fixtures served through Playwright `route_from_har`. Datacenter IPs get
   blocked anyway, and there is no logged-in profile in the cloud.
3. **The repo is public.** Never commit a HAR, snapshot or fixture containing
   real cookies, auth headers, tokens or personal data. Fixtures are synthetic.
   The scrub test (below) must pass on every fixture.
4. **Read-only by construction.** The runner may only: navigate to URLs on the
   map's own site domain, scroll, wait, and click selectors stored in a map.
   No typing, no form submission, no posting. Model output never becomes an
   action outside this whitelist.
5. **CAPTCHAs are never bypassed.** Detect, pause, notify the human, wait.
6. **Anthropic SDK 1.x**: `temperature`, `top_p`, `top_k` no longer exist on
   `messages.create()`. Do not pass them.

## Approved dependencies

| Package | Version | Notes |
|---|---|---|
| `playwright` (PyPI) | `1.62.0` | Chromium installed via `playwright install --with-deps chromium` |
| `anthropic` (PyPI) | `1.3.0` | Learner only |
| `yt-dlp` (PyPI) | `2026.8.19` | **No extras.** Uses Node (preinstalled) as JS runtime |
| stdlib | — | `sqlite3`, `json`, `urllib`, `hashlib`, `argparse`, `re` |

yt-dlp: never use `--exec`, aria2c / external downloaders, `--write-link`
family, or `--netrc-cmd` (all had command-injection CVEs; fixed in this
version, but the build has no reason to touch them).

## Layout

```
agent_surf/
  __main__.py      # python -m agent_surf → cli.main()
  cli.py           # argparse subcommands
  config.py        # env vars + defaults
  store.py         # SQLite: maps, seen, items
  sitemap.py       # map schema, validation, path extraction
  sites.py         # site registry: domains, page types, URL templates
  browser.py       # CDP connect + network response capture
  runner.py        # deterministic replay, delta stop
  learner.py       # Claude → map, validate, self-heal
  challenge.py     # CAPTCHA/checkpoint detection + Telegram ping + wait
  youtube.py       # yt-dlp adapter
  har_scrub.py     # strip cookies/auth from HAR files
tests/
  fixtures/        # synthetic HARs + aria snapshots only
  test_*.py
requirements.txt   # the three pins, nothing else
```

## Config (env vars)

| Var | Default | Use |
|---|---|---|
| `AGENT_SURF_CDP_URL` | `http://127.0.0.1:9222` | User's Chrome |
| `AGENT_SURF_HOME` | `~/.agent-surf` | DB, learned maps, chrome profile |
| `AGENT_SURF_MODEL` | `claude-sonnet-5-5` | Learner model |
| `ANTHROPIC_API_KEY` | — | Learner only; never logged |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | — | CAPTCHA ping; if unset, print to stderr and wait |

Never print, log or store a secret value.

## Components

### browser.py
- Connect with `chromium.connect_over_cdp(AGENT_SURF_CDP_URL)`; use the
  existing default context. Agent Surf never launches Chrome with the user's
  real profile and never handles credentials.
- `agent-surf chrome` prints the launch command for a **dedicated** profile:
  `chrome --remote-debugging-port=9222 --user-data-dir=<AGENT_SURF_HOME>/chrome-profile`
  (Chrome 136+ ignores the debug port on the default profile.) Note in the
  output that the port gives full control of that profile to local processes.
- Response capture: `page.on("response")`, keep responses whose content-type
  is JSON, store `(url, method, status, parsed_json)` in a bounded buffer.
  Ignore bodies that fail to parse; never raise from the listener.

### sitemap.py — the map
One JSON file per `(site, page_type)` in `<AGENT_SURF_HOME>/maps/`, history kept
as `<site>.<page_type>.v<N>.json`, current version recorded in SQLite.

```json
{
  "site": "x",
  "page_type": "search",
  "version": 1,
  "source": "network",
  "network": {
    "url_regex": "SearchTimeline",
    "items_path": "data.search.timeline.instructions[*].entries[*]",
    "id_path": "entryId",
    "fields": {"text": "content.legacy.full_text", "author": "content.user.screen_name"}
  },
  "dom": {
    "item": "article[data-testid=tweet]",
    "id_attr": "aria-labelledby",
    "fields": {"text": "[data-testid=tweetText]"}
  },
  "required_fields": ["text"],
  "fingerprint": "sha256-of-normalised-aria-skeleton",
  "scroll": {"max_scrolls": 30, "delay_s": 2.0, "stop_after_seen": 5},
  "limits": {"max_items": 200},
  "learned_at": "ISO-8601",
  "learned_by": "model id"
}
```
- `source` is `network` (preferred) or `dom`. Both blocks may exist; the runner
  tries `source` first and the other as fallback.
- Path language (implement it, no jsonpath dependency): dot keys, `[N]` index,
  `[*]` fan-out. Missing key → no value, never an exception.
- `validate_map(dict) -> list[str]` returns problems; empty list = valid.
  Reject unknown top-level keys, regex that fails to compile, CSS that is empty,
  limits above hard ceilings (`max_scrolls` ≤ 100, `max_items` ≤ 1000,
  `delay_s` ≥ 1.0).
- Fingerprint: hash of the aria snapshot with all names/text stripped, roles and
  nesting only. Used to *report* layout drift, not to block a run.

### sites.py
Registry: site → allowed domains, page types, URL templates with `{query}` /
`{handle}` placeholders. Initial set:
- x: `home`, `search`, `profile` — x.com
- reddit: `subreddit`, `post`, `search` — reddit.com
- instagram: `profile`, `feed` — instagram.com
- facebook: `page`, `feed` — facebook.com
- linkedin: `profile`, `feed`, `jobs` — linkedin.com
Navigation outside a site's domains is refused.

### store.py (SQLite at `<AGENT_SURF_HOME>/surf.db`)
Tables: `maps(site, page_type, version, path, fingerprint, created_at)`,
`seen(site, page_type, item_id, first_seen)` with a unique key,
`items(site, page_type, item_id, data_json, captured_at)`.

### runner.py — zero model calls
1. Load current map; refuse if none (tell the user to `learn`).
2. Navigate to the templated URL; run challenge check.
3. Loop: extract items (network buffer first, DOM fallback) → for each with an
   id: if unseen, record + emit; count consecutive already-seen items.
   Stop on `stop_after_seen`, `max_scrolls`, or `max_items`. Scroll, wait
   `delay_s`, challenge check, repeat.
4. If the first pass yields zero items or every item misses a required field,
   signal `MapBroken` (runner does not call the model itself).
Output: list of new items as dicts with `site`, `page_type`, `item_id`, fields.

### learner.py
- Inputs to the model: the aria snapshot of the page (truncated to a budget)
  plus up to 30 captured JSON responses, each reduced to url, status and a key
  skeleton with sample values truncated to 80 chars. Page content is wrapped as
  untrusted data, and the prompt says so.
- Output: a single JSON map. Parse → `validate_map` → dry-run extraction on the
  current page/buffer → require ≥ 3 items with ids and required fields. Only
  then save as a new version.
- Self-heal: on `MapBroken`, relearn once per run with the old map as a hint.
  Two failures → stop and report; never loop.
- The model client is injected (`learn(..., client=...)`) so tests use a fake.

### challenge.py
Detect: URL path contains `/challenge`, `/checkpoint`, `/captcha`; title
"Just a moment"; iframes from `challenges.cloudflare.com`, `recaptcha`,
`hcaptcha`. On detection: send one Telegram message (Bot API `sendMessage` via
`urllib`, token never logged), then poll every 5 s until the challenge is gone
or 10 minutes pass (then exit non-zero). Never interact with the challenge.

### youtube.py
yt-dlp Python API (`yt_dlp.YoutubeDL`): metadata, subtitles (`writesubtitles`,
`writeautomaticsub`, `skip_download`), comments (best-effort), `ytsearchN:`.
Return JSON-serialisable dicts. Delta logic via the same `seen` table.

### har_scrub.py
Remove `Cookie`, `Set-Cookie`, `Authorization`, `x-csrf-token`,
`x-guest-token` and any header matching `(?i)token|auth|session|csrf` from
requests and responses; drop `cookies` arrays; redact query params with the
same pattern. `agent-surf scrub in.har out.har`.

### cli.py
```
agent-surf chrome
agent-surf learn <site> <page_type> [--query Q | --handle H]
agent-surf run   <site> <page_type> [--query Q | --handle H] [--json]
agent-surf maps  list | show <site> <page_type>
agent-surf youtube <url-or-ytsearch> [--subs] [--comments] [--json]
agent-surf scrub <in.har> <out.har>
```
`--json` prints new items as a JSON array on stdout; logs go to stderr.

## Tests (offline, cloud-safe)
- Path language: nesting, `[*]`, missing keys.
- `validate_map`: good map passes; each bad case reports a problem.
- Runner against a synthetic HAR site (`https://feed.test/...` with an HTML
  page + JSON API responses): extracts items, stops after N seen, second run
  returns nothing new, DOM fallback works when the network block is absent.
- Learner with a fake client: valid map saved; invalid JSON rejected; map that
  extracts < 3 items rejected; self-heal runs at most once.
- Challenge detection on synthetic pages; Telegram call mocked.
- Domain lock: navigation off-site is refused.
- **Scrub guard:** every file under `tests/fixtures/` contains no `Cookie`,
  `Authorization` or token-shaped header. This test must never be skipped.

## Build order
1. `requirements.txt`, package skeleton, `config.py`
2. `store.py`
3. `sitemap.py` (+ path language) and tests
4. `sites.py` + domain lock
5. `browser.py`
6. `runner.py` + synthetic HAR fixtures and tests
7. `learner.py` + fake-client tests
8. `challenge.py` + tests
9. `youtube.py`
10. `har_scrub.py` + scrub guard test
11. `cli.py`, README usage section

Complete each unit with its tests before moving on.

## Out of scope for v1
MCP server, Supabase, scheduling, multi-account rotation, proxies, any
anti-detection or CAPTCHA-solving technique.

## Decisions after the v1 build
Gaps reported during the build, decided by the owner and implemented:

1. **Tests and the isolated pytest.** The cloud image's `pytest` runs in its own
   uv venv. `tests/conftest.py` appends the system `python3` site-packages to
   `sys.path` (and `PYTHONPATH` for subprocesses) when `playwright` is not
   importable. Nothing is installed. `pytest.ini` sets `testpaths` and
   `pythonpath = .`.
2. **Clicks.** Maps gain an optional top-level `"click"`: 1..5 CSS selectors for
   "show more" controls. Code, not the model, decides what is clicked
   (`browser.safe_to_click`): visible, enabled, not editable, not in a form or
   dialog, not submit/reset/file, not a link with a real href, label <= 40
   chars matching an expand allowlist and no denylist word. Clicks happen after
   the first load and after each scroll; a click that changes the URL disables
   clicks for the run; the domain lock applies after every click.
3. **YouTube without a JS solver.** `player_skip: ["js"]` and
   `ignore_no_formats_error: True`, since media is never downloaded. No
   `yt-dlp-ejs`, never `remote_components`. Node stays the configured runtime.
4. **Scrubbing request bodies.** `postData` params, JSON keys and form fields
   matching `(?i)token|auth|session|csrf` are redacted; an unparseable body that
   mentions such a name is replaced with `REDACTED`. The guard checks body params.
5. **Kept as built:** URL templates in `sites.py` (reddit `post` =
   `/comments/{handle}/`, linkedin `profile` = `/in/{handle}/recent-activity/all/`);
   `[*]` also fans out over dict values; unknown nested keys rejected;
   site/page_type names `[a-z0-9_]+`; fingerprint collapses repeated siblings;
   learner sends the 30 largest JSON responses and owns identity/provenance
   fields; YouTube targets are https YouTube URLs or `ytsearchN:` (N 1..1000);
   exit codes 3 (challenge timeout) and 4 (map broken, no API key); redirect
   URLs are scrubbed like query strings.
6. **End-to-end test.** `tests/test_e2e.py` runs learn → run → delta →
   self-heal through the CLI with a CDP-attached Chromium, a local HTTPS
   `feed.test` and the real `anthropic` SDK against a local fake API.


## Fixes after local validation
Local validation on Windows 11 (Python 3.12, Brave over CDP): 181/183 tests
passed; live X search worked end to end (learn 1 model call; run 1: 80 items,
0 model calls; run 2: 6 new, 0 overlap, stopped on stop_after_seen). Four
issues were fixed on branch `claude/validate-fixes`:

1. **CLI crashed printing non-ASCII on Windows.** Cause: Windows redirects
   stdout as cp1252 and `--json` prints with `ensure_ascii=False`, so an emoji
   raised a `charmap` error after the items were already marked seen.
   Fix: `cli.main()` reconfigures stdout/stderr to UTF-8 (`errors="replace"`).
   Test: `test_cli.py::test_cli_prints_non_ascii_on_cp1252_stdout` runs the CLI
   as a subprocess with `PYTHONIOENCODING=cp1252` and a synthetic emoji item.
2. **"Layout drift" warned on every run.** Cause: the fingerprint hashed every
   item's inner structure and every sidebar module, so which posts (media,
   text, quote) and which sidebar modules happened to be loaded changed it;
   and learn snapshotted after a scroll and ~4 s while run snapshotted before
   any scroll. Fix: fingerprint v2 (`v2-sha256-`) keeps landmark/container
   roles only, with item containers (article, listitem, row, treeitem) as
   leaves; learn fingerprints and counts its dry run before scrolling; the
   `maps` table gains `dry_run_items`, `last_fingerprint`, `drift_count`
   (added by `ALTER TABLE` on open); drift warns only after 3 consecutive
   mismatching runs, an older-scheme fingerprint is re-baselined, and a health
   drop (first pass < 50% of the dry-run count, or < 80% of items with all
   required fields) warns at once. Always advisory. For DOM-source maps the
   dry-run count is taken after the learner's scroll and can over-count.
   Tests: `test_drift.py` (synthetic `drift.har`: text/count/sidebar changes
   never warn; a removed landmark warns on the 3rd run and resets; an
   interrupted streak does not warn; sparse and field-less pages warn at once;
   the run right after learn is clean; an old DB migrates), updated
   fingerprint tests in `test_sitemap.py` and `test_runner.py`, and an
   assertion in `test_e2e.py` that the first run after learn logs no drift.
3. **Scrub guard rejected a correctly scrubbed HAR.** Cause: the text-level
   `JSON_HEADER` regex matched `"name": "auth_token"` even when its value was
   `"REDACTED"`. Fix: for `.har` files only, that one check ignores
   name/value pairs whose value is exactly `REDACTED`; the cookie/authorization,
   header-line and bearer checks and `problems_in_har` are unchanged. Tests in
   `test_scrub_guard.py`: planted Cookie, x-csrf-token, `?auth_token=`,
   response cookies and a Bearer body are flagged; the same HAR after
   `scrub_har` passes; a token-shaped name with a real value, a token-shaped
   header valued `REDACTED`, and a `REDACTED` pair outside a HAR still fail.
4. **CDP tests could not launch Playwright's Chromium on that machine**
   (`WinError 14001`). Fix: tests use `AGENT_SURF_TEST_CHROME` when it names an
   existing file (e.g. Brave or Edge), otherwise Playwright's executable as
   before; no production change. Documented in the README Tests section.
   Test: `test_browser.py::test_test_chrome_override`.
