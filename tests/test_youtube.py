import json
from pathlib import Path

import pytest

from agent_surf import youtube
from agent_surf.youtube import YouTubeError

VIDEO = {
    "id": "vid00000001", "title": "Synthetic video", "webpage_url": "https://www.youtube.com/watch?v=vid00000001",
    "channel": "Synthetic channel", "channel_id": "UC000", "upload_date": "20260101",
    "duration": 61, "view_count": 10, "like_count": 1, "description": "A made-up video.",
    "formats": [{"format_id": "18"}],
}
VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:02.000
Hello <c>there</c>

00:00:02.000 --> 00:00:04.000
Hello there

00:00:04.000 --> 00:00:06.000
General synthetic
"""


TRACK_URL = "https://www.youtube.com/api/timedtext?v=vid00000001&lang=en&fmt=vtt"


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self):
        return self.data


class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL; never touches the network. ``vtt``: the
    body served for any subtitle URL (an Exception instance is raised)."""
    instances = []

    def __init__(self, opts, info=None, fail_comments=False, vtt=VTT):
        self.opts, self.info, self.fail_comments, self.vtt = opts, info, fail_comments, vtt
        self.calls = []
        self.fetched = []
        FakeYDL.instances.append(self)

    def urlopen(self, url):
        self.fetched.append(url)
        if isinstance(self.vtt, Exception):
            raise self.vtt
        return FakeResponse(self.vtt.encode())

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def extract_info(self, target, download):
        self.calls.append((target, download))
        if self.fail_comments and self.opts.get("getcomments"):
            raise RuntimeError("comments endpoint changed")
        info = json.loads(json.dumps(self.info))
        if self.opts.get("getcomments"):
            info["comments"] = [{"id": "c1", "parent": "root", "author": "@someone",
                                 "text": "Nice", "like_count": 2, "timestamp": 1, "extra": "x"}]
        return info

    @staticmethod
    def sanitize_info(info):
        return info


def factory(info, **kw):
    FakeYDL.instances.clear()
    return lambda opts: FakeYDL(opts, info, **kw)


@pytest.mark.parametrize("target,kind", [
    ("https://www.youtube.com/watch?v=abc", "video"),
    ("https://youtu.be/abc", "video"),
    ("ytsearch5:synthetic query", "search"),
    ("ytsearch:one", "search"),
])
def test_classify_ok(target, kind):
    assert youtube.classify_target(target) == kind


@pytest.mark.parametrize("target", [
    "http://www.youtube.com/watch?v=abc", "https://evil.test/watch", "https://youtube.com.evil.test/",
    "ytsearchall:q", "ytsearch0:q", "ytsearch5000:q", "ytsearch3:  ", "/etc/passwd", "--exec rm",
])
def test_classify_rejects(target):
    with pytest.raises(YouTubeError):
        youtube.classify_target(target)


def test_opts_never_contain_forbidden_keys(tmp_path):
    for subs in (False, True):
        for comments in (False, True):
            for kind in ("video", "search", "playlist"):
                opts = youtube.build_opts(subs=subs, comments=comments, kind=kind)
                assert not youtube.FORBIDDEN_OPTS & opts.keys()
                assert opts["skip_download"] is True
                assert opts["js_runtimes"] == {"node": {}}
                assert opts["ignore_no_formats_error"] is True
                assert ("max_comments" in opts["extractor_args"]["youtube"]) == comments


def test_subs_run_the_player_js_metadata_skips_it():
    """Fix 32: caption tracks need the player; metadata and comments do not."""
    assert "player_skip" not in youtube.build_opts(subs=True)["extractor_args"]["youtube"]
    assert "player_skip" not in youtube.build_opts(subs=True, comments=True)["extractor_args"]["youtube"]
    assert youtube.build_opts()["extractor_args"]["youtube"]["player_skip"] == ["js"]
    assert youtube.build_opts(comments=True)["extractor_args"]["youtube"]["player_skip"] == ["js"]


def test_playlist_options():
    video = youtube.build_opts(kind="video")
    assert video["noplaylist"] is True and "playlistend" not in video
    for kind in ("search", "playlist"):
        opts = youtube.build_opts(kind=kind)
        assert opts["playlistend"] == youtube.MAX_SEARCH and "noplaylist" not in opts


def test_real_youtubedl_accepts_opts(tmp_path):
    from yt_dlp import YoutubeDL
    with YoutubeDL(youtube.build_opts(subs=True, comments=True)) as ydl:
        assert ydl.params["getcomments"] and ydl.params["noplaylist"]
        assert ydl.params["skip_download"] and not ydl.params["remote_components"]


