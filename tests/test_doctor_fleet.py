"""Doctor checks for the fleet, and a doctor that can be asked about another directory.

docs/plans/fleet-tui.md §5 ("Doctor" row), §5.6 last paragraph, §8.2. Three new
rows — ``tmux``, ``gh``, ``fleet`` — and ``doctor(cwd=…)``, which threads the
directory into the three project-scoped checks so the UI can report on a
selected project without ``os.chdir``.

Every check here is read-only and offline; ``tests/test_doctor_does_not_create_state.py``
already pins that ``doctor`` creates no home, and the tests below pin the fleet
flavour of that promise: no tmux server is started, and the bundled tmux conf is
not written into an existing home either. The tmux server is faked by
subclassing ``TmuxServer`` (its scripted answers are the CONTRACT the check
consumes) and exercised once for real on a private socket when tmux is present.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import brain as brain_core
from aisquare.core import paths
from aisquare.core import snapshot as snapshot_core
from aisquare.core import tmux as tmux_core
from aisquare.core.config import FleetSettings
from aisquare.core.ids import new_agent_id
from aisquare.core.store import store_session
from aisquare.core.tmux import PaneFacts, TmuxError, TmuxServer, TmuxUnavailable
from aisquare.core.workspace import project_id_for
from aisquare.models import CheckStatus, DoctorCheck, FleetAgent, ProjectInfo, Snapshot
from aisquare.services import diagnostics
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from aisquare.services.onboarding import fix_commands

# --- fakes and seeds -------------------------------------------------------------------


def _facts(pane_id: str, *, dead: bool = False) -> PaneFacts:
    return PaneFacts(
        pane_id=pane_id,
        width=200,
        height=50,
        cursor_x=0,
        cursor_y=0,
        cursor_visible=True,
        alternate_on=False,
        history_size=0,
        dead=dead,
        dead_status=1 if dead else None,
        in_mode=False,
        current_command="claude",
        title="",
    )


class FakeServer(TmuxServer):
    """A ``TmuxServer`` that never runs tmux: scripted answers, recorded questions.

    Subclassing keeps the fake honest against the contract — a renamed method
    on ``TmuxServer`` breaks this class at type-check time, not at 3 a.m.
    """

    def __init__(
        self,
        *,
        present: bool = True,
        version: tuple[int, int] | None = (3, 7),
        sessions: tuple[str, ...] = (),
        panes: dict[str, PaneFacts | None] | None = None,
        socket: str = "asq",
        version_raises: bool = False,
        facts_raise: bool = False,
    ) -> None:
        super().__init__(socket, conf=Path("/nonexistent/fleet-tmux.conf"))
        self._present = present
        self._version = version
        self._sessions = sessions
        self._panes = panes or {}
        self._version_raises = version_raises
        self._facts_raise = facts_raise
        self.asked: list[str] = []

    def binary(self) -> str:
        if not self._present:
            raise TmuxUnavailable("tmux is not installed")
        return "/usr/bin/tmux"

    def available(self) -> bool:
        return self._present

    def version(self) -> tuple[int, int] | None:
        self.asked.append("version")
        if self._version_raises:
            raise RuntimeError("tmux -V hung")
        return self._version

    def list_sessions(self) -> list[str]:
        self.asked.append("list_sessions")
        return list(self._sessions)

    def pane_facts(self, pane_id: str) -> PaneFacts | None:
        self.asked.append(f"pane_facts:{pane_id}")
        if self._facts_raise:
            raise TmuxError("unexpected display-message output")
        return self._panes.get(pane_id)


def _agent(
    project_id: str,
    label: str,
    pane_id: str,
    *,
    socket: str = "asq",
    ended: bool = False,
) -> FleetAgent:
    now = datetime.now(tz=UTC)
    return FleetAgent(
        id=new_agent_id(),
        project_id=project_id,
        label=label,
        role="coder",
        tmux_socket=socket,
        pane_id=pane_id,
        cwd=Path("/work"),
        created_at=now,
        ended_at=now if ended else None,
        exit_status=0 if ended else None,
    )


def _seed(root: Path, *agents: FleetAgent) -> ProjectInfo:
    """A registered project at ``root`` with these ``fleet_agent`` rows — real store, temp home."""
    project = ProjectInfo(id=project_id_for(root.resolve()), root=root.resolve(), linked_repos=[])
    with store_session() as store:
        store.ensure_project(project)
        for agent in agents:
            store.upsert_fleet_agent(agent)
    return project


@pytest.fixture
def home(isolated_home: Path) -> Path:
    """An initialised (but empty) home: the store opens, no project is onboarded."""
    paths.ensure_home()
    with store_session():
        pass
    return isolated_home


def _by_name(checks: list[DoctorCheck]) -> dict[str, DoctorCheck]:
    return {check.name: check for check in checks}


def _no_which(_cmd: str, *_args: Any, **_kwargs: Any) -> None:
    return None


# --- install_hint: the per-OS line, from a file ---------------------------------------


def test_install_hint_reads_the_distribution_family(tmp_path: Path) -> None:
    fedora = tmp_path / "fedora"
    fedora.write_text('NAME="Fedora Linux"\nID=fedora\n', encoding="utf-8")
    mint = tmp_path / "mint"
    mint.write_text('ID=linuxmint\nID_LIKE="ubuntu debian"\n', encoding="utf-8")

    assert diagnostics.install_hint("tmux", platform="linux", os_release=fedora) == (
        "dnf install tmux"
    )
    assert diagnostics.install_hint("tmux", platform="linux", os_release=mint) == (
        "apt install tmux"
    )
    assert diagnostics.install_hint("tmux", platform="darwin") == "brew install tmux"


def test_install_hint_names_every_manager_when_unsure(tmp_path: Path) -> None:
    """A wrong hint is worse than three right ones; an unreadable file never raises.

    "Unreadable" covers two shapes and each is a separate way out of the read:
    absent (``FileNotFoundError``, an ``OSError``) and UNDECODABLE — bytes that
    are not UTF-8 raise ``UnicodeDecodeError``, which is a ``ValueError`` and
    walked straight through the ``except OSError`` this function used to have.
    The known distribution is the negative control: whatever the guard
    swallows, a readable file must still be parsed.
    """
    alien = tmp_path / "alien"
    alien.write_text("ID=plan9\n", encoding="utf-8")
    undecodable = tmp_path / "binary-os-release"
    undecodable.write_bytes(b"\xff\xfe\x00ID=fedora\n")

    unsure = diagnostics.install_hint("tmux", platform="linux", os_release=alien)
    unreadable = diagnostics.install_hint(
        "tmux", platform="linux", os_release=tmp_path / "does-not-exist"
    )
    binary = diagnostics.install_hint("tmux", platform="linux", os_release=undecodable)

    for hint in (unsure, unreadable, binary):
        assert "apt install tmux" in hint and "dnf install tmux" in hint
        assert "brew install tmux" in hint
    fedora = tmp_path / "fedora"
    fedora.write_text("ID=fedora\n", encoding="utf-8")
    assert diagnostics.install_hint("tmux", platform="linux", os_release=fedora) == (
        "dnf install tmux"
    ), "a guard that swallowed the parse too would answer 'unsure' here as well"
    assert "WSL2" in diagnostics.install_hint("tmux", platform="win32")


# --- the tmux check --------------------------------------------------------------------


def test_tmux_absent_is_a_warning_that_says_what_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", _no_which)

    check = diagnostics._check_tmux()

    assert check.status is CheckStatus.warn
    assert "the fleet is unavailable; everything else works" in check.detail
    assert check.fix and "install tmux" in check.fix


def test_tmux_new_enough_is_ok_and_names_the_version() -> None:
    check = diagnostics._check_tmux(FakeServer(version=(3, 7)))

    assert check.status is CheckStatus.ok
    assert check.detail == "tmux 3.7 — fleet available"
    assert check.fix is None


def test_tmux_too_old_warns_with_the_minimum_and_an_install_hint() -> None:
    check = diagnostics._check_tmux(FakeServer(version=(3, 1)))

    assert check.status is CheckStatus.warn
    assert "tmux 3.1" in check.detail and "3.2" in check.detail
    assert "everything else works" in check.detail
    assert check.fix and "install tmux" in check.fix


def test_tmux_below_the_recommended_version_is_ok_but_says_what_is_missing() -> None:
    older = diagnostics._check_tmux(FakeServer(version=(3, 3)))
    current = diagnostics._check_tmux(FakeServer(version=(3, 7)))

    assert older.status is CheckStatus.ok and "Shift+Enter" in older.detail
    assert current.status is CheckStatus.ok and "Shift+Enter" not in current.detail


def test_tmux_with_an_unreadable_version_fails_open_and_says_so() -> None:
    check = diagnostics._check_tmux(FakeServer(version=None))

    assert check.status is CheckStatus.ok
    assert "version not readable" in check.detail
    assert "fleet available" in check.detail


def test_tmux_check_never_raises_and_names_the_cost() -> None:
    check = diagnostics._check_tmux(FakeServer(version_raises=True))

    assert check.status is CheckStatus.ok
    assert "not evaluated" in check.detail
    assert "first spawn" in check.detail, "failing open must say what it cost"


def test_tmux_check_does_not_create_the_home(isolated_home: Path) -> None:
    """Real binary or none — the check runs before ``init`` and leaves no trace."""
    assert not isolated_home.exists()

    diagnostics._check_tmux()
    diagnostics._check_tmux(FakeServer())

    assert not isolated_home.exists()


# --- the gh check ------------------------------------------------------------------------


def test_gh_absent_warns_and_names_the_roles_that_need_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", _no_which)

    check = diagnostics._check_gh()

    assert check.status is CheckStatus.warn
    assert "PR flow for coder/reviewer needs it" in check.detail
    assert check.fix and "gh auth login" in check.fix


def test_gh_present_and_logged_in_is_ok_without_a_nag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        shutil, "which", lambda cmd, *a, **k: "/usr/bin/gh" if cmd == "gh" else None
    )
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    (tmp_path / "gh").mkdir()
    (tmp_path / "gh" / "hosts.yml").write_text("github.com:\n    user: someone\n", encoding="utf-8")

    check = diagnostics._check_gh()

    assert check.status is CheckStatus.ok
    assert "PR flow available" in check.detail
    assert "gh auth login" not in check.detail


def test_gh_present_but_not_logged_in_is_ok_with_the_login_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        shutil, "which", lambda cmd, *a, **k: "/usr/bin/gh" if cmd == "gh" else None
    )
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh-empty"))

    without_token = diagnostics._check_gh()
    monkeypatch.setenv("GH_TOKEN", "ghp_test")
    with_token = diagnostics._check_gh()

    assert without_token.status is CheckStatus.ok
    assert "gh auth login" in without_token.detail
    assert "gh auth login" not in with_token.detail, "a token IS a login"


def test_a_hosts_file_that_is_not_utf8_is_unreadable_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_gh_login_note`` promises "unreadable is treated as logged in"; a
    ``hosts.yml`` of non-UTF-8 bytes was the unreadability it did not survive.

    ``read_text`` answers those bytes with ``UnicodeDecodeError`` — a
    ``ValueError``, so the ``except OSError`` beside it never saw it, and the
    exception left ``_check_gh``, left ``doctor()``, and ended the one command
    that exists to diagnose damage in a Rich traceback (measured on the PR head:
    ``GH_CONFIG_DIR`` pointed at a 4-byte ``\\xff\\xfe\\x00bad`` hosts file,
    ``aisquare doctor`` exited 1).
    """
    monkeypatch.setattr(
        shutil, "which", lambda cmd, *a, **k: "/usr/bin/gh" if cmd == "gh" else None
    )
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    config = tmp_path / "gh-binary"
    config.mkdir()
    (config / "hosts.yml").write_bytes(b"\xff\xfe\x00bad")
    monkeypatch.setenv("GH_CONFIG_DIR", str(config))

    assert diagnostics._gh_login_note() == "", "undecodable is unreadable is logged in"
    check = diagnostics._check_gh()

    assert check.status is CheckStatus.ok
    assert "PR flow available" in check.detail
    assert "not evaluated" not in check.detail, "the note handled it; no need to fail open"


