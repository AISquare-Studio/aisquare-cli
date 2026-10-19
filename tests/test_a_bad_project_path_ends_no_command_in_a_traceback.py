"""Every command that takes or resolves a project path ends in its result or a clean refusal,
never a traceback, whatever the path has become.

Rounds 39 to 43 of the review of #259 each found one more command that a root in one more
state ended in a traceback, or answered wrongly: a ``fleet pause`` of a root that became a
symlink loop, a ``reap --all`` past a root under a folder this user may not search, a phone
``remove`` of that root, a ``project forget`` of a ``~wip`` nickname, a ``project onboard`` of
a file. Each was fixed where it was found, and the next round found the next. So the commands
and the states are one table here: a project, ``app``, with a live coder and an ended one
that had a worktree, its root made each state in turn, and each command run against it the
way a user runs it. Every cell's outcome is pinned: a traceback fails the cell, and so does a
clean refusal where the command used to work.

A new command that takes or resolves a project path is a row of :data:`OUTCOMES`; a new
state a root can be in, a column.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app as cli
from aisquare.core import tmux as tmux_core
from aisquare.core.config import FleetSettings
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import project as project_service
from aisquare.services import remote_server
from aisquare.services.remote_server import Runtime, build_app, live_writes
from tests import fakebin
from tests.fsperms import can_deny_reads, can_symlink, remove_tree
from tests.remote_kit_helpers import base, make_client, make_runtime, unlock
from tests.test_fleet_service import FakeClock, FakeTmux, _as_080_left_it, _git

NICKNAME = "~wip"
"""A nickname a path lookup cannot expand: no user ``wip`` has a home here."""

STATES = ("missing", "a file", "a symlink loop", "under a folder it may not search", NICKNAME)


class Fleet:
    """``app``: a git checkout with a live coder (``coder-1``, a 0.8.0 row, so its board is
    worked out from the root) and an ended one whose worktree a reap may take
    (``coder-2``), on a tmux kept in memory."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        tmux = FakeTmux()
        monkeypatch.setattr(fleet_service, "server", lambda config=None: tmux)
        monkeypatch.setattr(
            fleet_service, "settings", lambda: FleetSettings(tmux_socket="asq-test")
        )
        monkeypatch.setattr(tmux_core, "desktop_environment", lambda environ=None: {})
        clock = FakeClock()
        monkeypatch.setattr(fleet_service, "_sleep", clock.sleep)
        monkeypatch.setattr(fleet_service, "_monotonic", clock.monotonic)
        monkeypatch.delenv("AISQUARE_TEAM_HUB", raising=False)
        fakebin.prepend_to_path(tmp_path / "bin", monkeypatch)
        fakebin.executable_fake(
            tmp_path / "bin",
            "claude",
            windows='echo fake claude: "%*"\nset /p line=',
            posix='echo "fake claude: $*"\nread line',
        )
        self.root = tmp_path / "code" / "app"
        self.root.mkdir(parents=True)
        _git("init", "-q", "-b", "main", cwd=self.root)
        _git("commit", "-q", "--allow-empty", "-m", "init", cwd=self.root)
        with store_session() as store:
            self.project: ProjectInfo = store.onboard_project(team_project(self.root))
        live = fleet_service.spawn(self.project, "coder", label="coder-1", worktree=False)
        _as_080_left_it(live.agent)
        fleet_service.spawn(self.project, "coder", label="coder-2", worktree=True)
        fleet_service.stop(self.project, "coder-2", force=True)
        with store_session() as store:
            stored = store.get_project(self.project.id)
        assert stored is not None and stored.codename
        self.codename = stored.codename

    def become(self, state: str) -> str:
        """Make the root ``state``; the ref a user names the project by in that state: its
        root, or its nickname."""
        if state == NICKNAME:
            project_service.set_nickname(self.project, NICKNAME)
            return NICKNAME
        if state == "under a folder it may not search":
            if not can_deny_reads():
                pytest.skip("mode 000 does not stop this user from reading")
            self.root.parent.chmod(0)
            return str(self.root)
        if state == "a symlink loop" and not can_symlink():
            pytest.skip("this machine cannot make a symlink")
        remove_tree(self.root)
        if state == "a file":
            self.root.write_text("", encoding="utf-8")
        elif state == "a symlink loop":
            self.root.symlink_to(self.root)
        return str(self.root)

    def restore(self) -> None:
        self.root.parent.chmod(0o700)


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Fleet]:
    made = Fleet(tmp_path, monkeypatch)
    try:
        yield made
    finally:
        made.restore()


Outcome = str
"""``ok``, or the error code a command refused with (its ``--json`` ``error``, or a phone's)."""


