"""Fix 37: Anthropic API errors fail cleanly (one line, exit 1, no key, no
headers), a relearn that could not ask the model is not a failed heal, and
`key set` / `key test` / `doctor --check-key` check the key without ever
showing it. The client is the real anthropic client on a fake transport."""

import logging

import httpx2
import pytest

from agent_surf import cli, credentials, doctor, learner, modelapi, runner
from agent_surf.config import Config

from test_credentials import KEY, utf16, windows  # noqa: F401  (fixture)
from test_learner import broken_map, heal
from test_runner import save

ERRORS = {
    401: (modelapi.ModelUnavailable, "auth", "rejected (401); run: agent-surf key set"),
    403: (modelapi.ModelUnavailable, "permission", "(403)"),
    429: (modelapi.ModelUnavailable, "rate_limit", "rate limited"),
    529: (modelapi.ModelUnavailable, "status", "(529"),
    500: (modelapi.ModelUnavailable, "status", "(500 InternalServerError)"),
}


def fake_client(status=200, *, connect_error=False, seen=None):
    import anthropic

    def handler(request):
        if seen is not None:
            seen.append(request)
        if connect_error:
            raise httpx2.ConnectError("no route to host", request=request)
        if status == 200:
            return httpx2.Response(200, json={"data": [{"type": "model", "id": "m", "display_name": "M",
                                                        "created_at": "2026-01-01T00:00:00Z"}],
                                              "has_more": False, "first_id": "m", "last_id": "m"})
        return httpx2.Response(status, json={"type": "error", "error": {
            "type": "x_error", "message": f"synthetic failure; header was {KEY}"}})

    return anthropic.Anthropic(api_key=KEY, max_retries=0,
                               http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))


@pytest.mark.parametrize("status", sorted(ERRORS))
def test_each_status_becomes_one_clean_line(status):
    _cls, kind, text = ERRORS[status]
    with pytest.raises(modelapi.ModelUnavailable) as e:
        modelapi.call(fake_client(status).messages.create, model="m", max_tokens=1,
                      messages=[{"role": "user", "content": "hi"}])
    assert e.value.kind == kind and text in str(e.value)
    assert KEY not in str(e.value) and "x-api-key" not in str(e.value)
    assert e.value.__context__ is None and e.value.__cause__ is None   # the original is not shown


def test_connection_error():
    with pytest.raises(modelapi.ModelUnavailable, match="cannot reach the Anthropic API") as e:
        modelapi.test_key(fake_client(connect_error=True))
    assert e.value.kind == "connection"


def test_other_errors_pass_through():
    def boom(**kw):
        raise KeyError("not an API error")

    with pytest.raises(KeyError):
        modelapi.call(boom)


def test_key_test_ok_is_one_minimal_call():
    seen = []
    assert modelapi.test_key(fake_client(seen=seen)) == "ok"
    (req,) = seen
    assert req.method == "GET" and req.url.path == "/v1/models" and "limit=1" in str(req.url)


def test_learn_with_rejected_key(store, har_page, tmp_path):
    with pytest.raises(modelapi.ModelUnavailable, match="401"):
        learner.learn(store, har_page("feed.har"), "feedtest", "home", client=fake_client(401),
                      model="m", maps_dir=tmp_path / "maps")
    assert store.current_map("feedtest", "home") is None


def test_self_heal_api_error_is_not_a_failed_relearn(store, har_page, tmp_path):
    save(store, tmp_path, broken_map())
    with pytest.raises(modelapi.ModelUnavailable) as e:
        heal(store, har_page("feed.har"), fake_client(429), tmp_path)
    assert not isinstance(e.value, learner.HealFailed)
    assert "map broken" in str(e.value) and "relearn not attempted" in str(e.value)
    assert store.current_map("feedtest", "home")["version"] == 1   # the map is untouched


class FakeSession:
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


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SURF_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return tmp_path / "home"


@pytest.mark.parametrize("status,exit_text", [(401, "key was rejected (401)"), (429, "rate limited"),
                                             (None, "cannot reach the Anthropic API")])
