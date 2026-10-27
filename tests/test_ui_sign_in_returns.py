"""A sign-in started somewhere comes back there once it lands (#286, #215).

Welcome's *Sign in*, the persona picker's *+ New account* and the Spawn dialog's *Pick… ▸
+ New account* each leave for the Accounts page. Before, nothing brought the user back:
the content stayed on Accounts, and what the dialog held was gone. Each round trip here
runs the real shell; the Claude Code sign-in is the Accounts tests' script, and Welcome's
machine is the Welcome tests' one.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
from collections.abc import Awaitable, Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, TypeVar

import pytest
from textual.notifications import SeverityLevel
from textual.pilot import Pilot
from textual.widgets import Button, Input, OptionList, Select

from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.attach import AttachTargetScreen
from aisquare.cli.ui.sidebar import AccountsSelected, ProjectSelected, SpawnAgent, action_text
from aisquare.cli.ui.spawn import SpawnDialog
from aisquare.cli.ui.views import welcome
from aisquare.cli.ui.views.accounts import AccountsView, next_step
from aisquare.cli.ui.views.personas_tab import AttachRequested, PersonasTab
from aisquare.cli.ui.views.project import ProjectView
from aisquare.cli.ui.views.welcome import WelcomeView
from aisquare.core import tmux as tmux_core
from aisquare.core.tmux import Completed, TmuxServer
from aisquare.models import (
    AccountsOverview,
    ClaudeAccount,
    ClaudeAccountStatus,
    ClaudeIdentity,
    ClaudeInstall,
    ClaudeUsage,
)
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import fleet as fleet_service
from aisquare.services import iam
from tests.test_ui_accounts import _script_claude_sign_in
from tests.test_ui_spawn import register
from tests.test_ui_welcome import READY, _ready_machine
from tests.ui_workers import settle_page

T = TypeVar("T")
SIZE = (140, 60)
PRIVATE_SOCKET = f"asq-test-{os.getpid()}-sign-in-returns"


def _overview(*slots: int) -> AccountsOverview:
    return AccountsOverview(
        claude=ClaudeInstall(installed=True, binary="/usr/bin/claude"),
        accounts=[
            ClaudeAccountStatus(
                account=ClaudeAccount(slot=slot, config_dir=Path(f"/accounts/{slot}")),
                label="default" if slot == 1 else f"account {slot}",
                identity=ClaudeIdentity(email=f"{slot}@example.com"),
                signed_in=True,
            )
            for slot in slots
        ],
    )


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """No tmux server but the test's, no usage fetch, no AISquare session, no fleet."""
    ran: list[tuple[str, ...]] = []

    def record(argv: Sequence[str], stdin: bytes | None) -> Completed:
        ran.append(tuple(argv))
        return Completed(1, "", "no server running (a UI test addresses no real fleet)\n")

    monkeypatch.setattr(tmux_core, "_tmux", record)
    monkeypatch.setattr(
        fleet_service,
        "server",
        lambda config=None: TmuxServer(PRIVATE_SOCKET, binary=sys.executable),
    )
    monkeypatch.setattr(
        accounts_service, "usage", lambda account, **_k: ClaudeUsage(available=False, reason="-")
    )
    monkeypatch.setattr(iam, "current_session", lambda api_url=None: None)
    accounts: dict[str, Any] = {"overview": _overview(1)}
    monkeypatch.setattr(accounts_service, "overview", lambda: accounts["overview"])
    monkeypatch.setattr(fleet_service, "list_agents", lambda project, live_only=True: [])
    yield accounts
    assert all("-L" not in argv or PRIVATE_SOCKET in argv for argv in ran), ran


class Shell(FleetApp):
    """The real shell, its notices recorded."""

    def __init__(self) -> None:
        super().__init__(refresh_seconds=3600, doctor=lambda: [], accounts=None)
        self.notices: list[str] = []

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: SeverityLevel = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        self.notices.append(message)
        super().notify(message, title=title, severity=severity, timeout=timeout, markup=markup)


