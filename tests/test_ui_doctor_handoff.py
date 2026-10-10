"""Update and Uninstall in asq's Doctor: thin hand-offs of the terminal (roadmap 9.1d).

The buttons only post :class:`DoctorView.HandOff`; the app quits and ``run_ui`` hands
this terminal to ``aisquare upgrade --reopen`` or ``aisquare uninstall`` through
``selfcli.exec_self``, where each command shows its own plan and asks y/N. After an
upgrade that did not fail, ``--reopen`` opens asq again. Every seam that would
leave the process is replaced here; each claim has its negative control beside it.
"""

from __future__ import annotations

import asyncio
import builtins
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.message import Message
from textual.pilot import Pilot
from textual.widgets import Button, Static
from typer.testing import CliRunner

from aisquare.cli import install as install_cli
from aisquare.cli.app import app as cli_app
from aisquare.cli.ui import app as ui_app
from aisquare.cli.ui.sidebar import AddProject, ProjectSelected
from aisquare.cli.ui.views.doctor import DoctorView
from aisquare.cli.ui.views.onboard import OnboardView
from aisquare.cli.ui.views.welcome import WelcomeView
from aisquare.core import selfcli
from aisquare.services.install_route import LatestRelease
from tests.installer_seams import no_real_installer  # noqa: F401 — autouse, applied by import
from tests.rendered import plain
from tests.test_lifecycle_upgrade import Machine, Tool, machine, tool  # noqa: F401
from tests.test_ui_shell import drive, script, seed  # noqa: F401 — `script` is a fixture
from tests.ui_workers import settle_page

#: The real function, taken before ``no_real_installer`` closes it for every test here.
_REAL_EXEC_SELF = selfcli.exec_self

# --------------------------------------------------------------------------- the view


class Host(App[None]):
    def __init__(self, view: DoctorView) -> None:
        super().__init__()
        self.view = view
        self.received: list[Message] = []

    def compose(self) -> ComposeResult:
        yield self.view

    def on_doctor_view_hand_off(self, message: DoctorView.HandOff) -> None:
        self.received.append(message)


def _pressed(view: DoctorView, *button_ids: str) -> tuple[dict[str, bool], Host, str]:
    """Mount ``view`` and press each button: its machine buttons (id → enabled) and note."""

    async def drive_view() -> tuple[dict[str, bool], Host, str]:
        host = Host(view)
        async with host.run_test(size=(120, 40)):
            await settle_page(host)
            shown = {
                str(b.id): not b.disabled
                for b in view.query("#doctor-machine Button").results(Button)
            }
            for button_id in button_ids:
                view.query_one(f"#{button_id}", Button).press()
                await settle_page(host)
            notes = view.query("#doctor-update-note").results(Static)
            return shown, host, " ".join(str(note.render()) for note in notes)

    return asyncio.run(drive_view())


def _never_refused() -> str | None:
    return None


def test_the_machine_doctor_hands_off_update_and_uninstall() -> None:
    view = DoctorView(machine=True, update_refusal=_never_refused)
    shown, host, note = _pressed(view, "doctor-update", "doctor-uninstall")

    assert shown == {"doctor-update": True, "doctor-uninstall": True} and note == ""
    handed = [m.args for m in host.received if isinstance(m, DoctorView.HandOff)]
    assert handed == [("upgrade", "--reopen"), ("uninstall", "--reopen")]


def test_a_project_or_onboard_doctor_has_neither_button() -> None:
    """Control: the same view without ``machine`` — the Onboard and Project copies."""
    shown, host, _note = _pressed(DoctorView())

    assert shown == {} and host.received == []


def test_update_is_disabled_with_the_reason_where_upgrade_would_only_refuse() -> None:
    """An editable checkout, pipx, Homebrew, a venv, system pip, Windows: the button
    would only quit asq into a refusal, so it says what to run instead."""
    refusal = "aisquare does not upgrade this install itself (pipx). Upgrade it with: pipx upgrade"
    view = DoctorView(machine=True, update_refusal=lambda: refusal)
    shown, host, note = _pressed(view, "doctor-update", "doctor-uninstall")

    assert shown == {"doctor-update": False, "doctor-uninstall": True}
    assert note == f"Update: {refusal}"
    handed = [m.args for m in host.received if isinstance(m, DoctorView.HandOff)]
    assert handed == [("uninstall", "--reopen")], "Uninstall still runs"


# --------------------------------------------------------------------------- the app


