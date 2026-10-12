"""Fix 35: the recorded review findings. Domain check after the first-pass
click, ReDoS limits on model-written url_regex, UTF-8 file I/O, HAR scrub of
fragments, path tokens, bearer/JWT values and only the redacted query values
(and no more over-redacting "author"), 0700 data folders, exit 5 for an
invalid action map, --all-drafts needs --yes, --json before a nested
subcommand, and logging state that cannot leak between tests or tokens."""

import json
import logging
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from agent_surf import actionmap, browser, cli, config, har_scrub, outbox, runner, sitemap
from agent_surf.har_scrub import REDACTED, scrub_har, scrub_url

from composekit import good_map
from test_runner import make_map, save

REPO = Path(__file__).parent.parent
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzeW50aGV0aWMifQ.c2lnbmF0dXJlLXN5bnRoZXRpYw"


# -- runner: domain check after the first-pass expand() ---------------------

def test_domain_checked_after_first_pass_expand(store, har_page, tmp_path, monkeypatch):
    save(store, tmp_path, make_map(click=["button.more"]))
    page = har_page("feed.har")
    calls = []
    monkeypatch.setattr(page, "expand", lambda selectors: (1, False))
    real = page.check_domain
    monkeypatch.setattr(page, "check_domain", lambda: (calls.append(page.url), real())[1])
    runner.run(store, page, "feedtest", "home")
    assert calls, "check_domain must run right after the first-pass clicks"


# -- ReDoS ------------------------------------------------------------------

@pytest.mark.parametrize("rx,ok", [
    ("/i/api/graphql/[^/]+/SearchTimeline", True),
    ("/i/api/graphql/[^/]+/CreateTweet", True),
    (r"/api/feed\?", True),
    ("(a+)+$", False), ("(?:[a-z]+/)*x", False), ("(x*)*", False), ("((ab)+c)+", False),
    ("a" * 301, False), ("(", False), ("", False),
])
def test_url_regex_limits(rx, ok):
    assert (sitemap.url_regex_problem(rx) is None) == ok


def test_reading_and_action_maps_refuse_a_dangerous_regex():
    m = make_map()
    m["network"] = dict(m["network"], url_regex="(a+)+$")
    assert any("nested quantifiers" in p for p in sitemap.validate_map(m))
    am = dict(good_map(), version=1)
    am["confirm"] = {"network": dict(am["confirm"]["network"], url_regex="x" * 400)}
    assert any("longer than 300" in p for p in actionmap.validate_action_map(am))


def test_url_search_is_bounded():
    rx = re.compile("NEEDLE")
    assert sitemap.url_matches(rx, "https://feed.test/" + "a" * 100 + "NEEDLE")
    assert not sitemap.url_matches(rx, "https://feed.test/?" + "a" * sitemap.URL_SEARCH_CHARS + "NEEDLE")


# -- UTF-8 everywhere --------------------------------------------------------

def test_maps_written_and_read_as_utf8(tmp_path, monkeypatch):
    import locale

    monkeypatch.setattr(locale, "getpreferredencoding", lambda *a: "cp1252")
    m = make_map()
    m["network"] = dict(m["network"], fields={"text": "body.text", "author": "author.handle"})
    path = sitemap.save_map(tmp_path, dict(m, learned_by="müller \U0001f680"))
    assert sitemap.load_map(path)["learned_by"] == "müller \U0001f680"
    raw = Path(path).read_bytes().decode("utf-8")
    assert "\U0001f680" in raw


