"""Per-session cache for CI endpoint responses.

The ``prompt_submit`` call is synchronous: a developer has hit enter and is
watching a cursor while it runs. That is what makes this cache load-bearing
rather than an optimisation — the design intent is that ``session_start``
prefetches a bundle and the common path resolves locally in single-digit
milliseconds, with the network call as the exception rather than the rule.

Storage is one JSON file per session under ``~/.aisquare/cache/ci/``. Entries
are keyed by the server's ``cache_hint.key`` and expire on its ``ttl_s``: the
CLI treats both as opaque, so what is cacheable and for how long stays a
server-side decision that can change without a CLI release.

Nothing here raises. A corrupt file, an unwritable directory or a half-written
entry resolves to a miss, because the only cost of a miss is a request — while
an exception on this path reaches a hook wrapping a developer's prompt.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from typing import Any

from aisquare.core import paths

_SAFE_SESSION = re.compile(r"[^A-Za-z0-9._-]")
_MAX_ENTRIES = 256
"""Cap per session file. A long session with a prompt-scoped key would grow
without one, and this file is read synchronously on the hot path — an
unbounded JSON parse in front of every prompt is the cost this cache exists to
avoid."""


def _session_file(session_id: str) -> Any:
    """Path of the cache file for ``session_id``, name-sanitised.

    ``session_id`` arrives from the agent's hook payload, so it is untrusted
    input being turned into a filename — ``../`` in it would otherwise write
    outside the cache directory.
    """
    safe = _SAFE_SESSION.sub("_", session_id)[:128] or "unknown"
    return paths.ci_cache_dir() / f"{safe}.json"


def _load(session_id: str) -> dict[str, Any]:
    """Every entry for ``session_id``; ``{}`` when absent or unreadable."""
    path = _session_file(session_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    entries = raw.get("entries")
    return entries if isinstance(entries, dict) else {}


def read(session_id: str, key: str) -> str | None:
    """The cached body for ``key``, or ``None`` on a miss or an expired entry."""
    entry = _load(session_id).get(key)
    if not isinstance(entry, dict):
        return None
    expires_at = entry.get("expires_at")
    body = entry.get("body")
    if not isinstance(expires_at, int | float) or not isinstance(body, str):
        return None
    if time.time() >= expires_at:
        return None
    return body


def write(session_id: str, key: str, body: str, ttl_s: int) -> None:
    """Cache ``body`` under ``key`` for ``ttl_s`` seconds. Never raises.

    A non-positive TTL is honoured as "do not cache" rather than as an instant
    expiry, so a server can turn caching off per response without the client
    writing a file it will never read.
    """
    if ttl_s <= 0:
        return
    entries = _load(session_id)
    entries[key] = {"expires_at": time.time() + ttl_s, "body": body}
    if len(entries) > _MAX_ENTRIES:
        entries = _newest(entries)
    _atomic_write(session_id, entries)


def _newest(entries: dict[str, Any]) -> dict[str, Any]:
    """The newest ``_MAX_ENTRIES`` by expiry, dropping the rest."""

    def expiry(item: tuple[str, Any]) -> float:
        value = item[1].get("expires_at") if isinstance(item[1], dict) else 0
        return float(value) if isinstance(value, int | float) else 0.0

    ordered = sorted(entries.items(), key=expiry, reverse=True)
    return dict(ordered[:_MAX_ENTRIES])


def _atomic_write(session_id: str, entries: dict[str, Any]) -> None:
    """Replace the session file in one step; a crash leaves the old one intact."""
    path = _session_file(session_id)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps({"entries": entries}), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()


def clear(session_id: str) -> None:
    """Drop everything cached for one session. Never raises."""
    with contextlib.suppress(OSError):
        _session_file(session_id).unlink()


def gc(max_age_s: float = 86_400) -> int:
    """Remove session files untouched for ``max_age_s``; returns how many.

    Session ids come from the agent, so nothing here observes a session ending
    reliably — without a sweep the directory only ever grows.
    """
    directory = paths.ci_cache_dir()
    cutoff = time.time() - max_age_s
    removed = 0
    try:
        candidates = list(directory.glob("*.json"))
    except OSError:
        return 0
    for path in candidates:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed
