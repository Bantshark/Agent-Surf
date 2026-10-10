"""Command line: python -m agent_surf <command>.

Results go to stdout (JSON with --json, on every command); logs go to stderr.
Exit codes: 0 ok, 1 error, 2 usage, 3 CAPTCHA not cleared, 4 map broken and no
API key, 5 publish refused (not attempted), 6 publish needs attention, 7 logged
out (no login within 10 minutes; nothing relearned, nothing sent to the model).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from agent_surf import challenge, config, sitemap, sites
from agent_surf.browser import BrowserError, BrowserSession, cdp_endpoint, chrome_instructions
from agent_surf.store import Store

log = logging.getLogger("agent_surf")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CHALLENGE = 3
EXIT_MAP_BROKEN = 4
EXIT_PUBLISH_REFUSED = 5    # not attempted: not approved, content changed, no action map, caps
EXIT_NEEDS_ATTENTION = 6    # attempted, not confirmed published: see `queue show <id>`
EXIT_LOGGED_OUT = 7         # login wall not cleared in time; a queue item stays approved


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-surf", description="Local agent browser with interface memory.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print the result as JSON")
    sub = p.add_subparsers(dest="command", required=True)
    _add = sub.add_parser

    def add_parser(name: str, **kw: Any) -> argparse.ArgumentParser:
        return _add(name, parents=[common], **kw)

    sub.add_parser = add_parser  # every command gets --json

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

    maps = sub.add_parser("maps", help="list or show learned maps")
    msub = maps.add_subparsers(dest="maps_command", required=True)
    msub.add_parser("list", parents=[common])
    show = msub.add_parser("show", parents=[common])
    show.add_argument("site")
    show.add_argument("page_type")

    yt = sub.add_parser("youtube", help="YouTube metadata, subtitles, comments via yt-dlp")
    yt.add_argument("target", help="YouTube URL or ytsearchN:query")
    yt.add_argument("--subs", action="store_true", help="include subtitles")
    yt.add_argument("--comments", action="store_true", help="include comments (best-effort)")

    scrub = sub.add_parser("scrub", help="strip cookies/auth from a HAR file")
    scrub.add_argument("src")
    scrub.add_argument("dst")

    # v2: write layer
    la = sub.add_parser("learn-action", help="learn a composer once (rehearsed, never submitted)")
    la.add_argument("site")
    la.add_argument("action", choices=["post", "reply", "dm", "comment"])
    la.add_argument("--target", help="post URL to reply to / comment on")
    la.add_argument("--thread", help="conversation URL for dm")
    la.add_argument("--start", help="composer URL, overriding the site default for posts")
    la.add_argument("--keep-debug", action="store_true",
                    help="also write the debug folder ($AGENT_SURF_HOME/debug) when learning succeeds")

    q = sub.add_parser("queue", help="add, review, approve or reject what will be published")
    qsub = q.add_subparsers(dest="queue_command", required=True)
    qa = qsub.add_parser("add", parents=[common], help="add a draft")
    qa.add_argument("site")
    qa.add_argument("action", choices=["post", "reply", "dm", "comment"])
    qa.add_argument("--text")
    qa.add_argument("--media", nargs="+", default=[], metavar="FILE")
    qa.add_argument("--target", help="post URL (reply, comment)")
    qa.add_argument("--thread", help="conversation URL (dm)")
    qa.add_argument("--at", help="ISO 8601 time to publish (no offset = local time)")
    qa.add_argument("--missed", choices=["skip", "run", "ask"], default="ask",
                    help="if the machine was asleep at --at (default ask)")
    ql = qsub.add_parser("list", parents=[common])
    ql.add_argument("--status")
    qs = qsub.add_parser("show", parents=[common])
    qs.add_argument("id", type=int)
    qp = qsub.add_parser("approve", parents=[common], help="freeze the content and allow publishing")
    g = qp.add_mutually_exclusive_group(required=True)
    g.add_argument("id", type=int, nargs="?")
    g.add_argument("--all-drafts", action="store_true")
    qr = qsub.add_parser("reject", parents=[common])
    qr.add_argument("id", type=int)

    pub = sub.add_parser("publish", help="publish one approved queue item and read back its receipt")
    pub.add_argument("id", type=int)
    d = sub.add_parser("dispatch", help="publish due approved items every 30 s")
    d.add_argument("--once", action="store_true", help="one tick, then exit")
    ib = sub.add_parser("inbox", help="new notifications/messages (zero model calls)")
    ib.add_argument("site")
    ib.add_argument("page_type")
    sub.add_parser("receipts", help="published items and their receipts")

    it = sub.add_parser("items", help="stored reading items again (e.g. ones never printed)")
    it.add_argument("--site")
    it.add_argument("--page-type")
    itg = it.add_mutually_exclusive_group()
    itg.add_argument("--run", metavar="RUN_ID", help="items stored by one run (run_id= on stderr)")
    itg.add_argument("--since", type=_since, metavar="ISO8601", help="items captured at or after")
    itg.add_argument("--undelivered", action="store_true",
                     help="items stored but never printed (stdout failed)")
    it.add_argument("--mark-delivered", action="store_true",
                    help="mark the printed items delivered once stdout is flushed")

    acct = sub.add_parser("account", help="your own handle per site (for checking unknown outcomes)")
    asub = acct.add_subparsers(dest="account_command", required=True)
    aset = asub.add_parser("set", parents=[common])
    aset.add_argument("site")
    aset.add_argument("--handle", required=True)
    asub.add_parser("show", parents=[common])

    acts = sub.add_parser("actions", help="edit learned action maps (always as a new version)")
    actsub = acts.add_subparsers(dest="actions_command", required=True)
    sl = actsub.add_parser("set-lookup", parents=[common],
                           help="where to look for your post after an unknown outcome")
    sl.add_argument("site")
    sl.add_argument("action", choices=["post", "reply", "dm", "comment"])
    sl.add_argument("--page-type", required=True)
    slg = sl.add_mutually_exclusive_group()
    slg.add_argument("--handle")
    slg.add_argument("--query")

    pg = sub.add_parser("purge", help="delete old stored items and debug folders (maps, queue, "
                                      "receipts and seen ids are kept)")
    pg.add_argument("--items-older-than", type=int, default=90, metavar="DAYS")
    pg.add_argument("--debug-older-than", type=int, default=14, metavar="DAYS")
    pg.add_argument("--dry-run", action="store_true")
    return p


def _since(value: str) -> str:
    """ISO 8601 to the store's UTC format; a naive time is taken as UTC."""
    from datetime import datetime, timezone

    try:
        t = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO 8601 time: {value!r}") from None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def print_items(items: list[dict], as_json: bool) -> None:
    if as_json:
        print(json.dumps(items, ensure_ascii=False, indent=1))
        return
    for item in items:
        fields = {k: v for k, v in item.items() if k not in ("site", "page_type", "item_id")}
        summary = " | ".join(f"{k}: {str(v)[:120]}" for k, v in fields.items()
                             if v is not None and k not in ("subtitles", "comments", "description"))
        print(f"{item['item_id']}\t{summary}")


