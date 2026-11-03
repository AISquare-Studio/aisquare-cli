"""#17: every asq UI knows the other live asqs on its home, and one whose release differs from a
live one's starts nothing, offering to quit the other.

The owner runs asq at the Mac and over SSH into the same Mac at once. Nothing listed them,
and an upgraded asq beside an old one ran two codebases against one store and one tmux.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from aisquare.cli import fleet as fleet_cli
from aisquare.cli.app import app as cli_app
from aisquare.cli.ui import app as ui_app
from aisquare.cli.ui import instance_gate
from aisquare.cli.ui.remote_control import RemoteController
from aisquare.cli.ui.views.doctor import DoctorView
from aisquare.core import instances, locking, paths, selfcli
from aisquare.core.instances import Instance, QuitRequest
from aisquare.core.store import SCHEMA_VERSION
from aisquare.core.version import __version__
from aisquare.services import fleet as fleet_service
from tests.asq_elsewhere import asq_running
from tests.fakebin import executable_fake
from tests.ui_workers import settle_page

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")

_ASKS_TMUX = instances.attached_kind
"""The real one, which asks tmux inside a tmux session."""


@pytest.fixture(autouse=True)
def _no_registration_outlives_its_test(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # The beat thread's first look asks tmux or runs `ps` (core.clipboard): not here.
    monkeypatch.setattr(instances, "attached_kind", lambda env=None: "native")
    yield
    registration = instances.this_registration()
    if registration is not None:
        registration.deregister()


def _files() -> list[str]:
    try:
        return sorted(os.listdir(instances.registry_dir()))
    except FileNotFoundError:
        return []


def _answering(answer: bool, asked: list[str]) -> instance_gate.Ask:
    """The y/N question, answered ``answer``, each asking written down in ``asked``."""

    def ask(question: str) -> bool:
        asked.append(question)
        return answer

    return ask


def _registered() -> instances.Registration:
    me = instances.describe_this_process()
    with instances.gate():
        return instances.register(me)


def _record_on_disk(instance_id: str) -> dict[str, Any]:
    data = json.loads((instances.registry_dir() / f"{instance_id}.json").read_text("utf-8"))
    assert isinstance(data, dict)
    return data


# --- the registry ----------------------------------------------------------------------------


def test_an_asq_registers_with_who_it_is_and_its_record_goes_when_it_quits() -> None:
    registration = _registered()
    me = registration.instance

    on_disk = _record_on_disk(me.id)
    fields = {"id", "pid", "host", "version", "schema", "kind", "tty", "started_at", "heartbeat"}
    assert set(on_disk) >= fields
    assert (on_disk["pid"], on_disk["host"]) == (os.getpid(), instances.host_name())
    assert (on_disk["version"], on_disk["schema"]) == (__version__, SCHEMA_VERSION)
    assert on_disk["kind"] == "native" and on_disk["channel"] == instances.QUIT_CHANNEL
    assert instances.this_instance() == me
    assert instances.live_instances() == [me] and instances.other_instances() == []

    registration.deregister()

    assert instances.this_instance() is None and instances.live_instances() == []
    assert _files() == [instances.GATE_LOCK], "the record, its lock: gone"


@POSIX_ONLY
def test_the_registry_is_the_users_alone() -> None:
    registration = _registered()

    folder = instances.registry_dir()
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    for name in (f"{registration.instance.id}.json", f"{registration.instance.id}.lock"):
        assert stat.S_IMODE((folder / name).stat().st_mode) == 0o600, name


def test_a_record_says_ssh_when_the_environment_does(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.2 51234 10.0.0.1 22")

    assert _registered().instance.kind == "ssh"


def test_the_heartbeat_rides_a_thread_of_its_own_and_says_how_tmux_attached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At least every 10 s, and no Textual timer: on Windows each holds an executor thread.
    The first beat asks how tmux's client attached (a Mac asq reattached over SSH is ssh)."""
    monkeypatch.setattr(instances, "attached_kind", lambda env=None: "ssh")
    monkeypatch.setattr(instances, "HEARTBEAT_SECONDS", 0.05)
    assert instances.HEARTBEAT_SECONDS <= 10
    registration = _registered()
    first = registration.instance.heartbeat

    registration.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        on_disk = _record_on_disk(registration.instance.id)
        if on_disk["kind"] == "ssh" and datetime.fromisoformat(on_disk["heartbeat"]) > first:
            break
        time.sleep(0.02)
    else:
        pytest.fail(f"no heartbeat: {on_disk}")
    [thread] = [t for t in threading.enumerate() if t.name == "asq-instance"]
    assert thread.daemon

    registration.deregister()
    time.sleep(0.2)
    assert not thread.is_alive() and _files() == [instances.GATE_LOCK], "no beat after the quit"


