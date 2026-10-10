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
5. **Cold start falsely reported a broken map** (branch `claude/cold-start-fix`).
   Symptom: the first `run x search` after Brave was freshly started exited 4
   in 7 s with "map broken: first pass found no items with ids"; an identical
   run right after returned 80 items. With `ANTHROPIC_API_KEY` set this would
   have spent a Claude call relearning a healthy map from a half-loaded page.
   Cause: `runner.replay` did `goto` (domcontentloaded) + one `delay_s` wait
   (2 s) and a single first pass; a cold browser's app had not requested the
   feed yet, so the buffer and DOM were empty. The learner likewise
   snapshotted after a fixed `LEARN_DELAY_S`.
   Change: the runner polls (0.5 s) until any of the map's sources has
   content — a response matching `network.url_regex` since navigation, or at
   least one `dom.item` element — up to `FIRST_PASS_TIMEOUT_S = 15`, running
   the challenge guard on every poll (challenge time does not count against
   the ceiling). The first pass is tried up to `FIRST_PASS_ATTEMPTS = 2` times,
   `delay_s` apart, each over all responses since navigation; `MapBroken` only
   if every attempt fails. `learner.learn` calls `wait_for_page` before its
   fingerprint, pre-scroll count and response samples: on self-heal it is
   satisfied by the old map's sources having content; otherwise (and as a
   fallback) by at least 3 item-role nodes in an aria snapshot that is
   unchanged across one poll; same ceiling and guard. Scroll loop, delta,
   drift, click and domain-lock logic unchanged.
   Tests (`test_cold_start.py`; `feed.test` pages `/late`, `/late-dom`,
   `/never`, `/late-challenge` whose script delays the feed or items by 3 s):
   late feed and late DOM succeed; a feed that never arrives raises
   `MapBroken` after the shortened timeout and self-heal is entered exactly
   once; a checkpoint appearing during the wait triggers challenge handling
   (Telegram mocked) and the run succeeds; learning the late-feed page saves a
   valid map with `dry_run_items == 5`.
6. **Cold-start wait gaps** (branch `claude/cold-start-fix`, after Fix 5).
   Gaps: the learner's first-learn wait counted item-role nodes anywhere, so a
   sidebar or nav list could end it before a late feed; pages whose items use
   no item role waited the full 15 s; a feed that never arrived took ~15 s +
   `delay_s` to report broken, plus up to 15 s more in the relearn.
   Change: `learner.item_nodes` counts only item-role nodes outside
   navigation, complementary, banner and contentinfo landmarks. Both waits
   also end when the page is quiet (`runner.QuietTracker`): no response of
   any type (`ResponseBuffer.activity`, counted before the JSON filter) and no
   aria snapshot change for `FIRST_PASS_QUIET_S = 5`. Any response or DOM
   change restarts the clock; challenge time is not quiet. Measured with the
   real ceiling on the synthetic never-arriving page: `MapBroken` in 7.4 s
   (was ~17 s), run + failed relearn 16.6 s (was ~38 s). A cold app that is
   completely silent (no requests, no DOM change) for over 5 s before
   requesting its feed would still be cut short; the first-pass retry
   (`FIRST_PASS_ATTEMPTS`) is the remaining margin.
   Tests (`test_cold_start.py`; pages `/late-sidebar`, `/static-divs`,
   `/ticking`, `/polling`): sidebar list with a 3 s late feed -> learner counts
   5 pre-scroll items; static div page -> learner wait < 9 s; never-arriving
   feed -> wait < 9 s; a page that keeps changing its DOM or fetching JSON
   waits to the ceiling; `item_nodes` ignores navigation/sidebar/header/footer.
   Not verifiable from the cloud: Windows and live sites (CLAUDE.md forbids
   live requests here). Live check on the user's machine: freshly start the
   browser, then `run x search --query ...` must not exit 4.