def start_run(store: Store) -> str:
    """Give this invocation a run_id: stamped on every item it stores, printed to stderr."""
    from agent_surf.store import new_run_id

    store.run_id = new_run_id()
    print(f"run_id={store.run_id}", file=sys.stderr, flush=True)
    return store.run_id


def _undelivered_hint(store: Store, run_id: str) -> None:
    n = len(store.select_items(run_id=run_id, undelivered=True))
    if n:
        log.error("%d item(s) from run_id=%s were stored and marked seen but not printed; "
                  "get them with: agent-surf items --run %s (or --undelivered)", n, run_id, run_id)


def deliver(store: Store, items: list[dict], as_json: bool, *, run_id: str | None = None,
            rowids: list[int] | None = None) -> int:
    """Print items, flush stdout, and only then mark them delivered. If stdout
    fails they stay undelivered and `items --undelivered` brings them back."""
    try:
        print_items(items, as_json)
        sys.stdout.flush()
    except (OSError, ValueError) as e:   # broken pipe, closed or full stdout
        log.error("could not write to stdout: %s", e)
        if run_id is not None:
            _undelivered_hint(store, run_id)
        return EXIT_ERROR
    store.mark_delivered(run_id=run_id, rowids=rowids)
    return EXIT_OK


class _RunScope:
    """On any failure after items were stored, say how to recover them."""

    def __init__(self, store: Store):
        self.store = store
        self.run_id = start_run(store)

    def __enter__(self) -> str:
        return self.run_id

    def __exit__(self, exc_type: Any, *a: Any) -> None:
        if exc_type is not None:
            _undelivered_hint(self.store, self.run_id)


