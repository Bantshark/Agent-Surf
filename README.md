# Agent Surf

A local agent browser with interface memory. A model learns a page once and writes a site map; plain code replays it on every later run with no model calls, and only new items come back. The model is called again only when a map breaks.

Targets: X, Reddit, Instagram, Facebook, LinkedIn, YouTube.

Status: v1 built from [BUILD-BRIEF.md](BUILD-BRIEF.md).

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

Results go to stdout (`--json` gives a JSON array); logs go to stderr.
Exit codes: `0` ok, `1` error, `2` usage, `3` CAPTCHA not cleared within 10
minutes, `4` map broken and no API key to relearn.

Environment:

| Var | Default | Use |
|---|---|---|
| `AGENT_SURF_CDP_URL` | `http://127.0.0.1:9222` | Your Chrome |
| `AGENT_SURF_HOME` | `~/.agent-surf` | DB, learned maps, Chrome profile |
| `AGENT_SURF_MODEL` | `claude-sonnet-5-5` | Learner model |
| `ANTHROPIC_API_KEY` | | `learn` and self-heal only |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | | CAPTCHA ping; otherwise stderr |

### Tests

Tests are offline. They use synthetic HAR fixtures for `https://feed.test`
served through Playwright `route_from_har`. If your `pytest` lives in its own
virtualenv (for example installed with `uv tool` or `pipx`), point it at the
packages from `requirements.txt`:

```
PYTHONPATH="$(python3 -c 'import playwright, os; print(os.path.dirname(os.path.dirname(playwright.__file__)))')" pytest -q
```

`tests/make_fixtures.py` regenerates the fixtures. `tests/test_scrub_guard.py`
fails if any fixture contains cookies, auth headers or token-shaped headers.

## Ground rules

- Runs against your own Chrome, started with a dedicated profile. Agent Surf never handles passwords.
- Read-only: it navigates, scrolls and reads. It does not type, post or submit.
- CAPTCHAs are never bypassed. Agent Surf pauses and asks you to solve them.
- Logged-in scraping can breach a platform's terms and get accounts banned. Use accounts you can lose.

## License

MIT