7. **Discard check matched a look-alike composer** (v2 live, X; branch
   `claude/v2-live-fixes`). Symptom: `learn-action x post` run #2 failed with
   "step discard[1]: still visible: textbox 'Post text'"; run #1 had passed only
   by a race. Cause: the discard `wait_for ... hidden` and the rehearsal's final
   check resolved the text box page-wide; after a correct discard X navigates to
   /home, whose inline composer has the same role and name. Change: every target
   a run resolves is pinned to its element; optional map key `composer {target}`
   pins the container that holds the text box, and type, attach and submit
   resolve only inside it (StepFailed if it is missing or gone, never a
   page-wide fallback); hidden waits and `composer_closed()` pass when the pinned
   element is detached/hidden or the pinned container is gone, never because of
   a different element elsewhere. The learner prompt asks for `composer` and for
   discard to end by waiting for the container to be hidden. Tests
   (`test_composer_scope.py`; compose.test dialog over an inline composer with
   the same name, discard navigating to the inline page, 500 ms race variant):
   the old page-wide check would fail (asserted); rehearsal passes with and
   without `composer`; a missing or vanished container fails with nothing
   typed; publish types into and submits from the dialog only.
8. **Text-only posts refused by a map that required media** (v2 live, X).
   Symptom: publishing "hello world" was refused: "the queue item has no media,
   which this action map requires". Cause: rehearsal always attached a PNG when
   the map had an attach step and nothing said media was optional, so the model
   put `media` in `start.requires`; the text-only path was never rehearsed.
   Change: `sites.media_required(site, action)` (instagram post True, everything
   else False); `validate_action_map` rejects `media` in requires unless
   required, and when required demands it plus an attach step; the prompt states
   the rule; rehearsal runs a text-only pass that must reach an enabled submit,
   then a media pass if the map has an attach step (media required: media pass
   only); every pass discards, closes the composer and sends no create request.
   A stored map that breaks the rule fails to load with a relearn hint. Tests
   (`test_media_rule.py`): validator cases, prompt wording, both passes (no file
   in pass 1), media-only pass, a composer whose Post needs media fails pass 1,
   text-only publish with an optional attach step gets a receipt and read-back,
   the old X map must be relearned.
9. **Failed learning left no evidence** (v2 live, X). Change: after the model
   returned a map, any ActionLearnError writes
   `<AGENT_SURF_HOME>/debug/<UTC>-<site>-<action>/` (rejected-map.json,
   error.txt, notes.txt, aria-before.yaml, aria-after.yaml; no cookies,
   headers, response bodies or non-synthetic typed text); the CLI prints the
   path; `learn-action --keep-debug` writes it on success (learned-map.json).
   Never inside the repository (refused in code; `debug/` gitignored). Tests
   (`test_learn_debug.py`): evidence on rehearsal and validation failures,
   none without a map, success only with --keep-debug, refused inside the repo,
   CLI prints the path, every file passes the scrub guard's text checks.

10. **Action-map clicks could reach unwanted controls** (security check;
    branch `claude/v2-hardening`). Cause: click steps fell back page-wide after
    the composer, guarded only by the DESTRUCTIVE denylist. Change: with a
    `composer`, click steps resolve only inside it (page-wide only for discard
    and dismiss); `actionmap.CLICK_LABELS` (per action) and
    `DISCARD_CLICK_LABELS` are checked against the accessible name before every
    click (case-insensitive, whitespace collapsed; empty names refused; order:
    DESTRUCTIVE, "could publish", allowlist); `validate_action_map` rejects named
    click/attach targets outside the allowlist (unnamed ones are only checked at
    run time); refusals are never retried or self-healed; the learner prompt
    lists the labels. Tests: `test_click_scope.py`.
11. **No domain lock between action steps** (security check). Change: the tab URL
    is checked against the site after every op (steps, discard, dismiss) and
    right before submit (DomainRefused -> needs_attention + quiet discard); after
    the submit click and while awaiting confirmation, off-site is an unknown
    outcome (never resubmitted). Tests: `test_domain_lock_v2.py`.
12. **Any regular file accepted as media** (security check). Change: jpg/jpeg,
    png, gif, webp (<= 15 MB) and mp4, mov, webm (<= 512 MB); extension AND magic
    bytes must agree; at most 4; symlinks refused (lstat, no resolve()); checked
    at add, approve and publish; `queue show` lists path, type, size, sha256
    prefix. No path denylist. Tests: `test_media_validation.py`.
