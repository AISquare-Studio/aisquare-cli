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
import shutil
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
    _as_recorded,
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


# --- #105's reopen 1 (tester-r9f, seq 11381): a row 0.9.2 recorded, replayed ----------------


def _as_0_9_2_recorded(row: FleetAgent, args: list[str], *, slot: int) -> FleetAgent:
    """``row`` as 0.9.2 recorded its spawn with ``args``: kept with the agent's arguments, and
    on the window's line among its launch's own options, which had no ``--``
    (services/fleet.py:4077 and 4294 at 6a77e650: ``selfcli.argv_for``'s ``python -P -m
    aisquare launch ROLE <flags> <args>``, recorded whole), its row naming the default
    ``slot``."""
    assert row.launch_spec is not None
    line = ["/opt/uv/tools/aisquare-cli/bin/python", "-P", "-m", "aisquare", "launch", row.role]
    line += ["--command", "claude"]
    line += ["--account", str(slot), "--name", row.label, *args]
    spec = row.launch_spec.model_copy(
        update={
            "extra_args": list(args),
            "command": line,
            "recorded_by": "0.9.2",
            "account_slot": slot,
            "account_pinned": False,
            "binary": "claude",
        }
    )
    with store_session() as store:
        upserted = store.upsert_fleet_agent(
            row.model_copy(update={"launch_spec": spec, "account_slot": slot, "binary": "claude"})
        )
    return _as_recorded(upserted)


def _replayed(project: ProjectInfo, tmux: FakeTmux, row: FleetAgent, how: str) -> FleetAgent:  # noqa: F811
    """``row`` restarted, switched to account 2, or forked, as the head does each."""
    tmux.die(row.pane_id, 0)
    if how == "restart":
        return fleet_service.restart(project, row.label).started
    if how == "switch":
        return fleet_service.switch(project, row.label, to="2").started
    return fleet_service.hand_off(project, row.label, fresh=True).started.agent


@pytest.mark.parametrize("how", ["restart", "switch", "fork"])
def test_a_0_9_2_rows_own_a_is_the_account_its_replay_runs_and_never_the_agents(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    three_accounts: list[int],
    how: str,
) -> None:
    """tester-r9f's repro: on 0.9.2, `fleet spawn agent -a 3 -e PROBE_U=1` ran account 3, its
    row said 4, and its own restart read `-a 3` again. The head's ran 4 and gave claude
    `-a 3`. Now a restart and a fork run 3 and say so; a switch runs the account it names.
    The pair is set, and a first-message file the record held is dropped, as 0.9.2's window
    took it (the spawn's, long gone): neither reaches claude."""
    row = fleet_service.spawn(project, "agent", account="4", worktree=False).agent
    recorded = ["-a", "3", "-e", "PROBE_U=1", "--first-prompt-file", "/gone/prompt.txt"]
    row = _as_0_9_2_recorded(row, recorded, slot=4)

    started = _replayed(project, tmux, row, how)

    command = _command(tmux)
    options, agents = command[: command.index("--")], _after_the_options(command)
    runs = "2" if how == "switch" else "3"
    assert _flag(command, "--account") == runs, command
    assert options[options.index("PROBE_U=1") - 1] == "--env", options
    assert not {"-a", "3", "--first-prompt-file", "PROBE_U=1"} & set(agents), agents
    assert started.account_slot == int(runs), "the row names what runs"
    assert started.launch_spec is not None
    assert "-a" not in started.launch_spec.extra_args, "recorded as this release reads it"


def test_a_0_9_2_rows_own_command_is_the_binary_its_restart_runs(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    three_accounts: list[int],
) -> None:
    """On 0.9.2, `fleet spawn agent --command altclaude` ran altclaude, recorded
    ['--command', 'altclaude'], and its own restart ran altclaude. The head's ran claude with
    `--command altclaude` among claude's arguments."""
    # Its suffix kept: on Windows the fake is `claude.cmd`, and `which` finds `altclaude.cmd`
    # for `altclaude` by PATHEXT, as it finds a real one.
    shutil.copy2(claude_on_path, claude_on_path.with_name("altclaude" + claude_on_path.suffix))
    row = fleet_service.spawn(project, "agent", worktree=False).agent
    row = _as_0_9_2_recorded(row, ["--command", "altclaude"], slot=4)

    started = _replayed(project, tmux, row, "restart")

    command = _command(tmux)
    assert _flag(command, "--command") == "altclaude", command
    assert "altclaude" not in _after_the_options(command)
    assert started.binary == "altclaude", "the row names what runs"


