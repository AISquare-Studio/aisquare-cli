"""Which features get used: a count per local day and name, never an argument.

The Typer root callback records the command path (``cli.fleet.spawn``) and the TUI each view it
switches to (``tui.view.usage_stats``), so the Usage & stats page can say what is used and the
opt-in sharing path can send the same names. A name is a dotted path of lower-case words;
anything else is dropped, so no argument, path or text can end up here by a caller's mistake.

**Recording is an append and nothing more.** :func:`record` writes one short line to a spool
file (``paths.feature_use_path``) opened for appending: microseconds, no database to open or
lock, nothing a command could wait on. :func:`fold` moves the spool into the store's
``feature_use`` table (a (day, name) → count) whenever the stats are read, renaming the spool
first so that a command recording meanwhile starts a new one and none is lost or counted
twice. **It fails open**: every error is swallowed, because a counter is the least important
thing a command does.
"""

from __future__ import annotations

import contextlib
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from aisquare.core import paths

if TYPE_CHECKING:  # the root callback imports this on every command: the store is not its cost
    from aisquare.core.store import ContextStore

#: A feature name: dotted lower-case words, digits, ``_`` and ``-``.
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*(\.[a-z0-9][a-z0-9_-]*){0,6}$")
MAX_NAME = 80
#: The spool stops growing here until something folds it: a machine whose stats are never
#: read still cannot fill its disk with counters.
MAX_SPOOL_BYTES = 1_000_000

_FOLDING = ".folding"


def valid(name: str) -> bool:
    """Whether ``name`` is a feature name this module records."""
    return len(name) <= MAX_NAME and _NAME.match(name) is not None


def view_feature(view_id: str) -> str:
    """The feature name of a TUI view (``tui.view.<name>``): the content pane's id with its
    ``-`` as ``_``, and a project's or an agent's view named for the kind, never for which —
    their ids are the project's or the agent's own."""
    for kind in ("project", "agent"):
        if view_id.startswith(f"{kind}-"):
            return f"tui.view.{kind}"
    return "tui.view." + view_id.replace("-", "_").lower()


def local_day(now: datetime | None = None) -> str:
    """The local calendar day, ``YYYY-MM-DD``."""
    return (now or datetime.now().astimezone()).strftime("%Y-%m-%d")


def record(name: str, *, now: datetime | None = None, spool: Path | None = None) -> bool:
    """Count one use of feature ``name`` today. ``False`` when it was not counted — an invalid
    name, a home that does not exist yet, a full spool, any error — and never raises."""
    try:
        if not valid(name):
            return False
        target = spool if spool is not None else paths.feature_use_path()
        fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            if os.fstat(fd).st_size > MAX_SPOOL_BYTES:
                return False
            os.write(fd, f"{local_day(now)}\t{name}\n".encode())
        finally:
            os.close(fd)
        return True
    except Exception:  # fail open: a counter never costs a command
        return False


def fold(store: ContextStore, *, spool: Path | None = None) -> int:
    """Move the spool (and any left by a fold that died) into ``store``; the uses folded.

    Never raises: a fold that cannot finish leaves its file where the next one reads it.
    """
    source = spool if spool is not None else paths.feature_use_path()
    folded = 0
    try:
        candidates = [source, *sorted(source.parent.glob(f"{source.name}.*{_FOLDING}"))]
        for path in candidates:
            mine = source.with_name(f"{source.name}.{os.getpid()}{_FOLDING}")
            try:
                os.replace(path, mine)  # atomic: a concurrent fold finds nothing to take
            except OSError:
                continue
            counts = _read(mine)
            store.add_feature_uses(counts)
            with contextlib.suppress(OSError):
                mine.unlink()
            folded += sum(counts.values())
    except Exception:  # fail open; the spool stays for the next fold
        return folded
    return folded


def _read(path: Path) -> Counter[tuple[str, str]]:
    counts: Counter[tuple[str, str]] = Counter()
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            day, _, name = line.rstrip("\n").partition("\t")
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) and valid(name):
                counts[(day, name)] += 1
    return counts
