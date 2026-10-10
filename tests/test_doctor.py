"""Fix 28: agent-surf doctor. No model calls, no publishing, at most one tab
per site (closed again), no secret values in its output."""

import json
from pathlib import Path

import pytest

from agent_surf import actionmap, challenge, cli, config, doctor, outbox, sitemap, sites
from agent_surf.store import Store

from composekit import good_map
from test_logged_out import LoginWall
from test_runner import make_map

KEY = "sk-ant-SYNTHETIC-DOCTOR-KEY"
TOKEN = "123456:SYNTHETIC-TELEGRAM-TOKEN"


def good_versions(name):
    return doctor.PINNED[name]


def test_pinned_versions_match_requirements():
    req = Path(__file__).parent.parent / "requirements.txt"
    pins = dict(line.strip().split("==") for line in req.read_text().splitlines()
                if "==" in line and not line.startswith("#"))
    assert {k.lower(): v for k, v in pins.items()} == doctor.PINNED


def test_packages_check():
    assert doctor.check_packages(good_versions).status == "ok"
    bad = doctor.check_packages(lambda n: "0.0.1" if n == "anthropic" else doctor.PINNED[n])
    assert bad.status == "fail" and "anthropic 0.0.1 (want 1.3.0)" in bad.detail

    def missing(n):
        raise doctor.metadata.PackageNotFoundError(n)

    assert "missing" in doctor.check_packages(missing).detail


def cfg_for(tmp_path, **env):
    return config.load({"AGENT_SURF_HOME": str(tmp_path / "home"), **env})


def test_cdp_check(tmp_path):
    cfg = cfg_for(tmp_path)
    urls = []

    def get(url):
        urls.append(url)
        return {"Browser": "Chrome/154.0", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/SECRET-ID"}

    check, endpoint = doctor.check_cdp(cfg, get)
    assert check.status == "ok" and "Chrome/154.0 at 127.0.0.1:9222" in check.detail
    assert "SECRET-ID" not in check.detail and urls == ["http://127.0.0.1:9222/json/version"]

    def down(url):
        raise ConnectionRefusedError()

    check, endpoint = doctor.check_cdp(cfg, down)
    assert check.status == "fail" and endpoint is None and "ConnectionRefusedError" in check.detail

    remote = cfg_for(tmp_path, AGENT_SURF_CDP_URL="http://10.0.0.5:9222")
    check, _ = doctor.check_cdp(remote, lambda u: pytest.fail("must not contact a remote host"))
    assert check.status == "fail" and "refusing CDP endpoint" in check.detail


class Session:
    """BrowserSession stand-in over HAR-backed pages; counts tabs per site."""

    def __init__(self, make_page, walls=None):
        self.make_page, self.walls, self.opened = make_page, walls or {}, []

    def __call__(self, endpoint):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def new_page(self, site):
        har = "compose.har" if site.name == "composetest" else "feed.har"
        page = self.make_page(har, site)
        if site.name in self.walls:
            self.walls[site.name].install(page._page)
        self.opened.append((site.name, page))
        return page


@pytest.fixture
def markers(monkeypatch):
    monkeypatch.setitem(sites.LOGGED_OUT, "feedtest", sites.LoggedOutMarkers(("/login",), ()))
    monkeypatch.setitem(sites.HOME_URL, "composetest", "https://compose.test/compose-plain")


def setup_store(cfg, maps=True, lookup=True):
    cfg.home.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.db_path)
    if maps:
        m = make_map()
        store.add_map(m["site"], m["page_type"], m["version"], sitemap.save_map(cfg.maps_dir, m), None)
        am = good_map()
        if not lookup:
            am.pop("lookup", None)
        actionmap.save_action_map(store, cfg.maps_dir, am)
    return store


def run(cfg, store, session, **kw):
    return doctor.run_doctor(cfg, store, env={}, open_session=session,
                             http_get=lambda u: {"Browser": "Chrome/154"}, version=good_versions,
                             wait_s=0.1, **kw)


def by(report, check, site=None):
    return [c for c in report["checks"] if c["check"] == check and (site is None or c.get("site") == site)]


def test_full_report_logged_in(tmp_path, har_page, markers):
    cfg = cfg_for(tmp_path)
    store = setup_store(cfg)
    session = Session(har_page)
    report = run(cfg, store, session)
    assert report["ok"] is True
    assert report["sites"] == ["composetest", "feedtest"]
    assert [c["status"] for c in by(report, "logged_in")] == ["ok", "ok"]
    assert by(report, "reading_map", "feedtest")[0]["status"] == "ok"
    assert by(report, "action_map", "composetest")[0]["status"] == "ok"
    # the lookup page (composetest profile) has no reading map: a warning, not a failure
    assert by(report, "lookup", "composetest")[0]["status"] == "warn"
    assert by(report, "account", "composetest")[0]["status"] == "warn"
    # at most one tab per site, each closed
    assert sorted(name for name, _ in session.opened) == ["composetest", "feedtest"]
    assert all(p._page.is_closed() for _, p in session.opened)
    store.close()


