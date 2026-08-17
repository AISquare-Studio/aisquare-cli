"""The project brain: lock-guarded access to a per-project gbrain store.

gbrain (a third-party CLI) is the team's long-term memory: durable decisions,
results and task outcomes get distilled into one brain per project at
``~/.aisquare/projects/<id>/brain`` and recalled with ``aisquare recall``.

Two hard rules, both learned the expensive way (see the aisquare-office
post-mortems):

- **Never two writers.** gbrain's PGLite engine is single-process and its own
  lock self-destructs after five minutes, so aisquare holds its *own*
  exclusive file lock around every gbrain invocation. Only the distiller and
  ``recall`` ever touch a brain — no daemon, no server, no lock to steal.
- **Never fatal.** A missing gbrain, a failed init or a busy brain degrades
  to "no long-term memory right now" — it must never break the orchestrator or a
  session. Every function here returns a value instead of raising.

Embeddings are stripped from the environment unless ``AISQUARE_BRAIN_EMBED=1``
— a brain write must never turn into a surprise network call.
``AISQUARE_BRAIN=0`` disables the layer entirely.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

from aisquare.core import paths, spawn

# The exclusive-lock primitive, per platform. ``fcntl`` is POSIX-only and
# ``msvcrt`` is Windows-only, so the import is branched on ``sys.platform``
# rather than wrapped in ``try``/``except ImportError``: mypy narrows on
# ``sys.platform`` and type-checks only the branch that is real for the
# platform it runs on, which a ``try`` block would not do.
if sys.platform == "win32":
    import msvcrt

    def _lock_exclusive(handle: IO[str]) -> None:
        """Take the lock without blocking; raise ``OSError`` if held."""
        # Windows byte-range locks are per handle and mandatory, so locking
        # one byte at offset 0 is exclusive across processes just like flock.
        # The region may sit past EOF, which is what keeps this working on the
        # empty lock file.
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle: IO[str]) -> None:
        """Release the lock taken by :func:`_lock_exclusive`."""
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_exclusive(handle: IO[str]) -> None:
        """Take the lock without blocking; raise ``OSError`` if held."""
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: IO[str]) -> None:
        """Release the lock taken by :func:`_lock_exclusive`."""
        fcntl.flock(handle, fcntl.LOCK_UN)


_OFF_VALUES = {"0", "false", "no", "off"}
_ON_VALUES = {"1", "true", "yes", "on"}
_INIT_TIMEOUT_S = 120
_CALL_TIMEOUT_S = 30
_RECALL_LOCK_WAIT_S = 5.0


def brain_enabled() -> bool:
    """Whether the brain layer is enabled (``AISQUARE_BRAIN=0`` disables)."""
    return os.environ.get("AISQUARE_BRAIN", "").strip().lower() not in _OFF_VALUES


def embeddings_enabled() -> bool:
    """Whether distilled pages may be embedded (``AISQUARE_BRAIN_EMBED``).

    Opt-in (default off), but accepts the usual truthy words — the value is
    baked into the brain schema at first distill, so a natural ``true`` that
    silently meant "off" would permanently create a vectorless brain.
    """
    return os.environ.get("AISQUARE_BRAIN_EMBED", "").strip().lower() in _ON_VALUES


def embed_model() -> str:
    """The embedding model a new brain is sized for when embeddings are on."""
    return (
        os.environ.get("AISQUARE_BRAIN_EMBED_MODEL", "").strip() or "openai:text-embedding-3-large"
    )


def gbrain_version() -> str | None:
    """The installed gbrain version, or ``None`` when unavailable."""
    binary = shutil.which("gbrain")
    if binary is None:
        return None
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
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


def brain_embeds(project_id: str) -> bool:
    """Whether the brain's *schema* was created embedding-capable.

    gbrain sizes embeddings at create time and records the choice in
    ``.gbrain/config.json``: verified against gbrain 0.42.1.0, a
    ``--no-embedding`` brain carries ``"embedding_disabled": true`` and an
    embedding brain carries ``"embedding_model"``/``"embedding_dimensions"``.
    This reads the actual schema, so callers never trust the env knob alone —
    a brain built before the knob was set, or with it off, is correctly
    reported as non-embedding. Anything unreadable or unexpected → False, so
    recall/doctor degrade safely rather than crash (both never-crash paths).
    """
    config = brain_home(project_id) / ".gbrain" / "config.json"
    if not config.exists():
        return False
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    return not data.get("embedding_disabled", False) and bool(data.get("embedding_model"))


def _env(home: Path) -> dict[str, str]:
    """The environment one gbrain command runs with.

    Never a tracing identity: gbrain is a store, not an agent session, and the
    ``ANTHROPIC_API_KEY`` guard below is the tell that an Anthropic path exists
    at all — with an inherited ``ANTHROPIC_BASE_URL`` that path would run
    through our proxy and post a Run under whichever role was distilling.
    """
    env = spawn.untraced_env()
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
                _lock_exclusive(handle)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.1)
        try:
            yield True
        finally:
            _unlock(handle)
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
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=_env(home),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _ensure(project_id: str) -> bool:
    """Initialise the brain if needed (idempotent; caller holds the lock).

    The embedding schema is a create-time ("file-plane") decision in gbrain:
    a ``--no-embedding`` brain cannot embed later even with a key present.
    So the ``AISQUARE_BRAIN_EMBED=1`` knob must be honoured at init — size
    the schema for :func:`embed_model` when it is set, ``--no-embedding``
    (network-call-free) otherwise.
    """
    if brain_ready(project_id):
        return True
    home = brain_home(project_id)
    if embeddings_enabled():
        argv = ["init", "--pglite", "--embedding-model", embed_model()]
    else:
        argv = ["init", "--pglite", "--no-embedding"]
    _run(home, argv, timeout=_INIT_TIMEOUT_S)
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
    """Search the project brain. ``None`` = unavailable (missing/busy/failed).

    Hybrid ``query`` (vector + keyword RRF) is used only when the knob is on
    AND the brain's schema actually embeds — that is what the vectors are for.
    If the hybrid call then fails at query time (e.g. the OpenAI key needed to
    embed the query text is missing or expired), it degrades to keyword
    ``search`` rather than turning a working recall into a hard failure.
    ``--no-expand`` keeps recall LLM-free on either path.
    """
    if not brain_enabled() or not brain_ready(project_id):
        return None
    home = brain_home(project_id)
    use_hybrid = embeddings_enabled() and brain_embeds(project_id)
    with _lock(home, wait_s=_RECALL_LOCK_WAIT_S) as won:
        if not won:
            return None
        if use_hybrid:
            hybrid = _run(home, ["query", query, "--no-expand"], timeout=_CALL_TIMEOUT_S)
            if hybrid is not None:
                return hybrid  # fell through: hybrid failed, degrade to keyword
        return _run(home, ["search", query], timeout=_CALL_TIMEOUT_S)