def test_only_asqs_own_doctor_is_machine_wide_and_no_doctor_may_be_busy(
    isolated_home: Path,
    tmp_path: Path,
    script: Any,  # noqa: F811 — pytest resolves fixtures by NAME, so the import must keep it
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a project's tab and Onboard really open, so the negative half has copies to see.
    Update quits only when no Doctor view anywhere still has a fix writing."""
    monkeypatch.setattr("aisquare.cli.ui.views.doctor._update_refusal", _never_refused)
    seed(tmp_path, ("prj_a", "alpha", None))

    async def fn(pilot: Pilot[None]) -> tuple[dict[str, bool], tuple[str, ...] | None, Any]:
        app = pilot.app
        assert isinstance(app, ui_app.FleetApp)
        await app.on_project_selected(ProjectSelected("prj_a"))
        await app.on_add_project(AddProject())
        await settle_page(app)
        doctors = {str(view.id): view.machine for view in app.query(DoctorView)}
        project = next(v for v in app.query(DoctorView) if v.id == "project-doctor")
        project.busy = True
        app.query_one("#doctor-update", Button).press()
        await settle_page(app)
        refused = app.hand_off
        project.busy = False
        app.query_one("#doctor-update", Button).press()
        await settle_page(app)
        return doctors, refused, app.hand_off

    doctors, while_busy, after = drive(fn)

    assert doctors == {"doctor": True, "project-doctor": False, "onboard-doctor": False}
    assert while_busy is None, "a project tab's fix is still writing"
    assert after == ("upgrade", "--reopen"), "control: with no fix running it quits"


@pytest.mark.parametrize(
    "work",
    ["welcome-onboard", "welcome-connect", "welcome-manager", "welcome-coders", "onboard-init"],
)
def test_update_waits_for_welcome_and_onboard_work_as_for_a_fix(
    isolated_home: Path,
    script: Any,  # noqa: F811 — pytest resolves fixtures by NAME, so the import must keep it
    monkeypatch: pytest.MonkeyPatch,
    work: str,
) -> None:
    """Update and Uninstall waited only for Doctor-view fixes. Welcome's setup (init, then
    doctor), its Connect and its fleet starts, and the Onboard view's init, were cut off:
    asq quit, asyncio.run joined their thread with the terminal blank, their outcome was
    never shown, and the hand-over could replace the install under a running init (round
    15 of #257). Each is waited for, with the toast saying so."""
    monkeypatch.setattr("aisquare.cli.ui.views.doctor._update_refusal", _never_refused)

    async def fn(pilot: Pilot[None]) -> tuple[Any, list[str], Any]:
        app = pilot.app
        assert isinstance(app, ui_app.FleetApp)
        await app.on_add_project(AddProject())  # the Onboard view, really open
        await settle_page(app)
        said: list[str] = []
        monkeypatch.setattr(app, "notify", lambda message, **_: said.append(str(message)))
        welcome = app.query_one(WelcomeView)
        onboard = app.query_one(OnboardView)
        if work == "onboard-init":
            onboard.running = True
        else:
            welcome.busy.add(work.removeprefix("welcome-"))
        app.query_one("#doctor-update", Button).press()
        await settle_page(app)
        refused = app.hand_off
        onboard.running = False
        welcome.busy.clear()
        app.query_one("#doctor-update", Button).press()
        await settle_page(app)
        return refused, said, app.hand_off

    while_busy, said, after = drive(fn)

    assert while_busy is None, f"{work} is still running"
    assert said == ["a fix, a setup or a start is still running — try again when it ends"]
    assert after == ("upgrade", "--reopen"), "control: once it has ended, Update quits"


class _FakeApp:
    def __init__(self, hand_off: tuple[str, ...] | None) -> None:
        self.hand_off = hand_off
        self.unsaved = ["the last layout could not be saved"]

    def run(self) -> None:
        return None


@pytest.mark.parametrize("hand_off", [("uninstall",), None], ids=["handed-off", "plain-quit"])
def test_run_ui_hands_the_terminal_over_only_when_asked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], hand_off: Any
) -> None:
    execs: list[tuple[str, ...]] = []
    monkeypatch.setattr(ui_app, "FleetApp", lambda **_options: _FakeApp(hand_off))
    monkeypatch.setattr(selfcli, "exec_self", lambda args: execs.append(tuple(args)))

    ui_app.run_ui()

    assert execs == ([hand_off] if hand_off else []), "a plain quit hands nothing over"
    assert "the last layout could not be saved" in capsys.readouterr().err, "said first"


# --------------------------------------------------------------------------- the exec


def test_exec_self_replaces_this_process_with_this_installs_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "execv", lambda path, argv: calls.append((path, list(argv))))

    _REAL_EXEC_SELF(["upgrade", "--reopen"])

    argv = [sys.executable, "-P", "-m", "aisquare", "upgrade", "--reopen"]
    assert calls == [(sys.executable, argv)]