def test_gh_check_never_raises_and_names_the_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard every sibling check in this file has, and this one did not.

    Sabotaging the login note is the point: the crash-guard's job is to hold for
    the failure nobody predicted, so the input is a raising collaborator rather
    than a shape the code above already handles.
    """
    monkeypatch.setattr(
        shutil, "which", lambda cmd, *a, **k: "/usr/bin/gh" if cmd == "gh" else None
    )

    def boom() -> str:
        raise RuntimeError("gh config unreadable in a new way")

    monkeypatch.setattr(diagnostics, "_gh_login_note", boom)

    check = diagnostics._check_gh()

    assert check.status is CheckStatus.ok
    assert "not evaluated" in check.detail
    assert "gh config unreadable in a new way" in check.detail
    assert "the PR step reports" in check.detail, "failing open must say what it cost"


def test_doctor_survives_a_damaged_gh_config_end_to_end(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the CLI, because that is where the traceback was: ``doctor`` is the
    command an operator runs BECAUSE something is damaged.

    The gh row is asserted present and ok — the claim is about the artefact
    ``doctor`` produced, not merely about "no exception escaped", which a
    swallow upstream could satisfy while dropping the row entirely.
    """
    monkeypatch.setattr(
        shutil, "which", lambda cmd, *a, **k: "/usr/bin/gh" if cmd == "gh" else None
    )
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    config = tmp_path / "gh-damaged"
    config.mkdir()
    (config / "hosts.yml").write_bytes(b"\xff\xfe\x00bad")
    monkeypatch.setenv("GH_CONFIG_DIR", str(config))

    result = runner.invoke(app, ["--json", "doctor"])

    assert "Traceback" not in result.output, result.output
    rows = {row["name"]: row for row in json.loads(result.stdout)}
    assert rows["gh"]["status"] == "ok", rows["gh"]
    assert "PR flow" in rows["gh"]["detail"]