def drive(fn: Callable[[Pilot[None], Shell], Awaitable[T]]) -> T:
    async def run() -> T:
        app = Shell()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await settle_page(app)
            return await fn(pilot, app)

    return asyncio.run(run())


async def sign_in_lands(pilot: Pilot[None], app: Shell, accounts: dict[str, Any]) -> None:
    """The Accounts page's watched login lands on its next poll (taken by hand), slot 2."""
    view = app.query_one("#accounts", AccountsView)
    assert view.login is not None, "a sign-in window is open"
    accounts["overview"] = _overview(1, 2)
    view._poll_login()
    await pilot.pause()
    await settle_page(app)
    await pilot.pause()
    await settle_page(app)


def test_welcomes_sign_in_comes_back_to_step_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline: dict[str, Any]
) -> None:
    """On b26dd140 the content stayed on Accounts. Now Welcome is shown again, looks again,
    and the keyboard is on step 3; from there the manager's pane is two keys away."""
    signed_out = dataclasses.replace(READY, signed_in=False)
    machine, _project = _ready_machine(tmp_path, claude=[signed_out])
    monkeypatch.setattr(welcome, "DEFAULT_SEAMS", machine.seams())
    _script_claude_sign_in(monkeypatch, tmp_path, lands=True)

    async def go(pilot: Pilot[None], app: Shell) -> dict[str, Any]:
        page = app.query_one(WelcomeView)
        page.query_one("#claude-sign-in", Button).press()
        await settle_page(app)
        seen: dict[str, Any] = {"left_for": app.content.current}
        await pilot.click("#claude-add")
        await settle_page(app)
        machine.claude = [READY]  # signed in now: the next look reads it
        await sign_in_lands(pilot, app, offline)
        seen["back_on"] = app.content.current
        seen["focused"] = app.focused.id if app.focused is not None else None
        presses = 0
        while page.opened is None and presses < 5:
            await pilot.press("enter")
            presses += 1
            await settle_page(app)
        seen["presses"], seen["opened"] = presses, page.opened
        return seen

    seen = drive(go)
    assert seen["left_for"] == "accounts"
    assert seen["back_on"] == "welcome"
    assert seen["focused"] == "fleet-manager", "step 3's next button"
    assert seen["opened"] is not None and seen["presses"] <= 3, seen


def test_the_pickers_new_account_comes_back_with_the_new_slot_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline: dict[str, Any]
) -> None:
    """#215: *+ New account* was one way. The picker is back, the new slot highlighted, and
    confirming it opens the Spawn dialog on that account for the persona."""
    project = register(tmp_path / "alpha", project_id="prj_alpha")
    _script_claude_sign_in(monkeypatch, tmp_path, lands=True)

    async def go(pilot: Pilot[None], app: Shell) -> dict[str, Any]:
        app.post_message(ProjectSelected(project.id))
        await settle_page(app)
        view = app.query_one(f"#project-{project.id}", ProjectView)
        view.active = "tab-personas"  # built when first opened (review of #201, R1-F10)
        await settle_page(app)
        tab = view.query_one(PersonasTab)
        tab.post_message(AttachRequested("skeptic", "new"))
        await settle_page(app)
        picker = app.screen
        assert isinstance(picker, AttachTargetScreen)
        picker.query_one("#picker-new-account", Button).press()
        await settle_page(app)
        seen: dict[str, Any] = {"left_for": app.content.current}
        await sign_in_lands(pilot, app, offline)
        again = app.screen
        seen["reopened"] = type(again).__name__
        if isinstance(again, AttachTargetScreen):
            listing = again.query_one("#picker-list", OptionList)
            highlighted = listing.highlighted
            option = listing.get_option_at_index(highlighted) if highlighted is not None else None
            seen["highlighted"] = option.id if option is not None else None
            seen["persona"] = again.persona_name
            listing.action_select()
            await settle_page(app)
            dialog = app.screen
            if isinstance(dialog, SpawnDialog):
                seen["spawn_account"] = dialog.query_one("#spawn-account", Select).value
                seen["spawn_persona"] = dialog.query_one("#spawn-persona", Select).value
        return seen

    seen = drive(go)
    assert seen["left_for"] == "accounts"
    assert seen["reopened"] == "AttachTargetScreen"
    assert (seen["highlighted"], seen["persona"]) == ("account:2", "skeptic")
    assert (seen["spawn_account"], seen["spawn_persona"]) == ("2", "skeptic")


