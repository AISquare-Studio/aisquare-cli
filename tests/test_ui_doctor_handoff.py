"""Update and Uninstall in asq's Doctor: thin hand-offs of the terminal (roadmap 9.1d).

The buttons only post :class:`DoctorView.HandOff`; the app quits and ``run_ui`` hands
this terminal to ``aisquare upgrade --reopen`` or ``aisquare uninstall`` through
``selfcli.exec_self``, where each command shows its own plan and asks y/N. After an
upgrade that did not fail, ``--reopen`` opens asq again. Every seam that would
leave the process is replaced here; each claim has its negative control beside it.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
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
from aisquare.cli.ui.views.doctor import DoctorView
from aisquare.core import selfcli
from aisquare.services.install_route import LatestRelease
from tests.installer_seams import no_real_installer  # noqa: F401 — autouse, applied by import
from tests.test_lifecycle_upgrade import Machine, Tool, machine, tool  # noqa: F401
from tests.test_ui_shell import drive
from tests.ui_workers import settle_page

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


def _pressed(view: DoctorView, *button_ids: str, busy: bool = False) -> tuple[list[str], Host, str]:
    """Mount ``view``, press each button; the machine buttons it showed, and its status line."""

    async def drive_view() -> tuple[list[str], Host, str]:
        host = Host(view)
        async with host.run_test(size=(120, 40)):
            await settle_page(host)
            shown = [str(b.id) for b in view.query("#doctor-machine Button").results(Button)]
            view.busy = busy
            for button_id in button_ids:
                view.query_one(f"#{button_id}", Button).press()
                await settle_page(host)
            return shown, host, str(view.query_one("#doctor-status", Static).render())

    return asyncio.run(drive_view())


def test_the_machine_doctor_hands_off_update_and_uninstall() -> None:
    shown, host, _status = _pressed(DoctorView(machine=True), "doctor-update", "doctor-uninstall")

    assert shown == ["doctor-update", "doctor-uninstall"]
    handed = [m.args for m in host.received if isinstance(m, DoctorView.HandOff)]
    assert handed == [("upgrade", "--reopen"), ("uninstall",)]


def test_a_project_or_onboard_doctor_has_neither_button() -> None:
    """Control: the same view without ``machine`` — the Onboard and Project copies."""
    shown, host, _status = _pressed(DoctorView())

    assert shown == [] and host.received == []


def test_no_hand_off_while_a_fix_is_still_running() -> None:
    shown, host, status = _pressed(DoctorView(machine=True), "doctor-update", busy=True)

    assert shown == ["doctor-update", "doctor-uninstall"]
    assert host.received == [], "quitting under a fix that is writing would cut it off"
    assert "a fix is still running" in status


# --------------------------------------------------------------------------- the app


def test_asqs_own_doctor_is_the_only_machine_one_and_update_quits_with_the_hand_off() -> None:
    async def fn(pilot: Pilot[None]) -> tuple[list[str], tuple[str, ...] | None, bool]:
        app = pilot.app
        assert isinstance(app, ui_app.FleetApp)
        machines = [str(view.id) for view in app.query(DoctorView) if view.machine]
        before = app.hand_off
        app.query_one("#doctor-update", Button).press()
        await settle_page(app)
        return machines, app.hand_off, before is None

    machines, hand_off, unset_before = drive(fn)

    assert machines == ["doctor"]
    assert unset_before, "control: nothing is handed off until the button is pressed"
    assert hand_off == ("upgrade", "--reopen")


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

    selfcli.exec_self(["upgrade", "--reopen"])

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
        selfcli.exec_self(["uninstall"])

    assert exited.value.code == 3
    assert children == [[sys.executable, "-P", "-m", "aisquare", "uninstall"]]


# --------------------------------------------------------------------------- upgrade --reopen


@pytest.fixture
def reopened(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    seen: list[list[str]] = []
    monkeypatch.setattr(selfcli, "exec_self", lambda args: seen.append(list(args)))
    return seen


def test_reopen_opens_asq_after_an_upgrade_that_did_not_fail(
    runner: CliRunner,
    tool: Tool,  # noqa: F811
    machine: Machine,  # noqa: F811
    reopened: list[list[str]],
) -> None:
    upgraded = runner.invoke(cli_app, ["upgrade", "--yes", "--reopen"])
    after_upgrade = list(reopened)
    plain = runner.invoke(cli_app, ["upgrade", "--yes"])

    assert upgraded.exit_code == 0, upgraded.output
    assert len(machine.installs) == 2
    assert after_upgrade == [["ui"]]
    assert plain.exit_code == 0 and reopened == [["ui"]], "control: no --reopen, no asq"


def test_reopen_opens_asq_when_there_was_nothing_to_do_or_the_answer_was_no(
    runner: CliRunner,
    tool: Tool,  # noqa: F811
    machine: Machine,  # noqa: F811
    reopened: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user pressed Update in asq; declining, or being current, returns them there."""
    machine.latest = LatestRelease("0.9.0")
    current = runner.invoke(cli_app, ["upgrade", "--reopen"])
    machine.latest = LatestRelease("0.9.1")
    monkeypatch.setattr(install_cli, "_stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr("aisquare.cli.install.typer.confirm", lambda *_a, **_k: False)
    declined = runner.invoke(cli_app, ["upgrade", "--reopen"])

    assert current.exit_code == 0 and "nothing to do" in current.stdout, current.output
    assert declined.exit_code == 0 and "nothing changed" in declined.stdout, declined.output
    assert reopened == [["ui"], ["ui"]] and machine.installs == []


def test_reopen_stays_out_of_the_way_of_a_failure_and_of_json(
    runner: CliRunner,
    tool: Tool,  # noqa: F811
    machine: Machine,  # noqa: F811
    reopened: list[list[str]],
) -> None:
    """A failure stays on screen to be read; ``--json`` output is for a program."""
    as_json = runner.invoke(cli_app, ["--json", "upgrade", "--yes", "--reopen"])
    machine.installer_exit = 1
    failed = runner.invoke(cli_app, ["upgrade", "--yes", "--reopen"])

    assert as_json.exit_code == 0, as_json.output
    assert failed.exit_code == 1 and "the upgrade failed" in failed.stderr, failed.output
    assert reopened == []


def test_reopen_is_hidden_from_help(runner: CliRunner) -> None:
    """asq passes it; a person typing `aisquare upgrade` has no use for it."""
    shown = runner.invoke(cli_app, ["upgrade", "--help"])

    assert shown.exit_code == 0 and "--dry-run" in shown.stdout, "control: help lists flags"
    assert "--reopen" not in shown.stdout
