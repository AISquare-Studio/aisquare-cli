"""Durable spool for insights on their way to the explainability gateway.

The CLI is not one process: every hook fire, every ``note``, every ``task
claim`` is a separate short-lived interpreter. Anything that shipped from
inside those processes would put the gateway on the primary path — a DNS
lookup, a TLS handshake and a POST between the user pressing enter and Claude
seeing its context. Tracing is an observer: it may cost a trace, never a
launch. So the primary path does exactly one thing here, a small local write,
and a separate process (``aisquare explainability ship``) does the network.

One file per record, not one appended log, because the writers are concurrent
and unco-ordinated: ``O_APPEND`` gives atomicity per write but nothing that
lets a sweeper mark half a file done, and a rewrite-minus-sent pass would race
every writer on the box. A file is claimed by renaming it, delivered by
unlinking it, and given up on by moving it to ``dead/`` — all single atomic
syscalls, all safe against a sweeper being killed mid-drain, and the queue
depth is a directory listing rather than a bookkeeping problem.

Every public function here fails open and returns instead of raising. A
spool that cannot be written is a lost trace; a spool that raises is a lost
prompt.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from aisquare.core import paths

#: Records the sweeper has picked up carry this suffix, so a second sweeper (or
#: the same one after a crash) can tell "in flight" from "waiting".
_CLAIMED_SUFFIX = ".claimed"

#: How long a claimed record may sit before another sweeper may take it back.
#: Generous: a slow gateway must not cause double delivery, and the only cost
#: of waiting is latency on a record that is already late.
_CLAIM_STALE_SECONDS = 900.0


def root() -> Path:
    """Directory holding the spool (not created)."""
    return paths.aisquare_home() / "explainability"


def queue_dir() -> Path:
    """Records waiting to be shipped."""
    return root() / "queue"


def dead_dir() -> Path:
    """Records the sweeper gave up on, kept for inspection rather than dropped."""
    return root() / "dead"


def sent_counter_path() -> Path:
    """File holding the running count of delivered records."""
    return root() / "sent"


@dataclass(frozen=True)
class OutboxCounts:
    """What ``aisquare status`` reports about shipping."""

    queued: int
    sent: int
    dead: int


def enqueue(record: dict[str, object]) -> Path | None:
    """Spool one record for later delivery; return its path, or ``None``.

    Never raises: a read-only home, a full disk or an unserialisable record all
    mean "no trace", never "no note". Callers on the primary path must be able
    to ignore the return value entirely.
    """
    try:
        directory = queue_dir()
        directory.mkdir(parents=True, exist_ok=True)
        # Time-ordered name so a drain delivers roughly in the order the user
        # acted, with a uuid tail because two hooks can fire in the same
        # nanosecond bucket on different processes.
        target = directory / f"{time.time_ns():019d}-{uuid.uuid4().hex[:8]}.json"
        payload = json.dumps(record, ensure_ascii=False, default=str)
        # Write-then-rename: a sweeper listing the directory must never find a
        # half-written record and dead-letter it as corrupt.
        staging = target.with_suffix(".partial")
        staging.write_text(payload, encoding="utf-8")
        staging.replace(target)
        return target
    except Exception:  # an observer may not break its subject
        return None


def pending(limit: int | None = None) -> list[Path]:
    """Spooled records waiting for delivery, oldest first."""
    try:
        files = sorted(p for p in queue_dir().glob("*.json") if p.is_file())
    except OSError:
        return []
    return files[:limit] if limit is not None else files


def counts() -> OutboxCounts:
    """Queue depth, lifetime delivered, and dead-lettered — for ``status``."""
    return OutboxCounts(queued=len(pending()), sent=_read_counter(), dead=_count(dead_dir()))


def claim(path: Path) -> Path | None:
    """Take ownership of one spooled record, or ``None`` if someone else has it.

    ``rename`` onto a fresh name is the whole mutex: exactly one process can
    move a given file, so two sweepers racing the same queue cannot both ship
    the same record.
    """
    claimed = path.with_name(path.name + _CLAIMED_SUFFIX)
    try:
        path.rename(claimed)
    except OSError:
        return None
    return claimed


def release(claimed: Path) -> None:
    """Put a claimed record back in the queue (delivery failed, retry later)."""
    if not claimed.name.endswith(_CLAIMED_SUFFIX):
        return
    with contextlib.suppress(OSError):
        claimed.rename(claimed.with_name(claimed.name[: -len(_CLAIMED_SUFFIX)]))


def reclaim_stale(now: float | None = None) -> int:
    """Return abandoned in-flight records to the queue; return how many.

    A sweeper killed mid-POST leaves its claim behind. Without this the record
    is stranded forever and the queue silently under-reports.
    """
    moment = time.time() if now is None else now
    recovered = 0
    try:
        candidates = list(queue_dir().glob(f"*.json{_CLAIMED_SUFFIX}"))
    except OSError:
        return 0
    for path in candidates:
        try:
            if moment - path.stat().st_mtime < _CLAIM_STALE_SECONDS:
                continue
        except OSError:
            continue
        release(path)
        recovered += 1
    return recovered


def mark_sent(claimed: Path) -> None:
    """Delivery confirmed: drop the record and bump the lifetime counter."""
    try:
        claimed.unlink()
    except OSError:
        return
    _bump_counter()


def mark_dead(claimed: Path, reason: str) -> Path | None:
    """Give up on a record: move it aside with the reason, keeping the payload.

    Dead-lettering is deliberately visible rather than silent — ``status``
    counts these, and the file itself still holds what would have been shipped
    so an operator can see exactly what was lost and why.
    """
    try:
        directory = dead_dir()
        directory.mkdir(parents=True, exist_ok=True)
        body = _read_record(claimed) or {}
        body["dead_letter_reason"] = reason
        target = directory / claimed.name.replace(_CLAIMED_SUFFIX, "")
        target.write_text(json.dumps(body, ensure_ascii=False, default=str), encoding="utf-8")
        claimed.unlink()
        return target
    except Exception:  # losing the corpse must not stop the drain
        return None


def load(path: Path) -> dict[str, object] | None:
    """Read one spooled record, or ``None`` when it is unreadable/corrupt."""
    return _read_record(path)


def _read_record(path: Path) -> dict[str, object] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _count(directory: Path) -> int:
    try:
        return sum(1 for p in directory.glob("*.json") if p.is_file())
    except OSError:
        return 0


def _read_counter() -> int:
    try:
        return int(sent_counter_path().read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def _bump_counter() -> None:
    """Increment the delivered counter. Best effort — it is a display number.

    Not worth a lock: the counter informs a human reading ``status``, and the
    authoritative record of what was delivered is the absence of the file.
    """
    try:
        path = sent_counter_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a+", encoding="utf-8") as fh:  # need a+ handle
            fh.seek(0)
            try:
                current = int(fh.read().strip() or 0)
            except ValueError:
                current = 0
            fh.seek(0)
            fh.truncate()
            fh.write(str(current + 1))
    except OSError:
        return


def clear_for_tests() -> None:
    """Remove the whole spool. Used by tests; harmless in production."""
    directory = root()
    if not directory.exists():
        return
    for path in sorted(directory.rglob("*"), reverse=True):
        if path.is_file():
            path.unlink()
        else:
            os.rmdir(path)
    os.rmdir(directory)