def test_exec_self_on_windows_waits_for_the_child_and_exits_with_its_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows' ``execv`` returns the prompt to the shell while the child still asks y/N."""
    children: list[list[str]] = []

    def call(argv: list[str]) -> int:
        children.append(argv)
        return 3

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "execv", lambda *_a: pytest.fail("no exec on Windows"))
    monkeypatch.setattr(subprocess, "call", call)

    with pytest.raises(SystemExit) as exited:
        _REAL_EXEC_SELF(["uninstall"])

    assert exited.value.code == 3
    assert children == [[sys.executable, "-P", "-m", "aisquare", "uninstall"]]


# --------------------------------------------------------------------------- upgrade --reopen


@dataclass
class Terminal:
    """A terminal for ``--reopen``: the Enter prompts it showed and what exec'd after."""

    prompts: list[str] = field(default_factory=list)
    reopened: list[list[str]] = field(default_factory=list)
    answer: BaseException | None = None
    """Raised at the prompt instead of returning (Ctrl-C, a closed stdin)."""


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> Terminal:
    at = Terminal()

    def enter(prompt: str = "") -> str:
        at.prompts.append(prompt)
        if at.answer is not None:
            raise at.answer
        return ""

    monkeypatch.setattr(install_cli, "_stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(builtins, "input", enter)
    monkeypatch.setattr(selfcli, "exec_self", lambda args: at.reopened.append(list(args)))
    return at


_ENTER = "Press Enter to go back to asq "


def test_reopen_waits_for_enter_then_opens_asq_after_an_upgrade_that_did_not_fail(
    runner: CliRunner,
    tool: Tool,  # noqa: F811
    machine: Machine,  # noqa: F811
    terminal: Terminal,
) -> None:
    """asq opens on the alternate screen: the report must be read before it goes."""
    upgraded = runner.invoke(cli_app, ["upgrade", "--yes", "--reopen"])
    after_upgrade = (list(terminal.prompts), list(terminal.reopened))
    plain = runner.invoke(cli_app, ["upgrade", "--yes"])

    assert upgraded.exit_code == 0, upgraded.output
    assert len(machine.installs) == 2
    assert after_upgrade == ([_ENTER], [["ui"]])
    assert plain.exit_code == 0 and terminal.reopened == [["ui"]], "control: no --reopen, no asq"


def test_reopen_opens_asq_when_there_was_nothing_to_do_or_the_answer_was_no(
    runner: CliRunner,
    tool: Tool,  # noqa: F811
    machine: Machine,  # noqa: F811
    terminal: Terminal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user pressed Update in asq; declining, or being current, returns them there."""
    machine.latest = LatestRelease("0.9.0")
    current = runner.invoke(cli_app, ["upgrade", "--reopen"])
    machine.latest = LatestRelease("0.9.1")
    monkeypatch.setattr("aisquare.cli.install.typer.confirm", lambda *_a, **_k: False)
    declined = runner.invoke(cli_app, ["upgrade", "--reopen"])

    assert current.exit_code == 0 and "nothing to do" in current.stdout, current.output
    assert declined.exit_code == 0 and "nothing changed" in declined.stdout, declined.output
    assert terminal.reopened == [["ui"], ["ui"]] and machine.installs == []


def test_reopen_stays_out_of_the_way_of_a_failure_json_ctrl_c_and_no_terminal(
    runner: CliRunner,
    tool: Tool,  # noqa: F811
    machine: Machine,  # noqa: F811
    terminal: Terminal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure stays on screen to be read; ``--json`` output is for a program; Ctrl-C at
    the prompt and a run with nobody at the terminal stay in this shell."""
    as_json = runner.invoke(cli_app, ["--json", "upgrade", "--yes", "--reopen"])
    terminal.answer = KeyboardInterrupt()
    interrupted = runner.invoke(cli_app, ["upgrade", "--yes", "--reopen"])
    monkeypatch.setattr(install_cli, "_stdin_is_a_terminal", lambda: False)
    unattended = runner.invoke(cli_app, ["upgrade", "--yes", "--reopen"])
    machine.installer_exit = 1
    failed = runner.invoke(cli_app, ["upgrade", "--yes", "--reopen"])

    assert as_json.exit_code == 0 and interrupted.exit_code == 0 and unattended.exit_code == 0
    assert failed.exit_code == 1 and "the upgrade failed" in failed.stderr, failed.output
    assert terminal.prompts == [_ENTER], "asked once: the interrupted run"
    assert terminal.reopened == []


def test_reopen_is_hidden_from_help(runner: CliRunner) -> None:
    """asq passes it; a person typing `aisquare upgrade` has no use for it."""
    shown = runner.invoke(cli_app, ["upgrade", "--help"])
    text = plain(shown.stdout)  # styled on CI, where typer forces a terminal

    assert shown.exit_code == 0 and "--dry-run" in text, "control: help lists flags"
    assert "--reopen" not in text
