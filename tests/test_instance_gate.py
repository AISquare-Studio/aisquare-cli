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
from aisquare.core import instances, paths, selfcli
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
        asq_running(host="mini-elsewhere", heartbeat=old) as silent,
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
    try:
        assert stand_in.stdout is not None
        assert stand_in.stdout.readline().strip() == "ready"
        [other] = instances.live_instances()
        assert (other.version, other.pid) == ("0.0.1", stand_in.pid)

        registration = instance_gate.admit(
            ask=lambda q: q == "Quit the other asq?", interactive=True
        )

        out, _ = stand_in.communicate(timeout=20)
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
    command: str, button: str
) -> None:
    async def run() -> None:
        app = ui_app.FleetApp(refresh_seconds=3600, doctor=lambda: [])
        async with app.run_test(size=(120, 40), notifications=True) as pilot:
            await settle_page(app)
            with asq_running(version="0.0.1") as other:
                app.on_doctor_view_hand_off(DoctorView.HandOff((command, "--reopen")))
                await pilot.pause()
                said = " ".join(note.message for note in app._notifications)
                assert app.hand_off is None and not app._exit, "it stays"
                assert f"another asq runs on this home ({other.label})" in said
                assert f"Quit it first (q in it), then press {button} again." in said
            app.on_doctor_view_hand_off(DoctorView.HandOff((command, "--reopen")))
            assert app.hand_off == (command, "--reopen"), "control: alone, it hands over"

    asyncio.run(run())


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
