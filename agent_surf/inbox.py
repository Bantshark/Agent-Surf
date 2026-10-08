"""v2 inbox reading: notifications and messages pages, read like any other
page (reading side only; never imports the publishing side). Many inboxes
stream over WebSockets, so reading maps may use "source": "websocket"."""

from __future__ import annotations

from typing import Any

from agent_surf import learner, sites
from agent_surf.browser import ReadOnlyPage
from agent_surf.runner import RunResult
from agent_surf.store import Store


def run_inbox(store: Store, page: ReadOnlyPage, site: str, page_type: str, *, client: Any, model: str,
              maps_dir: Any, guard: Any = None) -> RunResult:
    if page_type not in sites.INBOX_PAGE_TYPES.get(site, set()):
        known = ", ".join(sorted(sites.INBOX_PAGE_TYPES.get(site, set()))) or "none"
        raise ValueError(f"{site} {page_type} is not an inbox page (inbox pages: {known})")
    return learner.run_with_heal(store, page, site, page_type, client=client, model=model,
                                 maps_dir=maps_dir, guard=guard)