13. **Media swappable between check and upload** (security check, TOCTOU).
    Change: approve copies media to `<AGENT_SURF_HOME>/media/<id>/<n><ext>`
    (0700/0600 on POSIX), stores the snapshot paths (`queue.media_snapshot_json`,
    additive), hashes the snapshot bytes; publish uploads only snapshots and
    re-hashes them; re-approval re-snapshots; snapshots are deleted on publish or
    reject. Items approved earlier keep their original paths. Tests:
    `test_media_snapshot.py`.
14. **Unbounded local data; any CDP host accepted** (security check). Change:
    `purge [--items-older-than 90] [--debug-older-than 14] [--dry-run]` (items
    and debug folders only; never maps, queue, receipts, seen ids); learn,
    learn-action and inbox keep the newest 20 debug folders; CDP endpoints must
    be loopback in both attach modes unless `AGENT_SURF_ALLOW_REMOTE_CDP=1`.
    Known gap: encryption at rest (needs a dependency). Tests: `test_retention.py`.
15. **Slow tick mistaken for sleep** (code review). Cause: last_tick was the
    tick's start while publishes ran inline. Change: `last_tick_started` and
    `last_tick_finished`; detection uses finish -> next start; the run loop also
    compares wall clock with time.monotonic() across its sleep (POSIX suspend);
    items due during a long tick publish normally. Tests: `test_dispatcher.py`.
16. **Unknown outcome on X never checked the account** (code review). Change:
    `account set <site> --handle H` / `account show` (table `accounts`);
    learn-action fills `lookup {page_type: profile, handle}` for post/reply/
    comment when a handle is set (DMs: none) and warns without a working lookup;
    `actions set-lookup` writes a new validated map version; lookups must
    supply exactly the placeholders of their page type; publish/recover say
    explicitly when the lookup or its reading map is missing. Tests:
    `test_lookup_account.py`.
17. **Lookup could attach an older post with the same text** (code review).
    Change: a candidate is accepted only if (a) its id is not in any receipt and
    (b) when the reading map has a created_at/timestamp/time/date field, it
    parses and is >= submitted_at - 120 s; newest wins; the receipt records
    `match`. Tests: `test_lookup_match.py`.
18. **Read-back matched text anywhere on the page** (code review). Change: read
    back from the closest article/[role=article] (or map `readback.target`)
    containing a link with the post id; fallback to the page body records
    `read_back: "page"`; receipts carry true | "page" | false; published needs
    true or "page". Tests: `test_readback.py`.
19. **Caps counted from the publish start** (code review). Change: submitted_at
    is the real submit moment (also on SubmitUnknown); caps read only the last
    24 h through an index. Tests: `test_submit_time.py`.

20. **Symlink test false-failed on Windows** (local validation: WinError 1314
    in `os.symlink`). Cause: creating a symlink needs admin or Developer Mode on
    Windows. Change: the real-symlink test skips on WinError 1314,
    PermissionError or NotImplementedError; a new test monkeypatches outbox's
    `os.lstat` to report S_IFLNK and runs everywhere; another asserts
    `check_media` uses lstat, not stat. Tests: `test_media_validation.py`.
21. **Items marked seen before they were printed** (code review). Symptom: a
    broken pipe or closed stdout after a run lost the new items for good (their
    ids were already seen). Change: `items` gains `run_id` and `delivered_at`
    (additive; older rows count as delivered); every run, inbox and youtube
    invocation gets a run_id (UTC timestamp + random suffix, printed to stderr
    as `run_id=...`); delivered_at is set only after stdout was written and
    flushed (JSON and text); a failure exits 1 and says how to recover; the
    seen + item insert is one transaction. New command `items [--site S]
    [--page-type P] [--run ID | --since ISO8601 | --undelivered] [--json]
    [--mark-delivered]` prints what `run` prints; `run --json` stays a plain
    array. Tests: `test_items_delivery.py`.
