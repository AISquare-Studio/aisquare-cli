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
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.dom import DOMNode
from textual.message import Message
from textual.pilot import Pilot
from textual.widgets import Button, Static
from textual.worker import WorkerState
from typer.testing import CliRunner

from aisquare.cli import install as install_cli
from aisquare.cli.app import app as cli_app
from aisquare.cli.ui import app as ui_app
from aisquare.cli.ui.sidebar import AccountsSelected, AddProject, ProjectSelected
from aisquare.cli.ui.views import accounts as accounts_view
from aisquare.cli.ui.views import doctor as doctor_view
from aisquare.cli.ui.views import explainability as explainability_view
from aisquare.cli.ui.views import onboard as onboard_view
from aisquare.cli.ui.views import project as project_view
from aisquare.cli.ui.views import settings as settings_view
from aisquare.cli.ui.views import welcome as welcome_view
from aisquare.cli.ui.views.doctor import DoctorView
from aisquare.cli.ui.views.project import ProjectView
from aisquare.core import selfcli
from aisquare.core.state import get_state, reset_state
from aisquare.services import claude_accounts as accounts_service
from aisquare.services.install_route import LatestRelease
from tests.installer_seams import no_real_installer  # noqa: F401 — autouse, applied by import
from tests.rendered import plain
from tests.test_lifecycle_upgrade import Machine, Tool, machine, tool  # noqa: F401
from tests.test_ui_shell import drive, script, seed  # noqa: F401 — `script` is a fixture
from tests.test_ui_writing_workers import CUT_OFF, WAITED
from tests.ui_workers import settle_page, settle_until

#: The real function, taken before ``no_real_installer`` closes it for every test here.
_REAL_EXEC_SELF = selfcli.exec_self

#: Textual's own, which a test that records every worker start wraps.
_REAL_RUN_WORKER = DOMNode.run_worker

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


def test_the_install_route_is_read_off_the_ui_thread_and_update_waits_for_it() -> None:
    """Round 23 of #257: what Update rests on (distribution metadata, uv's receipt, a PATH
    lookup) was read on the UI thread as the machine Doctor composed, at every asq start and
    before its first paint, which showed on a network-mounted home. It is read in a thread
    worker, and Update is offered once it is known."""
    release = threading.Event()
    threads: list[int] = []

    def held_refusal() -> str | None:
        threads.append(threading.get_ident())
        release.wait(10)
        return None

    view = DoctorView(machine=True, update_refusal=held_refusal)

    async def drive_view() -> tuple[tuple[bool, str], tuple[bool, str]]:
        host = Host(view)
        async with host.run_test(size=(120, 40)):
            await settle_page(host, group="held")  # the read is held on purpose: settle no group
            update = view.query_one("#doctor-update", Button)
            while_read = (update.disabled, str(update.tooltip))
            release.set()
            await settle_page(host)
            return while_read, (update.disabled, str(update.tooltip))

    while_read, after = asyncio.run(drive_view())

    assert len(threads) == 1 and threads[0] != threading.get_ident(), "off the UI thread"
    assert while_read == (True, "Checking how aisquare was installed…")
    assert after == (False, doctor_view._UPDATE_TOOLTIP), "offered once it is known"


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
        release = threading.Event()
        project.run_worker(
            lambda: release.wait(10), name=doctor_view.FIX_WORKER, thread=True, exit_on_error=False
        )  # its fix, still writing
        app.query_one("#doctor-update", Button).press()
        await settle_page(app, group="held")  # the fix is held on purpose: settle no group
        refused = app.hand_off
        release.set()
        await settle_page(app)
        app.query_one("#doctor-update", Button).press()
        await settle_page(app)
        return doctors, refused, app.hand_off

    doctors, while_busy, after = drive(fn)

    assert doctors == {"doctor": True, "project-doctor": False, "onboard-doctor": False}
    assert while_busy is None, "a project tab's fix is still writing"
    assert after == ("upgrade", "--reopen"), "control: with no fix running it quits"


WRITING = ui_app._writing_workers()
"""Every worker Update and Uninstall refuse under, by name, with the toast's words for it."""


