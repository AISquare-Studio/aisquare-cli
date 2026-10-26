"""The Spawn dialog, driven headless: every field, every refusal, the receipt.

docs/plans/spawn-personas.md §4.1 and §7 "P3". ``fleet_service.spawn`` is a
recorder in every test, so nothing here starts tmux, git or an agent; what each
test asserts is the artefact the claim is about — the exact keywords the
recorder received, the text the dialog shows, the toasts the app raised, the
view the app switched to — never the string that was handed in.

Two hosts. A bare ``Host`` pushes the dialog for one project and records what it
dismissed with: that is the form. ``FleetApp`` itself drives the button that opens
the dialog and the receipt that closes it, because both are the shell's.

"No test reaches tmux" is ENFORCED, as in ``test_ui_shell.py``: the module-level
runner every ``TmuxServer`` uses is replaced, and teardown asserts each argv it
saw addressed this file's private socket — the new agent's view mounts a live
``TerminalPane``, and a scripted agent on the default ``asq`` socket would reach
into the developer's own fleet.
"""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Coroutine, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

import pytest
from textual.app import App
from textual.geometry import Size
from textual.notifications import SeverityLevel
from textual.pilot import Pilot
from textual.widget import Widget
from textual.widgets import (
    Button,
    Checkbox,
    Collapsible,
    Input,
    Label,
    OptionList,
    RadioButton,
    Select,
    Static,
    Switch,
    TextArea,
)

from aisquare.cli.ui import app as app_module
from aisquare.cli.ui import spawn as spawn_module
from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.attach import AttachTargetScreen, NewAccountRequested
from aisquare.cli.ui.persona_dialogs import ImportPersonaScreen
from aisquare.cli.ui.sidebar import (
    AddProject,
    SpawnAgent,
    SpawnButton,
    agent_row_text,
    short_path,
)
from aisquare.cli.ui.spawn import (
    LABEL_RULE,
    NO_TASK,
    PickTargetRequested,
    SpawnDialog,
    dice_label,
    role_choices,
)
from aisquare.cli.ui.views.accounts import AccountsView
from aisquare.core import claude_accounts as accounts_core
from aisquare.core import codenames, experimental, harness, personas
from aisquare.core import tmux as tmux_core
from aisquare.core.config import FleetRoleSettings, load_config, save_config
from aisquare.core.ids import new_task_id
from aisquare.core.store import store_session
from aisquare.core.tmux import Completed
from aisquare.models import (
    AccountsOverview,
    ClaudeAccount,
    ClaudeAccountStatus,
    ClaudeIdentity,
    ClaudeInstall,
    FleetAgent,
    FleetAgentStatus,
    ProjectInfo,
    TeamSession,
    TeamTask,
)
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import fleet as fleet_service
from aisquare.services import personas as personas_service
from aisquare.services import settings as settings_service
from aisquare.services import team as team_service
from aisquare.services.captain import state as captain_state
from tests.fleet_fakes import list_agents_as
from tests.ui_workers import released, settle_page, settle_until, settle_workers

T = TypeVar("T")
SIZE = (120, 50)
PRIVATE_SOCKET = f"asq-test-{os.getpid()}-ui-spawn"
LATE = 0.3
"""How long a scripted refusal holds its worker: past the click's own pause, so it lands
while ``settle`` already waits, the order windows-latest met by chance."""
"""The socket the spawned agent lives on. NOT ``asq``: the default, and the
developer's real fleet — see the module docstring."""

Kwargs = dict[str, object]


# --- fixtures and helpers -------------------------------------------------------------


def _socket_of(argv: Sequence[str]) -> str | None:
    args = list(argv)
    return args[args.index("-L") + 1] if "-L" in args else None


@pytest.fixture(autouse=True)
def no_real_tmux(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, ...]]]:
    """Every tmux command this file causes must address :data:`PRIVATE_SOCKET`."""
    ran: list[tuple[str, ...]] = []

    def record(argv: Sequence[str], stdin: bytes | None) -> Completed:
        ran.append(tuple(argv))
        return Completed(1, "", "no server running (a UI test addresses no real fleet)\n")

    monkeypatch.setattr(tmux_core, "_tmux", record)
    yield ran
    # `tmux -V` is the version probe (asked once per server, #203 round 9): it names no
    # socket and reaches no server, so it addresses no fleet — the rule is about the rest.
    wrong = [argv for argv in ran if "-V" not in argv and _socket_of(argv) != PRIVATE_SOCKET]
    assert not wrong, f"a UI test addressed a tmux socket that is not the test's: {wrong[:2]}"


def register(
    root: Path, *, project_id: str = "prj_spawn", codename: str = "amber-otter"
) -> ProjectInfo:
    """A project in the isolated store, with its codename, rooted at ``root``.

    Onboarded, not merely registered: since #139 a project the hooks captured is
    kept but not SHOWN, and the spawn row these tests click belongs to a listed
    project — the same call ``test_ui_shell.seed`` makes.
    """
    root.mkdir(parents=True, exist_ok=True)
    with store_session() as store:
        store.onboard_project(ProjectInfo(id=project_id, root=root))
        return store.set_codename(project_id, codename)


@pytest.fixture
def git_project(tmp_path: Path) -> ProjectInfo:
    """A git checkout, as far as ``is_git_project`` asks: a root with ``.git``."""
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    return register(tmp_path / "repo")


@pytest.fixture
def plain_project(tmp_path: Path) -> ProjectInfo:
    """A directory that is not a repository — a parent of several, say."""
    return register(tmp_path / "plain")


def add_task(project: ProjectInfo, title: str) -> TeamTask:
    now = datetime.now(tz=UTC)
    with store_session() as store:
        task, _ = store.upsert_task(
            TeamTask(
                id=new_task_id(),
                project_id=project.id,
                key=team_service.task_key(title),
                title=title,
                created_at=now,
                updated_at=now,
            )
        )
    return task


def spawned_agent(
    project: ProjectInfo, *, label: str = "coder-1", role: str = "coder"
) -> FleetAgent:
    return FleetAgent(
        id="agt_01spawnedbythedialog",
        project_id=project.id,
        label=label,
        role=role,
        pane_id="%9",
        cwd=project.root,
        created_at=datetime.now(tz=UTC),
        tmux_socket=PRIVATE_SOCKET,  # never the real fleet's default
    )


def receipt_for(
    project: ProjectInfo, *, notes: list[str] | None = None, label: str = "coder-1"
) -> fleet_service.SpawnReceipt:
    return fleet_service.SpawnReceipt(
        agent=spawned_agent(project, label=label),
        asked_label=None,
        tmux_session="asq-amber-otter",
        notes=list(notes or []),
    )


class SpawnRecorder:
    """What ``fleet_service.spawn`` is replaced by: every call, and a scripted answer."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Kwargs]] = []
        self.answer: Callable[[ProjectInfo, str], fleet_service.SpawnReceipt] = (
            lambda project, role: receipt_for(project)
        )

    def __call__(
        self, project: ProjectInfo, role: str, **kwargs: object
    ) -> fleet_service.SpawnReceipt:
        self.calls.append((project.id, role, dict(kwargs)))
        return self.answer(project, role)


@pytest.fixture(autouse=True)
def spawns(monkeypatch: pytest.MonkeyPatch) -> SpawnRecorder:
    recorder = SpawnRecorder()
    monkeypatch.setattr(fleet_service, "spawn", recorder)
    return recorder


UNTOUCHED: Kwargs = {
    "label": None,
    "task_id": None,
    "worktree": None,
    "permission_mode": None,
    "binary": None,
    "prompt": None,
    "agent_args": [],
    "account": None,
    "persona": None,
    "remote_control": None,
}
"""What a Spawn press sends for a form nobody changed: the role's default everywhere."""


def overview(*slots: tuple[int, str | None]) -> AccountsOverview:
    """An accounts overview with ``(slot, email or None)`` per account."""
    return AccountsOverview(
        claude=ClaudeInstall(installed=True, binary="/usr/bin/claude"),
        accounts=[
            ClaudeAccountStatus(
                account=ClaudeAccount(slot=slot, config_dir=Path(f"/accounts/{slot}")),
                label="default" if slot == 1 else f"account {slot}",
                identity=ClaudeIdentity(email=email) if email else None,
                signed_in=email is not None,
            )
            for slot, email in slots
        ],
    )


class Host(App[None]):
    """A bare app that opens the dialog for one project and records what surfaces from it."""

    def __init__(
        self,
        project: ProjectInfo,
        *,
        accounts: Callable[[], AccountsOverview] | None = None,
        presets: dict[str, str] | None = None,
        defaults: Any = None,
        projects: Sequence[ProjectInfo] | None = None,
    ) -> None:
        super().__init__()
        self._project = project
        self._accounts = accounts
        self._presets: dict[str, Any] = dict(presets or {})
        if defaults is not None:  # else the dialog's own: the real ladder, in this test's home
            self._presets["defaults"] = defaults
        self._projects = projects
        self.results: list[fleet_service.SpawnReceipt | None] = []
        self.notices: list[tuple[str, str]] = []
        self.picks: list[str] = []

    def on_mount(self) -> None:
        self.push_screen(
            SpawnDialog(
                self._project, projects=self._projects, accounts=self._accounts, **self._presets
            ),
            callback=self.results.append,
        )

    def on_pick_target_requested(self, event: PickTargetRequested) -> None:
        self.picks.append(event.project_id)

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: SeverityLevel = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        self.notices.append((message, severity))
        super().notify(message, title=title, severity=severity, timeout=timeout, markup=markup)


def drive(
    project: ProjectInfo,
    scenario: Callable[[Pilot[None], Host, SpawnDialog], Coroutine[Any, Any, T]],
    *,
    accounts: Callable[[], AccountsOverview] | None = None,
    presets: dict[str, str] | None = None,
    defaults: Any = None,
    projects: Sequence[ProjectInfo] | None = None,
) -> T:
    """Run ``scenario`` against the dialog open over a bare host."""

    async def run() -> T:
        host = Host(
            project, accounts=accounts, presets=presets, defaults=defaults, projects=projects
        )
        async with host.run_test(size=SIZE) as pilot:
            await settle(pilot)
            dialog = host.screen
            assert isinstance(dialog, SpawnDialog)
            return await scenario(pilot, host, dialog)

    return asyncio.run(run())


async def settle(pilot: Pilot[Any]) -> None:
    """Let the dialog go quiet: every worker of ours ended, whatever its state, its
    state-change handler run and the screen refreshed (``settle_page``).

    Not ``workers.wait_for_complete()``: a scripted refusal ends its worker in an error,
    which that call raised as ``WorkerFailed`` whenever the worker was still registered
    when the wait began (as on rc/captain-v1, #238, card tsk_01m3pt9eme0h).
    """
    await settle_page(pilot.app)


def shown(widget: Static) -> str:
    """The text a widget renders — the artefact, not the argument."""
    return str(widget.render())


def note(dialog: SpawnDialog, selector: str) -> str | None:
    """A note's text while it is displayed; ``None`` while it is hidden."""
    widget = dialog.query_one(selector, Static)
    return shown(widget) if widget.display else None


def submit(dialog: SpawnDialog) -> Button:
    return dialog.query_one("#spawn-submit", Button)


def label_input(dialog: SpawnDialog) -> Input:
    return dialog.query_one("#spawn-label", Input)


def select(dialog: SpawnDialog, name: str) -> Select[str]:
    widget: Select[str] = dialog.query_one(f"#spawn-{name}", Select)
    return widget


def advanced(dialog: SpawnDialog) -> Collapsible:
    return dialog.query_one("#spawn-advanced", Collapsible)


async def open_advanced(pilot: Pilot[Any]) -> None:
    """Unfold *Advanced* as the owner does, with a click on its title. Folded, its fields are
    not on screen, and a click at one of them lands on nothing at all."""
    dialog = pilot.app.screen
    assert isinstance(dialog, SpawnDialog)
    if advanced(dialog).collapsed:
        await pilot.click("#spawn-advanced CollapsibleTitle")
        await unfolded(pilot)
    assert not advanced(dialog).collapsed


async def unfolded(pilot: Pilot[Any]) -> None:
    """Wait out a fold or an unfold: the box grows or shrinks and centres again, and the
    section scrolls itself into view (``Collapsible.scroll_visible``, animated). A click
    before that lands where a widget was."""
    await settle(pilot)
    await pilot.wait_for_scheduled_animations()
    await pilot.pause()


async def resized(pilot: Pilot[Any], width: int, height: int) -> None:
    """Resize the terminal and wait until the screen is laid out at that size.

    ``Pilot.resize_terminal`` returns once the app looks idle, and the app hands a resize on
    to its screen from a timer (``App._on_resize``), so the screen could still be laid out at
    the old size. A click then aimed where *Advanced*'s title was in the 120x50 dialog: at
    80x24 off the screen (``OutOfBounds``), at 80x45 on nothing, and the section stayed
    folded (#298's full matrix, windows-latest, push run 40222702828). The screen's own size
    is set in the same reflow that places the dialog, so that is what is waited for."""
    size = Size(width, height)
    await pilot.resize_terminal(width, height)
    await settle_until(pilot.app, lambda: pilot.app.screen.outer_size == size)
    assert pilot.app.screen.outer_size == size, "the screen was never laid out at the new size"


