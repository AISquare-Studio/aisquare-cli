"""#21: the instance gate also sees an asq UI older than the registry (0.9.1 and before write no
record), found in ``ps``, and refuses beside it as beside one that cannot be asked to quit.

The owner's case is the first upgrade after the registry ships: the Mac's asq still 0.9.1,
the one over SSH the new release.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import typer

from aisquare.cli.ui import app as ui_app
from aisquare.cli.ui import instance_gate
from aisquare.core import instance_scan, instances, locking, procs
from aisquare.core.instance_scan import parse_elapsed, ui_command
from tests.fakebin import executable_fake

_REAL_PS = instance_scan._ps
"""Taken before conftest's fixture makes every test's ``ps`` say nothing."""

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="the scan is skipped on Windows")

UV = "/Users/u/.local/share/uv/tools/aisquare-cli/bin/python"
BIN = "/Users/u/.local/bin"
VENV = "/Users/u/src/aisquare-cli/.venv/bin"
FRAMEWORK = (
    "/Library/Frameworks/Python.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/Python"
)

UIS = [
    f"{UV} {BIN}/asq",  # what the one-line installer's asq is, in ps
    f"{UV} {BIN}/asq ui --project api",
    f"{UV} {BIN}/aisquare",
    f"{UV} {BIN}/aisquare ui --group ops",
    f"{VENV}/python {VENV}/asq",  # a dev checkout
    f"{FRAMEWORK} {VENV}/asq",
    "/usr/bin/python3 -m aisquare ui",
    f"{UV} -P -m aisquare ui --after-update",  # what ⬆ Restart & update reopens
    f"{UV} -P -m aisquare",
    f"{UV} -X dev -I {BIN}/asq",
    f"{BIN}/asq",
    "asq --profile work",
    "asq --profile=work ui",
    "aisquare -v --no-color",
    "asq -vq",  # global short flags written together (tester's reopen of #21)
    "aisquare -qv ui",
]

NOT_UIS = [
    f"{UV} -P -m aisquare launch --role coder --project api",  # a fleet agent
    f"{UV} -P -m aisquare watch",  # the watcher
    f"{UV} {BIN}/aisquare hook user-prompt-submit",
    f"{UV} {BIN}/asq-statusline",
    f"{BIN}/asq-statusline",
    f"{UV} {BIN}/aisquare remote serve",
    f"{UV} {BIN}/aisquare serve",
    f"{UV} {BIN}/asq fleet ls",
    f"{UV} {BIN}/aisquare upgrade --reopen",
    f"{UV} {BIN}/aisquare uninstall",
    f"{UV} {BIN}/asq --json",
    f"{UV} {BIN}/asq --version",
    "aisquare --help",
    f"{VENV}/python -m pytest -n auto",
    f"{VENV}/pytest tests/test_instance_gate.py",
    f"{VENV}/python -u -c import sys;exec(eval(sys.stdin.readline()))",  # an xdist worker
    "/opt/homebrew/bin/tmux -L asq -f /Users/u/.aisquare/fleet-tmux.conf -C attach-session "
    "-t $13 -f ignore-size,no-output,no-detach-on-destroy",
    "/opt/homebrew/bin/tmux -L asq new-session -d -s asq-loyal-narwhal -n manager",
    "/bin/zsh -c asq",
    "uv run asq",  # uv's own process: the python it starts is the UI
    "claude --append-system-prompt you run under asq",
    f"{UV} -m aisquare.cli.app",
    f"{UV} {BIN}/tasq",
    "asq -vV",
    "asq -qh",
    "",
]


@pytest.mark.parametrize("command", UIS)
def test_these_command_lines_are_asq_uis(command: str) -> None:
    assert ui_command(command)


@pytest.mark.parametrize("command", NOT_UIS)
def test_no_other_aisquare_process_is_taken_for_a_ui(command: str) -> None:
    assert not ui_command(command)


@pytest.mark.parametrize(
    ("text", "elapsed"),
    [
        ("00:07", timedelta(seconds=7)),
        ("05:07", timedelta(minutes=5, seconds=7)),
        ("01:02:03", timedelta(hours=1, minutes=2, seconds=3)),
        ("2-03:04:05", timedelta(days=2, hours=3, minutes=4, seconds=5)),
        ("12", None),
        ("a:b", None),
        ("x-01:02", None),
    ],
)
def test_ps_elapsed_time_is_read_in_every_form_ps_writes(
    text: str, elapsed: timedelta | None
) -> None:
    assert parse_elapsed(text) == elapsed


# --- the look ----------------------------------------------------------------------------------


def _line(pid: int, ppid: int, etime: str, tty: str, command: str) -> str:
    return f"{pid:>7} {ppid:>6} {etime:>11} {tty:<8} {command}"


@pytest.fixture
def listing(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Make ``ps`` say ``lines``; every process on this home unless ``elsewhere`` names it."""

    def say(lines: list[str], *, elsewhere: frozenset[int] = frozenset()) -> None:
        monkeypatch.setattr(instance_scan, "_ps", lambda: "\n".join(lines) + "\n")
        monkeypatch.setattr(
            instance_scan,
            "_environment",
            lambda process: {"AISQUARE_HOME": "/elsewhere"} if process.pid in elsewhere else None,
        )

    return say