def _held_hand_off(name: str) -> tuple[Any, list[str], Any]:
    """Update pressed while a worker called ``name`` runs, held; then again once it ended."""
    release = threading.Event()

    async def fn(pilot: Pilot[None]) -> tuple[Any, list[str], Any]:
        app = pilot.app
        assert isinstance(app, ui_app.FleetApp)
        said: list[str] = []
        app.notify = lambda message, **_: said.append(str(message))  # type: ignore[method-assign]
        app.run_worker(lambda: release.wait(10), name=name, thread=True, exit_on_error=False)
        app.query_one("#doctor-update", Button).press()
        await settle_page(app, group="held")  # the worker is held on purpose: settle no group
        refused = app.hand_off
        release.set()
        await settle_page(app)
        app.query_one("#doctor-update", Button).press()
        await settle_page(app)
        return refused, said, app.hand_off

    return drive(fn)


@pytest.mark.parametrize("name", sorted(WRITING))
def test_update_waits_for_every_worker_that_writes(
    isolated_home: Path,
    script: Any,  # noqa: F811 — pytest resolves fixtures by NAME, so the import must keep it
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    """Update and Uninstall quit asq and hand the terminal over. Each view kept its own flag
    for them to ask, and the next writer was missed: Welcome's setup, Connect and starts and
    the Onboard view's init (round 15 of #257), then the Project tab's Start manager, an
    agent's Stop and Restart and Explainability's writes (round 16). asq quit under them,
    asyncio.run joined their thread with the terminal blank, and the outcome was never
    shown. One rule on the app's workers, by the views' own names; the toast says which."""
    monkeypatch.setattr("aisquare.cli.ui.views.doctor._update_refusal", _never_refused)

    while_busy, said, after = _held_hand_off(name)

    assert while_busy is None, f"{name} is still running"
    assert said == [f"{WRITING[name]} is still running — try again when it ends"]
    assert after == ("upgrade", "--reopen"), "control: once it has ended, Update quits"


@pytest.mark.parametrize("name", sorted(WAITED))
def test_update_waits_out_every_other_worker_and_starts_no_other_meanwhile(
    isolated_home: Path,
    script: Any,  # noqa: F811 — pytest resolves fixtures by NAME, so the import must keep it
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    """Round 20 of #257: the accounts read runs on every two-second tick, and the doctor
    after every `r` and fix, and each writes as it reads (a reconcile, a usage sample, the
    store's migrations). Refused under them, Update and Uninstall were refused for much
    of the time, and a retry had the same odds; and a tick could start another read
    between the last look and the quit. The hand-over now waits the read out, and asq's
    tick, its accounts read and its doctor start nothing once it has been asked for, nor
    Welcome's folder check, which opens the store too (round 21). Every worker the UI
    starts is waited out but the writers, refused above, and those safe to cut off: a
    list of the reads that write missed three more (round 22)."""
    monkeypatch.setattr("aisquare.cli.ui.views.doctor._update_refusal", _never_refused)
    release = threading.Event()

    async def fn(pilot: Pilot[None]) -> tuple[list[str], list[Any], list[str], list[Any]]:
        app = pilot.app
        assert isinstance(app, ui_app.FleetApp)
        said: list[str] = []
        exits: list[Any] = []
        app.notify = lambda message, **_: said.append(str(message))  # type: ignore[method-assign]
        app.exit = lambda *_args, **_kwargs: exits.append(app.hand_off)  # type: ignore[method-assign]
        app.run_worker(lambda: release.wait(10), name=name, thread=True, exit_on_error=False)
        app.query_one("#doctor-update", Button).press()
        await settle_page(app, group="held")  # the read is held on purpose: settle no group
        while_held = list(exits)
        before = set(app.workers)
        app.refresh_data()  # the tick
        app.refresh_accounts()
        app.run_doctor()
        welcome = app.query_one(welcome_view.WelcomeView)
        welcome._judge()  # a keystroke in the path box
        welcome._tick()  # Welcome's own tick: its look and step 1's list
        welcome.look(full=True)
        welcome.find_candidates()
        started = [str(w.name) for w in app.workers if w not in before]
        release.set()
        await settle_until(app, lambda: exits)
        return said, while_held, started, exits

    said, while_held, started, exits = drive(fn)

    assert said == ["asq quits once the reads running end"], "not refused: waited out"
    assert while_held == [], "no quit under the read"
    assert started == [], "nothing new to wait for"
    assert exits == [("upgrade", "--reopen")], "the quit, once it has ended"


def test_update_waits_for_a_cancelled_doctor_run_whose_thread_still_runs(
    isolated_home: Path,
    script: Any,  # noqa: F811 — pytest resolves fixtures by NAME, so the import must keep it
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 23 of #257: Textual's cancel stops the task that awaits a thread worker, not its
    thread, and the cancelled worker leaves ``app.workers`` while its callable runs on. A
    newer exclusive start cancels the older one: the doctor on a project switch, here, and
    the Accounts page's usage and credits reads. Update quit at once under a doctor run still
    opening the store. The control: by the worker's own state, nothing runs."""
    monkeypatch.setattr("aisquare.cli.ui.views.doctor._update_refusal", _never_refused)
    started = threading.Event()
    release = threading.Event()

    def held_doctor() -> list[Any]:
        started.set()
        release.wait(10)
        return []

    async def fn(pilot: Pilot[None]) -> tuple[Any, list[Any], list[Any]]:
        app = pilot.app
        assert isinstance(app, ui_app.FleetApp)
        exits: list[Any] = []
        app.exit = lambda *_args, **_kwargs: exits.append(app.hand_off)  # type: ignore[method-assign]
        app.notify = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        app._doctor = held_doctor
        app.run_doctor()
        old = app._doctor_worker
        assert old is not None and await asyncio.to_thread(started.wait, 10)
        app._doctor = lambda: []
        app.run_doctor()  # the project switch: an exclusive start cancels the held run
        await settle_page(app)
        cancelled = (old.state, old in list(app.workers))
        app.query_one("#doctor-update", Button).press()
        await settle_page(app)
        while_running = list(exits)
        release.set()
        await settle_until(app, lambda: exits)
        return cancelled, while_running, exits

    cancelled, while_running, exits = drive(fn)

    assert cancelled == (WorkerState.CANCELLED, False), "control: the worker reads finished"
    assert while_running == [], "no quit under its thread"
    assert exits == [("upgrade", "--reopen")], "the quit, once its thread has ended"


@pytest.mark.parametrize("name", sorted(CUT_OFF))
def test_update_does_not_wait_for_a_worker_that_is_safe_to_cut_off(
    isolated_home: Path,
    script: Any,  # noqa: F811 — pytest resolves fixtures by NAME, so the import must keep it
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    """A pending AISquare sign-in waits for the browser until a token arrives, a quit
    cancels the wait (``on_unmount``) and nothing is stored past a cancel. Counted, it held
    Update and Uninstall for as long as the device code lived, 10 to 15 minutes (round 16
    of #257)."""
    monkeypatch.setattr("aisquare.cli.ui.views.doctor._update_refusal", _never_refused)

    while_running, said, _ = _held_hand_off(name)

    assert name == accounts_view.SIGN_IN_WORKER and name not in WRITING
    assert while_running == ("upgrade", "--reopen") and said == [], said


def test_update_leaves_a_task_worker_to_the_quit(
    isolated_home: Path,
    tmp_path: Path,
    script: Any,  # noqa: F811 — pytest resolves fixtures by NAME, so the import must keep it
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Waited for by default are thread workers, which a quit cannot stop. A task worker is
    cancelled at its next await, and one of Textual's own never ends: the Onboard view's
    ``DirectoryTree`` loader, there once ``+`` has been pressed. Waited for, it would hold
    Update for good. The control holds a thread worker under the same name."""
    monkeypatch.setattr("aisquare.cli.ui.views.doctor._update_refusal", _never_refused)
    monkeypatch.setenv("HOME", str(tmp_path))  # what the Onboard view's tree scans
    release = threading.Event()

    async def fn(pilot: Pilot[None]) -> tuple[list[str], list[Any], list[Any]]:
        app = pilot.app
        assert isinstance(app, ui_app.FleetApp)
        exits: list[Any] = []
        app.exit = lambda *_args, **_kwargs: exits.append(app.hand_off)  # type: ignore[method-assign]
        await app.on_add_project(AddProject())
        await settle_page(app)
        loaders = [str(w.name) for w in app.workers if not w.is_finished]
        app.query_one("#doctor-update", Button).press()
        await settle_page(app)
        with_the_loader = list(exits)
        app.hand_off = None  # the control: once more, under a thread worker of that name
        exits.clear()
        app.run_worker(lambda: release.wait(10), name="_loader", thread=True, exit_on_error=False)
        app.query_one("#doctor-update", Button).press()
        await settle_page(app, group="held")
        under_a_thread = list(exits)
        release.set()
        return loaders, with_the_loader, under_a_thread

    loaders, with_the_loader, under_a_thread = drive(fn)

    assert "_loader" in loaders, f"the tree's loader runs: {loaders}"
    assert with_the_loader == [("upgrade", "--reopen")], "quit at once beside the loader"
    assert under_a_thread == [], "control: a thread worker of that name holds the quit"


async def _until(fact: Callable[[], object], timeout: float = 10.0) -> None:
    """Poll for ``fact``, bounded, where a settle would wait on a node that is held."""
    deadline = time.monotonic() + timeout
    while not fact() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)


def test_update_leaves_the_thread_textuals_tree_lists_a_folder_in_to_the_quit(
    isolated_home: Path,
    tmp_path: Path,
    script: Any,  # noqa: F811 — pytest resolves fixtures by NAME, so the import must keep it
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 23 of #257: Textual's own include a thread worker too, ``_load_directory``, in
    which the Onboard view's ``DirectoryTree`` lists each folder. On a folder that hangs (a
    stale mount under the home) it never ends, and waited for, Update hung with it. It
    writes nothing, so it is left to the quit. The control holds asq's own thread worker of
    that name. The listing held is a reload's, after the view has mounted (its first one is
    awaited by the mount), and the page is polled, not settled, while it is held."""
    monkeypatch.setattr("aisquare.cli.ui.views.doctor._update_refusal", _never_refused)
    monkeypatch.setenv("HOME", str(tmp_path))  # what the Onboard view's tree scans
    hold = threading.Event()
    listing = threading.Event()
    release = threading.Event()

    def hung(_tree: Any, paths: Any) -> list[Path]:
        if hold.is_set():
            listing.set()
            release.wait(30)
        return list(paths)

    monkeypatch.setattr(onboard_view.ProjectTree, "filter_paths", hung)

    async def fn(pilot: Pilot[None]) -> tuple[list[str], list[Any], list[Any], list[Any]]:
        app = pilot.app
        assert isinstance(app, ui_app.FleetApp)
        exits: list[Any] = []
        app.exit = lambda *_args, **_kwargs: exits.append(app.hand_off)  # type: ignore[method-assign]
        await app.on_add_project(AddProject())
        await settle_page(app)
        hold.set()
        reloaded = app.query_one(onboard_view.ProjectTree).reload()
        try:
            assert await asyncio.to_thread(listing.wait, 10), "the tree lists the home"
            listings = [str(w.name) for w in app.workers if w._thread_worker and not w.is_finished]
            update = app.query_one("#doctor-update", Button)
            update.press()
            await _until(lambda: exits)
            beside_it = list(exits)
            app.hand_off = None  # the control: once more, under asq's thread worker of that name
            exits.clear()
            mine = threading.Event()
            app.run_worker(
                lambda: mine.wait(10), name="_load_directory", thread=True, exit_on_error=False
            )
            update.press()
            await _until(lambda: app.hand_off is not None)
            under_mine = list(exits)
            mine.set()
            await _until(lambda: exits)
            return listings, beside_it, under_mine, list(exits)
        finally:
            release.set()
            await reloaded

    listings, beside_it, under_mine, after = drive(fn)

    assert listings == ["_load_directory"], "the tree's listing runs, held"
    assert beside_it == [("upgrade", "--reopen")], "quit at once beside it"
    assert under_mine == [], "control: asq's thread worker of that name holds the quit"
    assert after == [("upgrade", "--reopen")], "and quits once it has ended"


def test_a_hand_over_kept_waiting_says_by_what_and_a_second_press_calls_it_off(
    isolated_home: Path,
    script: Any,  # noqa: F811 — pytest resolves fixtures by NAME, so the import must keep it
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 23 of #257: the wait had no end, so a read that never returns (the store on a
    hung mount) left asq with nothing on screen after the first toast and nothing to press.
    Past ``_HAND_OFF_PATIENCE`` it names what still runs, and a second press stays in asq:
    no quit when the read ends, and asq's own reads start again. Before that, a second
    press changes nothing."""
    monkeypatch.setattr("aisquare.cli.ui.views.doctor._update_refusal", _never_refused)
    monkeypatch.setattr(ui_app, "_HAND_OFF_PATIENCE", 0.3)
    release = threading.Event()

    async def fn(pilot: Pilot[None]) -> tuple[list[str], Any, Any, list[Any], bool]:
        app = pilot.app
        assert isinstance(app, ui_app.FleetApp)
        said: list[str] = []
        exits: list[Any] = []
        app.notify = lambda message, **_: said.append(str(message))  # type: ignore[method-assign]
        app.exit = lambda *_args, **_kwargs: exits.append(app.hand_off)  # type: ignore[method-assign]
        app.run_worker(release.wait, name="hung-read", thread=True, exit_on_error=False)
        update = app.query_one("#doctor-update", Button)
        update.press()
        update.press()  # too soon to call it off
        await settle_page(app, group="held")  # the read is held on purpose: settle no group
        early = app.hand_off
        await settle_until(app, lambda: len(said) == 2, group="held")
        overdue = app.hand_off
        update.press()
        await settle_page(app, group="held")
        release.set()
        await settle_page(app)
        before = set(app.workers)
        app.run_doctor()
        restarted = any(w not in before for w in app.workers)
        await settle_page(app)  # its report painted before the app goes
        return said, early, overdue, exits, restarted

    said, early, overdue, exits, restarted = drive(fn)

    assert early == overdue == ("upgrade", "--reopen"), "waiting, the early press ignored"
    assert said[:2] == [
        "asq quits once the reads running end",
        "Still waiting for hung-read after 0 s. Press Update or Uninstall again to stay "
        "in asq; quit, and it runs once they end.",
    ], said
    assert said[2:] == ["Called off: asq carries on"], said
    assert exits == [], "no quit once the read ended"
    assert restarted, "asq's reads start again"


def _starts_with_a_hand_over(pending: bool, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The workers asq's own starts begin: a project's page opened (its Explainability and
    Settings tabs read on mount, and the shell's doctor runs for it), its Doctor tab, the
    Accounts page's minute tick with a session and a signed-in slot, and Welcome's look and
    list. With a hand-over ``pending``, held by a worker it waits for."""
    monkeypatch.setattr(accounts_view, "_read_credits", lambda _session: [])
    monkeypatch.setattr(accounts_service, "read_usage_with_trends", lambda _accounts: {})
    monkeypatch.setattr(explainability_view, "status_report", lambda _project: None)
    # Every start, read where it is made: a quick worker has left ``app.workers`` by the
    # time the steps end. ``@work`` starts through ``run_worker`` too.
    names: list[str] = []

    def run_worker(node: DOMNode, *args: Any, **kwargs: Any) -> Any:
        worker = _REAL_RUN_WORKER(node, *args, **kwargs)
        names.append(str(worker.name))
        return worker

    monkeypatch.setattr(DOMNode, "run_worker", run_worker)
    release = threading.Event()

    async def fn(pilot: Pilot[None]) -> list[str]:
        app = pilot.app
        assert isinstance(app, ui_app.FleetApp)
        app.exit = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        app.notify = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        await app.on_accounts_selected(AccountsSelected())
        await settle_page(app)
        if pending:
            app.run_worker(lambda: release.wait(10), name="held", thread=True, exit_on_error=False)
            app.query_one("#doctor-update", Button).press()
            await settle_page(app, group="held")
            assert app.hand_off == ("upgrade", "--reopen")
        mark = len(names)
        await app.on_project_selected(ProjectSelected("prj_a"))
        project = app.query_one(ProjectView)
        project.run_project_doctor()
        accounts = app.query_one(accounts_view.AccountsView)
        accounts._on_screen = True
        accounts.session = SimpleNamespace(source="file")  # type: ignore[assignment]
        signed_in = SimpleNamespace(signed_in=True, account=SimpleNamespace(slot=2))
        accounts.overview = SimpleNamespace(accounts=[signed_in])  # type: ignore[assignment]
        accounts.refresh_readings()
        welcome = app.query_one(welcome_view.WelcomeView)
        welcome.look(full=True)
        welcome.find_candidates()
        await settle_page(app, group="held")  # the tabs' mounts, which start their reads
        started = sorted(set(names[mark:]))
        release.set()
        return started

    return drive(fn)


def test_no_start_on_asqs_own_schedule_begins_once_a_hand_over_is_pending(
    isolated_home: Path,
    tmp_path: Path,
    script: Any,  # noqa: F811 — pytest resolves fixtures by NAME, so the import must keep it
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 22 of #257: the hand-over waits for whatever is running, so a read that begins
    after its last look is cut off by the quit. Every start on asq's own schedule asks
    ``handoff.handing_over`` and stands down; the control is the same steps with none."""
    monkeypatch.setattr("aisquare.cli.ui.views.doctor._update_refusal", _never_refused)
    seed(tmp_path, ("prj_a", "alpha", None))

    started = _starts_with_a_hand_over(False, monkeypatch)
    pending = _starts_with_a_hand_over(True, monkeypatch)

    assert started == sorted(
        {
            explainability_view.STATUS_WORKER,
            settings_view.ACCOUNTS_WORKER,
            ui_app._DOCTOR_WORKER,  # the shell's, for the project now chosen
            project_view.DOCTOR_WORKER,
            accounts_view.USAGE_WORKER,
            accounts_view.CREDITS_WORKER,
            welcome_view.worker_name("look"),
            welcome_view.worker_name("candidates"),
        }
    ), "control: each step starts its read"
    assert pending == [], "none starts while the hand-over waits"


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


@pytest.mark.parametrize(
    ("given", "carried"),
    [
        (["--no-color", "--profile", "work", "config", "list"], ["--no-color", "--profile=work"]),
        (["config", "list", "-v", "-q"], ["--verbose", "--quiet"]),
        (["--profile", "-dash", "config", "list"], ["--profile=-dash"]),
        (["config", "list"], []),
    ],
    ids=["colour-and-profile", "after-the-command", "profile-like-a-flag", "none"],
)
def test_exec_self_hands_over_with_the_global_flags_asq_was_started_with(
    monkeypatch: pytest.MonkeyPatch, given: list[str], carried: list[str]
) -> None:
    """Round 21 of #257: the global flags set only the runtime state, so the command asq
    handed its terminal to lost them. After `aisquare --no-color ui`, Update's plan and
    y/N came out in colour, and the asq `--reopen` opened again was back on the `default`
    profile. Read back by the CLI, they are the state asq was started with."""
    calls: list[list[str]] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "execv", lambda _path, argv: calls.append(list(argv)))
    started = CliRunner().invoke(cli_app, given)
    assert started.exit_code == 0, started.output
    asq = get_state()

    _REAL_EXEC_SELF(["upgrade", "--reopen"])

    assert calls == [[sys.executable, "-P", "-m", "aisquare", *carried, "upgrade", "--reopen"]]
    reset_state()
    reread = CliRunner().invoke(cli_app, [*calls[0][4:-2], "config", "list"])
    assert reread.exit_code == 0, reread.output
    assert get_state() == asq


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
