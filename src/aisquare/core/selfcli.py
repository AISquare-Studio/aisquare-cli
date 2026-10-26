"""Run this CLI as a subprocess of itself, with a chosen working directory.

``init``, ``project onboard`` and several ``doctor`` checks resolve the project
from the PROCESS cwd. A UI that hosts many projects must not ``os.chdir`` —
that is process-wide and a race with every worker — so it runs them here, as
``python -P -m aisquare …`` with ``cwd=<project>``. The cost is one CLI import per
call (~350 ms); the gain is crash isolation and a cwd that is exactly right.

This is a registered spawn seam (``core.spawn.SEAMS``), ruled EXCLUDED and not
stripped: it starts no model process, and ``explainability doctor --live`` needs the
``EXPLAINABILITY_*`` environment to diagnose the machine it is on.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from aisquare.core.state import FLAGS, RuntimeState, get_state


@dataclass(frozen=True)
class CliResult:
    """One finished ``aisquare …`` subprocess."""

    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def argv_for(args: Sequence[str]) -> list[str]:
    """``python -P -m aisquare <args>`` on THIS interpreter — the one install we know runs.

    ``-P`` (the flag form of ``PYTHONSAFEPATH``, Python 3.11+, which this CLI
    requires) keeps the child's cwd OFF ``sys.path``. Without it ``-m`` puts the
    cwd FIRST, so any project whose root holds a top-level ``aisquare/`` package
    — the explainability SDK's own repo ships one — shadows the installed CLI
    and every re-invocation dies with "No module named aisquare.__main__;
    'aisquare' is a package and cannot be directly executed" (#81).

    The flag, not the variable, on purpose. A variable is inherited: the fleet
    window hands its environment to ``aisquare launch``, which ``execve``s the
    agent with all of it, so ``PYTHONSAFEPATH=1`` there would reach every
    command a coder runs and break a project's own ``python -m pytest`` that
    relies on the cwd import. ``-P`` ends with this one process. It is also
    what makes the fleet immune to the tmux SERVER's environment, which a
    window inherits regardless of what the spawner had set.

    Every self-invocation in the package builds its argv here — ``run`` below,
    the fleet's window command and the hook fallback — so the flag has
    exactly one home.
    """
    return [sys.executable, "-P", "-m", "aisquare", *args]


def run(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float = 900.0,
    env: Mapping[str, str] | None = None,
) -> CliResult:
    """Run ``aisquare <args>`` to completion and return what it said.

    Never raises for a non-zero exit — the caller reads ``returncode`` — but a
    ``TimeoutExpired`` or a missing interpreter propagates: those are not
    answers the command gave, they are failures to ask it.
    """
    argv = argv_for(args)
    completed = subprocess.run(
        argv,
        cwd=str(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else dict(os.environ),
        stdin=subprocess.DEVNULL,  # the child must never wait on the TUI's terminal
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    return CliResult(
        argv=argv, returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr
    )


def exec_self(args: Sequence[str]) -> NoReturn:
    """Hand this terminal to ``aisquare <args>`` on THIS install, and end here.

    A registered spawn seam (``core.spawn.SEAMS``), EXCLUDED: our own CLI, no
    model process. asq's Update and Uninstall buttons come here once the UI has
    quit (``run_ui``), and ``upgrade --reopen`` comes here to open asq again on
    the version it just installed. POSIX replaces this process (``os.execv``),
    so nothing of the old one keeps running, from an environment ``uv`` may be
    replacing, and the next program owns the terminal and its exit status.
    Windows has no such exec: its ``os.execv`` starts the program and exits at
    once, handing the prompt back to the shell while the program still asks its
    y/N. There the program runs as a child, waited for, and this process exits
    with its status.

    With the global flags this process was started with (:func:`global_flags`).
    """
    argv = argv_for([*global_flags(), *args])
    sys.stdout.flush()
    sys.stderr.flush()
    if sys.platform == "win32":
        raise SystemExit(subprocess.call(argv))
    os.execv(argv[0], argv)


def global_flags() -> list[str]:
    """The global flags this process was started with, as ``aisquare`` takes them again.

    They set only the runtime state (``cli.app.main_callback``), nothing in the
    environment, so a command :func:`exec_self` hands the terminal to lost them: after
    ``aisquare --no-color ui``, Update printed its plan and its y/N in colour, and the
    asq it reopened had lost ``--profile`` too, back on ``default`` (round 21 of #257).
    ``--json`` is not one: neither asq nor ``--reopen`` hands over under it.

    Every other field of the state, read through ``state.FLAGS``, the one table of the
    flags: kept here by hand, a third list of them would drop the next flag silently
    (round 22). Each one given goes back as itself, a value as ``--flag=VALUE`` so a
    profile named like a flag survives.
    """
    state, unset = get_state(), RuntimeState()
    flags: list[str] = []
    for field, flag in FLAGS.items():
        value = getattr(state, field)
        if field == "json_output" or value == getattr(unset, field):
            continue
        flags.append(flag if value is True else f"{flag}={value}")
    return flags