22. **A logged-out page looked like a broken map** (local validation risk).
    Cause: a login wall has no items, so the runner raised MapBroken and the
    self-heal sent the login page to the model. Change: per-site markers
    (`sites.LOGGED_OUT`: URL paths and title prefixes, kept as data) and
    `challenge.detect_logged_out`, separate from challenges (LinkedIn's
    /checkpoint/lg is a login, not a CAPTCHA). The guard (`challenge.Guard`)
    checks after navigation, before the first pass, inside wait_for_content and
    in the executor; the runner, learner and action learner refuse a login wall
    before MapBroken and before any model call. On detection: notify "log in to
    <site> in the Agent Surf browser window", poll every 5 s for 10 min, reopen
    the page that was being opened, continue; on timeout `LoggedOutTimeout`,
    CLI exit 7. Never MapBroken, never a relearn or self-heal, never a model
    call. The executor stops on a login wall; the publisher waits for the login
    and starts over on a fresh tab (nothing was submitted: the guard runs
    before the submit click), or puts the item back to approved with
    last_error via `outbox.release` (publishing -> approved keeping the
    approval; not a general transition). The dispatcher retries it next tick.
    Tests: `test_logged_out.py` (synthetic login walls on feed.test and
    compose.test).
23. **HAR scrub kept response bodies** (code review). Change: by default every
    response `content.text` becomes "REDACTED (<n> bytes, <mime>)" and
    `content.encoding` is dropped; WebSocket message data too. `scrub
    --keep-bodies` keeps JSON/text bodies and redacts keys matching the
    credential pattern or `(?i)email|phone|password|birth|address|dob|ssn`
    (request bodies use the same keys); HTML/text keeps its text with values
    after such keys redacted; binary bodies are still dropped. Files are read
    and written as UTF-8. The scrub guard also fails on email- or phone-shaped
    strings in fixtures. A scrubbed HAR still shows page structure and URLs:
    commit one only if it is synthetic. Tests: `test_har_scrub.py`,
    `test_scrub_guard.py`.
24. **Security check behind `assert`** (code review). Cause: `python -O`
    strips asserts; `youtube.build_opts` guarded FORBIDDEN_OPTS with one.
    Change: `youtube.check_opts` raises YouTubeError, called in build_opts and
    again before yt-dlp is constructed; the package has no assert statements
    (a test walks the AST). Tests: `test_no_asserts.py`.
