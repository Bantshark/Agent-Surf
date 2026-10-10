"""v2 publishing queue (SQLite table ``queue``).

Every publish comes from an approved queue item. Approval freezes the content:
``content_hash`` is the sha256 of the canonical payload plus the bytes of every
media file, and publishing refuses if it no longer matches. Status changes go
through ``transition`` and illegal ones raise.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_surf import sites
from agent_surf.store import Store, now_iso

ACTIONS = ("post", "reply", "dm", "comment")
STATUSES = ("draft", "approved", "publishing", "published", "failed", "needs_attention", "rejected")
MISSED_POLICIES = ("skip", "run", "ask")
PAYLOAD_KEYS = ("text", "media", "target_url", "thread_url")
# What each action needs in its payload, beyond text and/or media.
ACTION_NEEDS = {"post": (), "reply": ("target_url",), "comment": ("target_url",), "dm": ("thread_url",)}

TRANSITIONS = {
    "draft": {"approved", "rejected"},
    "approved": {"publishing", "rejected", "failed", "needs_attention"},
    "publishing": {"published", "failed", "needs_attention"},
    "needs_attention": {"approved", "rejected"},   # re-approval is a human decision
    "failed": {"approved", "rejected"},
    "published": set(),
    "rejected": set(),
}
APPROVABLE = {"draft", "needs_attention", "failed"}


class QueueError(ValueError):
    pass


class IllegalTransition(QueueError):
    pass


def parse_time(value: str) -> str:
    """ISO 8601 -> UTC ISO string. A time without an offset is local time."""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise QueueError(f"not an ISO 8601 time: {value!r}") from None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


# Fix 12: what may be attached. Extension AND magic bytes must agree.
MAX_MEDIA = 4
IMAGE_MAX_BYTES = 15 * 1024 * 1024
VIDEO_MAX_BYTES = 512 * 1024 * 1024
MEDIA_TYPES = {  # extension -> (mime type, magic kind)
    ".jpg": ("image/jpeg", "jpeg"), ".jpeg": ("image/jpeg", "jpeg"), ".png": ("image/png", "png"),
    ".gif": ("image/gif", "gif"), ".webp": ("image/webp", "webp"),
    ".mp4": ("video/mp4", "isobmff"), ".mov": ("video/quicktime", "isobmff"), ".webm": ("video/webm", "ebml"),
}


def sniff(head: bytes) -> str | None:
    """Kind of media from its first bytes, or None."""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp":
        return "isobmff"   # MP4 / MOV
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "ebml"      # WebM
    return None


def check_media(paths: list[str]) -> list[dict]:
    """Every media file must be a regular file (not a symlink) of an allowed type
    whose extension matches its content, within the size cap; at most MAX_MEDIA.
    Returns path, type, size and sha256 per file."""
    if len(paths) > MAX_MEDIA:
        raise QueueError(f"at most {MAX_MEDIA} media files per item (got {len(paths)})")
    info = []
    for p in paths:
        try:
            st = os.lstat(p)
        except OSError:
            raise QueueError(f"media file not found: {p}") from None
        if stat.S_ISLNK(st.st_mode):
            raise QueueError(f"media may not be a symlink: {p}")
        if not stat.S_ISREG(st.st_mode):
            raise QueueError(f"media is not a regular file: {p}")
        ext = Path(p).suffix.lower()
        if ext not in MEDIA_TYPES:
            raise QueueError(f"unsupported media type {ext or '(none)'}: {p} "
                             f"(allowed: {', '.join(sorted(MEDIA_TYPES))})")
        mime, kind = MEDIA_TYPES[ext]
        h = hashlib.sha256()
        with open(p, "rb") as f:
            head = f.read(32)
            h.update(head)
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        if sniff(head) != kind:
            raise QueueError(f"media content does not match its extension {ext}: {p}")
        cap = VIDEO_MAX_BYTES if mime.startswith("video/") else IMAGE_MAX_BYTES
        if st.st_size > cap:
            raise QueueError(f"media too large ({st.st_size} bytes, max {cap}): {p}")
        info.append({"path": p, "type": mime, "size": st.st_size, "sha256": h.hexdigest()})
    return info


def normalize_payload(site: str, action: str, payload: dict) -> dict:
    if action not in ACTIONS:
        raise QueueError(f"unknown action {action!r}; use {', '.join(ACTIONS)}")
    site_obj = sites.get_site(site)
    unknown = set(payload) - set(PAYLOAD_KEYS)
    if unknown:
        raise QueueError(f"unknown payload keys: {', '.join(sorted(unknown))}")
    out: dict[str, Any] = {}
    text = payload.get("text")
    if text is not None:
        if not isinstance(text, str) or not text.strip():
            raise QueueError("text must be a non-empty string")
        out["text"] = text
    media = payload.get("media") or []
    if not isinstance(media, list) or not all(isinstance(m, str) and m for m in media):
        raise QueueError("media must be a list of file paths")
    if media:
        # abspath, not resolve(): a symlink must stay visible to check_media
        out["media"] = [os.path.abspath(Path(m).expanduser()) for m in media]
        check_media(out["media"])
    if not out:
        raise QueueError("a queue item needs text and/or media")
    for key in ("target_url", "thread_url"):
        if payload.get(key):
            sites.check_url(site_obj, payload[key])
            out[key] = payload[key]
    for key in ACTION_NEEDS[action]:
        if key not in out:
            raise QueueError(f"{action} needs --{key.split('_')[0]}")
    return out


def content_hash(payload: dict, media_paths: list[str] | None = None) -> str:
    """sha256 of the canonical payload plus the bytes of each media file: the
    approved snapshots (Fix 13) when given, else the payload's own paths."""
    h = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                  ensure_ascii=False).encode())
    for path in (payload.get("media") or []) if media_paths is None else media_paths:
        h.update(b"\0")
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
    return "sha256-" + h.hexdigest()


