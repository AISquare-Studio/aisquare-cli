"""asq UIs older than the registry, found in ``ps`` (#21).

asq 0.9.1 and older write no record (``core.instances``), so the instance gate could not see
one: the first upgrade after the registry shipped is the owner's very case, the Mac's asq
still 0.9.1 while the one over SSH is the new release. So the gate also lists this user's
processes on this host, once, and an asq UI with no live record is one of an unknown release
older than the registry, which cannot be asked to quit (``cli.ui.instance_gate``).

What is an asq UI is decided on the command line alone (:func:`ui_command`): ``asq`` or
``aisquare`` with no subcommand (global options allowed) or with ``ui``, run as a script, by
an interpreter, or as ``python -m aisquare``. Every other aisquare process is left out: the
CLI's subcommands, ``launch``, ``watch``, the hooks, ``asq-statusline``, ``remote serve``,
pytest, and the fleet's tmux (``tmux -L asq``). So are this process and its ancestors, every
pid a live record names, one started in the last :data:`STARTING_SECONDS` (a registry-aware
asq still on its way to its record), and one whose environment names another home.

No ``ps``, a ``ps`` that fails or outlasts :data:`PS_TIMEOUT_SECONDS`, or Windows: nothing
is found, said once in the log, and the gate is the registry's alone.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from aisquare.core import paths

PS_TIMEOUT_SECONDS = 2.0
"""The bound on the one ``ps`` the gate runs."""

STARTING_SECONDS = 10.0
"""How old an asq UI with no record must be to count. A registry-aware asq registers within a
second or two of starting, and its own gate then sees this one: younger than this, a process
is far more likely one of those than an old release started at the same moment."""

_PROGRAMS = frozenset({"asq", "aisquare"})
_GLOBAL_FLAGS = frozenset({"--verbose", "-v", "--quiet", "-q", "--no-color"})
"""``aisquare``'s global options that take no value and still open the UI."""
_NEVER_THE_UI = frozenset({"--json", "--version", "-V", "--help", "-h"})
"""Options under which ``aisquare`` with no subcommand prints and exits."""
_INTERPRETER_VALUES = frozenset({"-X", "-W", "--check-hash-based-pycs"})
"""Python's options whose value is the next argument."""

_log = logging.getLogger(__name__)


class _Process(NamedTuple):
    pid: int
    ppid: int
    elapsed: timedelta
    tty: str | None
    command: str


@dataclass(frozen=True)
class UnregisteredUi:
    """An asq UI on this home with no record: of a release before the registry."""

    pid: int
    command: str
    tty: str | None
    started_at: datetime

    @property
    def where(self) -> str:
        """``pid 4242, /dev/ttys004, since 11:02``."""
        since = self.started_at.astimezone()
        today = datetime.now().astimezone().date()
        when = since.strftime("%H:%M") if since.date() == today else since.strftime("%b %d %H:%M")
        tty = f", {self.tty}" if self.tty else ""
        return f"pid {self.pid}{tty}, since {when}"

    @property
    def shown_command(self) -> str:
        """The command line, the home directory written ``~`` and long ones cut short."""
        home = str(Path.home())
        shown = self.command.replace(home + "/", "~/") if home not in ("", "/") else self.command
        return shown if len(shown) <= 160 else shown[:159] + "…"


# --- what is an asq UI -------------------------------------------------------------------------


def _name(token: str) -> str:
    return token.replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")


def ui_command(command: str) -> bool:
    """Whether ``command`` (``ps``'s, arguments joined by spaces) runs asq's UI."""
    tokens = command.split()
    if not tokens:
        return False
    if _name(tokens[0]) in _PROGRAMS:
        return _opens_the_ui(tokens[1:])
    if not _name(tokens[0]).startswith("python"):
        return False
    at = 1
    while at < len(tokens) and tokens[at].startswith("-") and tokens[at] not in ("-m", "-c", "-"):
        at += 2 if tokens[at] in _INTERPRETER_VALUES else 1
    if at >= len(tokens):
        return False
    if tokens[at] == "-m":
        return tokens[at + 1 : at + 2] == ["aisquare"] and _opens_the_ui(tokens[at + 2 :])
    if _name(tokens[at]) in _PROGRAMS:
        return _opens_the_ui(tokens[at + 1 :])
    return False  # -c, a script of another name, or stdin


def _opens_the_ui(args: list[str]) -> bool:
    """Whether ``aisquare <args>`` opens the UI: no subcommand, or ``ui``."""
    at = 0
    while at < len(args):
        arg = args[at]
        if arg in _NEVER_THE_UI:
            return False
        if arg == "--profile":
            at += 2
        elif arg.startswith("--profile=") or arg in _GLOBAL_FLAGS or _short_flags(arg):
            at += 1
        else:
            return arg == "ui"
    return True


def _short_flags(arg: str) -> bool:
    """``-vq``: the global short flags that still open the UI, written together."""
    return len(arg) > 2 and arg[0] == "-" and arg[1] != "-" and set(arg[1:]) <= {"v", "q"}


# --- the listing -------------------------------------------------------------------------------