def test_an_asq_ui_with_no_record_is_found_and_nothing_else(listing: Any) -> None:
    me, parent, grandparent = os.getpid(), 70001, 70000
    listing(
        [
            _line(grandparent, 1, "03:00:00", "ttys002", f"{UV} {BIN}/asq"),  # e.g. `asq` wrapping
            _line(parent, grandparent, "00:05", "ttys002", "/bin/zsh -c asq"),
            _line(me, parent, "00:01", "ttys002", f"{UV} {BIN}/asq"),
            _line(4242, 1, "02:31:07", "ttys004", f"{UV} {BIN}/asq"),  # the Mac's 0.9.1
            _line(4300, 1, "01:00:00", "ttys005", f"{UV} {BIN}/asq"),  # a registered one
            _line(4400, 1, "00:03", "ttys006", f"{UV} {BIN}/asq"),  # starting: no record yet
            _line(4500, 1, "01:00:00", "ttys007", f"{UV} {BIN}/asq"),  # another home's
            _line(4600, 1, "01:00:00", "??", f"{UV} -P -m aisquare watch"),
            "  garbage line",
        ],
        elsewhere=frozenset({4500}),
    )

    [found] = instance_scan.unregistered_uis({4300})

    assert (found.pid, found.tty, found.command) == (4242, "/dev/ttys004", f"{UV} {BIN}/asq")
    started = datetime.now(UTC) - timedelta(hours=2, minutes=31, seconds=7)
    assert abs(found.started_at - started) < timedelta(seconds=5)
    assert found.where.startswith("pid 4242, /dev/ttys004, since ")


def test_with_nothing_else_running_the_look_is_one_ps_bounded_at_two_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        line = _line(os.getpid(), 1, "00:01", "ttys001", f"{UV} {BIN}/asq")
        return subprocess.CompletedProcess(argv, 0, stdout=line + "\n", stderr="")

    monkeypatch.setattr(instance_scan, "_ps", _REAL_PS)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", run)

    assert instance_scan.unregistered_uis(set()) == []
    [(argv, kwargs)] = calls
    assert argv[0] == "ps" and kwargs["timeout"] == instance_scan.PS_TIMEOUT_SECONDS == 2.0


