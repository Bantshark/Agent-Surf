"""Fix 22: a login wall is not a broken map. It is detected (sites.LOGGED_OUT
markers) after navigation, before the first pass, while waiting for content,
before anything goes to the model and in the executor's guard; the human is
told to log in; the run continues once they have, or exits 7 after the wait.
Never MapBroken, never a relearn or self-heal, never a model call."""

import re
from urllib.parse import urlsplit

import pytest

from agent_surf import (action_learner, challenge, cli, executor, learner, outbox, publisher,
                        runner, sitemap, sites)
from agent_surf.challenge import LoggedOut, LoggedOutTimeout

from composekit import FakeBackend, good_map
from test_learner import FakeClient, fenced, model_map
from test_runner import make_map, save

LOGIN_HTML = """<!doctype html><html><head><title>{title}</title>
<script>history.replaceState(null, "", "/login")</script></head><body>
<main><h1>{title}</h1><form><input aria-label="Username" name="u">
<input aria-label="Password" type="password" name="p"><button type="button">Log in</button>
</form></main></body></html>"""


@pytest.fixture(autouse=True)
def markers(monkeypatch):
    monkeypatch.setitem(sites.LOGGED_OUT, "feedtest",
                        sites.LoggedOutMarkers(("/login",), ("log in to feedtest",)))
    monkeypatch.setitem(sites.LOGGED_OUT, "composetest",
                        sites.LoggedOutMarkers(("/login",), ("log in to composetest",)))
    monkeypatch.setattr(executor, "STEP_TIMEOUT_S", 3.0)
    monkeypatch.setattr(publisher, "CONFIRM_TIMEOUT_S", 3.0)
    monkeypatch.setattr(publisher, "READBACK_TIMEOUT_S", 3.0)
    monkeypatch.setattr(runner, "FIRST_PASS_TIMEOUT_S", 5.0)


class LoginWall:
    """While logged out, every page navigation on the host lands on /login, a
    synthetic login page (served in place; its script sets the URL to /login,
    as a redirect would: Chromium does not route a fulfilled 302's target). ``login()`` plays the human: sets the
    session and lands the tab on ``landing`` as a site would after login."""

    def __init__(self, host, title, landing):
        self.host, self.title, self.landing = host, title, landing
        self.logged_in = False
        self.pages = []

    def install(self, raw_page):
        raw_page.context.route(re.compile(rf"^https://{re.escape(self.host)}/"), self._handle)
        self.pages.append(raw_page)
        return raw_page

    def _handle(self, route):
        req = route.request
        if req.is_navigation_request() and (urlsplit(req.url).path == "/login" or not self.logged_in):
            return route.fulfill(status=200, content_type="text/html",
                                 body=LOGIN_HTML.format(title=self.title))
        return route.fallback()

    def login(self):
        self.logged_in = True
        self.pages[-1].goto(f"https://{self.host}{self.landing}", wait_until="domcontentloaded")


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def guard_for(wall=None, login_after=None, timeout_s=20.0):
    """A real challenge.Guard with a fake clock: each poll advances it 5 s;
    the wall logs in on poll ``login_after`` (never if None)."""
    clock, notes, polls = Clock(), [], [0]

    def sleep(s):
        polls[0] += 1
        clock.t += s
        if wall is not None and login_after is not None and polls[0] == login_after:
            wall.login()

    g = challenge.make_guard(notify=notes.append, poll_s=5.0, timeout_s=timeout_s, sleep=sleep, clock=clock)
    return g, notes


def feed_wall(har_page, landing="/more"):
    wall = LoginWall("feed.test", "Log in to FeedTest", landing)
    page = har_page("feed.har")
    wall.install(page._page)
    return wall, page


def assert_login_notice(notes, site):
    assert len(notes) == 1
    assert notes[0].startswith(f"log in to {site} in the Agent Surf browser window")


# -- markers ---------------------------------------------------------------

SPEC = {
    "x": ("x.com", ["/login", "/i/flow/login", "/i/flow/signup"]),
    "instagram": ("www.instagram.com", ["/accounts/login", "/accounts/emailsignup"]),
    "facebook": ("www.facebook.com", ["/login", "/login.php"]),
    "linkedin": ("www.linkedin.com", ["/login", "/authwall", "/uas/login", "/checkpoint/lg"]),
    "reddit": ("www.reddit.com", ["/login", "/account/login", "/register"]),
}


@pytest.mark.parametrize("site", sorted(SPEC))
def test_markers_per_site_resolve(site):
    host, paths = SPEC[site]
    assert set(paths) <= set(sites.logged_out_markers(site).paths)
    for p in paths:
        for url in (f"https://{host}{p}", f"https://{host}{p}/", f"https://{host}{p}?next=%2Fhome",
                    f"https://{host}{p}/step2"):
            assert challenge.detect_logged_out(site, url, ""), url
    for url in (f"https://{host}/", f"https://{host}/home", f"https://{host}/loginhelp",
                f"https://{host}/someone/status/1"):
        assert challenge.detect_logged_out(site, url, "Home") is None, url