# --- the fleet check ----------------------------------------------------------------------


def test_fleet_check_on_a_fresh_home_declines_to_look(isolated_home: Path) -> None:
    """No home: no store, no tmux, the same "not created yet" the other store checks use."""
    built: list[str] = []

    def factory(socket: str) -> TmuxServer:
        built.append(socket)
        return FakeServer(socket=socket)

    check = diagnostics._check_fleet(factory)

    assert check.status is CheckStatus.ok
    assert "not created yet" in check.detail
    assert built == [], "the check asked tmux about a home that does not exist"
    assert not isolated_home.exists()


def test_fleet_check_without_tmux_is_not_evaluated(home: Path) -> None:
    check = diagnostics._check_fleet(lambda socket: FakeServer(present=False, socket=socket))

    assert check.status is CheckStatus.ok
    assert "not evaluated" in check.detail and "tmux" in check.detail


def test_fleet_check_is_ok_with_no_agents_and_no_server(home: Path) -> None:
    check = diagnostics._check_fleet(lambda socket: FakeServer(socket=socket))

    assert check.status is CheckStatus.ok
    assert "no fleet agents" in check.detail
    assert check.fix is None


def test_fleet_check_is_ok_when_every_live_row_has_its_pane(home: Path, tmp_path: Path) -> None:
    """The negative control: a healthy fleet must not be told to reap."""
    project = _seed(tmp_path / "repo")
    _seed(
        tmp_path / "repo", _agent(project.id, "manager", "%1"), _agent(project.id, "coder-1", "%2")
    )
    server = FakeServer(
        sessions=("asq-amber-otter",), panes={"%1": _facts("%1"), "%2": _facts("%2")}
    )

    check = diagnostics._check_fleet(lambda socket: server)

    assert check.status is CheckStatus.ok
    assert "2 live agent(s)" in check.detail and "1 session(s)" in check.detail
    assert "reap" not in check.detail and check.fix is None
    assert {"pane_facts:%1", "pane_facts:%2"} <= set(server.asked), "the rows were not checked"