def test_metadata_is_json_serialisable():
    (item,) = youtube.fetch("https://www.youtube.com/watch?v=vid00000001", ydl_factory=factory(VIDEO))
    json.dumps(item)
    assert item["item_id"] == "vid00000001" and item["page_type"] == "video"
    assert "formats" not in item and "subtitles" not in item and "subtitles_status" not in item
    assert item["upload_date"] == "2026-01-01"   # Fix 32: ISO date
    assert FakeYDL.instances[0].calls == [("https://www.youtube.com/watch?v=vid00000001", False)]


def track(lang="en", url=TRACK_URL):
    return {lang: [{"ext": "json3", "url": url + "&j"}, {"ext": "vtt", "url": url}]}


def test_manual_subtitles_as_text():
    info = dict(VIDEO, subtitles=track("en"), automatic_captions=track("en", TRACK_URL + "&kind=asr"))
    (item,) = youtube.fetch("https://youtu.be/vid00000001", subs=True, ydl_factory=factory(info))
    assert item["subtitles"] == "Hello there\nGeneral synthetic"
    assert item["subtitles_status"] == "ok" and item["subtitles_kind"] == "manual"
    assert item["subtitles_lang"] == "en"
    ydl = FakeYDL.instances[0]
    assert ydl.calls[0][1] is False and ydl.fetched == [TRACK_URL]   # never a download


def test_automatic_captions_when_no_manual_track():
    info = dict(VIDEO, subtitles={"de": track("de")["de"]}, automatic_captions=track("en-orig"))
    (item,) = youtube.fetch("https://youtu.be/vid00000001", subs=True, ydl_factory=factory(info))
    assert item["subtitles_status"] == "ok" and item["subtitles_kind"] == "automatic"
    assert item["subtitles_lang"] == "en-orig"


def test_sub_lang_is_configurable():
    info = dict(VIDEO, subtitles=dict(track("en"), **track("fr-FR", TRACK_URL + "&lang=fr")))
    (item,) = youtube.fetch("https://youtu.be/vid00000001", subs=True, sub_lang="fr",
                            ydl_factory=factory(info))
    assert item["subtitles_lang"] == "fr-FR" and FakeYDL.instances[0].fetched == [TRACK_URL + "&lang=fr"]


def test_no_subtitles_available_is_said():
    (item,) = youtube.fetch("https://youtu.be/vid00000001", subs=True, ydl_factory=factory(VIDEO))
    assert item["subtitles"] == "" and item["subtitles_status"] == "none_available"
    assert FakeYDL.instances[0].fetched == []


@pytest.mark.parametrize("vtt,status", [
    (OSError("HTTP Error 429"), "failed: OSError"),
    ("WEBVTT\n\n", "failed: the track was empty"),
])
def test_subtitle_failures_are_said(vtt, status):
    info = dict(VIDEO, subtitles=track("en"))
    (item,) = youtube.fetch("https://youtu.be/vid00000001", subs=True, ydl_factory=factory(info, vtt=vtt))
    assert item["subtitles"] == "" and item["subtitles_status"] == status


def test_subtitle_track_off_youtube_is_refused():
    info = dict(VIDEO, subtitles=track("en", "https://evil.test/timedtext?x=1"))
    (item,) = youtube.fetch("https://youtu.be/vid00000001", subs=True, ydl_factory=factory(info))
    assert item["subtitles_status"].startswith("failed:") and FakeYDL.instances[0].fetched == []


@pytest.mark.parametrize("raw,iso", [("20251018", "2025-10-18"), (None, None), ("2025-10-18", "2025-10-18")])
def test_iso_date(raw, iso):
    assert youtube.iso_date(raw) == iso


def test_playlist_target():
    assert youtube.classify_target("https://www.youtube.com/playlist?list=PLsynthetic") == "playlist"
    assert youtube.classify_target("https://www.youtube.com/watch?v=abc&list=PLsynthetic") == "video"
    pl = {"_type": "playlist", "id": "PLsynthetic", "entries": [dict(VIDEO, id=f"vid0000000{i}") for i in (1, 2)]}
    items = youtube.fetch("https://www.youtube.com/playlist?list=PLsynthetic", ydl_factory=factory(pl))
    assert [i["page_type"] for i in items] == ["playlist", "playlist"]
    opts = FakeYDL.instances[0].opts
    assert opts["playlistend"] == youtube.MAX_SEARCH and opts["extract_flat"] == "in_playlist"
    youtube.fetch("https://www.youtube.com/watch?v=vid00000001&list=PLsynthetic", ydl_factory=factory(VIDEO))
    assert FakeYDL.instances[0].opts["noplaylist"] is True


