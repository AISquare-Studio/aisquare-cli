"""Naming one of two agents' accounts in one number (#100's reopen, seq 11287, A-F).

A home where 0.9.2, after a rollback, numbered its own Claude Code account as a Codex account
an earlier build made (``collided``: Codex 5 ranked, Claude Code 5 last). Then:

A. every read writes the reserve a Codex account lacks, so 0.9.2's next add skips its number;
C. an email, an alias or ``--agent`` acts on exactly the account it names, or is refused
   saying why: the one listed last has no place in the order of its own, so it is neither
   moved nor made the default; receipts name the account acted on; an alias is one account's
   across agents;
D. ``codex:5`` names one of them in every command that takes a slot, as the refusals say;
   ``team bind --agent claude-code --account 5`` binds Claude Code's;
E. the move-back line of a Claude-only home is exactly 0.9.2's warning, and ``--json`` carries
   the ambiguity's code and message;
F. doctor names the number and the commands that give the one listed last its own.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import claude_accounts as core
from aisquare.core.config import load_config
from aisquare.models import ClaudeAccount
from aisquare.services import agent_launch, diagnostics, lifecycle
from aisquare.services import claude_accounts as service
from aisquare.services import team as team_service
from tests.test_a_removed_slots_place_goes_with_it import ticking  # noqa: F401 - fixture
from tests.test_claude_accounts import _sign_in as _claude_sign_in
from tests.test_one_priority_order_across_agents import (  # noqa: F401 - fixture, by name
    _claude,
    machine,
)
from tests.test_two_agents_accounts_in_one_number import (  # noqa: F401 - fixture, by name
    CODEX,
    _add_on_092,
    _codex_account_of_an_earlier_build,
    _order,
    collided,
)

CLAUDE = "claude-code"
Pair = tuple[ClaudeAccount, ClaudeAccount]


def _said(result: object) -> str:
    return " ".join(str(getattr(result, "output", "")).split())


def _row(agent: str, slot: int) -> ClaudeAccount:
    return next(a for a in service.list_accounts() if (a.agent, a.slot) == (agent, slot))


@pytest.fixture
def signed(collided: Pair) -> Pair:  # noqa: F811
    """The tester's home: Claude Code 5 signed in as x1@example.com, and Codex 5 moved to
    rank 3, so the plain claude is rank 1 and a wrong rank write shows."""
    codex, claude = collided
    _claude_sign_in(claude, "x1@example.com")
    service.move("codex:5", "down")
    service.move("codex:5", "down")
    assert _order()[:3] == [(CLAUDE, 1), (CLAUDE, 4), (CODEX, 5)]
    assert _order()[-1] == (CLAUDE, 5)
    return codex, claude


# --- A: every read reserves the number of a Codex account that has none -----------------------


def test_a_read_reserves_the_number_of_a_codex_account_an_earlier_build_made(
    machine: Path,  # noqa: F811
) -> None:
    codex = _codex_account_of_an_earlier_build()
    reserve = core.accounts_root() / str(codex.slot)
    assert not reserve.exists()

    service.list_accounts()  # any read: `aisquare accounts`, a launch's pick, asq's frame

    assert reserve.is_file(), "the read wrote the reserve the earlier build never did"
    assert json.loads(reserve.read_text(encoding="utf-8")) == {
        "slot": codex.slot,
        "agent": CODEX,
        "reserved": True,
    }
    assert _add_on_092().slot == codex.slot + 1, "0.9.2's next add takes the next number"


def test_a_read_never_writes_where_0_9_2_already_added_an_account(collided: Pair) -> None:  # noqa: F811
    _codex, claude = collided
    service.list_accounts()

    assert claude.config_dir.is_dir() and (claude.config_dir / core.MARKER).is_file()


def test_a_read_of_a_claude_only_home_writes_nothing_in_its_accounts_folder(
    machine: Path,  # noqa: F811
) -> None:
    _claude("two@example.com")
    before = sorted(path.name for path in core.accounts_root().iterdir())

    service.list_accounts()

    assert sorted(path.name for path in core.accounts_root().iterdir()) == before == ["2"]


# --- C: naming one acts on that one, or is refused saying why ----------------------------------

REFUSED_RANK = (
    "Claude Code's account 5 shares its number with Codex's, whose place in the order it is: "
    "it is listed last and cannot be moved or made the default. To give it a number of its "
    "own: aisquare accounts remove claude-code:5, then aisquare accounts add"
)


@pytest.mark.parametrize(
    "argv",
    [
        ["accounts", "default", "x1@example.com"],
        ["accounts", "default", "--agent", CLAUDE, "5"],
        ["accounts", "default", "claude-code:5"],
        ["accounts", "move", "x1@example.com", "up"],
        ["accounts", "move", "x1@example.com", "top"],
        ["accounts", "move", "claude-code:5", "up"],
        ["accounts", "order", "claude-code:5"],
    ],
)
def test_a_place_for_the_account_listed_last_is_refused_and_nothing_moves(
    signed: Pair, runner: CliRunner, argv: list[str]
) -> None:
    before = _order()

    result = runner.invoke(app, argv)

    assert result.exit_code == 1, result.output
    assert _said(result) == f"✗ {REFUSED_RANK}"
    assert _order() == before, "no rank written: the read gave it to the Codex account"
    picked = service.choose(None, role="coder")
    assert picked.account is not None and picked.account.agent == CLAUDE, "launches stay put"


def test_the_ranked_one_moves_and_becomes_the_default_by_its_email_or_codex_5(
    signed: Pair, runner: CliRunner
) -> None:
    moved = runner.invoke(app, ["accounts", "move", "codex@example.com", "up"])
    assert moved.exit_code == 0, moved.output
    assert _order()[:3] == [(CLAUDE, 1), (CODEX, 5), (CLAUDE, 4)]

    made = runner.invoke(app, ["accounts", "default", "codex:5"])

    assert made.exit_code == 0, made.output
    assert _said(made).startswith(
        "✓ machine default: slot codex:5 · account 5 (codex@example.com) · Codex — moved to rank 1"
    ), "the receipt names the account acted on"
    assert _order()[0] == (CODEX, 5) and _order()[-1] == (CLAUDE, 5)


def test_an_alias_is_one_accounts_across_agents(signed: Pair) -> None:
    service.set_alias("x1@example.com", "work")
    service.set_alias("two@example.com", "dev")

    for ref, alias in (("codex@example.com", "work"), ("codex:5", "WORK"), ("codex:5", "dev")):
        with pytest.raises(service.AccountsError, match="is already taken"):
            service.set_alias(ref, alias)

    assert _row(CODEX, 5).alias is None
    assert (service.resolve("work").agent, service.resolve("work").slot) == (CLAUDE, 5)


def test_an_aliases_receipt_and_store_are_the_named_accounts(
    signed: Pair, runner: CliRunner
) -> None:
    result = runner.invoke(app, ["accounts", "alias", "x1@example.com", "work"])

    assert result.exit_code == 0, result.output
    assert _said(result) == "✓ slot claude-code:5 is now called work"
    assert (_row(CLAUDE, 5).alias, _row(CODEX, 5).alias) == ("work", None)

    cleared = runner.invoke(app, ["accounts", "alias", "work", "--clear"])
    assert _said(cleared) == "✓ slot claude-code:5: alias cleared"


def test_disable_and_enable_act_on_the_named_account_and_say_which(
    signed: Pair, runner: CliRunner
) -> None:
    result = runner.invoke(app, ["accounts", "disable", "x1@example.com"])

    assert result.exit_code == 0, result.output
    assert _said(result).startswith(
        "✓ slot claude-code:5 · account 5 (x1@example.com) is disabled — never picked"
    )
    assert (_row(CLAUDE, 5).disabled, _row(CODEX, 5).disabled) == (True, False)

    enabled = runner.invoke(app, ["accounts", "enable", "claude-code:5"])
    assert _said(enabled) == "✓ slot claude-code:5 · account 5 (x1@example.com) is enabled"
    assert (_row(CLAUDE, 5).disabled, _row(CODEX, 5).disabled) == (False, False)


def test_remove_by_alias_removes_that_one_and_the_other_keeps_its_place(
    signed: Pair, runner: CliRunner
) -> None:
    service.set_alias("codex:5", "work")

    result = runner.invoke(app, ["accounts", "remove", "work"])

    assert result.exit_code == 0, result.output
    assert {(a.agent, a.slot) for a in service.list_accounts() if a.slot == 5} == {(CLAUDE, 5)}


# --- D: `codex:5` names one in every command that takes a slot ---------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["accounts", "alias", "codex:5", "mine"],
        ["accounts", "disable", "codex:5"],
        ["accounts", "enable", "codex:5"],
        ["accounts", "move", "codex:5", "top"],
        ["accounts", "default", "codex:5"],
        ["accounts", "order", "codex:5"],
        ["accounts", "usage", "codex:5"],
        ["accounts", "remove", "codex:5"],
    ],
)
def test_every_accounts_command_takes_the_agents_own_form(
    signed: Pair, runner: CliRunner, argv: list[str]
) -> None:
    result = runner.invoke(app, argv)

    assert result.exit_code == 0, (argv, result.output)
    assert (_row(CLAUDE, 5).alias, _row(CLAUDE, 5).disabled) == (None, False), "never the other"


def test_a_launch_and_a_spawn_take_it_too(signed: Pair) -> None:
    for ref, agent in (("codex:5", CODEX), ("claude-code:5", CLAUDE)):
        # `launch coder --account codex:5`, and `fleet spawn`'s (``fleet._spawn_agent``)
        launch = agent_launch.resolve("coder", account=ref, measure=False)
        assert launch.adapter.id == agent, ref
        picked = service.choose(ref, role="coder", agent=launch.adapter.id).account
        assert picked is not None and (picked.agent, picked.slot) == (agent, 5), ref


def test_two_accounts_on_one_email_with_no_alias_are_each_named(
    signed: Pair, runner: CliRunner
) -> None:
    _codex, claude = signed
    _claude_sign_in(claude, "codex@example.com")  # one person's address on both agents

    refused = runner.invoke(app, ["accounts", "remove", "codex@example.com"])
    assert refused.exit_code == 1
    assert "name one as codex:5 or claude-code:5" in _said(refused)

    result = runner.invoke(app, ["accounts", "remove", "claude-code:5"])

    assert result.exit_code == 0, result.output
    assert [(a.agent, a.slot) for a in service.list_accounts() if a.slot == 5] == [(CODEX, 5)]


def test_a_codex_account_with_no_login_and_no_alias_is_named_as_codex_5(
    signed: Pair, runner: CliRunner
) -> None:
    codex, _claude_5 = signed
    (codex.config_dir / "auth.json").unlink()

    result = runner.invoke(app, ["accounts", "alias", "codex:5", "spare"])

    assert result.exit_code == 0, result.output
    assert (_row(CODEX, 5).alias, _row(CLAUDE, 5).alias) == ("spare", None)


def test_team_bind_with_agent_claude_code_binds_its_own_account_in_the_number(
    signed: Pair, runner: CliRunner
) -> None:
    team_service.activate(Path.cwd())

    refused = runner.invoke(app, ["team", "bind", "coder", "--account", "5"])
    assert refused.exit_code == 1 and "slot 5 is two accounts" in _said(refused)

    result = runner.invoke(app, ["team", "bind", "coder", "--agent", CLAUDE, "--account", "5"])

    assert result.exit_code == 0, result.output
    profile = load_config().team.profiles["coder"]
    assert (profile.agent, profile.account) == (CLAUDE, "5")
    assert agent_launch.resolve("coder", measure=False).adapter.id == CLAUDE


def test_an_alias_cannot_read_as_an_agents_slot(machine: Path) -> None:  # noqa: F811
    _claude("two@example.com")

    with pytest.raises(ValueError, match="reads as an agent's slot"):
        service.set_alias("2", "codex:5")
    with pytest.raises(service.NoSuchAccount, match="not a slot, an alias or a signed-in email"):
        service.resolve("gemini:5")  # no such agent: a name like any other


# --- E: the Claude-only move-back line, exactly; the --json refusal ---------------------------

CLAUDE_ONLY_MOVE_BACK = (
    "0.9.2 opens this home's store as it is and keeps every row, agent and setting; but an "
    "account order or default you set while on 0.9.2 is not kept when you upgrade again, and "
    "an account you add there is ranked last"
)


def _move_back_plan() -> lifecycle.UpgradePlan:
    plan = lifecycle.UpgradePlan.__new__(lifecycle.UpgradePlan)
    object.__setattr__(plan, "target", "0.9.2")
    object.__setattr__(plan, "current", "0.10.0")
    return plan


def test_a_claude_only_homes_move_back_line_is_exactly_the_one_it_was(
    machine: Path,  # noqa: F811
) -> None:
    _claude("two@example.com")

    assert _move_back_plan().store_warning == CLAUDE_ONLY_MOVE_BACK
    core.create_account(CODEX)
    assert _move_back_plan().store_warning == (
        f"{CLAUDE_ONLY_MOVE_BACK}; it may give it the number of a Codex account, which this "
        "release then lists last and asks which you mean"
    )


@pytest.mark.parametrize(
    ("argv", "ref"),
    [
        (["accounts", "remove", "5"], "5"),
        (["accounts", "disable", "5"], None),
        (["accounts", "default", "5"], None),
    ],
)
def test_a_json_refusal_carries_the_ambiguity_and_its_message(
    signed: Pair, runner: CliRunner, argv: list[str], ref: str | None
) -> None:
    result = runner.invoke(app, ["--json", *argv])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "ambiguous_account"
    assert payload.get("ref") == ref
    assert payload["detail"].startswith("slot 5 is two accounts, Codex's account 5 (")
    assert payload["detail"].endswith(
        "Name one as codex:5 or claude-code:5; see: aisquare accounts"
    )


# --- F: doctor names the number and the commands that fix it -----------------------------------


def test_doctor_names_a_shared_number_and_the_commands_that_fix_it(signed: Pair) -> None:
    [row] = diagnostics._shared_number_checks()

    assert (row.name, row.status) == ("account-numbers", "warn")
    assert row.detail == (
        "slot 5 is two accounts, Codex's (codex:5) and Claude Code's (claude-code:5): name each "
        "as shown; the one listed last cannot be moved or made the default"
    )
    assert row.fix == "aisquare accounts remove claude-code:5, then aisquare accounts add"
    assert any(check.name == "account-numbers" for check in diagnostics.doctor())


def test_doctor_has_no_such_row_where_no_number_is_shared(machine: Path) -> None:  # noqa: F811
    _claude("two@example.com")
    core.create_account(CODEX)

    assert diagnostics._shared_number_checks() == []
