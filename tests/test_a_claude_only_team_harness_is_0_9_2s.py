"""``aisquare team harness`` on a Claude-only machine is 0.9.2's, byte for byte (C1's audit).

#113 rewrote the matrix's text line for every role:
``coder      claude-code [default] sonnet→opus effort=high → sonnet [optimistic]
bin=claude [default]``. A Claude-only user gained the agent and an always-on ``bin=``, and
lost 0.9.2's effort offset ``(+1)`` and the ``+Nargs`` count of a role's bound arguments.
The column alignment went too. None of it was in [0.10.0] (the manager's ruling, seq 11720).

The agent (``agent=claude-code [default]``) now comes after the model, and only with
accounts of more than one agent listed: the test ``aisquare accounts`` and the Accounts
page name agents by. :data:`HARNESS_0_9_2` and :data:`BOUND_0_9_2` are what 0.9.2
(6a77e650, under typer 0.28.0) printed in a scratch home: with nothing bound, and with the
three bindings :func:`_bind` makes (the live diff, R14 rig ``harness-diff.sh``)."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import claude_accounts as core
from aisquare.models import ClaudeAccount

HARNESS_0_9_2 = """\
base effort: high (default)
planner    fable→opus→sonnet    effort=high      ( 0) → fable [optimistic]
coder      sonnet→opus          effort=high      ( 0) → sonnet [optimistic]
runner     sonnet→opus          effort=high      ( 0) → sonnet [optimistic]
validator  fable→opus           effort=xhigh     (+1) → fable [optimistic]
manager    fable→opus→sonnet    effort=high      ( 0) → fable [optimistic]
tester     sonnet→opus          effort=high      ( 0) → sonnet [optimistic]
reviewer   sonnet→opus          effort=high      ( 0) → sonnet [optimistic]
ui-tester  sonnet→opus          effort=high      ( 0) → sonnet [optimistic]  role_args=--chrome
Resolution shown without probing — `aisquare team spawn <role>` verifies live.
"""

BOUND_0_9_2 = {
    "coder": (
        "coder      sonnet→opus          effort=high      ( 0) → sonnet [optimistic]"
        "  bin=claude2 [config]  env=FOO+2args"
    ),
    "tester": (
        "tester     sonnet→opus          effort=high      ( 0) → sonnet [optimistic]  env=GH_TOKEN"
    ),
    "reviewer": (
        "reviewer   sonnet→opus          effort=high      ( 0) → sonnet [optimistic]  env=+1args"
    ),
}


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    # The matrix as a terminal of any width reads it: Rich never folds a role's line.
    monkeypatch.setenv("COLUMNS", "250")
    for name in ("AISQUARE_MODEL_CODER", "AISQUARE_EFFORT_CODER", "ANTHROPIC_MODEL"):
        monkeypatch.delenv(name, raising=False)
    return CliRunner()


def _bind(runner: CliRunner, tmp_path: Path) -> None:
    token = tmp_path / "tok"
    token.write_text("tok\n")
    coder = ["--bin", "claude2", "--env", "FOO=1", "--arg", "--verbose", "--arg", "--debug"]
    for argv in (
        ["team", "bind", "coder", *coder],
        ["team", "bind", "tester", "--env-file", f"GH_TOKEN={token}"],
        ["team", "bind", "reviewer", "--arg", "--x"],
    ):
        result = runner.invoke(app, argv)
        assert result.exit_code == 0, result.output


def test_a_claude_only_machines_matrix_is_0_9_2s(runner: CliRunner) -> None:
    result = runner.invoke(app, ["team", "harness"])
    assert result.exit_code == 0, result.output
    assert result.stdout == HARNESS_0_9_2


def test_a_claude_only_machines_bound_roles_read_as_0_9_2s(
    runner: CliRunner, tmp_path: Path
) -> None:
    """The offset, ``bin=`` for a binary that is not the default, and ``env=<keys>+<N>args``,
    as 0.9.2 printed them."""
    _bind(runner, tmp_path)
    result = runner.invoke(app, ["team", "harness"])
    assert result.exit_code == 0, result.output
    lines = {line.split()[0]: line for line in result.stdout.splitlines() if line[:1] != " "}
    for role, line in BOUND_0_9_2.items():
        assert lines[role] == line, result.stdout
    assert lines["planner"] == HARNESS_0_9_2.splitlines()[1], "an unbound role as before"


def test_beside_another_agents_accounts_each_role_says_its_agent_after_the_model(
    runner: CliRunner, tmp_path: Path
) -> None:
    core.create_account("codex")
    _bind(runner, tmp_path)
    result = runner.invoke(app, ["team", "harness"])
    assert result.exit_code == 0, result.output
    lines = {line.split()[0]: line for line in result.stdout.splitlines() if line[:1] != " "}
    assert lines["coder"] == (
        "coder      sonnet→opus          effort=high      ( 0) → sonnet [optimistic]"
        "  agent=claude-code [default]  bin=claude2 [config]  env=FOO+2args"
    ), result.stdout
    assert lines["validator"].endswith("(+1) → fable [optimistic]  agent=claude-code [default]")
    assert result.stdout.endswith(
        "Resolution shown without probing; native defaults stay with the selected agent.\n"
    )


# --- signed in: the binary's source is not the agent pick's rung (C1 reopen, seq 11753) -------
#
# A signed-in Claude-only user is the normal case, and the pins above ran with no login. With
# one, `agent_launch.resolve` copied the agent selection's rung ("accounts", or "account" for
# a role bound to one) into the default binary's source: `team harness` printed
# `bin=claude [accounts]` on every role, `team spawn` `·  binary claude [accounts]`, and both
# --json outputs said binary_source "accounts". 0.9.2 printed, in the same homes:

BOUND_SIGNED_0_9_2 = """\
base effort: high (default)
planner    fable→opus→sonnet    effort=high      ( 0) → fable [optimistic]
coder      sonnet→opus          effort=high      ( 0) → sonnet [optimistic]  env=X
runner     sonnet→opus          effort=high      ( 0) → sonnet [optimistic]
validator  fable→opus           effort=xhigh     (+1) → fable [optimistic]
manager    fable→opus→sonnet    effort=high      ( 0) → fable [optimistic]
tester     sonnet→opus          effort=high      ( 0) → sonnet [optimistic]
reviewer   sonnet→opus          effort=high      ( 0) → sonnet [optimistic]
ui-tester  sonnet→opus          effort=high      ( 0) → sonnet [optimistic]  bin=claude2 [config]  role_args=--chrome
Resolution shown without probing — `aisquare team spawn <role>` verifies live.
"""  # noqa: E501 - 0.9.2's ui-tester line, whole

#: 0.9.2's `team spawn coder` ✓ line, cut at its mission, then what follows it.
SPAWN_0_9_2 = {"plain": "", "managed": "  ·  env X [config]"}

#: The keys of 0.9.2's rows (``team harness --json``'s ``roles[]``, ``team spawn --json``).
HARNESS_ROW_KEYS_0_9_2 = {
    "binary", "binary_source", "default_args", "effort", "effort_offset", "effort_source",
    "env", "env_files", "extra_args", "ladder", "mission", "refusal", "resolves_to", "role",
    "source",
}  # fmt: skip
SPAWN_KEYS_0_9_2 = {
    "binary", "binary_source", "command", "denied_tools", "effort", "effort_source", "env",
    "env_files", "env_sources", "extra_args", "model", "not_added", "notes", "role",
    "skipped", "source",
}  # fmt: skip


def _signed_in(account: ClaudeAccount, email: str) -> None:
    """What a signed-in Claude Code leaves in a config directory: its identity, and a token
    good for hours (the suite's Keychain holds none)."""
    path = core.claude_json_path(account)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"oauthAccount": {"emailAddress": email}}))
    expires = int((datetime.now(UTC) + timedelta(hours=7)).timestamp() * 1000)
    token = {"accessToken": "sk-ant-oat01-test", "expiresAt": expires, "scopes": ["user:inference"]}
    credentials = core.credentials_path(account)
    credentials.parent.mkdir(parents=True, exist_ok=True)
    credentials.write_text(json.dumps({"claudeAiOauth": token}))