@POSIX_ONLY
@pytest.mark.parametrize("ps", ["missing", "fails", "hangs"])
def test_a_ps_that_cannot_answer_skips_the_look_with_one_log_line(
    ps: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if ps == "fails":
        executable_fake(
            bin_dir, "ps", posix="echo 'ps: illegal option' >&2", windows="", exit_code=1
        )
    elif ps == "hangs":
        executable_fake(bin_dir, "ps", posix="/bin/sleep 5", windows="")
        monkeypatch.setattr(instance_scan, "PS_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(instance_scan, "_ps", _REAL_PS)
    caplog.set_level(logging.INFO, logger=instance_scan.__name__)

    assert instance_scan.unregistered_uis(set()) == []
    [said] = [r for r in caplog.records if r.name == instance_scan.__name__]
    assert said.levelno == logging.INFO and "is skipped" in said.getMessage()


def test_on_windows_the_look_is_skipped_with_one_log_line(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ran: list[object] = []
    monkeypatch.setattr(instance_scan, "_ps", _REAL_PS)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a))
    caplog.set_level(logging.INFO, logger=instance_scan.__name__)

    assert instance_scan.unregistered_uis(set()) == [] and ran == []
    assert ["Windows has no ps" in r.getMessage() for r in caplog.records] == [True]


@POSIX_ONLY
def test_another_homes_asq_is_not_this_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sandboxed asq on another home (a tester's, a second home of one's own) shares neither
    store nor registry: its environment says so, read from the process itself."""
    sandbox = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env={**os.environ, "AISQUARE_HOME": "/tmp/another-aisquare-home"},
    )
    try:
        process = instance_scan._Process(sandbox.pid, 1, timedelta(minutes=5), None, "")
        command = subprocess.run(
            ["ps", "-ww", "-p", str(sandbox.pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        process = process._replace(command=command)
        assert not instance_scan._on_this_home(process)
        here = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            mine = process._replace(
                pid=here.pid,
                command=subprocess.run(
                    ["ps", "-ww", "-p", str(here.pid), "-o", "command="],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip(),
            )
            assert instance_scan._on_this_home(mine), "control: this home's"
        finally:
            here.kill()
            here.wait()
    finally:
        sandbox.kill()
        sandbox.wait()


# --- the gate ----------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_registration_outlives_its_test(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(instances, "attached_kind", lambda env=None: "native")
    yield
    registration = instances.this_registration()
    if registration is not None:
        registration.deregister()


def test_beside_an_asq_older_than_the_registry_asq_builds_nothing_and_says_how_to_quit_it(
    listing: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    listing([_line(4242, 1, "02:31:07", "ttys004", f"{UV} {BIN}/asq")])
    built: list[object] = []
    asked: list[str] = []
    monkeypatch.setattr(ui_app, "FleetApp", lambda **options: built.append(options))
    monkeypatch.setattr(instance_gate, "_interactive", lambda: True)
    monkeypatch.setattr(instance_gate, "_confirm", lambda question: asked.append(question))

    with pytest.raises(typer.Exit) as refused:
        ui_app.run_ui()

    said = " ".join(capsys.readouterr().err.split())
    assert refused.value.exit_code == 1 and built == [] and asked == [], "nothing to ask it"
    assert "asq of a release before the registry — pid 4242, /dev/ttys004, since " in said
    assert "~/.local/bin/asq" in said or f"{BIN}/asq" in said, "its command, named"
    assert "The asq of pid 4242 predates the registry: its release is unknown" in said
    assert "Quit it in its own terminal (q), or end it with `kill 4242`: a SIGTERM skips" in said
    assert "asq did not start. Start it again once that asq has quit." in said
    assert instances.live_instances() == [], "nothing of this asq registered"


_LINGERING_STAND_IN = r"""
import sys, threading, time
from dataclasses import replace
from aisquare.core import instances
instances.attached_kind = lambda env=None: "native"
me = replace(instances.describe_this_process(), version="0.0.1")
if len(sys.argv) > 1:
    me = replace(me, host=sys.argv[1])  # registered under a name the network has since changed
with instances.gate():
    registration = instances.register(me)
asked = threading.Event()
registration.deliver_quits_to(lambda request: asked.set())
registration.start()
print("ready", flush=True)
asked.wait(30)
registration.deregister()
time.sleep(1.0)  # what an asq's exit takes after it leaves the registry
"""


@pytest.mark.parametrize("host", ["same", "renamed"])
def test_yes_waits_for_the_asked_asqs_process_to_end_not_only_its_record(
    host: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An asq leaves the registry, then runs a moment longer to its exit. Taken as gone at its
    record, it was in ``ps`` still, an asq UI with no record: refused as one before the
    registry, the yes it had just been given undone. ``renamed``: its record names a host name
    the network has changed since (#466's review); its lock, held as the wait begins, says it
    runs on this machine all the same."""
    argv = [sys.executable, "-c", _LINGERING_STAND_IN]
    if host == "renamed":
        argv.append("dhcp-10-0-0-5.example.net")
    stand_in = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)
    threading.Thread(target=stand_in.wait, daemon=True).start()  # reaped as it ends: no zombie
    assert stand_in.stdout is not None and stand_in.stdout.readline().strip() == "ready"
    line = _line(stand_in.pid, 1, "05:00", "ttys004", f"{UV} {BIN}/asq")
    monkeypatch.setattr(
        instance_scan, "_ps", lambda: line + "\n" if procs.alive(stand_in.pid) else ""
    )
    monkeypatch.setattr(instance_scan, "_environment", lambda process: None)

    try:
        registration = instance_gate.admit(ask=lambda question: True, interactive=True)
    finally:
        stand_in.kill()

    said = capsys.readouterr().err
    assert registration is not None, said
    assert "✓ asq 0.0.1" in said and "predates the registry" not in said


