"""Codex parity on the 0.8.0 UI beyond Welcome: Settings, the spawn dialog, Doctor, sidebar.

- **Settings** offers Codex 0.162.1's approval policies (no ``untrusted``), the role's
  ``approve_for_me``, and under each role the flags its next ``fleet spawn`` passes —
  the adapter's own ``fleet_args`` — or the refusal it would meet.
- **The spawn dialog** offers the account and the permission in the launch agent's own
  words, and passes them to ``fleet.spawn`` as ``fleet spawn`` does.
- **Doctor** tells a partial Codex install from a missing one, gives the trust row its
  real step instead of a Connect that cannot trust, and makes a slot's sign-in one
  click (the Accounts page's pane), never a ``--json`` run.
- **The sidebar** does not call a Codex-only machine "Claude Code not installed".
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from textual.app import App, ComposeResult
from textual.message import Message
from textual.pilot import Pilot
from textual.widgets import Button, Select, Static

from aisquare.cli.ui.sidebar import AccountsSelected
from aisquare.cli.ui.views.accounts import summarise
from aisquare.cli.ui.views.doctor import DoctorView
from aisquare.cli.ui.views.project import ProjectView
from aisquare.cli.ui.views.settings import (
    APPROVAL_OPTIONS,
    SettingsView,
    account_options,
    launch_line,
)
from aisquare.cli.ui.views.spawn import SpawnScreen
from aisquare.core import claude_accounts as accounts_core
from aisquare.core.config import FleetRoleSettings, load_config, save_config
from aisquare.core.orchestrator import team_project
from aisquare.models import (
    AccountsOverview,
    CheckStatus,
    ClaudeAccount,
    ClaudeAccountStatus,
    ClaudeIdentity,
    ClaudeInstall,
    DoctorCheck,
    FleetAgent,
)
from aisquare.services import agent_launch, diagnostics, fleet, onboarding
from aisquare.services import agents as agents_service
from tests.test_ui_project import Host, drive
from tests.ui_workers import settle_page

# --------------------------------------------------------------------------- Settings


def test_settings_offer_codexs_approval_policies_and_keep_an_old_untrusted_shown(
    tmp_path: Path,
) -> None:
    assert [value for _label, value in APPROVAL_OPTIONS] == ["", "on-request", "never"]
    config = load_config()
    config.fleet.roles["coder"].approval_policy = "untrusted"  # saved by an older version
    save_config(config)

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[Any, str]:
        host.query_one(ProjectView).active = "tab-settings"
        await pilot.pause()
        view = host.query_one(SettingsView)
        control = view.query_one("#approval-coder", Select)
        launch = view.query_one("#launch-coder", Static)
        return control.value, str(launch.render())

    value, _line = drive(team_project(tmp_path), scenario)
    assert value == "untrusted"  # shown as (custom), never rewritten by opening the form


def test_launch_lines_show_each_agents_flags_and_a_refusal() -> None:
    coder, reviewer = FleetRoleSettings(worktree=True), FleetRoleSettings()
    assert "Codex --approve-for-me" in launch_line("coder", "codex", coder).plain
    seat = launch_line("reviewer", "codex", reviewer).plain
    assert "--ask-for-approval on-request -c 'approvals_reviewer=\"auto_review\"'" in seat
    assert "+ the aisquare store, writable in its read_only sandbox" in seat
    claude = launch_line("coder", "claude-code", coder).plain
    assert "Claude Code --permission-mode auto" in claude and "--name" not in claude
    off = coder.model_copy(update={"approve_for_me": False})
    assert "writable in its workspace sandbox" in launch_line("coder", "codex", off).plain
    clash = coder.model_copy(update={"approve_for_me": True, "sandbox": "read-only"})
    refused = launch_line("coder", "codex", clash).plain
    assert "refused at spawn" in refused and "approve_for_me" in refused
    assert "not an agent this version knows" in launch_line("coder", "gemini", coder).plain


def test_settings_save_approve_for_me_and_follow_the_roles_agent(tmp_path: Path) -> None:
    agent_launch.use("codex")

    async def scenario(pilot: Pilot[None], host: Host) -> list[str]:
        host.query_one(ProjectView).active = "tab-settings"
        await pilot.pause()
        view = host.query_one(SettingsView)
        lines = [str(view.query_one("#launch-coder", Static).render())]
        view.query_one("#afm-coder", Select).value = "off"
        await pilot.pause()
        lines.append(str(view.query_one("#launch-coder", Static).render()))
        view.query_one("#family-coder", Select).value = "claude-code"
        await pilot.pause()
        lines.append(str(view.query_one("#launch-coder", Static).render()))
        view.query_one("#save-settings", Button).press()
        await pilot.pause()
        return lines

    codex, off, claude = drive(team_project(tmp_path), scenario)
    assert "--approve-for-me" in codex  # the user default is Codex
    assert "--approve-for-me" not in off and "writable in its workspace sandbox" in off
    assert "Claude Code --permission-mode auto" in claude
    saved = load_config()
    assert saved.fleet.roles["coder"].approve_for_me is False
    assert saved.team.profiles["coder"].agent == "claude-code"


def test_account_options_name_another_agents_slot_against_the_roles_agent() -> None:
    codex = ClaudeAccount(slot=0, config_dir=Path("/h/.codex"), managed=False, agent="codex")
    claude = ClaudeAccount(slot=1, config_dir=Path("/h/.claude"), managed=False)
    for_codex = dict(
        (value, label) for label, value in account_options([claude, codex], None, "codex")
    )
    assert "Claude Code" in for_codex["1"] and "Codex" not in for_codex["0"]
    for_claude = dict((value, label) for label, value in account_options([claude, codex], None))
    assert "Codex" in for_claude["0"] and "Claude Code" not in for_claude["1"]


# --------------------------------------------------------------------------- spawn dialog


def _receipt(project_id: str, tmp_path: Path) -> fleet.SpawnReceipt:
    agent = FleetAgent(
        id="fixture",
        project_id=project_id,
        label="coder-1",
        role="coder",
        agent="codex",
        binary="codex",
        cwd=tmp_path,
        tmux_socket="test",
        pane_id="%1",
        created_at=datetime.now(UTC),
    )
    return fleet.SpawnReceipt(agent=agent, asked_label=None, tmux_session="test")


@pytest.mark.parametrize("agent", ["codex", "claude-code"])
def test_the_spawn_dialog_passes_account_and_permission_in_the_agents_words(
    agent: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = team_project(tmp_path)
    started = Mock(return_value=_receipt(project.id, tmp_path))
    monkeypatch.setattr(fleet, "spawn", started)
    slot = ClaudeAccount(slot=3, config_dir=tmp_path / "3", managed=True, agent=agent)
    monkeypatch.setattr("aisquare.services.claude_accounts.list_accounts", lambda: [slot])

    async def scenario() -> tuple[bool, bool, str]:
        host: App[None] = App()
        async with host.run_test(size=(100, 48)) as pilot:
            host.push_screen(SpawnScreen(project))
            await settle_page(host)
            screen = host.screen
            screen.query_one("#spawn-family", Select).value = agent
            await pilot.pause()
            sandbox_shown = screen.query_one("#spawn-sandbox", Select).display
            mode_shown = screen.query_one("#spawn-permission", Select).display
            screen.query_one("#spawn-account", Select).value = "3"
            if agent == "codex":
                screen.query_one("#spawn-sandbox", Select).value = "read-only"
                screen.query_one("#spawn-approval", Select).value = "on-request"
            else:
                screen.query_one("#spawn-permission", Select).value = "plan"
            await pilot.pause()
            line = str(screen.query_one("#spawn-launch", Static).render())
            screen.query_one("#spawn-submit", Button).press()
            await settle_page(host)
            return sandbox_shown, mode_shown, line

    sandbox_shown, mode_shown, line = asyncio.run(scenario())
    kwargs = started.call_args.kwargs
    assert started.call_args.args == (project, "coder")
    assert kwargs["agent"] == agent and kwargs["account"] == "3"
    if agent == "codex":
        assert sandbox_shown and not mode_shown
        assert (kwargs["sandbox"], kwargs["approval_policy"]) == ("read-only", "on-request")
        assert kwargs["permission_mode"] is None
        assert "--ask-for-approval on-request" in line and "its read_only sandbox" in line
    else:
        assert mode_shown and not sandbox_shown
        assert kwargs["permission_mode"] == "plan"
        assert kwargs["sandbox"] is None and kwargs["approval_policy"] is None
        assert "--permission-mode plan" in line


def test_the_spawn_dialog_reads_who_each_account_is_once_off_the_ui_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Claude slot's identity is its .claude.json, which can be tens of MB: read on
    the event loop on every change of role or agent, it froze the dialog."""
    import threading

    project = team_project(tmp_path)
    slots = [
        ClaudeAccount(slot=slot, config_dir=tmp_path / str(slot), managed=True)
        for slot in (2, 3, 4)
    ]
    monkeypatch.setattr("aisquare.services.claude_accounts.list_accounts", lambda: slots)
    readers: list[threading.Thread] = []

    def identity(account: ClaudeAccount, **_: Any) -> ClaudeIdentity:
        readers.append(threading.current_thread())
        return ClaudeIdentity(email=f"slot{account.slot}@example.com")

    monkeypatch.setattr(accounts_core, "identity", identity)

    async def scenario() -> list[str]:
        host: App[None] = App()
        async with host.run_test(size=(100, 48)) as pilot:
            host.push_screen(SpawnScreen(project))
            await settle_page(host)
            screen = host.screen
            for role in ("reviewer", "tester", "coder"):
                screen.query_one("#spawn-role", Select).value = role
                await pilot.pause()
            screen.query_one("#spawn-family", Select).value = "claude-code"
            await pilot.pause()
            options = screen.query_one("#spawn-account", Select)._options
            return [str(label) for label, _value in options]

    labels = asyncio.run(scenario())
    assert len(readers) == len(slots), "once per account, in the facts worker"
    assert threading.main_thread() not in readers
    assert any("slot3@example.com" in label for label in labels), labels