def test_cli_learn_fails_cleanly(home, monkeypatch, capsys, caplog, har_page, status, exit_text):
    caplog.set_level(logging.DEBUG)
    client = fake_client(connect_error=True) if status is None else fake_client(status)
    monkeypatch.setattr(cli, "make_client", lambda cfg: client)
    monkeypatch.setattr(cli, "BrowserSession", FakeSession(har_page))
    assert cli.main(["learn", "feedtest", "home"]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert exit_text in err and "Traceback" not in err
    assert KEY not in err + caplog.text and "x-api-key" not in err + caplog.text


def test_cli_run_heal_fails_cleanly(home, monkeypatch, capsys, har_page):
    from agent_surf import sitemap
    from agent_surf.store import Store

    m = broken_map()
    path = sitemap.save_map(home / "maps", m)
    with Store(home / "surf.db") as s:
        s.add_map(m["site"], m["page_type"], m["version"], path, None)
    monkeypatch.setattr(cli, "make_client", lambda cfg: fake_client(401))
    monkeypatch.setattr(cli, "BrowserSession", FakeSession(har_page))
    assert cli.main(["run", "feedtest", "home"]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert "relearn not attempted" in err and "401" in err and "Traceback" not in err


# key set: checks before storing; --visible; key test; doctor --check-key

@pytest.mark.parametrize("value,reason", [
    (KEY + KEY, "pasted 2 times"),
    (KEY * 3, "pasted 3 times"),
    ("sk-ant-abc def", "whitespace"),
    ('"' + KEY + '"', "quotes"),
    ("ANTHROPIC_API_KEY=" + KEY, "paste only the value"),
    ("abc-123456", "does not start with 'sk-ant-'"),
])
def test_key_problems(value, reason):
    problems = modelapi.key_problems(value)
    assert any(reason in p for p in problems)
    assert all(KEY not in p and "abc" not in p for p in problems)


def test_good_key_has_no_problems():
    assert modelapi.key_problems(KEY) == []


def test_key_set_doubled_paste_asks_and_refuses(windows, home, monkeypatch, capsys, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    answers = []
    monkeypatch.setattr("getpass.getpass", lambda prompt="": KEY + KEY)
    monkeypatch.setattr("builtins.input", lambda prompt="": answers.append(prompt) or "")
    assert cli.main(["key", "set"]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert windows.written == [] and answers == ["store it anyway? [y/N] "]
    assert f"({2 * len(KEY)} characters)" in err and "pasted 2 times" in err
    assert KEY not in err + caplog.text and KEY[:12] not in err + caplog.text


def test_key_set_confirmed_anyway(windows, home, monkeypatch):  # noqa: F811
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "not-a-claude-key")
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert cli.main(["key", "set"]) == 0
    assert windows.store[credentials.ANTHROPIC_TARGET] == utf16("not-a-claude-key")


def test_key_set_visible(windows, home, monkeypatch, capsys):  # noqa: F811
    prompts = []
    monkeypatch.setattr("getpass.getpass", lambda prompt="": pytest.fail("--visible uses input()"))
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or KEY)
    assert cli.main(["key", "set", "--visible"]) == 0
    assert prompts == ["Anthropic API key (VISIBLE as you paste): "]   # good key: no confirm question
    assert "clear the screen" in capsys.readouterr().err
    assert windows.store[credentials.ANTHROPIC_TARGET] == utf16(KEY)


@pytest.mark.parametrize("status,code,text", [(200, 0, "ok"), (401, 1, "rejected (401)")])
def test_key_test(home, monkeypatch, capsys, caplog, status, code, text):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(cli, "make_client", lambda cfg: fake_client(status))
    assert cli.main(["key", "test"]) == code
    captured = capsys.readouterr()
    assert text in captured.out + captured.err
    assert KEY not in captured.out + captured.err + caplog.text


def test_key_test_without_key(home, capsys):
    assert cli.main(["key", "test"]) == cli.EXIT_ERROR
    assert "no Anthropic key" in capsys.readouterr().err


@pytest.mark.parametrize("status,ok", [(200, "ok"), (401, "fail")])
def test_doctor_check_key(tmp_path, status, ok):
    from agent_surf.store import Store

    cfg = Config(home=tmp_path / "h", model="m", cdp_url="http://127.0.0.1:1")
    with Store(tmp_path / "s.db") as store:
        report = doctor.run_doctor(cfg, store, env={"ANTHROPIC_API_KEY": KEY},
                                   open_session=lambda e: pytest.fail("no tab"),
                                   http_get=lambda url: (_ for _ in ()).throw(OSError("down")),
                                   version=lambda p: "0", only_sites=[],
                                   key_client=lambda: fake_client(status))
    (check,) = [c for c in report["checks"] if c["check"] == "anthropic_key"]
    assert check["status"] == ok and KEY not in str(report)
    without = doctor.check_anthropic({"ANTHROPIC_API_KEY": KEY})
    assert without.status == "ok" and "accepted" not in without.detail



def test_dispatcher_reports_model_error_and_goes_on(store):
    from agent_surf import dispatcher, outbox

    qid = outbox.add(store, "composetest", "post", {"text": "hello"})
    outbox.approve(store, qid)
    sent = []

    def publish_fn(item_id):
        raise modelapi.ModelUnavailable("the Anthropic key was rejected (401); run: agent-surf key set", "auth")

    from datetime import datetime, timezone

    report = dispatcher.tick(store, publish_fn, now=datetime.now(timezone.utc), notify=sent.append)
    assert report.not_published and "model unavailable" in report.not_published[0][1]
    assert len(sent) == 1 and "stays approved" in sent[0]
