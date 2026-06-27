"""Filesystem layout of the ``~/.aisquare`` home directory.

Layout:
    ~/.aisquare/
    ├── config.toml     # typed TOML configuration (see core.config)
    ├── credentials     # API keys / tokens
    ├── context.db      # SQLite store: context entries and projects (see core.store)
    ├── agents.json     # registry of detected and connected agents
    ├── cache/          # disposable cached data
    └── log/            # capture and diagnostic logs

Set ``AISQUARE_HOME`` to relocate the whole tree (tests rely on this).
"""

from __future__ import annotations

import os
from pathlib import Path

HOME_ENV_VAR = "AISQUARE_HOME"
"""Environment variable that overrides the default ``~/.aisquare`` location."""


def aisquare_home() -> Path:
    """Return the aisquare home directory (without creating it)."""
    override = os.environ.get(HOME_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".aisquare"


def cache_dir() -> Path:
    """Directory for disposable cached data."""
    return aisquare_home() / "cache"


def log_dir() -> Path:
    """Directory for capture and diagnostic logs."""
    return aisquare_home() / "log"


def config_path() -> Path:
    """Path of the TOML config file."""
    return aisquare_home() / "config.toml"


def db_path() -> Path:
    """Path of the SQLite database holding context entries and projects."""
    return aisquare_home() / "context.db"


def last_injection_path() -> Path:
    """Path of the record describing the most recent context injection."""
    return cache_dir() / "last_injection.json"


def credentials_path() -> Path:
    """Path of the credentials file (API keys, tokens)."""
    return aisquare_home() / "credentials"


def agents_registry_path() -> Path:
    """Path of the agents registry (detected and connected agents)."""
    return aisquare_home() / "agents.json"


def ensure_home() -> Path:
    """Create the aisquare home layout if missing and return its root."""
    home = aisquare_home()
    for directory in (home, cache_dir(), log_dir()):
        directory.mkdir(parents=True, exist_ok=True)
    return home