def test_the_spawn_dialogs_new_account_comes_back_as_it_was_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline: dict[str, Any]
) -> None:
    """*Pick… ▸ + New account* dismissed the dialog with everything typed in it. It opens
    again from what was typed, on the new account."""
    project = register(tmp_path / "alpha", project_id="prj_alpha")
    _script_claude_sign_in(monkeypatch, tmp_path, lands=True)

    async def go(pilot: Pilot[None], app: Shell) -> dict[str, Any]:
        app.post_message(SpawnAgent(project.id))
        await settle_page(app)
        dialog = app.screen
        assert isinstance(dialog, SpawnDialog)
        dialog.query_one("#spawn-label", Input).value = "coder-auth"
        dialog.query_one("#spawn-persona", Select).value = "skeptic"
        await settle_page(app)
        dialog.query_one("#spawn-pick", Button).press()
        await settle_page(app)
        picker = app.screen
        assert isinstance(picker, AttachTargetScreen)
        picker.query_one("#picker-new-account", Button).press()
        await settle_page(app)
        seen: dict[str, Any] = {"left_for": app.content.current}
        await sign_in_lands(pilot, app, offline)
        again = app.screen
        seen["reopened"] = type(again).__name__
        if isinstance(again, SpawnDialog):
            seen["label"] = again.query_one("#spawn-label", Input).value
            seen["persona"] = again.query_one("#spawn-persona", Select).value
            seen["account"] = again.query_one("#spawn-account", Select).value
        return seen

    seen = drive(go)
    assert seen["left_for"] == "accounts"
    assert seen["reopened"] == "SpawnDialog"
    assert (seen["label"], seen["persona"], seen["account"]) == ("coder-auth", "skeptic", "2")


def test_the_spawn_dialogs_project_field_comes_back_on_the_project_it_was_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline: dict[str, Any]
) -> None:
    """*Spawn agent* asks which project (#442); the form comes back from a sign-in (#286)
    with that field, on the project it had been changed to, and still offering the others.
    Without the list the reopened form had a fixed project and nothing to change it with."""
    alpha = register(tmp_path / "alpha", project_id="prj_alpha")
    beta = register(tmp_path / "beta", project_id="prj_beta", codename="brave-lynx")
    _script_claude_sign_in(monkeypatch, tmp_path, lands=True)

    async def go(pilot: Pilot[None], app: Shell) -> dict[str, Any]:
        app.post_message(SpawnAgent(alpha.id))
        await settle_page(app)
        dialog = app.screen
        assert isinstance(dialog, SpawnDialog)
        dialog.query_one("#spawn-project", Select).value = beta.id
        await settle_page(app)
        dialog.query_one("#spawn-label", Input).value = "coder-auth"
        dialog.query_one("#spawn-pick", Button).press()
        await settle_page(app)
        picker = app.screen
        assert isinstance(picker, AttachTargetScreen)
        picker.query_one("#picker-new-account", Button).press()
        await settle_page(app)
        await sign_in_lands(pilot, app, offline)
        again = app.screen
        seen: dict[str, Any] = {"reopened": type(again).__name__}
        if isinstance(again, SpawnDialog):
            field = again.query_one("#spawn-project", Select)
            seen["project"] = again.project.id
            seen["field"] = field.value
            seen["offered"] = sorted(p.id for p in again._choices)
            seen["label"] = again.query_one("#spawn-label", Input).value
            seen["account"] = again.query_one("#spawn-account", Select).value
        return seen

    seen = drive(go)
    assert seen["reopened"] == "SpawnDialog"
    assert (seen["project"], seen["field"]) == (beta.id, beta.id)
    assert seen["offered"] == [alpha.id, beta.id]
    assert (seen["label"], seen["account"]) == ("coder-auth", "2")