def test_comments_best_effort():
    (item,) = youtube.fetch("https://youtu.be/vid00000001", comments=True, ydl_factory=factory(VIDEO))
    assert item["comments"] == [{"id": "c1", "parent": "root", "author": "@someone",
                                 "text": "Nice", "like_count": 2, "timestamp": 1}]
    (item,) = youtube.fetch("https://youtu.be/vid00000001", comments=True,
                            ydl_factory=factory(VIDEO, fail_comments=True))
    assert "comments" not in item and item["title"] == "Synthetic video"
    assert len(FakeYDL.instances) == 2


def test_search_flat_and_delta(store):
    search = {"_type": "playlist", "id": "synthetic", "entries": [
        dict(VIDEO, id=f"vid0000000{i}", url=f"https://www.youtube.com/watch?v=vid0000000{i}") for i in range(1, 4)]}
    first = youtube.run_youtube(store, "ytsearch3:synthetic", ydl_factory=factory(search))
    assert [i["item_id"] for i in first] == ["vid00000001", "vid00000002", "vid00000003"]
    assert FakeYDL.instances[0].opts["extract_flat"] == "in_playlist"
    assert youtube.run_youtube(store, "ytsearch3:synthetic", ydl_factory=factory(search)) == []


def test_vtt_to_text():
    assert youtube.vtt_to_text(VTT) == "Hello there\nGeneral synthetic"


# Fix 40: entities, retries, transport noise

def test_transcript_entities_and_whitespace():
    vtt = ("WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nTom&nbsp;&amp;&nbsp;Jerry   &#39;hi&#39;\n\n"
           "00:00:02.000 --> 00:00:04.000\n<c>a&lt;b</c>\t  end\n")
    assert youtube.vtt_to_text(vtt) == "Tom & Jerry 'hi'\na<b end"


class HTTPError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP Error {status}")
        self.status = status


class Flaky(FakeYDL):
    """Answers the subtitle URL with the given errors first, then the VTT."""
    errors: list = []

    def urlopen(self, url):
        self.fetched.append(url)
        if Flaky.errors:
            raise Flaky.errors.pop(0)
        return FakeResponse(self.vtt.encode())


def flaky_fetch(monkeypatch, errors):
    waits = []
    monkeypatch.setattr(youtube, "_sleep", waits.append)
    Flaky.errors = list(errors)
    info = dict(VIDEO, subtitles=track("en"))
    (item,) = youtube.fetch("https://youtu.be/vid00000001", subs=True,
                            ydl_factory=lambda opts: Flaky(opts, info))
    return item, waits


@pytest.mark.parametrize("errors,waits", [([HTTPError(429)], [2.0]), ([HTTPError(503), HTTPError(429)], [2.0, 6.0])])
def test_subtitle_retry_then_success(monkeypatch, errors, waits):
    item, slept = flaky_fetch(monkeypatch, errors)
    assert item["subtitles_status"] == "ok" and "Hello there" in item["subtitles"]
    assert slept == waits


def test_subtitle_retry_then_failed(monkeypatch):
    item, slept = flaky_fetch(monkeypatch, [HTTPError(429)] * 3)
    assert item["subtitles_status"] == "failed: HTTPError 429" and slept == [2.0, 6.0]


def test_subtitle_404_is_not_retried(monkeypatch):
    item, slept = flaky_fetch(monkeypatch, [HTTPError(404)])
    assert item["subtitles_status"] == "failed: HTTPError 404" and slept == []


def test_transport_noise_is_debug_only(caplog):
    import logging

    from agent_surf.browser import TRANSPORT_NOISE, quiet_transport_noise

    quiet_transport_noise()
    quiet_transport_noise()   # installed once
    aio = logging.getLogger("asyncio")
    assert sum(type(f).__name__ == "_TransportNoise" for f in aio.filters) == 1
    with caplog.at_level(logging.INFO):
        aio.warning(TRANSPORT_NOISE)
        aio.warning(TRANSPORT_NOISE)
        aio.error("Task exception was never retrieved")   # a real error still shows
    assert [r.getMessage() for r in caplog.records] == ["Task exception was never retrieved"]
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="asyncio"):
        aio.warning(TRANSPORT_NOISE)
    assert [(r.levelname, r.getMessage()) for r in caplog.records] == [("DEBUG", TRANSPORT_NOISE)]
