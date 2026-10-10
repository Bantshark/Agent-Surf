"""Local data retention (Fix 14): purge stored reading items and debug folders.

Never touches maps, the queue, receipts or seen ids (deleting seen ids would
make old items come back as new).
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_surf.store import Store

ITEMS_DAYS = 90
DEBUG_DAYS = 14
DEBUG_KEEP = 20


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def purge_items(store: Store, older_than_days: int = ITEMS_DAYS, *, now: datetime | None = None,
                dry_run: bool = False) -> int:
    """Delete stored reading items (items table) captured before the cutoff."""
    cutoff = (_now(now) - timedelta(days=older_than_days)).isoformat(timespec="seconds")
    (count,) = store.conn.execute("SELECT COUNT(*) FROM items WHERE captured_at < ?", (cutoff,)).fetchone()
    if not dry_run and count:
        with store.conn:
            store.conn.execute("DELETE FROM items WHERE captured_at < ?", (cutoff,))
    return count


def _debug_folders(debug_root: Path) -> list[Path]:
    if not debug_root.is_dir():
        return []
    folders = [p for p in debug_root.iterdir() if p.is_dir() and not p.is_symlink()]
    return sorted(folders, key=lambda p: (p.stat().st_mtime, p.name))


def purge_debug(debug_root: Path, older_than_days: int = DEBUG_DAYS, *, now: datetime | None = None,
                dry_run: bool = False) -> list[Path]:
    """Delete debug folders last modified before the cutoff."""
    cutoff = (_now(now) - timedelta(days=older_than_days)).timestamp()
    old = [p for p in _debug_folders(Path(debug_root)) if p.stat().st_mtime < cutoff]
    if not dry_run:
        for p in old:
            shutil.rmtree(p, ignore_errors=True)
    return old


def prune_debug(debug_root: Path, keep: int = DEBUG_KEEP) -> list[Path]:
    """Keep only the newest ``keep`` debug folders."""
    folders = _debug_folders(Path(debug_root))
    extra = folders[:-keep] if keep else folders
    for p in extra:
        shutil.rmtree(p, ignore_errors=True)
    return extra
