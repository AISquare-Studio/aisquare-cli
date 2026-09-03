"""Identifier generation for context entries and projects.

Entry ids are ULID-style: a 48-bit millisecond timestamp followed by 80 bits
of randomness, rendered in lowercase Crockford base32 behind a ``ctx_`` prefix
(e.g. ``ctx_01j9q8p3k7zr4m2n6v0c1d8e5f``). Two properties matter:

- **time-sortable** — lexicographic order of ids matches creation order, so a
  plain ``ORDER BY id`` lists entries oldest-first;
- **prefix-addressable** — like a git short hash, an unambiguous leading
  substring resolves to the full id (see ``core.store``).
"""

from __future__ import annotations

import os
import time

# Crockford base32, lowercased: digits plus letters minus i, l, o and u.
_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
_TS_CHARS = 10  # 48-bit timestamp → 10 base32 chars (50 bits)
_RAND_BYTES = 10  # 80 bits of randomness → 16 base32 chars

ENTRY_PREFIX = "ctx_"
PROJECT_PREFIX = "prj_"
PROMPT_PREFIX = "prm_"
TASK_PREFIX = "tsk_"
EVENT_PREFIX = "evt_"
TRACE_PREFIX = "trc_"


def _encode(value: int, length: int) -> str:
    """Render ``value`` as ``length`` lowercase Crockford base32 characters."""
    chars = ["0"] * length
    for i in range(length - 1, -1, -1):
        value, remainder = divmod(value, 32)
        chars[i] = _ALPHABET[remainder]
    return "".join(chars)


def _new_id(prefix: str) -> str:
    """A fresh, time-sortable ULID-style id behind ``prefix``."""
    timestamp_ms = int(time.time() * 1000)
    randomness = int.from_bytes(os.urandom(_RAND_BYTES), "big")
    return prefix + _encode(timestamp_ms, _TS_CHARS) + _encode(randomness, 16)


def new_entry_id() -> str:
    """Return a fresh, time-sortable context entry id (``ctx_…``)."""
    return _new_id(ENTRY_PREFIX)


def new_prompt_id() -> str:
    """Return a fresh, time-sortable captured-prompt id (``prm_…``)."""
    return _new_id(PROMPT_PREFIX)


def new_task_id() -> str:
    """Return a fresh, time-sortable team-task id (``tsk_…``)."""
    return _new_id(TASK_PREFIX)


def new_event_id() -> str:
    """Return a fresh, time-sortable team-event id (``evt_…``)."""
    return _new_id(EVENT_PREFIX)


def new_trace_id() -> str:
    """Return a fresh, time-sortable CI trace id (``trc_…``).

    One turn's identity across the hooks, the metrics row and the CI endpoint.
    Minted here rather than reused from the agent's ``session_id`` because a
    session spans many turns: keying on it would collapse every turn of a
    session into one trace and make per-turn comparison impossible.

    Note that ``run_id`` is *not* minted here — that one is the server's, and a
    client that generated its own would fork the run space silently.
    """
    return _new_id(TRACE_PREFIX)
