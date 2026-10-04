import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_surf import cli, sitemap
from agent_surf.store import Store

from test_har_scrub import SECRET, dirty_har
from test_runner import make_map
from test_youtube import VIDEO, FakeYDL


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SURF_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    return tmp_path / "home"


def save_map(home, m):
    path = sitemap.save_map(home / "maps", m)
    with Store(home / "surf.db") as s:
        s.add_map(m["site"], m["page_type"], m["version"], path, None)


class FakeSession:
    """Replaces the CDP session with an offline HAR-backed page."""

    def __init__(self, make_page):
        self.make_page = make_page

    def __call__(self, cdp_url):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def new_page(self, site):
        return self.make_page("feed.har")


def test_module_entry_point():
    out = subprocess.run([sys.executable, "-m", "agent_surf", "--help"], capture_output=True, text=True)
    assert out.returncode == 0
    for cmd in ("chrome", "learn", "run", "maps", "youtube", "scrub"):
        assert cmd in out.stdout


def test_chrome(home, capsys):
    assert cli.main(["chrome"]) == 0
    out = capsys.readouterr().out
    assert "--remote-debugging-port=9222" in out
    assert str(home / "chrome-profile") in out
    assert "full control" in out


def test_maps_list_and_show(home, capsys):
    assert cli.main(["maps", "list"]) == 0
    assert capsys.readouterr().out == ""
    save_map(home, make_map())
    assert cli.main(["maps", "list"]) == 0
    assert capsys.readouterr().out.startswith("feedtest\thome\tv1\t")
    assert cli.main(["maps", "show", "feedtest", "home"]) == 0
    assert json.loads(capsys.readouterr().out)["network"]["id_path"] == "id"
    assert cli.main(["maps", "show", "x", "home"]) == 1


def test_run_json_then_delta(home, capsys, monkeypatch, har_page):
    save_map(home, make_map())
    monkeypatch.setattr(cli, "BrowserSession", FakeSession(har_page))
    assert cli.main(["run", "feedtest", "home", "--json"]) == 0
    captured = capsys.readouterr()
    items = json.loads(captured.out)
    assert [i["item_id"] for i in items] == [f"p{i}" for i in range(1, 16)]
    assert "15 new item(s)" in captured.err
    assert cli.main(["run", "feedtest", "home", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_run_without_map_refuses_before_browser(home, capsys, monkeypatch):
    def no_browser(*a):
        raise AssertionError("must not connect to Chrome")

    monkeypatch.setattr(cli, "BrowserSession", no_browser)
    assert cli.main(["run", "feedtest", "home"]) == 1
    assert "agent-surf learn feedtest home" in capsys.readouterr().err


def test_run_broken_map_without_key(home, capsys, monkeypatch, har_page):
    net = dict(make_map()["network"], items_path="data.timeline[*]")
    save_map(home, make_map(network=net, dom=None))
    monkeypatch.setattr(cli, "BrowserSession", FakeSession(har_page))
    assert cli.main(["run", "feedtest", "home"]) == 4
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_learn_needs_api_key(home, capsys):
    assert cli.main(["learn", "feedtest", "home"]) == 1
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_bad_target_arguments(home, capsys):
    assert cli.main(["run", "x", "search"]) == 1
    assert "needs --query" in capsys.readouterr().err
    assert cli.main(["run", "myspace", "feed"]) == 1
    with pytest.raises(SystemExit):
        cli.main(["run", "x", "search", "--query", "a", "--handle", "b"])


def test_youtube(home, capsys, monkeypatch):
    import yt_dlp
    monkeypatch.setattr(yt_dlp, "YoutubeDL", lambda opts: FakeYDL(opts, VIDEO))
    assert cli.main(["youtube", "https://youtu.be/vid00000001", "--json"]) == 0
    assert [i["item_id"] for i in json.loads(capsys.readouterr().out)] == ["vid00000001"]
    assert cli.main(["youtube", "https://youtu.be/vid00000001", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert cli.main(["youtube", "https://evil.test/v"]) == 1


def test_scrub(home, tmp_path, capsys):
    src, dst = tmp_path / "in.har", tmp_path / "out.har"
    src.write_text(json.dumps(dirty_har()))
    assert cli.main(["scrub", str(src), str(dst)]) == 0
    captured = capsys.readouterr()
    assert SECRET not in dst.read_text()
    assert SECRET not in captured.out + captured.err
    assert "removed 8 header(s), 2 cookie(s)" in captured.err


EMOJI_SCRIPT = r'''
import sys
import yt_dlp
from agent_surf import cli


class FakeYDL:
    def __init__(self, opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def extract_info(self, target, download):
        return {"id": "vid00000001", "title": "Synthetic \U0001f3ad show été",
                "webpage_url": "https://www.youtube.com/watch?v=vid00000001"}

    @staticmethod
    def sanitize_info(info):
        return info


yt_dlp.YoutubeDL = FakeYDL
sys.exit(cli.main(sys.argv[1:]))
'''


def test_cli_prints_non_ascii_on_cp1252_stdout(tmp_path):
    """Windows redirects stdout as cp1252; --json output with an emoji must not crash."""
    env = dict(os.environ, PYTHONIOENCODING="cp1252", AGENT_SURF_HOME=str(tmp_path / "home"))
    out = subprocess.run(
        [sys.executable, "-c", EMOJI_SCRIPT, "youtube", "https://youtu.be/vid00000001", "--json"],
        capture_output=True, env=env, cwd=Path(__file__).parent.parent)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")
    items = json.loads(out.stdout.decode("utf-8"))
    assert items[0]["title"] == "Synthetic \U0001f3ad show été"