def test_fleet_check_warns_when_a_live_row_has_no_pane(home: Path, tmp_path: Path) -> None:
    project = _seed(tmp_path / "repo")
    _seed(
        tmp_path / "repo", _agent(project.id, "manager", "%1"), _agent(project.id, "coder-1", "%2")
    )
    server = FakeServer(sessions=("asq-amber-otter",), panes={"%1": _facts("%1"), "%2": None})

    check = diagnostics._check_fleet(lambda socket: server)

    assert check.status is CheckStatus.warn
    assert "1 recorded live but the tmux pane is gone" in check.detail
    assert "coder-1" in check.detail and "manager" not in check.detail
    assert check.fix and "aisquare fleet reap" in check.fix


def test_fleet_check_reads_empty_facts_as_a_gone_pane(home: Path, tmp_path: Path) -> None:
    """tmux 3.7c answers a vanished target with exit 0 and every field empty (measured)."""
    project = _seed(tmp_path / "repo")
    _seed(tmp_path / "repo", _agent(project.id, "coder-1", "%2"))
    server = FakeServer(sessions=("asq-amber-otter",), panes={"%2": _facts("")})

    check = diagnostics._check_fleet(lambda socket: server)

    assert check.status is CheckStatus.warn
    assert "coder-1" in check.detail and "pane is gone" in check.detail


def test_fleet_check_treats_a_display_message_error_as_a_gone_pane(
    home: Path, tmp_path: Path
) -> None:
    """A malformed answer about one pane is that pane's problem, not the whole check's."""
    project = _seed(tmp_path / "repo")
    _seed(tmp_path / "repo", _agent(project.id, "coder-1", "%2"))
    server = FakeServer(sessions=("asq-amber-otter",), facts_raise=True)

    check = diagnostics._check_fleet(lambda socket: server)

    assert check.status is CheckStatus.warn
    assert "not evaluated" not in check.detail
    assert "coder-1" in check.detail


def test_fleet_check_warns_when_the_private_server_is_not_running(
    home: Path, tmp_path: Path
) -> None:
    project = _seed(tmp_path / "repo")
    _seed(tmp_path / "repo", _agent(project.id, "manager", "%1"))
    server = FakeServer(sessions=(), panes={})

    check = diagnostics._check_fleet(lambda socket: server)

    assert check.status is CheckStatus.warn
    assert "private tmux server 'asq' is not running" in check.detail
    assert "manager" in check.detail
    assert check.fix and "aisquare fleet reap" in check.fix