def make_client(cfg: config.Config) -> Any:
    if not config.anthropic_api_key():
        return None
    import anthropic

    return anthropic.Anthropic()  # reads ANTHROPIC_API_KEY itself; never logged here


def emit(args: argparse.Namespace, data: Any, text: str | None = None) -> None:
    """--json prints data; otherwise the human text (if any)."""
    if getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=False, indent=1))
    elif text is not None:
        print(text)


def cmd_chrome(cfg: config.Config, args: argparse.Namespace) -> int:
    from agent_surf.browser import chrome_command

    emit(args, {"command": chrome_command(cfg.home, cfg.cdp_url),
                "warning": "the debugging port gives local processes full control of that profile"},
         chrome_instructions(cfg.home, cfg.cdp_url))
    return EXIT_OK


def cmd_learn(cfg: config.Config, args: argparse.Namespace) -> int:
    from agent_surf import learner

    prune_debug(cfg)
    site = sites.get_site(args.site)
    sites.build_url(args.site, args.page_type, query=args.query, handle=args.handle)
    client = make_client(cfg)
    if client is None:
        log.error("learn needs ANTHROPIC_API_KEY in the environment")
        return EXIT_ERROR
    with Store(cfg.db_path) as store, BrowserSession(cdp_endpoint(cfg)) as session:
        m = learner.learn(store, session.new_page(site), args.site, args.page_type, client=client,
                          model=cfg.model, maps_dir=cfg.maps_dir, query=args.query,
                          handle=args.handle, guard=challenge.make_guard())
    log.info("learned %s %s v%d (source: %s)", args.site, args.page_type, m["version"], m["source"])
    emit(args, m)
    return EXIT_OK


def cmd_run(cfg: config.Config, args: argparse.Namespace) -> int:
    from agent_surf import learner, runner

    site = sites.get_site(args.site)
    sites.build_url(args.site, args.page_type, query=args.query, handle=args.handle)
    with Store(cfg.db_path) as store:
        runner.load_current_map(store, args.site, args.page_type)  # refuse before touching Chrome
        client = make_client(cfg)
        with _RunScope(store) as run_id:
            with BrowserSession(cdp_endpoint(cfg)) as session:
                try:
                    result = learner.run_with_heal(
                        store, session.new_page(site), args.site, args.page_type, client=client,
                        model=cfg.model, maps_dir=cfg.maps_dir, query=args.query, handle=args.handle,
                        guard=challenge.make_guard())
                except runner.MapBroken as e:
                    log.error("map broken: %s. Set ANTHROPIC_API_KEY to self-heal, or run "
                              "agent-surf learn %s %s", e.reason, args.site, args.page_type)
                    return EXIT_MAP_BROKEN
            code = deliver(store, result.items, args.json, run_id=run_id)
    log.info("%d new item(s)", len(result.items))
    return code


def cmd_maps(cfg: config.Config, args: argparse.Namespace) -> int:
    with Store(cfg.db_path) as store:
        if args.maps_command == "list":
            rows = [dict(r) for r in store.list_maps()]
            if not rows:
                log.info("no maps yet; run: agent-surf learn <site> <page_type>")
            emit(args, rows, "\n".join(f"{r['site']}\t{r['page_type']}\tv{r['version']}\t{r['created_at']}"
                                       f"\t{r['path']}" for r in rows) or None)
            return EXIT_OK
        row = store.current_map(args.site, args.page_type)
        if row is None:
            log.error("no map for %s %s", args.site, args.page_type)
            return EXIT_ERROR
        print(json.dumps(sitemap.load_map(row["path"]), indent=2, ensure_ascii=False))
        return EXIT_OK


def cmd_youtube(cfg: config.Config, args: argparse.Namespace) -> int:
    from agent_surf import youtube

    with Store(cfg.db_path) as store, _RunScope(store) as run_id:
        items = youtube.run_youtube(store, args.target, subs=args.subs, comments=args.comments)
        code = deliver(store, items, args.json, run_id=run_id)
    log.info("%d new video(s)", len(items))
    return code


