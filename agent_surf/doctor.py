"""Fix 28: `agent-surf doctor`, a health check before a run or dispatch.

No model calls, no publishing, no clicks: at most one tab per site, opened on
the site's home page to look for a login wall (challenge.py's Fix 22 markers)
and closed again. Every value it reports is a presence, a count or a version;
never a key, token or cookie.

Each check is "ok", "warn" or "fail". It fails only on what configured sites
need: pinned packages, a reachable loopback CDP endpoint, a logged-in
profile, valid maps. Exit 0 when nothing failed, else 1.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from agent_surf import accounts, actionmap, challenge, config, sitemap, sites
from agent_surf.browser import BrowserError, cdp_endpoint
from agent_surf.store import Store

PINNED = {"playwright": "1.62.0", "anthropic": "1.3.0", "yt-dlp": "2026.8.19"}
HOME_WAIT_S = 3.0
OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    check: str
    status: str
    detail: str
    site: str | None = None


def check_packages(version: Callable[[str], str] = metadata.version) -> Check:
    found, bad = [], []
    for name, want in PINNED.items():
        try:
            have = version(name)
        except metadata.PackageNotFoundError:
            have = None
        found.append(f"{name} {have or 'missing'}")
        if have != want:
            bad.append(f"{name} {have or 'missing'} (want {want})")
    if bad:
        return Check("packages", FAIL, "not the pinned versions: " + ", ".join(bad)
                     + "; run: pip install -r requirements.txt")
    return Check("packages", OK, ", ".join(found))


def _host_port(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    return f"{parts.hostname}:{parts.port}" if parts.port else str(parts.hostname)


def _http_get_json(url: str, timeout: float = 3.0) -> Any:
    # Loopback only (checked before this is called); proxies would not see it anyway.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def check_cdp(cfg: config.Config, http_get: Callable[[str], Any] | None = None) -> tuple[Check, str | None]:
    """(check, endpoint or None). Only the host and port are reported."""
    http_get = http_get or _http_get_json
    try:
        endpoint = cdp_endpoint(cfg)   # refuses a non-loopback host (Fix 14)
    except BrowserError as e:
        return Check("cdp", FAIL, str(e)), None
    parts = urlsplit(endpoint)
    scheme = "https" if parts.scheme in ("https", "wss") else "http"
    try:
        info = http_get(f"{scheme}://{parts.netloc}/json/version")
    except Exception as e:
        return Check("cdp", FAIL, f"no browser at {_host_port(endpoint)} ({type(e).__name__}); "
                                  "start it: agent-surf chrome"), None
    browser = info.get("Browser", "unknown browser") if isinstance(info, dict) else "unknown browser"
    return Check("cdp", OK, f"{browser} at {_host_port(endpoint)} (loopback)"), endpoint


def home_url(site: str) -> str | None:
    if site in sites.HOME_URL:
        return sites.HOME_URL[site]
    for template in sites.get_site(site).page_types.values():
        if "{" not in template:
            return template
    return None


def check_login(session: Any, site: str, *, wait_s: float = HOME_WAIT_S) -> Check:
    """One tab on the site's home page; a login wall fails, a challenge warns."""
    url = home_url(site)
    if url is None:
        return Check("logged_in", WARN, "no home page without placeholders to check", site)
    page = session.new_page(sites.get_site(site))
    try:
        page.goto(url)
        page.wait(wait_s)
        reason = challenge.logged_out_page(page)
        if reason:
            return Check("logged_in", FAIL, f"logged out on {url} ({reason}): log in to {site} in "
                                            "the Agent Surf browser window", site)
        ch = challenge.detect_page(page)
        if ch:
            return Check("logged_in", WARN, f"challenge on {url} ({ch}); solve it in the browser", site)
        return Check("logged_in", OK, f"no login wall on {url}", site)
    except Exception as e:
        return Check("logged_in", FAIL, f"could not open {url} ({type(e).__name__})", site)
    finally:
        try:
            page.close()
        except Exception:
            pass


def configured_sites(store: Store) -> list[str]:
    reading = {r["site"] for r in store.list_maps()}
    acting = {r["site"] for r in store.conn.execute("SELECT DISTINCT site FROM action_maps")}
    return sorted(reading | acting)


def check_maps(store: Store, site: str) -> list[Check]:
    out = []
    for row in [r for r in store.list_maps() if r["site"] == site]:
        name = f"reading map {row['page_type']} v{row['version']}"
        try:
            sitemap.load_map(row["path"])
            out.append(Check("reading_map", OK, f"{name} valid", site))
        except sitemap.MapError as e:
            out.append(Check("reading_map", FAIL, f"{name}: {e}", site))
    from agent_surf.publisher import lookup_problem

    actions = [r["action"] for r in store.conn.execute(
        "SELECT DISTINCT action FROM action_maps WHERE site = ? ORDER BY action", (site,))]
    for action in actions:
        try:
            m = actionmap.current_action_map(store, site, action)
        except actionmap.ActionMapError as e:
            out.append(Check("action_map", FAIL, f"{action}: {e}", site))
            continue
        out.append(Check("action_map", OK, f"{action} v{m['version']} valid", site))
        problem = lookup_problem(store, m)
        out.append(Check("lookup", WARN if problem else OK,
                         f"{action}: {problem}" if problem else f"{action}: lookup on "
                         f"{m['lookup']['page_type']} works", site))
    return out


