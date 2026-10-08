"""SQLite store at <AGENT_SURF_HOME>/surf.db: map versions, seen ids, items."""

from __future__ import annotations

import json
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


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(maps)")}
        with self.conn:
            for col, decl in MAPS_ADDED_COLUMNS.items():
                if col not in have:
                    self.conn.execute(f"ALTER TABLE maps ADD COLUMN {col} {decl}")

    def close(self) -> None:
        self.conn.close()

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

    def mark_seen(self, site: str, page_type: str, item_id: str) -> bool:
        """Record an id. Returns True if it was not seen before."""
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO seen (site, page_type, item_id, first_seen) VALUES (?, ?, ?, ?)",
                (site, page_type, item_id, now_iso()),
            )
        return cur.rowcount == 1

    def record_item(self, site: str, page_type: str, item_id: str, data: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO items (site, page_type, item_id, data_json, captured_at) VALUES (?, ?, ?, ?, ?)",
                (site, page_type, item_id, json.dumps(data, ensure_ascii=False), now_iso()),
            )

    def add_new_item(self, site: str, page_type: str, item_id: str, data: dict[str, Any]) -> bool:
        """Mark seen and store the item if it is new. Returns True if new."""
        if not self.mark_seen(site, page_type, item_id):
            return False
        self.record_item(site, page_type, item_id, data)
        return True
