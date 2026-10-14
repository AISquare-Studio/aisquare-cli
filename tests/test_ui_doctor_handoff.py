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
import dataclasses
import os
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.message import Message
from textual.pilot import Pilot
from textual.widgets import Button, Static
from textual.worker import Worker, WorkerState, get_current_worker
from typer.testing import CliRunner

from aisquare.cli import install as install_cli
from aisquare.cli.app import app as cli_app
from aisquare.cli.ui import app as ui_app
from aisquare.cli.ui.remote_control import RemoteController
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
from aisquare.services import remote_server
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
_REAL_START = Worker._start

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

    async def fn(
        pilot: Pilot[None],
    ) -> tuple[list[str], list[Any], list[str], list[str], list[Any]]:
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
        new = [w for w in app.workers if w not in before]
        started = [str(w.name) for w in new if w.state is not WorkerState.PENDING]
        held = [str(w.name) for w in new if w.state is WorkerState.PENDING]
        release.set()
        await settle_until(app, lambda: exits)
        return said, while_held, started, held, exits

    said, while_held, started, held, exits = drive(fn)

    assert said == ["asq quits once the reads running end"], "not refused: waited out"
    assert while_held == [], "no quit under the read"
    assert started == [], "nothing new to wait for"
    assert held and set(held) <= set(ui_app._waited_reads()), "each read asked for, held"
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


@pytest.mark.parametrize("shape", ["async def", "awaitable"])
def test_update_waits_for_a_cancelled_async_thread_worker_whose_thread_still_runs(
    isolated_home: Path,
    script: Any,  # noqa: F811 — pytest resolves fixtures by NAME, so the import must keep it
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    """Round 24 of #257: only a plain callable was counted from its first line to its last.
    An ``async def`` run in a thread (or an awaitable) runs in a loop of its own on that
    thread, which a cancel leaves running just the same, and Update quit under it. The
    control: one not cancelled still answers, and knows its worker, as Textual runs it."""
    monkeypatch.setattr("aisquare.cli.ui.views.doctor._update_refusal", _never_refused)
    started = threading.Event()
    release = threading.Event()

    async def held() -> None:
        started.set()
        release.wait(10)

    async def answer() -> str:
        await asyncio.sleep(0)
        return get_current_worker().name

    async def fn(pilot: Pilot[None]) -> tuple[bool, list[Any], list[Any], Any]:
        app = pilot.app
        assert isinstance(app, ui_app.FleetApp)
        exits: list[Any] = []
        app.exit = lambda *_args, **_kwargs: exits.append(app.hand_off)  # type: ignore[method-assign]
        app.notify = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        work = held if shape == "async def" else held()
        worker = app.run_worker(work, name="async-read", thread=True, exit_on_error=False)
        assert await asyncio.to_thread(started.wait, 10)
        worker.cancel()
        await settle_page(app)
        app.query_one("#doctor-update", Button).press()
        await settle_page(app)
        while_running = list(exits)
        release.set()
        await settle_until(app, lambda: exits)
        asked = answer if shape == "async def" else answer()
        answered = app.run_worker(asked, name="async-answer", thread=True, exit_on_error=False)
        await answered.wait()
        return worker.is_finished, while_running, exits, answered.result

    finished, while_running, exits, answered = drive(fn)

    assert finished, "control: the worker reads finished"
    assert while_running == [], "no quit under its thread"
    assert exits == [("upgrade", "--reopen")], "the quit, once its thread has ended"
    assert answered == "async-answer", "run as Textual runs it: its result, its worker"


def test_the_worker_manager_hands_textual_whatever_textual_gave_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 25 of #257: ``_new_worker`` is Textual's private method, overridden with the
    keywords 8.2 passes, while any 8.x is accepted. One a later release adds made every
    worker start raise TypeError. What comes in goes on, and a thread worker is still
    counted under the name Textual would give it."""
    from textual.worker_manager import WorkerManager

    handed: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []

    @dataclass(eq=False)
    class Started:
        name: str | None
        is_finished: bool = False

    def textuals(_self: Any, work: Any, _node: Any, *args: Any, **kwargs: Any) -> Any:
        handed.append((work, args, kwargs))
        return Started(kwargs.get("name"))

    monkeypatch.setattr(WorkerManager, "_new_worker", textuals)
    manager = ui_app._WorkerThreads(SimpleNamespace())
    seen: list[set[str]] = []

    def read() -> str:
        seen.append(manager.still_running())
        return "read"

    manager._new_worker(read, None, thread=True, exclusive=True, later_keyword="kept")
    work, args, kwargs = handed[0]
    assert args == () and kwargs["later_keyword"] == "kept" and kwargs["exclusive"] is True
    assert kwargs["name"] == "read" and kwargs["description"] == repr(read)
    assert work() == "read" and seen == [{"read"}], "counted from its first line to its last"
    manager._new_worker(read, None, "positional", group="g")
    assert handed[1] == (read, ("positional",), {"group": "g"}), "not a thread: as it came"


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
    press changes nothing.

    The wait is timed by the test's own clock (``_hand_off_clock``), moved by exactly the
    patience: on the wall clock, against a 0.3 s patience, a slow runner's toast said
    "after 1 s" where this one said "after 0 s" (CI on 26fb6543)."""
    monkeypatch.setattr("aisquare.cli.ui.views.doctor._update_refusal", _never_refused)
    now = [1000.0]
    monkeypatch.setattr(ui_app, "_hand_off_clock", lambda: now[0])
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
        assert len(said) == 1, "no time has passed: nothing is named yet"
        now[0] += ui_app._HAND_OFF_PATIENCE  # the wait has lasted its patience, to the second
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
        "Still waiting for a read after 10 s. Press Update or Uninstall again to stay "
        "in asq; quit, and it runs once they end.",
    ], said
    assert said[2:] == ["Called off: asq carries on"], said
    assert exits == [], "no quit once the read ended"
    assert restarted, "asq's reads start again"


