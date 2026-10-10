"""SQLite store at <AGENT_SURF_HOME>/surf.db: map versions, seen ids, items."""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS maps (
    site TEXT NOT NULL,
    page_type TEXT NOT NULL,
    version INTEGER NOT NULL,
    path TEXT NOT NULL,
    fingerprint TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (site, page_type, version)
);
CREATE TABLE IF NOT EXISTS seen (
    site TEXT NOT NULL,
    page_type TEXT NOT NULL,
    item_id TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    UNIQUE (site, page_type, item_id)
);
CREATE TABLE IF NOT EXISTS items (
    site TEXT NOT NULL,
    page_type TEXT NOT NULL,
    item_id TEXT NOT NULL,
    data_json TEXT NOT NULL,
    captured_at TEXT NOT NULL
);
-- v2: action maps (see actionmap.py), versioned like reading maps
CREATE TABLE IF NOT EXISTS action_maps (
    site TEXT NOT NULL,
    action TEXT NOT NULL,
    version INTEGER NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (site, action, version)
);
-- v2: one row per publish attempt: dispatch, submit and completion recorded separately
CREATE TABLE IF NOT EXISTS dispatch_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id INTEGER NOT NULL,
    site TEXT NOT NULL,
    action TEXT NOT NULL,
    dispatched_at TEXT NOT NULL,
    submitted_at TEXT,
    completed_at TEXT,
    outcome TEXT
);
CREATE INDEX IF NOT EXISTS dispatch_log_caps ON dispatch_log (site, action, submitted_at);
-- v2: dispatcher state (last tick), so wake/restart detection survives restarts
CREATE TABLE IF NOT EXISTS dispatcher_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Fix 16: the user's own account per site (for unknown-outcome lookups)
CREATE TABLE IF NOT EXISTS accounts (
    site TEXT PRIMARY KEY,
    handle TEXT NOT NULL,
    set_at TEXT NOT NULL
);
-- v2: publishing queue (see outbox.py)
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site TEXT NOT NULL,
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_hash TEXT,
    status TEXT NOT NULL,
    scheduled_at TEXT,
    missed_policy TEXT NOT NULL DEFAULT 'ask',
    approved_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    receipt_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