def test_title_markers_and_unknown_site():
    assert challenge.detect_logged_out("x", "https://x.com/", "Log in to X / X")
    assert challenge.detect_logged_out("x", "https://x.com/a/status/1", 'A on X: "Log in to X is down"') is None
    assert challenge.detect_logged_out("nosuchsite", "https://example.test/login", "Log in") is None


def test_linkedin_login_checkpoint_is_not_a_challenge():
    class P:
        site = sites.get_site("linkedin")
        url = "https://www.linkedin.com/checkpoint/lg/login"

        def title(self):
            return "LinkedIn Login"

        def frame_urls(self):
            return []

    assert challenge.logged_out_page(P())
    assert challenge.detect_page(P()) is None           # a login wall, not a CAPTCHA
    P.url = "https://www.linkedin.com/checkpoint/challenge/abc"
    P.title = lambda self: "Security Verification"
    assert challenge.logged_out_page(P()) is None
    assert challenge.detect_page(P())                   # still a challenge


# -- reading side ----------------------------------------------------------

def test_run_on_login_wall_times_out_without_mapbroken_or_model(store, har_page, tmp_path):
    save(store, tmp_path, make_map())
    wall, page = feed_wall(har_page)
    guard, notes = guard_for(wall)
    client = FakeClient()
    with pytest.raises(LoggedOutTimeout, match="still logged out"):
        learner.run_with_heal(store, page, "feedtest", "home", client=client, model="m",
                              maps_dir=tmp_path, guard=guard)
    assert client.calls == []
    assert_login_notice(notes, "feedtest")
    assert store.current_map("feedtest", "home")["version"] == 1   # nothing relearned


def test_run_without_waiting_guard_raises_logged_out_not_mapbroken(store, har_page, tmp_path):
    save(store, tmp_path, make_map())
    wall, page = feed_wall(har_page)
    client = FakeClient()
    with pytest.raises(LoggedOut) as e:
        learner.run_with_heal(store, page, "feedtest", "home", client=client, model="m", maps_dir=tmp_path)
    assert not isinstance(e.value, runner.MapBroken)
    assert client.calls == []


def test_run_continues_after_login_and_reopens_the_page(store, har_page, tmp_path):
    save(store, tmp_path, make_map())
    wall, page = feed_wall(har_page, landing="/more")
    guard, notes = guard_for(wall, login_after=2)
    result = runner.run(store, page, "feedtest", "home", guard=guard)
    assert [it["item_id"] for it in result.items][:3] == ["p1", "p2", "p3"]
    assert urlsplit(page.url).path == "/"          # the run's own page, not the landing page
    assert_login_notice(notes, "feedtest")


def test_learn_on_login_wall_never_calls_the_model(store, har_page, tmp_path):
    wall, page = feed_wall(har_page)
    guard, notes = guard_for(wall)
    client = FakeClient(fenced(model_map()))
    with pytest.raises(LoggedOutTimeout):
        learner.learn(store, page, "feedtest", "home", client=client, model="m", maps_dir=tmp_path,
                      guard=guard)
    assert client.calls == [] and store.current_map("feedtest", "home") is None
    assert_login_notice(notes, "feedtest")
    with pytest.raises(LoggedOut):                       # no guard: refused before the model too
        learner.learn(store, page, "feedtest", "home", client=client, model="m", maps_dir=tmp_path)
    assert client.calls == []


def test_learn_continues_after_login(store, har_page, tmp_path):
    wall, page = feed_wall(har_page)
    guard, notes = guard_for(wall, login_after=1)
    client = FakeClient(fenced(model_map()))
    m = learner.learn(store, page, "feedtest", "home", client=client, model="m", maps_dir=tmp_path,
                      guard=guard)
    assert m["version"] == 1 and len(client.calls) == 1
    assert "Log in to FeedTest" not in client.calls[0]["messages"][0]["content"]


# -- write side ------------------------------------------------------------

def compose_wall(compose, backend=None, landing="/home-inline"):
    wall = LoginWall("compose.test", "Log in to ComposeTest", landing)
    backend = backend or FakeBackend()

    def open_page(site=None):
        page, _ = compose(backend=backend)
        wall.install(page.raw)
        return page

    return wall, backend, open_page


def plain_map():
    m = good_map()
    m["start"] = dict(m["start"], url_template="https://compose.test/compose-plain")
    return m


