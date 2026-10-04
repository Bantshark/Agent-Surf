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


class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL; never touches the network."""
    instances = []

    def __init__(self, opts, info=None, fail_comments=False):
        self.opts, self.info, self.fail_comments = opts, info, fail_comments
        self.calls = []
        FakeYDL.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def extract_info(self, target, download):
        self.calls.append((target, download))
        if self.fail_comments and self.opts.get("getcomments"):
            raise RuntimeError("comments endpoint changed")
        info = json.loads(json.dumps(self.info))
        if self.opts.get("writesubtitles"):
            for e in info.get("entries", [info]):
                path = Path(self.opts["outtmpl"].replace("%(id)s", e["id"]).replace("%(ext)s", "en.vtt"))
                path.write_text(VTT)
                e["requested_subtitles"] = {"en": {"ext": "vtt", "filepath": str(path)}}
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
            opts = youtube.build_opts(subs=subs, comments=comments, outdir=tmp_path)
            assert not youtube.FORBIDDEN_OPTS & opts.keys()
            assert opts["skip_download"] is True
            assert opts["js_runtimes"] == {"node": {}}
            assert opts["ignore_no_formats_error"] is True
            assert opts["extractor_args"]["youtube"]["player_skip"] == ["js"]
            assert ("max_comments" in opts["extractor_args"]["youtube"]) == comments


def test_real_youtubedl_accepts_opts(tmp_path):
    from yt_dlp import YoutubeDL
    with YoutubeDL(youtube.build_opts(subs=True, comments=True, outdir=tmp_path)) as ydl:
        assert ydl.params["writesubtitles"] and ydl.params["getcomments"]
        assert not ydl.params["remote_components"]


def test_metadata_is_json_serialisable():
    (item,) = youtube.fetch("https://www.youtube.com/watch?v=vid00000001", ydl_factory=factory(VIDEO))
    json.dumps(item)
    assert item["item_id"] == "vid00000001" and item["page_type"] == "video"
    assert "formats" not in item and "subtitles" not in item
    assert FakeYDL.instances[0].calls == [("https://www.youtube.com/watch?v=vid00000001", False)]


def test_subtitles_as_text():
    (item,) = youtube.fetch("https://youtu.be/vid00000001", subs=True, ydl_factory=factory(VIDEO))
    assert item["subtitles"] == {"en": "Hello there\nGeneral synthetic"}
    assert FakeYDL.instances[0].calls[0][1] is True  # download=True writes subtitle files only


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
