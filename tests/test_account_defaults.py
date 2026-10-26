"""#145 — a default account, a priority order, aliases and disabling; one resolver for launches.

``aisquare accounts`` could add and list several Claude Code logins and choose
none of them: slot 1 was always the default (``DEFAULT_SLOT``), the order was
the slot number, and the only way to make a role run elsewhere was a
``CLAUDE_CONFIG_DIR`` buried in ``team bind --env``. This file holds the
behaviour that replaces that — the registry (``claude_account`` in the store),
the resolver (``services.claude_accounts.choose``) and the commands over them —
and, in the CONTRIBUTING tradition, a control beside every claim: the rung that
must NOT win, the launch that must stay byte-identical, the slot that must not
inherit a default it was never given.

Every test runs in a redirected home (``fake_home``, tests/conftest.py), so
nothing here reads the developer's ``~/.claude.json`` or writes their registry.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import launch as launch_cli
from aisquare.cli.app import app
from aisquare.core import claude_accounts as core
from aisquare.core import paths
from aisquare.core.config import AccountsSettings, load_config, save_config
from aisquare.core.orchestrator import team_project
from aisquare.core.store import SqliteStore, store_session
from aisquare.models import ProjectInfo
from aisquare.services import claude_accounts as service
from aisquare.services import diagnostics
from aisquare.services import settings as settings_service
from tests.test_claude_accounts import _installed, _sign_in

CORRUPT = b"this is not a sqlite database, and the accounts surface must say so in words"


# --------------------------------------------------------------------------- helpers


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectInfo:
    """A registered project that is also the current directory (what `--project .` names)."""
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.chdir(root)
    project = team_project(root)
    with store_session() as store:
        store.ensure_project(project)
    return project


@pytest.fixture
def handover(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Intercept `launch`'s exec and record the environment it would have handed over."""
    captured: dict[str, Any] = {}

    def fake_exec(binary: str, argv: list[str], env: dict[str, str]) -> None:
        captured.update(binary=binary, argv=argv, env=env)

    monkeypatch.setattr(launch_cli, "_exec", fake_exec)
    monkeypatch.setattr(shutil, "which", lambda cmd, *a, **k: f"/usr/local/bin/{cmd}")
    return captured


def _slots(accounts: list[Any]) -> list[int]:
    return [account.slot for account in accounts]


def _positions(accounts: list[Any]) -> list[int | None]:
    return [account.position for account in accounts]


def _by_order() -> None:
    """``[accounts] pick = "default"``: no headroom rung, so the order alone decides."""
    config = load_config()
    config.accounts = AccountsSettings(pick="default")
    save_config(config)


def _signed_in(slot: int) -> Any:
    """Sign slot ``slot`` in (slot 1 is the plain claude's directory) and return it."""
    account = next(a for a in core.list_accounts() if a.slot == slot)
    _sign_in(account, f"slot{slot}@example.com")
    return account


# --------------------------------------------------------------------------- the registry


def test_a_fresh_machine_arranges_its_slots_in_slot_order_and_prefers_the_first_signed_in(
    fake_home: Path,
) -> None:
    """The migration is the first read: every existing machine is the no-rows case."""
    core.create_account()
    core.create_account()

    arranged = service.list_accounts()

    assert _slots(arranged) == [1, 2, 3]  # exactly the order `accounts list` always had
    assert _positions(arranged) == [1, 2, 3]  # dense, 1 first
    assert all(a.alias is None and not a.disabled for a in arranged)
    with store_session() as store:
        assert [record.slot for record in store.claude_accounts()] == [1, 2, 3]
    assert service.machine_default() is None  # nobody is signed in, so nobody runs a launch

    _signed_in(3)
    preferred = service.machine_default()
    assert preferred is not None and preferred.slot == 3  # the first that can launch


def test_a_vanished_directory_drops_its_row_and_closes_the_gap(fake_home: Path) -> None:
    """The directories are the record; a row whose directory is gone is not trusted."""
    second = core.create_account()
    core.create_account()
    _signed_in(2)
    assert service.machine_default() == service.resolve("2")

    shutil.rmtree(second.config_dir)  # moved by hand, not through `remove`

    arranged = service.list_accounts()
    assert _slots(arranged) == [1, 3]
    assert _positions(arranged) == [1, 2]
    assert service.machine_default() is None  # the default went with the directory
    with store_session() as store:
        assert [record.slot for record in store.claude_accounts()] == [1, 3]


