"""Filesystem layout of the ``~/.aisquare`` home directory.

Layout:
    ~/.aisquare/
    ├── config.toml     # typed TOML configuration (see core.config)
    ├── credentials     # API keys / tokens
    ├── context.db      # SQLite store: context entries and projects (see core.store)
    ├── agents.json     # registry of detected and connected agents
    ├── cache/          # disposable cached data
    ├── explainability/ # session→Run join records (see services.explainability)
    └── log/            # capture and diagnostic logs

Set ``AISQUARE_HOME`` to relocate the whole tree (tests rely on this).
"""

from __future__ import annotations

import os
from pathlib import Path

HOME_ENV_VAR = "AISQUARE_HOME"
"""Environment variable that overrides the default ``~/.aisquare`` location."""


def aisquare_home() -> Path:
    """Return the aisquare home directory (without creating it).

    The path is taken verbatim and nothing constrains it to a native disk. This
    is the one place that choice is made, so its consequence is recorded here
    rather than only beside the code that suffers it.

    ``core.config.save_config`` publishes changes with the durable-replace
    recipe — sibling temp, fsync, ``os.replace`` over the target, fsync the
    parent. Two of its properties come from the FILESYSTEM, not from our code:
    the replace is atomic, so a concurrent reader sees the whole old file or the
    whole new one and never a partial document; and the fsyncs cost about what a
    local disk costs, measured at +2.15 ms median for the directory flush.

    Both were measured on a native disk, where the default ``~/.aisquare``
    lives. NEITHER IS MEASURED FOR A WINDOWS-BACKED MOUNT (WSL 9p/DrvFs,
    ``/mnt/c/...``), where a rename becomes a Windows operation and an fsync is
    a round trip to the host. ``AISQUARE_HOME=/mnt/c/...``, or a Windows-side
    HOME, puts config.toml exactly there.

    What is NOT in doubt on such a mount: the temp file is created in the
    TARGET'S OWN DIRECTORY, so temp and target always share a filesystem and the
    precondition ``os.replace`` needs holds by construction. The open question is
    narrower than "does this work on 9p" — it is whether that filesystem's
    rename carries the same whole-old-or-whole-new guarantee, and what an fsync
    costs there.

    Until that is measured, treat a Windows-backed AISQUARE_HOME as unsupported
    FOR THE ATOMICITY GUARANTEE SPECIFICALLY. Everything still functions; what
    has not been ruled out is a torn read during a concurrent write, which on a
    native disk has been.

    ``aisquare doctor`` now reports which filesystem this path is on, so an
    operator does not have to know any of the above to find out they are in the
    unmeasured case — see ``services.diagnostics._check_home_filesystem``. It
    reports rather than refuses: being on a translated filesystem is unmeasured,
    not known-broken.
    """
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


def explainability_dir() -> Path:
    """Directory for explainability artefacts written by the launcher."""
    return aisquare_home() / "explainability"


def explainability_joins_path() -> Path:
    """Path of the session→Run join log (JSON Lines, append-only).

    Deliberately NOT under ``cache/``: this is the only local copy of the key
    that joins board rows to gateway Runs, and ``cache/`` is documented as
    disposable.
    """
    return explainability_dir() / "joins.jsonl"


def truncation_marker_path() -> Path:
    """Records that ``context.db`` was found emptied and rebuilt.

    Deliberately NOT under ``cache/``: that directory is documented as
    disposable, and this is the only surviving evidence that a board's history
    was lost. It exists because the fact is knowable for exactly one line —
    ``open_store`` sees a zero-length file, and one statement later the schema is
    back and nothing can tell an emptied store from a new machine.

    Cleared by the operator, not by us. A warning that clears itself is one
    nobody has to answer.
    """
    return aisquare_home() / "store-was-truncated"


def ensure_home() -> Path:
    """Create the aisquare home layout if missing and return its root."""
    home = aisquare_home()
    for directory in (home, cache_dir(), log_dir()):
        directory.mkdir(parents=True, exist_ok=True)
    return home