# --------------------------------------------------------------------------- Doctor


def _codex_rows(cwd: Path) -> list[DoctorCheck]:
    return [check for check in diagnostics._check_other_agents(cwd) if check.name == "codex"]


@pytest.fixture
def codex_home(isolated_agent_home: Path) -> Path:
    home = isolated_agent_home / ".codex"
    home.mkdir(parents=True)
    agent_launch.use("codex")
    return home


def test_a_partial_codex_install_names_what_is_missing_and_connect_fixes_it(
    codex_home: Path, tmp_path: Path
) -> None:
    agents_service.connect("codex", codex_home)
    hooks = codex_home / "hooks.json"
    data = json.loads(hooks.read_text(encoding="utf-8"))
    data["hooks"].pop("Stop")
    data["hooks"].pop("SessionEnd")
    hooks.write_text(json.dumps(data), encoding="utf-8")
    rows = _codex_rows(tmp_path)
    partial = next(row for row in rows if "Codex: partial in" in row.detail)
    assert partial.status is CheckStatus.warn
    assert "missing for Stop, SessionEnd" in partial.detail and str(codex_home) in partial.detail
    fixes = onboarding.fix_commands([partial])
    assert [fix.argv for fix in fixes] == [
        ("agents", "connect", "codex", "--config-dir", str(codex_home))
    ]
    # Control: with none of aisquare's hooks, the row says they are not there, not partial.
    hooks.unlink()
    bare = [row.detail for row in _codex_rows(tmp_path)]
    assert any("not installed there" in detail for detail in bare), bare
    assert not any("Codex: partial in" in detail for detail in bare), bare