# Columns added after v1. Existing databases get them via ALTER TABLE on open.
MAPS_ADDED_COLUMNS = {
    "dry_run_items": "INTEGER",          # items the learner's dry run found before scrolling
    "last_fingerprint": "TEXT",          # fingerprint seen on the latest run
    "drift_count": "INTEGER NOT NULL DEFAULT 0",  # consecutive runs whose fingerprint differed
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


QUEUE_ADDED_COLUMNS = {
    "media_snapshot_json": "TEXT",   # Fix 13: approved copies of the media, uploaded at publish
}

# Fix 21: which invocation stored an item, and when its stdout was written and flushed.
ITEMS_ADDED_COLUMNS = {
    "run_id": "TEXT",
    "delivered_at": "TEXT",          # NULL until the CLI printed it; recover with `items --undelivered`
}


def new_run_id() -> str:
    """UTC timestamp plus a short random suffix, e.g. 20261010T120501Z-3fa9c1."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.home: Path | None = Path(path).parent
        else:
            self.home = None
        self._tmp_home: str | None = None
        self.run_id: str | None = None  # stamped on items stored by this invocation (Fix 21)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        for table, columns in (("maps", MAPS_ADDED_COLUMNS), ("queue", QUEUE_ADDED_COLUMNS),
                               ("items", ITEMS_ADDED_COLUMNS)):
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            with self.conn:
                for col, decl in columns.items():
                    if col not in have:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                        if (table, col) == ("items", "delivered_at"):
                            # rows from before Fix 21 were printed by the run that stored them
                            self.conn.execute("UPDATE items SET delivered_at = captured_at")
        with self.conn:
            self.conn.execute("CREATE INDEX IF NOT EXISTS items_run ON items (run_id)")

    @property
    def media_dir(self) -> Path:
        """<AGENT_SURF_HOME>/media (beside surf.db); a temp dir for in-memory stores."""
        if self.home is not None:
            return self.home / "media"
        if self._tmp_home is None:
            import tempfile
            self._tmp_home = tempfile.mkdtemp(prefix="agent-surf-media-")
        return Path(self._tmp_home) / "media"

    def close(self) -> None:
        self.conn.close()
        if self._tmp_home is not None:
            import shutil
            shutil.rmtree(self._tmp_home, ignore_errors=True)

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # maps

    def add_map(self, site: str, page_type: str, version: int, path: str | Path,
                fingerprint: str | None, dry_run_items: int | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO maps (site, page_type, version, path, fingerprint, created_at, dry_run_items)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (site, page_type, version, str(path), fingerprint, now_iso(), dry_run_items),
            )

    def map_row(self, site: str, page_type: str, version: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM maps WHERE site = ? AND page_type = ? AND version = ?",
            (site, page_type, version),
        ).fetchone()

    def update_drift(self, site: str, page_type: str, version: int, *, last_fingerprint: str,
                     drift_count: int, baseline: str | None = None) -> None:
        """Record the latest run's fingerprint and mismatch streak. ``baseline``
        replaces the stored fingerprint (used when the stored one is from an
        older fingerprint scheme)."""
        with self.conn:
            self.conn.execute(
                "UPDATE maps SET last_fingerprint = ?, drift_count = ?,"
                " fingerprint = COALESCE(?, fingerprint)"
                " WHERE site = ? AND page_type = ? AND version = ?",
                (last_fingerprint, drift_count, baseline, site, page_type, version),
            )

    def current_map(self, site: str, page_type: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM maps WHERE site = ? AND page_type = ? ORDER BY version DESC LIMIT 1",
            (site, page_type),
        ).fetchone()

    def next_version(self, site: str, page_type: str) -> int:
        row = self.current_map(site, page_type)
        return 1 if row is None else row["version"] + 1

    def list_maps(self) -> list[sqlite3.Row]:
        """Current (highest) version of every (site, page_type)."""
        return self.conn.execute(
            "SELECT m.* FROM maps m JOIN ("
            "  SELECT site, page_type, MAX(version) AS v FROM maps GROUP BY site, page_type"
            ") cur ON m.site = cur.site AND m.page_type = cur.page_type AND m.version = cur.v"
            " ORDER BY m.site, m.page_type"
        ).fetchall()

    # seen / items

    def is_seen(self, site: str, page_type: str, item_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM seen WHERE site = ? AND page_type = ? AND item_id = ?",
            (site, page_type, item_id),
        ).fetchone() is not None

    def _insert_seen(self, site: str, page_type: str, item_id: str) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO seen (site, page_type, item_id, first_seen) VALUES (?, ?, ?, ?)",
            (site, page_type, item_id, now_iso()),
        )
        return cur.rowcount == 1

    def _insert_item(self, site: str, page_type: str, item_id: str, data: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO items (site, page_type, item_id, data_json, captured_at, run_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (site, page_type, item_id, json.dumps(data, ensure_ascii=False), now_iso(), self.run_id),
        )

    def mark_seen(self, site: str, page_type: str, item_id: str) -> bool:
        """Record an id. Returns True if it was not seen before."""
        with self.conn:
            return self._insert_seen(site, page_type, item_id)

    def record_item(self, site: str, page_type: str, item_id: str, data: dict[str, Any]) -> None:
        with self.conn:
            self._insert_item(site, page_type, item_id, data)

    def add_new_item(self, site: str, page_type: str, item_id: str, data: dict[str, Any]) -> bool:
        """Mark seen and store the item (undelivered, under this run_id) in one
        transaction if it is new. Returns True if new."""
        with self.conn:
            if not self._insert_seen(site, page_type, item_id):
                return False
            self._insert_item(site, page_type, item_id, data)
        return True

    def select_items(self, *, site: str | None = None, page_type: str | None = None,
                     run_id: str | None = None, since: str | None = None,
                     undelivered: bool = False) -> list[sqlite3.Row]:
        """Stored items, oldest first. ``since`` is a UTC ISO timestamp (seconds)."""
        where, params = [], []
        for col, val in (("site", site), ("page_type", page_type), ("run_id", run_id)):
            if val is not None:
                where.append(f"{col} = ?")
                params.append(val)
        if since is not None:
            where.append("captured_at >= ?")
            params.append(since)
        if undelivered:
            where.append("delivered_at IS NULL")
        sql = "SELECT rowid, * FROM items" + (" WHERE " + " AND ".join(where) if where else "")
        return self.conn.execute(sql + " ORDER BY rowid", params).fetchall()

    def mark_delivered(self, *, run_id: str | None = None, rowids: list[int] | None = None) -> int:
        """Set delivered_at on a run's items, or on the given rows; only call
        after stdout was written and flushed."""
        at = now_iso()
        with self.conn:
            if run_id is not None:
                cur = self.conn.execute(
                    "UPDATE items SET delivered_at = ? WHERE run_id = ? AND delivered_at IS NULL",
                    (at, run_id))
                return cur.rowcount
            count = 0
            for rowid in rowids or []:
                count += self.conn.execute(
                    "UPDATE items SET delivered_at = ? WHERE rowid = ? AND delivered_at IS NULL",
                    (at, rowid)).rowcount
            return count
