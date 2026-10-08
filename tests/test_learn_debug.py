"""Fix 9: keep evidence from failed (and, on request, successful) learn-action
runs, outside the repo and free of cookies, auth headers and tokens."""

import json

import pytest

from agent_surf import action_learner, cli, executor, runner
from agent_surf.action_learner import ActionLearnError

from composekit import good_map
from test_learner import FakeClient
from test_scrub_guard import problems_in_text

FILES = {"error.txt", "notes.txt", "aria-before.yaml", "aria-after.yaml"}


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(executor, "STEP_TIMEOUT_S", 3.0)
    monkeypatch.setattr(runner, "FIRST_PASS_TIMEOUT_S", 5.0)


def plain_map(**changes):
    m = good_map(**changes)
    m["start"] = dict(m["start"], url_template="https://compose.test/compose-plain")
    return m


def learn(store, page, reply, tmp_path, **kw):
    return action_learner.learn_action(store, page, "composetest", "post", client=FakeClient(reply), model="m",
                                       maps_dir=tmp_path / "maps", start_url="https://compose.test/compose-plain",
                                       debug_dir=tmp_path / "home" / "debug", **kw)


def assert_clean(folder):
    for f in folder.iterdir():
        assert problems_in_text(f.read_text(encoding="utf-8")) == [], f.name


def test_failed_rehearsal_writes_evidence(store, compose, tmp_path):
    page, backend = compose()
    reply = json.dumps(plain_map(discard=[{"op": "press", "value": "Tab"}]))
    with pytest.raises(ActionLearnError, match="still open") as err:
        learn(store, page, reply, tmp_path)
    folder = err.value.debug_path
    assert folder.parent == tmp_path / "home" / "debug" and folder.name.endswith("-composetest-post")
    assert {f.name for f in folder.iterdir()} == FILES | {"rejected-map.json"}
    rejected = json.loads((folder / "rejected-map.json").read_text())
    assert rejected["discard"] == [{"op": "press", "value": "Tab"}] and rejected["site"] == "composetest"
    assert "still open" in (folder / "error.txt").read_text()
    assert "pass 1 (text only): step 1: text entered via fill" in (folder / "notes.txt").read_text()
    assert 'textbox "Post text"' in (folder / "aria-before.yaml").read_text()
    assert (folder / "aria-after.yaml").read_text()
    assert_clean(folder)
    assert backend.create_calls == 0


def test_invalid_map_writes_evidence(store, compose, tmp_path):
    page, _ = compose()
    m = plain_map(start={"url_template": "https://compose.test/compose-plain", "requires": ["text"]})
    m["steps"][1]["value"] = "buy now"
    with pytest.raises(ActionLearnError, match="invalid") as err:
        learn(store, page, json.dumps(m), tmp_path)
    assert (err.value.debug_path / "rejected-map.json").exists()
    assert_clean(err.value.debug_path)


def test_no_map_returned_writes_nothing(store, compose, tmp_path):
    page, _ = compose()
    with pytest.raises(ActionLearnError, match="not valid JSON") as err:
        learn(store, page, "no map {", tmp_path)
    assert err.value.debug_path is None and not (tmp_path / "home" / "debug").exists()


def test_success_writes_only_with_keep_debug(store, compose, tmp_path):
    page, _ = compose()
    learn(store, page, json.dumps(plain_map()), tmp_path)
    assert not (tmp_path / "home" / "debug").exists()
    page, _ = compose()
    learn(store, page, json.dumps(plain_map()), tmp_path, keep_debug=True)
    (folder,) = (tmp_path / "home" / "debug").iterdir()
    assert {f.name for f in folder.iterdir()} == FILES | {"learned-map.json"}
    assert "no error" in (folder / "error.txt").read_text()
    assert "pass 2 (with media)" in (folder / "notes.txt").read_text()
    assert_clean(folder)


def test_never_written_inside_the_repo(tmp_path):
    inside = action_learner.REPO_ROOT / "debug-should-not-exist"
    assert action_learner.write_debug(inside, "x", "post", amap={}, error="e", notes=[],
                                      aria_before="", aria_after="") is None
    assert not inside.exists()


@pytest.fixture
def cli_env(tmp_path, monkeypatch, chromium):
    from conftest import COMPOSE_SITE, FIXTURES  # noqa: F401
    monkeypatch.setenv("AGENT_SURF_HOME", str(tmp_path / "home"))
    ctx = chromium.new_context()
    ctx.route_from_har(FIXTURES / "compose.har", not_found="abort")

    class Session:
        def __init__(self, endpoint):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def open_tab(self):
            return ctx.new_page()

    monkeypatch.setattr(cli, "BrowserSession", Session)
    yield monkeypatch
    ctx.close()


def test_cli_prints_the_debug_folder(cli_env, tmp_path, capsys):
    bad = json.dumps(plain_map(discard=[{"op": "press", "value": "Tab"}]))
    cli_env.setattr(cli, "make_client", lambda cfg: FakeClient(bad))
    code = cli.main(["learn-action", "composetest", "post", "--start", "https://compose.test/compose-plain"])
    err = capsys.readouterr().err
    assert code == 1 and "debug evidence: " + str(tmp_path / "home" / "debug") in err
    cli_env.setattr(cli, "make_client", lambda cfg: FakeClient(json.dumps(plain_map())))
    code = cli.main(["learn-action", "composetest", "post", "--start", "https://compose.test/compose-plain",
                     "--keep-debug"])
    assert code == 0 and "debug evidence: " in capsys.readouterr().err
    assert len(list((tmp_path / "home" / "debug").iterdir())) == 2
