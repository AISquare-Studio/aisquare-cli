"""The project brain: flock-guarded access to a per-project gbrain store.

gbrain (a third-party CLI) is the team's long-term memory: durable decisions,
results and task outcomes get distilled into one brain per project at
``~/.aisquare/projects/<id>/brain`` and recalled with ``aisquare recall``.

Two hard rules, both learned the expensive way (see the aisquare-office
post-mortems):

- **Never two writers.** gbrain's PGLite engine is single-process and its own
  lock self-destructs after five minutes, so aisquare holds its *own*
  ``flock`` around every gbrain invocation. Only the distiller and ``recall``
  ever touch a brain — no daemon, no server, no lock to steal.
- **Never fatal.** A missing gbrain, a failed init or a busy brain degrades
  to "no long-term memory right now" — it must never break the bus or a
  session. Every function here returns a value instead of raising.

Embeddings are stripped from the environment unless ``AISQUARE_BRAIN_EMBED=1``
— a brain write must never turn into a surprise network call.
``AISQUARE_BRAIN=0`` disables the layer entirely.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from aisquare.core import paths

_OFF_VALUES = {"0", "false", "no", "off"}
_INIT_TIMEOUT_S = 120
_CALL_TIMEOUT_S = 30
_RECALL_LOCK_WAIT_S = 5.0


def brain_enabled() -> bool:
    """Whether the brain layer is enabled (``AISQUARE_BRAIN=0`` disables)."""
    return os.environ.get("AISQUARE_BRAIN", "").strip().lower() not in _OFF_VALUES


def embeddings_enabled() -> bool:
    """Whether distilled pages may be embedded (``AISQUARE_BRAIN_EMBED=1``)."""
    return os.environ.get("AISQUARE_BRAIN_EMBED", "").strip() == "1"


def gbrain_version() -> str | None:
    """The installed gbrain version, or ``None`` when unavailable."""
    binary = shutil.which("gbrain")
    if binary is None:
        return None
    try:
        result = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = result.stdout.strip()
    return output.split()[-1] if result.returncode == 0 and output else None


def brain_home(project_id: str) -> Path:
    """Where this project's brain lives."""
    return paths.project_data_dir(project_id) / "brain"


def brain_ready(project_id: str) -> bool:
    """Whether the brain has been initialised (PGLite data marker present)."""
    return (brain_home(project_id) / ".gbrain" / "brain.pglite" / "PG_VERSION").exists()


def _env(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["GBRAIN_HOME"] = str(home)
    if not embeddings_enabled():
        env.pop("OPENAI_API_KEY", None)
        env.pop("ANTHROPIC_API_KEY", None)
    return env


@contextmanager
def _lock(home: Path, *, wait_s: float) -> Iterator[bool]:
    """aisquare's own exclusive lock around a brain; yields whether it was won.

    ``wait_s == 0`` is a try-lock (drains skip when another drain runs);
    otherwise the lock is polled for up to ``wait_s`` seconds.
    """
    home.mkdir(parents=True, exist_ok=True)
    handle = (home / ".aisquare.lock").open("w")
    deadline = time.monotonic() + wait_s
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.1)
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


def _run(home: Path, argv: list[str], *, stdin: str | None = None, timeout: int) -> str | None:
    """Run one gbrain command against ``home``; stdout on success, else ``None``."""
    binary = shutil.which("gbrain")
    if binary is None:
        return None
    try:
        result = subprocess.run(
            [binary, *argv],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=_env(home),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _ensure(project_id: str) -> bool:
    """Initialise the brain if needed (idempotent; caller holds the lock)."""
    if brain_ready(project_id):
        return True
    home = brain_home(project_id)
    _run(home, ["init", "--pglite", "--no-embedding"], timeout=_INIT_TIMEOUT_S)
    return brain_ready(project_id)


def distill_page(project_id: str, slug: str, content: str) -> bool:
    """Write one distilled page. Caller must hold the drain lock via ``drain_lock``."""
    if not _ensure(project_id):
        return False
    return (
        _run(brain_home(project_id), ["put", slug], stdin=content, timeout=_CALL_TIMEOUT_S)
        is not None
    )


@contextmanager
def drain_lock(project_id: str) -> Iterator[bool]:
    """Try-lock for a distiller drain; a running drain makes this yield False."""
    with _lock(brain_home(project_id), wait_s=0) as won:
        yield won


def recall(project_id: str, query: str) -> str | None:
    """Search the project brain. ``None`` = unavailable (missing/busy/failed)."""
    if not brain_enabled() or not brain_ready(project_id):
        return None
    with _lock(brain_home(project_id), wait_s=_RECALL_LOCK_WAIT_S) as won:
        if not won:
            return None
        return _run(brain_home(project_id), ["search", query], timeout=_CALL_TIMEOUT_S)
