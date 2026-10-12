"""YouTube via the yt-dlp Python API. No site map, no browser.

Metadata, subtitles (returned as plain text), comments (best-effort),
``ytsearchN:`` queries and playlists. Only ids not seen before are returned,
using the same ``seen`` table as the runner.

Media is never downloaded (``skip_download``). For metadata and comments the
YouTube player JavaScript is skipped (``player_skip: js``) and "no formats" is
not an error. Fix 32: caption tracks are only resolved with the player, so a
``--subs`` call runs yt-dlp's normal extraction with the configured Node
runtime (``js_runtimes: node``), then fetches the chosen track (manual
subtitles in the requested language, else automatic captions) as VTT through
yt-dlp's own opener and returns plain text with ``subtitles_status``:
"ok" | "none_available" | "failed: <reason>", so an empty transcript is never
silent. Video targets never expand a playlist (``noplaylist``); searches and
playlists stop at ``playlistend``.

Deliberately never set: exec / exec_before_download, external downloaders
(aria2c etc.), the write*link family, netrc_cmd, postprocessors,
remote_components.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from agent_surf.store import Store

log = logging.getLogger("agent_surf.youtube")

SITE = "youtube"
DOMAINS = ("youtube.com", "youtu.be")
SEARCH_RE = re.compile(r"^ytsearch(\d*):(.+)$", re.S)
DEFAULT_SUB_LANG = "en"
MAX_SEARCH = 1000
MAX_COMMENTS = 100
FORBIDDEN_OPTS = frozenset({
    "exec", "exec_before_download", "external_downloader", "external_downloader_args",
    "writelink", "writeurllink", "writewebloclink", "writedesktoplink", "netrc_cmd",
    "postprocessors", "remote_components",
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


def _is_youtube_host(host: str) -> bool:
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in DOMAINS)


def classify_target(target: str) -> str:
    """'search' for ytsearchN:query, 'playlist' for a youtube.com/playlist URL,
    'video' for any other youtube URL; else raise."""
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
    if parts.scheme == "https" and _is_youtube_host(parts.hostname or ""):
        return "playlist" if parts.path.rstrip("/") == "/playlist" else "video"
    raise YouTubeError("target must be an https YouTube URL or ytsearchN:query")


def check_opts(opts: dict) -> None:
    """Fix 24: an explicit check (not ``assert``, which ``python -O`` removes):
    no option that runs commands, writes link files or post-processes."""
    bad = FORBIDDEN_OPTS & opts.keys()
    if bad:
        raise YouTubeError(f"refusing yt-dlp options: {', '.join(sorted(bad))}")


def build_opts(*, subs: bool = False, comments: bool = False, flat: bool = False,
               outdir: Path | None = None, kind: str = "video") -> dict:
    """``kind``: "video" never expands a playlist; "search" and "playlist" stop
    at MAX_SEARCH entries. ``subs``: the player JS runs (Node) so caption tracks
    resolve; otherwise it is skipped. ``outdir`` is unused (kept for callers)."""
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "ignore_no_formats_error": True,
        "js_runtimes": {"node": {}},
        "logger": _Logger(),
    }
    extractor_args: dict[str, list[str]] = {} if subs else {"player_skip": ["js"]}
    if kind == "video":
        opts["noplaylist"] = True
    else:
        opts["playlistend"] = MAX_SEARCH
    if flat:
        opts["extract_flat"] = "in_playlist"
    if comments:
        opts["getcomments"] = True
        extractor_args["max_comments"] = [str(MAX_COMMENTS)]
    opts["extractor_args"] = {"youtube": extractor_args}
    check_opts(opts)
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


def _lang_keys(tracks: dict, lang: str) -> list[str]:
    """Track keys for a language: exact, "<lang>-orig", then "<lang>-*" regional ones."""
    lang = lang.lower()
    keys = [k for k in tracks if k.lower() == lang]
    keys += [k for k in tracks if k.lower() == f"{lang}-orig"]
    keys += sorted(k for k in tracks if k.lower().startswith(lang + "-") and k not in keys)
    return keys


def pick_track(info: dict, lang: str = DEFAULT_SUB_LANG) -> tuple[str, str, str] | None:
    """(kind, track language, VTT url): manual subtitles in ``lang`` first,
    else automatic captions in ``lang``; None if neither has a VTT track."""
    for kind, key in (("manual", "subtitles"), ("automatic", "automatic_captions")):
        tracks = {k: v for k, v in (info.get(key) or {}).items() if k != "live_chat"}
        for k in _lang_keys(tracks, lang):
            for fmt in tracks[k] or []:
                if fmt.get("ext") == "vtt" and fmt.get("url"):
                    return kind, k, fmt["url"]
    return None


def fetch_subtitles(ydl: Any, info: dict, lang: str = DEFAULT_SUB_LANG) -> dict:
    """``subtitles`` (plain text), ``subtitles_lang``, ``subtitles_kind`` and
    ``subtitles_status``: "ok", "none_available" or "failed: <reason>"."""
    out: dict[str, Any] = {"subtitles": "", "subtitles_lang": None, "subtitles_kind": None}
    track = pick_track(info, lang)
    if track is None:
        return dict(out, subtitles_status="none_available")
    kind, track_lang, url = track
    out.update(subtitles_lang=track_lang, subtitles_kind=kind)
    if urlsplit(url).scheme != "https" or not _is_youtube_host(urlsplit(url).hostname or ""):
        return dict(out, subtitles_status="failed: subtitle track is not on youtube.com")
    try:
        with ydl.urlopen(url) as resp:
            vtt = resp.read().decode("utf-8", "replace")
    except Exception as e:
        log.warning("subtitles for %s failed: %s", info.get("id"), type(e).__name__)
        return dict(out, subtitles_status=f"failed: {type(e).__name__}")
    text = vtt_to_text(vtt)
    if not text:
        return dict(out, subtitles_status="failed: the track was empty")
    return dict(out, subtitles=text, subtitles_status="ok")


def iso_date(value: Any) -> str | None:
    """yt-dlp's YYYYMMDD upload_date as YYYY-MM-DD (other values unchanged)."""
    if isinstance(value, str) and re.fullmatch(r"\d{8}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value


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
        "upload_date": iso_date(info.get("upload_date")),
        "duration": info.get("duration"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "description": info.get("description"),
    }
    if subs:
        item.update(info.get("_agent_surf_subtitles") or {"subtitles": "", "subtitles_lang": None,
                                                          "subtitles_kind": None,
                                                          "subtitles_status": "failed: not fetched"})
    if comments:
        item["comments"] = _comments(info)
    return item


def _entries(info: dict) -> list[dict]:
    entries = info.get("entries") if info.get("_type") in ("playlist", "multi_video") else [info]
    return [e for e in entries or [] if e and e.get("id")]


def _extract(target: str, opts: dict, ydl_factory: Callable[[dict], Any], *,
             subs: bool = False, sub_lang: str = DEFAULT_SUB_LANG) -> dict:
    check_opts(opts)
    with ydl_factory(opts) as ydl:
        info = ydl.sanitize_info(ydl.extract_info(target, download=False))
        if subs:
            for e in _entries(info):
                e["_agent_surf_subtitles"] = fetch_subtitles(ydl, e, sub_lang)
        return info


def fetch(target: str, *, subs: bool = False, comments: bool = False, sub_lang: str = DEFAULT_SUB_LANG,
          ydl_factory: Callable[[dict], Any] | None = None) -> list[dict]:
    """All videos for a target, as JSON-serialisable dicts (no delta)."""
    if ydl_factory is None:
        from yt_dlp import YoutubeDL as ydl_factory
    page_type = classify_target(target)
    target = target.strip()
    flat = page_type in ("search", "playlist") and not (subs or comments)
    opts = build_opts(subs=subs, comments=comments, flat=flat, kind=page_type)
    try:
        info = _extract(target, opts, ydl_factory, subs=subs, sub_lang=sub_lang)
    except Exception as e:
        if not comments:
            raise YouTubeError(f"yt-dlp failed: {e}") from e
        log.warning("comments failed (%s); retrying without comments", type(e).__name__)
        comments = False
        opts = build_opts(subs=subs, flat=flat, kind=page_type)
        try:
            info = _extract(target, opts, ydl_factory, subs=subs, sub_lang=sub_lang)
        except Exception as e2:
            raise YouTubeError(f"yt-dlp failed: {e2}") from e2
    return [to_item(e, page_type, subs=subs, comments=comments) for e in _entries(info)]


def run_youtube(store: Store, target: str, *, subs: bool = False, comments: bool = False,
                sub_lang: str = DEFAULT_SUB_LANG,
                ydl_factory: Callable[[dict], Any] | None = None) -> list[dict]:
    """Like ``fetch`` but returns only videos not seen before."""
    new = []
    for item in fetch(target, subs=subs, comments=comments, sub_lang=sub_lang, ydl_factory=ydl_factory):
        if store.add_new_item(SITE, item["page_type"], item["item_id"], item):
            new.append(item)
    return new