# --- the row opens it -----------------------------------------------------------------


class RecordingFleetApp(FleetApp):
    """``FleetApp`` with every notification recorded, a stub doctor and no accounts reader."""

    def __init__(self) -> None:
        super().__init__(refresh_seconds=3600, doctor=lambda: [], accounts=None)
        self.notices: list[tuple[str, str]] = []

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: SeverityLevel = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        self.notices.append((message, severity))
        super().notify(message, title=title, severity=severity, timeout=timeout, markup=markup)


@pytest.fixture
def fleet(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[FleetAgentStatus]]:
    """What ``fleet_service.list_agents`` answers, per project id — mutable mid-test."""
    agents: dict[str, list[FleetAgentStatus]] = {}

    def fake(project: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        return list(agents.get(project.id, []))

    list_agents_as(monkeypatch, fake)
    return agents


def drive_app(scenario: Callable[[Pilot[None], RecordingFleetApp], Coroutine[Any, Any, T]]) -> T:
    async def run() -> T:
        app = RecordingFleetApp()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            return await scenario(pilot, app)

    return asyncio.run(run())


def spawn_button(app: FleetApp) -> SpawnButton:
    """The sidebar's one *Spawn agent*: no card has a spawn row of its own."""
    return app.query_one(SpawnButton)


def test_the_spawn_button_opens_the_dialog_on_the_selected_project_and_no_toast(
    tmp_path: Path, fleet: dict[str, list[FleetAgentStatus]]
) -> None:
    register(tmp_path / "alpha", project_id="prj_a", codename="amber-otter")
    register(tmp_path / "beta", project_id="prj_b", codename="ruby-fox")

    async def scenario(pilot: Pilot[None], app: RecordingFleetApp) -> tuple[str, str, list[Any]]:
        await pilot.click("#card-prj_b ProjectTitle")
        await pilot.pause()
        await pilot.click(spawn_button(app))
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, SpawnDialog), f"the button opened {type(dialog).__name__}"
        return dialog.project.id, shown(dialog.query_one("#spawn-header", Static)), app.notices

    project_id, header, notices = drive_app(scenario)
    assert project_id == "prj_b"  # the selected card's project, not the first one
    assert "beta · ruby-fox" in header  # the header names it before anything happens
    assert notices == []  # the "not built yet" toast is gone


def test_the_spawn_button_with_nothing_selected_opens_on_the_first_project(
    tmp_path: Path, fleet: dict[str, list[FleetAgentStatus]]
) -> None:
    register(tmp_path / "alpha", project_id="prj_a")
    register(tmp_path / "beta", project_id="prj_b", codename="ruby-fox")

    async def scenario(pilot: Pilot[None], app: RecordingFleetApp) -> tuple[str, str, str | None]:
        await pilot.click(spawn_button(app))
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, SpawnDialog)
        return dialog.project.id, app.sidebar.ordered_projects()[0].id, app.sidebar.selected_key

    project_id, first, selected = drive_app(scenario)
    assert selected is None  # nothing was selected
    assert project_id == first  # the first project in the sidebar's order


def test_the_spawn_button_with_no_projects_says_so_and_opens_no_dialog(
    fleet: dict[str, list[FleetAgentStatus]],
) -> None:
    async def scenario(pilot: Pilot[None], app: RecordingFleetApp) -> tuple[str, list[Any]]:
        await pilot.click(spawn_button(app))
        await pilot.pause()
        return type(app.screen).__name__, app.notices

    screen, notices = drive_app(scenario)
    assert screen != "SpawnDialog"
    assert notices == [("No projects yet — press + to onboard one", "information")]


def test_a_spawn_for_a_project_that_left_the_frame_warns_and_opens_nothing(
    tmp_path: Path, fleet: dict[str, list[FleetAgentStatus]]
) -> None:
    register(tmp_path / "alpha", project_id="prj_a")

    async def scenario(pilot: Pilot[None], app: RecordingFleetApp) -> tuple[str, list[Any]]:
        app.post_message(SpawnAgent("prj_gone"))
        await pilot.pause()
        return type(app.screen).__name__, app.notices

    screen, notices = drive_app(scenario)
    assert screen != "SpawnDialog"
    assert notices == [("that project is no longer listed", "warning")]


def test_opening_and_closing_the_dialog_leaves_the_app_keys_as_they_were(
    tmp_path: Path, fleet: dict[str, list[FleetAgentStatus]]
) -> None:
    """The modal binds Esc and nothing else; the shell's keys are the same after it closes."""
    register(tmp_path / "alpha", project_id="prj_a")

    def keys(app: FleetApp) -> dict[str, list[tuple[str, bool]]]:
        return {
            key: [(binding.action, binding.priority) for binding in bindings]
            for key, bindings in app._bindings.key_to_bindings.items()
        }

    async def scenario(pilot: Pilot[None], app: RecordingFleetApp) -> tuple[Any, ...]:
        before = keys(app)
        # What every Screen binds anyway (focus movement, copy) — not the dialog's doing.
        screen_keys = set(app.screen._bindings.key_to_bindings)
        await pilot.click(spawn_button(app))
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, SpawnDialog)
        dialog_keys = set(dialog._bindings.key_to_bindings) - screen_keys
        label_input(dialog).focus()
        await pilot.press("q")  # typed into the form, not the app's quit
        await pilot.pause()
        still_running = app.is_running and isinstance(app.screen, SpawnDialog)
        typed = label_input(dialog).value
        await pilot.press("escape")
        await pilot.pause()
        return before, keys(app), dialog_keys, still_running, typed, type(app.screen).__name__

    before, after, dialog_keys, still_running, typed, screen = drive_app(scenario)
    assert after == before
    assert dialog_keys == {"escape"}
    assert still_running and typed.endswith("q")
    assert screen != "SpawnDialog"


# --- the form -------------------------------------------------------------------------


def test_the_label_prefills_role_n_and_a_picked_task_re_prefills_it(
    git_project: ProjectInfo,
) -> None:
    task = add_task(git_project, "Wire the auth flow")
    short = task.id.removeprefix("tsk_")[: fleet_service.TASK_SHORT]

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[str]:
        seen = [label_input(dialog).value]
        select(dialog, "task").value = task.id
        await pilot.pause()
        seen.append(label_input(dialog).value)
        select(dialog, "role").value = "tester"
        await pilot.pause()
        seen.append(label_input(dialog).value)
        label_input(dialog).value = "tester-auth"  # touched: no longer the form's to change
        await pilot.pause()
        select(dialog, "task").value = NO_TASK
        await pilot.pause()
        seen.append(label_input(dialog).value)
        return seen

    assert drive(git_project, scenario) == [
        "coder-1",
        f"coder-{short}",
        f"tester-{short}",
        "tester-auth",  # the control: a touched label survives a task change
    ]


def test_the_task_field_lists_only_open_tasks_with_short_id_status_and_title(
    git_project: ProjectInfo,
) -> None:
    open_task = add_task(git_project, "Still to do")
    finished = add_task(git_project, "Already done")
    with store_session() as store:
        store.set_task_status(finished.id, "done")

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[str]:
        overlay = select(dialog, "task").query_one(OptionList)
        return [str(overlay.get_option_at_index(i).prompt) for i in range(overlay.option_count)]

    short = open_task.id.removeprefix("tsk_")[: fleet_service.TASK_SHORT]
    assert drive(git_project, scenario) == ["(none)", f"{short} [todo] Still to do"]


def test_a_bad_label_disables_spawn_and_shows_the_rule(git_project: ProjectInfo) -> None:
    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        seen: list[Any] = [submit(dialog).disabled, note(dialog, "#spawn-label-rule")]
        label_input(dialog).value = "Bad Label!"
        await pilot.pause()
        seen += [submit(dialog).disabled, note(dialog, "#spawn-label-rule")]
        label_input(dialog).value = "manager"
        await pilot.pause()
        seen += [submit(dialog).disabled, note(dialog, "#spawn-label-rule")]
        label_input(dialog).value = "coder-auth"
        await pilot.pause()
        seen += [submit(dialog).disabled, note(dialog, "#spawn-label-rule")]
        await pilot.click("#spawn-submit")  # disabled a moment ago: this press must not be lost
        await settle(pilot)
        return seen

    seen = drive(git_project, scenario)
    assert seen[:2] == [False, None]  # the prefill is valid: nothing to say
    assert seen[2] is True and LABEL_RULE in seen[3]
    assert seen[4] is True and "reserved for the manager role" in seen[5]
    assert seen[6:] == [False, None]