def test_a_crashed_asqs_record_is_gone_after_the_next_start(tmp_path: Path) -> None:
    """Same host: its pid no longer runs, or runs another program (a recycled pid holds no
    lock). Another host: its heartbeat is a minute old. Each is left out and deleted."""
    gone = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
    )
    dead_pid = int(gone.stdout)
    old = datetime.now(UTC) - timedelta(seconds=instances.STALE_SECONDS + 1)
    with (
        asq_running(pid=dead_pid, hold_lock=False) as dead,
        asq_running(hold_lock=False) as recycled,
        asq_running(host="mini-elsewhere", heartbeat=old, hold_lock=False) as silent,
        asq_running(host="mini-elsewhere", pid=dead_pid) as beating,
        asq_running() as live,
    ):
        assert instances.live_instances() == sorted(
            [beating, live], key=lambda instance: instance.started_at
        ), "another host's fresh heartbeat is live whatever its pid is here"
        for stale in (dead, recycled, silent):
            assert f"{stale.id}.json" not in _files(), "pruned"


def test_files_no_record_explains_are_swept_once_old() -> None:
    folder = instances.registry_dir()
    folder.mkdir(parents=True)
    unreadable = folder / "garbled.json"
    unreadable.write_text("{not json", encoding="utf-8")
    orphan = folder / "nobody.quit"
    orphan.write_text("{}", encoding="utf-8")

    assert instances.live_instances() == []
    assert unreadable.exists() and orphan.exists(), "fresh: maybe a write under way"

    long_ago = time.time() - instances.STALE_SECONDS - 1
    for path in (unreadable, orphan):
        os.utime(path, (long_ago, long_ago))
    assert instances.live_instances() == []
    assert not unreadable.exists() and not orphan.exists()


# --- the gate ----------------------------------------------------------------------------------


