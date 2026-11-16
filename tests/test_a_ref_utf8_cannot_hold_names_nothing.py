"""An argument holding a byte that is not UTF-8 is refused in words, for every command, before
any of it runs (review r39 of #113, finding 6, reopened).

A shell passes a byte that is not UTF-8 (``x$'\\xff'``) as a lone surrogate, and the store, the
log and the board hold only text: each command that met one raised the codec's error.
``task claim tsk_\\udcff`` and ``--as <ref>\\udcff`` printed a Rich traceback, and r39's tester
found the same in ``fleet stop``/``tell``/``restart``/``relabel``, ``team signal``,
``focus`` and ``role``, and the codec's own words as the reason in ``brief show``/``check``/
``finding``, ``project switch``/``pin``/``unpin``/``forget`` and ``note --to``. Guarded one
verb at a time, the next verb was the next hole. So the CLI's root group refuses it where it
parses every command's argv (``cli.global_flags.GlobalFlagsGroup.parse_args``,
``core.names.undecodable_argument``): a usage error, exit 2, ``{"error": "usage"}`` under
``--json``, naming the argument with its byte escaped. A sandbox's broker refuses one the same
way before it runs anything, logged (tests/test_store_broker.py). The services still find
nothing for such a ref, for their callers that are not the CLI.

What follows the ``--`` of a command that hands it to another program
(``cli.native_args.PassthroughCommand``: ``launch``, ``fleet spawn``, ``accounts run``,
``exec``, ``fleet gate``) is asked nothing, by the manager's ruling: it never reaches the
store, the log or the screen as aisquare's own text, and ``asq exec -- grep $'\\xff' f``
runs. A positional of the command's own filled from there is still refused, and where such a
tail is stored (``exec``'s report, a fleet spawn's launch record) it is kept escaped.

The sweep below walks every command the CLI registers (``tests.cli_tree``), so a command
added later joins it, and gives each, in turn, the byte in an argument and in each option
that takes a value. Every command's own code is replaced by a stand-in that fails the test
if it runs: the guard must refuse before any of it, and a guard that is gone runs no real
command with the byte.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import typer.testing
from typer.core import TyperGroup
from typer.testing import CliRunner

from aisquare.cli import launch as launch_cli
from aisquare.cli.app import app
from aisquare.cli.global_flags import passes_through
from aisquare.core import launch_seal
from aisquare.services import command_reports
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from tests.cli_tree import leaf_invocations_by_path, root_command
from tests.test_a_claim_prints_its_contract import CODER, PLANNER, _ok, _start
from tests.test_fleet_service import (  # noqa: F401 - the fixtures, by name
    FakeTmux,
    claude_on_path,
    clock,
    project,
    repo,
    tmux,
)
from tests.test_fleet_service import _command as fleet_command

#: How a shell passes the byte 0xff, which is not UTF-8.
NOT_UTF8 = chr(0xDCFF)
BAD = f"x{NOT_UTF8}"
#: What the refusal says, and what it must never: the codec's words or a traceback.
SAID = "holds a byte that is not UTF-8"
CODEC = ("Traceback", "UnicodeEncodeError", "codec can't", "surrogates not allowed")


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from tests.projects import add_project

    work = tmp_path / "work"  # not "repo": the fleet's fixtures make that one
    work.mkdir()
    monkeypatch.chdir(work)
    add_project(work)
    return work


@pytest.fixture
def board(runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    _start(runner, PLANNER, "planner", work_dir, monkeypatch)
    _start(runner, CODER, "coder", work_dir, monkeypatch)
    _ok(runner, "task", "add", "wire auth", "--as", PLANNER[:8])
    return runner


def _said(output: str) -> str:
    """``output`` as one line, the error box's borders gone."""
    return " ".join(output.replace("│", " ").split())


def _refused_in_words(runner: CliRunner, argv: list[str]) -> str:
    """What ``aisquare <argv>`` said, once it is checked as the refusal: exit 2, its words,
    and nothing of the codec's."""
    result = runner.invoke(app, argv)
    said = _said(result.output)
    assert result.exit_code == 2, (argv, said, result.exception)
    assert isinstance(result.exception, SystemExit), (argv, result.exception)
    assert SAID in said and not any(word in said for word in CODEC), (argv, said)
    return said


# --- every command the CLI registers ---------------------------------------------------------


