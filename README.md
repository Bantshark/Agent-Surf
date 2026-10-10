# Agent Surf

A local agent browser with interface memory. A model learns a page once and writes a site map; plain code replays it on every later run with no model calls, and only new items come back. The model is called again only when a map breaks.

v2 adds a write layer built the same way: the model learns a composer once (an action map, rehearsed without posting), and plain code publishes **only items you approved in a queue**, each with a receipt read back from the platform.

Targets: X, Reddit, Instagram, Facebook, LinkedIn, YouTube.

Status: v1 and v2 built from [BUILD-BRIEF.md](BUILD-BRIEF.md). v2 is validated offline only; live validation happens locally (see below).

## Usage

Requires Python 3.11+ and the three pinned packages:

```
python3 -m pip install -r requirements.txt
python3 -m playwright install --with-deps chromium
```

There is no install step; run it as a module from the repo root.

```
python -m agent_surf chrome                         # print the Chrome launch command
python -m agent_surf learn <site> <page_type> [--query Q | --handle H]
python -m agent_surf run   <site> <page_type> [--query Q | --handle H] [--json]
python -m agent_surf maps  list | show <site> <page_type>
python -m agent_surf youtube <url-or-ytsearchN:query> [--subs] [--comments] [--json]
python -m agent_surf scrub <in.har> <out.har>
```

1. `chrome` prints a command that starts Chrome with a dedicated profile under
   `$AGENT_SURF_HOME/chrome-profile` and a debugging port. Start it, then log in
   to your sites in that window. While it runs, the port gives any local process
   full control of that profile.
2. `learn` opens the page in that Chrome, sends an accessibility snapshot and
   reduced JSON responses to Claude, and saves the returned map only if it
   validates and extracts at least 3 items.
3. `run` replays the map with no model calls and prints only items not seen
   before. If the map breaks and `ANTHROPIC_API_KEY` is set, it relearns once.

Sites and page types:

| Site | Page types |
|---|---|
| `x` | `home`, `search` (`--query`), `profile` (`--handle`) |
| `reddit` | `subreddit` (`--handle`), `post` (`--handle` = post id), `search` (`--query`) |
| `instagram` | `profile` (`--handle`), `feed` |
| `facebook` | `page` (`--handle`), `feed` |
| `linkedin` | `profile` (`--handle`), `feed`, `jobs` (`--query`) |

Inbox page types (v2, read with `inbox`): `x` `notifications`, `messages`;
`instagram` `messages`; `facebook` `notifications`; `linkedin`
`notifications`, `messages`; `reddit` `inbox`.

Results go to stdout (every command accepts `--json`); logs go to stderr.
Exit codes: `0` ok, `1` error, `2` usage, `3` CAPTCHA not cleared within 10
minutes, `4` map broken and no API key to relearn, `5` publish refused (not
attempted: not approved, content changed since approval, no action map, or a
cap), `6` publish needs attention (attempted but not confirmed published; see
`queue show <id>`).

Environment:

| Var | Default | Use |
|---|---|---|
| `AGENT_SURF_CDP_URL` | `http://127.0.0.1:9222` | Your Chrome |
| `AGENT_SURF_HOME` | `~/.agent-surf` | DB, learned maps, Chrome profile |
| `AGENT_SURF_MODEL` | `claude-sonnet-5-5` | Learner model |
| `ANTHROPIC_API_KEY` | | `learn` and self-heal only |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | | CAPTCHA and missed-schedule pings; otherwise stderr |
| `AGENT_SURF_ATTACH` | `cdp` | `devtools-active-port` (experimental, see below) |
| `AGENT_SURF_PROFILE_DIR` | | Chrome profile dir for `devtools-active-port` |
| `AGENT_SURF_ALLOW_REMOTE_CDP` | | `1` allows a non-loopback CDP endpoint (off by default) |

## v2: publishing