@pytest.fixture
def tmux_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ``tmux`` on PATH that writes down every call, inside a tmux session (``$TMUX``), where
    asking how the client attached would call it."""
    log = tmp_path / "tmux-calls.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable_fake(bin_dir, "tmux", posix=f'echo "$@" >> "{log}"', windows=f'echo %* >> "{log}"')
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,1234,0")
    return log


def test_beside_another_release_with_no_terminal_asq_builds_nothing_and_says_how(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmux_calls: Path
) -> None:
    """The gate runs before FleetApp is built: no tmux call, no store, no Remote, no socket."""
    # The real one: a gate that asked how tmux's client attached would show in the log.
    monkeypatch.setattr(instances, "attached_kind", _ASKS_TMUX)
    built: list[dict[str, Any]] = []
    monkeypatch.setattr(ui_app, "FleetApp", lambda **options: built.append(options))

    with asq_running(version="0.0.1") as other:
        with pytest.raises(typer.Exit) as refused:
            ui_app.run_ui()
        assert instances.live_instances() == [other], "nothing of this asq registered"

    said = capsys.readouterr().err
    assert refused.value.exit_code == 1
    assert built == [], "no FleetApp"
    assert not tmux_calls.exists(), tmux_calls.read_text() if tmux_calls.exists() else ""
    assert not (paths.aisquare_home() / "context.db").exists(), "no store"
    assert f"asq {__version__} (store schema {SCHEMA_VERSION}) cannot run beside" in said
    where = f"ssh on {other.host}, pid {other.pid}, /dev/ttys009, since"
    assert f"asq 0.0.1 (store schema {SCHEMA_VERSION}) — {where}" in said
    assert "No terminal to ask in" in said and "press q in that asq" in said


def test_a_store_schema_that_differs_is_refused_as_a_release_is(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with asq_running(schema=SCHEMA_VERSION - 1), pytest.raises(typer.Exit):
        instance_gate.admit(interactive=False)

    assert f"(store schema {SCHEMA_VERSION - 1})" in capsys.readouterr().err


def test_beside_its_own_release_asq_starts_at_once_and_asks_nothing() -> None:
    asked: list[str] = []
    with asq_running() as other:
        began = time.monotonic()
        registration = instance_gate.admit(ask=_answering(True, asked))
        took = time.monotonic() - began

        assert registration is not None and asked == []
        assert took < 0.5, f"the gate took {took:.3f}s"
        assert instances.other_instances() == [other], "both live, each sees the other"


def test_no_leaves_the_other_running_and_says_how_to_quit_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    asked: list[str] = []
    with asq_running(version="0.0.1") as other:
        with pytest.raises(typer.Exit) as refused:
            instance_gate.admit(ask=_answering(False, asked), interactive=True)
        assert instances.live_instances() == [other] and f"{other.id}.quit" not in _files()

    said = " ".join(capsys.readouterr().err.split())
    assert refused.value.exit_code == 1 and asked == ["Quit the other asq?"]
    assert (
        f"Quit it first — press q in that asq (ssh on {other.host}, pid {other.pid}, "
        "/dev/ttys009) — then start asq again." in said
    )


@pytest.mark.parametrize("answer", [EOFError, KeyboardInterrupt], ids=["end-of-input", "ctrl-c"])
def test_ctrl_c_or_the_end_of_input_at_the_question_is_a_no_that_says_how_to_quit_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], answer: type[BaseException]
) -> None:
    """Tester's reopen of #17: typer 0.27's ``confirm`` raises typer's own ``Abort``, no
    subclass of click's that ``_confirm`` caught, so ^C or ^D escaped as a bare "Aborted."
    with no word of how to quit the other asq. The real question, its input cut off."""

    def cut_off(_prompt: str = "") -> str:
        raise answer

    monkeypatch.setattr("typer._click.termui.visible_prompt_func", cut_off)
    with asq_running(version="0.0.1") as other:
        with pytest.raises(typer.Exit) as refused:
            instance_gate.admit(interactive=True)
        assert instances.live_instances() == [other] and f"{other.id}.quit" not in _files()

    out, err = capsys.readouterr()
    said = " ".join(err.split())
    assert refused.value.exit_code == 1
    # The prompt's last character is the cut-off input function's to echo.
    assert out == "Quit the other asq? [y/N]:\n", "the prompt's line ends before the refusal"
    assert f"press q in that asq (ssh on {other.host}, pid {other.pid}, /dev/ttys009)" in said
    assert "Aborted" not in out + err


def test_an_asq_that_cannot_be_asked_is_named_with_what_kill_costs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    asked: list[str] = []
    with asq_running(version="0.0.1", channel=0) as other:
        with pytest.raises(typer.Exit) as refused:
            instance_gate.admit(ask=_answering(True, asked), interactive=True)
        assert f"{other.id}.quit" not in _files(), "nothing asked of it"

    said = " ".join(capsys.readouterr().err.split())
    assert refused.value.exit_code == 1 and asked == [], "no question it could not act on"
    assert f"asq 0.0.1 (pid {other.pid}) predates being asked to quit" in said
    assert f"`kill {other.pid}`: a SIGTERM skips its clean quit" in said
    assert "what it has not saved yet" in said and 'not told "Remote is off"' in said


def test_an_asq_that_does_not_quit_in_time_is_said_and_nothing_starts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with asq_running(version="0.0.1") as other:
        with pytest.raises(typer.Exit) as refused:
            instance_gate.admit(ask=lambda q: True, interactive=True, wait_s=0.3)
        asked = json.loads((instances.registry_dir() / f"{other.id}.quit").read_text("utf-8"))

    said = " ".join(capsys.readouterr().err.split())
    assert refused.value.exit_code == 1 and instances.this_instance() is None
    me = {"version": __version__, "kind": "native", "host": other.host, "pid": os.getpid()}
    assert asked == me
    assert f"asq 0.0.1 (pid {other.pid}) has not quit within 0.3 s" in said


_STAND_IN = r"""
import sys, threading
from dataclasses import replace
from aisquare.core import instances
instances.attached_kind = lambda env=None: "ssh"
me = replace(instances.describe_this_process(kind="ssh"), version=sys.argv[1])
with instances.gate():
    registration = instances.register(me)
asked = threading.Event()
def deliver(request):
    print("asked by", request.who, flush=True)
    asked.set()
    return True