#: The commands that hand what follows their ``--`` to another program as given (the
#: manager's ruling on r39's reopen): the agent's arguments, the program a run starts.
PASSTHROUGH = {("launch",), ("exec",), ("accounts", "run"), ("fleet", "spawn"), ("fleet", "gate")}


@dataclass
class Swept:
    """The CLI's command tree as ``CliRunner`` invokes it here: every command's own code a
    stand-in that notes what it was given (``ran``: its path and the arguments it was left
    to pass on), every group's callback one that notes it ran."""

    leaves: dict[tuple[str, ...], Any]
    ran: list[tuple[tuple[str, ...], list[str]]] = field(default_factory=list)


def _nodes(command: Any, path: tuple[str, ...]) -> Iterator[tuple[tuple[str, ...], Any]]:
    yield path, command
    if isinstance(command, TyperGroup):
        for name, sub in command.commands.items():
            yield from _nodes(sub, (*path, name))


@pytest.fixture
def swept(monkeypatch: pytest.MonkeyPatch) -> Swept:
    root = root_command()
    nodes = list(_nodes(root, ()))
    made = Swept({path: command for path, command in nodes if not isinstance(command, TyperGroup)})

    def noted(path: tuple[str, ...]) -> Callable[..., None]:
        return lambda *_args, **_kwargs: made.ran.append((path, []))

    def ran(path: tuple[str, ...]) -> Callable[[Any], None]:
        return lambda ctx: made.ran.append((path, list(ctx.args)))

    for path, command in nodes:
        if path and isinstance(command, TyperGroup):
            monkeypatch.setattr(command, "callback", noted(path))
        elif path:
            monkeypatch.setattr(command, "invoke", ran(path))
    monkeypatch.setattr(typer.testing, "_get_command", lambda _app: root)
    return made


def _value_options(command: Any) -> list[str]:
    return [
        parameter.opts[0]
        for parameter in command.params
        if parameter.param_type_name == "option"
        and not getattr(parameter, "is_flag", False)
        and not getattr(parameter, "count", False)
    ]


def _runnable(path: tuple[str, ...]) -> list[str]:
    """``path``'s argv with its required arguments filled (``tests.cli_tree``), up to any
    ``--`` it was given."""
    argv = dict(leaf_invocations_by_path())[path]
    return argv[: argv.index("--")] if "--" in argv else argv


def test_every_command_refuses_the_byte_in_an_argument_and_in_each_option(
    runner: CliRunner, swept: Swept
) -> None:
    """Each command the CLI registers, the byte in an argument (or where one would be), in
    each option that takes a value, and after a ``--`` of a command that passes nothing on
    (``task add -- …``, a title): refused in words, exit 2, no code of it run. Mutants: the
    root's guard dropped (every command runs); the ``--`` tail let through for every
    command (the ``--`` cases run)."""
    assert len(swept.leaves) > 200, "the walk reached the whole tree"
    asked = 0
    for path, command in swept.leaves.items():
        tail = [] if path in PASSTHROUGH else [[*path, "--", BAD]]
        options = [[*path, option, BAD] for option in _value_options(command)]
        for argv in ([*path, BAD], *options, *tail):
            _refused_in_words(runner, argv)
            asked += 1
    assert swept.ran == [], swept.ran
    assert asked > 2 * len(swept.leaves), asked


def test_the_commands_that_pass_their_double_dash_on_are_these(swept: Swept) -> None:
    """``cli.native_args.PassthroughCommand``'s, and every command that takes extra
    arguments is one: a new one is added here, and asked all of its arguments until it is."""
    passing = {path for path, command in swept.leaves.items() if passes_through(command)}
    extra = {
        path
        for path, command in swept.leaves.items()
        if getattr(command, "context_settings", {}).get("allow_extra_args")
    }
    assert passing == extra == PASSTHROUGH, (passing, extra)


def test_after_a_passthrough_s_double_dash_the_byte_reaches_it_as_given(
    runner: CliRunner, swept: Swept
) -> None:
    """``asq exec -- grep $'\\xff' f``: what follows the ``--`` goes to another program, never
    to the store, the log or the screen as aisquare's text, so it is asked nothing and
    reaches the command unchanged. Mutant (the ``--`` tail asked for every command): each
    is refused, and none is reached."""
    for path in sorted(PASSTHROUGH):
        swept.ran.clear()
        result = runner.invoke(app, [*_runnable(path), "--", "tail", BAD])
        assert result.exit_code == 0, (path, _said(result.output), result.exception)
        [(_, passed)] = [ran for ran in swept.ran if ran[0] == path]
        assert passed[-1] == BAD and passed[-1].encode("utf-8", "surrogateescape") == b"x\xff"