def test_the_trust_row_names_its_step_and_offers_no_connect(
    codex_home: Path, tmp_path: Path
) -> None:
    agents_service.connect("codex", codex_home)
    row = next(row for row in _codex_rows(tmp_path) if row.status is CheckStatus.warn)
    assert row.detail.startswith(f"Codex: unverified in {codex_home}")  # install.sh reads this
    assert row.fix is not None and "/hooks" in row.fix and "codex" in row.fix
    assert "agents connect" not in row.fix
    assert onboarding.fix_commands([row]) == []  # nothing a button could run


def test_a_slots_sign_in_is_one_click_that_opens_accounts_never_a_json_run() -> None:
    check = DoctorCheck(
        name="codex-accounts",
        status=CheckStatus.warn,
        detail="2 added beside the plain codex: 3 not signed in · 4 a@x",
        fix="Sign in from asq → Accounts, or: aisquare accounts run 3 login",
    )
    (fix,) = onboarding.fix_commands([check])
    assert fix.interactive and fix.sign_in_slot == 3 and fix.argv == ("accounts", "run", "3")
    ran: list[Any] = []
    result = onboarding.apply_fix(fix, None, run=lambda *a, **k: ran.append(a))  # type: ignore[arg-type]
    assert not result.ok and not ran  # never handed to `--json`
    # Control: a hint carrying more than a slot is text, not a button.
    loose = check.model_copy(update={"fix": "aisquare accounts run 3 and sign in"})
    assert onboarding.fix_commands([loose]) == []

    class Recorder(App[None]):
        def __init__(self, view: DoctorView) -> None:
            super().__init__()
            self.view = view
            self.received: list[Message] = []

        def compose(self) -> ComposeResult:
            yield self.view

        def on_accounts_selected(self, message: AccountsSelected) -> None:
            self.received.append(message)

    async def press() -> tuple[str, list[Message]]:
        view = DoctorView(refresh=lambda cwd: [check])
        host = Recorder(view)
        async with host.run_test(size=(140, 40)) as pilot:
            view.show([check])
            await settle_page(host)
            button = next(iter(view.query("#doctor-fixes Button").results(Button)))
            label = str(button.label)
            await pilot.click(f"#{button.id}")
            await settle_page(host)
            return label, host.received

    label, received = asyncio.run(press())
    assert label == "Sign in account 3"
    assert [m.sign_in for m in received if isinstance(m, AccountsSelected)] == [3]