def test_a_cancelled_sign_in_reopens_nothing_and_says_so_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline: dict[str, Any]
) -> None:
    project = register(tmp_path / "alpha", project_id="prj_alpha")
    _script_claude_sign_in(monkeypatch, tmp_path, lands=False)

    async def go(pilot: Pilot[None], app: Shell) -> tuple[str, list[str], str | None]:
        app.post_message(SpawnAgent(project.id))
        await settle_page(app)
        dialog = app.screen
        assert isinstance(dialog, SpawnDialog)
        dialog.query_one("#spawn-pick", Button).press()
        await settle_page(app)
        app.screen.query_one("#picker-new-account", Button).press()
        await settle_page(app)
        before = len(app.notices)
        await pilot.click("#login-cancel")
        await settle_page(app)
        await pilot.pause()
        return type(app.screen).__name__, app.notices[before:], app.content.current

    screen, notices, current = drive(go)
    assert screen != "SpawnDialog" and current == "accounts"
    assert notices == ["no sign-in landed — the Spawn dialog was not reopened"]


def test_a_sign_in_from_nowhere_in_particular_names_the_next_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline: dict[str, Any]
) -> None:
    """With no way back to take, the page says the one step after the sign-in, from the
    shell's frame: a project with no manager is started from Welcome."""
    register(tmp_path / "alpha", project_id="prj_alpha")
    _script_claude_sign_in(monkeypatch, tmp_path, lands=True)

    async def go(pilot: Pilot[None], app: Shell) -> tuple[str | None, bool, str | None]:
        app.post_message(AccountsSelected())
        await settle_page(app)
        await pilot.click("#claude-add")
        await settle_page(app)
        await sign_in_lands(pilot, app, offline)
        view = app.query_one("#accounts", AccountsView)
        offered = view.query_one("#accounts-next-welcome", Button).display
        view.query_one("#accounts-next-welcome", Button).press()
        await settle_page(app)
        return view.next_step_shown(), bool(offered), app.content.current

    step, offered, current = drive(go)
    assert step == "Next: open alpha and press Start manager"
    assert offered and current == "welcome", "Go to Welcome takes it there"


def test_the_step_after_a_sign_in_names_the_sidebars_one_spawn_action(tmp_path: Path) -> None:
    """The last step says what to press: *Spawn agent*, which #442 made one action at the top
    of the sidebar, not a row under a project. It names the action as the sidebar labels it."""
    alpha = register(tmp_path / "alpha", project_id="prj_alpha")

    assert next_step([], set()).text == "Next: add a project (+ beside Fleet)"
    assert next_step([alpha], set()).welcome, "no manager yet: Welcome walks through it"
    fleet_up = next_step([alpha], {alpha.id})
    assert not fleet_up.welcome
    label = action_text("\uff0b", "Spawn agent").plain.split()
    assert fleet_up.text.split()[1:] == label, (fleet_up.text, label)


def test_spawn_dialog_presets_round_trip_through_form_state(
    tmp_path: Path, offline: dict[str, Any]
) -> None:
    """``SpawnDialog(project, **form_state())`` is the form again: what the user chose is
    carried; what the form filled in for itself is left to fill in again."""
    project = register(tmp_path / "alpha", project_id="prj_alpha")

    async def go(pilot: Pilot[None], app: Shell) -> tuple[dict[str, Any], dict[str, Any]]:
        app.push_screen(SpawnDialog(project, accounts=None))
        await settle_page(app)
        dialog = app.screen
        assert isinstance(dialog, SpawnDialog)
        untouched = dialog.form_state()
        dialog.query_one("#spawn-label", Input).value = "coder-auth"
        dialog.query_one("#spawn-binary", Input).value = "claude2"
        typed = dialog.form_state()
        return untouched, typed

    untouched, typed = drive(go)
    assert untouched == {"role": "coder"}
    assert typed == {"role": "coder", "binary": "claude2", "label": "coder-auth"}
