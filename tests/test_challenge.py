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


# Fix 36: invisible reCAPTCHA anchors are not challenges; visible challenge
# frames are. Decision: the visible reCAPTCHA checkbox ("I'm not a robot",
# 304x78) needs a human click, so it IS a challenge even though it is smaller
# than MIN_CHALLENGE_FRAME_PX; the invisible badge (size=invisible) is not.
@pytest.mark.parametrize("path,expected", [
    ("/recaptcha/zero", None),             # LinkedIn: 0x0 enterprise anchor
    ("/recaptcha/hidden", None),           # Reddit: 256x60 anchor, hidden
    ("/recaptcha/offscreen", None),        # pushed off the page
    ("/recaptcha/badge", None),            # visible invisible-reCAPTCHA badge
    ("/recaptcha/bframe-hidden", None),    # preloaded image challenge, hidden
    ("/recaptcha/checkbox", "reCAPTCHA checkbox (304x78)"),
    ("/recaptcha/bframe", "recaptcha challenge frame (400x580)"),
    ("/turnstile/widget", "challenges.cloudflare.com challenge frame (300x65)"),
    ("/recaptcha", "visible recaptcha iframe (304x154)"),   # a large captcha frame (300x150 + border)
])
def test_visibility_and_size_decide(har_page, path, expected):
    page = har_page("challenges.har")
    page.goto("https://feed.test" + path)
    reason = challenge.detect_page(page)
    if expected is None:
        assert reason is None, reason
    else:
        assert reason and expected in reason
    # the URL-only rule would have flagged every one of them
    assert detect(page.url, page.title(), page.frame_urls())


def test_frame_reason_pure():
    from agent_surf.challenge import MIN_CHALLENGE_FRAME_PX as N, FrameInfo, frame_reason

    anchor = "https://www.google.com/recaptcha/enterprise/anchor?ar=1&k=x"
    assert frame_reason(FrameInfo(anchor, False, 304, 78)) is None
    assert frame_reason(FrameInfo(anchor, True, 0, 0)) is None
    assert frame_reason(FrameInfo(anchor + "&size=invisible", True, 256, 60)) is None
    assert frame_reason(FrameInfo(anchor + "&size=invisible", True, N, N))    # big enough anyway
    assert frame_reason(FrameInfo(anchor, True, 304, 78))                     # the checkbox
    assert frame_reason(FrameInfo("https://www.google.com/recaptcha/api2/bframe?k=x", True, 1, 1))
    assert frame_reason(FrameInfo("https://www.google.com/recaptcha/api2/bframe?k=x", False, 400, 580)) is None
    assert frame_reason(FrameInfo("https://x.test/embed", True, 800, 800)) is None
    assert challenge.detect_frames("https://x.test/checkpoint", "", []) == "URL path contains /checkpoint"


def test_challenge_that_appears_later_is_caught(har_page):
    page = har_page("challenges.har")
    page.goto("https://feed.test/recaptcha/late")
    assert challenge.detect_page(page) is None       # only the hidden anchor at first
    page.wait(1.5)
    sent = []
    challenge.wait_until_clear(page, notify=sent.append, poll_s=0.5, timeout_s=20)
    assert len(sent) == 1 and "challenge frame" in sent[0]   # the image challenge opened
    assert challenge.detect_page(page) is None       # and the wait ended once it was hidden again


class FlippingFrames(FakePage):
    """The frame list is re-read on every poll: visible, visible, then hidden."""

    def __init__(self, states):
        super().__init__(["https://feed.test/"])
        self.states = list(states)

    def frame_infos(self):
        return [{"url": "https://www.google.com/recaptcha/api2/bframe?k=x", "visible": self.states[0],
                 "width": 400, "height": 580}]

    def advance(self, _seconds):
        if len(self.states) > 1:
            self.states.pop(0)


def test_wait_loop_rechecks_visibility():
    page = FlippingFrames([True, True, False])
    sent, sleeps = [], []

    def sleep(s):
        sleeps.append(s)
        page.advance(s)

    challenge.wait_until_clear(page, notify=sent.append, sleep=sleep)
    assert len(sent) == 1 and sleeps == [5.0, 5.0]
