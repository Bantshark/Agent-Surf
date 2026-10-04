"""YouTube via the yt-dlp Python API. No site map, no browser.

Metadata, subtitles (written to a temp dir, returned as text), comments
(best-effort) and ``ytsearchN:`` queries. Only ids not seen before are
returned, using the same ``seen`` table as the runner.

Deliberately never set: exec / exec_before_download, external downloaders
(aria2c etc.), the write*link family, netrc_cmd, postprocessors.
"""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from agent_surf.store import Store

log = logging.getLogger("agent_surf.youtube")

SITE = "youtube"
DOMAINS = ("youtube.com", "youtu.be")
SEARCH_RE = re.compile(r"^ytsearch(\d*):(.+)$", re.S)
MAX_SEARCH = 1000
MAX_COMMENTS = 100
FORBIDDEN_OPTS = frozenset({
    "exec", "exec_before_download", "external_downloader", "external_downloader_args",
    "writelink", "writeurllink", "writewebloclink", "writedesktoplink", "netrc_cmd",
    "postprocessors",
})


class YouTubeError(RuntimeError):
    pass


class _Logger:
    """Route yt-dlp output to logging so stdout stays clean for --json."""

    def debug(self, msg: str) -> None:
        log.debug(msg)

    def info(self, msg: str) -> None:
        log.debug(msg)

    def warning(self, msg: str) -> None:
        log.warning(msg)

    def error(self, msg: str) -> None:
        log.error(msg)


def classify_target(target: str) -> str:
    """'search' for ytsearchN:query, 'video' for a youtube URL; else raise."""
    target = target.strip()
    m = SEARCH_RE.match(target)
    if m:
        n = int(m.group(1) or 1)
        if not 1 <= n <= MAX_SEARCH:
            raise YouTubeError(f"ytsearchN: N must be 1..{MAX_SEARCH}")
        if not m.group(2).strip():
            raise YouTubeError("empty search query")
        return "search"
    parts = urlsplit(target)
    host = (parts.hostname or "").lower()
    if parts.scheme == "https" and any(host == d or host.endswith("." + d) for d in DOMAINS):
        return "video"
    raise YouTubeError("target must be an https YouTube URL or ytsearchN:query")


def build_opts(*, subs: bool = False, comments: bool = False, flat: bool = False,
               outdir: Path | None = None) -> dict:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "js_runtimes": {"node": {}},
        "logger": _Logger(),
    }
    if flat:
        opts["extract_flat"] = "in_playlist"
    if subs:
        if outdir is None:
            raise ValueError("subtitles need an output directory")
        opts.update(writesubtitles=True, writeautomaticsub=True, subtitlesformat="vtt",
                    outtmpl=str(outdir / "%(id)s.%(ext)s"))
    if comments:
        opts.update(getcomments=True,
                    extractor_args={"youtube": {"max_comments": [str(MAX_COMMENTS)]}})
    assert not FORBIDDEN_OPTS & opts.keys()
    return opts


_VTT_TIMING = re.compile(r"-->")
_VTT_TAG = re.compile(r"<[^>]+>")


def vtt_to_text(vtt: str) -> str:
    lines: list[str] = []
    for raw in vtt.splitlines():
        line = raw.strip()
        if (not line or line == "WEBVTT" or _VTT_TIMING.search(line) or line.isdigit()
                or line.startswith(("Kind:", "Language:", "NOTE", "STYLE"))):
            continue
        line = _VTT_TAG.sub("", line).strip()
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    return "\n".join(lines)


def _subtitles(info: dict) -> dict[str, str]:
    out = {}
    for lang, sub in (info.get("requested_subtitles") or {}).items():
        path = sub.get("filepath")
        if path and Path(path).exists():
            try:
                out[lang] = vtt_to_text(Path(path).read_text(errors="replace"))
            except OSError as e:
                log.warning("cannot read %s subtitles: %s", lang, type(e).__name__)
    return out


def _comments(info: dict) -> list[dict]:
    return [{k: c.get(k) for k in ("id", "parent", "author", "text", "like_count", "timestamp")}
            for c in info.get("comments") or []]


def to_item(info: dict, page_type: str, *, subs: bool, comments: bool) -> dict:
    item = {
        "site": SITE,
        "page_type": page_type,
        "item_id": str(info.get("id")),
        "title": info.get("title"),
        "url": info.get("webpage_url") or info.get("url"),
        "channel": info.get("channel") or info.get("uploader"),
        "channel_id": info.get("channel_id"),
        "upload_date": info.get("upload_date"),
        "duration": info.get("duration"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "description": info.get("description"),
    }
    if subs:
        item["subtitles"] = _subtitles(info)
    if comments:
        item["comments"] = _comments(info)
    return item


def _extract(target: str, opts: dict, ydl_factory: Callable[[dict], Any]) -> dict:
    with ydl_factory(opts) as ydl:
        info = ydl.extract_info(target, download=bool(opts.get("writesubtitles")))
        return ydl.sanitize_info(info)


def fetch(target: str, *, subs: bool = False, comments: bool = False,
          ydl_factory: Callable[[dict], Any] | None = None) -> list[dict]:
    """All videos for a target, as JSON-serialisable dicts (no delta)."""
    if ydl_factory is None:
        from yt_dlp import YoutubeDL as ydl_factory
    page_type = classify_target(target)
    target = target.strip()
    flat = page_type == "search" and not (subs or comments)
    with tempfile.TemporaryDirectory(prefix="agent-surf-yt-") as tmp:
        opts = build_opts(subs=subs, comments=comments, flat=flat, outdir=Path(tmp))
        try:
            info = _extract(target, opts, ydl_factory)
        except Exception as e:
            if not comments:
                raise YouTubeError(f"yt-dlp failed: {e}") from e
            log.warning("comments failed (%s); retrying without comments", type(e).__name__)
            comments = False
            opts = build_opts(subs=subs, flat=flat, outdir=Path(tmp))
            try:
                info = _extract(target, opts, ydl_factory)
            except Exception as e2:
                raise YouTubeError(f"yt-dlp failed: {e2}") from e2
        entries = info.get("entries") if info.get("_type") in ("playlist", "multi_video") else [info]
        return [to_item(e, page_type, subs=subs, comments=comments)
                for e in entries or [] if e and e.get("id")]


def run_youtube(store: Store, target: str, *, subs: bool = False, comments: bool = False,
                ydl_factory: Callable[[dict], Any] | None = None) -> list[dict]:
    """Like ``fetch`` but returns only videos not seen before."""
    new = []
    for item in fetch(target, subs=subs, comments=comments, ydl_factory=ydl_factory):
        if store.add_new_item(SITE, item["page_type"], item["item_id"], item):
            new.append(item)
    return new
