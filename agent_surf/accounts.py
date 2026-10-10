"""The user's own account per site (Fix 16), used to find their own posts after
an unknown publish outcome. One user, their own accounts: one handle per site."""

from __future__ import annotations

import re

from agent_surf import sites
from agent_surf.store import Store, now_iso

_HANDLE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def set_handle(store: Store, site: str, handle: str) -> str:
    sites.get_site(site)
    handle = handle.strip().lstrip("@")
    if not _HANDLE.match(handle):
        raise ValueError("handle must be letters, digits, '_', '.' or '-' (no URL, no spaces)")
    with store.conn:
        store.conn.execute("INSERT INTO accounts (site, handle, set_at) VALUES (?, ?, ?)"
                           " ON CONFLICT(site) DO UPDATE SET handle = excluded.handle, set_at = excluded.set_at",
                           (site, handle, now_iso()))
    return handle


def get_handle(store: Store, site: str) -> str | None:
    row = store.conn.execute("SELECT handle FROM accounts WHERE site = ?", (site,)).fetchone()
    return row["handle"] if row else None


def list_accounts(store: Store) -> list[dict]:
    return [dict(r) for r in store.conn.execute("SELECT site, handle, set_at FROM accounts ORDER BY site")]
