"""Run this CLI as a subprocess of itself, with a chosen working directory.

``init``, ``project onboard`` and several ``doctor`` checks resolve the project
from the PROCESS cwd. A UI that hosts many projects must not ``os.chdir`` —
that is process-wide and a race with every worker — so it runs them here, as
``python -m aisquare …`` with ``cwd=<project>``. The cost is one CLI import per
call (~350 ms); the gain is crash isolation and a cwd that is exactly right.

This is a registered spawn seam (``core.spawn.SEAMS``), ruled EXCLUDED and not
stripped: it starts no model process, and ``doctor --live`` needs the
``EXPLAINABILITY_*`` environment to diagnose the machine it is on.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


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
    """``python -m aisquare <args>`` on THIS interpreter — the one install we know runs."""
    return [sys.executable, "-m", "aisquare", *args]


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
