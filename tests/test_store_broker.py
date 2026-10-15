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
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths, selfcli
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
    for name in store_broker.LAUNCH_INPUTS:
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


def test_a_sandboxed_launch_is_refused_where_its_sandbox_may_write_aisquare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """Review r11 of #113, finding 2: the hooks and the broker run this aisquare outside the
    sandbox, so an aisquare in the project's ``.venv`` or an editable ``src/`` a sandboxed
    tester may write ran what it wrote, at its next hook. Such a launch is refused, naming
    the remedy, and the doctor says so."""
    from aisquare.services import diagnostics

    project = tmp_path / "proj"
    venv = project / ".venv"
    venv.mkdir(parents=True)
    monkeypatch.setattr(store_broker, "install_paths", lambda: (venv.resolve(),))
    with pytest.raises(store_broker.LaunchRefused, match="uv tool install aisquare"):
        store_broker.refuse_inside(project, get_adapter("codex"))
    monkeypatch.chdir(project)
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    monkeypatch.setattr(agent_launch, "executable", lambda selected: selected.binary.binary)
    refused = runner.invoke(app, ["launch", "tester", "--agent", "codex", "--", *_profile()])
    assert refused.exit_code == 1 and "uv tool install aisquare" in refused.output
    assert not execute.called
    row = diagnostics._sandboxed_install_check(project)
    assert row is not None and row.status.value == "warn" and ".venv" in row.detail
    monkeypatch.setattr(store_broker, "install_paths", lambda: (Path("/opt/elsewhere"),))
    assert diagnostics._sandboxed_install_check(project) is None
    store_broker.refuse_inside(project, get_adapter("codex"))  # no refusal


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