def cmd_scrub(cfg: config.Config, args: argparse.Namespace) -> int:
    from agent_surf import har_scrub

    stats = har_scrub.scrub_file(args.src, args.dst)
    log.info("scrubbed %s -> %s: removed %d header(s), %d cookie(s); redacted %d param(s), %d body(ies)",
             args.src, args.dst, stats.headers, stats.cookies, stats.params, stats.bodies)
    emit(args, {"headers": stats.headers, "cookies": stats.cookies, "params": stats.params,
                "bodies": stats.bodies})
    return EXIT_OK


# ---------------------------------------------------------------------------
# v2: write layer

def _item_line(item: dict) -> str:
    p = item["payload"]
    what = (p.get("text") or "").replace("\n", " ")[:60] + (f" [+{len(p['media'])} media]" if p.get("media") else "")
    when = item["scheduled_at"] or "-"
    return f"{item['id']}\t{item['status']}\t{item['site']} {item['action']}\t{when}\t{what}"


def _publish_one(cfg: config.Config, store: Store, item_id: int) -> Any:
    """Publish one item in its own browser session; its tabs close afterwards."""
    from agent_surf import publisher
    from agent_surf.executor import ActionPage

    with BrowserSession(cdp_endpoint(cfg)) as session:
        def open_page(site: sites.Site) -> ActionPage:
            return ActionPage(session.open_tab(), site)
        return publisher.publish(store, open_page, item_id, client=make_client(cfg), model=cfg.model,
                                 maps_dir=cfg.maps_dir, guard=challenge.make_guard())


def cmd_learn_action(cfg: config.Config, args: argparse.Namespace) -> int:
    from agent_surf import action_learner
    from agent_surf.executor import ActionPage

    prune_debug(cfg)
    site = sites.get_site(args.site)
    client = make_client(cfg)
    if client is None:
        log.error("learn-action needs ANTHROPIC_API_KEY in the environment")
        return EXIT_ERROR
    with Store(cfg.db_path) as store, BrowserSession(cdp_endpoint(cfg)) as session:
        try:
            m = action_learner.learn_action(
                store, ActionPage(session.open_tab(), site), args.site, args.action, client=client,
                model=cfg.model, maps_dir=cfg.maps_dir, target_url=args.target, thread_url=args.thread,
                start_url=args.start, guard=challenge.make_guard(),
                debug_dir=cfg.home / "debug", keep_debug=args.keep_debug)
        except action_learner.ActionLearnError as e:
            log.error("%s", e)
            if e.debug_path:
                log.error("debug evidence: %s", e.debug_path)
            emit(args, {"error": str(e), "debug": str(e.debug_path) if e.debug_path else None})
            return EXIT_ERROR
    log.info("learned %s %s action map v%d (rehearsed, nothing published)", args.site, args.action, m["version"])
    emit(args, m)
    return EXIT_OK


def cmd_queue(cfg: config.Config, args: argparse.Namespace) -> int:
    from agent_surf import outbox

    with Store(cfg.db_path) as store:
        c = args.queue_command
        if c == "add":
            payload = {k: v for k, v in (("text", args.text), ("media", args.media),
                                         ("target_url", args.target), ("thread_url", args.thread)) if v}
            qid = outbox.add(store, args.site, args.action, payload, scheduled_at=args.at,
                             missed_policy=args.missed)
            item = outbox.get(store, qid)
            log.info("added draft %d; review it, then: agent-surf queue approve %d", qid, qid)
            emit(args, item, str(qid))
        elif c == "list":
            items = outbox.list_items(store, args.status)
            emit(args, items, "\n".join(_item_line(i) for i in items) or None)
        elif c == "show":
            item = outbox.get(store, args.id)
            try:
                media = outbox.check_media(item["payload"].get("media") or [])
            except outbox.QueueError as e:
                media = [{"error": str(e)}]
            item["media_info"] = media
            lines = [json.dumps({k: v for k, v in item.items() if k != "media_info"}, ensure_ascii=False, indent=1)]
            lines += [f"media[{i}]: {m['path']}\t{m['type']}\t{m['size']} bytes\tsha256:{m['sha256'][:12]}"
                      if "path" in m else f"media: {m['error']}" for i, m in enumerate(media)]
            emit(args, item, "\n".join(lines))
        elif c == "approve":
            items = outbox.approve_all_drafts(store) if args.all_drafts else [outbox.approve(store, args.id)]
            log.info("approved %d item(s); content is frozen (content_hash)", len(items))
            emit(args, items, "\n".join(_item_line(i) for i in items) or None)
        elif c == "reject":
            item = outbox.reject(store, args.id)
            emit(args, item, _item_line(item))
    return EXIT_OK