def test_this_releases_record_keeps_an_a_after_its_windows_separator_as_the_agents(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    three_accounts: list[int],
) -> None:
    """A record whose window line put `-a 2` after its `--` gave it to the agent, as Codex's
    `-- -a never` is: its restart gives it to the agent again and runs the row's account.
    Read by where the line put it, not by the version it names: builds of this release before
    its version moved recorded "0.9.2" too."""
    row = fleet_service.spawn(
        project, "agent", account="4", worktree=False, agent_args=["-a", "2"]
    ).agent
    assert row.launch_spec is not None and row.launch_spec.extra_args == ["-a", "2"]
    spec = row.launch_spec.model_copy(update={"recorded_by": "0.9.2"})
    with store_session() as store:
        row = _as_recorded(store.upsert_fleet_agent(row.model_copy(update={"launch_spec": spec})))

    started = _replayed(project, tmux, row, "restart")

    command = _command(tmux)
    assert _flag(command, "--account") == "4", command
    agents = _after_the_options(command)
    assert agents[agents.index("-a") + 1] == "2", agents
    assert started.account_slot == 4


# --- the reopen's pin gaps ---------------------------------------------------------------


def test_a_separator_among_the_services_arguments_leaves_an_env_pair_to_the_agent(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    three_accounts: list[int],
) -> None:
    """The Spawn dialog's path, and a role's own arguments: a `--` among what the service is
    given ends the window launch's options, and `-e K=V` after it is the agent's."""
    fleet_service.spawn(project, "agent", worktree=False, agent_args=["--", "-e", "PROBE_S=1"])

    command = _command(tmux)
    assert "PROBE_S=1" not in command[: command.index("--")], command
    assert "PROBE_S=1" in _after_the_options(command)


def test_the_service_refuses_a_malformed_pair_where_no_cli_checked_it(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    three_accounts: list[int],
) -> None:
    """The Spawn dialog's path and a replay's never pass through `fleet spawn`'s check."""
    with pytest.raises(fleet_service.FleetError, match="expected KEY=VALUE"):
        fleet_service.spawn(project, "agent", worktree=False, agent_args=["--env", "NO_PAIR"])
    assert tmux.spawned == []


def test_a_number_is_an_account_for_codex_too_and_an_unknown_one_is_said_as_such(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    """`-a 9` on a Codex role names account 9, which there is none of: said as that, with no
    word of Codex's own -a, which takes no number."""
    monkeypatch.chdir(project.root)
    result = runner.invoke(app, ["fleet", "spawn", "coder", "--agent", "codex", "-a", "9"])

    assert result.exit_code == 1, result.output
    said = " ".join(result.output.split())
    assert "slot 9" in said and "pass -a" not in said, said
    assert tmux.spawned == []


def test_an_agent_with_no_a_of_its_own_is_told_nothing_of_one(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    three_accounts: list[int],
    runner: CliRunner,
) -> None:
    """Claude Code has no -a: `-a never` is an account it has none of, and nothing more."""
    result = runner.invoke(app, ["fleet", "spawn", "agent", "-a", "never"])

    assert result.exit_code == 1, result.output
    said = " ".join(result.output.split())
    assert "'never'" in said and "pass -a" not in said, said
    assert tmux.spawned == []


def test_a_first_message_file_in_a_spawns_own_arguments_is_refused_before_any_window(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    three_accounts: list[int],
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    """0.9.2's window launch read one; the head gave it to claude. It is the fleet's own:
    refused, pointing at --prompt, and the file is left as it was."""
    file = tmp_path / "prompt.txt"
    file.write_text("hello", encoding="utf-8")

    result = runner.invoke(app, ["fleet", "spawn", "agent", "--first-prompt-file", str(file)])

    assert result.exit_code == 1, result.output
    assert "--prompt" in " ".join(result.output.split()), result.output
    assert tmux.spawned == [] and file.read_text(encoding="utf-8") == "hello"