def test_fleet_check_names_exited_agents_still_recorded_live(home: Path, tmp_path: Path) -> None:
    """``remain-on-exit`` keeps the pane; the row stays live until reap records the exit."""
    project = _seed(tmp_path / "repo")
    _seed(tmp_path / "repo", _agent(project.id, "tester-1", "%3"))
    server = FakeServer(sessions=("asq-amber-otter",), panes={"%3": _facts("%3", dead=True)})

    check = diagnostics._check_fleet(lambda socket: server)

    assert check.status is CheckStatus.warn
    assert "1 exited but still recorded live" in check.detail and "tester-1" in check.detail
    assert check.fix and "fleet reap" in check.fix


def test_fleet_check_ignores_ended_rows(home: Path, tmp_path: Path) -> None:
    """An ended agent's pane is SUPPOSED to be gone; only live rows can be stale."""
    project = _seed(tmp_path / "repo")
    _seed(tmp_path / "repo", _agent(project.id, "coder-1", "%2", ended=True))
    server = FakeServer(sessions=(), panes={})

    check = diagnostics._check_fleet(lambda socket: server)

    assert check.status is CheckStatus.ok
    assert "pane_facts:%2" not in server.asked


def test_fleet_check_looks_on_the_socket_each_row_was_spawned_on(
    home: Path, tmp_path: Path
) -> None:
    """The socket is a default (§3.10); a row must not read as lost because the config moved."""
    project = _seed(tmp_path / "repo")
    _seed(tmp_path / "repo", _agent(project.id, "coder-1", "%5", socket="asq-old"))
    servers = {
        "asq": FakeServer(socket="asq"),
        "asq-old": FakeServer(socket="asq-old", sessions=("asq-x",), panes={"%5": _facts("%5")}),
    }

    check = diagnostics._check_fleet(lambda socket: servers[socket])

    assert check.status is CheckStatus.ok
    assert "pane_facts:%5" in servers["asq-old"].asked
    assert "pane_facts:%5" not in servers["asq"].asked


def test_fleet_check_caps_the_labels_it_lists(home: Path, tmp_path: Path) -> None:
    project = _seed(tmp_path / "repo")
    rows = [_agent(project.id, f"coder-{n}", f"%{n}") for n in range(1, 10)]
    _seed(tmp_path / "repo", *rows)
    server = FakeServer(sessions=("asq-amber-otter",), panes={})

    check = diagnostics._check_fleet(lambda socket: server)

    assert check.status is CheckStatus.warn
    assert "9 recorded live" in check.detail
    assert "+3 more" in check.detail
    assert "coder-9" not in check.detail


def test_fleet_check_fails_open_on_a_damaged_store(home: Path) -> None:
    paths.db_path().write_bytes(b"this is not a database")

    check = diagnostics._check_fleet(lambda socket: FakeServer(socket=socket))

    assert check.status is CheckStatus.ok
    reason = re.match(r"^not evaluated \((.+?)\) — ", check.detail)
    assert reason is not None and reason.group(1), (
        f"failing open must say what it could not read: {check.detail!r}"
    )
    assert "go unreported" in check.detail, "failing open must say what it cost"


def test_fleet_check_does_not_write_the_tmux_conf(home: Path) -> None:
    """Read-only in the fleet's own terms: ``argv`` would create the bundled conf; we do not."""
    conf = home / tmux_core.CONF_NAME
    assert not conf.exists()

    diagnostics._check_fleet(lambda socket: FakeServer(socket=socket))
    diagnostics._check_fleet()  # the real factory, against whatever tmux this machine has

    assert not conf.exists()


# --- once for real, on a private socket ------------------------------------------------------


@pytest.fixture
def private_tmux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TmuxServer]:
    """A real tmux server on a socket nobody else uses, killed on the way out."""
    socket = f"asq-test-{os.getpid()}-doctor"
    conf = tmp_path / "test-tmux.conf"
    conf.write_text("set -g remain-on-exit on\n", encoding="utf-8")
    server = TmuxServer(socket, conf=conf)
    monkeypatch.setattr(fleet_service, "settings", lambda: FleetSettings(tmux_socket=socket))
    try:
        yield server
    finally:
        with contextlib.suppress(TmuxError):
            server.run("kill-server")


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_real_tmux_reports_available_and_sees_a_pane_come_and_go(
    home: Path, tmp_path: Path, private_tmux: TmuxServer
) -> None:
    tmux = diagnostics._check_tmux()
    assert tmux.status is CheckStatus.ok, tmux.detail
    assert tmux.detail.startswith("tmux 3.") and "fleet available" in tmux.detail

    window = private_tmux.spawn_window(
        "asq-amber-otter", name="coder-1", cwd=tmp_path, command=["sleep", "30"]
    )
    project = _seed(tmp_path / "repo")
    _seed(
        tmp_path / "repo",
        _agent(project.id, "coder-1", window.pane_id, socket=private_tmux.socket),
    )

    healthy = diagnostics._check_fleet()
    private_tmux.kill_session("asq-amber-otter")
    stale = diagnostics._check_fleet()

    assert healthy.status is CheckStatus.ok, healthy.detail
    assert "1 live agent(s)" in healthy.detail
    assert stale.status is CheckStatus.warn, stale.detail
    assert "coder-1" in stale.detail and stale.fix and "fleet reap" in stale.fix
    assert not (home / tmux_core.CONF_NAME).exists(), "doctor wrote the fleet's conf"


