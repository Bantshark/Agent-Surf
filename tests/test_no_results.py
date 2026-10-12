"""Fix 29: an empty result page is not a broken map. A no-results marker, or
(search pages) a quiet page whose main landmark is present but empty, ends the
run with 0 items and stop_reason "no_results": no MapBroken, no relearn, no
model call. learn and learn-action refuse to learn from such pages. A missing
structure still raises MapBroken."""

import json

import pytest

from agent_surf import action_learner, cli, learner, noresults, runner, sites
from agent_surf.runner import MapBroken

from test_cli import FakeSession, save_map
from test_learner import FakeClient, fenced, model_map
from test_runner import make_map, save


@pytest.fixture(autouse=True)
def feed_markers(monkeypatch):
    monkeypatch.setitem(sites.NO_RESULTS, "feedtest",
                        sites.NoResultsMarkers(texts=("no results for",), testids=("emptyState",)))
    monkeypatch.setattr(runner, "FIRST_PASS_TIMEOUT_S", 3.0)
    monkeypatch.setattr(runner, "FIRST_PASS_QUIET_S", 1.0)


def search_map():
    return make_map(page_type="search")


def run_search(store, har_page, query):
    return runner.run(store, har_page("feed.har"), "feedtest", "search", query=query)


def test_marker_page_is_no_results(store, har_page, tmp_path):
    save(store, tmp_path, search_map())
    result = run_search(store, har_page, "nothing")
    assert result.items == [] and result.stop_reason == "no_results"
    assert store.map_row("feedtest", "search", 1)["drift_count"] == 0   # not counted as drift


def test_marker_by_testid_alone(store, har_page, tmp_path, monkeypatch):
    monkeypatch.setitem(sites.NO_RESULTS, "feedtest", sites.NoResultsMarkers(testids=("emptyState",)))
    save(store, tmp_path, search_map())
    assert run_search(store, har_page, "nothing").stop_reason == "no_results"


def test_empty_results_area_on_search_page_is_no_results(store, har_page, tmp_path, monkeypatch):
    monkeypatch.setitem(sites.NO_RESULTS, "feedtest", sites.NoResultsMarkers())   # no marker at all
    save(store, tmp_path, search_map())
    result = run_search(store, har_page, "blank")
    assert result.items == [] and result.stop_reason == "no_results"


@pytest.mark.parametrize("query", ["broken", "changed"])
def test_missing_or_changed_structure_is_still_broken(store, har_page, tmp_path, query):
    save(store, tmp_path, search_map())
    with pytest.raises(MapBroken, match="no items"):
        run_search(store, har_page, query)


def test_heal_path_never_calls_the_model_on_no_results(store, har_page, tmp_path):
    save(store, tmp_path, search_map())
    client = FakeClient(fenced(model_map(page_type="search")))
    for query in ("nothing", "blank"):
        result = learner.run_with_heal(store, har_page("feed.har"), "feedtest", "search", client=client,
                                       model="m", maps_dir=tmp_path / "maps", query=query)
        assert result.stop_reason == "no_results"
    assert client.calls == []
    assert store.current_map("feedtest", "search")["version"] == 1


def test_learn_refuses_no_results_and_empty_pages(store, har_page, tmp_path, monkeypatch):
    monkeypatch.setattr(learner, "LEARN_DELAY_S", 0.2)
    client = FakeClient(fenced(model_map(page_type="search")))
    for query in ("nothing", "blank"):
        with pytest.raises(learner.LearnError, match="nothing to learn from"):
            learner.learn(store, har_page("feed.har"), "feedtest", "search", client=client, model="m",
                          maps_dir=tmp_path / "maps", query=query)
    assert client.calls == [] and store.current_map("feedtest", "search") is None


def test_learn_action_refuses_an_empty_page(store, compose, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "FIRST_PASS_TIMEOUT_S", 1.0)
    page, backend = compose()
    page.raw.route("https://compose.test/empty", lambda route: route.fulfill(
        status=200, content_type="text/html", body="<main><h1>Nothing here</h1></main>"))
    client = FakeClient("```json\n{}\n```")
    with pytest.raises(action_learner.ActionLearnError, match="nothing to learn from"):
        action_learner.learn_action(store, page, "composetest", "post", client=client, model="m",
                                    maps_dir=tmp_path, start_url="https://compose.test/empty")
    assert client.calls == []


def test_cli_run_no_results_exits_0(tmp_path, monkeypatch, capsys, har_page):
    home = tmp_path / "home"
    monkeypatch.setenv("AGENT_SURF_HOME", str(home))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SYNTHETIC")   # a key is set: still no relearn
    save_map(home, search_map())
    monkeypatch.setattr(cli, "BrowserSession", FakeSession(har_page))
    monkeypatch.setattr(cli, "make_client", lambda cfg: FakeClient())
    assert cli.main(["run", "feedtest", "search", "--query", "nothing", "--json"]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out) == [] and "no results" in out.err and "map broken" not in out.err


# -- detector units --------------------------------------------------------

SNAP_EMPTY = """- navigation "Primary":
  - link "Home"
  - link "More"
  - link "Late"
  - link "Explore"
- main:
  - heading "Search" [level=1]
  - region "Results"
"""
SNAP_FULL = SNAP_EMPTY + "".join(f'  - article:\n    - paragraph: post {i}\n' for i in range(3))
SNAP_NO_MAIN = '- heading "Search" [level=1]\n- paragraph: Loading results\n'


def test_emptiness():
    assert noresults.emptiness(SNAP_EMPTY) == (True, True)       # nav links do not count
    assert noresults.emptiness(SNAP_FULL) == (False, True)
    assert noresults.emptiness(SNAP_NO_MAIN) == (True, False)


def test_takes_query():
    assert sites.takes_query("x", "search") and not sites.takes_query("x", "home")
    assert sites.takes_query("feedtest", "search") and not sites.takes_query("feedtest", "never")


@pytest.mark.parametrize("site", ["x", "linkedin", "reddit", "instagram", "facebook"])
def test_site_markers_exist(site):
    m = sites.no_results_markers(site)
    assert m.texts or m.testids or m.titles or m.paths