registration.deliver_quits_to(deliver)
registration.start()
print("ready", flush=True)
asked.wait(30)
registration.deregister()
"""


def test_yes_asks_the_other_asq_to_quit_and_starts_once_it_has(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The other asq is a process of its own: the request crosses on its quit-request channel,
    and this one starts once its record has gone."""
    stand_in = subprocess.Popen(
        [sys.executable, "-c", _STAND_IN, "0.0.1"], stdout=subprocess.PIPE, text=True
    )
    # Reaped as it ends, as a real asq's shell reaps it: the gate waits for its process to
    # end (#21), and an unreaped child of this test lives on as a zombie.
    reaped = threading.Thread(target=stand_in.wait, daemon=True)
    reaped.start()
    try:
        assert stand_in.stdout is not None
        assert stand_in.stdout.readline().strip() == "ready"
        [other] = instances.live_instances()
        assert (other.version, other.pid) == ("0.0.1", stand_in.pid)

        registration = instance_gate.admit(
            ask=lambda q: q == "Quit the other asq?", interactive=True
        )

        reaped.join(20)
        out = stand_in.stdout.read()
    finally:
        stand_in.kill()
    said = capsys.readouterr().err
    assert registration is not None and instances.live_instances() == [registration.instance]
    me = registration.instance
    assert out.strip() == f"asked by asq {__version__} (native on {me.host}, pid {os.getpid()})"
    assert f"asking asq 0.0.1 (pid {other.pid}) to quit" in said
    assert f"✓ asq 0.0.1 (pid {other.pid}) quit" in said