def _row(store: Store, item_id: int):
    row = store.conn.execute("SELECT * FROM queue WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise QueueError(f"no queue item {item_id}")
    return row


def to_dict(row: Any) -> dict:
    d = dict(row)
    d["payload"] = json.loads(d.pop("payload_json"))
    d["receipt"] = json.loads(d.pop("receipt_json")) if d.get("receipt_json") else None
    snap = d.pop("media_snapshot_json", None)
    d["media_snapshot"] = json.loads(snap) if snap else None
    return d


# ---------------------------------------------------------------------------
# Fix 13: media snapshots. Approval copies the media into
# <AGENT_SURF_HOME>/media/<id>/; the hash covers those bytes and publish uploads
# only them, so editing the originals afterwards changes nothing.

def snapshot_dir(store: Store, item_id: int) -> Path:
    return store.media_dir / str(item_id)


def remove_snapshot(store: Store, item_id: int) -> None:
    shutil.rmtree(snapshot_dir(store, item_id), ignore_errors=True)
    with store.conn:
        store.conn.execute("UPDATE queue SET media_snapshot_json = NULL WHERE id = ?", (item_id,))


def take_snapshot(store: Store, item_id: int, paths: list[str]) -> list[str]:
    folder = snapshot_dir(store, item_id)
    shutil.rmtree(folder, ignore_errors=True)       # re-approval re-snapshots
    folder.mkdir(parents=True)
    if os.name == "posix":
        os.chmod(store.media_dir, 0o700)
        os.chmod(folder, 0o700)
    out = []
    for n, src in enumerate(paths):
        dst = folder / f"{n}{Path(src).suffix.lower()}"
        shutil.copyfile(src, dst)
        if os.name == "posix":
            os.chmod(dst, 0o600)
        out.append(str(dst))
    return out


def publish_payload(item: dict) -> dict:
    """The approved payload as it is published: media replaced by the approved
    snapshots (items approved before snapshots existed keep their own paths)."""
    payload = dict(item["payload"])
    if payload.get("media") and item.get("media_snapshot"):
        payload["media"] = list(item["media_snapshot"])
    return payload


def get(store: Store, item_id: int) -> dict:
    return to_dict(_row(store, item_id))


def list_items(store: Store, status: str | None = None) -> list[dict]:
    if status is not None and status not in STATUSES:
        raise QueueError(f"unknown status {status!r}")
    rows = store.conn.execute(
        "SELECT * FROM queue" + (" WHERE status = ?" if status else "") + " ORDER BY id",
        (status,) if status else ()).fetchall()
    return [to_dict(r) for r in rows]


def add(store: Store, site: str, action: str, payload: dict, *, scheduled_at: str | None = None,
        missed_policy: str = "ask") -> int:
    if missed_policy not in MISSED_POLICIES:
        raise QueueError(f"missed policy must be one of {', '.join(MISSED_POLICIES)}")
    clean = normalize_payload(site, action, payload)
    when = parse_time(scheduled_at) if scheduled_at else None
    now = now_iso()
    with store.conn:
        cur = store.conn.execute(
            "INSERT INTO queue (site, action, payload_json, status, scheduled_at, missed_policy,"
            " created_at, updated_at) VALUES (?, ?, ?, 'draft', ?, ?, ?, ?)",
            (site, action, json.dumps(clean, ensure_ascii=False), when, missed_policy, now, now))
    return cur.lastrowid


def transition(store: Store, item_id: int, new_status: str, *, last_error: str | None = None,
               receipt: dict | None = None, content_hash_value: str | None = None,
               count_attempt: bool = False) -> dict:
    row = _row(store, item_id)
    old = row["status"]
    if new_status not in STATUSES:
        raise IllegalTransition(f"unknown status {new_status!r}")
    if new_status not in TRANSITIONS[old]:
        raise IllegalTransition(f"queue item {item_id}: {old} -> {new_status} is not allowed")
    now = now_iso()
    sets = ["status = ?", "updated_at = ?", "last_error = ?"]
    args: list[Any] = [new_status, now, last_error]
    if new_status == "approved":
        sets += ["approved_at = ?", "content_hash = ?"]
        args += [now, content_hash_value]
    if receipt is not None:
        sets.append("receipt_json = ?")
        args.append(json.dumps(receipt, ensure_ascii=False))
    if count_attempt:
        sets.append("attempts = attempts + 1")
    with store.conn:
        cur = store.conn.execute(
            f"UPDATE queue SET {', '.join(sets)} WHERE id = ? AND status = ?", (*args, item_id, old))
    if cur.rowcount != 1:  # someone else moved it first
        raise IllegalTransition(f"queue item {item_id} changed status concurrently")
    if new_status in ("published", "rejected"):
        remove_snapshot(store, item_id)  # receipt recorded, or never to be published
    return get(store, item_id)


def approve(store: Store, item_id: int) -> dict:
    item = get(store, item_id)
    if item["status"] not in APPROVABLE:
        raise IllegalTransition(f"queue item {item_id} is {item['status']}; only "
                                f"{', '.join(sorted(APPROVABLE))} items can be approved")
    media = item["payload"].get("media") or []
    check_media(media)
    snaps = take_snapshot(store, item_id, media) if media else []
    check_media(snaps)
    with store.conn:
        store.conn.execute("UPDATE queue SET media_snapshot_json = ? WHERE id = ?",
                           (json.dumps(snaps) if snaps else None, item_id))
    return transition(store, item_id, "approved",
                      content_hash_value=content_hash(item["payload"], snaps if media else None))


def approve_all_drafts(store: Store) -> list[dict]:
    return [approve(store, item["id"]) for item in list_items(store, "draft")]


def reject(store: Store, item_id: int) -> dict:
    return transition(store, item_id, "rejected")


def verify_approved(item: dict) -> str | None:
    """None if the item may be published as approved, else the reason it may not."""
    if item["status"] != "approved":
        return f"status is {item['status']}, not approved"
    media = publish_payload(item).get("media") or []   # the snapshots (or legacy originals)
    try:
        check_media(media)
        current = content_hash(item["payload"], media if item["payload"].get("media") else None)
    except (QueueError, OSError) as e:
        return f"media changed since approval: {e}"
    if current != item["content_hash"]:
        return "content changed since approval (content_hash mismatch)"
    return None
