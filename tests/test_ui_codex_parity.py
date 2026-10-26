"""Codex parity on the 0.8.0 UI beyond Welcome: Settings, the spawn dialog, Doctor, sidebar.

- **App settings** offers Codex 0.162.1's approval policies (no ``untrusted``), the role's
  ``approve_for_me``, and under each role the flags its next ``fleet spawn`` passes —
  the adapter's own ``fleet_args`` — or the refusal it would meet.
- **The spawn dialog** (tests/test_ui_spawn.py) offers the account and the permission in
  the launch agent's own words, and passes them to ``fleet.spawn`` as ``fleet spawn`` does.
- **Doctor** tells a partial Codex install from a missing one, gives the trust row its
  real step instead of a Connect that cannot trust, and makes a slot's sign-in one
  click (the Accounts page's pane), never a ``--json`` run.
- **The sidebar** does not call a Codex-only machine "Claude Code not installed".
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.message import Message
from textual.pilot import Pilot
from textual.widgets import Button, Select, Static

from aisquare.cli.ui.sidebar import AccountsSelected
from aisquare.cli.ui.views.app_settings import (
    APPROVAL_OPTIONS,
    AppSettingsView,
    account_options,
    launch_line,
)
from aisquare.cli.ui.views.doctor import DoctorView
from aisquare.core.config import FleetRoleSettings, load_config, save_config
from aisquare.models import (
    CheckStatus,
    ClaudeAccount,
    DoctorCheck,
)
from aisquare.services import agent_launch, diagnostics, onboarding
from aisquare.services import agents as agents_service
from tests.test_ui_app_settings import AppHost
from tests.test_ui_app_settings import drive as drive_app_settings
from tests.ui_workers import settle_page

# --------------------------------------------------------------------------- Settings


def test_settings_offer_codexs_approval_policies_and_keep_an_old_untrusted_shown(
    tmp_path: Path,
) -> None:
    assert [value for _label, value in APPROVAL_OPTIONS] == ["", "on-request", "never"]
    config = load_config()
    config.fleet.roles["coder"].approval_policy = "untrusted"  # saved by an older version
    save_config(config)

    async def scenario(pilot: Pilot[None], host: AppHost) -> tuple[Any, str]:
        await pilot.pause()
        view = host.query_one(AppSettingsView)
        control = view.query_one("#role-approval-coder", Select)
        launch = view.query_one("#role-launch-coder", Static)
        return control.value, str(launch.render())

    value, _line = drive_app_settings(scenario)
    assert value == "untrusted"  # shown as (custom), never rewritten by opening the form


def test_launch_lines_show_each_agents_flags_and_a_refusal() -> None:
    coder, reviewer = FleetRoleSettings(worktree=True), FleetRoleSettings()
    assert "Codex --approve-for-me" in launch_line("coder", "codex", coder).plain
    seat = launch_line("reviewer", "codex", reviewer).plain
    assert "--ask-for-approval on-request -c 'approvals_reviewer=\"auto_review\"'" in seat
    assert "+ the aisquare store, read-only in its read_only sandbox" in seat
    claude = launch_line("coder", "claude-code", coder).plain
    assert "Claude Code --permission-mode auto" in claude and "--name" not in claude
    off = coder.model_copy(update={"approve_for_me": False})
    assert "read-only in its workspace sandbox" in launch_line("coder", "codex", off).plain
    clash = coder.model_copy(update={"approve_for_me": True, "sandbox": "read-only"})
    refused = launch_line("coder", "codex", clash).plain
    assert "refused at spawn" in refused and "approve_for_me" in refused
    assert "not an agent this version knows" in launch_line("coder", "gemini", coder).plain


def test_settings_save_approve_for_me_and_follow_the_roles_agent(tmp_path: Path) -> None:
    agent_launch.use("codex")

    async def scenario(pilot: Pilot[None], host: AppHost) -> list[str]:
        await pilot.pause()
        view = host.query_one(AppSettingsView)
        lines = [str(view.query_one("#role-launch-coder", Static).render())]
        view.query_one("#role-afm-coder", Select).value = "off"
        await settle_page(host)  # the pick's message reaches the form's handler
        lines.append(str(view.query_one("#role-launch-coder", Static).render()))
        view.query_one("#role-family-coder", Select).value = "claude-code"
        await settle_page(host)
        lines.append(str(view.query_one("#role-launch-coder", Static).render()))
        view.query_one("#save-settings", Button).press()
        await settle_page(host)
        return lines

    codex, off, claude = drive_app_settings(scenario)
    assert "--approve-for-me" in codex  # the user default is Codex
    assert "--approve-for-me" not in off and "read-only in its workspace sandbox" in off
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