def test_ui_project_reads_no_store_before_the_gate_lets_asq_start(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """``--project`` and ``--group`` are resolved from the store, whose open migrates it."""
    monkeypatch.setattr(fleet_cli, "interactive_terminal", lambda: True)
    read: list[object] = []
    monkeypatch.setattr(fleet_service, "ui_scope", lambda *a: read.append(a))
    monkeypatch.setattr(ui_app, "run_ui", lambda **options: read.append("run_ui"))

    with asq_running(version="0.0.1"):
        result = runner.invoke(cli_app, ["ui", "--project", "alpha"])

    assert result.exit_code == 1, result.output
    assert read == [] and "cannot run beside" in result.stderr


# --- the app's end -----------------------------------------------------------------------------


class _QuitApp:
    """``run_ui``'s view of an app another asq asked to quit."""

    def __init__(self, hand_off: tuple[str, ...] | None = None) -> None:
        self.hand_off = hand_off
        self.unsaved = ["the last layout could not be saved"]
        self.quit_asked_by = QuitRequest(version="0.0.1", kind="ssh", host="mini", pid=4242)
        self.remote = RemoteController()

    def run(self) -> None:
        assert instances.this_instance() is not None, "registered while it runs"


def test_run_ui_says_who_asked_in_its_own_terminal_and_leaves_before_a_hand_over(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    at_exec: list[object] = []
    monkeypatch.setattr(ui_app, "FleetApp", lambda **_: _QuitApp(("upgrade", "--reopen")))
    monkeypatch.setattr(
        selfcli, "exec_self", lambda args: at_exec.extend([instances.this_instance(), _files()])
    )

    ui_app.run_ui()

    said = capsys.readouterr().err
    assert "asq quit: asq 0.0.1 (ssh on mini, pid 4242) asked it to" in said
    assert "the last layout could not be saved" in said, "its unsaved lines, here"
    assert at_exec == [None, [instances.GATE_LOCK]], "deregistered before the exec"


def test_an_asq_asked_to_quit_quits_as_q_does(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(instances, "POLL_SECONDS", 0.02)
    registration = _registered()
    registration.start()
    asker = replace(registration.instance, id="asker", version="0.0.1", kind="ssh", pid=4242)

    async def run() -> tuple[Any, list[str]]:
        app = ui_app.FleetApp(refresh_seconds=3600, doctor=lambda: [])
        async with app.run_test(size=(120, 40), notifications=True):
            await settle_page(app)
            instances.request_quit(registration.instance, by=asker)
            for _ in range(500):
                if app._exit:
                    break
                await asyncio.sleep(0.01)
            assert app._exit, "the request never quit it"
            return app.quit_asked_by, [note.message for note in app._notifications]

    asked_by, toasts = asyncio.run(run())

    who = f"asq 0.0.1 (ssh on {asker.host}, pid 4242)"
    assert asked_by == QuitRequest(version="0.0.1", kind="ssh", host=asker.host, pid=4242)
    assert f"{who} asked this asq to quit, to start in its place" in toasts


@pytest.mark.parametrize(("command", "button"), [("upgrade", "Update"), ("uninstall", "Uninstall")])
def test_doctors_update_and_uninstall_are_refused_while_another_asq_runs(
    command: str, button: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(instances, "HEARTBEAT_SECONDS", 0.05)
    registration = _registered()
    registration.start()

    async def run() -> None:
        app = ui_app.FleetApp(refresh_seconds=3600, doctor=lambda: [])
        async with app.run_test(size=(120, 40), notifications=True) as pilot:
            await settle_page(app)
            with asq_running(version="0.0.1") as other:
                await _beaten(registration, [other])
                app.on_doctor_view_hand_off(DoctorView.HandOff((command, "--reopen")))
                await pilot.pause()
                said = " ".join(note.message for note in app._notifications)
                assert app.hand_off is None and not app._exit, "it stays"
                assert f"another asq runs on this home ({other.label})" in said
                assert f"Quit it first (q in it), then press {button} again." in said
            await _beaten(registration, [])
            app.on_doctor_view_hand_off(DoctorView.HandOff((command, "--reopen")))
            assert app.hand_off == (command, "--reopen"), "control: alone, it hands over"

    asyncio.run(run())


def test_restart_and_updates_refusal_names_its_own_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REVIEW-RC092-MI, nit 4: the ⬆ dialog's *Restart & update* hands over as Doctor's
    Update does, and its refusal said "press Update again", a button that dialog has not."""
    monkeypatch.setattr(instances, "HEARTBEAT_SECONDS", 0.05)
    registration = _registered()
    registration.start()

    async def run() -> None:
        app = ui_app.FleetApp(refresh_seconds=3600, doctor=lambda: [])
        async with app.run_test(size=(120, 40), notifications=True) as pilot:
            await settle_page(app)
            with asq_running(version="0.0.1") as other:
                await _beaten(registration, [other])
                app.on_doctor_view_hand_off(DoctorView.HandOff(ui_app.UPDATE_HAND_OFF))
                await pilot.pause()
                said = " ".join(note.message for note in app._notifications)
                assert app.hand_off is None and not app._exit, "it stays"
                assert "Quit it first (q in it), then press Restart & update again." in said
                assert "press Update again" not in said

    asyncio.run(run())


def test_the_refusal_of_several_asqs_says_they() -> None:
    """REVIEW-RC092-MI, nit 1: "and them would go on running", "the program them run"."""
    from aisquare.services import lifecycle

    two = ("asq 0.0.1 (mac, pid 1)", "asq 0.0.1 (ssh on mini, pid 2)")
    upgrade = lifecycle.asqs_refusal("upgrade", two)
    uninstall = lifecycle.asqs_refusal("uninstall", two)
    assert upgrade is not None and uninstall is not None
    assert "replaced under them, and they would go on running the old release" in upgrade
    assert "it would remove the program they run, with nothing left to quit them cleanly" in (
        uninstall
    )
    assert "Quit them first (q in each)" in upgrade
    one = lifecycle.asqs_refusal("uninstall", two[:1])
    assert one is not None and "the program it runs, with nothing left to quit it cleanly" in one


async def _beaten(registration: instances.Registration, others: list[Instance]) -> None:
    """Wait for the beat thread to have read ``others`` as the other live asqs."""
    for _ in range(500):
        if registration.others == others:
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"the beat thread read {registration.others}, not {others}")


def test_the_lifecycle_names_every_other_live_asq_but_this_one() -> None:
    _registered()
    with asq_running(version="0.0.1") as other:
        assert instances.other_instances() == [other]
        from aisquare.services import lifecycle

        assert lifecycle.running_asqs() == (f"asq 0.0.1 (ssh on {other.host}, pid {other.pid})",)


def test_a_record_is_read_whatever_a_later_release_adds() -> None:
    me = instances.describe_this_process()
    data = {**me.to_record(), "panes": {"%1": [80, 24]}}

    assert Instance.from_record(data) == me
    assert Instance.from_record({"id": "x"}) is None
    assert Instance.from_record([1, 2]) is None


# --- review of #462: one test per finding, each failing before its fix ----------------------------


def test_1_windows_is_never_asked_for_a_tty_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """``os.ttyname`` does not exist on Windows, and ``mypy --platform win32`` (CI's lint)
    failed on the call. Off Windows only by the platform's test, which mypy narrows on."""

    def ttyname(fd: int) -> str:
        raise AssertionError("os.ttyname asked on Windows")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "ttyname", ttyname, raising=False)

    assert instances._tty() is None


_DEAD_PID = 2**22 + 12345
"""Past every platform's highest pid (Linux's ``pid_max`` tops out at 2**22): never alive."""


def test_2_a_records_paths_come_from_its_file_name_never_its_content() -> None:
    """A record whose id said ``../config`` made the pruner delete ``<home>/config.json``,
    and was itself never pruned."""
    home = paths.aisquare_home()
    folder = instances.registry_dir()
    folder.mkdir(parents=True)
    config = home / "config.json"
    config.write_text("{}", encoding="utf-8")
    dead = replace(instances.describe_this_process(), id="../config", pid=_DEAD_PID)
    planted = folder / "abcdef012345.json"
    planted.write_text(json.dumps(dead.to_record()), encoding="utf-8")
    long_ago = time.time() - instances.STALE_SECONDS - 1
    os.utime(planted, (long_ago, long_ago))
    copied = folder / "abcdef012345 (1).json"
    copied.write_text(json.dumps(dead.to_record()), encoding="utf-8")
    os.utime(copied, (long_ago, long_ago))

    assert instances.live_instances() == []

    assert config.exists(), "nothing outside the folder is touched"
    assert not planted.exists() and not copied.exists(), "both pruned, by their own names"
    with pytest.raises(ValueError):
        instances.request_quit(dead, by=instances.describe_this_process())


def test_3_the_lifecycle_plans_read_the_registry_and_write_nothing() -> None:
    """``uninstall``'s and ``upgrade``'s plans, a dry run's too, write nothing to the home;
    reading the registry pruned it."""
    from aisquare.services import lifecycle

    with asq_running(pid=_DEAD_PID, hold_lock=False) as dead:
        before = _files()
        assert lifecycle.running_asqs() == ()
        assert _files() == before and f"{dead.id}.json" in before, "the stale record stays"


def test_4a_a_fresh_record_this_release_cannot_read_is_an_asq_of_an_unknown_release(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A later release's record is the likeliest one this release cannot read: taken as no
    record, the gate let this asq start beside a live 0.9.9."""
    asked: list[str] = []
    with asq_running(version="0.9.9") as later:
        path = instances.registry_dir() / f"{later.id}.json"
        record = {**later.to_record(), "started_at": 1793377097}  # a shape this one cannot read
        path.write_text(json.dumps(record), encoding="utf-8")

        with pytest.raises(typer.Exit) as refused:
            instance_gate.admit(ask=_answering(True, asked), interactive=True)

        said = " ".join(capsys.readouterr().err.split())
        assert refused.value.exit_code == 1 and asked == [] and instances.this_instance() is None
        assert f"asq of a release this one cannot read — its record {path}" in said
    # Control: a minute old with no lock held (its asq gone), it is a leftover.
    with asq_running(version="0.9.9", hold_lock=False) as left:
        path = instances.registry_dir() / f"{left.id}.json"
        path.write_text(json.dumps({**left.to_record(), "started_at": 1}), encoding="utf-8")
        long_ago = time.time() - instances.STALE_SECONDS - 1
        os.utime(path, (long_ago, long_ago))
        assert instance_gate.admit() is not None, "control: a minute old, it is a leftover"


def test_4b_a_gate_held_past_its_wait_refuses_rather_than_start_unchecked_and_unseen(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(instances, "GATE_WAIT_SECONDS", 0.2)
    with asq_running(version="0.0.1"):
        folder = instances.registry_dir()
        fd = locking.open_lock_file(folder / instances.GATE_LOCK, mode=0o600)
        try:
            locking.lock_exclusive(fd)  # another asq, in its gate
            with pytest.raises(typer.Exit) as refused:
                instance_gate.admit(interactive=False)
        finally:
            os.close(fd)

    said = " ".join(capsys.readouterr().err.split())
    assert refused.value.exit_code == 1 and instances.this_instance() is None
    assert "Another asq is checking this home and has held its gate over 0.2 s" in said
    assert "none will see it" not in said


def test_5a_a_record_whose_lock_is_held_is_live_whatever_its_host_and_never_pruned() -> None:
    """A clock on an NFS home a minute off, or two machines that share a host name: the
    heartbeat or the pid said stale, and the pruner deleted a live asq's record AND its lock."""
    old = datetime.now(UTC) - timedelta(seconds=instances.STALE_SECONDS + 30)
    with (
        asq_running(host="mini-elsewhere", heartbeat=old) as skewed,
        asq_running(pid=_DEAD_PID) as same_name_elsewhere,
    ):
        live = instances.live_instances()

        assert {i.id for i in live} == {skewed.id, same_name_elsewhere.id}
        for kept in (skewed, same_name_elsewhere):
            assert {f"{kept.id}.json", f"{kept.id}.lock"} <= set(_files()), "not pruned"


def test_5b_a_record_is_stamped_at_admission_not_when_asq_was_described() -> None:
    """``me`` is described before the question, which a person may take minutes over."""
    an_hour_ago = datetime.now(UTC) - timedelta(hours=1)
    me = replace(instances.describe_this_process(), started_at=an_hour_ago, heartbeat=an_hour_ago)
    with instances.gate():
        registration = instances.register(me)

    on_disk = _record_on_disk(me.id)
    for moment in (registration.instance.started_at, registration.instance.heartbeat):
        assert datetime.now(UTC) - moment < timedelta(seconds=30)
    assert on_disk["started_at"] == registration.instance.started_at.isoformat()
    assert on_disk["heartbeat"] == registration.instance.heartbeat.isoformat()


def test_6_ui_project_is_registered_before_its_store_is_read(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """Checked but not registered, the asq read (and maybe migrated) the store unseen, and a
    differing release could start in that window."""
    monkeypatch.setattr(fleet_cli, "interactive_terminal", lambda: True)
    seen: list[object] = []

    def scope(*refs: object) -> None:
        seen.append(instances.this_instance())
        return None

    monkeypatch.setattr(fleet_service, "ui_scope", scope)
    monkeypatch.setattr(ui_app, "run_ui", lambda **options: seen.append("run_ui"))

    result = runner.invoke(cli_app, ["ui", "--project", "alpha"])

    assert result.exit_code == 0, result.output
    assert isinstance(seen[0], Instance) and seen[1:] == ["run_ui"], seen
    assert instances.this_instance() is None and _files() == [instances.GATE_LOCK], "ended"

    def unknown(*refs: object) -> None:
        raise fleet_service.NoSuchProject("no project matches 'alpha'")

    monkeypatch.setattr(fleet_service, "ui_scope", unknown)
    failed = runner.invoke(cli_app, ["ui", "--project", "alpha"])
    assert failed.exit_code == 1 and instances.this_instance() is None
    assert _files() == [instances.GATE_LOCK], "a scope that fails ends the registration"


def test_7_the_doctors_hand_off_reads_no_folder_on_the_ui_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A home on a hung NFS mount would hold the UI thread: the beat thread reads the others."""
    monkeypatch.setattr(instances, "HEARTBEAT_SECONDS", 0.05)
    registration = _registered()
    registration.start()
    on_the_ui_thread: list[str] = []
    listdir = os.listdir

    def watched(path: Any = ".") -> list[str]:
        on_ui_thread = threading.current_thread() is threading.main_thread()
        if on_ui_thread and Path(path) == instances.registry_dir():
            on_the_ui_thread.append(str(path))
        return listdir(path)

    async def run() -> None:
        app = ui_app.FleetApp(refresh_seconds=3600, doctor=lambda: [])
        async with app.run_test(size=(120, 40), notifications=True) as pilot:
            await settle_page(app)
            with asq_running(version="0.0.1") as other:
                for _ in range(500):  # the beat thread's read of it (none before the fix)
                    if getattr(registration, "others", [other]) == [other]:
                        break
                    await asyncio.sleep(0.01)
                monkeypatch.setattr(os, "listdir", watched)
                app.on_doctor_view_hand_off(DoctorView.HandOff(("uninstall", "--reopen")))
                await pilot.pause()
                monkeypatch.setattr(os, "listdir", listdir)
                assert app.hand_off is None, "refused, from what the beat thread read"

    asyncio.run(run())
    assert on_the_ui_thread == []


def test_9_inside_tmux_how_the_client_attached_is_asked_once_a_minute_not_every_beat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[float] = []

    def attached_kind(env: object = None) -> instances.Kind:
        asked.append(time.monotonic())
        return "ssh"

    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,1234,0")
    monkeypatch.setattr(instances, "attached_kind", attached_kind)
    monkeypatch.setattr(instances, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(instances, "POLL_SECONDS", 0.005)
    registration = _registered()
    registration.start()
    first = registration.instance.heartbeat
    deadline = time.monotonic() + 5
    beats = 0
    while time.monotonic() < deadline and beats < 20:
        if registration.instance.heartbeat > first:
            beats += 1
            first = registration.instance.heartbeat
        time.sleep(0.005)
    registration.deregister()

    assert beats >= 20 and len(asked) == 1, f"{len(asked)} asks over {beats} beats"


# --- re-review of #462 -------------------------------------------------------------------------


@pytest.mark.parametrize("read", ["vanished", "failed-once"])
def test_r1_a_record_that_vanishes_or_fails_one_read_is_no_asq_of_an_unknown_release(
    read: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An asq that quits between the folder's listing and the read, or a read that fails once
    (a sharing violation during another asq's beat on Windows, an NFS ESTALE): the gate took
    either for a record it could not read and refused a newcomer for it."""
    read_text = Path.read_text
    calls: list[Path] = []

    with asq_running() as other:
        record = instances.registry_dir() / f"{other.id}.json"

        def flaky(self: Path, *args: Any, **kwargs: Any) -> str:
            if self == record and not calls:
                calls.append(self)
                if read == "vanished":
                    self.unlink()  # its asq quit just now
                    raise FileNotFoundError(2, "No such file or directory", str(self))
                raise PermissionError(13, "sharing violation", str(self))
            return read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", flaky)
        registration = instance_gate.admit(interactive=False)
        monkeypatch.setattr(Path, "read_text", read_text)

        said = capsys.readouterr().err
        assert registration is not None and "cannot read" not in said, said
        if read == "failed-once":
            assert instances.other_instances() == [other], "read again, and live"


def test_r2_an_unreadable_record_whose_lock_is_held_counts_at_any_age(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A held lock is a running process, however old its record (review finding 5); a record
    this release cannot read was ignored once a minute old all the same."""
    with asq_running(version="0.9.9") as later:
        path = instances.registry_dir() / f"{later.id}.json"
        path.write_text(json.dumps({**later.to_record(), "started_at": 1}), encoding="utf-8")
        long_ago = time.time() - instances.STALE_SECONDS - 30
        os.utime(path, (long_ago, long_ago))

        with pytest.raises(typer.Exit):
            instance_gate.admit(interactive=False)

    assert f"its record {path}" in " ".join(capsys.readouterr().err.split())


def test_r3_on_a_home_asq_cannot_write_ui_project_says_so_once(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """``ui --project`` passes the gate before ``run_ui`` does, and each said it."""

    def unwritable() -> Path:
        raise PermissionError(13, "Permission denied", str(instances.registry_dir()))

    monkeypatch.setattr(instances, "_private_dir", unwritable)
    monkeypatch.setattr(fleet_cli, "interactive_terminal", lambda: True)
    monkeypatch.setattr(fleet_service, "ui_scope", lambda *refs: None)
    monkeypatch.setattr(ui_app, "run_ui", lambda **options: instance_gate.admit())

    result = runner.invoke(cli_app, ["ui", "--project", "alpha"])

    assert result.exit_code == 0, result.output
    assert result.stderr.count("could not check the other asqs on this home") == 1


def test_r4_a_record_whose_read_keeps_failing_while_its_lock_is_held_still_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Final re-review: a read that failed twice was skipped as no asq, though its lock held
    said one ran; the newcomer registered beside it silently, where 673e0237 refused."""
    read_text = Path.read_text
    with asq_running(version="0.9.9") as later:
        record = instances.registry_dir() / f"{later.id}.json"

        def failing(self: Path, *args: Any, **kwargs: Any) -> str:
            if self == record:
                raise PermissionError(13, "Permission denied", str(self))
            return read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", failing)
        with pytest.raises(typer.Exit):
            instance_gate.admit(interactive=False)
        monkeypatch.setattr(Path, "read_text", read_text)

    assert instances.this_instance() is None
    assert f"its record {record}" in " ".join(capsys.readouterr().err.split())
