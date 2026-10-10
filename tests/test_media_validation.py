"""Fix 12: media types by extension AND magic bytes, size caps, no symlinks,
at most 4 per item; checked at add, approve and publish."""

import json
import os

import pytest

from agent_surf import cli, outbox
from agent_surf.outbox import QueueError

MAGIC = {
    ".png": b"\x89PNG\r\n\x1a\n" + b"\0" * 24,
    ".jpg": b"\xff\xd8\xff\xe0" + b"\0" * 28,
    ".jpeg": b"\xff\xd8\xff\xe1" + b"\0" * 28,
    ".gif": b"GIF89a" + b"\0" * 26,
    ".webp": b"RIFF\x24\0\0\0WEBPVP8 " + b"\0" * 16,
    ".mp4": b"\0\0\0\x18ftypmp42" + b"\0" * 20,
    ".mov": b"\0\0\0\x14ftypqt  " + b"\0" * 20,
    ".webm": b"\x1aE\xdf\xa3" + b"\0" * 28,
}


def make(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


@pytest.mark.parametrize("ext", sorted(MAGIC))
def test_valid_media_accepted(store, tmp_path, ext):
    path = make(tmp_path, "f" + ext, MAGIC[ext])
    qid = outbox.add(store, "x", "post", {"media": [path]})
    assert outbox.approve(store, qid)["status"] == "approved"
    (info,) = outbox.check_media([path])
    assert info["size"] == 32 and len(info["sha256"]) == 64 and "/" in info["type"]


@pytest.mark.parametrize("name,data,match", [
    ("notes.png", b"just some text, renamed\n", "does not match its extension"),
    ("photo.jpg", MAGIC[".png"], "does not match its extension"),
    ("clip.mp4", MAGIC[".webm"], "does not match its extension"),
    ("doc.txt", b"text", "unsupported media type .txt"),
    ("logo.svg", b"<svg/>", "unsupported media type .svg"),
    ("noext", MAGIC[".png"], "unsupported media type"),
])
def test_bad_media_rejected(store, tmp_path, name, data, match):
    with pytest.raises(QueueError, match=match):
        outbox.add(store, "x", "post", {"media": [make(tmp_path, name, data)]})


def test_size_caps(store, tmp_path, monkeypatch):
    monkeypatch.setattr(outbox, "IMAGE_MAX_BYTES", 31)
    with pytest.raises(QueueError, match="too large"):
        outbox.add(store, "x", "post", {"media": [make(tmp_path, "a.png", MAGIC[".png"])]})
    outbox.add(store, "x", "post", {"media": [make(tmp_path, "a.mp4", MAGIC[".mp4"])]})  # video cap separate
    monkeypatch.setattr(outbox, "VIDEO_MAX_BYTES", 31)
    with pytest.raises(QueueError, match="too large"):
        outbox.add(store, "x", "post", {"media": [make(tmp_path, "b.mp4", MAGIC[".mp4"])]})


def test_default_caps():
    assert (outbox.IMAGE_MAX_BYTES, outbox.VIDEO_MAX_BYTES, outbox.MAX_MEDIA) == (15 << 20, 512 << 20, 4)


def test_symlink_rejected(store, tmp_path):
    real = make(tmp_path, "real.png", MAGIC[".png"])
    link = tmp_path / "link.png"
    os.symlink(real, link)
    with pytest.raises(QueueError, match="symlink"):
        outbox.add(store, "x", "post", {"media": [str(link)]})


def test_at_most_four(store, tmp_path):
    files = [make(tmp_path, f"{i}.png", MAGIC[".png"]) for i in range(5)]
    with pytest.raises(QueueError, match="at most 4"):
        outbox.add(store, "x", "post", {"media": files})
    outbox.add(store, "x", "post", {"media": files[:4]})


def test_checked_again_at_approve_and_publish(store, tmp_path):
    path = make(tmp_path, "a.png", MAGIC[".png"])
    qid = outbox.add(store, "x", "post", {"media": [path]})
    open(path, "wb").write(b"no longer an image")
    with pytest.raises(QueueError, match="does not match"):
        outbox.approve(store, qid)
    open(path, "wb").write(MAGIC[".png"])
    item = outbox.approve(store, qid)
    open(item["media_snapshot"][0], "wb").write(b"swapped after approval")
    assert "does not match" in outbox.verify_approved(outbox.get(store, qid))


def test_queue_show_lists_media(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_SURF_HOME", str(tmp_path / "home"))
    png, mp4 = make(tmp_path, "a.png", MAGIC[".png"]), make(tmp_path, "b.mp4", MAGIC[".mp4"])
    assert cli.main(["queue", "add", "x", "post", "--text", "t", "--media", png, mp4, "--json"]) == 0
    qid = json.loads(capsys.readouterr().out)["id"]
    assert cli.main(["queue", "show", str(qid)]) == 0
    out = capsys.readouterr().out
    sha = outbox.check_media([png])[0]["sha256"][:12]
    assert f"media[0]: {png}\timage/png\t32 bytes\tsha256:{sha}" in out
    assert f"media[1]: {mp4}\tvideo/mp4\t32 bytes\tsha256:" in out
    assert cli.main(["queue", "list"]) == 0
    assert "[+2 media]" in capsys.readouterr().out