25. **Accessible names ignored aria-labelledby** (known gap). Change: one JS
    resolver (`browser.ACCESSIBLE_NAME_FN`): aria-labelledby (referenced
    elements' text, joined), aria-label, labels/alt/title, innerText, value
    (then descendant img alt, textContent); a text field is named by its
    labels, title or placeholder, never by what was typed. The click allowlists and the submit
    allowlist use the resolved name; DESTRUCTIVE and the could-publish check
    see every name source; the reading side's safe clicks use it too. Tests:
    `test_accessible_name.py`.
26. **Unattended dispatch could fail silently** (local validation). Change:
    `dispatch` refuses to start without TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID
    unless `--stderr-only`; it notifies every publish outcome other than
    published (needs_attention, failed, a refusal that moved the item to
    needs_attention, logged out, challenge, error) but not a cap/spacing
    refusal (retried); login and challenge waits go through the same notifier.
    Messages carry queue id, site, action, status and reason, and at most the
    first 60 characters of the post text. Tests: `test_dispatch_notify.py`.
27. **Anthropic key only from the environment** (Windows usability). Change:
    on Windows, when ANTHROPIC_API_KEY is unset, a Generic credential
    `agent-surf/anthropic` is read from the Credential Manager (stdlib ctypes:
    advapi32 CredReadW/CredFree; blob UTF-16-LE or UTF-8, trailing NUL
    stripped); `key set` (getpass, CredWriteW, Generic,
    CRED_PERSIST_LOCAL_MACHINE) and `key clear`. Environment wins. Elsewhere:
    environment only, with a clear message. The value is never printed,
    logged or put in an exception; the client gets it as `api_key`. Tests use
    a fake advapi32 and set AGENT_SURF_NO_CREDMAN=1 everywhere else. Tests:
    `test_credentials.py`.
28. **No single health check** (local validation). Change: `agent-surf doctor
    [--json]`: pinned package versions; CDP endpoint loopback and reachable
    (`/json/version`, host and port only); for each site with a map, one tab
    on its home page (`sites.HOME_URL`) for the Fix 22 login check, closed
    again; reading and action maps valid; lookups working; account handles;
    Telegram and Anthropic key presence (and the key's source); home size,
    debug/media/items counts; queue counts by status. No model calls, no
    publishing. Exit 0 when nothing required failed, else 1. Tests:
    `test_doctor.py`.

## Agent Surf v2: the write layer
Built on branch `claude/agent-surf-v2` (base `claude/cold-start-fix`, so the
cold-start wait of Fix 5/6 is already in). v1 learns a page once and replays it
with zero model calls; v2 does the same for composers. All v1 rules still apply
except the read-only rule, which is replaced by the publishing boundary below.

### Publishing boundary (enforced in code)
- Text typed and files attached come only from the approved queue item's
  payload. Action-map values are placeholders (`{text}`, `{media}`,
  `{target_url}`, `{thread_url}`), never content; the model never supplies text.
- Publishing reads only from the queue (`publisher.publish`). The reading side
  (runner, learner, inbox, sitemap, browser...) imports nothing from the writing
  side; `tests/test_boundary.py` checks this structurally.
- Final submit only through `Submitter.submit`, on a target whose label is in
  both the map's allowlist and the per-action list in code (post: Post/Tweet/
  Share/Publish; reply: Reply/Post; dm: Send; comment: Comment/Post/Reply).
- Ops: navigate (site domains only), click (stored target), type (queue text
  only), attach (queue files only), press (Enter/Escape/Tab), wait_for, submit,
  discard. Additionally: once text is in the composer, Enter and clicks on
  submit-labelled controls are refused (they could publish outside submit), and
  destructive-looking controls (delete, block, report, follow, like, repost...)
  are never clicked.
- Challenges: the guard runs before navigation, after every step and before
  submit; a challenge is never clicked (detect, pause, notify, wait).
- No anti-detection: no spoofing, stealth, jitter, proxy rotation or CAPTCHA
  solving. Pacing is fixed caps. One user, their own accounts and browser.

### Action maps (`actionmap.py`)
One JSON file per (site, action), `<site>.action-<action>.v<N>.json` beside the
reading maps, versions in the `action_maps` table. Keys: site, action, version,
start {url_template, requires}, steps [{op, target, value, state, preview}],
submit {target, label_allowlist}, discard [steps], dismiss [steps] (optional,
overlay close), confirm {network {url_regex, method, id_path, error_path},
dom {target, state}}, permalink_template (`{id}`), limits {per_hour, per_day
<= 200, min_spacing_s >= 30}, lookup {page_type, handle?, query?} (optional,
for unknown-outcome recovery), learned_at, learned_by. A target is
{role, name, testid, css}; resolution tries role+name, then testid, then css,
and the first visible match wins (hidden file inputs allowed for attach).
`validate_action_map()` rejects unknown keys/ops, non-placeholder values,
off-domain URLs, allowlists wider than the code's, and limits above ceilings.
Decisions beyond the brief: `dismiss`, `preview` and `lookup` keys; `value`
must be exactly one placeholder; attach steps are skipped when the item has no
media.
After live use (Fixes 7-8): optional `composer {target}` (the container,
typically the [role=dialog], holding the text box and submit): type, attach and
submit resolve only inside it, and discard should end with a `wait_for` that
the composer is hidden. Every resolved target is pinned to its element for the
run; hidden checks never count a look-alike elsewhere on the page. Media:
`sites.media_required(site, action)` (only instagram post is True); `media` may
be in `start.requires` only when required, and then an attach step is
mandatory. Rehearsal: text-only pass (must reach an enabled submit), then a
media pass if there is an attach step; media-required: media pass only.

After the security check (Fixes 10-18): clicks are label-allowlisted in code
(`CLICK_LABELS` per action for steps; `DISCARD_CLICK_LABELS` for discard and
dismiss) and, with a `composer`, step clicks resolve only inside it; a named
click/attach target outside its allowlist fails validation. `lookup` must give
exactly the placeholders its page type needs (e.g. `{page_type: profile,
handle}`) and is filled automatically from `account set`. Optional
`readback {target}` names the post element on the permalink page.

### Executor (`executor.py`)
Text entry: fill() -> clear + keyboard.type() -> insertText(); accepted only
when the composer shows exactly the payload text and submit is enabled. Media:
set_input_files on a (hidden) file input, else expect_file_chooser around the
attach click, then the map's preview. Intercepted clicks: dismiss (map steps or
Escape), re-resolve, retry once. No networkidle waits. Step targets wait up to
`STEP_TIMEOUT_S = 15` (cold-start composers). `StepRunner` (rehearsal) has no
submit and aborts the create endpoint on its tab.

