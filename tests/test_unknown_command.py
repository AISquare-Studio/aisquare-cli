"""#21: unknown subcommands fail loudly — did-you-mean + JSON usage errors.

The silent-no-op class: scripts that suppress stderr used to see an empty
stdout and exit 2 with no clue the verb was wrong. Detection of ``--json``
is purely the already-parsed runtime state (never argv scanning), so a
``--json`` trailing the typo deliberately falls back to the human path —
that tradeoff is pinned here too.
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

from aisquare.cli.app import app
from aisquare.core.paths import HOME_ENV_VAR

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(text: str) -> str:
    """Rendered output flattened for content asserts: no ANSI, no wrapping."""
    return " ".join(_ANSI.sub("", text).split())


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("NO_COLOR", "1")
    return work


def test_unknown_verb_suggests_on_stderr(runner: CliRunner) -> None:
    result = runner.invoke(app, ["task", "get"])
    assert result.exit_code == 2
    flat = _plain(result.stderr)
    assert "No such command 'get'" in flat
    assert "Did you mean 'show'" in flat  # the synonym difflib cannot reach
    assert result.stdout == ""  # human mode: stdout stays untouched


def test_json_unknown_verb_emits_the_stdout_contract(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--json", "task", "get"])
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"] == "unknown_command"
    assert payload["group"] == "task"
    assert payload["given"] == "get"
    assert payload["did_you_mean"][0] == "show"


def test_real_binary_json_error_survives_stderr_suppression(tmp_path: Path) -> None:
    # The literal incident: stderr dropped, stdout must still say what broke.
    env = os.environ.copy()
    env[HOME_ENV_VAR] = str(tmp_path / "home")
    proc = subprocess.run(
        [sys.executable, "-m", "aisquare", "--json", "task", "get"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=30,
    )
    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["error"] == "unknown_command" and payload["given"] == "get"


def test_root_typo_suggests_without_duplicating(runner: CliRunner) -> None:
    result = runner.invoke(app, ["taks"])
    assert result.exit_code == 2
    flat = _plain(result.stderr)
    assert "'task'" in flat
    assert flat.count("Did you mean") == 1  # typer's built-in line, not two


def test_alias_group_gets_suggestions_and_group_name(runner: CliRunner) -> None:
    human = runner.invoke(app, ["ctx", "lst"])
    assert human.exit_code == 2
    assert "'list'" in _plain(human.stderr)

    machine = runner.invoke(app, ["--json", "ctx", "lst"])
    assert machine.exit_code == 2
    payload = json.loads(machine.stdout)
    assert payload["group"] == "ctx" and payload["did_you_mean"] == ["list"]


def test_typoed_option_under_json_is_a_usage_error(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--json", "task", "list", "--jsn"])
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"] == "usage"
    assert "--jsn" in payload["message"]


def test_typoed_option_without_json_stays_human(runner: CliRunner) -> None:
    result = runner.invoke(app, ["task", "list", "--jsn"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "--jsn" in _plain(result.stderr)


def test_trailing_json_after_the_typo_falls_back_to_human(runner: CliRunner) -> None:
    # click never parses past the unknown verb, so the trailing --json is
    # invisible by design (no argv scanning) — pinned: human error, clean
    # stdout, and the help text tells scripts to lead with --json.
    result = runner.invoke(app, ["task", "get", "--json"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "No such command 'get'" in _plain(result.stderr)


def test_json_help_says_lead_with_json(runner: CliRunner) -> None:
    result = runner.invoke(app, ["task", "show", "-h"])
    assert result.exit_code == 0
    assert "before the subcommand" in _plain(result.output)
