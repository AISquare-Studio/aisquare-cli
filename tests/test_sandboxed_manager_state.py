"""A sandboxed manager may write its own state directory, and nothing else in the store.

SMOKE13 N1 (#78 on #113): the cycle tells a manager to keep
``$AISQUARE_HOME/projects/<id>/manager/state.md`` (``paths.manager_state_file``), and a Codex
launch held all of the store read-only, so every rewrite of it was an escalation: a guardian
review each, live, or the human's question under a permission mode that asks, and a parent
manager told not to escalate could not write it at all. A Claude Code manager writes it
freely. Each launch of an agent that sandboxes its tools now names that one directory
writable (``services.store_broker.own_state_dir``), decided from the role and the project
its cycle names, never the agent; the adapter spells it (``AgentAdapter.broker_access``).
The synthetic sandboxed agent's grant is pinned in tests/test_third_agent_seams.py, and the
real Codex's in tests/test_codex_native.py.
"""

from __future__ import annotations

import os
import subprocess
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aisquare.core import paths
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.agent_adapters.codex import PERMISSION_PROFILE
from aisquare.core.agent_adapters.types import AgentAdapter, SandboxWrites, store_dir
from aisquare.core.ids import new_agent_id
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo, TeamSession
from aisquare.services import store_broker
from aisquare.services import team as team_service

LAUNCH = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectInfo:
    """A board in a git checkout, the launch's directory; no broker process is started."""
    if os.name != "posix":
        pytest.skip("a launch's broker is POSIX's")
    work = tmp_path / "repo"
    work.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    monkeypatch.chdir(work)
    for name in ("AISQUARE_FLEET_AGENT", "AISQUARE_TEAM_HUB", "AISQUARE_ROLE"):
        monkeypatch.delenv(name, raising=False)

    def not_started(directory: Path, env: object) -> None:
        raise RuntimeError("not started")

    monkeypatch.setattr(store_broker, "run_broker", not_started)
    return team_service.activate(work)


def _launch(
    role: str | None, adapter: AgentAdapter | None = None, **env: str
) -> tuple[list[str], str]:
    """What a launch of ``role`` (``AISQUARE_ROLE``) runs: ``adapter``'s store profile, as
    ``start_for_launch`` gives it its broker's paths."""
    agent = adapter or get_adapter("codex")
    launch = {"AISQUARE_LAUNCH_ID": LAUNCH, **env}
    if role is not None:
        launch["AISQUARE_ROLE"] = role
    argv = [agent.binary, *agent.store_access("workspace", store_dir())]
    return store_broker.start_for_launch(launch, agent, argv)


def _table(argv: list[str]) -> dict[str, str]:
    """The store profile's filesystem table a Codex launch runs under."""
    key = f"permissions.{PERMISSION_PROFILE}.filesystem="
    value = next(arg for arg in argv if arg.startswith(key)).partition("=")[2]
    table: dict[str, str] = tomllib.loads(f"t = {value}")["t"]
    return table


def _own(project_id: str) -> Path:
    """The manager directory of ``project_id`` as the store's profile names it (resolved)."""
    return store_dir() / "projects" / project_id / "manager"


def _writes(argv: list[str]) -> SandboxWrites:
    writes = get_adapter("codex").sandbox_writes(argv[1:], Path.cwd())
    assert writes is not None
    return writes


def _store_writes(table: dict[str, str]) -> set[str]:
    """Every path in the store the table names writable."""
    return {
        path
        for path, at in table.items()
        if at == "write" and Path(path).is_relative_to(store_dir())
    }


def test_a_codex_managers_launch_may_write_its_own_state_dir_and_nothing_else_in_the_store(
    board: ProjectInfo,
) -> None:
    """The launch argv grants write on the manager directory the cycle names, made before the
    launch, beside the broker's inbox and reports, and on no other path in the store: the
    store's root, its database, another project's manager directory and this project's own
    directory stay read-only. Mutants: no grant (the first assertion dies), the grant widened
    to AISQUARE_HOME (the store reads ``write``, and the set of writable paths differs)."""
    argv, _ = _launch("manager")
    table = _table(argv)
    own = _own(board.id)
    directory = store_broker.launch_dir(store_dir(), LAUNCH)

    assert table.get(str(own)) == "write"
    assert own.is_dir(), "made before the launch"
    assert own == paths.manager_state_file(board.id).parent.resolve()
    assert table[str(store_dir())] == "read"
    assert _store_writes(table) == {
        str(directory / store_broker.INBOX),
        str(directory / store_broker.REPORTS),
        str(own),
    }
    writes = _writes(argv)
    assert writes.may_write(own / "state.md")
    assert writes.may_write(own / "briefs" / "01-plan.md"), "what the manager keeps beside it"
    for refused in (
        store_dir() / "state.md",
        paths.db_path().resolve(),
        store_dir() / "projects" / board.id / "state.md",
        _own("prj_another") / "state.md",
        store_dir() / "projects" / "prj_another" / "manager",
    ):
        assert not writes.may_write(refused), refused


