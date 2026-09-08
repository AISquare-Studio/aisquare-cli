"""Bare ``aisquare``: the fleet UI at a terminal, usage everywhere else (plan §3.8).

For every earlier release ``aisquare`` with no arguments printed usage and
exited 2 (``no_args_is_help=True``). The fleet changes that ONLY at an
interactive terminal, so the rule has three branches and each needs its own
control: at a terminal the UI opens and nothing else happens; in a pipe or with
``TERM=dumb`` a script sees the help page and exit 2 exactly as it always did;
and under ``--json`` the refusal is ONE JSON object, because there stdout
belongs to a program. Nothing that ran ``aisquare`` by mistake ever meets a
full-screen app.

The predicate that decides is ``aisquare.cli.fleet.interactive_terminal`` (its
own unit tests are in ``test_fleet_cli.py``); here it is patched to each answer
in turn so the DISPATCH is what is under test. Two tests drive the real binary
through a pipe, because ``CliRunner``'s streams are pipes by construction and a
test that only ever sees the predicate say "no" cannot tell dispatch from luck.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare import __version__
from aisquare.cli import fleet as fleet_cli
from aisquare.cli.app import app
from aisquare.core.paths import HOME_ENV_VAR
from tests.cli_tree import all_command_paths

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(text: str) -> str:
    return " ".join(_ANSI.sub("", text).split())


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "200")
    return work


@pytest.fixture
def ui_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every call to the UI entry the root callback dispatches to."""
    calls: list[str] = []
    monkeypatch.setattr(fleet_cli, "ui", lambda: calls.append("ui"))
    return calls


def _usage(result_output: str) -> str:
    flat = _plain(result_output)
    assert flat, "no output at all — usage was not printed"
    return flat


# ── the pipe half: usage and exit 2, as before ────────────────────────────────


def test_bare_invocation_in_a_pipe_prints_usage_and_exits_2(
    runner: CliRunner, ui_calls: list[str]
) -> None:
    """CliRunner's stdin and stdout are pipes, so the real predicate says no."""
    result = runner.invoke(app, [])

    assert result.exit_code == 2
    flat = _usage(result.output)
    assert "Usage: " in flat
    assert "Commands" in flat and "fleet" in flat  # the help text, not a bare error
    assert ui_calls == []


USAGE_OBJECT = {"error": "usage", "message": "Missing command."}
"""The refusal a machine gets for a bare invocation: the same ``error`` key
``global_flags._handle_usage_error`` emits for an unknown command or option."""


@pytest.mark.parametrize("argv", [["--json"], ["--json", "--profile", "ci"], ["--json", "--quiet"]])
def test_json_alone_is_one_usage_object_on_stdout_and_exits_2(
    runner: CliRunner, ui_calls: list[str], argv: list[str]
) -> None:
    """A machine asked for a machine-readable answer and there is none — so the
    refusal is machine-readable too.

    Under ``--json`` stdout is for a program: empty, or ONE parseable object
    (``tests/test_json_stdout_is_machine_readable.py``, whose leaf-command sweep
    cannot reach the bare root, and ``cli/global_flags.py``'s #21 contract).
    Echoing ``ctx.get_help()`` there handed a ``jq`` pipeline ~40 lines of
    rich-formatted human text — measured 5159 bytes of "Usage: aisquare …" on
    stdout — and it was never what this path did either: ``no_args_is_help``
    left stdout EMPTY and sent "Missing command." to stderr. The parametrised
    companions are the flags a script really leads with; ``--json`` decides
    wherever it sits before the missing command.
    """
    result = runner.invoke(app, argv)

    assert result.exit_code == 2
    assert result.stdout.count("\n") == 1, result.stdout  # one line, not a page
    assert json.loads(result.stdout) == USAGE_OBJECT
    assert "Usage: " not in result.stdout
    assert ui_calls == []


def test_the_pipe_gets_the_help_page_and_json_gets_the_object(runner: CliRunner) -> None:
    """Same refusal, two audiences, one exit code — and each half is the other's
    control. Collapse the branches back together and one of these two lines
    fails whichever way it collapsed: a person piping nothing gets an error
    object, or a ``jq`` pipeline gets the help page."""
    bare = runner.invoke(app, [])
    machine = runner.invoke(app, ["--json"])

    assert bare.exit_code == machine.exit_code == 2
    assert "Usage: " in _plain(bare.stdout) and "Usage: " not in machine.stdout
    assert json.loads(machine.stdout) == USAGE_OBJECT
    with pytest.raises(json.JSONDecodeError):
        json.loads(bare.stdout)


