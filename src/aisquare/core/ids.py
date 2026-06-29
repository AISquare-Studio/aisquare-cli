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