def cmd_publish(cfg: config.Config, args: argparse.Namespace) -> int:
    from agent_surf import publisher

    with Store(cfg.db_path) as store:
        try:
            result = _publish_one(cfg, store, args.id)
        except publisher.PublishRefused as e:
            log.error("not published: %s", e)
            emit(args, {"id": args.id, "status": "refused", "reason": str(e)})
            return EXIT_PUBLISH_REFUSED
    item = result.item
    emit(args, item, _item_line(item) + (f"\t{item['receipt']['permalink']}" if item["receipt"] else ""))
    if item["status"] != "published":
        log.error("queue item %d needs attention: %s", item["id"], item["last_error"])
        return EXIT_NEEDS_ATTENTION
    return EXIT_OK


def cmd_dispatch(cfg: config.Config, args: argparse.Namespace) -> int:
    from agent_surf import dispatcher, publisher
    from agent_surf.executor import ActionPage

    with Store(cfg.db_path) as store:
        def recover() -> None:
            if not outbox_has(store, "publishing"):
                return
            with BrowserSession(cdp_endpoint(cfg)) as session:
                publisher.recover_publishing(store, lambda site: ActionPage(session.open_tab(), site))

        def on_tick(report: Any) -> None:
            data = {"at": report.at, "resumed": report.resumed, "missed": report.missed,
                    "published": report.published, "not_published": report.not_published}
            if args.json:
                print(json.dumps(data, ensure_ascii=False), flush=True)
            elif report.published or report.missed or report.not_published:
                log.info("tick %s: %s", report.at, data)

        try:
            dispatcher.run(store, lambda i: _publish_one(cfg, store, i), recover_fn=recover,
                           notify=challenge.make_notifier(), once=args.once, on_tick=on_tick)
        except KeyboardInterrupt:
            log.info("dispatcher stopped")
    return EXIT_OK


def outbox_has(store: Store, status: str) -> bool:
    return store.conn.execute("SELECT 1 FROM queue WHERE status = ? LIMIT 1", (status,)).fetchone() is not None


def cmd_inbox(cfg: config.Config, args: argparse.Namespace) -> int:
    from agent_surf import inbox, runner

    prune_debug(cfg)
    site = sites.get_site(args.site)
    if args.page_type not in sites.INBOX_PAGE_TYPES.get(args.site, set()):
        log.error("%s %s is not an inbox page; inbox pages: %s", args.site, args.page_type,
                  ", ".join(sorted(sites.INBOX_PAGE_TYPES.get(args.site, set()))) or "none")
        return EXIT_ERROR
    with Store(cfg.db_path) as store:
        runner.load_current_map(store, args.site, args.page_type)
        with _RunScope(store) as run_id:
            with BrowserSession(cdp_endpoint(cfg)) as session:
                try:
                    result = inbox.run_inbox(store, session.new_page(site), args.site, args.page_type,
                                             client=make_client(cfg), model=cfg.model,
                                             maps_dir=cfg.maps_dir, guard=challenge.make_guard())
                except runner.MapBroken as e:
                    log.error("map broken: %s", e.reason)
                    return EXIT_MAP_BROKEN
            code = deliver(store, result.items, args.json, run_id=run_id)
    log.info("%d new item(s)", len(result.items))
    return code


def cmd_items(cfg: config.Config, args: argparse.Namespace) -> int:
    """Stored items in the same shape `run` prints (a plain JSON array with --json)."""
    with Store(cfg.db_path) as store:
        rows = store.select_items(site=args.site, page_type=args.page_type, run_id=args.run,
                                  since=args.since, undelivered=args.undelivered)
        items = [json.loads(r["data_json"]) for r in rows]
        if args.mark_delivered:
            code = deliver(store, items, args.json, rowids=[r["rowid"] for r in rows])
        else:
            try:
                print_items(items, args.json)
                sys.stdout.flush()
            except (OSError, ValueError) as e:
                log.error("could not write to stdout: %s", e)
                return EXIT_ERROR
            code = EXIT_OK
    log.info("%d item(s)%s", len(items), " marked delivered" if args.mark_delivered and not code else "")
    return code