def test_learn_action_on_login_wall_never_calls_the_model(store, compose, tmp_path):
    wall, backend, open_page = compose_wall(compose)
    guard, notes = guard_for(wall)
    client = FakeClient("```json\n{}\n```")
    with pytest.raises(LoggedOutTimeout):
        action_learner.learn_action(store, open_page(), "composetest", "post", client=client, model="m",
                                    maps_dir=tmp_path, start_url="https://compose.test/compose-plain",
                                    guard=guard)
    assert client.calls == [] and backend.create_calls == 0
    assert_login_notice(notes, "composetest")


def add_approved(store):
    qid = outbox.add(store, "composetest", "post", {"text": "hello from behind the wall"})
    outbox.approve(store, qid)
    return qid


def test_publish_login_timeout_leaves_item_approved(store, compose, tmp_path):
    from agent_surf import actionmap

    wall, backend, open_page = compose_wall(compose)
    actionmap.save_action_map(store, tmp_path / "maps", plain_map())
    qid = add_approved(store)
    before = outbox.get(store, qid)
    guard, notes = guard_for(wall)
    client = FakeClient()
    with pytest.raises(LoggedOutTimeout):
        publisher.publish(store, open_page, qid, client=client, model="m", maps_dir=tmp_path, guard=guard)
    item = outbox.get(store, qid)
    assert item["status"] == "approved"                    # not needs_attention
    assert "logged out" in item["last_error"] and "nothing submitted" in item["last_error"]
    assert item["content_hash"] == before["content_hash"] and outbox.verify_approved(item) is None
    assert client.calls == [] and backend.create_calls == 0
    assert_login_notice(notes, "composetest")
    row = store.conn.execute("SELECT submitted_at, outcome FROM dispatch_log WHERE queue_id = ?",
                             (qid,)).fetchone()
    assert row["submitted_at"] is None and row["outcome"].startswith("logged_out")


def test_publish_without_waiting_guard_leaves_item_approved(store, compose, tmp_path):
    from agent_surf import actionmap

    wall, backend, open_page = compose_wall(compose)
    actionmap.save_action_map(store, tmp_path / "maps", plain_map())
    qid = add_approved(store)
    client = FakeClient()
    with pytest.raises(LoggedOutTimeout):
        publisher.publish(store, open_page, qid, client=client, model="m", maps_dir=tmp_path)
    assert outbox.get(store, qid)["status"] == "approved"
    assert client.calls == [] and backend.create_calls == 0


def test_publish_continues_after_login(store, compose, tmp_path):
    from agent_surf import actionmap

    wall, backend, open_page = compose_wall(compose)
    actionmap.save_action_map(store, tmp_path / "maps", plain_map())
    qid = add_approved(store)
    guard, notes = guard_for(wall, login_after=1)
    client = FakeClient()
    result = publisher.publish(store, open_page, qid, client=client, model="m", maps_dir=tmp_path,
                               guard=guard)
    assert result.status == "published", result.item["last_error"]
    assert backend.create_calls == 1 and client.calls == []
    assert_login_notice(notes, "composetest")


def test_executor_guard_refuses_login_wall(store, compose):
    wall, backend, open_page = compose_wall(compose)
    page = open_page()
    run = executor.StepRunner(page, plain_map(), {"text": "x"})
    with pytest.raises(LoggedOut):
        run.start()


# -- CLI: exit 7 -----------------------------------------------------------

@pytest.fixture
def quick_guard(monkeypatch):
    real = challenge.make_guard

    def make_guard(env=None, **kw):
        return real(env, poll_s=0.01, timeout_s=0.05)

    monkeypatch.setattr(challenge, "make_guard", make_guard)


def test_cli_run_exits_7(tmp_path, monkeypatch, capsys, har_page, quick_guard):
    from test_cli import save_map

    home = tmp_path / "home"
    monkeypatch.setenv("AGENT_SURF_HOME", str(home))
    for k in ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    save_map(home, make_map())

    class Session:
        def __init__(self, endpoint):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def new_page(self, site):
            return feed_wall(har_page)[1]

    monkeypatch.setattr(cli, "BrowserSession", Session)
    assert cli.main(["run", "feedtest", "home"]) == cli.EXIT_LOGGED_OUT == 7
    err = capsys.readouterr().err
    assert "log in to feedtest in the Agent Surf browser window" in err
    assert "still logged out" in err and "map broken" not in err


def test_dispatcher_keeps_going_when_logged_out(store):
    from datetime import datetime, timezone

    from agent_surf import dispatcher

    qid = add_approved(store)

    def publish_fn(i):
        raise LoggedOutTimeout("composetest", "URL path is /login")

    report = dispatcher.tick(store, publish_fn, now=datetime(2026, 10, 10, tzinfo=timezone.utc),
                             notify=lambda t: None)
    assert report.not_published == [(qid, "logged out: composetest: logged out (URL path is /login); "
                                          "log in to composetest in the Agent Surf browser window and "
                                          "run again")]
    assert outbox.get(store, qid)["status"] == "approved"
