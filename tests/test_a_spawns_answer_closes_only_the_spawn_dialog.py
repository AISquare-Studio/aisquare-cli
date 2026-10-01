"""A running spawn keeps its dialog: nothing opens over it, and its answer closes only it.

Review of #240, finding 5. While a spawn ran (tmux, git and the first-prompt wait take
seconds) only *Cancel* and *Spawn* were disabled. *Import…* and *Pick…* stayed live, and
Textual 8.2's ``Screen.dismiss`` pops the screen ON TOP, which need not be the screen it is
called on. So a receipt that landed under the import dialog closed the import dialog, the
Spawn dialog stayed on "spawning coder …" with *Cancel*, *Spawn* and ``Esc`` all waiting
for an answer that had already come, and the TUI had to be killed. The picker's *+ New
account* dismissed the running dialog with ``None`` instead, and the new agent's receipt
and notes were lost.

What holds now, on the bare host of ``tests/test_ui_spawn.py`` (its recorder in front of
``fleet_service.spawn``, its private tmux socket):

- while a spawn, a hand-off or the captain's start runs, the two buttons that open a
  screen over the dialog are disabled, and they come back with *Cancel* and *Spawn* once
  it has answered. *Pick…* comes back only as far as the form allows it: not for the
  captain, not while a teammate is chosen;
- an answer that finds another screen on top closes nothing. It waits until the dialog is
  the active screen again: then a receipt dismisses the dialog, and a refusal is shown;
- *+ New account* does not dismiss a dialog whose spawn has not answered yet.

Every spawn here is HELD in its worker (:class:`Held`) until the test has put the dialog
where the claim needs it, so no test depends on which side wins a race.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import Button

from aisquare.cli.ui import spawn as spawn_module
from aisquare.cli.ui.attach import (
    TARGETS_WORKER,
    AttachTargetScreen,
    NewAccountRequested,
    Target,
)
from aisquare.cli.ui.persona_dialogs import SKILLS_WORKER, ImportPersonaScreen
from aisquare.cli.ui.spawn import PickTargetRequested, SpawnDialog
from aisquare.models import FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import personas as personas_service
from tests import test_ui_spawn as spawn_suite
from tests.test_ui_spawn import (
    Host,
    SpawnRecorder,
    drive,
    note,
    receipt_for,
    select,
    settle,
    teammate,
    wait_for_screen,
)
from tests.ui_workers import settle_page

# The Spawn dialog suite's fixtures, bound here so pytest finds them for this module's tests
# (``no_real_tmux``, ``spawns`` and ``teammates`` are autouse there: no test reaches tmux,
# ``fleet_service.spawn`` is a recorder, and *Hand off from* lists only what a test adds).
no_real_tmux = spawn_suite.no_real_tmux
spawns = spawn_suite.spawns
teammates = spawn_suite.teammates
git_project = spawn_suite.git_project

REFUSAL = "already runs 8 agents [max_agents_per_project = 8]"
NOTES = ["label 'coder-1' is held by a live agent — using 'coder-1-2'"]
"""What a receipt carries besides its agent: lost with it when the dialog closed with ``None``."""

BUTTONS = {
    "spawn": "#spawn-submit",
    "cancel": "#spawn-cancel",
    "pick": "#spawn-pick",
    "import": "#spawn-import",
}
"""The dialog's four ways on: the two that answer it, and the two that open a screen over it."""
ALL_LOCKED = dict.fromkeys(BUTTONS, True)
ALL_FREE = dict.fromkeys(BUTTONS, False)

EITHER_ORDER = pytest.mark.parametrize(
    "resume_first", [False, True], ids=["pick-first", "resume-first"]
)
"""The two orders a dialog can hear that its picker closed with a pick (``choose_new_account``)."""


# --- helpers ----------------------------------------------------------------------------


class Held:
    """``fleet_service.spawn``'s answer, held in the worker until the test releases it."""

    def __init__(self, *, refuses: bool = False) -> None:
        self.refuses = refuses
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, project: ProjectInfo, role: str) -> fleet_service.SpawnReceipt:
        self.started.set()
        self.release.wait(timeout=10)
        if self.refuses:
            raise fleet_service.FleetError(REFUSAL)
        return receipt_for(project, notes=NOTES)