### Learning (`action_learner.py`, `learn-action`)
Opens the composer (`sites.ACTION_START` for posts; `--target`/`--thread` for
replies, comments, DMs), waits for a text box, sends the aria snapshot and
reduced JSON responses as untrusted data, validates the returned map, then
rehearses it with code-supplied placeholder text (and a stdlib 1x1 PNG if the
map attaches media): every step, a check of the submit target without
clicking, discard, composer closed, no create request. Saved only if clean.

### Queue (`outbox.py`, table `queue`)
States: draft -> approved | rejected; approved -> publishing | rejected |
failed (missed) | needs_attention; publishing -> published | failed |
needs_attention; needs_attention / failed -> approved (a human re-approves) |
rejected; published and rejected are terminal. Illegal transitions raise.
Approval stores `content_hash` = sha256 of the canonical payload plus every
media file's bytes; media must be existing regular files at approval and at
publish. `scheduled_at` is stored in UTC (input without offset = local time);
`missed_policy` skip|run|ask (default ask).

Media (Fixes 12-13): jpg/jpeg/png/gif/webp <= 15 MB, mp4/mov/webm <= 512 MB,
extension and magic bytes must agree, at most 4, no symlinks; approval
snapshots them into `<AGENT_SURF_HOME>/media/<id>/` and only the snapshots are
hashed and uploaded.

### Publish and receipts (`publisher.py`)
Only approved items whose hash matches; caps from `dispatch_log` (every submit
counts). Receipt = the create response's id at id_path with nothing at
error_path (GraphQL 200-with-errors is not published); DOM confirm only when
the map has no network block (id from the permalink link). Then the permalink
must show the approved text (whitespace/emoji normalised) before `published`.
Otherwise `needs_attention` with evidence (incl. an aria delta of the page
after submit). Unknown outcome (submit click error, no confirmation, crash ->
`recover_publishing`): the account is checked through the reading side (map
`lookup` page + its reading map); found -> receipt, else needs_attention;
never resubmitted. Self-heal: a step failing before submit discards the draft,
relearns once in rehearsal mode and retries once; a second failure ->
needs_attention.

