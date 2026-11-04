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

The sweep below walks every command the CLI registers (``tests.cli_tree``), so a command
added later joins it, and gives each, in turn, the byte in an argument and in each option
that takes a value. Every command's own code is replaced by a stand-in that fails the test
if it runs: the guard must refuse before any of it, and a guard that is gone runs no real
command with the byte.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import typer.testing
from typer.core import TyperGroup
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from tests.cli_tree import root_command
from tests.test_a_claim_prints_its_contract import CODER, PLANNER, _ok, _start

#: How a shell passes the byte 0xff, which is not UTF-8.
NOT_UTF8 = chr(0xDCFF)
BAD = f"x{NOT_UTF8}"
#: What the refusal says, and what it must never: the codec's words or a traceback.
SAID = "holds a byte that is not UTF-8"
CODEC = ("Traceback", "UnicodeEncodeError", "codec can't", "surrogates not allowed")


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from tests.projects import add_project

    work = tmp_path / "repo"
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


class Ran(AssertionError):
    """A command's own code ran with an argument holding a byte that is not UTF-8."""


def _nodes(command: Any, path: tuple[str, ...]) -> Iterator[tuple[tuple[str, ...], Any]]:
    yield path, command
    if isinstance(command, TyperGroup):
        for name, sub in command.commands.items():
            yield from _nodes(sub, (*path, name))


def _stand_in(path: tuple[str, ...]) -> Callable[..., None]:
    def ran(*_args: object, **_kwargs: object) -> None:
        raise Ran(f"aisquare {' '.join(path)} ran with a byte that is not UTF-8")

    return ran


@pytest.fixture
def every_command(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple[str, ...], Any]]:
    """The CLI's command tree, built once, every group's and command's own code (all but the
    root's) a stand-in that fails if it runs; ``CliRunner`` invokes this tree."""
    root = root_command()
    nodes = list(_nodes(root, ()))
    for path, command in nodes:
        if path:
            monkeypatch.setattr(command, "callback", _stand_in(path))
    monkeypatch.setattr(typer.testing, "_get_command", lambda _app: root)
    return [(path, command) for path, command in nodes if not isinstance(command, TyperGroup)]


def _value_options(command: Any) -> list[str]:
    return [
        parameter.opts[0]
        for parameter in command.params
        if parameter.param_type_name == "option"
        and not getattr(parameter, "is_flag", False)
        and not getattr(parameter, "count", False)
    ]


def test_every_command_refuses_the_byte_in_an_argument_and_in_each_option(
    runner: CliRunner, every_command: list[tuple[tuple[str, ...], Any]]
) -> None:
    """Each command the CLI registers, the byte in an argument (or where one would be) and
    in each option that takes a value: refused in words, exit 2, its code never run. Mutant
    (the root's guard dropped): every command runs, and the first fails the sweep."""
    assert len(every_command) > 200, "the walk reached the whole tree"
    asked = 0
    for path, command in every_command:
        for argv in ([*path, BAD], *([*path, option, BAD] for option in _value_options(command))):
            _refused_in_words(runner, argv)
            asked += 1
    assert asked > len(every_command), asked


def test_with_json_the_refusal_is_one_usage_object(
    runner: CliRunner, every_command: list[tuple[tuple[str, ...], Any]]
) -> None:
    """``--json`` before any command: ``{"error": "usage"}``, its message the same words."""
    for path, _ in every_command:
        result = runner.invoke(app, ["--json", *path, BAD])
        assert result.exit_code == 2, (path, result.output, result.exception)
        answer = json.loads(result.stdout)
        assert answer["error"] == "usage" and SAID in answer["message"], (path, answer)
        assert "x\\udcff" in answer["message"], answer


def test_a_root_option_holding_the_byte_is_refused_too(
    runner: CliRunner, every_command: list[tuple[tuple[str, ...], Any]]
) -> None:
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
