"""The Spawn dialog, driven headless: every field, every refusal, the receipt.

docs/plans/spawn-personas.md §4.1 and §7 "P3". ``fleet_service.spawn`` is a
recorder in every test, so nothing here starts tmux, git or an agent; what each
test asserts is the artefact the claim is about — the exact keywords the
recorder received, the text the dialog shows, the toasts the app raised, the
view the app switched to — never the string that was handed in.

Two hosts. A bare ``Host`` pushes the dialog for one project and records what it
dismissed with: that is the form. ``FleetApp`` itself drives the row that opens
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
import threading
from collections.abc import Callable, Coroutine, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import pytest
from textual.app import App
from textual.notifications import SeverityLevel
from textual.pilot import Pilot
from textual.widgets import Button, Input, OptionList, Select, Static, Switch, TextArea

from aisquare.cli.ui import spawn as spawn_module
from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.sidebar import SpawnAgent, SpawnRow, agent_row_text
from aisquare.cli.ui.spawn import (
    LABEL_RULE,
    NO_TASK,
    PICK_PENDING,
    PickTargetRequested,
    SpawnDialog,
    dice_label,
    role_choices,
)
from aisquare.core import codenames, harness, personas
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
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service

T = TypeVar("T")
SIZE = (120, 50)
PRIVATE_SOCKET = f"asq-test-{os.getpid()}-ui-spawn"
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
    wrong = [argv for argv in ran if _socket_of(argv) != PRIVATE_SOCKET]
    assert not wrong, f"a UI test addressed a tmux socket that is not the test's: {wrong[:2]}"


def register(
    root: Path, *, project_id: str = "prj_spawn", codename: str = "amber-otter"
) -> ProjectInfo:
    """A project in the isolated store, with its codename, rooted at ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    with store_session() as store:
        store.ensure_project(ProjectInfo(id=project_id, root=root))
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
    ) -> None:
        super().__init__()
        self._project = project
        self._accounts = accounts
        self._presets = presets or {}
        self.results: list[fleet_service.SpawnReceipt | None] = []
        self.notices: list[tuple[str, str]] = []
        self.picks: list[str] = []

    def on_mount(self) -> None:
        self.push_screen(
            SpawnDialog(self._project, accounts=self._accounts, **self._presets),
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
) -> T:
    """Run ``scenario`` against the dialog open over a bare host."""

    async def run() -> T:
        host = Host(project, accounts=accounts, presets=presets)
        async with host.run_test(size=SIZE) as pilot:
            await settle(pilot)
            dialog = host.screen
            assert isinstance(dialog, SpawnDialog)
            return await scenario(pilot, host, dialog)

    return asyncio.run(run())


async def settle(pilot: Pilot[Any]) -> None:
    """Let every worker finish, its state-change handler run, and the screen refresh."""
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.pause()


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

    monkeypatch.setattr(fleet_service, "list_agents", fake)
    return agents


def drive_app(scenario: Callable[[Pilot[None], RecordingFleetApp], Coroutine[Any, Any, T]]) -> T:
    async def run() -> T:
        app = RecordingFleetApp()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            return await scenario(pilot, app)

    return asyncio.run(run())


def spawn_row(app: FleetApp, project_id: str) -> SpawnRow:
    return next(row for row in app.query(SpawnRow) if row.project_id == project_id)


def test_the_spawn_row_opens_the_dialog_for_that_rows_project_and_no_toast(
    tmp_path: Path, fleet: dict[str, list[FleetAgentStatus]]
) -> None:
    register(tmp_path / "alpha", project_id="prj_a", codename="amber-otter")
    register(tmp_path / "beta", project_id="prj_b", codename="ruby-fox")

    async def scenario(pilot: Pilot[None], app: RecordingFleetApp) -> tuple[str, str, list[Any]]:
        await pilot.click(spawn_row(app, "prj_b"))
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, SpawnDialog), f"the row opened {type(dialog).__name__}"
        return dialog.project.id, shown(dialog.query_one("#spawn-header", Static)), app.notices

    project_id, header, notices = drive_app(scenario)
    assert project_id == "prj_b"  # the row's own project, not the first or the selected one
    assert "beta · ruby-fox" in header  # the header names it before anything happens
    assert notices == []  # the "not built yet" toast is gone


def test_a_spawn_row_for_a_project_that_left_the_frame_warns_and_opens_nothing(
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
        await pilot.click(spawn_row(app, "prj_a"))
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


def test_role_choices_are_the_fleet_roles_then_the_bound_ones_sorted() -> None:
    assert role_choices(["zeta", "coder", "alpha"]) == [
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


def test_the_manager_role_locks_the_label_and_a_live_manager_greys_it_out(
    git_project: ProjectInfo, monkeypatch: pytest.MonkeyPatch, spawns: SpawnRecorder
) -> None:
    async def pick_manager(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        overlay = select(dialog, "role").query_one(OptionList)
        greyed = overlay.get_option_at_index(list(fleet_service.FLEET_ROLES).index("manager"))
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
        # Past the first press's 0.2 s "-active" effect, during which Button ignores
        # a click — then a second press must spawn again: re-enabled for real.
        await pilot.pause(submit(dialog).active_effect_duration + 0.1)
        await pilot.click("#spawn-submit")
        await settle(pilot)
        seen.append(list(host.results))
        return seen

    open_, status, disabled, cancel_disabled, results = drive(git_project, scenario)
    assert open_ is True
    assert status == "already runs 8 agents [max_agents_per_project = 8]"  # brackets kept
    assert (disabled, cancel_disabled) == (False, False)
    assert len(spawns.calls) == 2 and results == []


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

        def counted() -> None:
            refreshes.append(1)
            original()

        app.refresh_data = counted  # type: ignore[method-assign]
        await pilot.click(spawn_row(app, "prj_a"))
        await pilot.pause()
        assert isinstance(app.screen, SpawnDialog)
        await pilot.click("#spawn-submit")
        ours = [worker for worker in app.workers if worker.group != "_loader"]
        await app.workers.wait_for_complete(ours)
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


def test_the_fields_read_who_runs_it_then_as_whom(git_project: ProjectInfo) -> None:
    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[str]:
        return [str(label.render()) for label in dialog.query(".spawn-row > Label")]

    labels = drive(git_project, scenario)
    assert labels[:5] == ["Role", "Account", "Binary", "Persona", "Label"]


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
    assert prompts == [
        "(none)",
        "careful · bundled",
        "mentor · bundled",
        "minimalist · bundled",
        "skeptic · bundled",
    ]
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


def test_pick_posts_pick_target_requested_and_says_the_picker_is_next(
    git_project: ProjectInfo,
) -> None:
    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        await pilot.click("#spawn-pick")
        await pilot.pause()
        return [host.picks, host.notices, type(host.screen).__name__]

    picks, notices, screen = drive(git_project, scenario)
    assert picks == [git_project.id]
    assert (PICK_PENDING, "information") in notices
    assert screen == "SpawnDialog"  # nothing else happened


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
