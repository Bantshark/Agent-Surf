"""Command line: python -m agent_surf <command>.

Results go to stdout (a JSON array with --json); logs go to stderr.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from agent_surf import challenge, config, sitemap, sites
from agent_surf.browser import BrowserError, BrowserSession, chrome_instructions
from agent_surf.store import Store

log = logging.getLogger("agent_surf")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CHALLENGE = 3
EXIT_MAP_BROKEN = 4


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-surf", description="Local agent browser with interface memory.")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("chrome", help="print the command to start Chrome with a dedicated profile")

    def target_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("site")
        sp.add_argument("page_type")
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--query", help="search query for {query} pages")
        g.add_argument("--handle", help="account/subreddit/post id for {handle} pages")

    target_args(sub.add_parser("learn", help="ask the model to write a map for a page"))
    run = sub.add_parser("run", help="replay a map and print new items (no model calls)")
    target_args(run)
    run.add_argument("--json", action="store_true", help="print new items as a JSON array")

    maps = sub.add_parser("maps", help="list or show learned maps")
    msub = maps.add_subparsers(dest="maps_command", required=True)
    msub.add_parser("list")
    show = msub.add_parser("show")
    show.add_argument("site")
    show.add_argument("page_type")

    yt = sub.add_parser("youtube", help="YouTube metadata, subtitles, comments via yt-dlp")
    yt.add_argument("target", help="YouTube URL or ytsearchN:query")
    yt.add_argument("--subs", action="store_true", help="include subtitles")
    yt.add_argument("--comments", action="store_true", help="include comments (best-effort)")
    yt.add_argument("--json", action="store_true", help="print new items as a JSON array")

    scrub = sub.add_parser("scrub", help="strip cookies/auth from a HAR file")
    scrub.add_argument("src")
    scrub.add_argument("dst")
    return p


def print_items(items: list[dict], as_json: bool) -> None:
    if as_json:
        print(json.dumps(items, ensure_ascii=False, indent=1))
        return
    for item in items:
        fields = {k: v for k, v in item.items() if k not in ("site", "page_type", "item_id")}
        summary = " | ".join(f"{k}: {str(v)[:120]}" for k, v in fields.items()
                             if v is not None and k not in ("subtitles", "comments", "description"))
        print(f"{item['item_id']}\t{summary}")


def make_client(cfg: config.Config) -> Any:
    if not config.anthropic_api_key():
        return None
    import anthropic

    return anthropic.Anthropic()  # reads ANTHROPIC_API_KEY itself; never logged here


def cmd_chrome(cfg: config.Config, args: argparse.Namespace) -> int:
    print(chrome_instructions(cfg.home, cfg.cdp_url))
    return EXIT_OK


def cmd_learn(cfg: config.Config, args: argparse.Namespace) -> int:
    from agent_surf import learner

    site = sites.get_site(args.site)
    sites.build_url(args.site, args.page_type, query=args.query, handle=args.handle)
    client = make_client(cfg)
    if client is None:
        log.error("learn needs ANTHROPIC_API_KEY in the environment")
        return EXIT_ERROR
    with Store(cfg.db_path) as store, BrowserSession(cfg.cdp_url) as session:
        m = learner.learn(store, session.new_page(site), args.site, args.page_type, client=client,
                          model=cfg.model, maps_dir=cfg.maps_dir, query=args.query,
                          handle=args.handle, guard=challenge.make_guard())
    log.info("learned %s %s v%d (source: %s)", args.site, args.page_type, m["version"], m["source"])
    return EXIT_OK


def cmd_run(cfg: config.Config, args: argparse.Namespace) -> int:
    from agent_surf import learner, runner

    site = sites.get_site(args.site)
    sites.build_url(args.site, args.page_type, query=args.query, handle=args.handle)
    with Store(cfg.db_path) as store:
        runner.load_current_map(store, args.site, args.page_type)  # refuse before touching Chrome
        client = make_client(cfg)
        with BrowserSession(cfg.cdp_url) as session:
            try:
                result = learner.run_with_heal(
                    store, session.new_page(site), args.site, args.page_type, client=client,
                    model=cfg.model, maps_dir=cfg.maps_dir, query=args.query, handle=args.handle,
                    guard=challenge.make_guard())
            except runner.MapBroken as e:
                log.error("map broken: %s. Set ANTHROPIC_API_KEY to self-heal, or run "
                          "agent-surf learn %s %s", e.reason, args.site, args.page_type)
                return EXIT_MAP_BROKEN
    print_items(result.items, args.json)
    log.info("%d new item(s)", len(result.items))
    return EXIT_OK


def cmd_maps(cfg: config.Config, args: argparse.Namespace) -> int:
    with Store(cfg.db_path) as store:
        if args.maps_command == "list":
            rows = store.list_maps()
            if not rows:
                log.info("no maps yet; run: agent-surf learn <site> <page_type>")
            for r in rows:
                print(f"{r['site']}\t{r['page_type']}\tv{r['version']}\t{r['created_at']}\t{r['path']}")
            return EXIT_OK
        row = store.current_map(args.site, args.page_type)
        if row is None:
            log.error("no map for %s %s", args.site, args.page_type)
            return EXIT_ERROR
        print(json.dumps(sitemap.load_map(row["path"]), indent=2, ensure_ascii=False))
        return EXIT_OK


def cmd_youtube(cfg: config.Config, args: argparse.Namespace) -> int:
    from agent_surf import youtube

    with Store(cfg.db_path) as store:
        items = youtube.run_youtube(store, args.target, subs=args.subs, comments=args.comments)
    print_items(items, args.json)
    log.info("%d new video(s)", len(items))
    return EXIT_OK


def cmd_scrub(cfg: config.Config, args: argparse.Namespace) -> int:
    from agent_surf import har_scrub

    stats = har_scrub.scrub_file(args.src, args.dst)
    log.info("scrubbed %s -> %s: removed %d header(s), %d cookie(s); redacted %d param(s), %d body(ies)",
             args.src, args.dst, stats.headers, stats.cookies, stats.params, stats.bodies)
    return EXIT_OK


COMMANDS = {"chrome": cmd_chrome, "learn": cmd_learn, "run": cmd_run, "maps": cmd_maps,
            "youtube": cmd_youtube, "scrub": cmd_scrub}


def main(argv: list[str] | None = None) -> int:
    # Windows redirects stdout as cp1252; items routinely contain emoji.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="agent-surf: %(message)s",
                        force=True)
    cfg = config.load()
    try:
        if args.command not in ("chrome", "scrub"):
            cfg.ensure_dirs()
        return COMMANDS[args.command](cfg, args)
    except challenge.ChallengeTimeout as e:
        log.error("%s", e)
        return EXIT_CHALLENGE
    except Exception as e:
        from agent_surf import learner, runner, youtube

        known = (BrowserError, sites.DomainRefused, sites.UnknownSite, sitemap.MapError,
                 runner.NoMap, learner.LearnError, learner.HealFailed, youtube.YouTubeError,
                 ValueError, OSError)
        if isinstance(e, known):
            log.error("%s", e)
            return EXIT_ERROR
        raise