```
python -m agent_surf learn-action <site> post|reply|dm|comment [--target URL | --thread URL | --start URL] [--keep-debug]
python -m agent_surf queue add <site> <action> [--text T] [--media F ...] [--target URL] [--thread URL] [--at ISO8601] [--missed skip|run|ask]
python -m agent_surf queue list [--status S] | show <id> | approve <id> | approve --all-drafts | reject <id>
python -m agent_surf publish <id>
python -m agent_surf dispatch [--once]
python -m agent_surf inbox <site> <page_type> [--json]
python -m agent_surf receipts [--json]
```

Workflow:

1. **Learn the composer once:** `learn-action x post`. Claude reads the
   composer page and writes an action map; Agent Surf then *rehearses* it with
   placeholder text, checks the submit button without clicking it, discards
   the draft and confirms nothing was created. The map is saved only if that
   rehearsal is clean. Replies and comments need `--target <post URL>`, DMs
   `--thread <conversation URL>`. Media is optional except for Instagram
   posts: the rehearsal first proves a text-only post reaches an enabled Post
   button, then (if the composer can attach photos) a post with a synthetic
   image. If learning fails, the evidence (rejected map, error, notes, page
   snapshots) is written to `$AGENT_SURF_HOME/debug/<time>-<site>-<action>/`
   and the path is printed; `--keep-debug` writes it on success too.
2. **Queue content:** `queue add x post --text "..." --media photo.jpg --at 2026-11-01T09:00`.
   Items start as drafts. You (or your own tooling) write the text; Agent Surf
   never generates it.
3. **Approve:** `queue show <id>`, then `queue approve <id>`. Approval freezes
   the content: a hash of the text and of every media file's bytes. If anything
   changes afterwards, publishing refuses and the item needs re-approval.
4. **Publish:** `publish <id>` now, or leave `dispatch` running to publish
   approved items when they are due. Without `--at`, an approved item is due
   immediately.
5. **Receipts:** an item becomes `published` only when the platform's own
   create response returns a post id with no errors **and** the post's
   permalink shows your text. Otherwise it is `needs_attention` with the
   evidence; nothing is ever retried blindly. `receipts` lists them.

Queue states: `draft`, `approved`, `publishing`, `published`, `failed`,
`needs_attention`, `rejected`. Only `approved` items are published.

**Caps** come from each action map's `limits` (`per_hour`, `per_day` <= 200,
`min_spacing_s` >= 30); every submit counts. A capped item stays approved and
goes out on a later tick. Pacing is fixed: no random delays.

**Missed schedules:** if the machine slept or `dispatch` was not running when
an item was due, its `--missed` policy applies when dispatch resumes: `skip`
(mark failed), `run` (publish once now) or `ask` (default: needs attention and
a Telegram ping).

**If the outcome is unknown** (crash or timeout after submit), Agent Surf looks
for the post on your account through the reading side (the action map's
`lookup` page, which needs a reading map) and records the receipt if found.
It never submits the same item twice on its own.

### Data on this machine

Everything lives under `$AGENT_SURF_HOME` (default `~/.agent-surf`), **unencrypted**
(encryption at rest would need a dependency; see BUILD-BRIEF). Protect it like
your browser profile.

