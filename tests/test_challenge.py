import logging
import urllib.error

import pytest

from agent_surf import challenge
from agent_surf.challenge import ChallengeTimeout, detect

TOKEN = "123456:SYNTHETIC-not-a-real-bot-token"


@pytest.mark.parametrize("path,expected", [
    ("/clean", None),
    ("/checkpoint/start", "/checkpoint"),
    ("/wait", "Just a moment"),
    ("/turnstile", "challenges.cloudflare.com"),
    ("/recaptcha", "recaptcha"),
    ("/hcaptcha", "hcaptcha"),
])
def test_detection_on_synthetic_pages(har_page, path, expected):
    page = har_page("challenges.har")
    page.goto("https://feed.test" + path)
    reason = challenge.detect_page(page)
    if expected is None:
        assert reason is None
    else:
        assert expected in reason


def test_detect_pure():
    assert detect("https://x.com/i/flow/captcha", "", [])
    assert detect("https://www.linkedin.com/checkpoint/challenge/x", "", [])
    assert detect("https://x.com/home", "", []) is None
    assert detect("https://x.com/search?q=/captcha", "", []) is None  # query, not path
    assert detect("https://x.com/home", "Home", ["https://x.com/embed"]) is None


class FakePage:
    def __init__(self, urls):
        self._urls = list(urls)
        self.url = self._urls.pop(0)

    def title(self):
        return ""

    def frame_urls(self):
        return []

    def advance(self, _seconds):
        if self._urls:
            self.url = self._urls.pop(0)


def test_no_challenge_no_notification():
    sent = []
    challenge.wait_until_clear(FakePage(["https://feed.test/"]), notify=sent.append, sleep=lambda s: None)
    assert sent == []


def test_waits_until_cleared_and_notifies_once():
    page = FakePage(["https://feed.test/checkpoint", "https://feed.test/checkpoint", "https://feed.test/"])
    sent, sleeps = [], []

    def sleep(s):
        sleeps.append(s)
        page.advance(s)

    challenge.wait_until_clear(page, notify=sent.append, sleep=sleep)
    assert len(sent) == 1 and "/checkpoint" in sent[0]
    assert sleeps == [5.0, 5.0]


def test_times_out_after_ten_minutes():
    page = FakePage(["https://feed.test/captcha"])
    now = [0.0]

    def sleep(s):
        now[0] += s

    sent = []
    with pytest.raises(ChallengeTimeout):
        challenge.wait_until_clear(page, notify=sent.append, sleep=sleep, clock=lambda: now[0])
    assert now[0] == 600.0
    assert len(sent) == 1


def test_real_page_clears(har_page):
    page = har_page("challenges.har")
    page.goto("https://feed.test/checkpoint/clears")
    sent = []
    challenge.wait_until_clear(page, notify=sent.append, poll_s=1.0, timeout_s=20)
    assert len(sent) == 1
    assert challenge.detect_page(page) is None


class FakeHTTPResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def test_telegram_message_sent(capsys):
    requests = []

    def urlopen(req, timeout):
        requests.append(req)
        return FakeHTTPResponse()

    notify = challenge.make_notifier({"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"}, urlopen=urlopen)
    notify("challenge detected")
    (req,) = requests
    assert req.get_method() == "POST"
    assert req.full_url.endswith("/sendMessage")
    assert b"chat_id=42" in req.data and b"challenge+detected" in req.data
    assert TOKEN not in capsys.readouterr().err


def test_telegram_failure_never_leaks_token(capsys, caplog):
    def urlopen(req, timeout):
        raise urllib.error.URLError(f"failed for {req.full_url}")

    caplog.set_level(logging.DEBUG)
    notify = challenge.make_notifier({"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"}, urlopen=urlopen)
    notify("challenge detected")
    captured = capsys.readouterr()
    assert TOKEN not in captured.err + captured.out + caplog.text
    assert "URLError" in caplog.text


def test_without_telegram_prints_to_stderr(capsys):
    def urlopen(req, timeout):
        raise AssertionError("must not call Telegram when unset")

    challenge.make_notifier({}, urlopen=urlopen)("challenge detected")
    assert "challenge detected" in capsys.readouterr().err