@pytest.mark.parametrize("role", ["coder", "reviewer", "tester", "runner", "planner", None])
def test_no_other_role_is_granted_a_state_dir(board: ProjectInfo, role: str | None) -> None:
    """Only a manager's cycle hands it a state file: every other seat's launch has the
    inbox and the reports alone, and makes no manager directory. Mutant (the grant given to
    every role): dies here."""
    argv, _ = _launch(role)
    directory = store_broker.launch_dir(store_dir(), LAUNCH)

    assert _store_writes(_table(argv)) == {
        str(directory / store_broker.INBOX),
        str(directory / store_broker.REPORTS),
    }
    assert not (store_dir() / "projects" / board.id / "manager").exists()


def test_a_numbered_managers_seat_is_a_manager(board: ProjectInfo) -> None:
    """``AISQUARE_ROLE`` carries a seat verbatim (``manager2``): its cycle is a manager's."""
    argv, _ = _launch("manager2")
    assert _table(argv).get(str(_own(board.id))) == "write"


def test_the_grant_is_the_directory_the_cycle_names(
    board: ProjectInfo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launch and the cycle name one directory (``team._cycle_state_file``): the board's
    for a hand launch; for a fleet window, the project of the row ``AISQUARE_FLEET_AGENT``
    names, whichever it is; under a hub, the directory's registered project, else the hub
    board's (team.py's ``_cycle_project``). The cycle's own text names the same file."""
    me = TeamSession(
        id="aaaa1111-0000-0000-0000-000000000000",
        project_id=board.id,
        role="manager",
        started_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )
    cycle = "\n".join(team_service._role_cycle(me, board))
    assert f"Keep your state in {paths.manager_state_file(board.id)};" in cycle
    assert team_service.own_state_dir("manager", Path.cwd(), None) == (
        paths.manager_state_file(board.id).parent
    )

    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    theirs = team_service.activate(other)
    with store_session() as store:
        row = store.upsert_fleet_agent(
            FleetAgent(
                id=new_agent_id(),
                project_id=theirs.id,
                label="manager",
                role="manager",
                pane_id="%1",
                cwd=other,
                created_at=datetime.now(tz=UTC),
            )
        )
    assert team_service.own_state_dir("manager", Path.cwd(), row.id) == (
        paths.manager_state_file(theirs.id).parent
    )
    argv, _ = _launch("manager", AISQUARE_FLEET_AGENT=row.id)
    assert _table(argv).get(str(_own(theirs.id))) == "write"
    assert str(_own(board.id)) not in _table(argv), "the board's is not its cycle's"

    # Under a hub: the directory's registered project, else the hub board's.
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(other))
    assert team_service.own_state_dir("manager", Path.cwd(), None) == (
        paths.manager_state_file(board.id).parent
    )
    loose = tmp_path / "loose"
    loose.mkdir()
    assert team_service.own_state_dir("manager", loose, None) == (
        paths.manager_state_file(theirs.id).parent
    )


def test_a_link_on_the_way_to_the_state_dir_is_refused_never_followed(
    board: ProjectInfo, tmp_path: Path
) -> None:
    """Made or refused from the store down, no name followed: a link on the way (the project's
    directory, or the manager directory itself) or a name that is not a directory leaves the
    launch without the grant, says so, and writes nothing where the link leads. An empty file
    there, what a sandbox leaves for a path its profile named that was missing, is replaced.
    Mutant (``mkdir(parents=True)`` and grant): the link's target gets a ``manager``
    directory and the grant, and dies here."""
    project_dir = store_dir() / "projects" / board.id
    outside = tmp_path / "outside"
    outside.mkdir()
    project_dir.parent.mkdir(parents=True, exist_ok=True)
    project_dir.symlink_to(outside, target_is_directory=True)

    argv, note = _launch("manager")

    assert str(_own(board.id)) not in _table(argv)
    assert not any(at == "write" and "outside" in path for path, at in _table(argv).items())
    assert list(outside.iterdir()) == [], "nothing made where the link leads"
    assert f"its sandbox may not write its state directory {_own(board.id)}" in note, note

    project_dir.unlink()
    project_dir.mkdir()
    (project_dir / "manager").symlink_to(outside, target_is_directory=True)
    argv, note = _launch("manager")
    assert str(_own(board.id)) not in _table(argv)
    assert "its sandbox may not write its state directory" in note, note

    (project_dir / "manager").unlink()
    (project_dir / "manager").write_text("not a directory", encoding="utf-8")
    argv, note = _launch("manager")
    assert str(_own(board.id)) not in _table(argv)
    assert (project_dir / "manager").read_text(encoding="utf-8") == "not a directory"

    (project_dir / "manager").write_text("", encoding="utf-8")
    argv, note = _launch("manager")
    assert _table(argv).get(str(_own(board.id))) == "write"
    assert (project_dir / "manager").is_dir()
    assert "state directory" not in note