def test_the_usage_printed_is_the_whole_help_page(runner: CliRunner) -> None:
    """What ``no_args_is_help`` printed was the full help page — every command
    listed — so that is what the dispatch must print, not a one-line ``Usage:``
    hint. Pinned against ``--help`` up to trailing newlines, and the "up to" is
    a measured gap, not slack: Typer's rich help renders itself inside
    ``ctx.get_help()`` and returns ``""``, so ``typer.echo`` of that adds one
    ``"\\n"`` the old ``no_args_is_help`` path never printed (typer 0.27.2:
    the old output ended ``╯\\n``; ``--help`` and today's dispatch end
    ``╯\\n\\n``). The byte-exact pin belongs beside the dispatch in
    ``cli/app.py`` once it echoes only a non-empty ``get_help()``; the content
    pin lives here."""
    bare = runner.invoke(app, [])
    helped = runner.invoke(app, ["--help"])

    assert bare.exit_code == 2 and helped.exit_code == 0
    page = bare.output.rstrip("\n")
    assert page  # a comparison of two empty strings would pin nothing
    assert page == helped.output.rstrip("\n")


def test_a_terminal_free_shell_never_opens_the_ui(
    runner: CliRunner, ui_calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative control for dispatch: with the predicate answering no, the
    UI is not called — this is the branch every script relies on."""
    monkeypatch.setattr(fleet_cli, "interactive_terminal", lambda: False)

    result = runner.invoke(app, [])

    assert result.exit_code == 2
    assert "Usage: " in _usage(result.output)
    assert ui_calls == []


# ── the terminal half: the UI, once, and nothing else ────────────────────────


def test_bare_invocation_at_a_terminal_opens_the_ui_exactly_once(
    runner: CliRunner, ui_calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fleet_cli, "interactive_terminal", lambda: True)

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.output
    assert ui_calls == ["ui"]
    assert result.output == ""  # nothing else: no usage, no banner, no JSON


def test_json_at_a_terminal_answers_the_machine_and_never_opens_the_ui(
    runner: CliRunner, ui_calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--json`` wins over the terminal: a pipeline run from a TTY is still a
    pipeline, and it gets the object rather than the page."""
    monkeypatch.setattr(fleet_cli, "interactive_terminal", lambda: True)

    result = runner.invoke(app, ["--json"])

    assert result.exit_code == 2
    assert json.loads(result.stdout) == USAGE_OBJECT
    assert ui_calls == []


def test_a_subcommand_at_a_terminal_does_not_open_the_ui(
    runner: CliRunner, ui_calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the BARE invocation dispatches; a real command at a terminal runs
    itself."""
    monkeypatch.setattr(fleet_cli, "interactive_terminal", lambda: True)

    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0, result.output
    assert ui_calls == []


# ── what must not change ─────────────────────────────────────────────────────


def test_version_still_works_without_no_args_is_help(runner: CliRunner) -> None:
    """``no_args_is_help`` went off to make room for the dispatch; the eager
    ``--version`` must still short-circuit before the callback decides anything."""
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0, result.output
    assert f"aisquare {__version__}" in result.output
    assert "Usage" not in result.output


def test_help_still_works(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    assert "Usage: " in _plain(result.output)


def test_the_ui_is_an_explicit_command_in_the_tree() -> None:
    """Reachable from a script or alias, and present in ``--help`` (§3.8)."""
    assert ("ui",) in all_command_paths()


def test_ui_help_exists(runner: CliRunner) -> None:
    result = runner.invoke(app, ["ui", "--help"])

    assert result.exit_code == 0, result.output
    assert "Usage: " in _plain(result.output)


def test_the_fleet_group_with_no_verb_prints_its_usage(runner: CliRunner) -> None:
    """The nested group keeps ``no_args_is_help``: ``aisquare fleet`` is a
    question about verbs, not a request for the UI."""
    result = runner.invoke(app, ["fleet"])

    assert result.exit_code == 2
    flat = _usage(result.output)
    assert "Usage: " in flat and "spawn" in flat


# ── the real binary, through a real pipe ─────────────────────────────────────


def _bare_binary(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """The real entry point with real stdio: stdin from /dev/null, stdout a pipe,
    ``TERM`` a real terminal so the pipe (or ``--json``) ALONE is what decides."""
    env = os.environ.copy()
    env[HOME_ENV_VAR] = str(tmp_path / "home")
    env["TERM"] = "xterm-256color"
    env["NO_COLOR"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "aisquare", *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
    )


def test_the_real_binary_in_a_pipe_prints_usage_and_exits_2(tmp_path: Path) -> None:
    """This is the invocation a stray ``aisquare`` in a script produces."""
    proc = _bare_binary(tmp_path)

    assert proc.returncode == 2, proc.stderr
    assert "Usage: " in _plain(proc.stdout)
    assert "Traceback" not in proc.stderr


def test_the_real_binary_under_json_puts_only_json_on_stdout(tmp_path: Path) -> None:
    """The measurement that found the defect, kept: ``aisquare --json`` printed
    the whole help page here. ``json.loads`` of the WHOLE stream is the
    assertion, not a substring — the claim is about every byte a pipeline
    reads, and one extra line of prose is what broke it."""
    proc = _bare_binary(tmp_path, "--json")

    assert proc.returncode == 2, proc.stderr
    assert json.loads(proc.stdout) == USAGE_OBJECT
    assert "Traceback" not in proc.stderr
