"""The store broker: what a sandboxed agent's board calls go through (``services.store_broker``).

The structural fix for PR #113's sandbox-boundary class (reviews r2-r9): the aisquare store
is read-only in Codex's sandbox, and the board calls the role protocols make there are run
outside it by the launch's own broker, from a whitelist, as the launch's own sessions.
The live half (``AISQUARE_TEST_CODEX=1``) is in ``tests/test_codex_native.py``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import brain, paths, selfcli
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.agent_adapters.types import WriteScope, store_dir
from aisquare.core.store import store_session
from aisquare.models import TeamSession
from aisquare.services import agent_launch, distill, store_broker
from aisquare.services import team as team_service
from tests.conftest import _PRIVATE_FLEETS

#: The real process start, before the suite's ``no_store_broker`` stands it in.
REAL_RUN_BROKER = store_broker.run_broker
REAL_INSTALL_PATHS = store_broker.install_paths
REAL_SPAWN_DRAIN = distill.spawn_drain  # the autouse fixture stubs it
pytestmark = pytest.mark.skipif(os.name != "posix", reason="the broker is POSIX's (FIFOs)")

MINE = "aaaa1111-0000-0000-0000-000000000000"
THEIRS = "bbbb2222-0000-0000-0000-000000000000"
LAUNCH = "11111111-2222-3333-4444-555555555555"
NONCE = "the-launch-nonce"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A board with two sessions, the first bound to this launch by its own hook."""
    work = tmp_path / "repo"
    work.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    monkeypatch.chdir(work)
    monkeypatch.setenv("AISQUARE_LAUNCH_ID", LAUNCH)
    monkeypatch.delenv("AISQUARE_FLEET_AGENT", raising=False)
    monkeypatch.delenv("AISQUARE_TEAM_HUB", raising=False)
    monkeypatch.setenv("AISQUARE_BRAIN", "0")
    project = team_service.activate(work)
    now = datetime.now(UTC)
    with store_session() as store:
        for sid in (MINE, THEIRS):
            store.upsert_session(
                TeamSession(id=sid, project_id=project.id, started_at=now, last_seen_at=now)
            )
        store.set_meta(f"launch-session:{LAUNCH}", MINE)
    return work


RID = "0" * 32


def _request(argv: list[str], cwd: Path, *, nonce: str = NONCE, rid: str = RID) -> bytes:
    body = {"v": 1, "rid": rid, "argv": argv, "cwd": str(cwd), "columns": None}
    return json.dumps({**body, "mac": store_broker.signature(nonce, body)}).encode()


def _answer(broker: store_broker.Broker, raw: bytes) -> dict[str, object]:
    """``raw`` as a request of its own (its id read from it)."""
    return broker.answer(raw, json.loads(raw).get("rid", RID))


def _broker(tmp_path: Path) -> store_broker.Broker:
    return store_broker.Broker(tmp_path / "broker-dir", NONCE, dict(os.environ))


# --- the whitelist ----------------------------------------------------------------------


def test_the_whitelist_holds_every_board_call_the_role_protocols_teach() -> None:
    """Every ``aisquare …`` a role's cycle tells an agent to run (``core.harness``) runs
    through its broker, but what chooses or runs something outside the sandbox, which the
    broker never runs for one: a fleet operation, a launch, a hook (the agent's own run
    outside its sandbox, and a nested agent's in it bind nothing)."""
    source = (ROOT / "src" / "aisquare" / "core" / "harness.py").read_text(encoding="utf-8")
    taught = set(re.findall(r"`aisquare ([^`]+)`", source))
    assert taught, "the role cycles name their commands"
    for command in taught:
        named = store_broker.command_path(command.split())
        assert not isinstance(named, str), command
        path, _ = named
        if path[0] in ("fleet", "launch", "hook"):
            assert store_broker.vet(command.split()) is not None, command
            continue
        assert path in store_broker.WHITELIST, f"aisquare {command} is not brokered"


def test_the_whitelist_is_the_one_the_docs_list() -> None:
    """The docs list exactly what the broker runs."""
    docs = (ROOT / "docs" / "coding-agents.md").read_text(encoding="utf-8")
    section = docs[docs.index("### The broker's whitelist") :]
    section = section[: section.index("\n## ")]
    listed = {
        tuple(found.split()) for found in re.findall(r"^- `aisquare ([a-z ]+)`", section, re.M)
    }
    assert listed == set(store_broker.WHITELIST)


@pytest.mark.parametrize(
    "argv",
    [
        ["note", "x", "--as", "abc"],
        ["--json", "task", "claim", "t1", "--as", "abc"],
        ["task", "next", "--role", "coder", "--claim", "--as", "abc"],
        ["ctx", "add", "a fact"],
        ["board", "--interval", "2"],
        ["team", "log", "--by", "abc", "--json"],
        ["task", "--help"],
        ["note", "--help"],
    ],
)
def test_the_broker_runs_board_calls(argv: list[str]) -> None:
    assert store_broker.vet(argv) is None


@pytest.mark.parametrize(
    ("argv", "why"),
    [
        (["fleet", "spawn", "coder"], "fleet operation"),
        (["fleet", "tell", "coder-1", "x"], "fleet operation"),
        (["fleet", "stop", "coder-1"], "fleet operation"),
        (["fleet", "shutdown"], "fleet operation"),
        (["fleet", "restart", "coder-1"], "fleet operation"),
        (["launch", "coder"], "cannot run from this agent's sandbox"),
        (["team", "spawn", "coder"], "cannot run"),
        (["team", "bind", "coder", "--bin", "/tmp/x"], "cannot run"),
        (["team", "role", "manager", "--as", "abc"], "cannot run"),
        (["team", "prune", "--release-claims"], "cannot run"),
        (["config", "set", "fleet.tmux_socket", "x"], "cannot run"),
        (["accounts", "default", "2"], "cannot run"),
        (["agents", "connect", "codex"], "cannot run"),
        (["hook", "codex", "Stop"], "cannot run"),
        (["context", "export", "/tmp/out.json"], "cannot run"),
        (["context", "import", "/etc/passwd"], "cannot run"),
        (["upgrade"], "cannot run"),
        (["board", "--watch"], "full-screen"),
        (["board", "-w"], "full-screen"),
        (["board", "-wi", "1"], "full-screen"),
        (["--profile", "x", "note", "y"], "--profile"),
        ([], "with no command"),
    ],
)
def test_the_broker_refuses_what_is_not_a_board_call(argv: list[str], why: str) -> None:
    refusal = store_broker.vet(argv)
    assert refusal is not None and why in refusal
    if "board --watch" not in refusal:
        assert "Its broker runs only: board, context add" in refusal


# --- a request --------------------------------------------------------------------------


def test_a_request_not_signed_by_its_launch_or_run_before_is_refused(
    work: Path, tmp_path: Path
) -> None:
    """A request names its launch by a MAC under the launch's nonce, never the nonce itself
    (a request file is readable by every sandbox), and is run once."""
    broker = _broker(tmp_path)
    signed = _request(["board"], work)
    assert NONCE.encode() not in signed
    altered = json.loads(signed)
    altered["argv"] = ["note", "altered"]
    for raw, rid in (
        (_request(["board"], work, nonce="guessed"), RID),
        (b'{"v": 1, "argv": ["board"]}', RID),
        (json.dumps(altered).encode(), RID),
        (signed, "f" * 32),  # signed for another request id
    ):
        answer = broker.answer(raw, rid)
        assert answer["code"] == 77 and "not from this agent's own launch" in str(answer["stderr"])
    for raw in (b"not json", b"[]", b'{"v": 2}'):
        assert broker.answer(raw, RID)["code"] == 2
    assert _answer(broker, signed)["code"] == 0
    replayed = _answer(broker, signed)
    assert replayed["code"] == 77 and "run already" in str(replayed["stderr"])