def test_a_poll_queued_before_the_call_off_neither_waits_on_nor_quits(
    isolated_home: Path,
    script: Any,  # noqa: F811 — pytest resolves fixtures by NAME, so the import must keep it
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 24 of #257: the hand-over's timer only queues its poll (``set_timer`` goes by
    ``call_next``), so stopping the timer cannot take back a poll already queued when the
    second press is handled. That poll ran with nothing handed over: it said "Still
    waiting" after "Called off", polled on, and once the read ended quit asq, which then
    ran no command. The order here is the one a tick and a press queued together take."""
    monkeypatch.setattr("aisquare.cli.ui.views.doctor._update_refusal", _never_refused)
    monkeypatch.setattr(ui_app, "_HAND_OFF_PATIENCE", 0.3)
    release = threading.Event()

    async def fn(pilot: Pilot[None]) -> tuple[list[str], list[Any]]:
        app = pilot.app
        assert isinstance(app, ui_app.FleetApp)
        said: list[str] = []
        exits: list[Any] = []
        app.notify = lambda message, **_: said.append(str(message))  # type: ignore[method-assign]
        app.exit = lambda *_args, **_kwargs: exits.append(app.hand_off)  # type: ignore[method-assign]
        app.run_worker(release.wait, name="hung-read", thread=True, exit_on_error=False)
        app.query_one("#doctor-update", Button).press()
        await settle_until(app, lambda: len(said) == 2, group="held")
        app.call_next(app._exit_when_read)  # the timer fired: its poll is queued
        app._call_off_hand_off()  # the second press, handled before it
        await settle_page(app, group="held")
        release.set()
        await settle_page(app)
        return said, exits

    said, exits = drive(fn)

    assert said[1].startswith("Still waiting for a read"), "control: the wait was overdue"
    assert said[2:] == ["Called off: asq carries on"], said
    assert exits == [], "no quit once the read ended"


Then = Callable[[ui_app.FleetApp, list[str]], Awaitable[list[str]]]
"""A step after the starts, given the app and every start so far; it says what to return."""


def _starts_with_a_hand_over(
    pending: bool, monkeypatch: pytest.MonkeyPatch, *, then: Then | None = None
) -> list[str]:
    """The workers asq's own starts begin: a project's page opened (its Explainability and
    Settings tabs read on mount, and the shell's doctor runs for it), its Doctor tab, the
    Accounts page's minute tick with a session and a signed-in slot, and Welcome's look and
    list. With a hand-over ``pending``, held by a worker it waits for; ``then``, what to do
    once the starts have been made, and so what to return.

    asq opens on Welcome each time: a page an earlier run left selected would open with the
    app, mounted before the hand-over, and none of its tabs would have a read to start."""
    ui_app._remember_ui_state(ui_app.SELECTED_KEY, None)
    monkeypatch.setattr(accounts_view, "_read_credits", lambda _session: [])
    monkeypatch.setattr(accounts_service, "read_usage_with_trends", lambda _accounts: {})
    monkeypatch.setattr(explainability_view, "status_report", lambda _project: None)
    # Every start, read as it begins: a quick worker has left ``app.workers`` by the time
    # the steps end, and a read held for the hand-over begins only if it is called off.
    names: list[str] = []

    def start(worker: Worker[Any], *args: Any, **kwargs: Any) -> None:
        names.append(str(worker.name))
        _REAL_START(worker, *args, **kwargs)

    monkeypatch.setattr(Worker, "_start", start)
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
        if then is not None:
            started = await then(app, names)
        release.set()
        return started

    return drive(fn)


def test_a_called_off_hand_over_begins_each_start_that_stood_down_once(
    isolated_home: Path,
    tmp_path: Path,
    script: Any,  # noqa: F811 — pytest resolves fixtures by NAME, so the import must keep it
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 24 of #257: until a hand-over could be called off it always ended in a quit,
    so a start that stood down lost nothing. Called off, asq carried on with what the wait
    had skipped: the Doctor section named the new project over the old one's report, the
    Settings tab offered no slot, Welcome never judged its path box, the Project Doctor tab
    stayed empty. Each read held meanwhile begins once on the call-off (the shell's
    accounts read too, however many ticks asked for it), and a full look stays full; one
    held for a view no longer in the app does not. The control is the same steps with no
    hand-over."""
    monkeypatch.setattr("aisquare.cli.ui.views.doctor._update_refusal", _never_refused)
    seed(tmp_path, ("prj_a", "alpha", None))
    looks: list[bool] = []
    claude = welcome_view.DEFAULT_SEAMS.claude

    def asked(full: bool, root: Path | None) -> Any:
        looks.append(full)
        return claude(full, root)

    # Welcome's look takes its seams as it is made, and a held one was made before the call-off.
    seams = dataclasses.replace(welcome_view.DEFAULT_SEAMS, claude=asked)
    monkeypatch.setattr(welcome_view, "DEFAULT_SEAMS", seams)

    async def call_off(app: ui_app.FleetApp, names: list[str]) -> list[str]:
        welcome = app.query_one(welcome_view.WelcomeView)
        welcome.look(full=False)  # Welcome's tick, after the full look asked for
        welcome._judge()  # a keystroke in the path box
        for _ in range(2):  # the shell's tick, and the accounts read it starts
            app.refresh_data()
            app.refresh_accounts()
        gone = Static()  # a view that asked for a read, then left the app
        await app.screen.mount(gone)
        gone.run_worker(lambda: None, name=ui_app._DOCTOR_WORKER, thread=True, exit_on_error=False)
        await gone.remove()
        app.query_one(accounts_view.AccountsView)._on_screen = True  # shown again by then
        looks.clear()  # the looks before the hand-over
        mark = len(names)
        app._call_off_hand_off()
        await settle_page(app, group="held")
        return sorted(names[mark:])

    started = _starts_with_a_hand_over(False, monkeypatch)
    owed = _starts_with_a_hand_over(True, monkeypatch, then=call_off)

    assert owed == sorted(
        [*started, ui_app.ACCOUNTS_WORKER, welcome_view.worker_name("validate")]
    ), "each start that stood down, once, and nothing else"
    assert looks == [True], "the look held is the full one asked for"


def test_a_held_read_that_cannot_start_is_said_and_the_rest_still_begin() -> None:
    """Round 25 of #257: the starts owed to a call-off ran in a bare loop inside the key's
    handler, so one that raised took asq down on the press that should keep it open, and
    every start after it was lost. The held reads start each on its own."""
    began: list[str] = []
    said: list[tuple[str, str | None]] = []

    class Node:
        is_attached = True

    class Held:
        is_cancelled = False

        def __init__(self, name: str) -> None:
            self.name = self.group = name
            self.node = node

        def _start(self, _app: Any, _done: Any) -> None:
            if self.name == ui_app._DOCTOR_WORKER:
                raise LookupError("No nodes match '#doctor-report'")
            began.append(self.name)

    node = Node()
    threads = ui_app._WorkerThreads(SimpleNamespace())
    for name in (ui_app.ACCOUNTS_WORKER, ui_app._DOCTOR_WORKER, accounts_view.CREDITS_WORKER):
        threads._held[(node, name)] = (Held(name), False)  # type: ignore[index, assignment]
    app = SimpleNamespace(
        _hand_off_poll=None,
        hand_off=("upgrade", "--reopen"),
        _hand_off_overdue=True,
        _worker_threads=threads,
        notify=lambda text, *, severity="information", **_: said.append((text, severity)),
    )

    ui_app.FleetApp._call_off_hand_off(app)  # type: ignore[arg-type]

    assert began == [ui_app.ACCOUNTS_WORKER, accounts_view.CREDITS_WORKER], "the rest begin"
    assert said[0] == ("Called off: asq carries on", "information")
    assert said[1:] == [
        ("could not start the doctor's checks again: No nodes match '#doctor-report'", "warning")
    ]
    assert app.hand_off is None and threads._held == {}


def test_a_read_asked_for_while_a_hand_over_waits_is_held_and_a_write_is_not() -> None:
    """Round 26 of #257: each start site asked whether a hand-over was pending, and rounds
    19 to 25 each found one that did not. The worker manager holds every read instead: made
    but not begun, the newest start of a node's read in place of one held before it, begun
    on a call-off and forgotten on the quit. A writer begins as ever, and is waited for, so
    nothing anyone asked to write is dropped."""
    ran: list[str] = []

    class Holding(App[None]):
        hand_off: tuple[str, ...] | None = None

        def __init__(self) -> None:
            super().__init__()
            self._workers = self._worker_threads = ui_app._WorkerThreads(self)

    def start(app: Holding, name: str, said: str, *, exclusive: bool = False) -> Any:
        return app.run_worker(
            lambda: ran.append(said),
            name=name,
            group=name,
            exclusive=exclusive,
            thread=True,
            exit_on_error=False,
        )

    async def run() -> None:
        app = Holding()
        async with app.run_test() as pilot:
            app.hand_off = ("upgrade", "--reopen")
            first = start(app, ui_app._DOCTOR_WORKER, "the first doctor", exclusive=True)
            newest = start(app, ui_app._DOCTOR_WORKER, "the newest doctor", exclusive=True)
            write = start(app, doctor_view.FIX_WORKER, "a fix")
            await app.workers.wait_for_complete([write])
            assert (first.state, newest.state) == (WorkerState.PENDING, WorkerState.PENDING)
            assert first not in set(app.workers) and newest in set(app.workers)
            assert ran == ["a fix"], "the writer began; the reads are held"
            assert app._worker_threads.still_running() == set(), "a held read holds no quit"

            app.hand_off = None  # called off
            assert app._worker_threads.release() == []
            await app.workers.wait_for_complete([newest])
            assert ran == ["a fix", "the newest doctor"], "the newest read, once"

            app.hand_off = ("uninstall",)
            dropped = start(app, accounts_view.USAGE_WORKER, "a usage read")
            app._worker_threads.drop_held()  # the quit
            await pilot.pause()
            assert dropped not in set(app.workers) and ran[-1] == "the newest doctor"

    asyncio.run(run())


@pytest.mark.parametrize(
    ("name", "state"),
    [
        (ui_app.ACCOUNTS_WORKER, WorkerState.SUCCESS),
        (ui_app._DOCTOR_WORKER, WorkerState.SUCCESS),
        (ui_app._DOCTOR_WORKER, WorkerState.ERROR),
    ],
    ids=["accounts", "doctor", "doctor-crashed"],
)
def test_an_answer_that_lands_as_asq_closes_paints_nothing(name: str, state: WorkerState) -> None:
    """Rounds 25 and 26 of #257: a thread worker finishing as the app shuts down delivers
    its answer after the frame has gone. On windows-latest the accounts read's query for
    the sidebar raised NoMatches out of the handler, and a doctor run (one a call-off began,
    then a quit) did the same in ``show_doctor`` and ``show_doctor_failure``. An app that
    has stopped running paints none of them, and reads nothing to try."""
    report: tuple[None, list[Any]] | None = (None, []) if name == ui_app._DOCTOR_WORKER else None
    worker = SimpleNamespace(name=name, result=report, error=LookupError("the checks crashed"))

    read: list[str] = []

    class Closing:
        is_running = False
        _doctor_worker = worker  # the newest run: not dismissed as superseded

        def __getattr__(self, attribute: str) -> Any:
            read.append(attribute)  # what painting would ask of the app: its frame, its views
            return lambda *_args, **_kwargs: None

    answered = SimpleNamespace(state=state, worker=worker)

    ui_app.FleetApp.on_worker_state_changed(Closing(), answered)  # type: ignore[arg-type]

    assert read == [], "an app that has stopped running is asked nothing"


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
    def __init__(self, hand_off: tuple[str, ...] | None, remote: Any = None) -> None:
        self.hand_off = hand_off
        self.unsaved = ["the last layout could not be saved"]
        # Off unless given: nothing for run_ui to wait for.
        self.remote = remote if remote is not None else RemoteController()

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


def test_run_ui_hands_over_only_once_remote_and_the_phones_writes_are_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Update or Uninstall with Remote on: the quit leaves Remote stopping on a thread of its
    own (``FleetApp.on_unmount``), and ``exec_self`` replaces the process, so nothing after it
    runs. The hand-over waits for Remote's server and ngrok to stop and for the phones' writes
    first, or an upgrade would replace the install under a server still answering, and the
    ngrok that no exit stopped would hold its domain."""
    order: list[str] = []

    class _Stopping:
        def wait_until_off(self, timeout: float | None = None) -> bool:
            order.append("remote off")
            return True

    monkeypatch.setattr(
        ui_app, "FleetApp", lambda **_options: _FakeApp(("upgrade", "--reopen"), _Stopping())
    )
    monkeypatch.setattr(remote_server, "remote_wait_for_writes", lambda: order.append("writes"))
    monkeypatch.setattr(selfcli, "exec_self", lambda args: order.append(" ".join(args)))

    ui_app.run_ui()

    assert order == ["remote off", "writes", "upgrade --reopen"]


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
