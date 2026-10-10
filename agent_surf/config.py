"""Environment variables and defaults.

Secrets (ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN) are never stored on the Config
object, so they cannot leak through a repr or a log line. Read them on demand
with the accessor functions below.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

DEFAULT_CDP_URL = "http://127.0.0.1:9222"
DEFAULT_HOME = "~/.agent-surf"
DEFAULT_MODEL = "claude-sonnet-5-5"


ATTACH_MODES = ("cdp", "devtools-active-port")


@dataclass(frozen=True)
class Config:
    cdp_url: str
    home: Path
    model: str
    attach: str = "cdp"                 # AGENT_SURF_ATTACH
    profile_dir: Path | None = None     # AGENT_SURF_PROFILE_DIR (devtools-active-port mode)
    allow_remote_cdp: bool = False      # AGENT_SURF_ALLOW_REMOTE_CDP=1: allow a non-loopback CDP host

    @property
    def db_path(self) -> Path:
        return self.home / "surf.db"

    @property
    def maps_dir(self) -> Path:
        return self.home / "maps"

    @property
    def chrome_profile(self) -> Path:
        return self.home / "chrome-profile"

    def ensure_dirs(self) -> None:
        self.maps_dir.mkdir(parents=True, exist_ok=True)


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def load(env: Mapping[str, str] | None = None) -> Config:
    e = _env(env)
    return Config(
        cdp_url=e.get("AGENT_SURF_CDP_URL") or DEFAULT_CDP_URL,
        home=Path(e.get("AGENT_SURF_HOME") or DEFAULT_HOME).expanduser(),
        model=e.get("AGENT_SURF_MODEL") or DEFAULT_MODEL,
        attach=e.get("AGENT_SURF_ATTACH") or "cdp",
        profile_dir=Path(e["AGENT_SURF_PROFILE_DIR"]).expanduser() if e.get("AGENT_SURF_PROFILE_DIR") else None,
        allow_remote_cdp=e.get("AGENT_SURF_ALLOW_REMOTE_CDP") == "1",
    )


def anthropic_api_key(env: Mapping[str, str] | None = None) -> str | None:
    return _env(env).get("ANTHROPIC_API_KEY") or None


def telegram_credentials(env: Mapping[str, str] | None = None) -> tuple[str, str] | None:
    e = _env(env)
    token = e.get("TELEGRAM_BOT_TOKEN")
    chat_id = e.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        return token, chat_id
    return None