def test_a_passthrough_s_own_arguments_are_asked_before_and_after_its_double_dash(
    runner: CliRunner, swept: Swept
) -> None:
    """Before the ``--`` the byte is refused as for any command; after it, a positional of the
    command's own filled from there (``launch -- $'\\xff'`` names the role) is refused by
    the command (``PassthroughCommand.parse_args``). Mutants: the tail let through before the
    ``--`` too; the command's own parameters not asked."""
    for path in sorted(PASSTHROUGH):
        _refused_in_words(runner, [*_runnable(path), BAD, "--", "tail"])
        command = swept.leaves[path]
        if any(param.param_type_name == "argument" for param in command.params):
            said = _refused_in_words(runner, [*path, "--", BAD])
            assert "x\\udcff" in said, said
    assert not [ran for ran in swept.ran if ran[0] in PASSTHROUGH], swept.ran


@pytest.mark.parametrize("json_led", [False, True], ids=["text", "--json"])
@pytest.mark.parametrize(
    "argv",
    [
        ["--profile", "exec", "--", "fleet", "stop", BAD],
        ["--profile=exec", "task", "add", "--", BAD],
        ["--profile", "launch", "task", "add", "--", BAD],
    ],
    ids=["--profile exec -- fleet stop", "--profile=exec task add --", "--profile launch …"],
)
def test_a_root_option_s_value_naming_a_passthrough_command_exempts_nothing(
    runner: CliRunner, swept: Swept, argv: list[str], json_led: bool
) -> None:
    """``--profile exec`` names a profile, not the command ``exec``: walked as a command, the
    tail after the ``--`` was let through, and ``fleet stop x$'\\xff'`` a traceback again
    (r39's tester, seq 10988). Mutant (``_named_command`` skipping no option's value): the
    command after it runs with the byte."""
    if json_led:
        result = runner.invoke(app, ["--json", *argv])
        assert result.exit_code == 2, (argv, result.output, result.exception)
        answer = json.loads(result.stdout)
        assert answer["error"] == "usage" and SAID in answer["message"], answer
    else:
        _refused_in_words(runner, argv)
    assert swept.ran == [], swept.ran


def test_with_json_the_refusal_is_one_usage_object(runner: CliRunner, swept: Swept) -> None:
    """``--json`` before any command: ``{"error": "usage"}``, its message the same words."""
    for path in swept.leaves:
        result = runner.invoke(app, ["--json", *path, BAD])
        assert result.exit_code == 2, (path, result.output, result.exception)
        answer = json.loads(result.stdout)
        assert answer["error"] == "usage" and SAID in answer["message"], (path, answer)
        assert "x\\udcff" in answer["message"], answer


def test_a_root_option_holding_the_byte_is_refused_too(runner: CliRunner, swept: Swept) -> None:
    said = _refused_in_words(runner, ["--profile", BAD, "board"])
    assert f"argument 2 (x\\udcff) {SAID}" in said, said


# --- what r39's tester ran, for real ---------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["task", "claim", f"tsk_{NOT_UTF8}", "--as", CODER[:8]],
        ["task", "claim", "1", "--as", f"{CODER[:8]}{NOT_UTF8}"],
        ["fleet", "stop", BAD],
        ["fleet", "tell", BAD, "hi"],
        ["fleet", "restart", BAD],
        ["fleet", "relabel", BAD, "y"],
        ["team", "signal", BAD, "--as", CODER[:8]],
        ["team", "focus", BAD, "--as", CODER[:8]],
        ["team", "role", BAD, "--as", CODER[:8]],
        ["brief", "show", BAD],
        ["project", "switch", BAD],
        ["project", "forget", BAD],
        ["note", "hi", "--to", BAD, "--as", CODER[:8]],
        ["note", "hi", "--as", f"{CODER[:8]}{NOT_UTF8}"],
        ["board", "--project", BAD],
        ["team", "show", BAD, "--as", CODER[:8]],
    ],
    ids=lambda argv: " ".join(argv[:2]),
)
def test_what_r39_s_tester_ran_is_refused_in_words(board: CliRunner, argv: list[str]) -> None:
    """The real commands, unpatched: each was a traceback or the codec's words."""
    said = _refused_in_words(board, argv)
    assert "\\udcff)" in said, said