| Path | What | Kept until |
|---|---|---|
| `surf.db` | reading/action map versions, seen ids, stored reading items, the queue (your drafts and approved text), receipts, dispatch log, dispatcher state | items: `purge`; everything else: until you delete it |
| `maps/` | learned reading and action maps (selectors, no content) | relearn / delete |
| `media/<id>/` | approved media snapshots (0700/0600 on POSIX) | removed when the item is published or rejected |
| `debug/` | learn-action evidence (map, error, notes, page snapshots that can include other people's posts) | newest 20 kept automatically; `purge` |
| `chrome-profile/` | the dedicated Chrome profile (cookies, logins) if you use `agent-surf chrome` | Chrome |

```
python -m agent_surf purge [--items-older-than 90] [--debug-older-than 14] [--dry-run]
```

`purge` deletes stored reading items and debug folders older than the
thresholds. It never deletes maps, the queue, receipts or seen ids (without
seen ids old posts would come back as new). `learn`, `learn-action` and `inbox`
also keep only the newest 20 debug folders.

The CDP endpoint must be loopback (`127.0.0.1`, `::1`, `localhost`): the
debugging port gives full control of the profile. Set
`AGENT_SURF_ALLOW_REMOTE_CDP=1` only if you really attach to another machine.

### Attach modes

- Default: `AGENT_SURF_CDP_URL`, Chrome started with `agent-surf chrome`
  (a dedicated profile).
- **Experimental:** `AGENT_SURF_ATTACH=devtools-active-port` with
  `AGENT_SURF_PROFILE_DIR=<your Chrome profile dir>` attaches to the Chrome 144+
  remote-debugging toggle (chrome://inspect/#remote-debugging) on an everyday
  profile by reading its `DevToolsActivePort` file. Tested offline only.

Agent Surf always opens its own tabs and closes them; it never reads,
navigates or closes your other tabs.

### Validating v2 live (locally)

All v2 tests run offline against a synthetic site. Before relying on it, on
your own machine and with an account you can lose: run `learn-action` for one
site, read the saved `<site>.action-<action>.v1.json` in `$AGENT_SURF_HOME/maps/`, queue one
harmless post, approve it, `publish` it, and confirm the receipt's permalink.

### Maps and clicks

A map may carry an optional `"click"` list of up to 5 CSS selectors for
"Show more" / "Load more" controls. The runner clicks a matching element only
if it is visible, enabled, outside any form or dialog, not a submit button or
real link, and its short label matches an expand word (show, see, view, load,
read, expand, more, older) with no deny word (like, follow, reply, post, share,
sign, less, ...). A click that changes the URL turns clicking off for the rest
of the run.

### YouTube

Media is never downloaded, so the player JavaScript is skipped and a video
with no playable formats is not an error. Metadata, subtitles and comments
need no JS-challenge solver: no extra package and no code fetched at runtime.

### Tests

```
pytest -q
```

Tests are offline. Most use synthetic HAR fixtures for `https://feed.test`
served through Playwright `route_from_har`. `tests/test_e2e.py` drives the real
CLI against a separately started Chromium over CDP, a local HTTPS server for
`feed.test` (throwaway cert from `openssl`), and the real `anthropic` SDK
pointed at a local fake Messages API. `tests/conftest.py` lets a `pytest`
installed in its own virtualenv (`uv tool`, `pipx`) find the packages from
`requirements.txt` installed for `python3`.

`tests/test_browser.py::test_cdp_session_and_dom_extraction` and
`tests/test_e2e.py` start a browser with a debugging port. They use Playwright's
bundled Chromium unless `AGENT_SURF_TEST_CHROME` points to an existing browser
executable. Set it where the bundled Chromium will not launch (for example
`WinError 14001` on some Windows machines):

```
# PowerShell, Brave
$env:AGENT_SURF_TEST_CHROME = "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
# PowerShell, Edge
$env:AGENT_SURF_TEST_CHROME = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
pytest -q
```

`tests/make_fixtures.py` regenerates the fixtures. `tests/test_scrub_guard.py`
fails if any fixture contains cookies, auth headers or token-shaped headers.

## Ground rules

- Runs against your own Chrome, started with a dedicated profile. Agent Surf never handles passwords.
- Reading is read-only: it navigates, scrolls and reads.
- Publishing (v2) types only the text and attaches only the files of a queue item you approved, and clicks only an allowlisted submit button (Post, Tweet, Share, Publish, Reply, Send, Comment, per action). It never likes, follows, reposts or deletes.
- No anti-detection of any kind: no spoofing, stealth plugins, random delays, proxy rotation or CAPTCHA solving.
- One user, their own accounts, in their own browser.
- CAPTCHAs are never bypassed. Agent Surf pauses and asks you to solve them.
- Logged-in scraping and automated posting can breach a platform's terms and get accounts banned. Use accounts you can lose.

## License

MIT