Receipts (Fixes 16-19): `read_back` is true (the post's own element), "page"
(no post element; page text) or false (needs_attention); account-lookup
receipts carry `via: "account lookup"` and the `match` rule; submitted_at is
the real submit moment and caps read the last 24 h.

### Scheduler (`dispatcher.py`, `dispatch [--once]`)
Ticks every 30 s; publishes due approved items (scheduled_at <= now or none)
one by one through the publisher. A gap since the stored last tick
(`dispatcher_state`) of more than 3 ticks, or no previous tick, is a wake:
items that came due in the gap get their missed policy (skip -> failed
"missed"; run -> publish once; ask -> needs_attention + notifier ping).
Restart-safe; dispatch and completion recorded separately in `dispatch_log`.

### Inbox (`inbox.py`, `inbox <site> <page_type>`)
The response buffer also captures WebSocket frames (str and bytes; kept when
they decode as JSON). Reading maps accept `"source": "websocket"` (url_regex on
the socket URL, same paths). Inbox page types: x notifications/messages,
instagram messages, facebook notifications, linkedin notifications/messages,
reddit inbox. `sitemap.aria_delta()` returns only changed nodes.

### Attach modes (`browser.py`)
`AGENT_SURF_CDP_URL` (default). Experimental: `AGENT_SURF_ATTACH=
devtools-active-port` + `AGENT_SURF_PROFILE_DIR` reads the profile's
`DevToolsActivePort` and attaches to `ws://127.0.0.1:<port><path>` (Chrome
144+ chrome://inspect toggle on an everyday profile). Every run opens its own
tabs and closes them; the user's tabs are never touched.

### Experimental / validated only offline
DevToolsActivePort attach against a real Chrome 144+ profile; every real
site's composer (no action map has been learned against a live site); the
default composer URLs in `sites.ACTION_START`; live WebSocket inboxes; real
GraphQL create responses and permalinks; account lookup through real
profile pages. All of this needs local live validation.

### Known gaps after hardening
- Encryption at rest: `$AGENT_SURF_HOME` (queue text, receipts, debug folders,
  media snapshots) is stored unencrypted; encrypting it needs a dependency.
- Accessible names: aria-labelledby is resolved since Fix 25; aria-owns,
  CSS generated content and name-from-content recursion are not.

## Review findings (recorded)
Findings from the code review of the hardening round (Fixes 10-19) and this
round. Fixed ones name their fix; the rest are recorded here, not fixed.

Fixed:
- runner.py: items marked seen before they were printed: Fix 21 (also
  inbox and youtube).
- cli.py: a broken pipe after items were marked seen (run, inbox, youtube):
  Fix 21.
- youtube.py: FORBIDDEN_OPTS guarded by `assert`: Fix 24.
- har_scrub.py: response bodies and `_webSocketMessages` not scrubbed: Fix 23.
- har_scrub.py: HAR read/written without an explicit encoding: Fix 23.
- executor.py: names from aria-labelledby not resolved: Fix 25.

Not fixed (file:line on branch `claude/v2-polish`; recommendation):
- runner.py:213: no `page.check_domain()` after the first-pass `expand()`
  (the scroll loop has one). A click that navigates off-site raises inside
  `expand()` already; add the check anyway for symmetry.
- sitemap.py:169, sitemap.py:313, runner.py:120, publisher.py:89,
  executor.py:152: model-written `url_regex` is compiled and run on every
  response URL; a catastrophic pattern could stall a run (ReDoS). Reject
  nested quantifiers in validate_map/validate_action_map, cap the pattern
  length, and only search URLs up to a fixed length.
- sitemap.py:404, sitemap.py:411, actionmap.py:354, actionmap.py:364,
  browser.py:358: maps (and DevToolsActivePort) are read/written without
  `encoding="utf-8"`; on Windows the locale code page is used. Pass the
  encoding explicitly (existing files are ASCII-only JSON, so this is safe).
- youtube.py:76 and youtube.py:171: any youtube.com URL is a "video"; a
  playlist or `&list=` URL extracts every entry. Set `noplaylist: True` for
  video targets and a `playlistend` cap.
- har_scrub.py:75: URL fragments and path segments are not redacted (tokens
  in `#access_token=` or `/reset/<token>`); `urlencode` re-encodes the whole
  query when one parameter is redacted. Redact fragment parameters with the
  same pattern; rebuild the query by replacing only the redacted values.
- har_scrub.py:71/137: values are redacted by name only; a `Bearer ...` or
  JWT-shaped value under an innocuous name survives. Add value patterns
  (Bearer, three base64url segments) to the scrub, not only the guard.
- config.py:45 (ensure_dirs) and store.py:119: AGENT_SURF_HOME is created
  with the default umask, not 0700. Create it with mode 0o700 (POSIX).
- cli.py:441: an invalid current action map raises ActionMapError (a
  ValueError) and exits 1, not 5 (refused, not attempted). Catch it in
  cmd_publish and exit 5.
- cli.py:426: `queue approve --all-drafts` approves drafts the user has not
  looked at. Require `--yes` or list them and ask.
- cli.py:42: `--json` given before a nested subcommand (`queue --json list`)
  is overridden by the nested parser's default. Use
  `argparse.SUPPRESS` defaults on the nested parsers.
- har_scrub.py (keep-bodies): the credential pattern `auth` also matches
  keys like `author`, which are redacted; harmless over-redaction.