def cmd_receipts(cfg: config.Config, args: argparse.Namespace) -> int:
    from agent_surf import outbox

    with Store(cfg.db_path) as store:
        rows = [{"id": i["id"], "site": i["site"], "action": i["action"], **(i["receipt"] or {})}
                for i in outbox.list_items(store, "published")]
    emit(args, rows, "\n".join(f"{r['id']}\t{r['site']} {r['action']}\t{r.get('post_id')}\t"
                               f"{r.get('confirmed_at')}\t{r.get('permalink')}\t"
                               f"read_back={r.get('read_back', '-')}" for r in rows) or None)
    return EXIT_OK


def cmd_purge(cfg: config.Config, args: argparse.Namespace) -> int:
    from agent_surf import retention

    with Store(cfg.db_path) as store:
        items = retention.purge_items(store, args.items_older_than, dry_run=args.dry_run)
    debug = retention.purge_debug(cfg.home / "debug", args.debug_older_than, dry_run=args.dry_run)
    verb = "would delete" if args.dry_run else "deleted"
    log.info("%s %d stored item(s) older than %d days and %d debug folder(s) older than %d days",
             verb, items, args.items_older_than, len(debug), args.debug_older_than)
    emit(args, {"dry_run": args.dry_run, "items": items, "debug_folders": [str(p) for p in debug]})
    return EXIT_OK


def cmd_account(cfg: config.Config, args: argparse.Namespace) -> int:
    from agent_surf import accounts

    with Store(cfg.db_path) as store:
        if args.account_command == "set":
            handle = accounts.set_handle(store, args.site, args.handle)
            log.info("%s account set; learn-action will add a lookup on your profile page", args.site)
            emit(args, {"site": args.site, "handle": handle})
        else:
            rows = accounts.list_accounts(store)
            emit(args, rows, "\n".join(f"{r['site']}\t{r['handle']}\t{r['set_at']}" for r in rows) or None)
    return EXIT_OK


def cmd_actions(cfg: config.Config, args: argparse.Namespace) -> int:
    from agent_surf import actionmap
    from agent_surf.publisher import lookup_problem

    with Store(cfg.db_path) as store:
        m = actionmap.current_action_map(store, args.site, args.action)
        if m is None:
            log.error("no action map for %s %s; run: agent-surf learn-action %s %s",
                      args.site, args.action, args.site, args.action)
            return EXIT_ERROR
        lookup = {"page_type": args.page_type}
        lookup.update({k: v for k, v in (("handle", args.handle), ("query", args.query)) if v})
        new = dict(m, lookup=lookup)
        new.pop("version", None)
        path = actionmap.save_action_map(store, cfg.maps_dir, new)  # validated; a new version
        saved = actionmap.load_action_map(path)
        log.info("saved %s with lookup %s", path.name, lookup)
        problem = lookup_problem(store, saved)
        if problem:
            log.warning("lookup not working yet: %s", problem)
        emit(args, saved)
    return EXIT_OK


def prune_debug(cfg: config.Config) -> None:
    from agent_surf import retention

    for p in retention.prune_debug(cfg.home / "debug"):
        log.info("pruned old debug folder %s", p.name)


COMMANDS = {"chrome": cmd_chrome, "learn": cmd_learn, "run": cmd_run, "maps": cmd_maps,
            "youtube": cmd_youtube, "scrub": cmd_scrub, "learn-action": cmd_learn_action,
            "queue": cmd_queue, "publish": cmd_publish, "dispatch": cmd_dispatch, "inbox": cmd_inbox,
            "receipts": cmd_receipts, "items": cmd_items, "purge": cmd_purge, "account": cmd_account, "actions": cmd_actions}


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
    except challenge.LoggedOut as e:
        log.error("%s", e)
        return EXIT_LOGGED_OUT
    except Exception as e:
        from agent_surf import learner, runner, youtube

        known = (BrowserError, sites.DomainRefused, sites.UnknownSite, sitemap.MapError,
                 runner.NoMap, learner.LearnError, learner.HealFailed, youtube.YouTubeError,
                 ValueError, OSError)  # QueueError/IllegalTransition/ActionMapError are ValueErrors
        if isinstance(e, known):
            log.error("%s", e)
            return EXIT_ERROR
        raise
