"""`fleet spawn ROLE -a N` runs account N, as 0.9.2's did (#105, tester-r9f's seq 11339, B3-4).

0.9.2's spawn ran its window as `aisquare launch ROLE --account <default> … <the spawn's own
arguments>` (services/fleet.py:4077 at 6a77e650), and that `launch` read its own options among
them: `-a 3` was the account, the last value winning, and `-e K=V` an environment pair. The head
ends the window's options with `--`, so both reached claude as its own arguments, and the agent
ran the default account. Now `-a` and `-e`/`--env` are `fleet spawn`'s own, as they are
`launch`'s, and the pairs reach the window's `launch` again, recorded so a restart reads them.
`--command` and `-c` are `--bin`, as 0.9.2's window took them and the head's `launch` does;
Codex's `-c KEY=VALUE` stays Codex's by its shape, as `launch` keeps it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import claude_accounts as core
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo
from aisquare.services import fleet as fleet_service
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_fleet_service import (  # noqa: F401 - fixtures, by name
    FakeTmux,
    _command,
    _flag,
    claude_on_path,
    project,
    repo,
    tmux,
)


@pytest.fixture
def three_accounts(project: ProjectInfo, monkeypatch: pytest.MonkeyPatch) -> list[int]:  # noqa: F811
    """A Claude-only home with accounts 2, 3 and 4 (the repro's), the spawn run from the
    project."""
    slots = [core.create_account().slot for _ in range(3)]
    monkeypatch.chdir(project.root)
    return slots


def _after_the_options(command: list[str]) -> list[str]:
    """What the window's `launch` hands the agent: everything after its `--`."""
    return command[command.index("--") + 1 :]


def _row(where: ProjectInfo) -> FleetAgent:
    with store_session() as store:
        [row] = store.fleet_agents(where.id, live_only=True)
    return row


def test_a_spawn_with_a_short_a_runs_that_account_and_its_row_says_it(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    three_accounts: list[int],
    runner: CliRunner,
) -> None:
    """The repro: accounts 2, 3, 4 and `fleet spawn agent -a 3`: 0.9.2 ran claude-accounts/3
    with no -a in claude's argv. The head ran the default and passed `-a 3` to claude."""
    result = runner.invoke(app, ["fleet", "spawn", "agent", "-a", "3"])

    assert result.exit_code == 0, result.output
    command = _command(tmux)
    assert _flag(command, "--account") == "3", command
    assert "-a" not in _after_the_options(command), "never claude's argument"
    assert _row(project).account_slot == 3, "the row names it (0.9.2's said the default)"


def _launchs_options_and_the_agents(command: list[str]) -> tuple[list[str], list[str]]:
    cut = command.index("--")
    return command[:cut], command[cut + 1 :]


def test_an_environment_pair_reaches_the_windows_launch_and_its_replay(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    three_accounts: list[int],
    runner: CliRunner,
) -> None:
    """`-e K=V` and `--env=K=V` are `fleet spawn`'s own, as `launch`'s are, so an option of the
    spawn's after them is read too (the step-5 repro: `spawn coder -e PROBE_ENV=1 --label
    coder-env`, which the head refused). They reach the window's `launch` before its `--`, as
    0.9.2's window read them, and a restart gives them again."""
    result = runner.invoke(
        app,
        ["fleet", "spawn", "agent", "-e", "PROBE_ONE=1", "--env=PROBE_TWO=2", "--label", "envy"],
    )

    assert result.exit_code == 0, result.output
    row = _row(project)
    assert row.label == "envy"
    tmux.die(row.pane_id, 0)
    fleet_service.restart(project, row.label)
    for command in (_command(tmux, 0), _command(tmux, -1)):
        options, agents = _launchs_options_and_the_agents(command)
        for pair in ("PROBE_ONE=1", "PROBE_TWO=2"):
            assert options[options.index(pair) - 1] == "--env", options
        assert not any("PROBE_" in part for part in agents), agents
    assert len(tmux.spawned) == 2, "the spawn's window, then the restart's"


def test_a_malformed_pair_is_refused_before_any_window_opens(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    three_accounts: list[int],
    runner: CliRunner,
) -> None:
    """As `launch --env` refuses it, and with its code."""
    result = runner.invoke(app, ["--json", "fleet", "spawn", "agent", "-e", "NO_EQUALS_SIGN"])

    assert result.exit_code == 1, result.output
    assert json.loads(result.output)["error"] == "bad_env_pair", result.output
    assert tmux.spawned == []


@pytest.mark.parametrize("spelling", ["--command", "-c"])
def test_command_and_c_are_the_binary_as_0_9_2_s_window_read_them(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    three_accounts: list[int],
    runner: CliRunner,
    spelling: str,
) -> None:
    """`--command X` and `-c X` are `--bin X`, as 0.9.2's window launch read them and the
    head's `launch` reads them. Claude Code's own -c (--continue) is given after --; a bare
    -c is refused before any window opens (0.9.2's window refused it too, as a binary with no
    value, once open)."""
    named = runner.invoke(app, ["fleet", "spawn", "agent", spelling, str(claude_on_path)])
    bare = runner.invoke(app, ["fleet", "spawn", "agent", "-c"])
    continued = runner.invoke(app, ["fleet", "spawn", "agent", "--", "-c"])

    assert named.exit_code == 0, named.output
    assert bare.exit_code == 2, bare.output
    assert continued.exit_code == 0, continued.output
    assert len(tmux.spawned) == 2, "the named one and the continued one"
    first = _command(tmux, 0)
    assert _flag(first, "--command") == str(claude_on_path), first
    assert spelling not in _after_the_options(first), "never the agent's"
    assert "-c" in _after_the_options(_command(tmux, -1)), "the agent's own -c, after --"


def test_a_codex_spawns_a_with_codexs_own_word_says_where_codexs_goes(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    """For a Codex role too, `-a` is the account (one meaning for the flag, as `launch` gives
    it). `-a never`, Codex's own approval word, is no account: refused before any window
    opens, saying where Codex's goes, as `launch` says it. Its own -a after -- reaches it."""
    monkeypatch.chdir(project.root)
    refused = runner.invoke(app, ["fleet", "spawn", "coder", "--agent", "codex", "-a", "never"])

    assert refused.exit_code == 1, refused.output
    assert "pass -a never after --" in refused.output, refused.output
    assert tmux.spawned == []

    given = runner.invoke(app, ["fleet", "spawn", "coder", "--agent", "codex", "--", "-a", "never"])
    assert given.exit_code == 0, given.output
    after = _after_the_options(_command(tmux))
    assert after[after.index("-a") + 1] == "never", after