def _claude_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Claude Code on PATH, as on a machine that runs it: the accounts rung reads only the
    accounts of an agent installed here (``claude_accounts.usable_accounts``, by
    ``shutil.which``). tests/conftest.py's stand-in ``claude`` (no_real_fleet, which also
    fails a test that launches it) is POSIX-only, so on Windows nothing was found, no account
    was usable, and the pick fell to "default" (CI 43338150685, windows-py3.12 shard 4/10).
    A ``claude.cmd`` stands in there, found through PATHEXT as an installed claude.exe is."""
    if sys.platform != "win32":
        return
    folder = tmp_path / "claude-bin"
    folder.mkdir()
    (folder / "claude.cmd").write_text("@exit /b 97\r\n", encoding="utf-8")
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ.get('PATH', '')}")


@pytest.mark.parametrize(("home", "rung"), [("plain", "accounts"), ("managed", "account")])
def test_a_signed_in_claude_only_machine_reads_0_9_2s_binary_and_its_source(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: str, rung: str
) -> None:
    _claude_on_path(tmp_path, monkeypatch)
    _signed_in(core.default_account(), "plain@example.invalid")
    if home == "managed":
        for n in (2, 3):
            _signed_in(core.create_account(), f"slot{n}@example.invalid")
        for argv in (
            ["team", "bind", "coder", "--account", "2", "--env", "X=1"],
            ["team", "bind", "manager", "--account", "3"],
            ["team", "bind", "ui-tester", "--bin", "claude2"],
        ):
            result = runner.invoke(app, argv)
            assert result.exit_code == 0, result.output
    listed = runner.invoke(app, ["--json", "accounts"])
    assert any(row["signed_in"] for row in json.loads(listed.stdout)["accounts"]), listed.stdout

    text = runner.invoke(app, ["team", "harness"])
    assert text.exit_code == 0, text.output
    assert text.stdout == (HARNESS_0_9_2 if home == "plain" else BOUND_SIGNED_0_9_2)
    rows = json.loads(runner.invoke(app, ["--json", "team", "harness"]).stdout)["roles"]
    for row in rows:
        assert set(row) >= HARNESS_ROW_KEYS_0_9_2, row
        wrapper = home == "managed" and row["role"] == "ui-tester"
        assert (row["binary"], row["binary_source"]) == (
            ("claude2", "config") if wrapper else ("claude", "default")
        ), row

    spawn = runner.invoke(app, ["team", "spawn", "coder", "--no-probe"])
    assert spawn.exit_code == 0, spawn.output
    (tick,) = [line for line in spawn.stdout.splitlines() if line.startswith("✓ coder:")]
    assert "binary" not in tick and tick.endswith(
        "implements them to their acceptance criteria" + SPAWN_0_9_2[home]
    ), tick
    shown = json.loads(
        runner.invoke(app, ["--json", "team", "spawn", "coder", "--no-probe"]).stdout
    )
    assert set(shown) >= SPAWN_KEYS_0_9_2, shown
    assert (shown["binary"], shown["binary_source"]) == ("claude", "default")
    # The agent pick's rung: the signed-in plain slot's ("accounts"), or the role's bound
    # account's ("account"). It stays in agent_source, never the binary's source.
    assert shown["agent_source"] == rung, shown["agent_source"]


#: 0.9.2's note for a role bound to a wrapper binary (`--bin claude2`), as `team spawn` said it.
WITHHELD_0_9_2 = (
    "coder: --disallowedTools=Artifact ArtifactComments PushNotification ReportFindings "
    "ScheduleWakeup SendFeedback ShareOnboardingGuide Workflow, skill_listing names withheld: "
    "'claude2' is not claude"
)


def test_a_wrapper_bindings_withheld_deny_list_is_said_as_0_9_2_said_it(
    runner: CliRunner, tmp_path: Path
) -> None:
    """The note #113 reworded for every agent (``the deny list (…)``) names Claude Code's flag
    again, as 0.9.2 did; Codex's, which has no such flag, keeps the neutral words."""
    _bind(runner, tmp_path)
    spawn = runner.invoke(app, ["team", "spawn", "coder", "--no-probe"])
    assert spawn.exit_code == 0, spawn.output
    # Said on stderr, as 0.9.2 said it, beside the line on stdout.
    assert WITHHELD_0_9_2 in spawn.output.splitlines(), spawn.output
    shown = json.loads(
        runner.invoke(app, ["--json", "team", "spawn", "coder", "--no-probe"]).stdout
    )
    assert WITHHELD_0_9_2.removeprefix("coder: ") in shown["notes"], shown["notes"]