def test_no_text_file_io_without_an_encoding():
    """Every read_text/write_text in the package names its encoding."""
    found = []
    for path in sorted((REPO / "agent_surf").glob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\.(read_text|write_text)\(", line) and "encoding=" not in line:
                found.append(f"{path.name}:{n}")
    assert found == []


def test_devtools_active_port_read_as_utf8(tmp_path):
    (tmp_path / "DevToolsActivePort").write_text("9333\n/devtools/browser/abc\n", encoding="utf-8")
    assert browser.read_devtools_active_port(tmp_path) == "ws://127.0.0.1:9333/devtools/browser/abc"


# -- HAR scrub ---------------------------------------------------------------

def test_fragment_parameters_redacted():
    url = "https://feed.test/cb#access_token=SYNTH-TOKEN-1&state=ok"
    assert scrub_url(url) == f"https://feed.test/cb#access_token={REDACTED}&state=ok"


def test_token_shaped_path_segments_redacted_ids_kept():
    token = "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7"
    assert scrub_url(f"https://feed.test/reset/{token}/confirm") == f"https://feed.test/reset/{REDACTED}/confirm"
    assert scrub_url(f"https://feed.test/magic/{JWT}") == f"https://feed.test/magic/{REDACTED}"
    keep = "https://x.com/i/api/graphql/abcDEF123xyz_-AbCdEf/SearchTimeline?x=1"
    assert scrub_url(keep) == keep
    assert scrub_url("https://x.com/i/status/1975000000000000001") == "https://x.com/i/status/1975000000000000001"


def test_only_redacted_query_values_change():
    url = "https://feed.test/api?q=a%20b+c&list=%5B1%2C2%5D&auth_token=SYNTH&x=%E2%9C%93"
    assert scrub_url(url) == f"https://feed.test/api?q=a%20b+c&list=%5B1%2C2%5D&auth_token={REDACTED}&x=%E2%9C%93"


def test_bearer_and_jwt_values_redacted_whatever_the_name():
    har = {"log": {"entries": [{
        "request": {"method": "POST", "url": f"https://feed.test/api?next={JWT}",
                    "headers": [{"name": "X-Custom", "value": "Bearer SYNTHETICbearerVALUE123"}],
                    "queryString": [{"name": "next", "value": JWT}],
                    "postData": {"mimeType": "application/json", "text": json.dumps({"ctx": JWT, "n": 1})}},
        "response": {"status": 200, "headers": [], "content": {"mimeType": "application/json",
                                                               "text": json.dumps({"session_note": "x",
                                                                                   "blob": "Bearer abcdefghijkl"})}},
    }]}}
    clean, _ = scrub_har(har, keep_bodies=True)
    text = json.dumps(clean)
    assert JWT not in text and "SYNTHETICbearerVALUE123" not in text and "abcdefghijkl" not in text
    entry = clean["log"]["entries"][0]
    assert entry["request"]["headers"][0]["name"] == "X-Custom"   # header kept, value redacted
    assert json.loads(entry["request"]["postData"]["text"]) == {"ctx": REDACTED, "n": 1}


def test_author_is_not_a_secret():
    body = {"author": {"handle": "user_a"}, "authority": "ok", "auth_token": "SYNTH", "authorization": "x",
            "oauth_token": "SYNTH", "authenticity_token": "SYNTH"}
    har = {"log": {"entries": [{"request": {"method": "GET", "url": "https://feed.test/", "headers": []},
                                "response": {"status": 200, "headers": [],
                                             "content": {"mimeType": "application/json", "text": json.dumps(body)}}}]}}
    clean, _ = scrub_har(har, keep_bodies=True)
    out = json.loads(clean["log"]["entries"][0]["response"]["content"]["text"])
    assert out["author"] == {"handle": "user_a"} and out["authority"] == "ok"
    for k in ("auth_token", "authorization", "oauth_token", "authenticity_token"):
        assert out[k] == REDACTED, k
    assert har_scrub.is_sensitive("Authorization") and har_scrub.is_sensitive("x-auth-hint")
    assert not har_scrub.is_sensitive("X-Author")


# -- 0700 folders --------------------------------------------------------------

@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_home_and_folders_are_private(tmp_path):
    cfg = config.load({"AGENT_SURF_HOME": str(tmp_path / "home")})
    cfg.ensure_dirs()
    from agent_surf.store import Store

    with Store(cfg.db_path) as store:
        qid = outbox.add(store, "x", "post", {"text": "t"})
    sitemap.save_map(cfg.maps_dir, make_map())
    from agent_surf.action_learner import write_debug

    folder = write_debug(cfg.home / "debug", "x", "post", amap={}, error="e", notes=[],
                         aria_before="", aria_after="")
    for d in (cfg.home, cfg.maps_dir, cfg.home / "debug", folder):
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700, d


# -- CLI ---------------------------------------------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SURF_HOME", str(tmp_path / "home"))
    for k in ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path / "home"


def test_publish_with_invalid_action_map_exits_5(home, capsys, monkeypatch):
    from agent_surf.store import Store

    (home / "maps").mkdir(parents=True)
    with Store(home / "surf.db") as s:
        path = home / "maps" / "x.action-post.v1.json"
        path.write_text('{"site": "x", "action": "post", "version": 1}', encoding="utf-8")
        s.conn.execute("INSERT INTO action_maps VALUES ('x', 'post', 1, ?, 'now')", (str(path),))
        s.conn.commit()
        qid = outbox.add(s, "x", "post", {"text": "t"})
        outbox.approve(s, qid)
    monkeypatch.setattr(cli, "BrowserSession", lambda *a: pytest.fail("no browser"))
    assert cli.main(["publish", str(qid)]) == cli.EXIT_PUBLISH_REFUSED == 5
    assert "action map is invalid" in capsys.readouterr().err
    with Store(home / "surf.db") as s:
        assert outbox.get(s, qid)["status"] == "approved"


def test_publisher_refuses_an_invalid_action_map(store, tmp_path):
    from agent_surf import publisher

    path = tmp_path / "bad.json"
    path.write_text('{"site": "x"}', encoding="utf-8")
    store.conn.execute("INSERT INTO action_maps VALUES ('x', 'post', 1, ?, 'now')", (str(path),))
    qid = outbox.add(store, "x", "post", {"text": "t"})
    outbox.approve(store, qid)
    with pytest.raises(publisher.PublishRefused, match="action map is invalid"):
        publisher.publish(store, lambda site: pytest.fail("no tab"), qid)
    assert outbox.get(store, qid)["status"] == "approved"


def test_approve_all_drafts_needs_yes(home, capsys):
    for t in ("one", "two"):
        assert cli.main(["queue", "add", "x", "post", "--text", t]) == 0
    capsys.readouterr()
    assert cli.main(["queue", "approve", "--all-drafts"]) == 2
    err = capsys.readouterr().err
    assert "one" in err and "two" in err and "--yes" in err
    assert cli.main(["queue", "list", "--status", "approved", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert cli.main(["queue", "approve", "--all-drafts", "--yes", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 2


@pytest.mark.parametrize("argv", [["queue", "--json", "list"], ["queue", "list", "--json"],
                                  ["account", "--json", "show"],
                                  ["sites", "--json", "list"], ["maps", "--json", "list"]])
def test_json_before_or_after_a_nested_subcommand(home, capsys, argv):
    assert cli.main(argv) == 0
    json.loads(capsys.readouterr().out)


def test_json_defaults_off(home, capsys):
    assert cli.main(["queue", "add", "x", "post", "--text", "t"]) == 0
    capsys.readouterr()
    assert cli.main(["queue", "list"]) == 0
    out = capsys.readouterr().out
    assert not out.lstrip().startswith("[")


# -- logging isolation and the token leak --------------------------------------

def test_failing_log_handler_never_prints_the_token(capsys):
    """A broken handler makes logging print the exception being handled; the
    Telegram failure is logged outside its except block, so no token."""
    from agent_surf import challenge

    token = "123456:SYNTHETIC-not-a-real-bot-token"
    closed = open(os.devnull, "w")
    closed.close()
    handler = logging.StreamHandler(closed)
    logging.getLogger().addHandler(handler)
    try:
        def urlopen(req, timeout):
            raise OSError(f"failed for {req.full_url}")

        assert challenge.send_telegram("hi", token, "42", urlopen=urlopen) is False
    finally:
        logging.getLogger().removeHandler(handler)
    captured = capsys.readouterr()
    assert token not in captured.err + captured.out


@pytest.mark.parametrize("order", [
    ["tests/test_cli.py::test_chrome", "tests/test_challenge.py::test_telegram_failure_never_leaks_token"],
    ["tests/test_challenge.py::test_telegram_failure_never_leaks_token", "tests/test_cli.py::test_chrome"],
])
def test_challenge_and_cli_tests_pass_in_either_order(order):
    out = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *order],
                         cwd=REPO, capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stdout[-2000:] + out.stderr[-2000:]
    assert "2 passed" in out.stdout
