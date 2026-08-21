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

import getpass
import os
import stat
import subprocess
import sys
from pathlib import Path

HOME_ENV_VAR = "AISQUARE_HOME"
"""Environment variable that overrides the default ``~/.aisquare`` location."""


def restrict_to_owner(path: Path) -> bool:
    """Make ``path`` readable and writable by its owner only. True when enforced.

    ``chmod(0o600)`` is the whole story on POSIX, and *nothing* on Windows:
    the group/other bits have no NTFS equivalent, so ``os.chmod`` silently
    leaves the file readable by every other account on the machine. Since the
    two callers are an API key and a bearer token, "silently" is the problem —
    the ACL has to be rewritten for real, dropping inherited entries
    (``/inheritance:r``) and replacing the grants (``/grant:r``) so only the
    current user is left.

    Returns False when the restriction could not be applied, so a caller can
    say so rather than implying a protection that is not there.
    """
    if sys.platform != "win32":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return True
    try:
        user = getpass.getuser()
    except Exception:  # pragma: no cover - getuser needs an identifiable user
        return False
    try:
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R,W)"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


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


def state_path() -> Path:
    """Path of the small runtime-state file (e.g. the pinned active project)."""
    return aisquare_home() / "state.json"


def project_data_dir(project_id: str) -> Path:
    """Per-project data directory (codebase snapshots, future sync artifacts)."""
    return aisquare_home() / "projects" / project_id


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