def _skipped(why: str) -> None:
    _log.info("asq's look for an asq older than the registry is skipped: %s", why)


def _ps() -> str | None:
    """``ps``'s listing of this user's processes, or ``None`` when it cannot be had."""
    if sys.platform == "win32":
        _skipped("Windows has no ps")
        return None
    argv = ["ps", "-U", str(os.getuid()), "-ww", "-o", "pid=,ppid=,etime=,tty=,command="]
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=PS_TIMEOUT_SECONDS,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _skipped(f"{type(exc).__name__}: {exc}")
        return None
    if done.returncode != 0:
        _skipped(f"ps exited {done.returncode}: {done.stderr.strip()[:200]}")
        return None
    return done.stdout


def parse_elapsed(text: str) -> timedelta | None:
    """``ps``'s ``etime``, ``[[dd-]hh:]mm:ss``, as a duration; ``None`` for anything else."""
    days, _, clock = text.rpartition("-")
    parts = clock.split(":")
    if not 2 <= len(parts) <= 3 or not all(part.isdigit() for part in parts):
        return None
    if days and not days.isdigit():
        return None
    hours, minutes, seconds = ([0] * (3 - len(parts))) + [int(part) for part in parts]
    return timedelta(days=int(days or 0), hours=hours, minutes=minutes, seconds=seconds)


def _parse(listing: str) -> list[_Process]:
    found: list[_Process] = []
    for line in listing.splitlines():
        fields = line.split(None, 4)
        if len(fields) != 5 or not fields[0].isdigit() or not fields[1].isdigit():
            continue
        elapsed = parse_elapsed(fields[2])
        if elapsed is None:
            continue
        tty = fields[3]
        named = None if tty in ("?", "??", "-") else tty if tty.startswith("/") else f"/dev/{tty}"
        found.append(_Process(int(fields[0]), int(fields[1]), elapsed, named, fields[4].strip()))
    return found


def _lineage(pid: int, processes: list[_Process]) -> set[int]:
    """``pid`` and its ancestors, as the listing says them."""
    parents = {process.pid: process.ppid for process in processes}
    lineage: set[int] = set()
    while pid > 0 and pid not in lineage:
        lineage.add(pid)
        pid = parents.get(pid, 0)
    return lineage


# --- whose home --------------------------------------------------------------------------------

_HOME_VARIABLE = re.compile(r"(?:^|\s)(AISQUARE_HOME|HOME)=(\S*)")


def _environment(process: _Process) -> dict[str, str] | None:
    """``AISQUARE_HOME`` and ``HOME`` as ``process`` has them, or ``None`` when unreadable.

    Linux says it in ``/proc/<pid>/environ``; macOS and the BSDs append it to the command in
    ``ps -E``, for this user's processes. A value with a space in it is cut at the space.
    """
    environ = Path("/proc") / str(process.pid) / "environ"
    if Path("/proc/self/environ").exists():
        try:
            pairs = environ.read_bytes().decode(errors="replace").split("\0")
        except OSError:
            return None
        found = dict(pair.split("=", 1) for pair in pairs if "=" in pair)
        return {key: found[key] for key in ("AISQUARE_HOME", "HOME") if key in found}
    argv = ["ps", "-E", "-ww", "-p", str(process.pid), "-o", "command="]
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=PS_TIMEOUT_SECONDS,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    said = done.stdout.strip()
    if done.returncode != 0 or not said.startswith(process.command):
        return None
    environment = said[len(process.command) :]
    return {match[1]: match[2] for match in _HOME_VARIABLE.finditer(environment)}


def _on_this_home(process: _Process) -> bool:
    """Whether ``process`` runs on this home; ``True`` when that cannot be told."""
    environment = _environment(process)
    if environment is None:
        return True
    named = environment.get("AISQUARE_HOME")
    if named:
        home = Path(named).expanduser()
        if not home.is_absolute():
            return True  # relative to its working directory, which is not ours to read
    elif environment.get("HOME"):
        home = Path(environment["HOME"]) / ".aisquare"
    else:
        return True
    try:
        return home.resolve() == paths.aisquare_home().resolve()
    except OSError:
        return True


# --- the look ---------------------------------------------------------------------------------


def unregistered_uis(registered: set[int]) -> list[UnregisteredUi]:
    """The asq UIs on this home and host with no live record, oldest first: ``registered`` are
    the pids live records on this host name. ``[]`` when ``ps`` cannot say (logged)."""
    listing = _ps()
    if listing is None:
        return []
    try:
        processes = _parse(listing)
        mine = _lineage(os.getpid(), processes)
        now = datetime.now(UTC)
        found = [
            UnregisteredUi(process.pid, process.command, process.tty, now - process.elapsed)
            for process in processes
            if process.pid not in mine
            and process.pid not in registered
            and process.elapsed >= timedelta(seconds=STARTING_SECONDS)
            and ui_command(process.command)
            and _on_this_home(process)
        ]
    except Exception as exc:  # a look that fails costs the look, never asq's start
        _skipped(f"{type(exc).__name__}: {exc}")
        return []
    return sorted(found, key=lambda ui: ui.started_at)