def test_logged_out_site_fails(tmp_path, har_page, markers):
    cfg = cfg_for(tmp_path)
    store = setup_store(cfg)
    wall = LoginWall("feed.test", "Log in to FeedTest", "/")
    report = run(cfg, store, Session(har_page, {"feedtest": wall}))
    assert report["ok"] is False
    (feed,) = by(report, "logged_in", "feedtest")
    assert feed["status"] == "fail" and "log in to feedtest in the Agent Surf browser window" in feed["detail"]
    store.close()


def test_invalid_map_fails(tmp_path, har_page, markers):
    cfg = cfg_for(tmp_path)
    store = setup_store(cfg)
    path = store.current_map("feedtest", "home")["path"]
    Path(path).write_text("{}")
    report = run(cfg, store, Session(har_page))
    assert report["ok"] is False and by(report, "reading_map", "feedtest")[0]["status"] == "fail"
    store.close()


def test_no_sites_needs_no_browser(tmp_path):
    cfg = cfg_for(tmp_path)
    store = setup_store(cfg, maps=False)

    def down(url):
        raise OSError("refused")

    report = doctor.run_doctor(cfg, store, env={}, open_session=lambda e: pytest.fail("no tab"),
                               http_get=down, version=good_versions)
    assert report["ok"] is True and by(report, "cdp")[0]["status"] == "warn"
    assert by(report, "sites")[0]["status"] == "warn"
    store.close()


def test_counts_and_presence_checks(tmp_path, har_page, markers):
    cfg = cfg_for(tmp_path)
    store = setup_store(cfg)
    qid = outbox.add(store, "composetest", "post", {"text": "a"})
    outbox.approve(store, qid)
    outbox.add(store, "composetest", "post", {"text": "b"})
    store.add_new_item("feedtest", "home", "i1", {"item_id": "i1"})
    (cfg.home / "debug" / "2026-x").mkdir(parents=True)
    env = {"ANTHROPIC_API_KEY": KEY, "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"}
    report = doctor.run_doctor(cfg, store, env=env, open_session=Session(har_page),
                               http_get=lambda u: {"Browser": "Chrome/154"}, version=good_versions, wait_s=0.1)
    assert report["queue"]["approved"] == 1 and report["queue"]["draft"] == 1
    assert report["home"]["items"] == 1 and report["home"]["undelivered_items"] == 1
    assert report["home"]["debug_folders"] == 1 and report["home"]["bytes"] > 0
    assert by(report, "anthropic_key")[0]["detail"] == "present via env"
    assert by(report, "telegram")[0]["status"] == "ok"
    assert by(report, "items")[0]["status"] == "warn"
    text = json.dumps(report) + doctor.format_text(report)
    assert KEY not in text and TOKEN not in text
    store.close()


def test_anthropic_key_sources(monkeypatch):
    assert doctor.check_anthropic({}).detail.startswith("missing")
    monkeypatch.setattr(config, "anthropic_key_and_source", lambda env=None: (KEY, "credential manager"))
    assert doctor.check_anthropic({}).detail == "present via credential manager"


def test_cli_doctor_json_shape_exit_and_no_secrets(tmp_path, monkeypatch, capsys, har_page, markers):
    home = tmp_path / "home"
    monkeypatch.setenv("AGENT_SURF_HOME", str(home))
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    setup_store(config.load()).close()
    monkeypatch.setattr(cli, "BrowserSession", Session(har_page))
    monkeypatch.setattr(doctor, "_http_get_json", lambda url, timeout=3.0: {"Browser": "Chrome/154"})
    monkeypatch.setattr(doctor, "HOME_WAIT_S", 0.1)
    monkeypatch.setattr(cli, "make_client", lambda cfg: pytest.fail("doctor must not make a model client"))
    assert cli.main(["doctor", "--json"]) == 0
    out = capsys.readouterr()
    report = json.loads(out.out)
    assert set(report) == {"ok", "sites", "checks", "home", "queue"}
    assert all(set(c) == {"check", "status", "detail", "site"} for c in report["checks"])
    assert all(c["status"] in ("ok", "warn", "fail") for c in report["checks"])
    assert KEY not in out.out + out.err and TOKEN not in out.out + out.err
    assert cli.main(["doctor"]) == 0
    text = capsys.readouterr().out
    assert "OK   packages" in text and "all required checks passed" in text and KEY not in text
    # a logged-out site makes it exit 1
    monkeypatch.setattr(cli, "BrowserSession",
                        Session(har_page, {"feedtest": LoginWall("feed.test", "Log in to FeedTest", "/")}))
    assert cli.main(["doctor"]) == 1
    assert "FAIL logged_in [feedtest]" in capsys.readouterr().out
