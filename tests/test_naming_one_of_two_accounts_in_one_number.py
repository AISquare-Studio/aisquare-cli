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
from typer.testing import CliRunner, Result

from aisquare.cli import launch as launch_cli
from aisquare.cli.app import app
from aisquare.core import claude_accounts as core
from aisquare.core.config import load_config
from aisquare.models import ClaudeAccount
from aisquare.services import agent_launch, diagnostics, lifecycle
from aisquare.services import claude_accounts as service
from aisquare.services import settings as settings_service
from aisquare.services import team as team_service
from tests.test_a_removed_slots_place_goes_with_it import ticking  # noqa: F401 - fixture
from tests.test_claude_accounts import _sign_in as _claude_sign_in
from tests.test_codex_accounts import _sign_in as _codex_sign_in
from tests.test_one_priority_order_across_agents import (  # noqa: F401 - fixture, by name
    _claude,
    _codex,
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


#: `team bind`'s advice for a bare 5 two agents hold (#100's reopen 4, B4): `--agent
#: claude-code --account 5` first, which 0.9.2 reads after a rollback, then claude-code:5.
BIND_ONE_OF_5 = (
    "Bind one as --agent claude-code --account 5 or claude-code:5, or as --agent codex "
    "--account 5 or codex:5; see: aisquare accounts"
)


def _launch(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, role: str
) -> tuple[Result, dict[str, str]]:
    """`aisquare launch ROLE`, as the bound role's window runs it: its exec held, and the
    environment it would have handed over kept (empty when it refused)."""
    seen: dict[str, str] = {}
    monkeypatch.setattr(launch_cli, "_exec", lambda _binary, _argv, env: seen.update(env))
    return runner.invoke(app, ["launch", role]), seen


def _runs(env: dict[str, str], account: ClaudeAccount) -> bool:
    """Whether the launch ran ``account``: its home is the one the environment names."""
    home = account.config_dir.resolve()
    return any(Path(value).resolve() == home for value in env.values() if Path(value).is_absolute())


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


# --- #100's reopen 2 (seq 11300) -------------------------------------------------------------


def test_the_hook_refresh_an_upgrade_runs_keeps_the_number_with_no_read_before_it(
    machine: Path,  # noqa: F811
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    """A2: 0.9.2's own upgrade runs `agents refresh-hooks claude-code --config-dir <site>` in
    the new install, and nothing else of it but `--version`; a Codex account an earlier
    build made then had no reserve until something read the order."""
    codex = _codex_account_of_an_earlier_build()
    reserve = core.accounts_root() / str(codex.slot)
    site = tmp_path / "claude-site"
    site.mkdir()

    result = runner.invoke(
        app, ["agents", "refresh-hooks", "claude-code", "--config-dir", str(site)]
    )

    assert result.exit_code == 0, result.output
    assert reserve.is_file(), "kept before 0.9.2 can add an account"
    assert _add_on_092().slot == codex.slot + 1


@pytest.fixture
def a_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    """For `fleet spawn`, a fleet server on every OS (Windows' runners have no tmux, which
    the spawn asks for before the account): the fleet tests' fake."""
    from aisquare.services import fleet as fleet_service
    from tests.test_fleet_service import FakeTmux

    fake = FakeTmux()
    monkeypatch.setattr(fleet_service, "server", lambda config=None: fake)


@pytest.mark.parametrize(
    "argv",
    [
        ["launch", "agent", "--account", "5"],
        ["team", "bind", "coder", "--account", "5"],
        ["fleet", "spawn", "agent", "--account", "5"],
    ],
    ids=["launch", "team-bind", "fleet-spawn"],
)
@pytest.mark.usefixtures("a_tmux")
def test_a_json_refusal_outside_accounts_carries_the_ambiguity_too(
    signed: Pair, runner: CliRunner, argv: list[str]
) -> None:
    """E2: as `accounts` refuses it, {"error": "ambiguous_account", "ref": "5", "detail"}.
    Claude Code first in the order (``signed``): no agent of the role's names one of the two."""
    team_service.activate(Path.cwd())

    result = runner.invoke(app, ["--json", *argv])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert (payload["error"], payload.get("ref")) == ("ambiguous_account", "5"), payload
    assert payload["detail"].startswith("slot 5 is two accounts, Codex's account 5 (")
    assert payload["detail"].endswith(
        BIND_ONE_OF_5
        if argv[0] == "team"
        else "Name one as codex:5 or claude-code:5; see: aisquare accounts"
    )


def test_an_unknown_account_keeps_its_own_code(
    collided: Pair,  # noqa: F811
    runner: CliRunner,
) -> None:
    team_service.activate(Path.cwd())

    for argv in (
        ["launch", "agent", "--account", "99"],
        ["team", "bind", "coder", "--account", "99"],
    ):
        result = runner.invoke(app, ["--json", *argv])
        assert json.loads(result.stdout)["error"] == "unknown_account", (argv, result.stdout)


def test_team_bind_of_another_agents_account_says_the_agent_that_binds_it(
    signed: Pair, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A role bound to Codex, and claude-code:5: another agent's account, which its launch
    refuses. The refusal says how to bind it, in the launch's own words (B4)."""
    team_service.activate(Path.cwd())
    settings_service.bind_role("cx", agent=CODEX, account="claude-code:5")
    launched, env = _launch(runner, monkeypatch, "cx")
    settings_service.clear_role_binding("cx")
    assert runner.invoke(app, ["team", "bind", "cx", "--agent", CODEX]).exit_code == 0

    result = runner.invoke(app, ["team", "bind", "cx", "--account", "claude-code:5"])

    assert result.exit_code == 1, result.output
    assert _said(result).endswith(
        "— name a Codex account, or pass --agent claude-code (see: aisquare accounts)"
    ), _said(result)
    assert launched.exit_code != 0 and env == {}, launched.output
    assert _said(result).startswith(_said(launched).removesuffix(" (see: aisquare accounts)")), (
        "the launch's own words, then how to bind it (B5)"
    )
    assert load_config().team.profiles["cx"].account is None, "nothing bound"


def test_team_bind_of_an_account_that_names_its_agent_is_what_the_launch_runs(
    signed: Pair, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B4, the other way: Codex 5 first in the order, and a role bound to nothing but
    claude-code:5. Its launch runs the agent the account names, Claude Code, on that account
    (`agent_launch.resolve`'s account rung, above the order's), so bind accepts it. Reopen 2
    refused it as the order's agent's, a binding every launch ran."""
    _codex_5, claude = signed
    service.move("codex:5", "top")
    team_service.activate(Path.cwd())

    result = runner.invoke(app, ["team", "bind", "newrole", "--account", "claude-code:5"])

    assert result.exit_code == 0, result.output
    launched, env = _launch(runner, monkeypatch, "newrole")
    assert launched.exit_code == 0, launched.output
    assert _runs(env, claude), env


def test_an_agents_own_form_is_read_whatever_its_case(signed: Pair) -> None:
    for ref, agent in (("CODEX:5", CODEX), ("Claude-Code:5", CLAUDE), (" codex : 5 ", CODEX)):
        account = service.resolve(ref)
        assert (account.agent, account.slot) == (agent, 5), ref


@pytest.mark.parametrize("command", ["list", "usage"])
def test_the_list_shows_the_agents_form_only_where_a_number_is_shared(
    signed: Pair, runner: CliRunner, command: str
) -> None:
    """At 80 columns too, never cut to `claude-code…`: it is the name every command takes."""
    result = runner.invoke(app, ["accounts", command])

    slots = [line.split()[0] for line in result.stdout.splitlines() if line[:1].isalnum()]
    assert "codex:5" in slots and "claude-code:5" in slots, result.stdout
    assert {"1", "2", "3", "4"} <= set(slots), "a number of one account reads as it did"
    assert "5" not in slots


# --- #100's reopen 3 (seq 11320) -------------------------------------------------------------


def test_team_bind_of_a_bare_shared_number_is_refused_with_codex_5_signed_in(
    collided: Pair,  # noqa: F811
    runner: CliRunner,
) -> None:
    """B3-1: Codex 5 signed in and first in the order, so the role's agent read Codex and the
    bare 5 narrowed to Codex 5: a ✓, and then every launch of the role refused the 5 the
    binding stored with no agent. Refused now, whoever is signed in, as `accounts` refuses."""
    team_service.activate(Path.cwd())

    text = runner.invoke(app, ["team", "bind", "coder", "--account", "5"])
    as_json = runner.invoke(app, ["--json", "team", "bind", "coder", "--account", "5"])

    assert text.exit_code == 1, text.output
    assert _said(text).endswith(BIND_ONE_OF_5), _said(text)
    payload = json.loads(as_json.stdout)
    assert (payload["error"], payload["ref"]) == ("ambiguous_account", "5"), payload
    assert payload["detail"].startswith("slot 5 is two accounts")
    assert "coder" not in load_config().team.profiles, "nothing bound"


@pytest.mark.parametrize("agent", [CODEX, CLAUDE])
def test_team_bind_with_agent_stores_what_it_resolved_and_the_launch_runs_it(
    collided: Pair,  # noqa: F811
    runner: CliRunner,
    agent: str,
) -> None:
    """B3-1: --agent narrows it; the binding keeps that agent with the number, so the role's
    launch runs exactly that account (and 0.9.2, after a rollback, reads the number as it
    always did)."""
    team_service.activate(Path.cwd())

    result = runner.invoke(app, ["team", "bind", "coder", "--agent", agent, "--account", "5"])

    assert result.exit_code == 0, result.output
    profile = load_config().team.profiles["coder"]
    assert (profile.agent, profile.account) == (agent, "5")
    launch = agent_launch.resolve("coder", measure=False)
    assert launch.adapter.id == agent
    picked = service.choose(profile.account, role="coder", agent=launch.adapter.id).account
    assert picked is not None and (picked.agent, picked.slot) == (agent, 5)


def test_an_email_on_both_agents_is_no_binding_without_agent(
    signed: Pair, runner: CliRunner
) -> None:
    _codex, claude = signed
    _claude_sign_in(claude, "codex@example.com")  # one person's address on both agents
    team_service.activate(Path.cwd())

    result = runner.invoke(
        app, ["--json", "team", "bind", "coder", "--account", "codex@example.com"]
    )

    assert json.loads(result.stdout)["error"] == "ambiguous_account", result.stdout


def test_a_launch_naming_two_accounts_by_one_alias_says_the_ambiguity_under_json(
    signed: Pair, runner: CliRunner
) -> None:
    """B3-2, the launch's second branch (after `choose`): two "work"s an older build stored,
    which `--agent` does not narrow (an alias names an account, not an agent's)."""
    from aisquare.core.store import store_session

    with store_session() as store:
        store.set_claude_account_alias(5, "work")
        store.set_agent_account_alias(CODEX, 5, "work")

    result = runner.invoke(
        app, ["--json", "launch", "agent", "--agent", CLAUDE, "--account", "work"]
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert (payload["error"], payload["ref"]) == ("ambiguous_account", "work"), payload
    assert payload["detail"].startswith("'work' names codex:5 and claude-code:5"), payload


# --- B4: team bind accepts a binding if and only if the role's launch would run it ---------


@pytest.mark.parametrize("agent", [CODEX, CLAUDE])
def test_a_role_bound_to_an_agent_narrows_a_bare_shared_number_as_its_launch_does(
    collided: Pair,  # noqa: F811
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    agent: str,
) -> None:
    """11339 nit 1: the role's binding names its agent, so its launch reads a bare 5 as that
    agent's 5 and runs it. Bind reads it so too: a ✓, and the launch runs that account."""
    codex, claude = collided
    team_service.activate(Path.cwd())
    assert runner.invoke(app, ["team", "bind", "coder", "--agent", agent]).exit_code == 0

    result = runner.invoke(app, ["team", "bind", "coder", "--account", "5"])

    assert result.exit_code == 0, result.output
    profile = load_config().team.profiles["coder"]
    assert (profile.agent, profile.account) == (agent, "5")
    launched, env = _launch(runner, monkeypatch, "coder")
    assert launched.exit_code == 0, launched.output
    assert _runs(env, codex if agent == CODEX else claude), env


def test_agent_claude_code_with_a_number_only_codex_holds_is_refused_with_the_launchs_reason(
    machine: Path,  # noqa: F811
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """11339 nit 3: a normal home, Codex 5 alone (its number reserved). `--agent claude-code
    --account 5` gave a ✓, and every launch of the role was then refused. Refused now, in the
    launch's own words, and nothing is bound."""
    for email in ("two@example.com", "three@example.com", "four@example.com"):
        _claude(email)
    codex = _codex("codex@example.com")
    assert codex.slot == 5
    assert [a.agent for a in service.list_accounts() if a.slot == 5] == [CODEX]
    team_service.activate(Path.cwd())
    # The launch of that binding, as the bind before this one wrote it: refused.
    settings_service.bind_role("coder", agent=CLAUDE, account="5")
    launched, env = _launch(runner, monkeypatch, "coder")
    settings_service.clear_role_binding("coder")
    assert launched.exit_code != 0 and env == {}, launched.output
    reason = _said(launched).removeprefix("✗ ")

    result = runner.invoke(app, ["team", "bind", "coder", "--agent", CLAUDE, "--account", "5"])
    as_json = runner.invoke(
        app, ["--json", "team", "bind", "coder", "--agent", CLAUDE, "--account", "5"]
    )

    assert result.exit_code == 1, result.output
    assert _said(result) == f"✗ {reason}", "the launch's own words"
    assert "is a Codex account, and this launch runs Claude Code" in reason
    payload = json.loads(as_json.stdout)
    assert (payload["error"], payload["ref"]) == ("unknown_account", "5"), payload
    assert "coder" not in load_config().team.profiles, "nothing bound"


@pytest.mark.parametrize("ref", ["5", "x1@example.com"])
def test_binds_advice_names_agent_claude_code_first_and_the_launch_runs_it(
    signed: Pair, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, ref: str
) -> None:
    """B3-1's refusal stays, and its advice for bind is `--agent claude-code --account 5`
    FIRST, which 0.9.2 reads after a rollback, then claude-code:5, then Codex's. An email
    signed in on both reads so too. The first, followed, launches Claude Code's 5."""
    codex, claude = signed  # Claude Code 5 is signed in as x1@example.com
    _codex_sign_in(codex.config_dir, "x1@example.com")  # one person's address on both
    team_service.activate(Path.cwd())

    refused = runner.invoke(app, ["team", "bind", "coder", "--account", ref])

    assert refused.exit_code == 1, refused.output
    said = _said(refused)
    first = f"--agent claude-code --account {ref}"
    assert said.endswith(
        f"Bind one as {first} or claude-code:5, or as --agent codex --account {ref} or codex:5;"
        " see: aisquare accounts"
    ), said
    assert said.index(first) < said.index("claude-code:5") < said.index("codex:5"), said
    assert "coder" not in load_config().team.profiles, "nothing bound"

    bound = runner.invoke(app, ["team", "bind", "coder", *first.split()])
    assert bound.exit_code == 0, bound.output
    launched, env = _launch(runner, monkeypatch, "coder")
    assert launched.exit_code == 0, launched.output
    assert _runs(env, claude), env


# --- B5: one dry run of the complete would-be binding; its account names its agent above ----
# --- the folder's default, so bind and every folder's launch agree by construction ---------


def _folders(root: Path) -> dict[str, Path]:
    """Folders a role is launched from: one with no project default, one whose default agent
    is Codex, one whose default is Claude Code (`agents use X --project`)."""
    folders: dict[str, Path] = {}
    for name, agent in (("none", None), ("codex", CODEX), ("claude-code", CLAUDE)):
        folder = root / f"folder-{name}"
        folder.mkdir()
        team_service.activate(folder)
        if agent is not None:
            agent_launch.use(agent, project=True, cwd=folder)
        folders[name] = folder
    return folders


def _ran(env: dict[str, str]) -> str | None:
    """The account a held launch ran, as `agent:slot`, by the home its environment names."""
    for account in service.list_accounts():
        if _runs(env, account):
            return core.qualified_ref(account)
    return None


def _verdicts(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    folders: dict[str, Path],
    row: str,
    steps: list[list[str]],
) -> tuple[str, dict[str, str]]:
    """``team bind`` of ``steps`` (its last step's verdict, from the Codex folder), and the
    launch of a twin role bound to the same complete binding without bind's check
    (`settings.bind_role`), from each folder: `✓` or `✗ <code>`, and the account run."""
    role, twin = f"b5-{row}", f"b5-{row}-twin"
    monkeypatch.chdir(folders["codex"])
    said = ""
    for step in steps:
        result = runner.invoke(app, ["--json", "team", "bind", role, *step])
        said = "✓" if result.exit_code == 0 else f"✗ {json.loads(result.stdout)['error']}"
    for step in steps:
        pairs = dict(zip(step[::2], step[1::2], strict=True))
        settings_service.bind_role(
            twin,
            agent=pairs.get("--agent"),
            agent_bin=pairs.get("--bin"),
            account=pairs.get("--account"),
        )
    launches: dict[str, str] = {}
    for name, folder in folders.items():
        monkeypatch.chdir(folder)
        launched, env = _launch(runner, monkeypatch, twin)
        launches[name] = (
            f"✓ {_ran(env)}" if launched.exit_code == 0 else f"✗ rc {launched.exit_code}"
        )
    return said, launches


def _agree(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    folders: dict[str, Path],
    rows: list[tuple[str, list[list[str]], str | None]],
) -> list[str]:
    """The rows where bind's ✓ and each folder's launch disagree, or run another account than
    the one expected (``None``: refused by all). Printed whole as the DONE note's table (run
    with -s)."""
    table: list[str] = []
    wrong: list[str] = []
    for row, steps, expected in rows:
        bound, launches = _verdicts(runner, monkeypatch, folders, row, steps)
        typed = " then ".join(" ".join(step) for step in steps)
        table.append(f"| {row} | {typed} | {bound} | " + " | ".join(launches.values()) + " |")
        agrees = (
            bound.startswith("✗") and all(v.startswith("✗") for v in launches.values())
            if expected is None
            else bound == "✓" and set(launches.values()) == {f"✓ {expected}"}
        )
        if not agrees:
            wrong.append(table[-1])
    print(
        "\n| row | team bind … | bind | launch: no default | Codex default | Claude Code default |"
    )
    print("\n".join(table))
    return wrong


def test_b5_the_collided_homes_bindings_agree_from_every_folder(
    signed: Pair, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Codex 5 (codex@example.com) and Claude Code 5 (x1@example.com) on one number, Claude
    Code 3 aliased "work"; every binding bound, and launched from three kinds of folder."""
    service.set_alias("3", "work")
    folders = _folders(tmp_path)
    disagree = _agree(
        runner,
        monkeypatch,
        folders,
        [
            ("c1", [["--account", "5"]], None),
            ("c2", [["--account", "05"]], None),
            ("c3", [["--account", "codex@example.com"]], "codex:5"),
            ("c4", [["--account", "x1@example.com"]], "claude-code:5"),
            ("c5", [["--account", "codex:5"]], "codex:5"),
            ("c6", [["--account", "claude-code:5"]], "claude-code:5"),
            ("c7", [["--agent", CODEX, "--account", "5"]], "codex:5"),
            ("c8", [["--agent", CLAUDE, "--account", "5"]], "claude-code:5"),
            ("c9", [["--agent", CODEX, "--account", "claude-code:5"]], None),
            ("c10", [["--agent", CLAUDE, "--account", "codex:5"]], None),
            ("c11", [["--bin", "codex", "--account", "5"]], "codex:5"),
            ("c12", [["--bin", "claude", "--account", "5"]], "claude-code:5"),
            ("c13", [["--agent", CODEX, "--bin", "claude", "--account", "5"]], None),
            ("c14", [["--account", "3"]], "claude-code:3"),
            ("c15", [["--agent", CODEX, "--account", "3"]], None),
            ("c16", [["--account", "work"]], "claude-code:3"),
        ],
    )
    assert disagree == [], "\n".join(disagree)


def test_b5_the_reserve_homes_bindings_agree_from_every_folder(
    machine: Path,  # noqa: F811
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Codex 5 alone, its number reserved; Claude Code 2, 3 and 4. P11 and P12 (11387,
    failure 2): a later bind of --agent or --bin alone is read with the account bound."""
    for email in ("two@example.com", "three@example.com", "four@example.com"):
        _claude(email)
    assert _codex("codex@example.com").slot == 5
    folders = _folders(tmp_path)
    disagree = _agree(
        runner,
        monkeypatch,
        folders,
        [
            ("r1", [["--account", "5"]], "codex:5"),
            ("r2", [["--agent", CLAUDE, "--account", "5"]], None),
            ("r3", [["--account", "5"], ["--agent", CLAUDE]], None),
            ("r4", [["--account", "5"], ["--bin", "claude"]], None),
            ("r5", [["--agent", CODEX, "--account", "5"]], "codex:5"),
            ("r6", [["--account", "2"]], "claude-code:2"),
            ("r7", [["--agent", CODEX, "--account", "2"]], None),
        ],
    )
    assert disagree == [], "\n".join(disagree)


def test_b5_a_later_bind_of_agent_alone_is_refused_with_the_launchs_reason_and_binds_nothing(
    machine: Path,  # noqa: F811
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """11387 P11: `team bind ac --account 5` (Codex 5 alone), then `--agent claude-code` gave
    a ✓ and every launch was refused. Refused now in the launch's words; the binding stays."""
    for email in ("two@example.com", "three@example.com", "four@example.com"):
        _claude(email)
    _codex("codex@example.com")
    team_service.activate(Path.cwd())
    assert runner.invoke(app, ["team", "bind", "ac", "--account", "5"]).exit_code == 0
    settings_service.bind_role("twin", agent=CLAUDE, account="5")
    launched, _env = _launch(runner, monkeypatch, "twin")

    result = runner.invoke(app, ["team", "bind", "ac", "--agent", CLAUDE])

    assert result.exit_code == 1, result.output
    reason = _said(launched).split("names account '5': ", 1)[1]
    assert _said(result).endswith(reason), (_said(result), reason)
    profile = load_config().team.profiles["ac"]
    assert (profile.agent, profile.account) == (None, "5"), "the binding as it was"


def test_b5_an_agent_and_a_binary_that_disagree_are_refused_as_the_launch_refuses_them(
    signed: Pair, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """11387 P6: `--agent codex --bin claude` gave a ✓, and every launch refused it."""
    team_service.activate(Path.cwd())
    settings_service.bind_role("twin", agent=CODEX, agent_bin="claude", account="5")
    launched, _env = _launch(runner, monkeypatch, "twin")

    as_json = runner.invoke(
        app, ["--json", "team", "bind", "p6", "--agent", CODEX, "--bin", "claude"]
    )
    result = runner.invoke(app, ["team", "bind", "p6", "--agent", CODEX, "--bin", "claude"])

    assert launched.exit_code == 1 and "'claude' runs claude-code" in _said(launched)
    assert json.loads(as_json.stdout)["error"] == "agent_configuration", as_json.stdout
    assert result.exit_code == 1, result.output
    assert _said(result) == _said(launched), "the launch's own words"
    assert "p6" not in load_config().team.profiles


def test_b5_an_alias_two_accounts_hold_is_refused_in_the_launchs_own_words(
    signed: Pair, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent narrows no alias: bind's advice of --agent is no way out, so the refusal is
    the launch's own, prefix and all (the tester's surviving mutant of round 5)."""
    from aisquare.core.store import store_session

    with store_session() as store:
        store.set_claude_account_alias(5, "work")
        store.set_agent_account_alias(CODEX, 5, "work")
    team_service.activate(Path.cwd())
    settings_service.bind_role("twin", agent=CLAUDE, account="work")
    launched, _env = _launch(runner, monkeypatch, "twin")

    result = runner.invoke(app, ["team", "bind", "al", "--agent", CLAUDE, "--account", "work"])

    assert result.exit_code == 1, result.output
    assert "Bind one as" not in _said(result)
    assert _said(result) == _said(launched).replace("'twin'", "'al'"), "the launch's words"


def test_b5_bind_reads_no_folders_default_where_the_binding_names_no_agent(
    signed: Pair, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A role binding is the machine's: bind from a folder whose default agent is Codex reads
    the binding as a launch from a folder with no default does. An account no agent holds,
    with nothing naming the agent, is refused in that launch's words (the order's agent's,
    Claude Code's), never as "no Codex account", which only that folder would say."""
    folders = _folders(tmp_path)
    settings_service.bind_role("twin", account="9")
    monkeypatch.chdir(folders["none"])
    launched, _env = _launch(runner, monkeypatch, "twin")
    monkeypatch.chdir(folders["codex"])

    result = runner.invoke(app, ["team", "bind", "n9", "--account", "9"])

    assert result.exit_code == 1, result.output
    assert _said(launched).startswith("✗ the role binding for 'twin' names account '9':")
    assert _said(result) == _said(launched).replace("'twin'", "'n9'"), (
        _said(result),
        _said(launched),
    )


@pytest.mark.parametrize("surface", ["team-spawn", "fleet-spawn"])
@pytest.mark.usefixtures("a_tmux")
def test_b5_a_spawn_from_a_folder_whose_default_is_the_other_agent_runs_the_bound_accounts(
    signed: Pair,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    surface: str,
) -> None:
    """B5 (b) through the one resolver: a role bound to claude-code:5 and nothing else, spawned
    from a folder whose default agent is Codex, runs Claude Code, on account 5. It ran Codex
    there, and refused the account."""
    folders = _folders(tmp_path)
    settings_service.bind_role("cc5", account="claude-code:5")
    monkeypatch.chdir(folders["codex"])
    argv = (
        ["--json", "team", "spawn", "cc5", "--no-probe"]
        if surface == "team-spawn"
        else ["--json", "fleet", "spawn", "cc5", "--no-worktree"]
    )

    result = runner.invoke(app, argv)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    if surface == "team-spawn":
        assert payload["agent"] == CLAUDE, payload
    else:
        row = payload["agent"]
        assert (row["agent"], row["account_slot"]) == (CLAUDE, 5), row
