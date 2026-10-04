# Agent Surf

A local agent browser with interface memory. A model learns a page once and writes a site map; plain code replays it on every later run with no model calls, and only new items come back. The model is called again only when a map breaks.

Targets: X, Reddit, Instagram, Facebook, LinkedIn, YouTube.

Status: pre-build. See [BUILD-BRIEF.md](BUILD-BRIEF.md).

## Ground rules

- Runs against your own Chrome, started with a dedicated profile. Agent Surf never handles passwords.
- Read-only: it navigates, scrolls and reads. It does not type, post or submit.
- CAPTCHAs are never bypassed. Agent Surf pauses and asks you to solve them.
- Logged-in scraping can breach a platform's terms and get accounts banned. Use accounts you can lose.

## License

MIT