def test_a_launch_with_no_sandbox_of_the_stores_makes_and_grants_nothing_of_it(
    board: ProjectInfo,
) -> None:
    """An unsandboxed agent (Claude Code) writes its state file itself: its launch is as it
    was, its argv untouched."""
    argv = ["claude", "--permission-mode", "auto"]
    env = {"AISQUARE_LAUNCH_ID": LAUNCH, "AISQUARE_ROLE": "manager"}
    assert store_broker.start_for_launch(env, get_adapter("claude-code"), argv) == (argv, "")


def test_the_state_dir_is_made_below_the_store_or_refused_on_every_platform(
    tmp_path: Path,
) -> None:
    """``_make_below`` on each platform (Windows asks each name, POSIX walks descriptors):
    the missing names are made, a name that is not a directory is refused and kept, and so is
    a link where this machine can make one, its target given nothing."""
    from tests.fsperms import can_symlink

    root = tmp_path / "home"
    root.mkdir()
    store_broker._make_below(root, ("projects", "prj_a", "manager"))
    assert (root / "projects" / "prj_a" / "manager").is_dir()
    store_broker._make_below(root, ("projects", "prj_a", "manager"))  # there: nothing to do

    (root / "projects" / "prj_b").write_text("mine", encoding="utf-8")
    with pytest.raises(OSError):
        store_broker._make_below(root, ("projects", "prj_b", "manager"))
    assert (root / "projects" / "prj_b").read_text(encoding="utf-8") == "mine"

    if not can_symlink():
        return
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "projects" / "prj_c").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        store_broker._make_below(root, ("projects", "prj_c", "manager"))
    assert list(outside.iterdir()) == []


def test_the_windows_walk_refuses_a_junction_on_the_way_on_python_3_11_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_make_below``'s Windows walk, run on every platform, as Python 3.11 sees a junction:
    a directory whose ``lstat`` has ``FILE_ATTRIBUTE_REPARSE_POINT`` and no
    ``os.path.isjunction`` to ask (3.12's). It is refused, nothing made below it; a plain
    directory is walked. Mutant (``_is_junction`` asking ``os.path.isjunction`` alone, as it
    did): 3.11 walked through the junction, and made ``manager`` where it leads (review r39
    of #113, finding 4)."""
    import stat
    from types import SimpleNamespace

    root = tmp_path / "home"
    junction = root / "projects" / "prj_j"
    junction.mkdir(parents=True)
    (root / "projects" / "prj_d").mkdir()
    lstat = os.lstat

    def as_windows(path: str | os.PathLike[str], **kwargs: object) -> object:
        found = lstat(path, **kwargs)  # type: ignore[arg-type]
        if Path(path) != junction:
            return found
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return SimpleNamespace(st_mode=found.st_mode, st_file_attributes=reparse)

    with monkeypatch.context() as patch:
        patch.delattr(os.path, "isjunction", raising=False)
        patch.setattr(os, "lstat", as_windows)
        assert store_broker._is_junction(junction)
        assert not store_broker._is_junction(root / "projects" / "prj_d")
        assert not store_broker._is_junction(root / "nothing-there")
        with pytest.raises(OSError, match="a link"):
            store_broker._make_below_windows(root, ("projects", "prj_j", "manager"))
        store_broker._make_below_windows(root, ("projects", "prj_d", "manager"))
    assert not (junction / "manager").exists()
    assert (root / "projects" / "prj_d" / "manager").is_dir()


def test_a_launch_with_no_store_profile_makes_no_state_dir(board: ProjectInfo) -> None:
    """A Codex manager launched unsandboxed (``--sandbox danger-full-access``) writes its state
    file itself: its launch is as it was, and makes no manager directory. Mutant (the grant's
    directory made before the store's profile is asked): dies here."""
    codex = get_adapter("codex")
    argv = ["codex", "--sandbox", "danger-full-access"]
    env = {"AISQUARE_LAUNCH_ID": LAUNCH, "AISQUARE_ROLE": "manager"}
    assert store_broker.start_for_launch(env, codex, argv)[0] == argv
    assert not (store_dir() / "projects" / board.id / "manager").exists()