def check_account(store: Store, site: str) -> Check:
    handle = accounts.get_handle(store, site)
    if handle:
        return Check("account", OK, f"handle set ({handle})", site)
    return Check("account", WARN, f"no handle; set one: agent-surf account set {site} --handle <you>", site)


def check_telegram(env: Any = None) -> Check:
    if config.telegram_credentials(env):
        return Check("telegram", OK, "configured (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID present)")
    return Check("telegram", WARN, "not configured; dispatch needs it or --stderr-only")


def check_anthropic(env: Any = None) -> Check:
    key, source = config.anthropic_key_and_source(env)
    if key:
        return Check("anthropic_key", OK, f"present via {source}")
    return Check("anthropic_key", WARN, "missing (only needed to learn and self-heal): set "
                                        "ANTHROPIC_API_KEY or, on Windows, agent-surf key set")


def _dir_stats(path: Path) -> tuple[int, int]:
    """(bytes, files) under path, not following symlinks."""
    total = files = 0
    if not path.exists():
        return 0, 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
                files += 1
        except OSError:
            pass
    return total, files


def home_stats(cfg: config.Config, store: Store) -> dict:
    size, _ = _dir_stats(cfg.home)
    debug = cfg.home / "debug"
    media = cfg.home / "media"
    (items,) = store.conn.execute("SELECT COUNT(*) FROM items").fetchone()
    (undelivered,) = store.conn.execute("SELECT COUNT(*) FROM items WHERE delivered_at IS NULL").fetchone()
    return {"path": str(cfg.home), "bytes": size,
            "debug_folders": sum(1 for p in debug.iterdir() if p.is_dir()) if debug.is_dir() else 0,
            "media_files": _dir_stats(media)[1], "items": items, "undelivered_items": undelivered}


def queue_counts(store: Store) -> dict:
    from agent_surf import outbox

    counts = {s: 0 for s in outbox.STATUSES}
    for row in store.conn.execute("SELECT status, COUNT(*) AS n FROM queue GROUP BY status"):
        counts[row["status"]] = row["n"]
    return counts


def run_doctor(cfg: config.Config, store: Store, *, env: Any = None,
               open_session: Callable[[str], Any] | None = None,
               http_get: Callable[[str], Any] | None = None,
               version: Callable[[str], str] | None = None,
               wait_s: float | None = None) -> dict:
    """All checks; ``open_session(endpoint)`` gives a BrowserSession-like
    context manager (default: the real one)."""
    if open_session is None:
        from agent_surf.browser import BrowserSession as open_session
    checks: list[Check] = [check_packages(version or metadata.version)]
    cdp, endpoint = check_cdp(cfg, http_get)
    site_names = configured_sites(store)
    if not site_names and cdp.status == FAIL:
        cdp.status = WARN   # nothing configured needs the browser yet
    checks.append(cdp)
    if site_names and endpoint is not None:
        with open_session(endpoint) as session:
            for site in site_names:
                checks.append(check_login(session, site,
                                          wait_s=HOME_WAIT_S if wait_s is None else wait_s))
    elif site_names:
        checks += [Check("logged_in", FAIL, "not checked: no browser", s) for s in site_names]
    for site in site_names:
        checks += check_maps(store, site)
        checks.append(check_account(store, site))
    if not site_names:
        checks.append(Check("sites", WARN, "no maps yet; start with: agent-surf learn <site> <page_type>"))
    checks.append(check_telegram(env))
    checks.append(check_anthropic(env))
    stats = home_stats(cfg, store)
    queue = queue_counts(store)
    if queue.get("needs_attention"):
        checks.append(Check("queue", WARN, f"{queue['needs_attention']} item(s) need attention: "
                                           "agent-surf queue list --status needs_attention"))
    if stats["undelivered_items"]:
        checks.append(Check("items", WARN, f"{stats['undelivered_items']} stored item(s) were never "
                                           "printed: agent-surf items --undelivered"))
    return {"ok": not any(c.status == FAIL for c in checks),
            "sites": site_names,
            "checks": [asdict(c) for c in checks],
            "home": stats,
            "queue": queue}


def format_text(report: dict) -> str:
    lines = []
    for c in report["checks"]:
        where = f" [{c['site']}]" if c.get("site") else ""
        lines.append(f"{c['status'].upper():4} {c['check']}{where}: {c['detail']}")
    h = report["home"]
    lines.append(f"home {h['path']}: {h['bytes']} bytes, {h['debug_folders']} debug folder(s), "
                 f"{h['media_files']} media file(s), {h['items']} item(s)")
    lines.append("queue: " + ", ".join(f"{k} {v}" for k, v in report["queue"].items()))
    lines.append("doctor: " + ("all required checks passed" if report["ok"] else "some checks FAILED"))
    return "\n".join(lines)