def test_doctor_json_carries_the_codex_rows(
    codex_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from aisquare.cli.app import app

    agents_service.connect("codex", codex_home)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["--json", "doctor"])
    rows = [row for row in json.loads(result.stdout) if row["name"] == "codex"]
    assert any(row["detail"].startswith("Codex: unverified in ") for row in rows), rows
    assert all("/hooks" in (row["fix"] or "") for row in rows if row["status"] == "warn"), rows


# --------------------------------------------------------------------------- sidebar


def _codex_status(email: str | None) -> ClaudeAccountStatus:
    account = ClaudeAccount(slot=0, config_dir=Path("/h/.codex"), managed=False, agent="codex")
    identity = ClaudeIdentity(email=email) if email else None
    return ClaudeAccountStatus(
        account=account,
        label=accounts_core.label(account),
        identity=identity,
        signed_in=identity is not None,
        token_state="ok" if identity else "missing",
    )


def _claude_unsigned() -> ClaudeAccountStatus:
    account = ClaudeAccount(slot=1, config_dir=Path("/h/.claude"), managed=False)
    return ClaudeAccountStatus(
        account=account, label=accounts_core.label(account), signed_in=False, token_state="missing"
    )


def _codex_only(*statuses: ClaudeAccountStatus) -> AccountsOverview:
    return AccountsOverview(
        claude=ClaudeInstall(installed=False),
        installs={"codex": ClaudeInstall(installed=True, binary="/opt/bin/codex")},
        accounts=list(statuses),
    )


def test_a_codex_only_machine_is_not_told_claude_code_is_missing() -> None:
    signed = summarise(_codex_only(_claude_unsigned(), _codex_status("dev@x.io")), None)
    assert signed.line is not None and signed.line.plain == "1 Codex · AISquare: sign in"
    unsigned = summarise(_codex_only(_claude_unsigned(), _codex_status(None)), None)
    assert unsigned.line is not None and unsigned.line.plain == "0 Codex · ⚠ #0 no login"
    # Claude Code's plain slot, with no Claude Code here, is not the thing to sign in.
    assert "#1" not in unsigned.line.plain and "Claude" not in unsigned.line.plain