def _cli(runner: CliRunner, *argv: str) -> Outcome:
    """``aisquare --json <argv>``: ``ok``, or its error code; never a traceback."""
    result = runner.invoke(cli, ["--json", *argv])
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        raise AssertionError(f"`{' '.join(argv)}` ended in a traceback") from result.exception
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, f"`{' '.join(argv)}` printed nothing (exit {result.exit_code})"
    payload = json.loads(lines[-1])
    if result.exit_code == 0:
        return "ok"
    assert isinstance(payload, dict) and "error" in payload, result.stdout
    return str(payload["error"])


def _phone(runtime: Runtime, tmp_path: Path, method: str, route: str, **kw: Any) -> Outcome:
    """A phone's request to the real services: ``ok``, or its error code; never a 500."""
    built = build_app(
        runtime, sources=remote_server.live_sources(), writes=live_writes(), dist_dir=tmp_path
    )
    client = make_client(built)
    unlock(client, runtime)
    runtime.set_allow_write(True)
    answer = client.request(method, f"{base(runtime)}/api/{route}", **kw)
    assert answer.status_code < 500, answer.text
    return "ok" if answer.status_code == 200 else str(answer.json()["error"])


Command = Callable[[Fleet, str, CliRunner, Runtime], Outcome]


def _project_ref(fleet: Fleet, ref: str) -> str:
    """The ``--project`` a user gives: the nickname, or the codename (``--project`` takes no
    path)."""
    return ref if ref == NICKNAME else fleet.codename


COMMANDS: dict[str, Command] = {
    "project onboard": lambda f, ref, r, _: _cli(r, "project", "onboard", ref),
    "init": lambda f, ref, r, _: _cli(r, "init", "--local", "--no-onboard", ref),
    "project forget": lambda f, ref, r, _: _cli(r, "project", "forget", ref),
    "project prune": lambda f, ref, r, _: _cli(r, "project", "prune", "--missing", "--yes"),
    "fleet reap": lambda f, ref, r, _: _cli(r, "fleet", "reap", "--all"),
    "fleet pause": lambda f, ref, r, _: _cli(r, "fleet", "pause", "-P", _project_ref(f, ref)),
    "fleet resume": lambda f, ref, r, _: _cli(r, "fleet", "resume", "-P", _project_ref(f, ref)),
    "fleet restart": lambda f, ref, r, _: _cli(
        r, "fleet", "restart", "coder-1", "-P", _project_ref(f, ref)
    ),
    "fleet switch": lambda f, ref, r, _: _cli(
        r, "fleet", "switch", "coder-1", "-P", _project_ref(f, ref)
    ),
    "remote board": lambda f, ref, _, rt: _phone(
        rt, f.tmp_path, "GET", "board", params={"project": _project_ref(f, ref)}
    ),
    "remote tasks": lambda f, ref, _, rt: _phone(
        rt, f.tmp_path, "GET", "tasks", params={"project": _project_ref(f, ref)}
    ),
    "remote remove": lambda f, ref, _, rt: _phone(
        rt, f.tmp_path, "POST", "project/remove", json={"ref": ref}
    ),
}


OK, BUSY, NOT_A_DIRECTORY, FLEET = "ok", "project_busy", "not_a_directory", "fleet_error"

#: Each command's outcome in each of :data:`STATES`, in that order. ``project_busy`` is the
#: project found, and refused for its live coder. A path ``onboard`` and ``init`` are given
#: is a folder or refused (round 43, 3/7; ``~wip`` is a relative path that does not exist).
#: ``pause`` and ``restart`` work a gone root's board out as its own (round 42, 3/11) and
#: refuse one that cannot be looked at (round 43, 4/7). ``switch`` refuses in every state,
#: for the loop and the unsearchable root on the board, else for want of another account.
OUTCOMES: dict[str, tuple[Outcome, ...]] = {
    "project onboard": (NOT_A_DIRECTORY,) * 5,
    "init": (NOT_A_DIRECTORY,) * 5,
    "project forget": (BUSY,) * 5,
    "project prune": (OK,) * 5,
    "fleet reap": (OK,) * 5,
    "fleet pause": (OK, OK, FLEET, FLEET, OK),
    "fleet resume": (OK,) * 5,
    "fleet restart": (OK, OK, FLEET, FLEET, OK),
    "fleet switch": (FLEET,) * 5,
    "remote board": (OK,) * 5,
    "remote tasks": (OK,) * 5,
    "remote remove": (BUSY,) * 5,
}


def test_every_command_has_an_outcome_in_every_state() -> None:
    assert list(OUTCOMES) == list(COMMANDS)
    assert {len(outcomes) for outcomes in OUTCOMES.values()} == {len(STATES)}


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("command", list(COMMANDS))
def test_a_command_on_a_project_path_in_any_state_ends_cleanly(
    fleet: Fleet, runner: CliRunner, command: str, state: str
) -> None:
    ref = fleet.become(state)

    outcome = COMMANDS[command](fleet, ref, runner, make_runtime())

    assert outcome == OUTCOMES[command][STATES.index(state)]