# --- doctor(cwd=…) ----------------------------------------------------------------------------


def _ready_snapshot(project_id: str) -> None:
    directory = snapshot_core.snapshot_dir(project_id)
    directory.mkdir(parents=True, exist_ok=True)
    meta = Snapshot(
        project_id=project_id,
        generated_at=datetime.now(tz=UTC),
        pack_path=snapshot_core.pack_path(project_id),
        skeleton_path=snapshot_core.skeleton_path(project_id),
        index_path=snapshot_core.index_path(project_id),
        token_count=42,
        file_count=3,
    )
    snapshot_core.meta_path(project_id).write_text(meta.model_dump_json(), encoding="utf-8")


def _too_large_snapshot(project_id: str) -> Snapshot:
    directory = snapshot_core.snapshot_dir(project_id)
    directory.mkdir(parents=True, exist_ok=True)
    meta = Snapshot(
        project_id=project_id,
        generated_at=datetime.now(tz=UTC),
        pack_path=snapshot_core.pack_path(project_id),
        skeleton_path=snapshot_core.skeleton_path(project_id),
        index_path=snapshot_core.index_path(project_id),
        token_count=203_991,
        compressed=True,
        status="too_large",
        full_token_count=412_318,
        max_tokens=150_000,
    )
    snapshot_core.meta_path(project_id).write_text(meta.model_dump_json(), encoding="utf-8")
    return meta