def test_the_console_script_refuses_the_byte_from_a_real_command_line(work_dir: Path) -> None:
    """``python -m aisquare fleet stop x$'\\xff'``, the tester's repro, as a shell runs it:
    the byte itself on the command line, through ``main``."""
    done = subprocess.run(
        [sys.executable, "-m", "aisquare", "fleet", "stop", BAD],
        capture_output=True,
        timeout=60,
        check=False,
    )
    said = _said(done.stderr.decode("utf-8", "backslashreplace"))
    assert done.returncode == 2, said
    assert SAID in said and not any(word in said for word in CODEC), said


# --- the commands that pass it on, for real ---------------------------------------------------


def test_exec_runs_the_byte_and_its_report_keeps_it_escaped(runner: CliRunner) -> None:
    """The program gets the byte itself; the report, JSON, holds its escape. Mutant (the
    report's argv as given): its write failed, "Command report failed", exit 1."""
    shown = "import sys; print(ascii(sys.argv[1:]))"
    result = runner.invoke(app, ["exec", "--", sys.executable, "-c", shown, BAD])
    assert result.exit_code == 0, _said(result.output)
    assert "['x\\udcff']" in result.output, result.output  # how the child decoded b"x\xff"
    [report] = command_reports.list_reports()
    assert report.argv[-1] == "x\\udcff", report.argv


def test_fleet_gate_runs_the_byte_and_its_json_carries_it(runner: CliRunner) -> None:
    argv = ["--json", "fleet", "gate", "--min-free-mb", "0", "--", sys.executable, "-c", "pass"]
    result = runner.invoke(app, [*argv, BAD])
    assert result.exit_code == 0, _said(result.output)
    assert json.loads(result.stdout)["ran"][-1] == BAD


def test_launch_hands_the_byte_to_its_agent(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, list[str]] = {}
    monkeypatch.setattr(launch_cli, "_exec", lambda _binary, argv, _env: seen.update(argv=argv))
    monkeypatch.setattr(shutil, "which", lambda command: f"/usr/local/bin/{command}")
    result = runner.invoke(app, ["launch", "coder", "--", "--resume", BAD])
    assert result.exit_code == 0, _said(result.output)
    assert seen["argv"][-2:] == ["--resume", BAD], seen


def test_a_fleet_spawn_keeps_the_byte_escaped_in_what_it_records_and_runs(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: Any,  # noqa: F811
) -> None:
    """The launch record and the row hold only text, and the window runs what they hold, so
    a restart replays what the spawn ran: the byte's escape. Mutant (the agent's arguments
    as given): the record's write raised the codec's error, after the spawn had begun."""
    agent = fleet_service.spawn(project, "coder", worktree=False, agent_args=["--resume", BAD])
    record = launch_seal.read(agent.agent.id)
    assert isinstance(record, launch_seal.LaunchRecord), record
    assert record.spec.command[-2:] == ["--resume", "x\\udcff"], record.spec
    assert fleet_command(tmux)[-2:] == ["--resume", "x\\udcff"]
    said = "1 of the agent's arguments held a byte that is not UTF-8 (x\\udcff)"
    assert any(note.startswith(said) for note in agent.notes), agent.notes  # mutant: unsaid


# --- the services, for their callers that are not the CLI ----------------------------------


def test_the_services_find_nothing_for_a_ref_utf8_cannot_hold(board: CliRunner) -> None:
    """Remote, the UI and the hooks call the services with refs the CLI never parsed: each
    finds nothing for one, as for any ref that names nothing. Mutants, one guard dropped
    each (``find_task``, ``_session_named``, ``verify_receipt``, ``_delegated_board``,
    ``fleet.resolve_project``): each raises the codec's error here."""
    with pytest.raises(KeyError):
        team_service.claim_task(f"tsk_{NOT_UTF8}", session_ref=CODER[:8])
    with pytest.raises(team_service.NoSuchSession):
        team_service.claim_task("1", session_ref=f"{CODER[:8]}{NOT_UTF8}")
    assert team_service.verify_receipt(f"evt_{NOT_UTF8}", session_ref=CODER[:8]).event is None
    with pytest.raises(ValueError, match="no such project"):
        team_service.add_task("x", project=BAD, session_ref=PLANNER[:8])
    with pytest.raises(fleet_service.NoSuchProject):
        fleet_service.resolve_project(BAD)
    assert "todo" in board.invoke(app, ["task", "show", "1"]).output  # nothing was claimed
