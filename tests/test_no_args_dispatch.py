"""Bare ``aisquare``: the fleet UI at a terminal, usage everywhere else (plan §3.8).

For every earlier release ``aisquare`` with no arguments printed usage and
exited 2 (``no_args_is_help=True``). The fleet changes that ONLY at an
interactive terminal, so the rule has two halves and each needs its own
control: at a terminal the UI opens and nothing else happens; in a pipe, under
``--json`` or with ``TERM=dumb`` a script sees exactly what it always saw, so
nothing that ran ``aisquare`` by mistake ever meets a full-screen app.

The predicate that decides is ``aisquare.cli.fleet.interactive_terminal`` (its
own unit tests are in ``test_fleet_cli.py``); here it is patched to each answer
in turn so the DISPATCH is what is under test. One test drives the real binary
through a pipe, because ``CliRunner``'s streams are pipes by construction and a
test that only ever sees the predicate say "no" cannot tell dispatch from luck.
"""

from __future__ import annotations

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


def test_json_alone_prints_usage_and_exits_2(runner: CliRunner, ui_calls: list[str]) -> None:
    """A machine asked for a machine-readable answer and there is none — usage is
    the honest reply, byte-for-byte what it always was."""
    result = runner.invoke(app, ["--json"])

    assert result.exit_code == 2
    assert "Usage: " in _usage(result.output)
    assert ui_calls == []


def test_the_pipe_and_json_answers_are_the_same_usage(runner: CliRunner) -> None:
    bare = runner.invoke(app, [])
    machine = runner.invoke(app, ["--json"])

    assert bare.exit_code == machine.exit_code == 2
    assert bare.output == machine.output


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


def test_json_at_a_terminal_still_prints_usage_and_never_opens_the_ui(
    runner: CliRunner, ui_calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--json`` wins over the terminal: a pipeline run from a TTY is still a
    pipeline."""
    monkeypatch.setattr(fleet_cli, "interactive_terminal", lambda: True)

    result = runner.invoke(app, ["--json"])

    assert result.exit_code == 2
    assert "Usage: " in _usage(result.output)
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


def test_the_real_binary_in_a_pipe_prints_usage_and_exits_2(tmp_path: Path) -> None:
    """End to end, with the real stdio: stdin from /dev/null and stdout into a
    pipe, TERM set to a real terminal so the pipe ALONE is what decides. This
    is the invocation a stray ``aisquare`` in a script produces."""
    env = os.environ.copy()
    env[HOME_ENV_VAR] = str(tmp_path / "home")
    env["TERM"] = "xterm-256color"
    env["NO_COLOR"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "aisquare"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
    )

    assert proc.returncode == 2, proc.stderr
    assert "Usage: " in _plain(proc.stdout)
    assert "Traceback" not in proc.stderr