def test_a_disabled_spawn_button_sends_nothing(
    git_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> bool:
        label_input(dialog).value = "Bad Label!"
        await pilot.pause()
        await pilot.click("#spawn-submit")
        await settle(pilot)
        return isinstance(host.screen, SpawnDialog)

    assert drive(git_project, scenario) is True
    assert spawns.calls == []


def test_dice_offers_role_adjective_animal_matching_the_label_rule(
    git_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> tuple[str, bool]:
        await open_advanced(pilot)
        await pilot.click("#spawn-dice")
        await pilot.pause()
        rolled = label_input(dialog).value
        enabled = not submit(dialog).disabled
        await pilot.click("#spawn-submit")
        await settle(pilot)
        return rolled, enabled

    rolled, enabled = drive(git_project, scenario)
    match = re.fullmatch(r"coder-([a-z]+)-([a-z]+)", rolled)
    assert match is not None, rolled
    assert match.group(1) in codenames.ADJECTIVES and match.group(2) in codenames.ANIMALS
    assert fleet_service.LABEL.match(rolled) and enabled
    assert spawns.calls[0][2]["label"] == rolled  # a rolled label is a chosen one: it is sent


def test_dice_fits_every_role_in_the_limit_and_refuses_a_role_too_long() -> None:
    import random

    for role in fleet_service.FLEET_ROLES:
        for seed in range(300):
            label = dice_label(role, random.Random(seed))
            assert label is not None and fleet_service.LABEL.match(label), (role, label)
            assert len(label) <= 24
    # 23 characters leaves no room for "-<3>-<3>": no label rather than an invalid one.
    assert dice_label("a-very-long-custom-role") is None


def test_role_choices_are_none_then_the_fleet_roles_then_the_bound_ones_sorted() -> None:
    assert role_choices(["zeta", "coder", "alpha", fleet_service.PLAIN_ROLE]) == [
        fleet_service.PLAIN_ROLE,  # (none): offered once, first, even when bound
        *fleet_service.FLEET_ROLES,
        "alpha",
        "zeta",
    ]


def test_a_non_git_project_disables_the_worktree_switch_and_says_why(
    plain_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    async def scenario(
        pilot: Pilot[None], host: Host, dialog: SpawnDialog
    ) -> tuple[bool, bool, str]:
        switch = dialog.query_one("#spawn-worktree", Switch)
        why = shown(dialog.query_one("#spawn-worktree-note", Static))
        await pilot.click("#spawn-submit")
        await settle(pilot)
        return switch.disabled, switch.value, why

    disabled, value, why = drive(plain_project, scenario)
    assert disabled is True and value is False
    assert why == "not a git repository"
    # A coder's default IS a worktree, which this project cannot have: the form's
    # "off" is sent rather than a None the service would refuse.
    assert spawns.calls[0][2]["worktree"] is False


def test_a_git_project_offers_the_worktree_at_the_roles_default(git_project: ProjectInfo) -> None:
    async def scenario(
        pilot: Pilot[None], host: Host, dialog: SpawnDialog
    ) -> tuple[bool, bool, str]:
        switch = dialog.query_one("#spawn-worktree", Switch)
        return (
            switch.disabled,
            switch.value,
            shown(dialog.query_one("#spawn-worktree-note", Static)),
        )

    assert drive(git_project, scenario) == (False, True, "")  # coder: worktree = true


def test_spawn_sends_none_for_every_untouched_field(
    git_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        await pilot.click("#spawn-submit")
        await settle(pilot)
        return list(host.results)

    results = drive(git_project, scenario)
    assert spawns.calls == [(git_project.id, "coder", UNTOUCHED)]  # exactly once
    assert len(results) == 1 and isinstance(results[0], fleet_service.SpawnReceipt)


def test_spawn_sends_exactly_the_chosen_values_with_the_agent_args_shlex_split(
    git_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    task = add_task(git_project, "Wire the auth flow")

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> None:
        select(dialog, "role").value = "tester"
        await pilot.pause()
        select(dialog, "task").value = task.id
        await pilot.pause()
        label_input(dialog).value = "tester-auth"
        dialog.query_one("#spawn-worktree", Switch).value = True  # a tester's default is False
        select(dialog, "permission").value = "plan"
        select(dialog, "account").value = "2"
        dialog.query_one("#spawn-binary", Input).value = " claude2 "
        dialog.query_one(
            "#spawn-args", Input
        ).value = '--model opus --append-system-prompt "be brief"'
        dialog.query_one("#spawn-prompt", TextArea).text = "start from the failing test"
        await pilot.pause()
        await pilot.click("#spawn-submit")
        await settle(pilot)

    drive(git_project, scenario, accounts=lambda: overview((1, "me@example.com"), (2, None)))
    assert spawns.calls == [
        (
            git_project.id,
            "tester",
            {
                "label": "tester-auth",
                "task_id": task.id,
                "worktree": True,
                "permission_mode": "plan",
                "binary": "claude2",
                "prompt": "start from the failing test",
                "agent_args": ["--model", "opus", "--append-system-prompt", "be brief"],
                "account": "2",
                "persona": None,
                "remote_control": None,
            },
        )
    ]


def test_a_quoting_error_in_the_agent_args_is_shown_inline_and_disables_spawn(
    git_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        dialog.query_one("#spawn-args", Input).value = '--append-system-prompt "unclosed'
        await pilot.pause()
        seen: list[Any] = [submit(dialog).disabled, note(dialog, "#spawn-args-error")]
        dialog.query_one("#spawn-args", Input).value = '--append-system-prompt "closed"'
        await pilot.pause()
        seen += [submit(dialog).disabled, note(dialog, "#spawn-args-error")]
        return seen

    seen = drive(git_project, scenario)
    assert seen[0] is True and "extra agent args: No closing quotation" in seen[1]
    assert seen[2:] == [False, None]
    assert spawns.calls == []


def test_untouched_defaults_follow_the_role_and_touched_ones_stay(
    git_project: ProjectInfo,
) -> None:
    config = load_config()
    config.fleet.roles["tester"] = FleetRoleSettings(permission_mode="plan")
    config.fleet.roles["reviewer"] = FleetRoleSettings(permission_mode="dontAsk", worktree=True)
    save_config(config)

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        mode = select(dialog, "permission")
        switch = dialog.query_one("#spawn-worktree", Switch)
        binary = dialog.query_one("#spawn-binary", Input)
        seen: list[Any] = [(mode.value, switch.value, binary.placeholder)]
        select(dialog, "role").value = "tester"
        await pilot.pause()
        seen.append((mode.value, switch.value))
        mode.value = "acceptEdits"  # touched
        await pilot.pause()
        select(dialog, "role").value = "reviewer"
        await pilot.pause()
        seen.append((mode.value, switch.value))
        seen.append(dialog.spawn_kwargs()["permission_mode"])
        return seen

    seen = drive(git_project, scenario)
    assert seen[0] == ("auto", True, harness.resolve_binary("coder").binary)
    assert seen[1] == ("plan", False)  # both untouched: they followed coder → tester
    assert seen[2] == ("acceptEdits", True)  # the touched mode stayed; the switch followed
    assert seen[3] == "acceptEdits"  # and differs from the reviewer's default: it is sent


def test_the_remote_control_switch_follows_the_role_until_touched_and_is_sent_then(
    git_project: ProjectInfo,
) -> None:
    """#289: *Claude Remote Control* opens at the role's ``remote_control``, follows a role
    change while untouched, and is sent only when it differs from the role's default."""
    config = load_config()
    config.fleet.roles["reviewer"] = FleetRoleSettings.model_validate({"remote_control": True})
    save_config(config)

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        remote = dialog.query_one("#spawn-remote", Switch)
        seen: list[Any] = [remote.value, dialog.spawn_kwargs()["remote_control"]]
        select(dialog, "role").value = "reviewer"
        await pilot.pause()
        seen += [remote.value, dialog.spawn_kwargs()["remote_control"]]
        remote.value = False  # touched: off for this one reviewer
        await pilot.pause()
        seen.append(dialog.spawn_kwargs()["remote_control"])
        select(dialog, "role").value = "coder"
        await pilot.pause()
        seen += [remote.value, dialog.spawn_kwargs()["remote_control"]]
        return seen

    seen = drive(git_project, scenario)
    assert seen[:2] == [False, None], "a coder: off, the role's default, sent as None"
    assert seen[2:4] == [True, None], "untouched, it followed the reviewer's default"
    assert seen[4] is False, "touched off over the reviewer's default: sent"
    assert seen[5:] == [False, None], "back to coder: off is coder's default again"


def test_a_resize_the_app_hands_on_late_is_waited_for(
    git_project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``resized`` waits for the layout, not for the app to look idle. With the app's hand-on
    of a resize held 150 ms, as a loaded runner's timer can hold it, a click on *Advanced*'s
    title at 80x24 still lands and unfolds it; after ``Pilot.resize_terminal`` alone it was
    aimed at the 120x50 dialog's title, off the screen."""
    hand_on = App._check_resize
    held: list[bool] = []

    def late(app: App[Any]) -> None:
        if app._resize_timer is not None and not held:
            held.append(True)
            app._resize_timer.stop()
            app._resize_timer = app.set_timer(0.15, lambda: late(app))
            return
        hand_on(app)

    monkeypatch.setattr(Host, "_check_resize", late)

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[object]:
        await resized(pilot, 80, 24)
        await open_advanced(pilot)
        return [held, advanced(dialog).collapsed]

    assert drive(git_project, scenario) == [[True], False]


def test_the_remote_control_note_wraps_at_80_columns(git_project: ProjectInfo) -> None:
    """At 80 columns the note beside the switch was laid out on one line wider than its row
    and painted cut, '…or the Cl' (#335's walk). It wraps in its row now. The label is
    *Claude Remote Control*, never main's *Remote control*, the phone remote's name in
    Settings (owner call W4-8, revised at the main sync), and it is painted whole."""

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[str]:
        await resized(pilot, 80, 45)
        await open_advanced(pilot)  # the row is in Advanced
        note = dialog.query_one("#spawn-remote-note", Static)
        note.scroll_visible(animate=False)
        await settle(pilot)
        row = note.parent
        assert row is not None
        label = row.query_one(Label)
        # What the screen paints there, not the layout: a line wider than its row is laid
        # out whole and painted cut (test_ui_remote.painted_in).
        update = host.screen._compositor.render_full_update()
        rows = ["".join(chop.text for chop in strip) for strip in update.strips]

        def painted(widget: Widget) -> str:
            x, y, width, height = widget.region
            return " ".join(" ".join(row[x : x + width] for row in rows[y : y + height]).split())

        return [painted(label), painted(note)]

    label, note = drive(git_project, scenario)
    assert label == "Claude Remote Control"
    assert note == "continue it from claude.ai/code or the Claude app"


@pytest.mark.parametrize("size", [(80, 24), (200, 50)], ids=["80x24", "200x50"])
def test_the_claude_remote_control_label_reads_on_one_line(
    git_project: ProjectInfo, size: tuple[int, int]
) -> None:
    """Owner call W4-8 (revised) named the switch *Claude Remote Control*, 21 characters,
    and the 18-column label column wrapped it, 'Claude Remote' over 'Control', at every
    width (#411's review). It is painted on one line, at 80 columns and wide."""

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[str]:
        await resized(pilot, *size)
        await open_advanced(pilot)
        switch = dialog.query_one("#spawn-remote", Switch)
        switch.scroll_visible(animate=False)
        await settle(pilot)
        row = switch.parent
        assert row is not None
        x, y, width, height = row.query_one(Label).content_region
        update = host.screen._compositor.render_full_update()
        rows = ["".join(chop.text for chop in strip) for strip in update.strips]
        return [line[x : x + width].strip() for line in rows[y : y + height]]

    assert drive(git_project, scenario) == ["Claude Remote Control"]


def test_every_row_of_the_open_dialog_fits_80x24_and_each_label_one_line(
    git_project: ProjectInfo, teammates: list[FleetAgentStatus]
) -> None:
    """The label column is as wide as its longest label: at 80x24, with *Advanced* open and
    a teammate picked (so the *How* row shows), every row's widgets fit inside it, and no
    label wraps. A note with no width of its own took the whole row and ran past its edge:
    *Pick…*'s was painted cut, '…fills Role, Binary'."""
    teammates.append(teammate(git_project, role="tester", label="tester-1"))

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[str]:
        await resized(pilot, 80, 24)
        select(dialog, "from").value = teammates[0].agent.id
        await settle(pilot)
        assert dialog.query_one("#spawn-how-row").display
        advanced(dialog).collapsed = False  # its title is below the fold at 24 rows
        await unfolded(pilot)
        problems: list[str] = []
        for row in dialog.query(".spawn-row"):
            if not row.display:
                continue
            shown = [child for child in row.children if child.display]
            widths = {
                child.id or type(child).__name__: child.outer_size.width
                + child.styles.margin.left
                + child.styles.margin.right
                for child in shown
            }
            if sum(widths.values()) > row.size.width:
                problems.append(f"{widths} > {row.size.width}")
            for label in row.query(Label):
                if label.content_size.height != 1:
                    problems.append(f"{label.content!s} wraps")
        return problems

    assert drive(git_project, scenario) == []


def test_the_manager_role_locks_the_label_and_a_live_manager_greys_it_out(
    git_project: ProjectInfo, monkeypatch: pytest.MonkeyPatch, spawns: SpawnRecorder
) -> None:
    async def pick_manager(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        overlay = select(dialog, "role").query_one(OptionList)
        greyed = overlay.get_option_at_index(role_choices([]).index("manager"))
        label_input(dialog).value = "coder-auth"
        await pilot.pause()
        select(dialog, "role").value = "manager"
        await pilot.pause()
        seen: list[Any] = [
            greyed.disabled,
            label_input(dialog).value,
            label_input(dialog).disabled,
            submit(dialog).disabled,
            note(dialog, "#spawn-role-note"),
        ]
        select(dialog, "role").value = "coder"
        await pilot.pause()
        seen.append(label_input(dialog).value)  # what was typed comes back with the role
        select(dialog, "role").value = "manager"
        await pilot.pause()
        await pilot.click("#spawn-submit")
        await settle(pilot)
        return seen

    free = drive(git_project, pick_manager)
    assert free == [False, "manager", True, False, None, "coder-auth"]
    assert spawns.calls == [(git_project.id, "manager", UNTOUCHED)]  # the label is the service's

    spawns.calls.clear()
    monkeypatch.setattr(fleet_service, "manager_of", lambda project: spawned_agent(project))
    live = drive(git_project, pick_manager)
    assert live[0] is True  # greyed out in the list
    assert live[3] is True and live[4] == "repo already has a manager — one per project"
    assert spawns.calls == []


def test_accounts_are_read_in_a_worker_and_listed_by_slot(git_project: ProjectInfo) -> None:
    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[str]:
        overlay = select(dialog, "account").query_one(OptionList)
        return [str(overlay.get_option_at_index(i).prompt) for i in range(overlay.option_count)]

    options = drive(
        git_project,
        scenario,
        accounts=lambda: overview((1, "me@example.com"), (2, None)),
    )
    assert options == [
        "(this shell's)",
        "1 · default · me@example.com",
        "2 · account 2 · not signed in",
    ]


def test_an_accounts_reader_that_fails_costs_the_list_not_the_dialog(
    git_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    def broken() -> AccountsOverview:
        raise OSError("permission denied: /accounts")

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> str | None:
        reason = note(dialog, "#spawn-account-note")
        await pilot.click("#spawn-submit")  # and the dialog still spawns
        await settle(pilot)
        return reason

    reason = drive(git_project, scenario, accounts=broken)
    assert reason == "accounts unavailable — OSError: permission denied: /accounts"
    assert spawns.calls == [(git_project.id, "coder", UNTOUCHED)]


# --- #12: the no-account option says what the spawn runs on ----------------------------------


def account_field(dialog: SpawnDialog) -> tuple[str, str]:
    """What the Account field shows closed, and its first option: the no-account one."""
    field = select(dialog, "account")
    overlay = field.query_one(OptionList)
    return shown(field.query_one("SelectCurrent #label", Static)), str(
        overlay.get_option_at_index(0).prompt
    )


def _account(slot: int, alias: str | None = None, position: int | None = None) -> ClaudeAccount:
    return ClaudeAccount(
        slot=slot, config_dir=Path(f"/accounts/{slot}"), alias=alias, position=position
    )


def test_the_no_account_option_says_what_the_spawn_runs_on_and_this_shells_only_when_none_is_in(
    git_project: ProjectInfo, spawns: SpawnRecorder, fake_home: Path
) -> None:
    """tester-tls on #406: with rank 1 the default, the Account field still opened on
    '(this shell's)', which read as if the spawn skipped the accounts, while it ran on rank 1.
    The option names what the spawn runs on, by the real ladder in this test's home; it reads
    '(this shell's)' only while no account is signed in. What is sent is unchanged: no
    ``--account``, so the spawn asks the same ladder when it runs."""
    from aisquare.core.config import AccountsSettings
    from tests.test_claude_accounts import _sign_in

    config = load_config()
    config.accounts = AccountsSettings(pick="default")  # the order alone: no usage read
    save_config(config)
    work = accounts_core.create_account()
    accounts_service.set_alias("2", "work")

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> tuple[str, str]:
        shown_now = account_field(dialog)
        await pilot.click("#spawn-submit")
        await settle(pilot)
        return shown_now

    assert drive(git_project, scenario) == ("(this shell's)", "(this shell's)")
    _sign_in(work, "work@example.com")
    accounts_service.set_default("2")  # rank 1
    signed = drive(git_project, scenario)
    assert signed == ("(default: rank 1 · work)", "(default: rank 1 · work)")
    picked = accounts_service.choose(None, role="coder", project=git_project).account
    assert picked is not None and picked.slot == 2, "the account the spawn itself runs on"
    assert [sent["account"] for _, _, sent in spawns.calls] == [None, None]  # no --account


def test_the_label_follows_the_role_and_says_which_rung_picked(git_project: ProjectInfo) -> None:
    """A role's binding is a rung of the ladder, so the option is read again for the role
    the field holds. Each rung reads as itself; a ladder that would refuse the spawn says
    so under the field, and the option reads '(default)'."""
    asked: list[tuple[str, str]] = []
    work = _account(3, alias="work", position=1)

    def defaults(
        project: ProjectInfo, role: str, args: list[str]
    ) -> accounts_service.LaunchDefault:
        asked.append((project.id, role))
        if role == "reviewer":
            raise accounts_service.NoSuchAccount("the role binding for 'reviewer' names 9")
        if role == "tester":
            choice = accounts_service.AccountChoice(_account(2), "role binding")
        else:
            choice = accounts_service.AccountChoice(work, "preferred")
        return accounts_service.LaunchDefault(choice)

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        seen: list[Any] = [account_field(dialog)[1]]
        for role in ("tester", "reviewer", "coder"):
            select(dialog, "role").value = role
            await settle(pilot)
            seen.append((account_field(dialog)[1], note(dialog, "#spawn-account-note")))
        return seen

    seen = drive(git_project, scenario, defaults=defaults)
    assert seen[0] == "(default: rank 1 · work)"
    assert seen[1] == ("(default: bound to tester · account 2)", None)
    assert seen[2] == (
        "(default)",
        "with no account the spawn is refused: the role binding for 'reviewer' names 9",
    )
    assert seen[3][0] == "(default: rank 1 · work)"
    assert [role for _, role in asked] == ["coder", "tester", "reviewer", "coder"]
    assert {project for project, _ in asked} == {git_project.id}


def test_a_model_in_the_extra_args_is_the_family_the_label_picks_by(
    git_project: ProjectInfo, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #424: the label read the model family from the role's args alone, where
    ``fleet.spawn`` adds the form's. With work (rank 1) over the line in its Opus week and
    spare with room, a spawn with ``--model opus`` in Extra agent args ran on spare under a
    label naming work. The form's args are read too, by the real ladder here, and read
    again when they name another model, not on every key typed."""
    from aisquare.core.config import AccountsSettings
    from tests.test_claude_accounts import NOW as ACCOUNTS_NOW
    from tests.test_usage_aware_accounts import _payload, _slot, _Usage

    config = load_config()
    config.accounts = AccountsSettings(pick="headroom")
    save_config(config)
    _slot("two@example.com", "tok-two")
    _slot("three@example.com", "tok-three")
    accounts_service.set_alias("2", "work")
    accounts_service.set_alias("3", "spare")
    out_of_opus = _payload(10.0, week=20.0)
    out_of_opus["seven_day_opus"] = {
        "utilization": 99.0,
        "resets_at": (ACCOUNTS_NOW + timedelta(days=2)).isoformat(),
    }
    endpoint = _Usage({"tok-two": out_of_opus, "tok-three": _payload(10.0, week=20.0)})
    monkeypatch.setattr(accounts_service, "_http_get", endpoint)
    reads: list[list[str]] = []

    def counted(project: ProjectInfo, role: str, args: list[str]) -> accounts_service.LaunchDefault:
        reads.append(list(args))
        return spawn_module.read_launch_default(project, role, args)

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[str]:
        field = dialog.query_one("#spawn-args", Input)
        seen: list[str] = []
        for value in ("--model sonnet", "--model sonnet --verbose", "--model opus"):
            field.value = value
            await settle(pilot)
            seen.append(account_field(dialog)[0])  # what the closed field shows
        return seen

    seen = drive(git_project, scenario, defaults=counted)
    assert seen == [
        "(default: headroom · work)",
        "(default: headroom · work)",  # another flag, the same model: not read again
        "(default: headroom · spare)",
    ]
    assert reads == [[], ["--model", "sonnet"], ["--model", "opus"]]
    spawned = accounts_service.choose(None, role="coder", project=git_project, family="opus")
    assert spawned.account is not None and spawned.account.slot == 3, "what the spawn runs on"


@pytest.mark.needs_captain
def test_the_captains_dialog_says_what_the_captain_runs_on(spawns: SpawnRecorder) -> None:
    """The captain starts through ``fleet.spawn`` on the home board as role ``captain``
    (``brain.start``), so its dialog asks the ladder for that role in that project and
    labels the option the same way."""
    home = captain_state.home_project()
    asked: list[tuple[str, str]] = []

    def defaults(
        project: ProjectInfo, role: str, args: list[str]
    ) -> accounts_service.LaunchDefault:
        asked.append((project.id, role))
        work = _account(2, alias="work", position=1)
        return accounts_service.LaunchDefault(accounts_service.AccountChoice(work, "headroom"))

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> tuple[str, str]:
        return account_field(dialog)

    shown_now = drive(
        home, scenario, presets={"role": fleet_service.CAPTAIN_ROLE}, defaults=defaults
    )
    assert shown_now == ("(default: headroom · work)", "(default: headroom · work)")
    assert asked == [(home.id, fleet_service.CAPTAIN_ROLE)]


def test_default_account_label_names_each_rung() -> None:
    def label(choice: accounts_service.AccountChoice, bound: bool = False) -> str:
        return spawn_module.default_account_label(
            accounts_service.LaunchDefault(choice, bound), "coder"
        )

    choice = accounts_service.AccountChoice
    work = _account(2, alias="work", position=2)
    assert label(choice(work, "preferred")) == "(default: rank 2 · work)"
    assert label(choice(work, "headroom")) == "(default: headroom · work)"
    assert label(choice(work, "role binding")) == "(default: bound to coder · work)"
    assert label(choice(work, "project default")) == "(default: project default · work)"
    assert label(choice(None, None)) == "(this shell's)"
    assert label(choice(None, None), bound=True) == "(default: coder's CLAUDE_CONFIG_DIR)"


# --- refusals, cancel, and the receipt --------------------------------------------------


def test_a_fleet_error_keeps_the_dialog_open_with_the_message_and_spawn_re_enables(
    git_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    def refuse(project: ProjectInfo, role: str) -> fleet_service.SpawnReceipt:
        raise fleet_service.FleetError("already runs 8 agents [max_agents_per_project = 8]")

    spawns.answer = refuse

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        await pilot.click("#spawn-submit")
        await settle(pilot)
        seen: list[Any] = [
            isinstance(host.screen, SpawnDialog),
            note(dialog, "#spawn-status"),
            submit(dialog).disabled,
            dialog.query_one("#spawn-cancel", Button).disabled,
        ]
        # Past the first press's "-active" effect, during which Button ignores a
        # click — then a second press must spawn again: re-enabled for real.
        await released(pilot, submit(dialog))
        await pilot.click("#spawn-submit")
        await settle(pilot)
        seen.append(list(host.results))
        return seen

    open_, status, disabled, cancel_disabled, results = drive(git_project, scenario)
    assert open_ is True
    assert status == "already runs 8 agents [max_agents_per_project = 8]"  # brackets kept
    assert (disabled, cancel_disabled) == (False, False)
    assert len(spawns.calls) == 2 and results == []


def test_a_fleet_error_that_lands_while_the_test_waits_keeps_the_dialog_open(
    git_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    """The fleet error above, landing after ``settle`` began: windows-latest's order, every run.

    ``settle`` was ``workers.wait_for_complete()``, which raises ``WorkerFailed`` for a
    worker that failed on purpose while it is still registered, and a refusal held past
    the click's pause always is (card tsk_01m3pt9eme0h, #234's Windows leg).
    """

    def refuse(project: ProjectInfo, role: str) -> fleet_service.SpawnReceipt:
        time.sleep(LATE)
        raise fleet_service.FleetError("already runs 8 agents [max_agents_per_project = 8]")

    spawns.answer = refuse

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        await pilot.click("#spawn-submit")
        await settle(pilot)
        return [
            isinstance(host.screen, SpawnDialog),
            note(dialog, "#spawn-status"),
            submit(dialog).disabled,
        ]

    open_, status, disabled = drive(git_project, scenario)
    assert open_ is True
    assert status == "already runs 8 agents [max_agents_per_project = 8]"
    assert disabled is False


def test_any_other_exception_lands_in_the_status_line_with_its_class_name(
    git_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    def crash(project: ProjectInfo, role: str) -> fleet_service.SpawnReceipt:
        raise KeyError("pane_id")

    spawns.answer = crash

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> tuple[bool, Any]:
        await pilot.click("#spawn-submit")
        await settle(pilot)
        return host.is_running and isinstance(host.screen, SpawnDialog), note(
            dialog, "#spawn-status"
        )

    alive, status = drive(git_project, scenario)
    assert alive is True
    assert status == "KeyError: 'pane_id'"


def test_escape_and_cancel_dismiss_with_none_and_call_nothing(
    git_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    async def by_escape(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        await pilot.press("escape")
        await pilot.pause()
        return [isinstance(host.screen, SpawnDialog), list(host.results)]

    async def by_cancel(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        await pilot.click("#spawn-cancel")
        await pilot.pause()
        return [isinstance(host.screen, SpawnDialog), list(host.results)]

    assert drive(git_project, by_escape) == [False, [None]]
    assert drive(git_project, by_cancel) == [False, [None]]
    assert spawns.calls == []


def test_escape_waits_for_a_spawn_that_has_started(
    git_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    """A started spawn cannot be taken back, so the dialog does not pretend to cancel it."""
    started, release = threading.Event(), threading.Event()

    def slow(project: ProjectInfo, role: str) -> fleet_service.SpawnReceipt:
        started.set()
        release.wait(timeout=10)
        return receipt_for(project)

    spawns.answer = slow

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        await pilot.click("#spawn-submit")
        await asyncio.to_thread(started.wait, 10)
        await pilot.pause()
        seen: list[Any] = [
            submit(dialog).disabled,
            dialog.query_one("#spawn-cancel", Button).disabled,
            note(dialog, "#spawn-status"),
        ]
        await pilot.press("escape")
        await pilot.pause()
        seen.append(isinstance(host.screen, SpawnDialog))
        release.set()
        await settle(pilot)
        seen.append(host.results)
        return seen

    seen = drive(git_project, scenario)
    assert seen[:3] == [True, True, "spawning coder …"]
    assert seen[3] is True  # Esc waited
    assert len(seen[4]) == 1 and isinstance(seen[4][0], fleet_service.SpawnReceipt)
    assert len(spawns.calls) == 1


def test_a_receipt_dismisses_toasts_it_and_its_notes_refreshes_and_opens_the_new_agent(
    tmp_path: Path,
    fleet: dict[str, list[FleetAgentStatus]],
    spawns: SpawnRecorder,
) -> None:
    (tmp_path / "alpha" / ".git").mkdir(parents=True)
    project = register(tmp_path / "alpha", project_id="prj_a", codename="amber-otter")
    notes = [
        "label 'coder-1' is held by a live agent — using 'coder-1-2'",
        "reused the worktree at [alpha]/.aisquare-worktrees/coder-1-2",
    ]

    def answer(target: ProjectInfo, role: str) -> fleet_service.SpawnReceipt:
        receipt = receipt_for(target, notes=notes, label="coder-1-2")
        fleet[target.id] = [FleetAgentStatus(agent=receipt.agent, state="working")]
        return receipt

    spawns.answer = answer

    async def scenario(pilot: Pilot[None], app: RecordingFleetApp) -> list[Any]:
        refreshes: list[int] = []
        original = app.refresh_data

        def counted(**kwargs: Any) -> None:
            refreshes.append(1)
            original(**kwargs)

        app.refresh_data = counted  # type: ignore[method-assign]
        await pilot.click(spawn_button(app))
        await pilot.pause()
        assert isinstance(app.screen, SpawnDialog)
        await pilot.click("#spawn-submit")
        await settle_workers(app)
        for _ in range(3):
            await pilot.pause()
        return [type(app.screen).__name__, app.notices, len(refreshes), app.content.current]

    screen, notices, refreshes, current = drive_app(scenario)
    agent = spawned_agent(project, label="coder-1-2")
    assert screen != "SpawnDialog"
    # The spawn's three, first and in order. What may follow is the new agent's
    # own pane saying `%9: (pane gone)` — no tmux server runs in this test.
    assert notices[:3] == [
        (f"✓ spawned coder-1-2 ({agent.id}) → asq-amber-otter %9", "information"),
        (notes[0], "warning"),
        (notes[1], "warning"),
    ]
    assert all(message.startswith("%9:") for message, _ in notices[3:]), notices[3:]
    assert refreshes >= 1
    assert current == f"agent-{agent.id}"  # the new agent's live pane is what shows next
    assert [call[1] for call in spawns.calls] == ["coder"]


async def _submit_spawn(pilot: Pilot[None], app: RecordingFleetApp) -> None:
    """Open prj_a's spawn dialog from its row and press Spawn."""
    await pilot.click(spawn_button(app))
    await pilot.pause()
    assert isinstance(app.screen, SpawnDialog)
    await pilot.click("#spawn-submit")


def test_a_spawn_whose_next_read_finds_the_store_unreadable_says_its_row_comes_next(
    tmp_path: Path,
    fleet: dict[str, list[FleetAgentStatus]],
    spawns: SpawnRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #272, m1: the frame read after a spawn found the store unreadable and kept
    the frame shown, and the select that waited for it said "that agent is no longer
    listed" of the agent just spawned. It says its row comes on the next refresh, as a
    restart's does, and moves nothing."""
    (tmp_path / "alpha" / ".git").mkdir(parents=True)
    register(tmp_path / "alpha", project_id="prj_a", codename="amber-otter")

    def unreadable() -> Any:
        raise sqlite3.OperationalError("database is locked")

    def answer(target: ProjectInfo, role: str) -> fleet_service.SpawnReceipt:
        receipt = receipt_for(target, label="coder-1-2")
        fleet[target.id] = [FleetAgentStatus(agent=receipt.agent, state="working")]
        monkeypatch.setattr(app_module, "store_session", unreadable)  # the read after it
        return receipt

    spawns.answer = answer

    async def scenario(pilot: Pilot[None], app: RecordingFleetApp) -> list[Any]:
        shown_before = app.content.current
        await _submit_spawn(pilot, app)
        await settle_workers(app)
        for _ in range(3):
            await pilot.pause()
        assert app.store_error is not None, "the premise: the read after it found no store"
        return [shown_before, app.content.current, app.notices]

    shown_before, current, notices = drive_app(scenario)
    assert ("that agent is no longer listed", "warning") not in notices, notices
    assert ("coder-1-2 spawned — its row appears on the next refresh", "information") in notices
    assert current == shown_before


def test_a_spawn_whose_read_outlives_the_users_move_leaves_them_where_they_went(
    tmp_path: Path,
    fleet: dict[str, list[FleetAgentStatus]],
    spawns: SpawnRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #272, m2: the new agent is opened off a frame read after the spawn, and a
    read that hung for seconds then moved the view and the focus off wherever the user
    had gone meanwhile. A restart's select never did (``_show_restarted``); nor does this."""
    (tmp_path / "alpha" / ".git").mkdir(parents=True)
    register(tmp_path / "alpha", project_id="prj_a", codename="amber-otter")
    spawned = threading.Event()
    held = threading.Event()
    gate = threading.Event()
    listing = fleet_service.list_agents

    def answer(target: ProjectInfo, role: str) -> fleet_service.SpawnReceipt:
        receipt = receipt_for(target, label="coder-1-2")
        fleet[target.id] = [FleetAgentStatus(agent=receipt.agent, state="working")]
        spawned.set()
        return receipt

    def hung(project: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        if spawned.is_set():  # the read after the spawn
            held.set()
            gate.wait(10)
        return listing(project, live_only=live_only)

    spawns.answer = answer
    list_agents_as(monkeypatch, hung)  # the tick reads through list_agents_many (#83)

    async def scenario(pilot: Pilot[None], app: RecordingFleetApp) -> list[Any]:
        try:
            await _submit_spawn(pilot, app)
            for _ in range(200):
                if held.is_set():
                    break
                await pilot.pause(0.02)
            assert held.is_set(), "the premise: the read after the spawn is held"
            app.post_message(AddProject())  # the user goes elsewhere meanwhile
            for _ in range(200):
                if app.content.current == "onboard":
                    break
                await pilot.pause(0.02)
        finally:
            gate.set()
        await settle_workers(app)
        for _ in range(3):
            await pilot.pause()
        return [app.content.current, app.notices]

    current, notices = drive_app(scenario)
    assert current == "onboard", "where the user went"
    assert not any("no longer listed" in message for message, _ in notices), notices


@pytest.mark.needs_captain
def test_a_first_captain_whose_next_read_fails_says_its_row_comes_next(
    fleet: dict[str, list[FleetAgentStatus]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #272, m3: the first captain ever spawned, and the captain's read in the
    frame after it failed. That frame keeps the last frame's home, which had none, so the
    select said "that agent is no longer listed" of the captain just started."""
    home = captain_state.home_project()
    listing = fleet_service.list_agents

    def failing(project: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        if project.id == home.id:
            raise fleet_service.FleetUnavailable("database is locked")
        return listing(project, live_only=live_only)

    monkeypatch.setattr(fleet_service, "list_agents", failing)
    captain = spawned_agent(home, label="captain", role=fleet_service.CAPTAIN_ROLE)

    async def scenario(pilot: Pilot[None], app: RecordingFleetApp) -> list[Any]:
        assert app.snapshot is not None and app.snapshot.home is None, "no home shown yet"
        shown_before = app.content.current
        app.spawn_finished(
            fleet_service.SpawnReceipt(
                agent=captain, asked_label=None, tmux_session="asq-home", notes=[]
            )
        )
        await settle_workers(app)
        for _ in range(3):
            await pilot.pause()
        return [shown_before, app.content.current, app.notices]

    shown_before, current, notices = drive_app(scenario)
    assert ("that agent is no longer listed", "warning") not in notices, notices
    assert ("captain spawned — its row appears on the next refresh", "information") in notices
    assert current == shown_before


def test_the_module_names_its_workers_apart_from_the_manager_tabs() -> None:
    from aisquare.cli.ui.views import project as project_view

    assert spawn_module.SPAWN_WORKER != project_view.SPAWN_WORKER


# --- the persona step (P4) --------------------------------------------------------------


def configure_role(role: str, **fields: object) -> None:
    config = load_config()
    config.fleet.roles[role] = config.fleet.roles.get(role, FleetRoleSettings()).model_copy(
        update=fields
    )
    save_config(config)


def option_prompts(dialog: SpawnDialog, name: str) -> list[str]:
    overlay = select(dialog, name).query_one(OptionList)
    return [str(overlay.get_option_at_index(i).prompt) for i in range(overlay.option_count)]


def test_the_fields_read_role_hand_off_and_account_then_advanced_who_runs_it_then_as_whom(
    git_project: ProjectInfo,
) -> None:
    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[list[str]]:
        folded = advanced(dialog)
        labels = list(dialog.query(".spawn-row > Label"))
        return [
            [str(label.render()) for label in labels if folded not in label.ancestors],
            [str(label.render()) for label in labels if folded in label.ancestors],
        ]

    basic, folded = drive(git_project, scenario)
    # The owner's three (tsk_01m5mm2sz23b): the role, the teammate it comes from with its
    # How row, the account. Then, folded, who runs it — the bind, the binary — and as whom.
    assert basic == ["Role", "Hand off from", "How", "Account"]
    assert folded[:4] == ["Bind", "Binary", "Persona", "Label"]


def test_the_persona_select_lists_none_and_the_catalogue_with_the_description_below(
    git_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        seen: list[Any] = [
            option_prompts(dialog, "persona"),
            note(dialog, "#spawn-persona-description"),
        ]
        select(dialog, "persona").value = "skeptic"
        await pilot.pause()
        seen.append(note(dialog, "#spawn-persona-description"))
        await pilot.click("#spawn-submit")
        await settle(pilot)
        return seen

    prompts, before, after = drive(git_project, scenario)
    assert prompts == (
        [
            "(none)",
            "captain · bundled",
            "careful · bundled",
            "mentor · bundled",
            "minimalist · bundled",
            "skeptic · bundled",
        ]
        if experimental.captain_enabled()
        else [
            "(none)",
            "careful · bundled",
            "mentor · bundled",
            "minimalist · bundled",
            "skeptic · bundled",
        ]
    )
    assert before is not None and before.startswith("(no persona")
    assert after == personas.resolve("skeptic", git_project.root).description
    assert spawns.calls[0][2]["persona"] == "skeptic"  # the recorder receives the choice


def test_the_roles_persona_is_preselected_and_follows_the_role_until_touched(
    git_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    configure_role("coder", persona="minimalist")

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        persona_field = select(dialog, "persona")
        seen: list[Any] = [persona_field.value]
        select(dialog, "role").value = "tester"
        await pilot.pause()
        seen.append(persona_field.value)
        select(dialog, "role").value = "coder"
        await pilot.pause()
        seen.append(persona_field.value)
        seen.append(dialog.spawn_kwargs()["persona"])  # untouched: the role's default -> None
        persona_field.value = "mentor"  # touched
        await pilot.pause()
        select(dialog, "role").value = "tester"
        await pilot.pause()
        seen.append(persona_field.value)
        await pilot.click("#spawn-submit")
        await settle(pilot)
        return seen

    seen = drive(git_project, scenario)
    assert seen == ["minimalist", "", "minimalist", None, "mentor"]
    assert spawns.calls[0][1] == "tester" and spawns.calls[0][2]["persona"] == "mentor"


def test_an_explicit_none_over_a_roles_default_is_sent_as_an_empty_name(
    git_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    """``spawn`` reads ``""`` as "no persona": the user's (none) beats the config."""
    configure_role("coder", persona="minimalist")

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> None:
        select(dialog, "persona").value = ""
        await pilot.pause()
        await pilot.click("#spawn-submit")
        await settle(pilot)

    drive(git_project, scenario)
    assert spawns.calls[0][2]["persona"] == ""


def test_presets_show_on_open_and_reach_the_recorder(
    git_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        seen: list[Any] = [
            select(dialog, "role").value,
            select(dialog, "persona").value,
            select(dialog, "account").value,
            dialog.query_one("#spawn-binary", Input).value,
            option_prompts(dialog, "role")[-1],
        ]
        await pilot.click("#spawn-submit")
        await settle(pilot)
        return seen

    seen = drive(
        git_project,
        scenario,
        presets={"persona": "skeptic", "role": "coder2", "account": "2", "binary": "claude2"},
        accounts=lambda: overview((1, "me@example.com"), (2, "two@example.com")),
    )
    assert seen == ["coder2", "skeptic", "2", "claude2", "coder2"]  # the seat is an option
    (_project_id, role, kwargs) = spawns.calls[0]
    assert role == "coder2"
    assert (kwargs["persona"], kwargs["account"], kwargs["binary"]) == ("skeptic", "2", "claude2")


def test_a_preset_account_shows_before_and_after_the_accounts_are_read(
    git_project: ProjectInfo,
) -> None:
    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        return [select(dialog, "account").value, option_prompts(dialog, "account")]

    value, prompts = drive(
        git_project, scenario, presets={"account": "7"}, accounts=lambda: overview((1, None))
    )
    assert value == "7"
    assert prompts[-1] == "7 (preset)"  # a slot the read did not produce still shows


async def wait_for_screen(pilot: Pilot[Any], kind: type[Any], seconds: float = 5.0) -> Any:
    for _ in range(int(seconds / 0.05)):
        if isinstance(pilot.app.screen, kind):
            return pilot.app.screen
        await pilot.pause(0.05)
    raise AssertionError(f"{kind.__name__} never opened; the screen is {pilot.app.screen!r}")


def picker_ids(picker: AttachTargetScreen) -> list[str]:
    listing = picker.query_one("#picker-list", OptionList)
    return [listing.get_option_at_index(i).id or "" for i in range(listing.option_count)]


def test_pick_opens_the_picker_in_new_order_and_fills_who_runs_it(
    git_project: ProjectInfo, monkeypatch: pytest.MonkeyPatch, spawns: SpawnRecorder
) -> None:
    settings_service.bind_role("coder2", agent_bin="claude2")
    list_agents_as(monkeypatch, lambda project, *, live_only=True: [])

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        seen: list[Any] = []
        await open_advanced(pilot)
        await pilot.click("#spawn-pick")
        picker = await wait_for_screen(pilot, AttachTargetScreen)
        await settle(pilot)
        listing = picker.query_one("#picker-list", OptionList)
        ids = picker_ids(picker)
        assert listing.highlighted is not None
        seen.append((picker.attach_intent, ids[0], ids[listing.highlighted], list(host.picks)))
        await pilot.press("enter")  # the highlighted bind
        await settle(pilot)
        seen.append((select(dialog, "role").value, dialog.query_one("#spawn-binary", Input).value))
        await pilot.pause(0.3)
        await pilot.click("#spawn-pick")
        picker = await wait_for_screen(pilot, AttachTargetScreen)
        await settle(pilot)
        picker.query_one("#picker-list", OptionList).highlighted = picker_ids(picker).index(
            "account:2"
        )
        await pilot.press("enter")
        await settle(pilot)
        seen.append(select(dialog, "account").value)
        await pilot.click("#spawn-submit")
        await settle(pilot)
        return seen

    seen = drive(
        git_project,
        scenario,
        accounts=lambda: overview((1, "me@example.com"), (2, "two@example.com")),
    )
    assert seen[0] == ("new", "section:binds", "bind:coder2", [git_project.id])
    assert seen[1] == ("coder2", "claude2")
    assert seen[2] == "2"
    (_project_id, role, kwargs) = spawns.calls[0]
    assert (role, kwargs["binary"], kwargs["account"]) == ("coder2", "claude2", "2")


def test_import_beside_the_persona_select_selects_what_it_imports(
    git_project: ProjectInfo, monkeypatch: pytest.MonkeyPatch, spawns: SpawnRecorder
) -> None:
    def answer(source: str, **_: object) -> personas_service.ImportResult:
        directory = dict(personas.layer_dirs(None))["user"] / "fresh-one"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text("---\ndescription: Fresh.\n---\nWork.\n", "utf-8")
        loaded = personas.load(directory, layer="user")
        return personas_service.ImportResult(persona=loaded, engine="copy", source=source)

    monkeypatch.setattr(personas_service, "import_source", answer)
    monkeypatch.setattr(personas_service, "importable_skills", lambda root: [])

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        await open_advanced(pilot)
        await pilot.click("#spawn-import")
        importer = await wait_for_screen(pilot, ImportPersonaScreen)
        await settle(pilot)
        importer.query_one("#import-source", Input).value = "./fresh-one"
        await pilot.pause()
        await pilot.click("#import-submit")
        await settle(pilot)
        seen = [
            type(host.screen).__name__,
            select(dialog, "persona").value,
            note(dialog, "#spawn-persona-description"),
        ]
        await pilot.click("#spawn-submit")
        await settle(pilot)
        return seen

    screen, value, description = drive(git_project, scenario)
    assert (screen, value, description) == ("SpawnDialog", "fresh-one", "Fresh.")
    assert spawns.calls[0][2]["persona"] == "fresh-one"


def test_new_account_from_the_picker_opens_the_accounts_page_and_starts_its_add_flow(
    tmp_path: Path, fleet: dict[str, list[FleetAgentStatus]], monkeypatch: pytest.MonkeyPatch
) -> None:
    register(tmp_path / "alpha", project_id="prj_a")
    started: list[object] = []
    monkeypatch.setattr(
        AccountsView, "begin_claude_sign_in", lambda self, slot: started.append(slot)
    )

    async def scenario(pilot: Pilot[None], app: RecordingFleetApp) -> str | None:
        app.post_message(NewAccountRequested())
        await pilot.pause()
        await pilot.pause()
        return app.content.current

    assert drive_app(scenario) == "accounts"
    assert started == [None]  # a fresh slot's sign-in, the page's own flow


def test_a_persona_this_project_lacks_still_shows_and_says_the_spawn_refuses_it(
    git_project: ProjectInfo,
) -> None:
    configure_role("coder", persona="ghost")

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        return [select(dialog, "persona").value, note(dialog, "#spawn-persona-description")]

    value, description = drive(git_project, scenario)
    assert value == "ghost"
    assert description is not None and "not one of this project's personas" in description


def test_a_sidebar_agent_row_shows_the_persona_badge_only_when_there_is_one(
    git_project: ProjectInfo,
) -> None:
    plain = spawned_agent(git_project)
    with_row = plain.model_copy(update={"persona": "skeptic"})
    now = datetime.now(tz=UTC)
    session = TeamSession(
        id="11111111",
        project_id=git_project.id,
        started_at=now,
        last_seen_at=now,
        persona="mentor",
    )

    def row(agent: FleetAgent, session: TeamSession | None = None) -> str:
        return agent_row_text(FleetAgentStatus(agent=agent, state="waiting", session=session)).plain

    assert "·" not in row(plain)
    assert row(with_row).endswith(" · skeptic")
    assert row(plain, session).endswith(" · mentor")  # the session's, when the row has none
    assert row(with_row, session).endswith(" · skeptic")  # the row's wins


# --- a hand-off from a teammate (HANDOFF, tsk_01m3ns5a736s) --------------------------------


@pytest.fixture(autouse=True)
def teammates(monkeypatch: pytest.MonkeyPatch) -> list[FleetAgentStatus]:
    """The teammates *Hand off from* offers: none, unless a test lists some."""
    listed: list[FleetAgentStatus] = []
    monkeypatch.setattr(fleet_service, "handoff_sources", lambda project: list(listed))
    return listed


class HandOffRecorder:
    """What ``fleet_service.hand_off`` is replaced by: every call, and a scripted answer."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Kwargs]] = []

    def __call__(
        self, project: ProjectInfo, source: str, **kwargs: object
    ) -> fleet_service.HandoffReceipt:
        self.calls.append((project.id, source, dict(kwargs)))
        started = receipt_for(project, label="coder-2", notes=["a spawn note"])
        return fleet_service.HandoffReceipt(
            mode="take_over" if kwargs.get("mode") == "take_over" else "fork",
            source=started.agent,
            started=started,
            resumed=True,
            notes=["a hand-off note", "a spawn note"],
        )


@pytest.fixture
def hand_offs(monkeypatch: pytest.MonkeyPatch) -> HandOffRecorder:
    recorder = HandOffRecorder()
    monkeypatch.setattr(fleet_service, "hand_off", recorder)
    return recorder


def teammate(
    project: ProjectInfo,
    *,
    label: str = "coder-1",
    role: str = "coder",
    state: str = "working",
    task_id: str | None = None,
    slot: int | None = 2,
    persona: str | None = "careful",
) -> FleetAgentStatus:
    agent = FleetAgent(
        id=f"agt_{label.replace('-', '')}",
        project_id=project.id,
        label=label,
        role=role,
        pane_id="%3",
        cwd=project.root,
        created_at=datetime.now(tz=UTC),
        tmux_socket=PRIVATE_SOCKET,
        task_id=task_id,
        account_slot=slot,
        persona=persona,
    )
    return FleetAgentStatus(agent=agent, state=state)


NO_CHANGE: Kwargs = {
    "mode": "fork",
    "fresh": False,
    "role": None,
    "account": None,
    "persona": None,
    "label": None,
    "prompt": None,
}
"""What a hand-off sends for a form that shows the teammate's own: its role, account and persona.
Its ``agent_id``, the row the form shows, is sent as well, and is each test's own."""


def test_the_hand_off_field_lists_the_open_teammates_and_none_is_todays_spawn(
    git_project: ProjectInfo,
    spawns: SpawnRecorder,
    hand_offs: HandOffRecorder,
    teammates: list[FleetAgentStatus],
) -> None:
    """The picker is ``fleet_service.handoff_sources``, each teammate with its state, role,
    task and account. ``(none)`` stays chosen until one is picked, the How row stays hidden,
    and Spawn sends exactly what it sent before the field existed."""
    teammates.extend(
        [teammate(git_project), teammate(git_project, label="coder-3", state="exited", slot=None)]
    )

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[str]:
        field = select(dialog, "from")
        shown_options = [str(prompt) for prompt, _ in field._options]
        assert field.value == "" and not dialog.query_one("#spawn-how-row").display
        await pilot.click("#spawn-submit")
        await settle(pilot)
        return shown_options

    options = drive(git_project, scenario)
    assert options == [
        "(none)",
        "coder-1 · working · coder · no task · slot 2",
        "coder-3 · exited · coder · no task · this shell's",
    ]
    assert spawns.calls == [(git_project.id, "coder", UNTOUCHED)] and hand_offs.calls == []


def test_a_teammate_prefills_role_account_and_persona_and_forks_by_default(
    git_project: ProjectInfo,
    spawns: SpawnRecorder,
    hand_offs: HandOffRecorder,
    teammates: list[FleetAgentStatus],
) -> None:
    """The owner's decisions: Fork is the default, Start fresh an unchecked box, and Role,
    Account and Persona prefilled from the teammate and still editable. A fork takes no
    task, so Task is locked at none; the button says what it does. Untouched, nothing is
    sent but the mode: the service reads None as the teammate's own."""
    teammates.append(teammate(git_project, role="tester", label="tester-1"))

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        select(dialog, "from").value = teammates[0].agent.id
        await settle(pilot)
        assert dialog.query_one("#spawn-how-row").display
        assert dialog.query_one("#spawn-fork", RadioButton).value
        assert not dialog.query_one("#spawn-fresh", Checkbox).value
        assert (select(dialog, "role").value, select(dialog, "account").value) == ("tester", "2")
        assert select(dialog, "persona").value == "careful"
        assert not select(dialog, "role").disabled and not select(dialog, "persona").disabled
        assert select(dialog, "task").value == NO_TASK and select(dialog, "task").disabled
        assert not label_input(dialog).disabled  # sent as None untouched: the service picks
        assert str(submit(dialog).label) == "Fork"
        await pilot.click("#spawn-submit")
        await settle(pilot)
        return list(host.results)

    results = drive(git_project, scenario)
    sent = {**NO_CHANGE, "agent_id": teammates[0].agent.id}
    assert hand_offs.calls == [(git_project.id, "tester-1", sent)] and spawns.calls == []
    [receipt] = results
    assert isinstance(receipt, fleet_service.SpawnReceipt)
    assert receipt.notes == ["a hand-off note", "a spawn note"]


def test_take_over_locks_the_label_and_task_to_the_teammates_and_asks_first(
    git_project: ProjectInfo,
    hand_offs: HandOffRecorder,
    teammates: list[FleetAgentStatus],
) -> None:
    """Take over keeps the teammate's label, task and tree, so those are locked to its own;
    and it stops the teammate, so one question comes first. Cancel starts nothing."""
    task = add_task(git_project, "Wire the auth flow")
    teammates.append(teammate(git_project, task_id=task.id))

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        select(dialog, "from").value = teammates[0].agent.id
        await settle(pilot)
        dialog.query_one("#spawn-take-over", RadioButton).value = True
        await settle(pilot)
        assert (label_input(dialog).value, label_input(dialog).disabled) == ("coder-1", True)
        assert (select(dialog, "task").value, select(dialog, "task").disabled) == (task.id, True)
        assert str(submit(dialog).label) == "Take over"
        await pilot.click("#spawn-submit")
        await settle(pilot)
        assert isinstance(host.screen, spawn_module.ConfirmTakeOverScreen)
        await pilot.click("#take-over-no")
        await settle(pilot)
        assert isinstance(host.screen, SpawnDialog) and hand_offs.calls == []
        # Pressed, not clicked, and the question awaited: a click right after the question
        # closed opened nothing on windows-latest (`NoMatches: '#take-over-yes' on
        # SpawnDialog()`, rc/p0 run 39985702872), the dialog not yet hit-testable again.
        submit(dialog).press()
        question = await wait_for_screen(pilot, spawn_module.ConfirmTakeOverScreen)
        # Pressed on the question itself for the same reason: a click the moment it was
        # pushed landed before it could be hit-tested, so no hand-off was ever asked for
        # (a gate under load at cc8d5e0c, captain on and off, and 1 in 5 alone).
        question.query_one("#take-over-yes", Button).press()
        await settle(pilot)
        return list(host.results)

    results = drive(git_project, scenario)
    sent = {**NO_CHANGE, "mode": "take_over", "agent_id": teammates[0].agent.id}
    assert hand_offs.calls == [(git_project.id, "coder-1", sent)]
    assert len(results) == 1


def test_start_fresh_and_a_changed_role_are_sent(
    git_project: ProjectInfo, hand_offs: HandOffRecorder, teammates: list[FleetAgentStatus]
) -> None:
    teammates.append(teammate(git_project))

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> None:
        select(dialog, "from").value = teammates[0].agent.id
        await settle(pilot)
        dialog.query_one("#spawn-fresh", Checkbox).value = True
        select(dialog, "role").value = "tester"
        await settle(pilot)
        assert select(dialog, "persona").value == "careful", "a chosen persona stays"
        await pilot.click("#spawn-submit")
        await settle(pilot)

    drive(git_project, scenario)
    [(_, source, sent)] = hand_offs.calls
    assert source == "coder-1" and (sent["fresh"], sent["role"]) == (True, "tester")
    assert sent["persona"] is None and sent["mode"] == "fork"


@pytest.mark.needs_captain
def test_the_captains_dialog_has_no_hand_off(
    git_project: ProjectInfo, teammates: list[FleetAgentStatus]
) -> None:
    teammates.append(teammate(git_project))

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> int:
        return len(dialog.query("#spawn-from"))

    assert drive(git_project, scenario, presets={"role": fleet_service.CAPTAIN_ROLE}) == 0


@pytest.mark.parametrize("take_over", [False, True])
def test_account_set_to_this_shells_is_sent_as_this_shells(
    git_project: ProjectInfo,
    hand_offs: HandOffRecorder,
    teammates: list[FleetAgentStatus],
    take_over: bool,
) -> None:
    """runner2-1's reopen of #234: the teammate runs on slot 2, and Account edited to
    (this shell's) was sent as None, which the service reads as the teammate's own slot.
    It is sent as THIS_SHELL_ACCOUNT, on Fork and on Take over."""
    teammates.append(teammate(git_project))

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> None:
        select(dialog, "from").value = teammates[0].agent.id
        await settle(pilot)
        if take_over:
            dialog.query_one("#spawn-take-over", RadioButton).value = True
            await settle(pilot)
        assert select(dialog, "account").value == "2"
        select(dialog, "account").value = ""  # (this shell's)
        await settle(pilot)
        await pilot.click("#spawn-submit")
        await settle(pilot)
        if take_over:
            await pilot.click("#take-over-yes")
            await settle(pilot)

    drive(git_project, scenario)
    [(_, _, sent)] = hand_offs.calls
    assert sent["mode"] == ("take_over" if take_over else "fork")
    assert sent["account"] == fleet_service.THIS_SHELL_ACCOUNT


# --- review of #240, R2-m5: the captain's label in the dialog ---------------------------------


@pytest.mark.needs_captain
def test_on_the_label_captain_is_refused_in_the_dialog_as_the_fleet_refuses_it(
    git_project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spawn was enabled for a coder labelled ``captain``, and the fleet then refused it. The
    dialog keeps the switch it opened with: flipped off while it is open, it still refuses."""

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        monkeypatch.setenv(experimental.CAPTAIN_ENV, "0")
        label_input(dialog).value = "captain"
        await pilot.pause()
        return [submit(dialog).disabled, note(dialog, "#spawn-label-rule")]

    disabled, rule = drive(git_project, scenario)
    assert disabled is True and rule is not None and "reserved for the captain" in rule


def test_off_the_label_captain_is_a_label_like_any_in_the_dialog(
    git_project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(experimental.CAPTAIN_ENV, raising=False)

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        label_input(dialog).value = "captain"
        await pilot.pause()
        return [submit(dialog).disabled, note(dialog, "#spawn-label-rule")]

    assert drive(git_project, scenario) == [False, None]


@pytest.mark.needs_captain
def test_a_declared_captain_role_shows_the_label_it_sends_and_gives_the_typed_one_back(
    git_project: ProjectInfo,
) -> None:
    """``_relabel`` kept the typed label for any role but the manager's, so picking a
    declared ``captain`` showed the typed label in a locked field, and sent none of it."""
    from aisquare.core.config import AppConfig, RoleLaunchProfile, TeamSettings

    save_config(AppConfig(team=TeamSettings(profiles={"captain": RoleLaunchProfile(bin="echo")})))

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        label_input(dialog).value = "coder-auth"
        await pilot.pause()
        select(dialog, "role").value = "captain"
        await pilot.pause()
        seen: list[Any] = [label_input(dialog).value, label_input(dialog).disabled]
        select(dialog, "role").value = "coder"
        await pilot.pause()
        return [*seen, label_input(dialog).value]

    assert drive(git_project, scenario) == ["captain", True, "coder-auth"]


def test_off_the_spawn_dialog_preset_to_a_declared_captain_is_an_ordinary_spawn(
    git_project: ProjectInfo, spawns: SpawnRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #240, R2-m3: ``_label_fixed`` and ``self._captain`` off."""
    from aisquare.core.config import AppConfig, RoleLaunchProfile, TeamSettings

    monkeypatch.delenv(experimental.CAPTAIN_ENV, raising=False)
    save_config(AppConfig(team=TeamSettings(profiles={"captain": RoleLaunchProfile(bin="echo")})))

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        label = label_input(dialog)
        seen: list[Any] = [label.disabled, select(dialog, "role").disabled]
        await pilot.click("#spawn-submit")
        await settle(pilot)
        return seen

    seen = drive(git_project, scenario, presets={"role": "captain"})
    assert seen == [False, False], "a label of its own and a role that can change"
    assert [call[1] for call in spawns.calls] == ["captain"], "through fleet.spawn"


# --- task #4: the spawn dialog's follow-ups from PR #303 -------------------------------------


def test_with_no_role_the_empty_persona_says_plain_claude_not_its_role_alone(
    git_project: ProjectInfo,
) -> None:
    """Role *(none)* has no role to run as: the note said "runs as its role alone" for it."""

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        seen: list[Any] = [
            select(dialog, "persona").value,
            note(dialog, "#spawn-persona-description"),
        ]
        select(dialog, "role").value = spawn_module.NO_ROLE
        await settle(pilot)
        seen.append(note(dialog, "#spawn-persona-description"))
        select(dialog, "role").value = "coder"
        await settle(pilot)
        seen.append(note(dialog, "#spawn-persona-description"))
        return seen

    persona, with_role, without, back = drive(git_project, scenario)
    assert persona == ""
    assert without == "(plain Claude: no role, no persona)"
    assert with_role == back == "(no persona — the agent runs as its role alone)"  # the control


def test_a_role_change_re_describes_the_note_under_the_owners_own_empty_persona(
    git_project: ProjectInfo,
) -> None:
    """The owner's pick is kept across a role change, so nothing re-puts the persona and
    its note was described only by the role change itself: with *(none)* picked by hand,
    Role *(none)* still said "runs as its role alone" (review of #405 in #401, n9)."""

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        select(dialog, "persona").value = "careful"
        await settle(pilot)
        select(dialog, "persona").value = ""  # the owner's own pick: no persona
        await settle(pilot)
        seen: list[Any] = [note(dialog, "#spawn-persona-description")]
        select(dialog, "role").value = spawn_module.NO_ROLE
        await settle(pilot)
        seen += [select(dialog, "persona").value, note(dialog, "#spawn-persona-description")]
        return seen

    with_role, kept, without = drive(git_project, scenario)
    assert with_role == "(no persona — the agent runs as its role alone)"  # the control
    assert kept == "", "the owner's pick stands"
    assert without == "(plain Claude: no role, no persona)"


def test_the_pick_note_shows_only_beside_a_pick_that_presses(
    git_project: ProjectInfo, teammates: list[FleetAgentStatus]
) -> None:
    """A hand-off disables *Pick…* (``_lock_other_screens``), and the note went on saying what
    it does; back to *(none)*, both come back."""
    teammates.append(teammate(git_project))

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        pick = dialog.query_one("#spawn-pick", Button)
        seen: list[Any] = [pick.disabled, note(dialog, "#spawn-pick-note")]
        select(dialog, "from").value = teammates[0].agent.id
        await settle(pilot)
        seen += [pick.disabled, note(dialog, "#spawn-pick-note")]
        select(dialog, "from").value = spawn_module.NO_SOURCE
        await settle(pilot)
        seen += [pick.disabled, note(dialog, "#spawn-pick-note")]
        return seen

    free, free_note, handed, handed_note, back, back_note = drive(git_project, scenario)
    assert (free, handed, back) == (False, True, False)
    assert free_note is not None and "fills Role, Binary and Account" in free_note
    assert handed_note is None
    assert back_note == free_note


@pytest.mark.needs_captain
def test_the_captains_dialog_hides_the_pick_note_beside_its_disabled_pick(
    git_project: ProjectInfo,
) -> None:
    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        return [dialog.query_one("#spawn-pick", Button).disabled, note(dialog, "#spawn-pick-note")]

    disabled, shown_note = drive(
        git_project, scenario, presets={"role": fleet_service.CAPTAIN_ROLE}
    )
    assert disabled is True and shown_note is None


# --- the Project field (tsk_01m5v6mvbqphwq8jtdw9sbc3he) -------------------------------------


@pytest.fixture
def other_project(tmp_path: Path) -> ProjectInfo:
    """A second project, not a repository: where the Project field moves the dialog to."""
    return register(tmp_path / "plain", project_id="prj_other", codename="ruby-fox")


def test_the_project_field_is_the_first_field_and_offers_every_project_in_the_order_given(
    git_project: ProjectInfo, other_project: ProjectInfo
) -> None:
    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        labels = [str(label.render()) for label in dialog.query(".spawn-row > Label")]
        field = select(dialog, "project")
        return [labels[:2], field.value, option_prompts(dialog, "project"), field.disabled]

    seen = drive(git_project, scenario, projects=[other_project, git_project])
    assert seen == [["Project", "Role"], git_project.id, ["plain", "repo"], False]


def test_two_projects_of_one_name_are_told_apart_by_codename_else_path(
    tmp_path: Path,
) -> None:
    first = register(tmp_path / "a" / "app", project_id="prj_a", codename="amber-otter")
    second = register(tmp_path / "b" / "app", project_id="prj_b", codename="ruby-fox")
    third = register(tmp_path / "c" / "app", project_id="prj_c", codename="jade-lynx")
    third = third.model_copy(update={"codename": None})  # a project that has no codename (yet)
    solo = register(tmp_path / "solo", project_id="prj_solo", codename="coral-wren")

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[str]:
        return option_prompts(dialog, "project")

    prompts = drive(first, scenario, projects=[first, second, third, solo])
    assert prompts == [
        "app · amber-otter",
        "app · ruby-fox",
        f"app · {short_path(third.root)}",
        "solo",  # a name of its own needs nothing after it
    ]


def test_a_dialog_opened_without_a_project_list_has_no_project_field(
    git_project: ProjectInfo,
) -> None:
    """The persona-first flow (the Personas tab) opens on its own project: nothing to pick."""

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        return [
            bool(dialog.query("#spawn-project")),
            shown(dialog.query_one("#spawn-header", Static)),
        ]

    has_field, header = drive(git_project, scenario, presets={"persona": "skeptic"})
    assert has_field is False and "repo · amber-otter" in header


@pytest.mark.needs_captain
def test_the_captains_dialog_has_no_project_field_whatever_it_is_offered(
    git_project: ProjectInfo, other_project: ProjectInfo
) -> None:
    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        return [bool(dialog.query("#spawn-project")), dialog.project.id, dialog._role]

    seen = drive(
        git_project,
        scenario,
        projects=[git_project, other_project],
        presets={"role": fleet_service.CAPTAIN_ROLE, "persona": "captain"},
    )
    assert seen == [False, git_project.id, "captain"]  # the home it was opened on, as ever


def test_the_spawn_goes_to_the_project_the_field_shows_and_the_header_follows(
    git_project: ProjectInfo, other_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[str]:
        header = [shown(dialog.query_one("#spawn-header", Static))]
        select(dialog, "project").value = other_project.id
        await settle(pilot)
        header.append(shown(dialog.query_one("#spawn-header", Static)))
        await pilot.click("#spawn-submit")
        await settle(pilot)
        return header

    before, after = drive(git_project, scenario, projects=[git_project, other_project])
    assert "repo · amber-otter" in before and "plain · ruby-fox" in after
    assert spawns.calls == [(other_project.id, "coder", {**UNTOUCHED, "worktree": False})]


def test_a_switch_from_a_git_project_to_a_plain_one_turns_the_worktree_off_and_back(
    git_project: ProjectInfo, other_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        switch = dialog.query_one("#spawn-worktree", Switch)

        def state() -> tuple[bool, bool, str]:
            why = shown(dialog.query_one("#spawn-worktree-note", Static))
            return switch.disabled, switch.value, why

        seen = [state()]
        select(dialog, "project").value = other_project.id
        await settle(pilot)
        seen.append(state())
        seen.append(
            dialog.spawn_kwargs()["worktree"]
        )  # a coder's default is a worktree: "off" is sent
        select(dialog, "project").value = git_project.id
        await settle(pilot)
        seen.append(state())
        seen.append(dialog.spawn_kwargs()["worktree"])
        return seen

    seen = drive(git_project, scenario, projects=[git_project, other_project])
    assert seen == [
        (False, True, ""),
        (True, False, "not a git repository"),
        False,
        (False, True, ""),  # back at the role's default
        None,
    ]


def test_a_switch_to_a_project_whose_manager_runs_greys_the_role_and_blocks_the_manager(
    git_project: ProjectInfo,
    other_project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    spawns: SpawnRecorder,
) -> None:
    monkeypatch.setattr(
        fleet_service,
        "manager_of",
        lambda project: spawned_agent(project) if project.id == other_project.id else None,
    )
    manager = role_choices([]).index("manager")

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        def state() -> list[Any]:
            greyed = select(dialog, "role").query_one(OptionList).get_option_at_index(manager)
            return [
                option_prompts(dialog, "role")[manager],
                greyed.disabled,
                label_input(dialog).value,
                label_input(dialog).disabled,
                submit(dialog).disabled,
                note(dialog, "#spawn-role-note"),
            ]

        select(dialog, "role").value = "manager"
        await settle(pilot)
        seen = [state()]
        select(dialog, "project").value = other_project.id
        await settle(pilot)
        seen.append(state())
        select(dialog, "project").value = git_project.id
        await settle(pilot)
        seen.append(state())
        return seen

    free, running, back = drive(git_project, scenario, projects=[git_project, other_project])
    assert free == ["manager", False, "manager", True, False, None]  # the label is locked
    assert running == [
        "manager — one per project, already running",
        True,
        "manager",
        True,
        True,  # Spawn waits: the role is the one this project already has
        "plain already has a manager — one per project",
    ]
    assert back == free
    assert spawns.calls == []


def test_a_switch_re_reads_hand_off_sources_and_task_options_and_leaves_none_chosen(
    git_project: ProjectInfo,
    other_project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    spawns: SpawnRecorder,
    hand_offs: HandOffRecorder,
) -> None:
    mine = add_task(git_project, "Wire the auth flow")
    theirs = add_task(other_project, "Fix the docs")
    short = theirs.id.removeprefix("tsk_")[: fleet_service.TASK_SHORT]
    listed = {
        git_project.id: [teammate(git_project, role="tester", label="tester-1")],
        other_project.id: [teammate(other_project, label="coder-7", slot=None)],
    }
    monkeypatch.setattr(fleet_service, "handoff_sources", lambda project: list(listed[project.id]))

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        select(dialog, "from").value = listed[git_project.id][0].agent.id
        await settle(pilot)
        chosen = [select(dialog, "role").value, select(dialog, "account").value]
        select(dialog, "task").value = mine.id  # a hand-off locks it; the switch frees it
        await settle(pilot)
        select(dialog, "project").value = other_project.id
        await settle(pilot)
        seen: list[Any] = [chosen]
        seen += [option_prompts(dialog, "from"), select(dialog, "from").value]
        seen += [dialog.query_one("#spawn-how-row").display, str(submit(dialog).label)]
        seen += [select(dialog, "role").value, select(dialog, "account").value]
        seen += [option_prompts(dialog, "task"), select(dialog, "task").value]
        seen += [select(dialog, "task").disabled, label_input(dialog).value]
        await pilot.click("#spawn-submit")
        await settle(pilot)
        return seen

    seen = drive(git_project, scenario, projects=[git_project, other_project])
    assert seen == [
        ["tester", "2"],  # the teammate's, prefilled
        ["(none)", "coder-7 · working · coder · no task · this shell's"],
        "",
        False,  # the How row goes with the teammate
        "Spawn",
        "coder",  # the plain form's own role and account, given back
        "",
        ["(none)", f"{short} [todo] Fix the docs"],
        NO_TASK,
        False,
        "coder-1",
    ]
    assert hand_offs.calls == []
    assert [call[0] for call in spawns.calls] == [other_project.id]


def test_a_switch_moves_the_label_unless_it_was_edited(
    git_project: ProjectInfo,
    other_project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    spawns: SpawnRecorder,
) -> None:
    monkeypatch.setattr(
        fleet_service,
        "next_label",
        lambda project, role, task_id=None: f"{role}-{project.root.name}",
    )

    async def untouched(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[str]:
        seen = [label_input(dialog).value]
        select(dialog, "project").value = other_project.id
        await settle(pilot)
        seen.append(label_input(dialog).value)
        return seen

    async def edited(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        label_input(dialog).value = "coder-auth"
        await pilot.pause()
        select(dialog, "project").value = other_project.id
        await settle(pilot)
        shown_label = label_input(dialog).value
        await pilot.click("#spawn-submit")
        await settle(pilot)
        return [shown_label, spawns.calls[0][2]["label"]]

    projects = [git_project, other_project]
    assert drive(git_project, untouched, projects=projects) == ["coder-repo", "coder-plain"]
    assert drive(git_project, edited, projects=projects) == ["coder-auth", "coder-auth"]


def test_a_switch_re_reads_the_persona_catalogue_and_the_roles_default_unless_one_was_picked(
    git_project: ProjectInfo, other_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    tempo = other_project.root / ".aisquare" / "personas" / "tempo" / "SKILL.md"
    tempo.parent.mkdir(parents=True)
    tempo.write_text("---\nname: tempo\ndescription: A tempo.\n---\nKeep tempo.\n", "utf-8")
    configure_role("coder", persona="tempo")  # this project (git_project) lacks it
    layer = personas.resolve("tempo", other_project.root).layer

    async def untouched(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        persona = select(dialog, "persona")
        seen: list[Any] = [persona.value, note(dialog, "#spawn-persona-description")]
        seen.append("tempo · " + layer in option_prompts(dialog, "persona"))
        select(dialog, "project").value = other_project.id
        await settle(pilot)
        seen += [persona.value, note(dialog, "#spawn-persona-description")]
        seen.append("tempo · " + layer in option_prompts(dialog, "persona"))
        seen.append(dialog.spawn_kwargs()["persona"])  # still the role's default: not sent
        return seen

    async def picked(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        select(dialog, "persona").value = "mentor"
        await pilot.pause()
        select(dialog, "project").value = other_project.id
        await settle(pilot)
        return [select(dialog, "persona").value, dialog.spawn_kwargs()["persona"]]

    projects = [git_project, other_project]
    seen = drive(git_project, untouched, projects=projects)
    assert seen[0] == "tempo" and "not one of this project's personas" in str(seen[1])
    assert seen[2] is False  # the catalogue of the first project lacks it
    assert seen[3:] == ["tempo", "A tempo.", True, None]
    assert drive(git_project, picked, projects=projects) == ["mentor", "mentor"]


def test_a_switch_keeps_account_binary_permission_mode_args_and_the_first_prompt(
    git_project: ProjectInfo, other_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> None:
        select(dialog, "account").value = "2"
        dialog.query_one("#spawn-binary", Input).value = "my-claude"
        select(dialog, "permission").value = "acceptEdits"
        dialog.query_one("#spawn-args", Input).value = "--model opus"
        dialog.query_one("#spawn-prompt", TextArea).text = "start with the tests"
        await pilot.pause()
        select(dialog, "project").value = other_project.id
        await settle(pilot)
        await pilot.click("#spawn-submit")
        await settle(pilot)

    drive(
        git_project,
        scenario,
        accounts=lambda: overview((1, "a@example.com"), (2, "b@example.com")),
        projects=[git_project, other_project],
    )
    assert spawns.calls == [
        (
            other_project.id,
            "coder",
            {
                **UNTOUCHED,
                "worktree": False,
                "account": "2",
                "binary": "my-claude",
                "permission_mode": "acceptEdits",
                "agent_args": ["--model", "opus"],
                "prompt": "start with the tests",
            },
        )
    ]


def test_a_switch_reads_the_no_account_label_again_for_the_new_project(
    git_project: ProjectInfo, other_project: ProjectInfo
) -> None:
    """#12: the option says what a spawn with no account runs on, and a project's own default
    account is a rung of that ladder, so the label follows the Project field. A ladder that
    refuses the spawn in the new project says so under the field, and the note goes when the
    project the field holds again does not."""
    asked: list[tuple[str, str]] = []
    work = _account(3, alias="work", position=1)

    def defaults(
        project: ProjectInfo, role: str, args: list[str]
    ) -> accounts_service.LaunchDefault:
        asked.append((project.id, role))
        if project.id == other_project.id:
            raise accounts_service.NoSuchAccount("the project default names 9")
        return accounts_service.LaunchDefault(accounts_service.AccountChoice(work, "preferred"))

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        seen: list[Any] = [(account_field(dialog)[1], note(dialog, "#spawn-account-note"))]
        for project in (other_project, git_project):
            select(dialog, "project").value = project.id
            await settle(pilot)
            seen.append((account_field(dialog)[1], note(dialog, "#spawn-account-note")))
        return seen

    seen = drive(git_project, scenario, defaults=defaults, projects=[git_project, other_project])
    assert seen == [
        ("(default: rank 1 · work)", None),
        ("(default)", "with no account the spawn is refused: the project default names 9"),
        ("(default: rank 1 · work)", None),
    ]
    assert asked == [
        (git_project.id, "coder"),
        (other_project.id, "coder"),
        (git_project.id, "coder"),
    ]


def test_the_project_field_waits_while_a_spawn_runs(
    git_project: ProjectInfo, other_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    gate = threading.Event()

    def slow(project: ProjectInfo, role: str) -> fleet_service.SpawnReceipt:
        gate.wait(timeout=10)
        return receipt_for(project)

    spawns.answer = slow

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[bool]:
        await pilot.click("#spawn-submit")
        await pilot.pause()
        seen = [select(dialog, "project").disabled]
        gate.set()
        await settle(pilot)
        return seen

    assert drive(git_project, scenario, projects=[git_project, other_project]) == [True]
    assert [call[0] for call in spawns.calls] == [git_project.id]


@pytest.fixture
def second_git_project(tmp_path: Path) -> ProjectInfo:
    """A second git checkout, for a switch that keeps the worktree switch on."""
    (tmp_path / "repo2" / ".git").mkdir(parents=True)
    return register(tmp_path / "repo2", project_id="prj_repo2", codename="jade-lynx")


def test_a_switch_frees_every_field_a_take_over_had_locked(
    git_project: ProjectInfo,
    second_git_project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    spawns: SpawnRecorder,
    hand_offs: HandOffRecorder,
) -> None:
    """A take-over keeps its teammate's label, task, tree and launch, so those fields are locked.
    The teammate belongs to the project left behind: the switch returns to the plain spawn
    and unlocks all of them, as picking *(none)* does."""
    task = add_task(git_project, "Wire the auth flow")
    listed = {
        git_project.id: [teammate(git_project, task_id=task.id)],
        second_git_project.id: [],
    }
    monkeypatch.setattr(fleet_service, "handoff_sources", lambda project: list(listed[project.id]))
    guarded = ("#spawn-binary", "#spawn-permission", "#spawn-args", "#spawn-prompt")

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        def state() -> list[Any]:
            return [
                str(submit(dialog).label),
                label_input(dialog).value,
                label_input(dialog).disabled,
                select(dialog, "task").disabled,
                select(dialog, "task").value,
                [dialog.query_one(selector).disabled for selector in guarded],
                dialog.query_one("#spawn-worktree", Switch).disabled,
                dialog.query_one("#spawn-worktree", Switch).value,
                dialog.query_one("#spawn-take-over", RadioButton).value,
                dialog.query_one("#spawn-how-row").display,
                select(dialog, "from").value,
            ]

        select(dialog, "from").value = listed[git_project.id][0].agent.id
        await settle(pilot)
        dialog.query_one("#spawn-take-over", RadioButton).value = True
        await settle(pilot)
        dialog.query_one("#spawn-fresh", Checkbox).value = True
        await settle(pilot)
        locked = state()
        select(dialog, "project").value = second_git_project.id
        await settle(pilot)
        freed = state()
        freed.append(dialog.query_one("#spawn-fresh", Checkbox).value)
        freed.append(note(dialog, "#spawn-how-note"))
        await pilot.click("#spawn-submit")
        await settle(pilot)
        return [locked, freed]

    locked, freed = drive(git_project, scenario, projects=[git_project, second_git_project])
    teammate_id = listed[git_project.id][0].agent.id
    assert locked == [
        "Take over",  # the button names what it does
        "coder-1",
        True,  # the label is the teammate's
        True,  # and so is the task
        task.id,
        [True, True, True, True],  # binary, permission, args, prompt
        True,  # worktree: the teammate's tree
        False,
        True,  # Take over chosen
        True,  # its How row shown
        teammate_id,
    ]
    assert freed == [
        "Spawn",
        "coder-1",
        False,
        False,
        NO_TASK,
        [False, False, False, False],
        False,  # worktree enabled again, for a git project
        True,  # at the role's default
        False,  # back on Fork
        False,  # How row hidden
        "",  # (none) chosen
        False,  # Start fresh cleared
        None,  # and no hand-off sentence left
    ]
    assert hand_offs.calls == []  # no hand-off was asked for
    assert [call[0] for call in spawns.calls] == [second_git_project.id]


def test_a_picked_persona_the_new_project_lacks_falls_back_to_the_roles_default_and_says_so(
    git_project: ProjectInfo, other_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    tempo = git_project.root / ".aisquare" / "personas" / "tempo" / "SKILL.md"
    tempo.parent.mkdir(parents=True)
    tempo.write_text("---\nname: tempo\ndescription: A tempo.\n---\nKeep tempo.\n", "utf-8")
    configure_role("coder", persona="minimalist")

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        select(dialog, "persona").value = "tempo"  # this project's own
        await pilot.pause()
        picked = [select(dialog, "persona").value, note(dialog, "#spawn-status")]
        select(dialog, "project").value = other_project.id
        await settle(pilot)
        fell: list[Any] = [select(dialog, "persona").value, note(dialog, "#spawn-status")]
        fell += [submit(dialog).disabled, dialog.spawn_kwargs()["persona"]]
        select(dialog, "persona").value = "mentor"  # a bundled one: in every project
        await pilot.pause()
        select(dialog, "project").value = git_project.id
        await settle(pilot)
        kept = [select(dialog, "persona").value, note(dialog, "#spawn-status")]
        return [picked, fell, kept]

    picked, fell, kept = drive(git_project, scenario, projects=[git_project, other_project])
    assert picked == ["tempo", None]
    assert fell == [
        "minimalist",  # the role's default, not "tempo — not one of this project's personas"
        "persona tempo is not one of plain's personas — using the role's default (minimalist)",
        False,  # Spawn is not blocked: nothing is left to refuse
        None,  # and the default is not sent as a choice
    ]
    assert kept == ["mentor", None]  # a pick the new project has survives, silently


def test_the_project_field_takes_the_focus_when_it_is_shown_else_the_role(
    git_project: ProjectInfo, other_project: ProjectInfo
) -> None:
    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> str | None:
        return dialog.focused.id if dialog.focused is not None else None

    projects = [git_project, other_project]
    assert drive(git_project, scenario, projects=projects) == "spawn-project"
    assert drive(git_project, scenario) == "spawn-role"


def test_the_header_and_the_manager_refusal_name_the_project_by_its_nickname(
    git_project: ProjectInfo,
    other_project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As the dropdown and the sidebar do (``display_name``), not by the folder."""
    named = git_project.model_copy(update={"nickname": "Docs site"})
    shared = other_project.model_copy(update={"nickname": "Shared board"})
    monkeypatch.setattr(
        fleet_service,
        "manager_of",
        lambda project: spawned_agent(project) if project.id == other_project.id else None,
    )

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        seen: list[Any] = [shown(dialog.query_one("#spawn-header", Static))]
        seen.append(option_prompts(dialog, "project"))
        select(dialog, "role").value = "manager"
        await settle(pilot)
        select(dialog, "project").value = other_project.id
        await settle(pilot)
        seen.append(shown(dialog.query_one("#spawn-header", Static)))
        seen.append(note(dialog, "#spawn-role-note"))
        return seen

    first, options, second, refusal = drive(named, scenario, projects=[named, shared])
    assert "Docs site · amber-otter" in first and "repo" not in first
    assert options == ["Docs site", "Shared board"]
    assert "Shared board · ruby-fox" in second and "plain" not in second
    assert refusal == "Shared board already has a manager — one per project"