@pytest.mark.parametrize("hub", [False, True])
def test_a_request_from_outside_the_launchs_tree_is_refused(
    hub: bool, work: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The broker runs commands only in its launch's own tree: a cwd the sandbox chose in
    another repository would otherwise make a project row there, and post to it. Asked of
    the directory, never of its board: with ``AISQUARE_TEAM_HUB``, which every launch with
    a project sets, every directory resolved to the hub's board and passed (review r10 of
    #113, finding 3)."""
    if hub:
        monkeypatch.setenv("AISQUARE_TEAM_HUB", str(work))
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    for where in (other, Path("/")):
        answer = _answer(_broker(tmp_path), _request(["task", "add", "planted"], where))
        assert answer["code"] == 2 and "outside this agent's tree" in str(answer["stderr"])
    nowhere = _answer(_broker(tmp_path), _request(["board"], tmp_path / "missing"))
    assert nowhere["code"] == 2
    log = paths.log_dir() / store_broker.LOG_NAME
    assert "board: " in log.read_text(encoding="utf-8"), "the refusal is on record"


def test_the_command_runs_as_the_launchs_own_session_and_no_other(
    work: Path, tmp_path: Path
) -> None:
    """A note as the launch's own session lands; one as another session is refused where
    every ``--as`` resolves, whatever the argv says (``services.team._acting_as``)."""
    broker = _broker(tmp_path)
    mine = _answer(broker, _request(["note", "brokered note", "--as", MINE[:8]], work))
    assert mine["code"] == 0, mine
    assert "✓ shared (note): brokered note" in str(mine["stdout"])
    theirs = _answer(broker, _request(["note", "forged", "--as", THEIRS[:8]], work, rid="1" * 32))
    assert theirs["code"] == 1
    assert "is not this agent's own session (its own: aaaa1111)" in str(theirs["stderr"])
    focus = _answer(
        broker, _request(["team", "focus", "x", "--as", THEIRS[:8]], work, rid="2" * 32)
    )
    assert focus["code"] == 1 and "not this agent's own session" in str(focus["stderr"])
    with store_session() as store:
        project = store.get_session(MINE)
        assert project is not None
        texts = [event.text for event in store.recent_events(project.project_id, limit=20)]
    assert "brokered note" in texts and "forged" not in texts


def test_a_brokered_command_acts_only_as_its_own_session(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule itself, in process: on under :data:`store_broker.CALLER_ENV` only; a filter
    (``team log --by``) names any session; before the agent's first hook bound one, none."""
    monkeypatch.setenv(store_broker.CALLER_ENV, "1")
    assert team_service.add_note("mine", session_ref=MINE[:8]).session_id == MINE
    with pytest.raises(team_service.ForeignSessionError, match="its own: aaaa1111"):
        team_service.add_note("theirs", session_ref=THEIRS[:8])
    with pytest.raises(team_service.ForeignSessionError):
        team_service.set_focus("theirs", THEIRS)
    with pytest.raises(team_service.ForeignSessionError):
        team_service.set_role("manager", THEIRS)
    assert team_service.add_note("unattributed").session_id is None
    assert team_service.log_events(by=THEIRS[:8]) == []
    with pytest.raises(store_broker.OutOfScope, match="the fleet's own"):
        team_service.set_signal("fleet-paused", "off", session_ref=MINE)
    assert team_service.set_signal("fold-ready", "on", session_ref=MINE)[0].value == "on"
    with store_session() as store:
        store.delete_meta(f"launch-session:{LAUNCH}")
    with pytest.raises(team_service.ForeignSessionError, match="none yet"):
        team_service.add_note("before the hook", session_ref=MINE[:8])
    monkeypatch.delenv(store_broker.CALLER_ENV)
    assert team_service.add_note("outside", session_ref=THEIRS[:8]).session_id == THEIRS


def test_the_fleets_pause_signal_is_the_fleets_own() -> None:
    from aisquare.services import fleet

    assert fleet.PAUSE_SIGNAL.startswith(team_service._FLEET_SIGNAL_PREFIX)


def test_a_request_is_never_read_through_a_link_or_a_fifo(tmp_path: Path) -> None:
    """``in/`` is the sandbox's to write: a request is opened by name, never through a
    link (the broker would read what the link names), never blocking on a FIFO, and
    never past its size."""
    if sys.platform == "win32":
        pytest.skip("POSIX's")
    inbox = tmp_path / "in"
    inbox.mkdir()
    secret = tmp_path / "secret"
    secret.write_text("not for the sandbox", encoding="utf-8")
    (inbox / "link.json").symlink_to(secret)
    os.mkfifo(inbox / "fifo.json")
    (inbox / "big.json").write_bytes(b"x" * (store_broker.MAX_REQUEST + 1))
    (inbox / "ok.json").write_bytes(b"{}")
    fd = os.open(inbox, os.O_RDONLY)
    try:
        assert "could not be opened" in str(store_broker._take(fd, "link.json"))
        assert store_broker._take(fd, "fifo.json") == "the request is not a file"
        assert "over" in str(store_broker._take(fd, "big.json"))
        assert store_broker._take(fd, "ok.json") == b"{}"
    finally:
        os.close(fd)
    assert sorted(os.listdir(inbox)) == []
    assert secret.read_text(encoding="utf-8") == "not for the sandbox"


# --- the round trip, through a real broker process --------------------------------------


@pytest.fixture
def running(work: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A broker started as a launch starts one, owned by a stand-in for the agent, and the
    CLI's side seeing the store read-only as in the agent's sandbox."""
    agent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    env = dict(os.environ)
    # Less the suite's stand-in `claude` (conftest's `no_real_fleet`, 0.8.2), first on PATH
    # in a folder under /tmp, which the sandbox may write: a launch refuses such a PATH.
    env["PATH"] = os.pathsep.join(
        entry
        for entry in env.get("PATH", "").split(os.pathsep)
        if not any(Path(entry).is_relative_to(private) for private in _PRIVATE_FLEETS)
    )
    with pytest.MonkeyPatch.context() as launching:
        launching.setattr(
            store_broker,
            "run_broker",
            lambda directory, env: REAL_RUN_BROKER(directory, env, owner=agent.pid),
        )
        argv, note = store_broker.start_for_launch(
            env, get_adapter("codex"), ["codex", *_profile()]
        )
    assert note == "" and any(str(store_broker.INBOX) in arg for arg in argv)
    directory = Path(env[store_broker.DIR_ENV])
    monkeypatch.chdir(work)
    monkeypatch.setenv(store_broker.DIR_ENV, str(directory))
    monkeypatch.setenv(store_broker.NONCE_ENV, env[store_broker.NONCE_ENV])
    monkeypatch.setattr(store_broker, "_WRITABLE", {})
    monkeypatch.setattr(store_broker, "_writable", lambda home: False)
    try:
        yield directory
    finally:
        agent.kill()
        agent.wait()
        deadline = time.monotonic() + 10
        while directory.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not directory.exists(), "the broker leaves with its agent"


def _profile() -> list[str]:
    return get_adapter("codex").store_access("workspace", store_dir())


def test_the_cli_runs_a_board_call_through_the_broker_and_prints_its_output(
    running: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert store_broker.brokered() == running
    assert store_broker.forward(["note", "through the broker", "--as", MINE[:8]]) == 0
    printed = capsys.readouterr()
    assert "✓ shared (note): through the broker" in printed.out
    assert store_broker.forward(["fleet", "stop", "coder-1"]) == 2
    assert "fleet operation" in capsys.readouterr().err
    # Read nothing, run here: --version; a nested agent's hook does nothing.
    assert store_broker.forward(["--version"]) is None
    assert store_broker.forward(["hook", "codex", "Stop"]) == 0
    assert list((running / store_broker.INBOX).iterdir()) == []


def test_a_cli_whose_broker_is_gone_says_so(
    running: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(store_broker.DIR_ENV, str(running.parent / "no-such-launch"))
    assert store_broker.forward(["board"]) == 75
    assert "cannot hand the command to this agent's broker" in capsys.readouterr().err


def test_the_cli_writes_the_store_itself_where_it_may(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outside the sandbox (the agent's own hooks, an escalated command) the CLI is what
    it always was, the broker named or not."""
    assert store_broker.brokered() is None
    monkeypatch.setenv(store_broker.DIR_ENV, str(tmp_path))
    store_dir().mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(store_broker, "_WRITABLE", {})
    assert store_broker.brokered() is None, "a writable home"
    monkeypatch.setattr(store_broker, "_WRITABLE", {})
    monkeypatch.setattr(store_broker, "_writable", lambda home: False)
    assert store_broker.brokered() == tmp_path
    monkeypatch.setenv("AISQUARE_HOME", str(tmp_path / "missing"))
    assert store_broker.brokered() is None, "no home to read"


def test_the_entry_point_forwards_before_any_command_runs(running: Path, tmp_path: Path) -> None:
    """``aisquare …`` itself, as a sandboxed tool runs it: the console script's
    ``main`` hands the argv over before the app opens anything."""
    env = dict(os.environ)
    code = (
        "import sys; from aisquare.services import store_broker as b; "
        "b._writable = lambda home: False; from aisquare.cli.app import main; "
        "sys.argv = ['aisquare', *sys.argv[1:]]; main()"
    )
    done = subprocess.run(
        [sys.executable, "-P", "-c", code, "task", "list"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "No shared tasks yet" in done.stdout


# --- the launch's side ------------------------------------------------------------------


def test_a_launch_of_an_unsandboxed_agent_has_no_broker() -> None:
    env: dict[str, str] = {"AISQUARE_LAUNCH_ID": LAUNCH}
    argv = ["claude", "--permission-mode", "auto"]
    assert store_broker.start_for_launch(env, get_adapter("claude-code"), argv) == (argv, "")
    assert store_broker.DIR_ENV not in env
    # Codex without the store's profile (a sandbox of the operator's own): none either.
    native = ["codex", "--sandbox", "workspace-write"]
    assert store_broker.start_for_launch(env, get_adapter("codex"), native) == (native, "")
    assert store_broker.DIR_ENV not in env


def test_a_launch_whose_broker_cannot_start_holds_the_store_read_only_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    def fails(directory: Path, env: object) -> None:
        raise RuntimeError("it exited 1")

    monkeypatch.setattr(store_broker, "run_broker", fails)
    env: dict[str, str] = {"AISQUARE_LAUNCH_ID": LAUNCH}
    argv, note = store_broker.start_for_launch(env, get_adapter("codex"), ["codex", *_profile()])
    assert "its broker did not start (it exited 1)" in note
    assert store_broker.DIR_ENV not in env
    assert not store_broker.launch_dir(store_dir(), LAUNCH).exists()
    table = argv[-1].partition("=")[2]
    assert f'{json.dumps(str(store_dir()))} = "read"' in table, "read-only all the same"


def test_a_launch_prunes_the_directories_of_brokers_that_are_gone() -> None:
    root = store_dir() / store_broker.DIR_NAME
    gone, live, fresh, stale = (root / name for name in ("gone", "live", "fresh", "stale"))
    for directory in (gone, live, fresh, stale):
        (directory / store_broker.INBOX).mkdir(parents=True)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (gone / store_broker.READY).write_text(json.dumps({"pid": dead.pid}), encoding="utf-8")
    (live / store_broker.READY).write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    os.utime(stale, (time.time() - 120, time.time() - 120))
    (gone / store_broker.INBOX / "planted").symlink_to(store_dir())
    store_broker.prune(root)
    assert sorted(path.name for path in root.iterdir()) == ["fresh", "live"]
    assert store_dir().is_dir(), "a link the sandbox planted is removed, never followed"


def test_the_broker_module_runs_as_its_own_process() -> None:
    """``run_broker``'s command line names the module that serves."""
    argv = selfcli.argv_for(["--help"], module="aisquare.services.store_broker")
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0 and "--owner-pid" in done.stdout


def test_aisquare_launch_gives_a_sandboxed_codex_its_broker(
    work: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """The window's ``aisquare launch`` runs the agent with its inbox the one writable path
    in the store, and the broker named in its environment; with no store profile in its
    arguments (Codex's own sandbox, or none), with none."""
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    monkeypatch.setattr(agent_launch, "executable", lambda selected: selected.binary.binary)
    result = runner.invoke(app, ["launch", "coder", "--agent", "codex", "--", *_profile()])
    assert result.exit_code == 0, result.output
    argv, env = execute.call_args.args[1], execute.call_args.args[2]
    directory = Path(env[store_broker.DIR_ENV])
    assert directory == store_broker.launch_dir(store_dir(), env["AISQUARE_LAUNCH_ID"])
    assert env[store_broker.NONCE_ENV] and (directory / store_broker.BELL).exists()
    table = argv[-1].partition("=")[2]
    assert f'{json.dumps(str(directory / store_broker.INBOX))} = "write"' in table
    assert f'{json.dumps(str(store_dir()))} = "read"' in table

    result = runner.invoke(app, ["launch", "coder", "--agent", "codex", "--sandbox", "read-only"])
    assert result.exit_code == 0, result.output
    assert store_broker.DIR_ENV not in execute.call_args.args[2]


def test_team_spawn_exec_starts_the_broker_as_launch_does(
    work: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    started: list[list[str]] = []
    executed: list[list[str]] = []

    def broker(selected: object, env: dict[str, str], argv: list[str]) -> tuple[list[str], str]:
        started.append(argv)
        return [*argv, "--with-inbox"], ""

    monkeypatch.setattr(agent_launch, "start_broker", broker)
    monkeypatch.setattr(agent_launch, "executable", lambda selected: selected.binary.binary)
    monkeypatch.setattr(
        "aisquare.cli.team.os.execvpe", lambda file, argv, env: executed.append(argv)
    )
    result = runner.invoke(app, ["team", "spawn", "coder", "--agent", "codex", "--exec"])
    assert result.exit_code == 0, result.output
    assert started and executed == [[*started[0], "--with-inbox"]]


# --- review r10 of #113 -----------------------------------------------------------------


def _brokered(monkeypatch: pytest.MonkeyPatch, work: Path) -> str:
    """This process as a command the launch's broker runs, scoped to ``work``'s board and
    pool; the board's id."""
    from aisquare.core.orchestrator import team_project
    from aisquare.core.workspace import current_project

    board = team_project(work).id
    monkeypatch.setenv(store_broker.CALLER_ENV, "1")
    monkeypatch.setenv(store_broker.BOARD_ENV, board)
    monkeypatch.setenv(store_broker.POOL_ENV, current_project(work).id)
    return board


def _other_repo(tmp_path: Path) -> Path:
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    team_service.activate(other)
    return other


def test_a_brokered_command_names_only_its_own_boards_tasks(
    work: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r10 of #113, finding 3: task ids resolve across every board, so a sandboxed
    agent's ``task done`` (block, drop, release, reopen, claim, review, show) acted on any
    board's task, retiring its assignments and waking that board's manager."""
    other = _other_repo(tmp_path)
    theirs, _ = team_service.add_task("their task", cwd=other)
    mine, _ = team_service.add_task("my task", cwd=work)
    _brokered(monkeypatch, work)
    for act in (
        lambda ref: team_service.finish_task(ref, session_ref=MINE),
        lambda ref: team_service.claim_task(ref, session_ref=MINE),
        lambda ref: team_service.block_task(ref, reason="x", session_ref=MINE),
        lambda ref: team_service.release_task(ref, session_ref=MINE),
        lambda ref: team_service.drop_task(ref, session_ref=MINE),
        team_service.show_task,
    ):
        with pytest.raises(store_broker.OutOfScope, match="another board"):
            act(theirs.id)
    with store_session() as store:
        untouched = store.get_task(theirs.id)
    assert untouched is not None and untouched.status == "todo"
    assert team_service.claim_task(mine.id, session_ref=MINE).status == "doing"
    with pytest.raises(store_broker.OutOfScope, match="this directory's board"):
        team_service.add_note("planted", cwd=other)


def test_a_brokered_command_writes_only_its_own_projects_pool(
    work: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """Review r10 of #113, finding 3 (and SMOKE5's ``remember --user``): the user pool is
    given to every project's agents, and another project's pool to that project's."""
    from aisquare.services import context as context_service

    other = _other_repo(tmp_path)
    _brokered(monkeypatch, work)
    with pytest.raises(store_broker.OutOfScope, match="user pool"):
        context_service.add_entry("everyone's", "user", [])
    assert context_service.add_entry("ours", None, []).pool == "project", "the default"
    refused = runner.invoke(app, ["remember", "everyone's", "--user"])
    assert refused.exit_code == 1 and "user pool" in refused.output
    monkeypatch.chdir(other)
    with pytest.raises(store_broker.OutOfScope, match="not this agent's project"):
        context_service.add_entry("theirs", "project", [])


def test_a_refusal_is_logged_and_noted_once_per_kind(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SMOKE5 of #113 (tsk_01m50ex2xtjw): a refused request left nothing outside the
    caller's stderr. Each is a line in the brokers' log; a forged identity, another board's
    task, a fleet operation or a bad signature is also a note on the board, once per launch
    and kind, for the manager and the human."""
    board = _brokered(monkeypatch, work)
    for _ in range(2):
        store_broker.record_refusal("identity", "--as bbbb2222 is not this agent's own session")
    store_broker.record_refusal("fleet", "aisquare fleet stop coder-1 is a fleet operation")
    store_broker.record_refusal("command", "aisquare config set x y cannot run")
    lines = (paths.log_dir() / store_broker.LOG_NAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4 and all(f"launch={LAUNCH}" in line for line in lines)
    with store_session() as store:
        notes = [
            event.text for event in store.recent_events(board, limit=50) if event.kind == "note"
        ]
    assert len([note for note in notes if "refused (identity)" in note]) == 1
    assert len([note for note in notes if "refused (fleet)" in note]) == 1
    assert not any("refused (command)" in note for note in notes)


def test_no_agent_tool_sets_the_fleets_own_signals(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r10 of #113, finding 8: the guard held only for a brokered command, while the
    same agent's MCP ``signal`` tool, which runs outside its sandbox, set ``fleet-paused``."""
    from aisquare.core.spawn import LAUNCH_AGENT_ENV
    from aisquare.services import mcp_server

    with pytest.raises(store_broker.OutOfScope, match="the fleet's own"):
        team_service.set_signal("fleet-paused", "off", agent_tool=True)
    told: dict[str, object] = {}

    def fake(name: str, value: str, **kwargs: object) -> tuple[object, None]:
        told.update(kwargs)
        raise ValueError("stop here")

    monkeypatch.setattr(team_service, "set_signal", fake)
    monkeypatch.setattr(mcp_server, "_ensure_virtual_session", lambda: MINE)
    with pytest.raises(Exception, match="stop here"):
        mcp_server.signal("fleet-paused", "off")
    assert told.get("agent_tool") is True
    # Its tools name only its board's tasks when it serves an agent that sandboxes them.
    monkeypatch.setenv(LAUNCH_AGENT_ENV, "codex")
    assert mcp_server._sandboxed_launch_board() == _brokered(monkeypatch, work)
    monkeypatch.setenv(LAUNCH_AGENT_ENV, "claude-code")
    assert mcp_server._sandboxed_launch_board() is None


def test_a_bell_this_process_may_not_open_is_not_a_broker_that_is_gone(
    running: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review r10 of #113, finding 2: any failure to open the bell read as "not running",
    exit 75, though the request was in ``in/`` and the broker polls it: a retrying agent
    filed its note twice where a sandbox refuses the FIFO (Seatbelt, Landlock)."""
    os.chmod(running / store_broker.BELL, 0o400)
    assert store_broker.forward(["note", "rung or not", "--as", MINE[:8]]) == 0
    assert "✓ shared (note): rung or not" in capsys.readouterr().out


def _no_broker(tmp_path: Path) -> Path:
    if sys.platform == "win32":
        pytest.skip("POSIX's")
    directory = tmp_path / "launch"
    (directory / store_broker.INBOX).mkdir(parents=True)
    (directory / store_broker.OUTBOX).mkdir()
    os.mkfifo(directory / store_broker.BELL)
    return directory


def test_a_command_no_broker_took_is_withdrawn_and_said_not_to_have_run(
    tmp_path: Path,
) -> None:
    directory = _no_broker(tmp_path)
    with pytest.raises(store_broker.BrokerGone, match="not running"):
        store_broker.ask(directory, ["note", "x"])
    assert list((directory / store_broker.INBOX).iterdir()) == [], "withdrawn: it never runs"


def test_a_command_its_broker_took_and_did_not_answer_may_have_run(tmp_path: Path) -> None:
    """Said so, never "failed": the agent reads the board before it runs it again."""
    if sys.platform == "win32":
        pytest.skip("POSIX's")
    directory = _no_broker(tmp_path)
    reader = os.open(directory / store_broker.BELL, os.O_RDONLY | os.O_NONBLOCK)
    inbox = directory / store_broker.INBOX

    def take() -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for entry in inbox.iterdir():
                if entry.name.endswith(".json"):
                    entry.rename(inbox / f".{entry.name}.taken")
                    return
            time.sleep(0.01)

    taker = threading.Thread(target=take)
    taker.start()
    try:
        with pytest.raises(store_broker.BrokerUnavailable, match="may have run"):
            store_broker.ask(directory, ["note", "x"], wait=1.0)
    finally:
        taker.join()
        os.close(reader)


def test_the_cli_asks_the_store_itself_whether_it_may_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r10 of #113, finding 6: ``access(2)`` sees bwrap's read-only mount but not a
    sandbox that refuses at ``open`` (Landlock), and no variable tells a sandboxed command
    from an escalated one (measured). A file is made and removed to know."""
    import tempfile

    home = tmp_path / "home"
    home.mkdir()
    assert store_broker._writable(home) and list(home.iterdir()) == [], "nothing left behind"

    def refused(**kwargs: object) -> tuple[int, str]:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(tempfile, "mkstemp", refused)
    assert not store_broker._writable(home)


def _sleeper(seconds: float) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def test_the_broker_ends_its_commands_when_its_agent_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r10 of #113, finding 7: the broker left its commands running when the agent
    exited, so a claim taken just before ``fleet stop`` landed after the session's claims
    were released. They are ended, and nothing more is taken."""
    broker = _broker(tmp_path)
    monkeypatch.setattr(selfcli, "argv_for", lambda argv: _sleeper(30))
    answers: list[dict[str, object]] = []
    worker = threading.Thread(
        target=lambda: answers.append(broker._run(["note", "x"], str(tmp_path), os.environ))
    )
    worker.start()
    deadline = time.monotonic() + 10
    while not broker._children and time.monotonic() < deadline:
        time.sleep(0.02)
    started = time.monotonic()
    broker.close(grace=0.5)
    worker.join(timeout=10)
    assert time.monotonic() - started < 8
    [answer] = answers
    assert isinstance(answer["code"], int) and answer["code"] < 0
    assert "exited while the command ran" in str(answer["stderr"])
    after = broker.answer(_request(["board"], tmp_path), RID)
    assert after["code"] == 75 and "has exited" in str(after["stderr"])


def test_a_directory_in_the_inbox_is_removed_and_never_taken_again(tmp_path: Path) -> None:
    """Review r10 of #113, finding 9: a directory named like a request was opened, refused
    and left, then taken again every half second for the broker's life."""
    broker = _broker(tmp_path)
    (broker.directory / store_broker.OUTBOX).mkdir(parents=True)
    inbox = broker.directory / store_broker.INBOX
    planted = inbox / f"{RID}.json"
    (planted / "inside").mkdir(parents=True)
    (planted / "inside" / "file").write_text("x", encoding="utf-8")
    fd = os.open(inbox, os.O_RDONLY)
    try:
        broker.take_requests(fd)
        deadline = time.monotonic() + 5
        while not broker.idle() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert list(inbox.iterdir()) == []
        answer = json.loads((broker.directory / store_broker.OUTBOX / f"{RID}.json").read_text())
        assert "not a file" in answer["stderr"]
        broker.take_requests(fd)
        assert broker.idle()
    finally:
        os.close(fd)


def test_output_past_the_cap_is_cut_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r10 of #113, finding 10: output was cut at 4 MB with the exit status kept, so a
    large ``--json`` answer came back unparseable with exit 0 and no sign it was cut."""
    broker = _broker(tmp_path)
    monkeypatch.setattr(store_broker, "MAX_OUTPUT", 100)
    monkeypatch.setattr(
        selfcli, "argv_for", lambda argv: [sys.executable, "-c", "print('x' * 1000)"]
    )
    answer = broker._run(["board", "--json"], str(tmp_path), os.environ)
    assert answer["code"] == 74 and len(str(answer["stdout"])) == 100
    assert "cut this command's output" in str(answer["stderr"])


def test_launch_paths_hold_the_project_trees_inputs_and_the_tmux_sockets(
    tmp_path: Path,
) -> None:
    """Review r10 of #113, findings 1 and 4: outside the store, a sandboxed agent wrote the
    project tree's launch inputs (``.claude/``, ``.mcp.json`` …) and the other agents'
    worktrees, which an unsandboxed Claude Code start there runs, and could move the
    fleet's tmux socket out of ``/tmp``. Each is named read-only; its own worktree stays
    writable, its own inputs read-only within it."""
    if sys.platform == "win32":
        pytest.skip("POSIX's")
    root = tmp_path / "proj"
    root.mkdir()
    git = ["git", "-C", str(root)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run(
        [*git, "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-q", "--allow-empty",
         "-m", "init"],
        check=True,
    )  # fmt: skip
    worktree = root / ".aisquare-worktrees" / "coder-1"
    subprocess.run([*git, "worktree", "add", "-q", "-b", "c1", str(worktree)], check=True)
    env = {"TMUX": "/somewhere/tmux-1000/asq,123,0", "TMUX_TMPDIR": str(tmp_path / "tt")}

    protect, writable = store_broker.launch_paths(env, worktree)

    real = root.resolve()
    for name in store_broker.launch_inputs():
        assert real / name in protect, name
        assert worktree.resolve() / name in protect, name
    assert real / ".aisquare-worktrees" in protect, "the other agents' worktrees"
    assert writable == (worktree.resolve(),), "its own"
    assert Path("/somewhere/tmux-1000") in protect
    assert (tmp_path / "tt").resolve() / f"tmux-{os.getuid()}" in protect
    _, at_root = store_broker.launch_paths(env, root)
    assert at_root == (), "a role with no worktree writes none of the others'"


def test_codex_profile_carries_the_launch_paths(tmp_path: Path) -> None:
    import tomllib

    store, inbox = tmp_path / "home", tmp_path / "home" / "broker" / "l" / "in"
    protect, writable = (tmp_path / "proj" / ".claude", tmp_path / "s"), (tmp_path / "wt",)
    codex = get_adapter("codex")
    args = codex.broker_access(
        codex.store_access("workspace", store), store, inbox, protect=protect, writable=writable
    )
    assert args is not None
    table = tomllib.loads(f"t = {args[-1].partition('=')[2]}")["t"]
    assert table == {
        str(store): "read",
        str(protect[0]): "read",
        str(protect[1]): "read",
        str(inbox): "write",
        str(writable[0]): "write",
    }


# --- review r11 of #113 -----------------------------------------------------------------


def _fleet_record(agent: str, project_id: str, root: Path, cwd: Path) -> object:
    from aisquare.core import launch_seal
    from aisquare.models import LaunchSpec

    return launch_seal.LaunchRecord.model_construct(
        id=agent,
        project_id=project_id,
        project_root=str(root),
        role="coder",
        agent="codex",
        binary="codex",
        cwd=str(cwd),
        spec=LaunchSpec.model_construct(binary="codex"),
        label="coder-1",
    )


def test_under_a_hub_a_fleet_agents_brokered_calls_land_on_the_hubs_board(
    work: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r11 of #113, finding 1: the broker took a fleet agent's board from its launch
    record, spawn's project, which is not the hub's, while every board call resolves the
    hub: under ``AISQUARE_TEAM_HUB`` every brokered note, task call, focus and signal was
    refused, and the refusal went to a board nobody on the hub reads."""
    from aisquare.core import launch_seal
    from aisquare.core.orchestrator import team_project
    from aisquare.core.workspace import current_project

    hub = tmp_path / "hub"
    hub.mkdir()
    spawn_project = current_project(work).id
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub))
    hub_board = team_project(work).id
    team_service.activate(work)
    assert hub_board != spawn_project, "the two resolutions disagree under a hub"
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", "agt_hub")
    record = _fleet_record("agt_hub", spawn_project, work, work)
    monkeypatch.setattr(launch_seal, "read", lambda agent: record)
    hubbed = "cccc3333-0000-0000-0000-000000000000"
    now = datetime.now(UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(id=hubbed, project_id=hub_board, started_at=now, last_seen_at=now)
        )
        store.set_meta(f"launch-session:{LAUNCH}", hubbed)
    broker = store_broker.Broker(tmp_path / "broker-dir", NONCE, dict(os.environ))

    assert broker.scope().board == hub_board
    landed = _answer(broker, _request(["note", "under the hub", "--as", hubbed[:8]], work))

    assert landed["code"] == 0, landed
    with store_session() as store:
        texts = [event.text for event in store.recent_events(hub_board, limit=20)]
    assert "under the hub" in texts


def test_a_project_pin_does_not_turn_a_brokered_memory_write_away(
    work: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r11 of #113, finding 7: the pool was the pinned project's (``project switch``,
    which ``project forget`` sets too), so while a pin pointed elsewhere every brokered
    ``remember`` was refused. The launch's own project's pool takes it."""
    from aisquare.core.workspace import current_project, pin_project
    from aisquare.services import context as context_service

    other = _other_repo(tmp_path)
    pin_project(current_project(other).id)
    _brokered(monkeypatch, work)

    entry = context_service.add_entry("ours, whatever the pin", None, [])

    assert entry.pool == "project" and entry.project_id == current_project(work).id


def test_a_hand_launch_runs_calls_from_its_project_and_a_read_elsewhere_is_only_logged(
    work: Path, tmp_path: Path
) -> None:
    """Review r11 of #113, finding 8: a hand launch's tree was its cwd alone, though its
    project's directory is its own too; and a harmless read from elsewhere (``cd /tmp &&
    aisquare board``) put "asked for what it may not do" on the board."""
    sub = work / "backend"
    sub.mkdir()
    scope = store_broker.launch_scope(sub, dict(os.environ))
    assert scope.holds(work) and scope.holds(sub)
    broker = store_broker.Broker(tmp_path / "broker-dir", NONCE, dict(os.environ), scope=scope)
    with store_session() as store:
        before = len(store.recent_events(scope.board, limit=200))

    read = _answer(broker, _request(["board"], Path("/")))
    write = _answer(broker, _request(["note", "x"], Path("/"), rid="3" * 32))

    assert read["code"] == 2 and write["code"] == 2
    log = (paths.log_dir() / store_broker.LOG_NAME).read_text(encoding="utf-8")
    assert " read: " in log and " board: " in log
    with store_session() as store:
        notes = [e.text for e in store.recent_events(scope.board, limit=200)][before:]
    assert len([note for note in notes if "refused" in note]) == 1, "the write only"


def _a_codex_project(project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``project`` a real one (its ``.aisquare`` marker) on a machine with a ``codex``: where
    the doctor's sandbox rows look (``diagnostics._sandboxed_project``)."""
    (project / ".aisquare").mkdir(parents=True, exist_ok=True)
    tools = tmp_path / "codex-bin"
    tools.mkdir(exist_ok=True)
    codex = tools / "codex"
    codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    codex.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ.get('PATH', '')}")


def _installed_at(monkeypatch: pytest.MonkeyPatch, *places: Path) -> None:
    monkeypatch.setattr(
        store_broker, "install_paths", lambda *args, **kwargs: tuple(p.resolve() for p in places)
    )


def test_a_sandboxed_launch_is_refused_where_its_sandbox_may_write_aisquare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """Review r11 of #113, finding 2: the hooks and the broker run this aisquare outside the
    sandbox, so an aisquare in the project's ``.venv`` or an editable ``src/`` a sandboxed
    tester may write ran what it wrote, at its next hook. Such a launch is refused, naming
    the remedy with the distribution's own name, ``aisquare-cli`` (r12, finding 6), and the
    doctor says so with no fix it would run from that install."""
    from aisquare.services import diagnostics, onboarding

    project = tmp_path / "proj"
    venv = project / ".venv"
    venv.mkdir(parents=True)
    _a_codex_project(project, tmp_path, monkeypatch)
    _installed_at(monkeypatch, venv)
    codex = get_adapter("codex")
    argv = ["codex", *_profile()]
    with pytest.raises(store_broker.LaunchRefused, match="uv tool install aisquare-cli"):
        store_broker.refuse_inside(argv, project, codex)
    monkeypatch.chdir(project)
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    monkeypatch.setattr(agent_launch, "executable", lambda selected: selected.binary.binary)
    refused = runner.invoke(app, ["launch", "tester", "--agent", "codex", "--", *_profile()])
    assert refused.exit_code == 1 and "uv tool install aisquare-cli" in refused.output
    assert not execute.called
    row = diagnostics._sandboxed_install_check(project)
    assert row is not None and row.status.value == "warn" and ".venv" in row.detail
    assert "aisquare-cli" in str(row.fix)
    assert onboarding.fix_commands([row]) == [], "no button runs connect from this install"
    _installed_at(monkeypatch, Path("/opt/elsewhere"))
    assert diagnostics._sandboxed_install_check(project) is None
    store_broker.refuse_inside(argv, project, codex)  # no refusal


@pytest.fixture
def beyond_tmp() -> Iterator[Path]:
    """A directory outside every temporary directory a sandbox may write (``/tmp``,
    ``$TMPDIR``), where only the launch's arguments make it writable: pytest's own is in
    ``/tmp``, which every workspace sandbox may write."""
    if sys.platform == "win32":
        pytest.skip("POSIX's")
    place = Path(tempfile.mkdtemp(prefix="asq-test-", dir="/var/tmp"))
    try:
        yield place.resolve()
    finally:
        shutil.rmtree(place, ignore_errors=True)


def test_the_install_check_reads_what_the_sandbox_may_write(
    beyond_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r12 of #113, findings 3, 7 and R1: the check ran only with the store's profile
    and assumed ``:workspace``. A native ``--sandbox workspace-write`` (or ``-c
    sandbox_mode``) skipped it, a ``-C`` or ``--add-dir`` tree was not looked at, and a
    ``:read-only`` seat, whose sandbox writes none of the project, was refused. It asks
    what the sandbox may write."""
    monkeypatch.delenv("TMPDIR", raising=False)
    project = beyond_tmp / "proj"
    (project / ".venv").mkdir(parents=True)
    _installed_at(monkeypatch, project / ".venv")
    elsewhere = beyond_tmp / "elsewhere"
    elsewhere.mkdir()
    codex = get_adapter("codex")
    store = store_dir()
    writes = ["codex", "--sandbox", "workspace-write"]
    refused = [
        (writes, project),
        (["codex", "-c", 'sandbox_mode="workspace-write"'], project),
        (["codex", "--approve-for-me", *codex.store_access("workspace", store)], project),
        ([*writes, "-C", str(project)], elsewhere),
        ([*writes, "--cd", "../proj"], elsewhere),
        ([*writes, "--add-dir", str(project)], elsewhere),
        ([*writes, "--add-dir", "../proj"], elsewhere),  # as the launch's directory reads it
        ([*writes, "-c", f"sandbox_workspace_write.writable_roots=[{json.dumps(str(project))}]"],
         elsewhere),
    ]  # fmt: skip
    for argv, cwd in refused:
        with pytest.raises(store_broker.LaunchRefused):
            store_broker.refuse_inside(argv, cwd, codex)
    allowed = [
        (writes, elsewhere),  # given none of the project
        (["codex", *codex.store_access("read_only", store)], project),
        (["codex", "--sandbox", "read-only"], project),
        (["codex", "--sandbox", "danger-full-access"], project),  # unsandboxed: as its user
    ]
    for argv, cwd in allowed:
        store_broker.refuse_inside(argv, cwd, codex)


def test_the_install_check_compares_paths_as_the_kernel_resolves_them(
    beyond_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace named through a link (``/tmp`` is ``/private/tmp`` on macOS) held an
    install the resolved comparison saw outside it."""
    real = beyond_tmp / "real"
    (real / ".venv").mkdir(parents=True)
    link = beyond_tmp / "link"
    link.symlink_to(real)
    _installed_at(monkeypatch, real / ".venv")
    with pytest.raises(store_broker.LaunchRefused):
        store_broker.refuse_inside(
            ["codex", "--sandbox", "workspace-write"], link, get_adapter("codex")
        )


def test_the_install_check_reads_the_interpreters_whole_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What runs at the start of the broker's interpreter and of a hook's: the installation
    the interpreter comes from, the user site it loads (its ``.pth`` files), and a hook
    program's own environment, not the program alone."""
    import site

    from aisquare.core import agents

    monkeypatch.setattr(store_broker, "install_paths", REAL_INSTALL_PATHS)
    user_site = tmp_path / "home" / ".local" / "lib" / "site-packages"
    monkeypatch.setattr(site, "ENABLE_USER_SITE", True)
    monkeypatch.setattr(sys, "base_prefix", sys.prefix)  # an interpreter that loads it
    monkeypatch.setattr(site, "getusersitepackages", lambda: str(user_site))
    environment = tmp_path / "hooks-venv"
    program = environment / "bin" / "aisquare"
    program.parent.mkdir(parents=True)
    program.write_text("#!/bin/sh\n", encoding="utf-8")
    command = f"{program} hook codex"
    monkeypatch.setattr(agents, "hook_commands", lambda agent, config_dir=None: [command])
    found = store_broker.install_paths(tmp_path / "codex-home", "codex")
    assert (Path(sys.base_prefix) / "lib").resolve() in found
    assert user_site.resolve() in found
    assert (environment / "lib").resolve() in found and program.resolve() in found
    assert environment.resolve() not in found, "by its parts, not its prefix (GATE #113)"


def test_the_install_check_reads_the_install_the_agents_hooks_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r12 of #113, finding 1: the check read only the launcher's install, so hooks
    connected earlier from ``<repo>/.venv`` kept running it unsandboxed after the launcher
    moved to an install elsewhere, and the doctor said nothing."""
    from aisquare.core import agents
    from aisquare.services import diagnostics

    monkeypatch.setattr(store_broker, "install_paths", REAL_INSTALL_PATHS)
    project = tmp_path / "proj"
    in_tree = project / ".venv" / "bin" / "aisquare"
    in_tree.parent.mkdir(parents=True)
    in_tree.write_text("#!/bin/sh\n", encoding="utf-8")
    _a_codex_project(project, tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "prefix", "/opt/aisquare-elsewhere")
    monkeypatch.setattr(sys, "executable", "/opt/aisquare-elsewhere/bin/python")
    monkeypatch.setattr(agents, "current_install", lambda: Path("/opt/aisquare-elsewhere/bin/x"))
    command = f"{in_tree} hook codex"
    monkeypatch.setattr(agents, "hook_commands", lambda agent, config_dir=None: [command])
    assert in_tree.resolve() in store_broker.install_paths(tmp_path / "codex-home", "codex")
    with pytest.raises(store_broker.LaunchRefused, match=r"\.venv"):
        store_broker.refuse_inside(
            ["codex", "--sandbox", "workspace-write"], project, get_adapter("codex")
        )
    row = diagnostics._sandboxed_install_check(project)
    assert row is not None and ".venv" in row.detail


def test_a_launch_from_above_the_project_is_told_to_launch_from_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r12 of #113, finding 14: a launch from the home, whose tree holds every install
    under it, was refused with advice that cannot help there."""
    home = tmp_path / "home"
    install = home / "tools" / "venv"
    install.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    _installed_at(monkeypatch, install)
    with pytest.raises(store_broker.LaunchRefused, match="from its project's directory"):
        store_broker.refuse_inside(
            ["codex", "--sandbox", "workspace-write"], home, get_adapter("codex")
        )


def test_the_socket_dirs_never_name_a_shared_temporary_directory(tmp_path: Path) -> None:
    """Review r11 of #113, finding 6: under ``tmux -S /tmp/x`` the parent of ``$TMUX``'s
    socket is ``/tmp`` itself, and held read-only every write of the session there failed."""
    if sys.platform == "win32":
        pytest.skip("POSIX's")
    dirs = store_broker._socket_dirs({"TMUX": "/tmp/x,1,0", "TMPDIR": str(tmp_path)}, "asq")
    assert Path("/tmp").resolve() not in dirs and Path("/").resolve() not in dirs
    assert Path(f"/tmp/tmux-{os.getuid()}").resolve() in dirs


def test_a_command_is_not_started_once_the_agent_has_exited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r11 of #113, finding 9: the exit was looked at once, before the vetting, and a
    command past that look started after ``close`` took its list: nothing ended it."""
    broker = _broker(tmp_path)
    started: list[object] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: started.append(a))
    broker._closing.set()
    answer = broker._run(["task", "claim", "t"], str(tmp_path), os.environ)
    assert answer["code"] == 75 and started == []


def test_a_brokered_command_refuses_to_run_outside_the_launchs_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r11 of #113, finding 11: the broker checked the request's directory, resolved,
    and started the command in it as spelt: a directory the sandbox swapped for a link in
    between ran it elsewhere. The command asks where it really is."""
    tree, elsewhere = tmp_path / "tree", tmp_path / "elsewhere"
    tree.mkdir()
    elsewhere.mkdir()
    environ = {store_broker.CALLER_ENV: "1", store_broker.ROOTS_ENV: str(tree.resolve())}
    monkeypatch.chdir(tree)
    assert store_broker.check_cwd(environ) is None
    monkeypatch.chdir(elsewhere)
    assert "outside this agent's tree" in str(store_broker.check_cwd(environ))
    assert store_broker.check_cwd({store_broker.ROOTS_ENV: str(tree)}) is None, "brokered only"


def test_every_refusal_of_a_brokered_call_leaves_its_record(
    work: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """Review r11 of #113, finding 13: the pool refusal, the fleet-signal refusal and a ref
    to another board's task in ``note --task`` or ``task add --needs`` left no log line and
    no note, though the docs said every refusal does."""
    other = _other_repo(tmp_path)
    theirs, _ = team_service.add_task("their task", cwd=other)
    _brokered(monkeypatch, work)
    for argv in (
        ["remember", "everyone", "--user"],
        ["team", "signal", "fleet-paused", "off", "--as", MINE[:8]],
        ["note", "x", "--task", theirs.id, "--as", MINE[:8]],
        ["task", "add", "y", "--needs", theirs.id, "--as", MINE[:8]],
    ):
        result = runner.invoke(app, argv)
        assert result.exit_code == 1, (argv, result.output)
    log = (paths.log_dir() / store_broker.LOG_NAME).read_text(encoding="utf-8")
    assert log.count(" pool: ") == 1 and log.count(" fleet: ") == 1
    assert log.count(" board: ") == 2


def test_the_sandbox_probe_runs_once_per_process_and_a_full_disk_is_no_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Review r11 of #113, finding 14: the probe made a file in the home at every start that
    names a broker, every Codex hook's, and read a full disk as a sandbox, so a hook outside
    one did nothing."""
    import errno as errno_module
    import tempfile

    store_dir().mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(store_broker.DIR_ENV, str(tmp_path))
    monkeypatch.setattr(store_broker, "_WRITABLE", {})
    asked: list[Path] = []
    real = store_broker._writable

    def counted(home: Path) -> bool:
        asked.append(home)
        return real(home)

    monkeypatch.setattr(store_broker, "_writable", counted)
    assert store_broker.brokered() is None and store_broker.brokered() is None
    assert len(asked) == 1

    def full(**kwargs: object) -> tuple[int, str]:
        raise OSError(errno_module.ENOSPC, "No space left on device")

    monkeypatch.setattr(tempfile, "mkstemp", full)
    with caplog.at_level("WARNING", logger="aisquare.services.store_broker"):
        assert real(store_dir()) is True
    assert any("full" in record.message for record in caplog.records)


# --- review r12 of #113 -----------------------------------------------------------------


def test_a_brokered_commands_own_child_carries_no_brokered_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r12 of #113, finding 5: the ``team distill`` a brokered note starts kept the
    brokered scope, so its own cwd check refused it (the board's directory is outside the
    launch's tree under a hub), and a false refusal was noted under no launch id."""

    monkeypatch.setenv(store_broker.CALLER_ENV, "1")
    monkeypatch.setenv(store_broker.BOARD_ENV, "prj_x")
    monkeypatch.setenv(store_broker.ROOTS_ENV, "/somewhere")
    started: list[dict[str, str]] = []
    monkeypatch.setattr(brain, "brain_enabled", lambda: True)
    monkeypatch.setattr(shutil, "which", lambda name: "/bin/gbrain")
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kwargs: started.append(kwargs["env"]))
    REAL_SPAWN_DRAIN(root=tmp_path / "hub")
    assert started and not any(key.startswith("AISQUARE_BROKERED") for key in started[0])


def test_a_refusal_with_no_launch_to_name_is_logged_and_never_noted(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = _brokered(monkeypatch, work)
    monkeypatch.delenv("AISQUARE_LAUNCH_ID")
    store_broker.record_refusal("board", "no launch")
    assert "launch=? " in (paths.log_dir() / store_broker.LOG_NAME).read_text(encoding="utf-8")
    with store_session() as store:
        assert not [e for e in store.recent_events(board, limit=50) if "refused" in e.text]


def test_a_refusal_note_that_fails_leaves_no_mark(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r12 of #113, finding 10: the once-only mark committed before the note, so a
    note that failed left it, and that launch's refusals of that kind were never noted."""
    board = _brokered(monkeypatch, work)
    real = team_service._emit

    def failing(*args: object, **kwargs: object) -> object:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(team_service, "_emit", failing)
    with pytest.raises(sqlite3.OperationalError):
        team_service.note_refusal(board, "first", key="broker-refused:l:identity")
    monkeypatch.setattr(team_service, "_emit", real)
    assert team_service.note_refusal(board, "second", key="broker-refused:l:identity")


def test_every_refusal_the_broker_makes_is_logged(work: Path, tmp_path: Path) -> None:
    """Review r12 of #113, finding 11: a replay, an unparsable or wrong-version request, an
    argv that is not strings, a NUL, a bad cwd, a request that is not a file and one after
    the agent exited were refused with no record, though the docs said every refusal has
    one."""
    broker = _broker(tmp_path)
    signed = _request(["board"], work)
    _answer(broker, signed)
    _answer(broker, signed)  # replayed
    broker.answer(b"not json", RID)
    broker.answer(b'{"v": 2}', RID)
    not_strings: list[str] = json.loads('["board", 1]')
    broker.answer(_request(not_strings, work, rid="3" * 32), "3" * 32)
    broker.answer(_request(["note", "a\x00b"], work, rid="4" * 32), "4" * 32)
    broker.answer(_request(["board"], work / "missing", rid="5" * 32), "5" * 32)
    (tmp_path / "broker-dir" / store_broker.OUTBOX).mkdir(parents=True)
    broker._handle("6" * 32, "the request is not a file")
    broker._closing.set()
    broker.answer(_request(["board"], work, rid="7" * 32), "7" * 32)
    lines = (paths.log_dir() / store_broker.LOG_NAME).read_text(encoding="utf-8").splitlines()
    kinds = [line.split()[3] for line in lines]
    assert kinds.count("replay:") == 1 and kinds.count("request:") == 6, kinds
    assert kinds.count("closed:") == 1, kinds


def test_a_brokered_read_follows_the_launchs_project_past_a_pin(
    work: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r12 of #113, finding 12: brokered writes went to the launch's project's pool,
    brokered reads (``inject``, ``context list``/``search``/``preview``) to the pinned one."""
    from aisquare.core.workspace import current_project, pin_project
    from aisquare.services import context as context_service

    other = _other_repo(tmp_path)
    _brokered(monkeypatch, work)
    context_service.add_entry("ours alone", None, [])
    pin_project(current_project(other).id)
    assert "ours alone" in [entry.text for entry in context_service.list_entries()]
    assert "ours alone" in [entry.text for entry in context_service.search_entries("ours")]
    assert "ours alone" in context_service.preview()


def test_a_non_git_projects_root_is_in_a_hand_launchs_tree(tmp_path: Path) -> None:
    """Review r12 of #113, finding 13: a non-git project (an ``.aisquare`` marker) launched
    from a subdirectory had only that subdirectory in its tree."""
    root = tmp_path / "marked"
    (root / ".aisquare").mkdir(parents=True)
    sub = root / "sub"
    sub.mkdir()
    scope = store_broker.launch_scope(sub, {})
    assert scope.holds(root) and scope.holds(sub)


def test_a_socket_made_straight_in_tmp_is_itself_held(tmp_path: Path) -> None:
    """Review r12 of #113, finding 4: with ``/tmp`` itself never named, a socket made there
    (``tmux -S /tmp/x``) had no protection: the socket file is named instead."""
    if sys.platform == "win32":
        pytest.skip("POSIX's")
    dirs = store_broker._socket_dirs({"TMUX": "/tmp/asq-x,1,0"}, "asq")
    assert Path("/tmp/asq-x").resolve() in dirs and Path("/tmp").resolve() not in dirs


def test_a_read_only_seat_in_its_own_worktree_may_write_none_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fleet reviewer runs in its own worktree under ``:read-only``; the launch named that
    worktree writable within the read-only worktree directory whatever the seat, so the
    reviewer's sandbox wrote all of it. Only a seat that writes its workspace has it."""
    import tomllib

    if sys.platform == "win32":
        pytest.skip("POSIX's")
    root = tmp_path / "proj"
    root.mkdir()
    git = ["git", "-C", str(root), "-c", "user.email=a@b", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    worktree = root / ".aisquare-worktrees" / "reviewer-1"
    subprocess.run([*git, "worktree", "add", "-q", "-b", "r1", str(worktree)], check=True)
    monkeypatch.chdir(worktree)

    def fails(directory: Path, env: object) -> None:
        raise RuntimeError("not started")

    monkeypatch.setattr(store_broker, "run_broker", fails)
    codex = get_adapter("codex")

    def table(write: WriteScope) -> dict[str, str]:
        argv = ["codex", *codex.store_access(write, store_dir())]
        launched, _ = store_broker.start_for_launch({"AISQUARE_LAUNCH_ID": LAUNCH}, codex, argv)
        written: dict[str, str] = tomllib.loads(f"t = {launched[-1].partition('=')[2]}")["t"]
        return written

    own = str(worktree.resolve())
    assert table("workspace").get(own) == "write"
    read_only = table("read_only")
    assert own not in read_only
    assert read_only.get(str((root / ".aisquare-worktrees").resolve())) == "read"


def test_a_tree_codex_is_given_holds_its_launch_inputs_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r12 of #113, finding 7: the launch inputs were held at the launcher's own
    directory's trees, while ``-C`` and ``--add-dir`` passed through unexamined: the tree
    Codex was given had its ``.claude/``, ``.mcp.json`` … writable."""
    import tomllib

    from aisquare.core.agent_adapters.codex import PERMISSION_PROFILE

    launch, workspace, added = (tmp_path / name for name in ("launch", "workspace", "added"))
    for directory in (launch, workspace, added):
        directory.mkdir()
    monkeypatch.chdir(launch)

    def fails(directory: Path, env: object) -> None:
        raise RuntimeError("not started")

    monkeypatch.setattr(store_broker, "run_broker", fails)
    codex = get_adapter("codex")
    argv = [
        "codex",
        *codex.store_access("workspace", store_dir()),
        "-C",
        str(workspace),
        "--add-dir",
        str(added),
    ]
    launched, _ = store_broker.start_for_launch({"AISQUARE_LAUNCH_ID": LAUNCH}, codex, argv)
    key = f"permissions.{PERMISSION_PROFILE}.filesystem="
    value = next(arg for arg in launched if arg.startswith(key)).partition("=")[2]
    table: dict[str, str] = tomllib.loads(f"t = {value}")["t"]
    for tree in (launch, workspace, added):
        for name in store_broker.launch_inputs():
            assert table.get(str(tree.resolve() / name)) == "read", (tree, name)


# --- review r13 of #113 -----------------------------------------------------------------


def test_the_agent_keeps_its_pythonpath_and_a_launch_warns_of_a_path_its_sandbox_writes(
    beyond_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r14 of #113, finding 1 (the manager's decision): r13 dropped a ``PYTHONPATH``
    entry the sandbox may write from the environment Codex is exec'd with, so the agent's
    own ``pytest`` in a direnv project (``PYTHONPATH=$PWD/src``) failed. The agent keeps its
    environment whole; aisquare's own interpreters ignore the variable (``-E``, below).
    A ``PATH`` entry the sandbox may write is warned of, an empty one too, which is the
    directory a process runs in (finding 3); ``PYTHONPATH`` is warned of only where the
    agent's hooks, connected by an older aisquare, would read it."""
    from aisquare.core.agent_adapters.types import SandboxWrites

    project = beyond_tmp / "proj"
    (project / "src").mkdir(parents=True)
    writes = SandboxWrites((project,), workspace=project)
    env = {
        "PYTHONPATH": f"{project / 'src'}{os.pathsep}/opt/libs",
        "PATH": f"{project / '.venv' / 'bin'}{os.pathsep}/usr/bin",
    }
    kept = dict(env)
    said = store_broker.sandbox_environment(env, writes, project, True)
    assert env == kept, "nothing changed in the agent's environment"
    assert [line for line in said if line.startswith("PATH names")], said
    assert not [line for line in said if "PYTHONPATH" in line], said
    older = store_broker.sandbox_environment(env, writes, project, False)
    assert [line for line in older if line.startswith("PYTHONPATH names")], older
    empty = store_broker.sandbox_environment(
        {"PATH": f"/usr/bin{os.pathsep}"}, writes, project, True
    )
    assert [line for line in empty if "an empty entry" in line], empty
    relative = store_broker.sandbox_environment({"PATH": "bin"}, writes, project, True)
    assert [line for line in relative if "bin" in line], relative
    assert store_broker.sandbox_environment(dict(env), None, project, True) == [], "unsandboxed"

    # Through the launch itself: the environment it execs the agent with is the one given.
    monkeypatch.chdir(project)
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.setattr(store_broker, "run_broker", lambda directory, env: None)
    launched = {"AISQUARE_LAUNCH_ID": LAUNCH, "PYTHONPATH": str(project / "src")}
    store_broker.start_for_launch(launched, get_adapter("codex"), ["codex", *_profile()])
    assert launched["PYTHONPATH"] == str(project / "src")


def test_aisquares_own_interpreters_ignore_a_pythonpath_into_the_tree(tmp_path: Path) -> None:
    """Review r14 of #113, findings 1, 3 and 4, measured: a ``sitecustomize`` on
    ``PYTHONPATH``, or in the directory an empty entry names (``-P`` keeps the script's
    directory off ``sys.path``, not an empty entry's), runs in every interpreter that reads
    the variable. The ones aisquare starts for a sandboxed agent and the fleet's launches
    (``selfcli.isolated_argv_for``: ``-E``) never run it."""
    tree = tmp_path / "tree"
    tree.mkdir()
    marker = tmp_path / "ran"
    (tree / "sitecustomize.py").write_text(
        f"open({str(marker)!r}, 'a').write('x')\n", encoding="utf-8"
    )
    base = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}

    def runs(argv: list[str], pythonpath: str) -> bool:
        if marker.exists():
            marker.unlink()
        subprocess.run(
            argv, cwd=tree, env={**base, "PYTHONPATH": pythonpath}, capture_output=True,
            timeout=120, check=False,
        )  # fmt: skip
        return marker.exists()

    for pythonpath in (str(tree), f"{os.pathsep}/nonexistent"):
        assert runs(selfcli.argv_for(["--version"]), pythonpath), "the control reads it"
        assert not runs(selfcli.isolated_argv_for(["--version"]), pythonpath), pythonpath


def test_every_interpreter_aisquare_starts_for_a_sandboxed_agent_is_isolated(
    work: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r14 of #113, findings 1 and 4: the broker's commands, the broker itself, the
    distiller and the hand-over run with ``-E``; so do a Codex agent's hooks as this version
    connects them, which :func:`agents.hook_runs_isolated` reads; Claude Code's keep the
    console script its own tools' environment runs."""
    from aisquare.core import agents

    started: list[list[str]] = []

    class Child:
        stdout = stderr = None
        returncode = 0

        def __init__(self, argv: list[str], **kwargs: object) -> None:
            started.append(list(argv))
            raise OSError("not started")

    monkeypatch.setattr(subprocess, "Popen", Child)
    broker = _broker(tmp_path)
    broker.answer(_request(["board"], work), RID)
    with pytest.raises(OSError):
        REAL_RUN_BROKER(tmp_path / "b", {})
    monkeypatch.setattr(brain, "brain_enabled", lambda: True)
    monkeypatch.setattr(shutil, "which", lambda name: "/bin/gbrain")
    REAL_SPAWN_DRAIN(root=tmp_path)
    ours = [argv for argv in started if "-m" in argv]
    assert len(ours) == 3 and all(argv[1:3] == ["-E", "-P"] for argv in ours), started
    # The hand-over the agent's own hook starts (its argv is built inline there).
    source = (ROOT / "src" / "aisquare" / "services" / "hooks.py").read_text(encoding="utf-8")
    assert re.search(r"isolated_argv_for\(\s*\[\s*\"--quiet\", \"hook\", \"hand-over\"", source)

    home = tmp_path / "codex-home"
    home.mkdir()
    agents.install_hooks("codex", home)
    commands = agents.hook_commands("codex", home)
    assert commands and all(agents.hook_runs_isolated(command) for command in commands)
    assert all(" -E -P -m aisquare hook codex" in command for command in commands)
    assert not agents.hook_runs_isolated("/usr/local/bin/aisquare hook codex")


def test_the_doctors_sandbox_rows_look_only_in_a_real_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r13 of #113, finding 2: from the home (or ``/``), with no marker there, the
    doctor's project was that directory itself, so every install under the home, the
    official uv-tool one included, read as in the project, on Claude-only machines too,
    and the installer, which runs the doctor where it is run, exited 2. The rows look only
    in a real project (a git root or a project marker, never the home or ``/``), only where
    a Codex is installed, and their names are one word."""
    from aisquare.services import diagnostics

    home = tmp_path / "home"
    install = home / ".local" / "share" / "uv" / "tools" / "aisquare-cli"
    install.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    _installed_at(monkeypatch, install)
    venv_bin = home / "proj" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    _a_codex_project(home / "proj", tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", f"{venv_bin}{os.pathsep}{os.environ['PATH']}")
    for place in (home, Path("/"), home / "scratch"):
        place.mkdir(exist_ok=True)
        assert diagnostics._sandboxed_install_check(place) is None, place
        assert diagnostics._sandbox_path_check(place) is None, place
    row = diagnostics._sandbox_path_check(home / "proj")
    assert row is not None and row.name == "sandbox-path" and ".venv" in row.detail
    _installed_at(monkeypatch, home / "proj" / ".venv")
    row = diagnostics._sandboxed_install_check(home / "proj")
    assert row is not None and row.name == "sandbox-install"
    # No Codex installed, no sandbox to keep anything from: nothing to say.
    monkeypatch.setenv("PATH", "/nonexistent")
    assert diagnostics._sandboxed_install_check(home / "proj") is None
    assert diagnostics._sandbox_path_check(home / "proj") is None


def test_the_doctor_run_from_above_the_real_install_says_nothing_of_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review r13 of #113, finding 2, with the real install: the doctor run from a directory
    above it that is no project (as from a home above a uv-tool install) warned of it."""
    import aisquare
    from aisquare.core.workspace import git_common_root, marks_project_root
    from aisquare.services import diagnostics

    monkeypatch.setattr(store_broker, "install_paths", REAL_INSTALL_PATHS)
    package = Path(aisquare.__file__).resolve().parent
    above = next(
        (
            place
            for place in package.parents
            if git_common_root(place) is None
            and not any(marks_project_root(d) for d in (place, *place.parents))
            and place != Path(place.anchor)
        ),
        None,
    )
    if above is None:
        pytest.skip("every directory above this install is in a project")
    monkeypatch.setenv("HOME", str(above))
    codex_bin = Path(tempfile.mkdtemp(prefix="asq-codex-"))
    try:
        (codex_bin / "codex").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (codex_bin / "codex").chmod(0o755)
        monkeypatch.setenv("PATH", f"{codex_bin}{os.pathsep}{os.environ.get('PATH', '')}")
        assert diagnostics._sandboxed_install_check(above) is None
    finally:
        shutil.rmtree(codex_bin, ignore_errors=True)


def test_the_plugin_routes_launcher_is_an_install_the_hooks_run(
    beyond_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r13 of #113, finding 3: on the plugin route a hook runs the ``aisquare`` on
    Codex's ``PATH``, else ``~/.local/bin``'s, else ``uvx``'s, and none was checked: with a
    project venv's first on ``PATH``, every hook ran the in-tree install."""
    from aisquare.core import agents

    monkeypatch.setattr(store_broker, "install_paths", REAL_INSTALL_PATHS)
    project = beyond_tmp / "proj"
    venv_bin = project / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "aisquare").write_text("#!/bin/sh\n", encoding="utf-8")
    (venv_bin / "aisquare").chmod(0o755)
    monkeypatch.setattr(agents, "hook_commands", lambda agent, config_dir=None: [])

    class Installed:
        def installed(self, config_dir: Path | None) -> object:
            from aisquare.core.agent_adapters.types import PluginInstall

            return PluginInstall(beyond_tmp / "codex-home", "0.8.0")

    monkeypatch.setattr(agents, "plugin_of", lambda agent: Installed())
    found = store_broker.install_paths(beyond_tmp / "codex-home", "codex", str(venv_bin))
    assert (venv_bin / "aisquare").resolve() in found
    assert (project / ".venv" / "lib").resolve() in found
    monkeypatch.delenv("TMPDIR", raising=False)
    with pytest.raises(store_broker.LaunchRefused, match=r"\.venv"):
        store_broker.refuse_inside(
            ["codex", "--sandbox", "workspace-write"],
            project,
            get_adapter("codex"),
            beyond_tmp / "codex-home",
            str(venv_bin),
        )
    monkeypatch.setattr(agents, "plugin_of", lambda agent: None)
    found = store_broker.install_paths(beyond_tmp / "codex-home", "codex", str(venv_bin))
    assert (venv_bin / "aisquare").resolve() not in found, "no plugin: its launcher runs nothing"


def test_a_module_form_hooks_venv_is_an_install_the_hooks_run(
    beyond_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r13 of #113, finding 8: a module-form hook (``<proj>/.venv/bin/python -P -m
    aisquare hook codex``) names the venv's ``python``, a link to the base interpreter;
    resolved, its environment was the base one, and the venv, which ``-m aisquare`` loads
    from, was never checked."""
    from aisquare.core import agents

    monkeypatch.setattr(store_broker, "install_paths", REAL_INSTALL_PATHS)
    venv = beyond_tmp / "proj" / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(Path(sys.executable).resolve())
    command = f"{venv / 'bin' / 'python'} -P -m aisquare hook codex"
    monkeypatch.setattr(agents, "hook_commands", lambda agent, config_dir=None: [command])
    hook = agents.hook_binary(command)
    assert hook is not None and hook.module_form
    found = store_broker.install_paths(beyond_tmp / "codex-home", "codex")
    assert (venv / "lib").resolve() in found


def test_codex_reads_its_sandbox_from_its_config_and_profile_too(beyond_tmp: Path) -> None:
    """Review r13 of #113, finding 6: the sandbox was read off the arguments alone, while
    Codex layers ``config.toml``, the ``-p`` profile and ``-c``: a ``writable_roots`` there
    (``~/.local/share/uv``, the uv-tool install's) went unchecked, and a ``danger-full-access``
    there was refused as workspace-write. The inline-table spelling of
    ``sandbox_workspace_write`` was missed as well."""
    codex = get_adapter("codex")
    home, cwd = beyond_tmp / "codex-home", beyond_tmp / "w"
    home.mkdir()
    cwd.mkdir()
    uv = beyond_tmp / "uv"
    (home / "config.toml").write_text(
        f"[sandbox_workspace_write]\nwritable_roots = [{json.dumps(str(uv))}]\n", encoding="utf-8"
    )
    writes = codex.sandbox_writes([], cwd, home)
    assert writes is not None and writes.may_write(uv / "tools")
    (home / "config.toml").write_text('sandbox_mode = "danger-full-access"\n', encoding="utf-8")
    assert codex.sandbox_writes([], cwd, home) is None
    assert codex.sandbox_writes(["--sandbox", "workspace-write"], cwd, home) is not None
    store = store_dir()
    assert codex.sandbox_writes(codex.store_access("workspace", store), cwd, home) is not None, (
        "the store's profile holds over the file's sandbox_mode (measured on 0.162.1)"
    )
    (home / "config.toml").write_text("", encoding="utf-8")
    (home / "loose.config.toml").write_text('sandbox_mode = "danger-full-access"\n', "utf-8")
    assert codex.sandbox_writes(["-p", "loose"], cwd, home) is None
    inline = f"sandbox_workspace_write={{writable_roots=[{json.dumps(str(uv))}]}}"
    writes = codex.sandbox_writes(["-c", inline], cwd, home)
    assert writes is not None and writes.may_write(uv)


def test_a_refusal_names_its_paths_as_they_are_and_its_remedy_by_the_root_that_holds_it(
    beyond_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r13 of #113, findings 9 and 10: ``str.capitalize`` lowered every letter of the
    remedy but its first, the path it names with them (``/home/u/code/x`` for
    ``/home/u/Code/X``), and the advice to launch from the project's directory was given
    for an install the launch reached another way (``--add-dir``, a temporary directory),
    where the user was in the project already and it could not help."""
    monkeypatch.delenv("TMPDIR", raising=False)
    codex = get_adapter("codex")
    writes = ["codex", "--sandbox", "workspace-write"]
    project = beyond_tmp / "Code" / "Proj"
    (project / ".aisquare").mkdir(parents=True)
    added = beyond_tmp / "Shared" / "Tools"
    added.mkdir(parents=True)
    _installed_at(monkeypatch, added / "venv")
    with pytest.raises(store_broker.LaunchRefused) as refusal:
        store_broker.refuse_inside([*writes, "--add-dir", str(added)], project, codex)
    said = str(refusal.value)
    assert str(added.resolve()) in said and "Drop " in said, said
    assert "launch the agent from its project's directory" not in said
    _installed_at(monkeypatch, project / ".venv")
    with pytest.raises(store_broker.LaunchRefused) as refusal:
        store_broker.refuse_inside(writes, project, codex)
    assert "Install aisquare outside the project" in str(refusal.value)
    above = beyond_tmp / "Code"
    with pytest.raises(store_broker.LaunchRefused) as refusal:
        store_broker.refuse_inside(writes, above, codex)
    assert f"not {above.resolve()}," in str(refusal.value), "the path as it is spelled"
    with pytest.raises(store_broker.LaunchRefused) as refusal:
        _installed_at(monkeypatch, Path("/tmp/asq-tools-x"))
        store_broker.refuse_inside(writes, project, codex)
    assert "outside /tmp" in str(refusal.value), str(refusal.value)


def test_a_brokered_context_show_reads_only_the_pools_the_agent_is_given(
    work: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """Review r13 of #113, finding 4: ``context show`` took any entry's id or prefix, of any
    project's pool, from a sandboxed agent's broker."""
    from aisquare.core.workspace import current_project
    from aisquare.services import context as context_service

    other = _other_repo(tmp_path)
    monkeypatch.chdir(other)
    theirs = context_service.add_entry("another project's secret", "project", [])
    mine_user = context_service.add_entry("for every project", "user", [])
    monkeypatch.chdir(work)
    _brokered(monkeypatch, work)
    ours = context_service.add_entry("ours", None, [])
    assert current_project(work).id == ours.project_id
    assert context_service.show_entry(ours.id).text == "ours"
    assert context_service.show_entry(mine_user.id).text == "for every project"
    with pytest.raises(store_broker.OutOfScope):
        context_service.show_entry(theirs.id[:-3])
    shown = runner.invoke(app, ["context", "show", theirs.id])
    assert shown.exit_code != 0 and "secret" not in shown.output
    assert "pool:" in (paths.log_dir() / store_broker.LOG_NAME).read_text(encoding="utf-8")


def test_brokered_reads_take_the_launchs_pool_not_a_directorys_or_a_pin(
    work: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r13 of #113, finding 5: ``aisquare log`` still followed the ``project switch``
    pin, and the pool reads took the project of the request's directory, which in the tree
    may be another's (a nested marker), where the same write was refused."""
    from aisquare.core.store import store_session as session
    from aisquare.core.workspace import current_project, pin_project
    from aisquare.services import context as context_service
    from aisquare.services import diagnostics

    other = _other_repo(tmp_path)
    _brokered(monkeypatch, work)
    ours = current_project(work)
    with session() as store:
        store.add_prompt("a prompt of ours", ours.id)
        store.add_prompt("a prompt of theirs", current_project(other).id)
    context_service.add_entry("ours alone", None, [])
    pin_project(current_project(other).id)
    assert [record.text for record in diagnostics.show_log()] == ["a prompt of ours"]
    nested = work / "vendored"
    (nested / ".aisquare").mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert "ours alone" in [entry.text for entry in context_service.list_entries()]


# --- review r14 of #113 -----------------------------------------------------------------


def test_the_plugins_uvx_cache_counts_only_where_its_launcher_would_run_uvx(
    beyond_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r14 of #113, finding 2: with the plugin installed, uv's cache and
    ``~/.cache/aisquare`` always counted as what the hooks run, so ``writable_roots =
    ["~/.cache"]`` refused every workspace launch, naming an install that was not there.
    The launcher runs ``uvx`` only with no ``aisquare`` on ``PATH`` or in ``~/.local/bin``,
    runs nothing for an untrusted plugin (unless the launch bypasses hook trust: r15),
    stands down beside a hook of ours that can start (r15), and finds uv's cache under
    ``$XDG_CACHE_HOME``."""
    from aisquare.core import agents
    from aisquare.core.agent_adapters.types import PluginInstall

    monkeypatch.setattr(store_broker, "install_paths", REAL_INSTALL_PATHS)
    home = beyond_tmp / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(agents, "_home", lambda: home)
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(beyond_tmp / "no-config"))
    cache = beyond_tmp / "xdg-cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setattr(agents, "hook_commands", lambda agent, config_dir=None: [])
    _executable(home / ".cargo" / "bin" / "uvx")  # the launcher's uvx, off PATH
    trusted = PluginInstall(home / ".codex", "0.8.0")

    class Plugin:
        def __init__(self, install: PluginInstall | None) -> None:
            self.install = install

        def installed(self, config_dir: Path | None) -> PluginInstall | None:
            return self.install

    monkeypatch.setattr(agents, "plugin_of", lambda agent: Plugin(trusted))
    nothing = str(beyond_tmp / "empty-bin")
    found = store_broker.install_paths(home / ".codex", "codex", nothing)
    assert (cache / "uv").resolve() in found and (cache / "aisquare").resolve() in found
    assert (home / ".cache" / "uv").resolve() not in found, "XDG_CACHE_HOME is uv's"
    # An aisquare in ~/.local/bin is what it runs: uv's cache is not read.
    local = home / ".local" / "bin" / "aisquare"
    local.write_text("#!/bin/sh\n", encoding="utf-8")
    local.chmod(0o755)
    found = store_broker.install_paths(home / ".codex", "codex", nothing)
    assert local.resolve() in found and (cache / "uv").resolve() not in found
    local.unlink()
    for plugin, hooks_file in (
        (Plugin(None), False),
        (Plugin(replace(trusted, trusted=False)), False),
        (Plugin(trusted), True),
    ):
        monkeypatch.setattr(agents, "plugin_of", lambda agent, plugin=plugin: plugin)
        monkeypatch.setattr(
            agents, "plugin_stands_down", lambda agent, config_dir=None, down=hooks_file: down
        )
        found = store_broker.install_paths(home / ".codex", "codex", nothing)
        assert (cache / "uv").resolve() not in found, (plugin.install, hooks_file)


def test_an_unexpandable_home_in_a_setting_neither_crashes_nor_launches_unprotected(
    beyond_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r14 of #113, finding 7: ``Path.expanduser`` raises on ``~nosuchuser/...``,
    in ``writable_roots`` or ``PATH``, and the launch's catch-all then started the agent
    with the arguments it had before its store profile: no store read-only, no launch
    inputs held, no install check. Such a path is read as written, and any other failure
    to work out a sandboxed launch refuses it."""
    from aisquare.core.agent_adapters.types import SandboxWrites
    from aisquare.services import agent_launch

    codex = get_adapter("codex")
    roots = ["-c", 'sandbox_workspace_write.writable_roots=["~nosuchuser-asq/x"]']
    writes = codex.sandbox_writes(roots, beyond_tmp)
    assert writes is not None
    said = store_broker.sandbox_environment(
        {"PATH": "~nosuchuser-asq/bin"}, SandboxWrites((beyond_tmp,), workspace=beyond_tmp),
        beyond_tmp, True,
    )  # fmt: skip
    assert said and "~nosuchuser-asq/bin" in said[0], "a literal relative entry: in the workspace"

    def broken(env: object, adapter: object, argv: object) -> object:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(store_broker, "start_for_launch", broken)
    with pytest.raises(store_broker.LaunchRefused, match="not started without"):
        agent_launch.start_broker(agent_launch.resolve(agent="codex"), {}, ["codex"])


def test_a_worktree_outside_its_repository_is_its_own_tree_for_the_doctor_and_the_remedy(
    beyond_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r14 of #113, finding 8: a linked worktree outside its principal repository
    (``../feature``) resolved to the principal, so the doctor showed no sandbox row there
    while a launch from there warned or was refused, and the remedy said to launch from the
    project's directory."""
    from aisquare.services import diagnostics

    monkeypatch.delenv("TMPDIR", raising=False)
    principal = beyond_tmp / "repo"
    principal.mkdir()
    git = ["git", "-C", str(principal), "-c", "user.email=a@b", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    feature = beyond_tmp / "feature"
    subprocess.run([*git, "worktree", "add", "-q", "-b", "f", str(feature)], check=True)
    venv_bin = feature / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    _a_codex_project(feature, beyond_tmp, monkeypatch)
    monkeypatch.setenv("PATH", f"{venv_bin}{os.pathsep}{os.environ['PATH']}")
    row = diagnostics._sandbox_path_check(feature)
    assert row is not None and ".venv" in row.detail
    _installed_at(monkeypatch, feature / ".venv")
    assert diagnostics._sandboxed_install_check(feature) is not None
    with pytest.raises(store_broker.LaunchRefused) as refusal:
        store_broker.refuse_inside(
            ["codex", "--sandbox", "workspace-write"], feature, get_adapter("codex")
        )
    said = str(refusal.value)
    assert f"a worktree of {principal.resolve()}" in said and str(feature.resolve()) in said, said


def test_codex_reads_its_system_config_under_the_users(
    beyond_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r14 of #113, finding 9: ``/etc/codex/config.toml``, which 0.162.1 reads, was
    skipped: a system-wide ``writable_roots`` was missed, and a system-wide
    ``danger-full-access`` read as workspace-write."""
    from aisquare.core.agent_adapters import codex as codex_module

    system = beyond_tmp / "etc-codex.toml"
    monkeypatch.setattr(codex_module, "SYSTEM_CONFIG", system)
    home, cwd = beyond_tmp / "codex-home", beyond_tmp / "w"
    home.mkdir()
    cwd.mkdir()
    shared = beyond_tmp / "shared"
    system.write_text(
        f"[sandbox_workspace_write]\nwritable_roots = [{json.dumps(str(shared))}]\n", "utf-8"
    )
    writes = get_adapter("codex").sandbox_writes([], cwd, home)
    assert writes is not None and writes.may_write(shared / "x")
    system.write_text('sandbox_mode = "danger-full-access"\n', encoding="utf-8")
    assert get_adapter("codex").sandbox_writes([], cwd, home) is None
    (home / "config.toml").write_text('sandbox_mode = "workspace-write"\n', encoding="utf-8")
    assert get_adapter("codex").sandbox_writes([], cwd, home) is not None, "the user's wins"


# --- review r15 of #113 -----------------------------------------------------------------


def _executable(place: Path, text: str = "#!/bin/sh\nexit 0\n") -> Path:
    place.parent.mkdir(parents=True, exist_ok=True)
    place.write_text(text, encoding="utf-8")
    place.chmod(0o755)
    return place


def test_the_plugins_launches_are_found_as_its_launcher_finds_them(
    beyond_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r15 of #113, findings 2, 3 and 9: the plugin's launches were skipped for an
    untrusted plugin though Codex runs its hooks under ``--dangerously-bypass-hook-trust``;
    an ``aisquare`` on PATH that cannot start was counted where the launcher passes over it
    to ``uvx``; ``~/.cargo/bin`` was not looked in; the stand-down read events where the
    launcher reads whether a hook's program can start; and on the ``uvx`` route only uv's
    cache counted, not the ``uvx`` the launcher finds off PATH, the uv-managed Python the
    cached environment links to, nor a cache ``uv.toml`` moves."""
    from aisquare.core import agents
    from aisquare.core.agent_adapters.types import PluginInstall

    monkeypatch.setattr(store_broker, "install_paths", REAL_INSTALL_PATHS)
    home = beyond_tmp / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(agents, "_home", lambda: home)
    for variable in ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(beyond_tmp / "config"))
    codex_home = home / ".codex"
    codex_home.mkdir()
    untrusted = PluginInstall(codex_home, "0.8.0", trusted=False)

    class Plugin:
        def installed(self, config_dir: Path | None) -> PluginInstall | None:
            return untrusted

    monkeypatch.setattr(agents, "plugin_of", lambda agent: Plugin())
    bare = str(beyond_tmp / "bare-bin")
    uvx = _executable(home / ".local" / "bin" / "uvx")

    def found(args: Sequence[str] = ("--dangerously-bypass-hook-trust",)) -> tuple[Path, ...]:
        return store_broker.install_paths(codex_home, "codex", bare, args)

    assert found([]) == found([]) and uvx.resolve() not in found([]), "untrusted, no bypass"
    paths_found = found()
    assert uvx.resolve() in paths_found, "the bypass runs it: its uvx, off PATH"
    assert (home / ".cache" / "uv").resolve() in paths_found
    assert (home / ".local" / "share" / "uv" / "python").resolve() in paths_found
    (beyond_tmp / "config" / "uv").mkdir(parents=True)
    moved = beyond_tmp / "moved-cache"
    (beyond_tmp / "config" / "uv" / "uv.toml").write_text(
        f"cache-dir = {json.dumps(str(moved))}\n", encoding="utf-8"
    )
    assert moved.resolve() in found(), "uv.toml's cache-dir"
    # An aisquare that cannot start is passed over; one in ~/.cargo/bin is found.
    dead = _executable(beyond_tmp / "path-bin" / "aisquare", "#!/nonexistent/python\n")
    assert dead.resolve() not in store_broker.install_paths(
        codex_home, "codex", str(dead.parent), ["--dangerously-bypass-hook-trust"]
    )
    cargo = _executable(home / ".cargo" / "bin" / "aisquare")
    assert cargo.resolve() in found() and moved.resolve() not in found(), "it runs, no uvx"
    # Beside a hook of ours whose program can start, the launcher stands down.
    hook_program = _executable(beyond_tmp / "hooks-bin" / "aisquare")
    monkeypatch.setattr(
        agents, "hook_commands", lambda agent, config_dir=None: [f"{hook_program} hook codex"]
    )
    assert cargo.resolve() not in found(), "standing down"
    hook_program.unlink()
    assert cargo.resolve() in found(), "its hook's program is gone: the plugin runs instead"


def test_the_user_site_counts_where_an_isolated_interpreter_loads_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review r15 of #113, finding 8: ``-E`` ignores ``PYTHONNOUSERSITE`` too, so the
    broker, the hooks and the receiver load the user site, which was counted only where
    the launching process had ``site.ENABLE_USER_SITE``."""
    import site

    monkeypatch.setattr(store_broker, "install_paths", REAL_INSTALL_PATHS)
    user_site = tmp_path / "user-site"
    monkeypatch.setattr(site, "ENABLE_USER_SITE", False)  # PYTHONNOUSERSITE=1 here
    monkeypatch.setattr(site, "getusersitepackages", lambda: str(user_site))
    monkeypatch.setattr(sys, "base_prefix", sys.prefix)  # no venv to keep it out
    assert user_site.resolve() in store_broker.install_paths()
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("include-system-site-packages = false\n", "utf-8")
    monkeypatch.setattr(sys, "prefix", str(venv))
    assert user_site.resolve() not in store_broker.install_paths(), "a venv keeps it out"


def test_an_unexpandable_socket_path_is_read_as_written(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review r15 of #113, finding 13: ``_socket_dirs`` expanded ``fleet.tmux_socket``
    unguarded, and a ``~olduser/...`` one refused every sandboxed launch, naming nothing."""
    dirs = store_broker._socket_dirs({}, "~nosuchuser-asq/sockets/fleet")  # no RuntimeError
    assert [d for d in dirs if d.parts[-2:] == ("~nosuchuser-asq", "sockets")], dirs


def test_the_doctor_probes_an_isolated_hook_as_it_runs(tmp_path: Path) -> None:
    """Review r15 of #113, finding 12: Codex's hooks run ``<python> -E -P -m aisquare``, and
    the doctor's version probe of the module form ran without ``-E``, so in a checkout on
    ``PYTHONPATH=$PWD/src`` it graded the tree's version, not the hook's."""
    from aisquare.core import agents

    isolated = agents.hook_binary(f"{sys.executable} -E -P -m aisquare hook codex")
    plain = agents.hook_binary(f"{sys.executable} -P -m aisquare hook codex")
    assert isolated is not None and plain is not None
    assert isolated.version_argv()[1:5] == ["-E", "-P", "-m", "aisquare"]
    assert plain.version_argv()[1:4] == ["-P", "-m", "aisquare"]


def test_the_doctor_flags_codex_hooks_that_read_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r15 of #113, finding 1: Codex hooks a pre-merge build of this branch connected
    run the console script, which reads ``PYTHONPATH`` outside the sandbox. The doctor says
    so (``sandbox-hooks``), and ``agents connect codex`` writes the ``-E`` form."""
    from aisquare.core import agents
    from aisquare.services import diagnostics

    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    console = _executable(tmp_path / "bin" / "aisquare")
    monkeypatch.setattr(agents, "_aisquare_command", lambda *args, **kwargs: str(console))
    agents.install_hooks("codex", home)
    row = diagnostics._sandbox_hooks_check()
    assert row is not None and row.name == "sandbox-hooks" and "PYTHONPATH" in row.detail
    monkeypatch.undo()
    monkeypatch.setenv("CODEX_HOME", str(home))
    agents.install_hooks("codex", home)
    assert diagnostics._sandbox_hooks_check() is None


def test_codex_reads_its_managed_config_over_the_users(
    beyond_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r15 of #113, finding 4: ``/etc/codex/managed_config.toml``, which 0.162.1
    reads over every user's config, was skipped."""
    from aisquare.core.agent_adapters import codex as codex_module

    managed = beyond_tmp / "managed.toml"
    monkeypatch.setattr(codex_module, "MANAGED_CONFIG", managed)
    monkeypatch.setattr(codex_module, "SYSTEM_CONFIG", beyond_tmp / "no-system.toml")
    home, cwd = beyond_tmp / "codex-home", beyond_tmp / "w"
    home.mkdir()
    cwd.mkdir()
    shared = beyond_tmp / "shared"
    managed.write_text(
        f"[sandbox_workspace_write]\nwritable_roots = [{json.dumps(str(shared))}]\n", "utf-8"
    )
    writes = get_adapter("codex").sandbox_writes([], cwd, home)
    assert writes is not None and writes.may_write(shared)
    (home / "config.toml").write_text('sandbox_mode = "danger-full-access"\n', "utf-8")
    managed.write_text('sandbox_mode = "workspace-write"\n', encoding="utf-8")
    assert get_adapter("codex").sandbox_writes([], cwd, home) is not None, "managed wins"


# --- SMOKE6 (the final live pass of #113) -----------------------------------------------


@pytest.mark.parametrize(
    ("install", "inside"),
    [
        pytest.param("w/.venv", "w/.venv/lib", id="venv-lib"),
        pytest.param("w/.venv", "w/.venv/lib/python3.14/site-packages", id="site-packages"),
        pytest.param("w/src/aisquare", "w/src/aisquare/services", id="package-subdir"),
        pytest.param("py", "py/lib/python3.14", id="base-lib"),
        pytest.param(
            "home/.local/lib/python3.14/site-packages",
            "home/.local/lib/python3.14/site-packages/extra",
            id="user-site",
        ),
        pytest.param(
            "home/.local/share/uv/tools/aisquare-cli",
            "home/.local/share/uv/tools/aisquare-cli/lib",
            id="uv-tool-env",
        ),
    ],
)
def test_a_root_the_sandbox_may_write_inside_an_install_is_refused_in_every_spelling(
    beyond_tmp: Path, monkeypatch: pytest.MonkeyPatch, install: str, inside: str
) -> None:
    """SMOKE6: the install check asked only whether an install lay under a root the sandbox
    may write, never whether such a root lay inside an install, so ``--add-dir
    <venv>/lib`` (a ``.pth`` in site-packages runs at every start of that interpreter,
    ``-E`` or not), a ``writable_roots`` entry in site-packages or the package's own
    directory, ``-C`` into one, or a profile's ``write`` path there was let through. Both
    directions refuse; a sibling outside the install does not."""
    monkeypatch.delenv("TMPDIR", raising=False)
    root, sub = beyond_tmp / install, beyond_tmp / inside
    sub.mkdir(parents=True)
    _installed_at(monkeypatch, root)
    elsewhere = beyond_tmp / "elsewhere"
    elsewhere.mkdir()
    codex = get_adapter("codex")
    store = store_dir()
    writes = ["codex", "--sandbox", "workspace-write"]
    spelled = [
        [*writes, "--add-dir", str(sub)],
        [*writes, "-c", f"sandbox_workspace_write.writable_roots=[{json.dumps(str(sub))}]"],
        [*writes, "-c", f"sandbox_workspace_write={{writable_roots=[{json.dumps(str(sub))}]}}"],
        [*writes, "-C", str(sub)],
    ]
    profile = codex.broker_access(
        codex.store_access("read_only", store), store, store / "broker" / "l" / "in",
        writable=(sub,),
    )  # fmt: skip
    assert profile is not None
    spelled.append(["codex", *profile])  # a read-only seat with a write path inside it
    for argv in spelled:
        with pytest.raises(store_broker.LaunchRefused) as refusal:
            store_broker.refuse_inside(argv, elsewhere, codex)
        assert str(sub.resolve()) in str(refusal.value), (argv, str(refusal.value))
    sibling = root.parent / f"{root.name}-sibling" / "lib"
    sibling.mkdir(parents=True)
    store_broker.refuse_inside([*writes, "--add-dir", str(sibling)], elsewhere, codex)


# --- GATE #113 --------------------------------------------------------------------------


def test_every_registered_agents_launch_inputs_are_held_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GATE #113, finding 2: the launch inputs were a list of Claude Code's and Codex's, so a
    next agent's project config (a ``.gemini/``) would have been a sandboxed Codex's to
    plant for that agent's next unsandboxed start. Each adapter declares its own
    (``capabilities.launch_inputs``), and the profile holds every registered one's."""
    from aisquare.core.agent_adapters import register, unregister
    from tests.fake_agent import FakeAgentAdapter

    fake = FakeAgentAdapter()
    register(fake)
    try:
        assert ".fakeagent" in store_broker.launch_inputs()
        assert {".claude", ".mcp.json", ".codex", ".agents", ".aisquare", ".git"} <= set(
            store_broker.launch_inputs()
        )
        tree = tmp_path / "proj"
        tree.mkdir()
        protect, _ = store_broker.launch_paths({}, tree)
        assert tree.resolve() / ".fakeagent" in protect
    finally:
        unregister(fake.id)
    assert ".fakeagent" not in store_broker.launch_inputs()


def test_the_doctors_sandbox_rows_ask_every_agent_that_sandboxes_its_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GATE #113, finding 2: the doctor's sandbox rows asked Codex by name. They ask every
    registered agent with ``sandboxes_tools`` that is installed, and say which."""
    from aisquare.core.agent_adapters import register, unregister
    from aisquare.services import diagnostics
    from tests.fake_agent import FakeAgentAdapter

    project = tmp_path / "proj"
    venv_bin = project / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (project / ".aisquare").mkdir()
    tools = tmp_path / "tools"
    tools.mkdir()
    fake_binary = tools / FakeAgentAdapter.binary
    fake_binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{venv_bin}{os.pathsep}{tools}")  # no codex here
    assert diagnostics._sandbox_path_check(project) is None, "no sandboxing agent installed"
    fake = FakeAgentAdapter()
    register(fake)
    try:
        row = diagnostics._sandbox_path_check(project)
        assert row is not None and FakeAgentAdapter.label in row.detail, row
    finally:
        unregister(fake.id)


def test_a_usage_line_under_python_m_names_the_program_aisquare() -> None:
    """SMOKE6, observed: a brokered command's usage error named the program ``python -m
    aisquare``, which the agent cannot type; it is ``aisquare``."""
    done = subprocess.run(
        [*selfcli.isolated_argv_for(["task", "no-such-command"])],
        capture_output=True, text=True, timeout=120, check=False,
        env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
    )  # fmt: skip
    # Plain text however the runner styles it (CI forces colour: the escapes split the line).
    said = re.sub(r"\x1b\[[0-9;]*m", "", done.stdout + done.stderr)
    assert "Usage: aisquare task" in said and "python -m" not in said, said


# --- GATE #113 at 303042b0 ----------------------------------------------------------------


@pytest.mark.parametrize("case", ["home-prefix", "bin-in-home"])
def test_an_environment_under_a_home_prefix_holds_no_project_of_it(
    beyond_tmp: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """GATE #113 at 303042b0, finding 1: since SMOKE6 a root the sandbox may write inside an
    install is refused, and an environment counted whole, by its prefix: a Python built
    with ``--prefix=$HOME``, or an ``aisquare`` the hooks run from ``~/bin``, has the home
    for one, so every sandboxed launch of a project under the home was refused, and the
    doctor's ``sandbox-install`` row warned in each. An environment counts by the parts
    that hold its code, which are still refused in either direction, and so is the home."""
    from aisquare.core import agents
    from aisquare.services import diagnostics

    monkeypatch.setattr(store_broker, "install_paths", REAL_INSTALL_PATHS)
    monkeypatch.delenv("TMPDIR", raising=False)
    home = beyond_tmp / "home"
    project = home / "proj"
    site_packages = home / "lib" / "python3.14" / "site-packages"
    for directory in (project, site_packages, home / "bin"):
        directory.mkdir(parents=True)
    _a_codex_project(project, beyond_tmp, monkeypatch)
    if case == "home-prefix":
        monkeypatch.setattr(sys, "base_prefix", str(home))
    else:
        program = home / "bin" / "aisquare"
        program.write_text("#!/bin/sh\n", encoding="utf-8")
        command = f"{program} hook codex"
        monkeypatch.setattr(agents, "hook_commands", lambda agent, config_dir=None: [command])
    codex = get_adapter("codex")
    writes = ["codex", "--sandbox", "workspace-write"]
    store_broker.refuse_inside(writes, project, codex)
    assert diagnostics._sandboxed_install_check(project) is None
    for inside in (site_packages, home / "bin"):
        with pytest.raises(store_broker.LaunchRefused) as refusal:
            store_broker.refuse_inside([*writes, "--add-dir", str(inside)], project, codex)
        assert str(inside) in str(refusal.value)
    with pytest.raises(store_broker.LaunchRefused, match=re.escape(str(home))):
        store_broker.refuse_inside(writes, home, codex)  # the home holds the code


def test_the_site_packages_an_interpreter_loads_count_outside_its_prefix(
    beyond_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GATE #113 at 303042b0, finding 1: an environment counts by its parts, not its prefix,
    and Debian's system interpreter (prefix ``/usr``) loads
    ``/usr/local/lib/python3/dist-packages``, in neither its ``bin`` nor its ``lib``: the
    site-packages it loads count wherever they are."""
    import site

    monkeypatch.setattr(store_broker, "install_paths", REAL_INSTALL_PATHS)
    monkeypatch.delenv("TMPDIR", raising=False)
    dist = beyond_tmp / "usr" / "local" / "lib" / "python3" / "dist-packages"
    (dist / "extra").mkdir(parents=True)
    project = beyond_tmp / "proj"
    project.mkdir()
    monkeypatch.setattr(site, "getsitepackages", lambda prefixes=None: [str(dist)])
    assert dist.resolve() in store_broker.install_paths()
    argv = ["codex", "--sandbox", "workspace-write", "--add-dir", str(dist / "extra")]
    with pytest.raises(store_broker.LaunchRefused, match=re.escape(str(dist / "extra"))):
        store_broker.refuse_inside(argv, project, get_adapter("codex"))