def locked(dialog: SpawnDialog) -> dict[str, bool]:
    """Which of the dialog's four buttons are disabled right now."""
    return {name: dialog.query_one(button, Button).disabled for name, button in BUTTONS.items()}


def notes_of(results: list[fleet_service.SpawnReceipt | None]) -> list[list[str] | None]:
    """What the dialog was dismissed with, each time: a receipt's notes, or ``None``."""
    return [receipt.notes if receipt is not None else None for receipt in results]


async def quiet(pilot: Pilot[None], *, readers: str = spawn_module.ACCOUNTS_WORKER) -> None:
    """``settle`` for a test that holds the spawn: every message handled, and of the workers
    only the ``readers`` group waited for. A plain ``settle`` waits for the held one too."""
    await settle_page(pilot.app, group=readers)


async def spawn_and_hold(pilot: Pilot[None], held: Held) -> None:
    """Press the dialog's button and return once its worker is inside the held service call."""
    await pilot.click("#spawn-submit")
    assert await asyncio.to_thread(held.started.wait, 10), "the spawn never reached the service"
    await quiet(pilot)


async def heard(pilot: Pilot[None], dialog: SpawnDialog) -> None:
    """Let the released spawn answer, and the DIALOG hear it while another screen is on top.

    ``settle`` waits for the worker and for the screen on top; a screen under a modal is not
    counted (``settle_page``). So the dialog is asked itself: a callback queued on it now
    runs after the state change the finished worker has posted to it.
    """
    await settle(pilot)
    done = asyncio.Event()
    assert dialog.call_later(done.set), "the dialog is closed: there is nothing left to hear"
    await asyncio.wait_for(done.wait(), 5)
    await settle(pilot)


async def choose_new_account(
    pilot: Pilot[None], dialog: SpawnDialog, picker: AttachTargetScreen, *, resume_first: bool
) -> None:
    """Close the picker with *+ New account*, in either order its dialog can hear of it.

    A closing screen hands its result back as a ``call_next`` on the screen that opened it
    and then pops, which posts that screen a ``ScreenResume``. Textual runs the hand-back
    first: that is the press. Unless a message is queued right behind the resume, when the
    resume is handled first: built here with no ``await`` between the two lines, so the
    dialog's queue is exactly the resume and one callback behind it.
    """
    if resume_first:
        picker.dismiss(Target("new-account"))
        dialog.call_later(lambda: None)
    else:
        await pilot.click("#picker-new-account")
    await settle(pilot)


@pytest.fixture
def no_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    """The picker's Agents section reads ``list_agents``, which asks tmux: none listed."""
    monkeypatch.setattr(fleet_service, "list_agents", lambda project, *, live_only=True: [])


@pytest.fixture
def no_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    """The import dialog's Browse list reads the machine's Claude Code skills: none found."""
    monkeypatch.setattr(personas_service, "importable_skills", lambda root: [])


@pytest.fixture
def new_accounts(monkeypatch: pytest.MonkeyPatch) -> list[NewAccountRequested]:
    """Every *+ New account* that reached the app, where the shell opens the Accounts page."""
    reached: list[NewAccountRequested] = []

    def record(self: Host, event: NewAccountRequested) -> None:
        reached.append(event)

    monkeypatch.setattr(Host, "on_new_account_requested", record, raising=False)
    return reached


# --- nothing opens over a running spawn -------------------------------------------------


def test_import_and_pick_are_disabled_while_a_spawn_runs_and_come_back_with_cancel_and_spawn(
    git_project: ProjectInfo, spawns: SpawnRecorder
) -> None:
    held = Held(refuses=True)
    spawns.answer = held

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        seen: list[Any] = [locked(dialog)]
        try:
            await spawn_and_hold(pilot, held)
            seen += [locked(dialog), note(dialog, "#spawn-status")]
        finally:
            held.release.set()
        await settle(pilot)
        seen += [locked(dialog), note(dialog, "#spawn-status")]
        return seen

    before, during, waiting, after, refused = drive(git_project, scenario)
    assert before == ALL_FREE  # the control: the plain form opens with all four live
    assert during == ALL_LOCKED and waiting == "spawning coder …"
    assert after == ALL_FREE and refused == REFUSAL  # together, once the answer is in