def test_pruning_several_vanished_slots_renumbers_the_order_once(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each delete renumbered the whole table, so pruning k slots was k passes of writes
    inside one reconcile (review of #205, fourth round). One delete of all of them, one
    renumbering, and the order still closes every gap."""
    made = [core.create_account() for _ in range(4)]  # slots 2..5
    service.list_accounts()  # the rows exist
    for account in made[:3]:
        shutil.rmtree(account.config_dir)
    passes: list[int] = []
    real = SqliteStore.order_claude_accounts

    def counted(self: SqliteStore, slots: Any) -> None:
        passes.append(1)
        real(self, slots)

    monkeypatch.setattr(SqliteStore, "order_claude_accounts", counted)

    arranged = service.list_accounts()

    assert _slots(arranged) == [1, 5] and _positions(arranged) == [1, 2]
    assert len(passes) == 1
    with store_session() as store:
        assert [record.slot for record in store.claude_accounts()] == [1, 5]


def test_set_default_moves_the_slot_to_rank_1_and_the_machine_default_cannot_be_cleared(
    fake_home: Path,
) -> None:
    """``accounts default <ref>`` at machine level is ``move <ref> top`` (#8 on the board):
    rank 1 IS the default, so there is no second thing to pick and nothing to clear."""
    core.create_account()
    core.create_account()

    assert service.set_default("2") is not None
    assert _slots(service.list_accounts()) == [2, 1, 3]
    service.set_default("3")
    assert _slots(service.list_accounts()) == [3, 2, 1]  # moved to the top, the rest kept
    with pytest.raises(service.AccountsError, match="cannot be cleared"):
        service.set_default(None)
    assert _slots(service.list_accounts()) == [3, 2, 1]
    with pytest.raises(service.NoSuchAccount, match="slot 9"):
        service.set_default("9")


def test_an_alias_resolves_case_insensitively_and_a_bad_or_taken_one_is_refused(
    fake_home: Path,
) -> None:
    second = core.create_account()
    third = core.create_account()

    named = service.set_alias("2", "  Work ")
    assert named.alias == "Work" and core.label(named) == "Work"  # trimmed, its case kept
    assert service.resolve("WORK").slot == second.slot
    assert service.resolve("work").config_dir == second.config_dir
    for taken in ("work", "WORK", " wOrK "):  # taken in any case: --account could not tell
        with pytest.raises(service.AccountsError, match="already taken"):
            service.set_alias("3", taken)
    assert service.resolve("3").alias is None  # the refused write changed nothing
    for bad in ("", "  ", "123", " 4 ", "me@example.com", "a" * 65, "two\nlines", "tab\there"):
        with pytest.raises(ValueError):
            service.set_alias("3", bad)
    assert service.set_alias("2", "work").alias == "work"  # its own name in another case
    assert service.set_alias("2", None).alias is None
    with pytest.raises(service.NoSuchAccount, match="not a slot, an alias or a signed-in email"):
        service.resolve("work")
    assert third.slot == 3  # nothing above touched the other slot


def test_an_alias_is_free_text_up_to_64_characters(fake_home: Path) -> None:
    """The human: "rename them in place into anything I want" — spaces, capitals,
    punctuation and letters beyond ASCII; the only refusals are a name that would
    read as a slot or an email, an empty one, and one past 64 characters."""
    core.create_account()
    core.create_account()
    for name in ("Work laptop", "-x", "2nd account", "Ana's Max 5x", "仕事", "a" * 64):
        assert service.set_alias("2", name).alias == name
        assert service.resolve(name.upper()).slot == 2
        assert core.label(service.resolve("2")) == name
    service.set_alias("3", "Personal (home)")
    assert service.slot_labels() == {1: "plain claude", 2: "a" * 64, 3: "Personal (home)"}
    assert service.resolve("personal (HOME)").slot == 3
    with pytest.raises(ValueError, match="at most 64"):
        core.normalise_alias("a" * 65)
    with pytest.raises(ValueError, match="only digits"):
        core.normalise_alias("42")
    with pytest.raises(ValueError, match="'@'"):
        core.normalise_alias("me@home")


def test_a_name_is_never_another_accounts_own_name(fake_home: Path, runner: CliRunner) -> None:
    """``account 3`` on slot 2 was accepted (reopen of the Accounts page task): two accounts
    went by ``account 3`` on the board, in headroom notes and in ``fleet switch``, and a
    manager's ``--to "account 3"`` landed on slot 2. Another slot's label is refused in any
    case or spacing, whether that slot exists yet or not (``accounts add`` reuses a removed
    slot's number); a slot's own label is its own, and a name that only starts like one
    is a name."""
    core.create_account()
    core.create_account()
    for taken, slot in (
        ("account 3", 3),
        ("Account  3", 3),
        ("plain claude", 1),
        ("Plain Claude", 1),
        ("account 9", 9),
    ):
        with pytest.raises(ValueError, match=f"what slot {slot} is called"):
            service.set_alias("2", taken)
    # A selector after a space draws nothing, and is refused before the label is asked (review
    # of #259, round 33, 10/10).
    with pytest.raises(ValueError, match="invisible formatting character"):
        service.set_alias("2", "ACCOUNT \ufe003")
    assert service.slot_labels() == {1: "plain claude", 2: "account 2", 3: "account 3"}
    refused = runner.invoke(app, ["accounts", "alias", "2", "account 3"])
    assert refused.exit_code != 0 and "slot 3" in refused.output
    assert service.resolve("2").alias is None

    assert service.set_alias("2", "Account 2").alias == "Account 2"
    assert service.set_alias("1", "Plain Claude").alias == "Plain Claude"
    assert service.set_alias("3", "account 3 laptop").alias == "account 3 laptop"
    assert service.slot_labels() == {1: "Plain Claude", 2: "Account 2", 3: "account 3 laptop"}


@pytest.mark.parametrize("name", ["account 1", "Account 01", "account 03", "account 0"])
def test_a_name_no_slot_prints_is_a_name(fake_home: Path, name: str) -> None:
    """Review of #259, round 21: ``account 1`` was refused on slot 2 as "what slot 1 is called
    without an alias", but slot 1 is called ``plain claude`` everywhere; ``account 03`` was
    refused as slot 3's name, which prints ``account 3``. Only what a slot prints is taken."""
    core.create_account()  # slot 2
    core.create_account()  # slot 3
    assert core.normalise_alias(name, slot=2) == name
    assert service.set_alias("2", name).alias == name
    assert service.resolve(name).slot == 2
    assert service.slot_labels() == {1: "plain claude", 2: name, 3: "account 3"}


@pytest.mark.parametrize("name", ["a\u200bb", "evil\u202eexe", "\ufeffwork"])
def test_a_name_has_no_invisible_formatting_character(name: str) -> None:
    """A zero-width space or a right-to-left override prints a name the same as another
    (``a\u200bb`` beside ``ab``) or as something it is not."""
    with pytest.raises(ValueError, match="invisible"):
        core.normalise_alias(name)


def test_order_and_move_rewrite_positions_densely(fake_home: Path) -> None:
    core.create_account()
    core.create_account()

    assert _slots(service.reorder(["3"])) == [3, 1, 2]  # named first, the rest as they were
    assert _slots(service.move("2", "top")) == [2, 3, 1]
    assert _slots(service.move("2", "down")) == [3, 2, 1]
    assert _slots(service.move("1", "up")) == [3, 1, 2]
    assert _slots(service.move("3", "up")) == [3, 1, 2]  # already first: stays
    assert _slots(service.move("2", "down")) == [3, 1, 2]  # already last: stays
    assert _slots(service.move("3", "bottom")) == [1, 2, 3]
    assert _positions(service.list_accounts()) == [1, 2, 3]  # dense after every rewrite


def test_disable_keeps_a_slot_out_of_automatic_choice_but_not_out_of_reach(
    fake_home: Path,
) -> None:
    _by_order()
    core.create_account()
    _signed_in(2)
    service.move("2", "top")

    disabled = service.set_disabled("2", True)
    assert disabled.disabled and disabled.position == 1  # still rank 1 on paper…

    choice = service.choose(role="coder", project=None)
    assert choice.account is None  # …but never picked, and slot 1 has no login
    assert service.choose("2").account is not None  # the flag is the operator insisting
    assert service.set_disabled("2", False).disabled is False
    assert service.choose(role="coder", project=None).account is not None


# --------------------------------------------------------------------------- the resolver


def test_choose_with_nothing_more_specific_picks_rank_1(fake_home: Path) -> None:
    """The machine default is the first enabled, signed-in account in priority order (#8)."""
    _by_order()
    core.create_account()
    core.create_account()
    for slot in (1, 2, 3):
        _signed_in(slot)

    first = service.choose()
    assert first.account is not None and first.account.slot == 1
    assert first.source == "preferred"
    assert first.describe() == "plain claude · preferred (rank 1)"

    service.move("3", "top")  # moving a slot to the top changes the pick
    moved = service.choose()
    assert moved.account is not None and moved.account.slot == 3

    service.set_disabled("3", True)  # a disabled rank 1 hands the place to the next
    skipped = service.choose()
    assert skipped.account is not None and skipped.account.slot == 1
    assert skipped.describe() == "plain claude · preferred (rank 2)"


def test_choose_passes_over_a_rank_1_with_no_login(fake_home: Path) -> None:
    _by_order()
    core.create_account()
    _signed_in(2)  # slot 1, rank 1, has no login: it cannot run a launch

    choice = service.choose()
    assert choice.account is not None and choice.account.slot == 2 and choice.notes == []


def test_choose_walks_the_ladder_and_the_top_rung_wins(fake_home: Path, work: ProjectInfo) -> None:
    _by_order()
    core.create_account()
    core.create_account()

    # Nothing signed in: no choice, and no source — the launch leaves its environment alone.
    nothing = service.choose(role="coder", project=work)
    assert nothing.account is None and nothing.source is None and nothing.notes == []

    _signed_in(2)
    machine = service.choose(role="coder", project=work)
    assert machine.account is not None and machine.account.slot == 2
    assert machine.source == "preferred"

    service.set_default("3", project=work)
    per_project = service.choose(role="coder", project=work)
    assert per_project.account is not None and per_project.account.slot == 3
    assert per_project.source == "project default"
    # Another project is untouched by this one's default.
    elsewhere = ProjectInfo(id="prj_other", root=Path("/elsewhere"))
    assert service.choose(role="coder", project=elsewhere).source == "preferred"

    settings_service.bind_role("coder", account="1")
    bound = service.choose(role="coder", project=work)
    assert bound.account is not None and bound.account.slot == 1
    assert bound.source == "role binding"
    # A seat or another role has no binding of its own and falls through.
    assert service.choose(role="tester", project=work).source == "project default"

    flagged = service.choose("2", role="coder", project=work)
    assert flagged.account is not None and flagged.account.slot == 2
    assert flagged.source == "flag"
    assert flagged.describe() == "account 2"  # a flag reads as it always did
    assert bound.describe() == "plain claude · role binding"


def test_choose_refuses_a_rung_that_names_a_missing_account_and_says_which(
    fake_home: Path, work: ProjectInfo
) -> None:
    settings_service.bind_role("coder", account="9")
    with pytest.raises(service.NoSuchAccount, match="role binding for 'coder' names account '9'"):
        service.choose(role="coder", project=work)
    # The same machine launches every OTHER role: the refusal is the binding's, not the ladder's.
    assert service.choose(role="tester", project=work).account is None

    second = core.create_account()
    service.set_default("2", project=work)
    shutil.rmtree(second.config_dir)
    with pytest.raises(service.NoSuchAccount, match="slot 2, which no longer exists"):
        service.choose(role="tester", project=work)


def test_a_disabled_binding_or_default_is_skipped_with_a_note(
    fake_home: Path, work: ProjectInfo
) -> None:
    _by_order()
    core.create_account()
    core.create_account()
    _signed_in(3)
    settings_service.bind_role("coder", account="2")
    service.set_disabled("2", True)

    choice = service.choose(role="coder", project=work)

    assert choice.account is not None and choice.account.slot == 3  # fell through to rank 1
    assert choice.source == "preferred"
    assert choice.notes == ["account 2 (bound to coder) is disabled — skipped"]


def test_the_agent_header_and_fleet_ls_name_the_account_as_the_rest_of_the_cli_does(
    fake_home: Path,
) -> None:
    """An aliased account is ``work`` on the launch line, the feed, the page — and now on the
    agent header and ``fleet ls`` too; slot 1 is ``plain claude`` there as well (review of
    #205, finding 10)."""
    from datetime import UTC, datetime

    from aisquare.cli import fleet as fleet_cli
    from aisquare.cli.ui.views import agent as agent_view
    from aisquare.models import FleetAgent, FleetAgentStatus

    core.create_account()
    service.set_alias("2", "work")
    labels = service.slot_labels()
    assert labels == {1: "plain claude", 2: "work"}

    def status(slot: int) -> FleetAgentStatus:
        agent = FleetAgent(
            id="agt_label",
            project_id="prj_label",
            label="coder-1",
            role="coder",
            pane_id="%1",
            cwd=Path("/tmp"),
            account_slot=slot,
            created_at=datetime.now(tz=UTC),
        )
        return FleetAgentStatus(agent=agent, state="waiting")

    assert agent_view.account_text(status(2), labels) == "work"
    assert agent_view.account_text(status(1), labels) == "plain claude"
    assert agent_view.account_text(status(2)) == "account 2"  # no labels: the built-in name
    assert "  work  " in fleet_cli._agent_line(status(2), labels)
    assert "  plain claude  " in fleet_cli._agent_line(status(1), {})  # never `account 1`
    assert "  account 2  " in fleet_cli._agent_line(status(2), {})


def test_a_disabled_account_cannot_be_made_the_default(fake_home: Path, runner: CliRunner) -> None:
    """Accepted silently, the default was a rung every launch skipped (third round)."""
    core.create_account()
    core.create_account()
    service.set_disabled("2", True)
    with pytest.raises(service.AccountsError, match="enable it first: aisquare accounts enable 2"):
        service.set_default("2")
    refused = runner.invoke(app, ["accounts", "default", "2"])
    assert refused.exit_code == 1 and "enable it first" in refused.output
    assert _slots(service.list_accounts()) == [1, 2, 3]  # nothing moved


def test_a_slot_removed_between_the_reconcile_and_the_write_is_a_sentence(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store's ``KeyError`` reached the top as a traceback (third round)."""
    from aisquare.core.store import SqliteStore

    core.create_account()

    def vanished(self: Any, *args: Any, **kwargs: Any) -> None:
        raise KeyError(2)

    monkeypatch.setattr(SqliteStore, "set_claude_account_alias", vanished)
    monkeypatch.setattr(SqliteStore, "set_claude_account_disabled", vanished)
    monkeypatch.setattr(SqliteStore, "order_claude_accounts", vanished)
    calls: list[Callable[[], object]] = [
        lambda: service.set_alias("2", "work"),
        lambda: service.set_disabled("2", True),
        lambda: service.set_default("2"),
    ]
    for call in calls:
        with pytest.raises(service.NoSuchAccount, match="removed meanwhile"):
            call()


def test_a_machine_default_on_a_slot_removed_after_it_was_resolved_is_refused(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """The real store, not a mocked ``KeyError``: ``order_claude_accounts`` ignores an unknown
    slot, so a slot removed between ``resolve()`` and the write printed ``✓ … moved to rank
    1`` over an order it had not changed (review of #406 in #401, n4). The retired
    ``set_claude_account_default`` raised there."""
    core.create_account()
    gone = core.create_account()
    resolve = service.resolve

    def removed_meanwhile(ref: str, **kwargs: Any) -> Any:
        account = resolve(ref, **kwargs)
        shutil.rmtree(gone.config_dir)  # by another process, after the lookup
        return account

    monkeypatch.setattr(service, "resolve", removed_meanwhile)
    with pytest.raises(service.NoSuchAccount, match="slot 3 any more"):
        service.set_default("3")
    monkeypatch.setattr(service, "resolve", resolve)
    assert _slots(service.list_accounts()) == [1, 2]  # nothing moved, the slot dropped


# --------------------------------------------------------------------------- launch and spawn


def test_launch_runs_on_the_machine_default_and_stays_byte_identical_without_one(
    fake_home: Path, work: ProjectInfo, handover: dict[str, Any], runner: CliRunner
) -> None:
    _by_order()
    second = core.create_account()

    # The control first: nothing signed in, nothing touched — the pre-#145 launch.
    plain = runner.invoke(app, ["launch", "coder"])
    assert plain.exit_code == 0, plain.output
    assert core.CONFIG_DIR_VAR not in handover["env"]
    assert "[" not in plain.stderr.split("Launching", 1)[1].split(" as ")[0]

    _sign_in(second, "two@example.com")
    handover.clear()
    result = runner.invoke(app, ["launch", "coder"])
    assert result.exit_code == 0, result.output
    assert handover["env"][core.CONFIG_DIR_VAR] == str(second.config_dir)
    assert handover["env"][core.TMPDIR_VAR] == str(second.tmp_dir)
    assert "[account 2 · preferred (rank 2)]" in result.stderr

    # A binding's env loses to the default, as it loses to the flag.
    handover.clear()
    bound = runner.invoke(app, ["launch", "coder", "--env", f"{core.CONFIG_DIR_VAR}=/elsewhere"])
    assert bound.exit_code == 0, bound.output
    assert handover["env"][core.CONFIG_DIR_VAR] == str(second.config_dir)

    # An alias on the flag is the same account, and reads as the flag.
    service.set_alias("2", "work")
    handover.clear()
    aliased = runner.invoke(app, ["launch", "coder", "--account", "work"])
    assert aliased.exit_code == 0, aliased.output
    assert handover["env"][core.CONFIG_DIR_VAR] == str(second.config_dir)
    assert "[work]" in aliased.stderr

    # A free-text name with a space, quoted, in another case: the same account.
    service.set_alias("2", "Work laptop")
    handover.clear()
    spaced = runner.invoke(app, ["launch", "coder", "--account", "work LAPTOP"])
    assert spaced.exit_code == 0, spaced.output
    assert handover["env"][core.CONFIG_DIR_VAR] == str(second.config_dir)
    assert "[Work laptop]" in spaced.stderr


@pytest.mark.parametrize("bound", ["env", "env_files"])
@pytest.mark.parametrize("pick", ["default", "headroom"], ids=["pick-default", "unreadable-usage"])
def test_rank_1_never_overrides_a_hand_written_config_dir_binding(
    fake_home: Path,
    work: ProjectInfo,
    handover: dict[str, Any],
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    pick: str,
    bound: str,
) -> None:
    """``team bind coder --env CLAUDE_CONFIG_DIR=/hand/written`` launched there before rank 1
    was the default, and must still: the preferred rung is a silent default, and applying
    slot 1 RESTORES this shell's variables over the binding's (review of #406, M1). With
    ``pick = "default"``, and with headroom on but no usage readable, which falls to it. The
    directory bound as a value (``--env``) or read from a file (``--env-file``) alike."""
    config = load_config()
    config.accounts = AccountsSettings(pick=pick)
    save_config(config)
    monkeypatch.setattr(service, "_http_get", lambda url, headers, timeout: (503, b""))
    core.create_account()
    _signed_in(1)
    _signed_in(2)
    if bound == "env":
        settings_service.bind_role("coder", env={core.CONFIG_DIR_VAR: "/hand/written"})
    else:
        secret = fake_home / "config-dir.txt"
        secret.write_text("/hand/written\n", encoding="utf-8")
        settings_service.bind_role("coder", env_files={core.CONFIG_DIR_VAR: str(secret)})

    choice = service.choose(role="coder", project=work)
    assert choice.account is None and choice.source is None  # nothing chose: left as written
    launched = runner.invoke(app, ["launch", "coder"])
    assert launched.exit_code == 0, launched.output
    assert handover["env"][core.CONFIG_DIR_VAR] == "/hand/written"

    # The control: a role with no such binding gets rank 1, and an account binding or the
    # flag still win over the hand-written directory, as they always did.
    assert service.choose(role="tester", project=work).source == "preferred"
    assert service.choose("2", role="coder", project=work).source == "flag"
    settings_service.bind_role("coder", account="2")
    assert service.choose(role="coder", project=work).source == "role binding"


def test_a_default_set_by_an_older_build_is_rank_1_on_the_next_read(fake_home: Path) -> None:
    """The retired ``is_default`` flag is folded into the order by the registry's read, keyed
    on the flag and not on a schema version (review of #406, C1)."""
    core.create_account()
    core.create_account()
    service.list_accounts()  # the rows exist, in slot order
    raw = sqlite3.connect(str(paths.db_path()))
    try:
        raw.execute("UPDATE claude_account SET is_default = 1 WHERE slot = 3")  # an old build
        raw.commit()
    finally:
        raw.close()

    assert _slots(service.list_accounts()) == [3, 1, 2]
    service.move("1", "top")  # cleared once promoted: a later reorder stands
    assert _slots(service.list_accounts()) == [1, 3, 2]


def test_launch_says_why_a_rung_was_skipped_and_refuses_a_dangling_one(
    fake_home: Path, work: ProjectInfo, handover: dict[str, Any], runner: CliRunner
) -> None:
    _by_order()
    core.create_account()
    _signed_in(2)
    settings_service.bind_role("coder", account="2")
    service.set_disabled("2", True)

    skipped = runner.invoke(app, ["launch", "coder"])
    assert skipped.exit_code == 0, skipped.output
    assert "accounts: account 2 (bound to coder) is disabled — skipped" in skipped.stderr
    assert core.CONFIG_DIR_VAR not in handover["env"]  # fell through to "touch nothing"

    settings_service.bind_role("coder", account="9")
    handover.clear()
    refused = runner.invoke(app, ["launch", "coder"])
    assert refused.exit_code == 1
    assert "role binding for 'coder' names account '9'" in refused.stderr
    assert handover == {}  # never handed over
    as_json = runner.invoke(app, ["--json", "launch", "coder"])
    assert json.loads(as_json.stdout)["error"] == "unknown_account"
    assert handover == {}


# --------------------------------------------------------------------------- removal


def test_remove_forgets_the_default_the_alias_and_every_project_default_that_named_it(
    fake_home: Path, work: ProjectInfo
) -> None:
    second = core.create_account()
    service.set_default("2", project=work)
    service.set_alias("2", "work")
    core.create_account()  # slot 3, so the order has something left to renumber

    _sign_in(second, "two@example.com")
    settings_service.bind_role("reviewer", account="2")  # by NUMBER: the hazard of finding 8
    settings_service.bind_role("tester", account="work")  # by alias: dies with the alias
    settings_service.bind_role("coder", account="3")  # another slot: untouched
    notes: list[str] = []

    assert service.machine_default() == service.resolve("2")
    service.remove(second, notes=notes)

    assert service.machine_default() is None
    assert service.project_default(work) is None
    assert _slots(service.list_accounts()) == [1, 3]
    assert _positions(service.list_accounts()) == [1, 2]
    # The bindings that named the number AND the alias now name the person — dangling on
    # purpose; the alias is free again, and naming the next account `work` must not
    # re-point `tester` (second round).
    bindings = settings_service.role_account_bindings()
    assert bindings == {"reviewer": "two@example.com", "tester": "two@example.com", "coder": "3"}
    assert notes == [
        "role reviewer was bound to slot 2; it now names two@example.com — refused at launch "
        "until that account is signed in again, or re-bound",
        "role tester was bound to alias 'work' (slot 2); it now names two@example.com — "
        "refused at launch until that account is signed in again, or re-bound",
    ]
    # THE reuse hazard: the next add takes slot 2 again and must inherit nothing.
    again = core.create_account()
    assert again.slot == 2
    fresh = service.resolve("2")
    assert fresh.alias is None and fresh.position == 3 and not fresh.disabled  # queued last
    with pytest.raises(service.NoSuchAccount):
        service.resolve("work")
    with pytest.raises(service.NoSuchAccount, match=r"reviewer.*two@example\.com"):
        service.choose(role="reviewer", project=work)  # refused with the rung named, not adopted
    assert service.choose(role="coder", project=work).source == "role binding"
    service.set_alias("2", "work")  # the ordinary thing to do with the newcomer…
    with pytest.raises(service.NoSuchAccount, match=r"tester.*two@example\.com"):
        service.choose(role="tester", project=work)  # …and tester does not follow it


def test_remove_clears_a_number_binding_to_a_slot_that_never_signed_in(fake_home: Path) -> None:
    blank = core.create_account()  # slot 2, no login, no email to name
    settings_service.bind_role("reviewer", account="2", env={"FOO": "bar"})
    notes: list[str] = []

    service.remove(blank, notes=notes)

    profile = load_config().team.profiles["reviewer"]
    assert profile.account is None and profile.env == {"FOO": "bar"}  # the rest survives
    assert notes == [
        "role reviewer was bound to slot 2, which had no login — the account binding is cleared"
    ]
    removed = core.create_account()
    assert removed.slot == 2 and service.choose(role="reviewer").source is None  # nothing inherited


# --------------------------------------------------------------------------- a damaged store


def test_a_damaged_store_costs_the_arrangement_never_the_listing_or_the_launch(
    fake_home: Path,
    work: ProjectInfo,
    handover: dict[str, Any],
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _installed(monkeypatch)
    core.create_account()
    service.set_alias("2", "kept")  # a real arrangement, about to become unreadable
    paths.db_path().write_bytes(CORRUPT)

    listing = runner.invoke(app, ["--json", "accounts", "list"])
    assert listing.exit_code == 0, listing.output
    rows = json.loads(listing.stdout)["accounts"]
    assert [row["account"]["slot"] for row in rows] == [1, 2]  # the directories still list
    assert not any(row["account"]["alias"] for row in rows)  # the arrangement is what is lost

    launched = runner.invoke(app, ["launch", "coder"])
    assert launched.exit_code == 0, launched.output
    assert handover, "a damaged registry stopped the agent from starting"
    assert core.CONFIG_DIR_VAR not in handover["env"]  # no default could be read, so none applied
    assert "accounts registry unreadable" in launched.stderr
    assert "Traceback" not in launched.output

    # An ALIAS binding cannot be checked against the fallback list (no aliases there): it
    # costs its rung with a note, never the launch (review of #205, second round).
    settings_service.bind_role("tester", account="work")
    choice = service.choose(role="tester", project=work)
    assert choice.account is None and choice.source is None
    assert any("names account 'work', which cannot be resolved" in n for n in choice.notes)
    handover.clear()
    bound = runner.invoke(app, ["launch", "tester"])
    assert bound.exit_code == 0, bound.output
    assert handover and core.CONFIG_DIR_VAR not in handover["env"]
    assert "cannot be resolved while the accounts registry unreadable" in bound.stderr
    # A NUMBER binding still resolves on the fallback list, exactly as before.
    settings_service.bind_role("runner", account="2")
    assert service.choose(role="runner", project=work).source == "role binding"

    refused = runner.invoke(app, ["--json", "accounts", "default", "2"])
    assert refused.exit_code == 1
    assert json.loads(refused.stdout)["error"] == "store_unreadable"
    assert "Traceback" not in refused.output

    with pytest.raises(service.AccountsUnreadable):
        service.set_alias("2", "work")
    with pytest.raises(sqlite3.Error), store_session():  # the control: it really is unreadable
        pass


# --------------------------------------------------------------------------- the commands


def test_accounts_default_sets_shows_and_clears_at_all_three_levels(
    fake_home: Path, work: ProjectInfo, runner: CliRunner
) -> None:
    core.create_account()
    core.create_account()

    shown = runner.invoke(app, ["accounts", "default"])
    assert shown.exit_code == 0, shown.output
    assert "machine default: none" in shown.stdout
    assert "no default of its own" in shown.stdout  # the cwd's project is reported

    _signed_in(2)
    _signed_in(3)
    machine = runner.invoke(app, ["accounts", "default", "2"])
    assert machine.exit_code == 0, machine.output
    assert "✓ machine default: slot 2" in machine.stdout and "moved to rank 1" in machine.stdout
    assert "not signed in" not in machine.stdout  # signed in: nothing to warn about
    assert _slots(service.list_accounts()) == [2, 1, 3]  # an alias of `move 2 top`

    per_project = runner.invoke(app, ["accounts", "default", "3", "--project", "."])
    assert per_project.exit_code == 0, per_project.output
    assert f"✓ project {work.root.name} default: slot 3" in per_project.stdout

    per_role = runner.invoke(app, ["accounts", "default", "1", "--role", "coder"])
    assert per_role.exit_code == 0, per_role.output
    assert load_config().team.profiles["coder"].account == "1"

    as_json = runner.invoke(app, ["--json", "accounts", "default"])
    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.stdout)
    assert payload["machine_default"]["slot"] == 2
    assert payload["project_default"]["slot"] == 3 and payload["project"] == work.id
    assert payload["role_bindings"] == {"coder": "1"}

    listed = runner.invoke(app, ["accounts", "list"])
    assert listed.exit_code == 0, listed.output
    rows = [line.split() for line in listed.stdout.splitlines()]
    assert [row[0] for row in rows if row and row[0].isdigit()] == ["2", "1", "3"]  # by rank
    assert "*" not in listed.stdout and "no default account" not in listed.stdout  # no star
    ordered = runner.invoke(app, ["accounts", "order", "2"])
    assert ordered.exit_code == 0, ordered.output
    assert ordered.stdout.splitlines()[0].startswith("1. slot 2")  # rank 1 first, unmarked
    assert "*" not in ordered.stdout and "★" not in ordered.stdout

    bindings = runner.invoke(app, ["team", "bind"])
    assert "coder" in bindings.stdout and "account=1" in bindings.stdout

    for args in (
        ["accounts", "default", "--clear", "--role", "coder"],
        ["accounts", "default", "--clear", "--project", "."],
    ):
        cleared = runner.invoke(app, args)
        assert cleared.exit_code == 0, cleared.output
    assert (
        "coder" not in load_config().team.profiles
        or not load_config().team.profiles["coder"].account
    )
    assert service.project_default(work) is None
    # The machine's is rank 1 of the order: there is always a first, so no clearing it.
    kept = runner.invoke(app, ["accounts", "default", "--clear"])
    assert kept.exit_code == 1 and "cannot be cleared" in kept.output
    assert service.machine_default() == service.resolve("2")

    both = runner.invoke(
        app, ["--json", "accounts", "default", "2", "--project", ".", "--role", "x"]
    )
    assert both.exit_code == 1 and json.loads(both.stdout)["error"] == "usage"
    unknown = runner.invoke(app, ["--json", "accounts", "default", "9"])
    assert unknown.exit_code == 1 and json.loads(unknown.stdout)["error"] == "unknown_account"
    unknown_role = runner.invoke(app, ["--json", "accounts", "default", "9", "--role", "coder"])
    assert unknown_role.exit_code == 1
    assert json.loads(unknown_role.stdout)["error"] == "unknown_account"
    assert "coder" not in load_config().team.profiles  # a refused reference binds nothing


def test_making_a_signed_out_account_the_default_says_launches_skip_it(
    fake_home: Path, runner: CliRunner
) -> None:
    """Rank 1 is the default only for an account that can launch (review of #406, m1)."""
    core.create_account()
    moved = runner.invoke(app, ["accounts", "default", "2"])
    assert moved.exit_code == 0, moved.output
    assert "moved to rank 1" in moved.stdout
    assert "not signed in, so launches skip it until it is: aisquare accounts run 2" in (
        moved.stdout
    )
    assert _slots(service.list_accounts()) == [2, 1]


def test_a_project_default_lists_the_project_it_is_set_for(
    fake_home: Path, work: ProjectInfo, runner: CliRunner
) -> None:
    """`accounts default <slot> --project` registered the project the way a hooked
    prompt does, so since #139 a project configured on purpose stayed off `project
    list` and the sidebar. Choosing its account is choosing it; clearing adds nothing."""
    core.create_account()

    def listed() -> set[str]:
        with store_session() as store:
            return {project.id for project in store.list_projects()}

    assert work.id not in listed(), "the fixture's registration is a capture"
    cleared = runner.invoke(app, ["accounts", "default", "--clear", "--project", "."])
    assert cleared.exit_code == 0, cleared.output
    assert work.id not in listed(), "clearing a default is not an add"

    chosen = runner.invoke(app, ["accounts", "default", "2", "--project", "."])

    assert chosen.exit_code == 0, chosen.output
    assert work.id in listed()
    default = service.project_default(work)
    assert default is not None and default.slot == 2


def test_clearing_a_forgotten_projects_default_leaves_it_forgotten(
    fake_home: Path, work: ProjectInfo, runner: CliRunner
) -> None:
    """`accounts default --clear --project .` registered the directory before it cleared
    the setting, which un-forgot a forgotten project: back as a capture, with every
    later read reaching it (review of #168, round 2). A clear is a delete; it needs
    no row."""
    core.create_account()
    service.set_default("2", project=work)
    with store_session() as store:
        store.forget_project(work.id)

    cleared = runner.invoke(app, ["accounts", "default", "--clear", "--project", "."])

    assert cleared.exit_code == 0, cleared.output
    with store_session() as store:
        assert store.get_project(work.id) is None, "still forgotten"
        assert store.project_setting(work.id, service.PROJECT_ACCOUNT_KEY) is None


def test_accounts_alias_order_move_disable_and_enable_commands(
    fake_home: Path, runner: CliRunner
) -> None:
    core.create_account()
    core.create_account()

    alias = runner.invoke(app, ["accounts", "alias", "2", "Work"])
    assert alias.exit_code == 0 and "slot 2 is now called Work" in alias.stdout
    bad = runner.invoke(app, ["--json", "accounts", "alias", "3", "42"])
    assert bad.exit_code == 1 and json.loads(bad.stdout)["error"] == "bad_alias"
    taken = runner.invoke(app, ["accounts", "alias", "3", "work"])
    assert taken.exit_code == 1 and "already taken" in taken.stderr
    spaced = runner.invoke(app, ["--json", "accounts", "alias", "3", "Side project"])
    assert spaced.exit_code == 0, spaced.output
    assert json.loads(spaced.stdout)["account"]["alias"] == "Side project"
    long = runner.invoke(app, ["--json", "accounts", "alias", "3", "x" * 65])
    assert long.exit_code == 1 and json.loads(long.stdout)["error"] == "bad_alias"
    emailish = runner.invoke(app, ["--json", "accounts", "alias", "3", "me@home"])
    assert emailish.exit_code == 1 and json.loads(emailish.stdout)["error"] == "bad_alias"
    by_name = runner.invoke(app, ["--json", "accounts", "alias", "side PROJECT", "--clear"])
    assert by_name.exit_code == 0 and json.loads(by_name.stdout)["account"]["slot"] == 3

    ordered = runner.invoke(app, ["--json", "accounts", "order", "work", "3"])
    assert ordered.exit_code == 0, ordered.output
    assert [row["slot"] for row in json.loads(ordered.stdout)["order"]] == [2, 3, 1]

    moved = runner.invoke(app, ["accounts", "move", "1", "top"])
    assert moved.exit_code == 0, moved.output
    assert [line.split(". ")[0][-1] for line in moved.stdout.splitlines()] == ["1", "2", "3"]
    assert moved.stdout.splitlines()[0].endswith("plain claude")
    sideways = runner.invoke(app, ["--json", "accounts", "move", "1", "left"])
    assert sideways.exit_code == 1 and json.loads(sideways.stdout)["error"] == "usage"

    off = runner.invoke(app, ["accounts", "disable", "work"])
    assert off.exit_code == 0 and "never picked automatically" in off.stdout
    assert service.resolve("work").disabled
    listed = runner.invoke(app, ["accounts", "list"])
    assert "(disabled)" in listed.stdout
    on = runner.invoke(app, ["--json", "accounts", "enable", "2"])
    assert on.exit_code == 0 and json.loads(on.stdout)["account"]["disabled"] is False

    cleared = runner.invoke(app, ["accounts", "alias", "2", "--clear"])
    assert cleared.exit_code == 0 and "alias cleared" in cleared.stdout
    assert service.resolve("2").alias is None
    nothing = runner.invoke(app, ["--json", "accounts", "alias", "2"])
    assert nothing.exit_code == 1 and json.loads(nothing.stdout)["error"] == "usage"


def test_team_bind_account_resolves_the_reference_before_writing_it(
    fake_home: Path, runner: CliRunner
) -> None:
    core.create_account()

    refused = runner.invoke(app, ["--json", "team", "bind", "coder", "--account", "9"])
    assert refused.exit_code == 1 and json.loads(refused.stdout)["error"] == "unknown_account"
    assert "coder" not in load_config().team.profiles  # nothing written

    bound = runner.invoke(app, ["team", "bind", "coder", "--account", "2", "--env", "FOO=bar"])
    assert bound.exit_code == 0, bound.output
    assert "account=2" in bound.stdout and "FOO=bar" in bound.stdout
    profile = load_config().team.profiles["coder"]
    assert profile.account == "2" and profile.env == {"FOO": "bar"}

    cleared = runner.invoke(app, ["team", "bind", "coder", "--clear-account"])
    assert cleared.exit_code == 0, cleared.output
    profile = load_config().team.profiles["coder"]
    assert profile.account is None and profile.env == {"FOO": "bar"}  # the rest survives

    # `--account X --clear-account` used to clear and report success (review of #205,
    # finding 13): refused as a usage error, nothing written — on both commands.
    both = runner.invoke(
        app, ["--json", "team", "bind", "coder", "--account", "2", "--clear-account"]
    )
    assert both.exit_code == 1 and json.loads(both.stdout)["error"] == "usage"
    assert load_config().team.profiles["coder"].account is None
    also = runner.invoke(app, ["--json", "accounts", "default", "2", "--role", "coder", "--clear"])
    assert also.exit_code == 1 and json.loads(also.stdout)["error"] == "usage"
    assert load_config().team.profiles["coder"].account is None
    # The writer itself, asked for both, replaces rather than discards.
    replaced = settings_service.bind_role("coder", account="2", clear_account=True)
    assert replaced.account == "2"

    empty = runner.invoke(app, ["--json", "team", "bind", "tester"])
    assert empty.exit_code == 1 and json.loads(empty.stdout)["error"] == "nothing_to_bind"


# --------------------------------------------------------------------------- doctor


def test_doctor_names_the_machine_default_under_a_binding_and_is_silent_when_nothing_is_arranged(
    fake_home: Path, work: ProjectInfo
) -> None:
    core.create_account()  # slot 2, signed in below
    third = core.create_account()  # slot 3, never signed in
    _sign_in(core.find_account(2) or third, "two@example.com")

    names = {check.name for check in diagnostics._claude_accounts_checks()}
    assert names == {"claude-accounts"}  # the store exists, nothing is arranged: no new lines

    service.set_default("3", project=work)  # something arranged: the rung under it is said
    checks = {check.name: check for check in diagnostics._claude_accounts_checks()}
    default = checks["claude-account-default"]
    assert default.status.value == "ok" and "slot 2" in default.detail  # rank 1 has no login

    service.set_disabled("2", True)
    checks = {check.name: check for check in diagnostics._claude_accounts_checks()}
    assert checks["claude-account-default"].status.value == "warn"
    assert "no enabled account is signed in" in checks["claude-account-default"].detail

    settings_service.bind_role("coder", account="9")  # straight to config: `team bind` would refuse
    shutil.rmtree(third.config_dir)
    checks = {check.name: check for check in diagnostics._claude_accounts_checks()}
    dangling = checks["claude-account-bindings"]
    assert dangling.status.value == "warn"
    assert "role coder → 9" in dangling.detail
    assert f"project {work.root.name} → slot 3" in dangling.detail


def test_doctor_flags_the_binding_that_outlives_the_last_added_account(
    fake_home: Path, work: ProjectInfo
) -> None:
    """``_retarget_bindings`` re-points a binding at the removed slot's email and says
    ``doctor`` flags it — but the check ran only while an added account existed, so removing
    the LAST one left the binding refusing every launch of its role with no doctor line
    (review of the #205 fold, round 1)."""
    only = core.create_account()  # slot 2, the only added account
    _sign_in(only, "two@example.com")
    settings_service.bind_role("reviewer", account="2")

    service.remove(only)

    assert settings_service.role_account_bindings() == {"reviewer": "two@example.com"}
    checks = {check.name: check for check in diagnostics._claude_accounts_checks()}
    assert "claude-accounts" not in checks  # nothing added: still no line of its own
    dangling = checks["claude-account-bindings"]
    assert dangling.status.value == "warn" and "role reviewer → two@example.com" in dangling.detail
    # A machine with nothing arranged keeps the doctor output it had.
    settings_service.bind_role("reviewer", clear_account=True)
    assert diagnostics._claude_accounts_checks() == []


def test_doctor_leaves_a_machine_that_added_and_bound_nothing_as_it_was(
    fake_home: Path, work: ProjectInfo
) -> None:
    """Round 1 ran the default checks with no added account too, for the binding a removal
    leaves behind — and they read the registry through ``list_accounts``, which reconciles
    as it reads: every doctor on a machine that never added an account wrote slot 1's row,
    and a store it could not open was a ``claude-account-default`` warning that machine never
    had (review of the #205 fold, round 2). Nothing arranged, nothing is written."""
    assert paths.db_path().exists()  # `work` opened the store: the gate is not the file

    assert diagnostics._claude_accounts_checks() == []
    with store_session() as store:
        assert store.claude_accounts() == []  # no reconcile write

    paths.db_path().write_bytes(CORRUPT)
    assert diagnostics._claude_accounts_checks() == []  # the database line says it, alone


def test_doctor_flags_a_project_default_that_outlives_the_last_added_account(
    fake_home: Path, work: ProjectInfo
) -> None:
    """Round 2 gated the default checks, with no added account, on a role binding alone:
    "a project default is a registry row ``forget_arrangement`` drops with its slot". It is
    a project setting — ``forget_arrangement`` drops it best effort, and a slot directory
    deleted by hand drops none — so every launch in that project was refused while doctor
    said nothing (review of the #205 fold, round 3)."""
    only = core.create_account()  # slot 2, the only added account
    _sign_in(only, "two@example.com")
    service.set_default("2", project=work)
    shutil.rmtree(only.config_dir)  # by hand, not through `remove`

    with pytest.raises(service.NoSuchAccount, match="slot 2"):
        service.choose(None, role="coder", project=work)
    checks = {check.name: check for check in diagnostics._claude_accounts_checks()}
    assert "claude-accounts" not in checks  # nothing added: still no line of its own
    dangling = checks["claude-account-bindings"]
    assert dangling.status.value == "warn"
    assert f"project {work.root.name} → slot 2" in dangling.detail

    service.set_default(None, project=work)
    assert diagnostics._claude_accounts_checks() == []  # nothing arranged is left to report


def test_doctor_checks_every_binding_against_one_registry_read(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``resolve(ref)`` per binding re-opened the store and rescanned the directories each
    time — six bound roles, seven opens for one doctor line (review of #205, fourth round).
    The list already in hand answers every binding."""
    core.create_account()  # slot 2
    service.set_alias("2", "work")
    for role in ("coder", "tester", "reviewer"):
        settings_service.bind_role(role, account="work")
    settings_service.bind_role("runner", account="9")  # dangling, still found
    reads: list[int] = []
    real = service._read_registry

    def counted(project: Any = None) -> Any:
        reads.append(1)
        return real(project)

    monkeypatch.setattr(service, "_read_registry", counted)

    checks = {check.name: check for check in diagnostics._claude_account_default_checks()}

    assert len(reads) == 1
    detail = checks["claude-account-bindings"].detail
    assert "role runner → 9" in detail and "role coder" not in detail


def test_project_default_reads_the_registry_in_one_store_open(
    fake_home: Path, work: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two opens — the slot, then the list with a directory scan of its own — for what one
    guarded read returns (review of #205, fourth round). ``accounts default`` asks on every
    invocation."""
    import contextlib

    core.create_account()  # slot 2
    service.set_default("2", project=work)
    opens: list[int] = []
    real_session = store_session

    @contextlib.contextmanager
    def counted_session() -> Any:
        opens.append(1)
        with real_session() as store:
            yield store

    monkeypatch.setattr("aisquare.services.claude_accounts.store_session", counted_session)

    chosen = service.project_default(work)

    assert chosen is not None and chosen.slot == 2
    assert len(opens) == 1


def test_doctor_reads_no_registry_and_creates_no_store_before_one_exists(
    fake_home: Path, runner: CliRunner
) -> None:
    """`doctor` must not build the thing it diagnoses — the registry read is gated on the file."""
    core.create_account()
    assert not paths.db_path().exists()

    checks = diagnostics._claude_accounts_checks()

    assert [check.name for check in checks] == ["claude-accounts"]
    assert not paths.db_path().exists()  # this check built nothing
    # The whole command, for the traceback property only: with a HOME present, doctor's
    # other checks open the store by design (tests/test_doctor_does_not_create_state.py
    # gates on the home, not the file), so the file's absence is not asserted after it.
    doctor = runner.invoke(app, ["doctor"])
    assert "Traceback" not in doctor.output
