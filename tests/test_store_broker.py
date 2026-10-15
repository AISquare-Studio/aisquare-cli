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
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import selfcli
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.agent_adapters.types import store_dir
from aisquare.core.store import store_session
from aisquare.models import TeamSession
from aisquare.services import agent_launch, store_broker
from aisquare.services import team as team_service

#: The real process start, before the suite's ``no_store_broker`` stands it in.
REAL_RUN_BROKER = store_broker.run_broker
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


def test_a_request_from_another_board_is_refused(work: Path, tmp_path: Path) -> None:
    """The broker runs commands on its launch's own board: a cwd the sandbox chose in
    another repository would otherwise make a project row there, and post to it."""
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    answer = _answer(_broker(tmp_path), _request(["task", "add", "planted"], other))
    assert answer["code"] == 2 and "another board" in str(answer["stderr"])
    nowhere = _answer(_broker(tmp_path), _request(["board"], tmp_path / "missing"))
    assert nowhere["code"] == 2


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
    with pytest.raises(ValueError, match="the fleet's own"):
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
    assert store_broker.brokered() is None, "a writable home"
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