def test_doctor_reports_an_over_budget_snapshot_with_its_numbers_and_a_re_pack(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#82: a ``too_large`` verdict is not "no codebase snapshot" — it has numbers and a way out.

    The fix is ``--refresh`` on purpose: the old hint, ``Pack one: aisquare
    project onboard``, reloaded the same verdict, which is how the line stayed a
    warning forever. The detail is the SAME sentence ``project onboard`` prints,
    by identity, so the two can never disagree on the numbers; and the UI turns
    the hint into the ``--refresh`` button its fix table already knows.
    """
    root = tmp_path / "big"
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setattr(brain_core, "gbrain_version", lambda: "9.9")
    monkeypatch.setattr(brain_core, "brain_ready", lambda project_id: False)
    verdict = _too_large_snapshot(_seed(root).id)

    check = _by_name(diagnostics.doctor())["snapshot"]

    assert check.status is CheckStatus.warn
    assert check.detail == snapshot_core.too_large_detail(verdict)
    assert "full 412318 tokens, compressed 203991 tokens, budget 150000" in check.detail
    assert check.fix == "Re-pack: aisquare project onboard --refresh"
    assert [fix.argv for fix in fix_commands([check])] == [("project", "onboard", "--refresh")]


def test_doctor_cwd_selects_the_project_for_the_project_scoped_checks(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two directories, one process: ``cwd`` decides which project the three checks describe."""
    here = tmp_path / "here"
    there = tmp_path / "there"
    here.mkdir()
    there.mkdir()
    monkeypatch.chdir(here)
    monkeypatch.setattr(brain_core, "gbrain_version", lambda: "9.9")
    monkeypatch.setattr(brain_core, "brain_ready", lambda project_id: False)
    # Activate the orchestrator in `there` only, with an off-ladder live session.
    monkeypatch.setenv("AISQUARE_ROLE", "runner")
    team_service.hook_session_start("sess-there-1", there, "startup", model="claude-haiku-4-5")
    _ready_snapshot(_seed(there).id)

    there_rows = _by_name(diagnostics.doctor(cwd=there))
    here_rows = _by_name(diagnostics.doctor())

    assert there_rows["snapshot"].status is CheckStatus.ok
    assert "3 files, 42 tokens" in there_rows["snapshot"].detail
    assert there_rows["agent harness"].status is CheckStatus.warn
    assert "off-ladder" in there_rows["agent harness"].detail
    assert there_rows["brain"].status is CheckStatus.warn
    assert "not initialised" in there_rows["brain"].detail
    # The control: the process cwd is a different, never-activated project.
    assert here_rows["snapshot"].status is CheckStatus.warn
    assert here_rows["agent harness"].detail == "not activated for this project"
    assert "orchestrator not active here" in here_rows["brain"].detail


def test_doctor_without_cwd_means_the_process_cwd(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default is unchanged: no argument and ``Path.cwd()`` describe the same project."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("AISQUARE_ROLE", "runner")
    team_service.hook_session_start("sess-work-1", work, "startup", model="claude-haiku-4-5")

    implicit = _by_name(diagnostics.doctor())["agent harness"]
    explicit = _by_name(diagnostics.doctor(cwd=Path.cwd()))["agent harness"]
    elsewhere = _by_name(diagnostics.doctor(cwd=tmp_path))["agent harness"]

    assert implicit == explicit
    assert implicit.status is CheckStatus.warn
    assert elsewhere.detail == "not activated for this project"


# --- the Claude Code version, read from disk -----------------------------------------------


def test_claude_version_from_the_native_installer_layout(tmp_path: Path) -> None:
    target = tmp_path / "share" / "claude" / "versions" / "2.1.250"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\x7fELF")
    link = tmp_path / "bin" / "claude"
    link.parent.mkdir()
    link.symlink_to(target)

    assert diagnostics.claude_code_version(str(link)) == "2.1.250"


def test_claude_version_from_an_npm_layout(tmp_path: Path) -> None:
    package = tmp_path / "lib" / "node_modules" / "@anthropic-ai" / "claude-code"
    package.mkdir(parents=True)
    (package / "cli.js").write_text("#!/usr/bin/env node\n", encoding="utf-8")
    (package / "package.json").write_text(json.dumps({"version": "2.1.9"}), encoding="utf-8")
    link = tmp_path / "bin" / "claude"
    link.parent.mkdir()
    link.symlink_to(package / "cli.js")

    assert diagnostics.claude_code_version(str(link)) == "2.1.9"


def test_claude_version_is_none_when_the_layout_is_unknown_or_damaged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = tmp_path / "claude"
    plain.write_text("#!/bin/sh\n", encoding="utf-8")
    package = tmp_path / "npm" / "claude-code"
    package.mkdir(parents=True)
    (package / "package.json").write_text("{not json", encoding="utf-8")
    (package / "cli.js").write_text("", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", _no_which)

    assert diagnostics.claude_code_version(str(plain)) is None
    assert diagnostics.claude_code_version(str(package / "cli.js")) is None
    assert diagnostics.claude_code_version() is None


def test_claude_code_check_carries_the_version_when_known(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    monkeypatch.setattr("aisquare.core.agents._home", lambda: fake_home)
    runner.invoke(app, ["agents", "connect", "claude-code"], catch_exceptions=False)

    monkeypatch.setattr(diagnostics, "claude_code_version", lambda binary=None: "2.1.250")
    with_version = diagnostics._check_claude_code()
    monkeypatch.setattr(diagnostics, "claude_code_version", lambda binary=None: None)
    without = diagnostics._check_claude_code()

    assert with_version.status is CheckStatus.ok
    assert with_version.detail.startswith("Claude Code 2.1.250 connected")
    assert without.detail.startswith("Claude Code connected"), "no version, no gap"


# --- the CLI surface stays what the suite reads --------------------------------------------------


def test_cli_doctor_emits_the_fleet_rows_in_text_and_json(runner: CliRunner) -> None:
    text = runner.invoke(app, ["doctor"], catch_exceptions=False).output
    payload = json.loads(runner.invoke(app, ["--json", "doctor"], catch_exceptions=False).stdout)

    names = [row["name"] for row in payload]
    for name in ("tmux", "gh", "fleet"):
        assert name in names, names
        assert f" {name}: " in text, text
    assert names.index("claude-code") < names.index("tmux") < names.index("gh")
    assert names.index("snapshot") < names.index("fleet") < names.index("explainability")


def test_a_healthy_machine_yields_no_fleet_warning(runner: CliRunner) -> None:
    """After ``init`` and before any spawn, the fleet row is ok whatever tmux this machine has."""
    runner.invoke(app, ["init", "--no-onboard"], catch_exceptions=False)

    rows = {row["name"]: row for row in json.loads(runner.invoke(app, ["--json", "doctor"]).stdout)}

    assert rows["fleet"]["status"] == "ok", rows["fleet"]
    assert rows["fleet"]["fix"] is None


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_a_stale_row_reaches_the_cli_as_a_warning_without_failing_doctor(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive half of the test above, through the CLI, on a socket nobody runs."""
    monkeypatch.setattr(
        fleet_service,
        "settings",
        lambda: FleetSettings(tmux_socket=f"asq-test-{os.getpid()}-doctor"),
    )
    runner.invoke(app, ["init", "--no-onboard"], catch_exceptions=False)
    project = _seed(tmp_path / "repo")
    _seed(
        tmp_path / "repo",
        _agent(project.id, "coder-1", "%7", socket=f"asq-test-{os.getpid()}-doctor"),
    )

    result = runner.invoke(app, ["--json", "doctor"])
    rows = {row["name"]: row for row in json.loads(result.stdout)}

    assert rows["fleet"]["status"] == "warn", rows["fleet"]
    assert "aisquare fleet reap" in rows["fleet"]["fix"]
    assert result.exit_code == 0, "a stale fleet row is advice, not a broken machine"


# --- a home that cannot be created: one wrong verdict is worse than a deferral ----------
#
# ``_check_home`` asked ``home.exists()``, which is true of a regular FILE, so the
# one check whose entire job is the home reported ``ok`` for a home that can never
# be created — and the failure surfaced two rows later as ``context.db is
# unreadable: [Errno 17] File exists`` carrying the corrupt-store remedy, a
# ``mv <file>/context.db …`` that cannot run because there is no directory to move
# anything out of. Measured with ``AISQUARE_HOME=<a 1-byte file> asq --json doctor``.


def _file_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``AISQUARE_HOME`` pointing at a regular file — a foreseeable operator mistake."""
    blocked = tmp_path / "homefile"
    blocked.write_text("x", encoding="utf-8")
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(blocked))
    return blocked


def test_a_file_home_fails_the_home_check_and_says_what_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = _file_home(tmp_path, monkeypatch)

    rows = _by_name(diagnostics.doctor())

    assert rows["home"].status is CheckStatus.fail, rows["home"]
    assert "not a directory" in rows["home"].detail
    assert str(blocked) in rows["home"].detail
    assert rows["home"].fix is not None and "AISQUARE_HOME" in rows["home"].fix
    # Diagnosis must not be a side effect — least of all overwriting the file.
    assert blocked.read_text(encoding="utf-8") == "x"


def test_no_check_offers_a_remedy_inside_a_home_that_is_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The artefact this is about: the FIX TEXT an operator would paste.

    ``database`` was measured printing ``mv <homefile>/context.db
    <homefile>/context.db.broken && aisquare init``. Nothing under a regular file
    can be moved, so that command fails with ENOTDIR and the real answer — repoint
    AISQUARE_HOME — appears nowhere.
    """
    blocked = _file_home(tmp_path, monkeypatch)

    rows = _by_name(diagnostics.doctor())

    inside = f"{blocked}{os.sep}"
    offenders = {name: row.fix for name, row in rows.items() if row.fix and inside in row.fix}
    assert not offenders, offenders
    assert rows["database"].status is CheckStatus.ok, rows["database"]
    assert "not a directory" in rows["database"].detail
    assert "home" in rows["database"].detail, "it points at the check that owns the verdict"


def test_the_cli_reports_a_file_home_as_json_and_not_a_traceback(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--json`` paths emit JSON or nothing, and ``doctor`` never crashes.

    Exit 1 because a check failed, which is ``doctor``'s contract — the point is
    that stdout is still the machine-readable report and not an empty string with
    a traceback beside it.
    """
    _file_home(tmp_path, monkeypatch)

    result = runner.invoke(app, ["--json", "doctor"])

    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output, result.output
    rows = {row["name"]: row for row in json.loads(result.stdout)}
    assert rows["home"]["status"] == "fail", rows["home"]
    assert [name for name, row in rows.items() if row["status"] == "fail"] == ["home"], rows


def test_a_directory_home_is_still_ok_and_a_missing_one_still_says_init(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two negative controls. Without them the cheapest way to pass the three
    tests above is a ``home`` check that fails for every machine, and a
    ``_uncreated_home`` that defers every downstream check forever."""
    real = _by_name(diagnostics.doctor())

    assert real["home"].status is CheckStatus.ok, real["home"]
    assert real["database"].status is CheckStatus.ok, real["database"]
    assert "unreadable" not in real["database"].detail

    missing = tmp_path / "never-created"
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(missing))
    absent = _by_name(diagnostics.doctor())

    assert absent["home"].status is CheckStatus.fail
    assert "is missing" in absent["home"].detail
    assert absent["home"].fix is not None and "aisquare init" in absent["home"].fix
    assert absent["database"].detail.startswith("not created yet")
    assert not missing.exists(), "doctor must not create the home it reports on"


def test_a_symlinked_home_is_a_directory_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shape the ``is_dir()`` test must NOT accuse: ``ensure_home`` follows a
    link to a directory and everything works, so the check has to agree."""
    target = tmp_path / "real-home"
    target.mkdir()
    link = tmp_path / "linked-home"
    link.symlink_to(target)
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(link))

    rows = _by_name(diagnostics.doctor())

    assert rows["home"].status is CheckStatus.ok, rows["home"]
    assert rows["database"].status is CheckStatus.ok, rows["database"]