@pytest.mark.parametrize("button", ["#spawn-import", "#spawn-pick"], ids=["import", "pick"])
def test_a_click_on_import_or_pick_during_a_spawn_opens_nothing_and_the_receipt_closes_the_dialog(
    git_project: ProjectInfo,
    spawns: SpawnRecorder,
    no_agents: None,
    no_skills: None,
    button: str,
) -> None:
    """The finding's own steps: *Spawn*, then *Import…* (or *Pick…*) during the wait.

    The click opened that dialog over the running spawn, the receipt then closed IT, and
    the Spawn dialog stayed on "spawning coder …" for good, every way out disabled."""
    held = Held()
    spawns.answer = held

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        seen: list[Any] = []
        try:
            await spawn_and_hold(pilot, held)
            await pilot.click(button)
            await quiet(pilot)
            seen.append(type(host.screen).__name__)
        finally:
            held.release.set()
        await settle(pilot)
        return [*seen, isinstance(host.screen, SpawnDialog), notes_of(host.results)]

    on_top, still_open, results = drive(git_project, scenario)
    assert on_top == "SpawnDialog"  # the click opened nothing over the running spawn
    assert still_open is False
    assert results == [NOTES]  # one receipt, notes and all


def test_a_hand_off_locks_import_while_it_runs_and_pick_stays_locked_for_the_teammate(
    git_project: ProjectInfo,
    teammates: list[FleetAgentStatus],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hand-off runs in the same worker. *Pick…* was already the teammate's lock, and a
    refused hand-off must not free it: "come back" means back to what the form allows."""
    teammates.append(teammate(git_project))
    held = Held(refuses=True)
    monkeypatch.setattr(
        spawn_module, "hand_off", lambda project, source, kwargs: held(project, source)
    )

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        select(dialog, "from").value = "coder-1"
        await settle(pilot)
        seen: list[Any] = [locked(dialog)]
        try:
            await spawn_and_hold(pilot, held)
            seen.append(locked(dialog))
        finally:
            held.release.set()
        await settle(pilot)
        seen += [locked(dialog), note(dialog, "#spawn-status")]
        return seen

    before, during, after, refused = drive(git_project, scenario)
    assert before == {**ALL_FREE, "pick": True}  # a teammate decides who runs it
    assert during == ALL_LOCKED
    assert after == before and refused == REFUSAL


def test_the_captains_start_locks_import_while_it_runs_and_pick_stays_locked_for_the_captain(
    git_project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = Held(refuses=True)
    monkeypatch.setattr(
        spawn_module, "start_captain", lambda kwargs: held(git_project, fleet_service.CAPTAIN_ROLE)
    )

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        seen: list[Any] = [locked(dialog)]
        try:
            await spawn_and_hold(pilot, held)
            seen.append(locked(dialog))
        finally:
            held.release.set()
        await settle(pilot)
        seen += [locked(dialog), note(dialog, "#spawn-status")]
        return seen

    before, during, after, refused = drive(
        git_project, scenario, presets={"role": fleet_service.CAPTAIN_ROLE}
    )
    assert before == {**ALL_FREE, "pick": True}  # the captain's role is fixed: nothing to pick
    assert during == ALL_LOCKED
    assert after == before and refused == REFUSAL


def test_unpicking_a_teammate_during_a_spawn_does_not_bring_pick_back_before_the_answer(
    git_project: ProjectInfo, spawns: SpawnRecorder, teammates: list[FleetAgentStatus]
) -> None:
    """The form's fields stay live while a spawn runs, and *Hand off from* locks and frees
    *Pick…* on its own account. Freed by it mid-spawn, *Pick…* was live again."""
    teammates.append(teammate(git_project))
    held = Held(refuses=True)
    spawns.answer = held

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        seen: list[Any] = []
        try:
            await spawn_and_hold(pilot, held)
            select(dialog, "from").value = "coder-1"
            await quiet(pilot)
            select(dialog, "from").value = ""  # back to (none): the plain form frees Pick…
            await quiet(pilot)
            seen.append(locked(dialog))
        finally:
            held.release.set()
        await settle(pilot)
        return [*seen, locked(dialog)]

    during, after = drive(git_project, scenario)
    assert during == ALL_LOCKED
    assert after == ALL_FREE


# --- the answer closes only its own dialog ----------------------------------------------


def test_a_receipt_that_lands_under_another_screen_leaves_it_open_and_closes_the_dialog_after_it(
    git_project: ProjectInfo, spawns: SpawnRecorder, no_skills: None
) -> None:
    """A screen is on top all the same (a press that was already on its way): the receipt
    dismissed THAT screen and never the dialog. It waits for the dialog to be on top."""
    held = Held()
    spawns.answer = held

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        importer = ImportPersonaScreen(None)
        try:
            await spawn_and_hold(pilot, held)
            await host.push_screen(importer)
            await quiet(pilot, readers=SKILLS_WORKER)
        finally:
            held.release.set()
        await heard(pilot, dialog)
        seen: list[Any] = [host.screen is importer, notes_of(host.results)]
        await pilot.press("escape")  # the import dialog's own Cancel
        await settle(pilot)
        return [*seen, type(host.screen).__name__, notes_of(host.results)]

    stayed, early, on_top, results = drive(git_project, scenario)
    # The answer closed nothing that was not its own, and the receipt waits with the dialog:
    # handed to nobody yet.
    assert (stayed, early) == (True, [])
    assert on_top not in ("SpawnDialog", "ImportPersonaScreen")  # both closed, each by its own
    assert results == [NOTES]  # exactly one receipt, not lost


def test_a_refusal_that_lands_under_another_screen_is_shown_once_that_screen_closes(
    git_project: ProjectInfo, spawns: SpawnRecorder, no_agents: None
) -> None:
    """The same wait for a ``FleetError``: it closes nothing, and the dialog is not left on
    "spawning …" when the screen above closes. It shows the reason, all four buttons back."""
    held = Held(refuses=True)
    spawns.answer = held

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        picker = AttachTargetScreen(git_project, persona=None, intent="new", accounts=None)
        try:
            await spawn_and_hold(pilot, held)
            await host.push_screen(picker)
            await quiet(pilot, readers=TARGETS_WORKER)
        finally:
            held.release.set()
        await heard(pilot, dialog)
        stayed = host.screen is picker
        await pilot.press("escape")  # the picker's own Cancel
        await settle(pilot)
        return [
            stayed,
            host.screen is dialog,
            note(dialog, "#spawn-status"),
            locked(dialog),
            notes_of(host.results),
        ]

    stayed, on_top, status, buttons, results = drive(git_project, scenario)
    assert stayed is True
    assert (on_top, results) == (True, [])  # a refusal keeps the dialog, as it always did
    assert status == REFUSAL
    assert buttons == ALL_FREE


# --- + New account never costs a receipt ------------------------------------------------


def test_new_account_on_a_dialog_that_is_not_spawning_still_leaves_for_the_accounts_page(
    git_project: ProjectInfo, no_agents: None, new_accounts: list[NewAccountRequested]
) -> None:
    """The control: with no spawn to wait for, *+ New account* is the navigation it was."""

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        await pilot.click("#spawn-pick")
        await wait_for_screen(pilot, AttachTargetScreen)
        await settle(pilot)
        await pilot.click("#picker-new-account")
        await settle(pilot)
        return [type(host.screen).__name__, notes_of(host.results), len(new_accounts)]

    on_top, results, asked = drive(git_project, scenario)
    assert on_top not in ("SpawnDialog", "AttachTargetScreen")
    assert results == [None] and asked == 1


def test_new_account_during_a_running_spawn_does_not_dismiss_the_dialog_and_the_receipt_arrives(
    git_project: ProjectInfo,
    spawns: SpawnRecorder,
    no_agents: None,
    new_accounts: list[NewAccountRequested],
) -> None:
    """*Pick…* → *+ New account* dismissed the running dialog with ``None``: the agent was
    spawned all the same, and its receipt and notes were lost with the dialog."""
    held = Held()
    spawns.answer = held

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        seen: list[Any] = []
        try:
            await spawn_and_hold(pilot, held)
            dialog.post_message(PickTargetRequested(git_project.id))  # a press on its way
            await wait_for_screen(pilot, AttachTargetScreen)
            await quiet(pilot, readers=TARGETS_WORKER)
            await pilot.click("#picker-new-account")
            await quiet(pilot)
            seen += [host.screen is dialog, notes_of(host.results), len(new_accounts)]
        finally:
            held.release.set()
        await settle(pilot)
        return [*seen, isinstance(host.screen, SpawnDialog), notes_of(host.results)]

    on_top, early, asked, still_open, results = drive(git_project, scenario)
    assert (on_top, early) == (True, [])  # not dismissed with None: its answer is on the way
    assert asked == 0  # nor does the shell leave for the Accounts page under it
    assert still_open is False
    assert results == [NOTES]


@EITHER_ORDER
def test_new_account_from_a_picker_the_receipt_landed_under_closes_the_dialog_with_the_receipt(
    git_project: ProjectInfo,
    spawns: SpawnRecorder,
    no_agents: None,
    new_accounts: list[NewAccountRequested],
    resume_first: bool,
) -> None:
    """Both at once: the receipt waits under the picker, and the picker then closes with
    *+ New account*. The dialog dismisses once, with the receipt, never with ``None``."""
    held = Held()
    spawns.answer = held

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        try:
            await spawn_and_hold(pilot, held)
            dialog.post_message(PickTargetRequested(git_project.id))  # a press on its way
            picker: AttachTargetScreen = await wait_for_screen(pilot, AttachTargetScreen)
            await quiet(pilot, readers=TARGETS_WORKER)
        finally:
            held.release.set()
        await heard(pilot, dialog)
        assert host.screen is picker, "the spawn's answer closed the picker over its dialog"
        assert host.results == []
        await choose_new_account(pilot, dialog, picker, resume_first=resume_first)
        return [type(host.screen).__name__, notes_of(host.results), len(new_accounts)]

    on_top, results, asked = drive(git_project, scenario)
    assert on_top not in ("SpawnDialog", "AttachTargetScreen")
    assert results == [NOTES]
    assert asked == 0


@EITHER_ORDER
def test_new_account_from_a_picker_the_refusal_landed_under_leaves_the_dialog_showing_it(
    git_project: ProjectInfo,
    spawns: SpawnRecorder,
    no_agents: None,
    new_accounts: list[NewAccountRequested],
    resume_first: bool,
) -> None:
    """The dialog under the picker still said "spawning coder …" when *+ New account* was
    pressed, so the press meets a dialog that waits, whatever the answer turns out to be:
    the refusal is shown, not lost with the dialog to the Accounts page. In either order:
    the answer lands after the pick."""
    held = Held(refuses=True)
    spawns.answer = held

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> list[Any]:
        try:
            await spawn_and_hold(pilot, held)
            dialog.post_message(PickTargetRequested(git_project.id))  # a press on its way
            picker: AttachTargetScreen = await wait_for_screen(pilot, AttachTargetScreen)
            await quiet(pilot, readers=TARGETS_WORKER)
        finally:
            held.release.set()
        await heard(pilot, dialog)
        await choose_new_account(pilot, dialog, picker, resume_first=resume_first)
        on_top = host.screen is dialog
        return [
            on_top,
            note(dialog, "#spawn-status") if on_top else None,
            locked(dialog) if on_top else None,
            notes_of(host.results),
            len(new_accounts),
        ]

    on_top, status, buttons, results, asked = drive(git_project, scenario)
    assert (on_top, results) == (True, [])
    assert status == REFUSAL and buttons == ALL_FREE
    assert asked == 0
