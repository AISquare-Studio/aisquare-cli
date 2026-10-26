"""The prompts Codex's sessions ran since a moment: its answer to
``AgentAdapter.first_prompts_since``, what settles a first message no session has spoken for
(``services.fleet``, #113's #17).

Codex starts its session, and so fires its first hook, with its first prompt, and with its
hooks not trusted fires none; the board alone never learns whether a first message ran.
Measured on 0.164.0 (a loopback provider, temp homes): its rollout records every prompt, a
positional one included, hooks or not, and one held behind its hook review is recorded once
the review is passed.

Read for a listing, so bounded (review r22 of #113, finding 2):

- **Which rollouts.** Codex writes ``sessions/YYYY/MM/DD/rollout-<local time>-<id>.jsonl``;
  only the day directories from ``since`` on are listed, and of those only the rollouts
  whose name says they began at or after it. A session that began before the message was
  given cannot have run it as its first. The tree is never walked, and a rollout of any age
  or size outside that window is never opened.
- **How much of one.** Its first line is its ``session_meta``, which names the folder; one
  of another folder is closed there. Of one in this folder, the head and the tail, within
  :data:`HEAD_BYTES` and :data:`TAIL_BYTES`, are read a line at a time, never the whole.
- **How often.** What a rollout gave is kept against its size and modification time
  (:data:`_KEPT`), so a listing that finds it unchanged reads none of it again.
"""

from __future__ import annotations

import json
import os
import re
from collections import OrderedDict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from aisquare.core.agent_adapters.codex_transcript import _message_text, _own

HEAD_BYTES = 1_000_000
"""How much of a rollout's head is read: its ``session_meta`` and the opening prompts."""
TAIL_BYTES = 1_000_000
"""How much of its tail is read, when it is longer than head and tail together."""
SLACK = timedelta(seconds=2)
"""What a rollout's name may be older than ``since`` and still count: its seconds are whole."""
_NAME = re.compile(r"^rollout-(\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d)-.+\.jsonl$")
_KEPT: OrderedDict[tuple[str, str], tuple[tuple[int, int], tuple[str, ...]]] = OrderedDict()
"""``(rollout, folder)`` to the ``(size, mtime)`` it was read at and the prompts it gave."""
_KEPT_MAX = 256


def prompts_since(home: Path, cwd: Path, since: datetime) -> list[str]:
    """The prompts the sessions under ``home`` that began in ``cwd`` at or after ``since``
    ran, as typed, in the order their rollouts began; Codex's own context is left out."""
    folder = os.path.realpath(cwd)
    found: list[str] = []
    for path in _begun_since(home / "sessions", since):
        found.extend(_prompts_of(path, folder))
    return found


def _begun_since(root: Path, since: datetime) -> list[Path]:
    """The rollouts under ``root`` whose names say they began at or after ``since``, oldest
    first, from the day directories of ``since`` to today's."""
    if not root.is_dir():
        return []
    cutoff = since.astimezone() - SLACK
    today = datetime.now().astimezone().date()
    named: list[tuple[datetime, Path]] = []
    day = cutoff.date()
    while day <= today + timedelta(days=1):  # a clock a little ahead writes tomorrow's
        named.extend(_in_day(root, day, cutoff))
        day += timedelta(days=1)
    return [path for _, path in sorted(named)]


def _in_day(root: Path, day: date, cutoff: datetime) -> list[tuple[datetime, Path]]:
    folder = root / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
    named: list[tuple[datetime, Path]] = []
    try:
        names = os.listdir(folder)
    except OSError:
        return named
    for name in names:
        match = _NAME.match(name)
        if match is None:
            continue
        try:
            began = datetime.strptime(match.group(1), "%Y-%m-%dT%H-%M-%S").astimezone()
        except ValueError:
            continue
        if began >= cutoff:
            named.append((began, folder / name))
    return named


def _prompts_of(path: Path, folder: str) -> tuple[str, ...]:
    """The prompts of one rollout if it began in ``folder``, else none: read once for as
    long as its size and modification time stay what they were."""
    try:
        stat = path.stat()
    except OSError:
        return ()
    key, seen = (str(path), folder), (stat.st_size, stat.st_mtime_ns)
    kept = _KEPT.get(key)
    if kept is not None and kept[0] == seen:
        _KEPT.move_to_end(key)
        return kept[1]
    prompts = tuple(_read(path, folder, stat.st_size))
    _KEPT[key] = (seen, prompts)
    while len(_KEPT) > _KEPT_MAX:
        _KEPT.popitem(last=False)
    return prompts


def _read(path: Path, folder: str, size: int) -> list[str]:
    try:
        with path.open("rb") as handle:
            first = handle.readline(HEAD_BYTES)
            if not _began_in(first, folder):
                return []
            head = handle.read(max(0, min(HEAD_BYTES, size - len(first))))
            if size - len(first) - len(head) <= TAIL_BYTES:
                lines = (head + handle.read(TAIL_BYTES)).split(b"\n")  # all of it
            else:
                lines = head.split(b"\n")[:-1]  # without its last line, which the cut left half
                handle.seek(size - TAIL_BYTES)
                handle.readline()  # and without the half line the tail starts in
                lines += handle.read(TAIL_BYTES).split(b"\n")
    except OSError:
        return []
    return [text for line in lines if (text := _user_prompt(line)) is not None]


def _began_in(line: bytes, folder: str) -> bool:
    record = _record(line)
    payload = record.get("payload") if record else None
    if not isinstance(payload, dict) or record is None or record.get("type") != "session_meta":
        return False
    where = payload.get("cwd")
    return isinstance(where, str) and os.path.realpath(where) == folder


def _user_prompt(line: bytes) -> str | None:
    """The text of a ``user`` message in ``line`` that the human (or a spawn) gave, not Codex's
    own context, else ``None``."""
    if b'"user"' not in line:
        return None
    record = _record(line)
    payload = record.get("payload") if record else None
    if (
        record is None
        or record.get("type") != "response_item"
        or not isinstance(payload, dict)
        or payload.get("type") != "message"
        or payload.get("role") != "user"
    ):
        return None
    text = _message_text(payload)
    return text if text.strip() and not _own(text) else None


def _record(line: bytes) -> dict[str, Any] | None:
    try:
        record = json.loads(line)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None