def test_an_asq_whose_record_this_release_cannot_read_is_not_also_taken_for_a_pre_registry_one(
    listing: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A later release's record this one cannot read is an asq of an unknown release (review
    of #462), and its process is a UI in ``ps`` with no record this one reads: said once, as
    the unreadable record, never as one that predates the registry."""
    from tests.asq_elsewhere import asq_running

    with asq_running(version="0.9.9", pid=4242) as later:
        path = instances.registry_dir() / f"{later.id}.json"
        path.write_text(
            json.dumps({**later.to_record(), "started_at": 1793377097}), encoding="utf-8"
        )
        listing([_line(4242, 1, "01:00:00", "ttys004", f"{UV} {BIN}/asq")])

        with pytest.raises(typer.Exit):
            instance_gate.admit(interactive=False)

    said = " ".join(capsys.readouterr().err.split())
    assert f"its record {path}" in said
    assert "predates the registry" not in said and "before the registry" not in said


# --- the tester's reopen of #21 ----------------------------------------------------------------


def test_a_live_record_whose_lock_is_held_is_never_taken_for_a_pre_registry_asq(
    listing: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """macOS renames the host with the network when no HostName is set: an asq of this very
    machine, registered under the old name, holds its lock and its record is live, yet only
    records naming this host had their pids left out of the look, and it was refused as one
    before the registry, with `kill <pid>` advice."""
    from tests.asq_elsewhere import asq_running

    renamed = "dhcp-10-0-0-5.example.net"
    assert renamed != instances.host_name()
    with asq_running(host=renamed, pid=4242) as mine_renamed:
        listing([_line(4242, 1, "01:00:00", "ttys003", f"{UV} {BIN}/asq")])

        registration = instance_gate.admit(interactive=False)

        said = capsys.readouterr().err
        assert registration is not None, said
        assert "before the registry" not in said and "kill 4242" not in said
        assert instances.other_instances() == [mine_renamed], "both live, each sees the other"


def _gate_held() -> bool:
    """Whether some holder has ``.gate.lock`` right now (as another asq starting would find it)."""
    lock = instances.registry_dir() / instances.GATE_LOCK
    try:
        fd = os.open(lock, os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        try:
            locking.acquire(fd, wait_s=0)
        except TimeoutError:
            return True
        locking.unlock(fd)
        return False
    finally:
        os.close(fd)


def test_the_look_in_ps_runs_outside_the_gates_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Its ps calls are bounded at 2 s each, one more per asq UI of another home: held under
    the gate's lock, N of them held an asq starting beside this one past GATE_WAIT_SECONDS,
    and it was refused ("held its gate over 2 s")."""
    seen: list[tuple[str, bool]] = []
    instances.registry_dir().mkdir(parents=True)  # where an asq that ran before left it
    (instances.registry_dir() / instances.GATE_LOCK).touch()

    def ps() -> str:
        seen.append(("ps", _gate_held()))
        return _line(4500, 1, "01:00:00", "ttys007", f"{UV} {BIN}/asq") + "\n"

    def environment(process: Any) -> dict[str, str]:
        seen.append(("ps -E", _gate_held()))
        return {"AISQUARE_HOME": "/elsewhere"}

    monkeypatch.setattr(instance_scan, "_ps", ps)
    monkeypatch.setattr(instance_scan, "_environment", environment)

    assert instance_gate.admit(interactive=False) is not None
    assert seen and all(not held for _, held in seen), seen


def test_an_asq_starting_beside_a_slow_look_is_not_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(instances, "GATE_WAIT_SECONDS", 0.3)
    looking = threading.Event()

    def slow_ps() -> str:
        looking.set()
        time.sleep(1.0)  # a loaded machine's ps, under its 2 s bound
        return ""

    monkeypatch.setattr(instance_scan, "_ps", slow_ps)
    outcome: list[object] = []

    def beside() -> None:
        looking.wait(5)
        try:
            with instances.gate():
                outcome.append("passed")
        except TimeoutError as exc:
            outcome.append(exc)

    other = threading.Thread(target=beside)
    other.start()
    registration = instance_gate.admit(interactive=False)
    other.join(5)

    assert registration is not None and outcome == ["passed"], outcome


def test_beside_an_unreadable_record_whose_pid_cannot_be_read_no_asq_is_called_pre_registry(
    listing: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """#466's review, nit 2: a record this release cannot read and whose pid it cannot read
    either; its own asq is in ``ps`` with no record this release reads, and was named a
    release before the registry, with `kill <pid>` advice. The refusal names the record."""
    from tests.asq_elsewhere import asq_running

    with asq_running(version="0.9.9", pid=4242) as later:
        path = instances.registry_dir() / f"{later.id}.json"
        unreadable = {**later.to_record(), "started_at": 1, "pid": "a pid this release cannot read"}
        path.write_text(json.dumps(unreadable), encoding="utf-8")
        listing([_line(4242, 1, "01:00:00", "ttys004", f"{UV} {BIN}/asq")])

        with pytest.raises(typer.Exit):
            instance_gate.admit(interactive=False)

    said = " ".join(capsys.readouterr().err.split())
    assert f"its record {path}" in said
    assert "before the registry" not in said and "kill 4242" not in said


_RENAMED_STAND_IN = r"""
import sys, threading, time
from dataclasses import replace
from aisquare.core import instances
instances.attached_kind = lambda env=None: "native"
instances.POLL_SECONDS = 0.02  # each takes its quit request at once, the two together
me = replace(instances.describe_this_process(), version="0.0.1", host=sys.argv[1])
with instances.gate():
    registration = instances.register(me)
asked = threading.Event()
registration.deliver_quits_to(lambda request: asked.set())
registration.start()
print("ready", flush=True)
asked.wait(30)
registration.deregister()  # its lock let go here, its process on for sys.argv[2] seconds
time.sleep(float(sys.argv[2]))
"""


def test_two_renamed_host_asqs_asked_to_quit_at_once_are_both_waited_out(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#466's re-review, nit 1 (confirmed by the tester, 5432): the owner's case, a Mac asq and
    an SSH one, both of another release and registered under a host name the network has since
    changed. Answered y, the gate asks both to quit, then waits for each in turn; the second
    had let its lock go while the first was waited for, so its wait took it for gone while its
    process still ran, and it was refused as one before the registry: the yes undone. What
    the gate's read found held (``Roll.held``) decides, not the lock as each wait begins."""
    renamed = "dhcp-10-0-0-5.example.net"
    stand_ins: list[subprocess.Popen[str]] = []
    for linger in ("0", "1.0"):  # the second's exit lags the first's by a second
        stand_in = subprocess.Popen(
            [sys.executable, "-c", _RENAMED_STAND_IN, renamed, linger],
            stdout=subprocess.PIPE,
            text=True,
        )
        threading.Thread(target=stand_in.wait, daemon=True).start()  # reaped: no zombie
        assert stand_in.stdout is not None and stand_in.stdout.readline().strip() == "ready"
        stand_ins.append(stand_in)  # started in turn: the gate waits for them in this order
    lines = {
        stand_in.pid: _line(stand_in.pid, 1, "05:00", f"ttys00{at}", f"{UV} {BIN}/asq")
        for at, stand_in in enumerate(stand_ins)
    }
    monkeypatch.setattr(
        instance_scan,
        "_ps",
        lambda: "".join(line + "\n" for pid, line in lines.items() if procs.alive(pid)),
    )
    monkeypatch.setattr(instance_scan, "_environment", lambda process: None)

    try:
        registration = instance_gate.admit(ask=lambda question: True, interactive=True)
    finally:
        for stand_in in stand_ins:
            stand_in.kill()

    said = capsys.readouterr().err
    assert registration is not None, said
    assert "predates the registry" not in said
    for stand_in in stand_ins:
        assert f"✓ asq 0.0.1 (pid {stand_in.pid}) quit" in said
