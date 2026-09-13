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

Every test runs in a redirected home (``fake_home`` from the sibling file), so
nothing here reads the developer's ``~/.claude.json`` or writes their registry.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import launch as launch_cli
from aisquare.cli.app import app
from aisquare.core import claude_accounts as core
from aisquare.core import paths
from aisquare.core.config import load_config
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import ProjectInfo
from aisquare.services import claude_accounts as service
from aisquare.services import diagnostics
from aisquare.services import settings as settings_service
from tests.test_claude_accounts import _installed, _sign_in
from tests.test_claude_accounts import fake_home as _redirected_home

#: The sibling file's redirected home, re-exported so pytest collects it here too: every
#: test below reads and writes Claude Code's files, and none may touch the developer's.
fake_home = _redirected_home

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


# --------------------------------------------------------------------------- the registry


def test_a_fresh_machine_arranges_its_slots_in_slot_order_and_marks_nothing_default(
    fake_home: Path,
) -> None:
    """The migration is the first read: every existing machine is the no-rows case."""
    core.create_account()
    core.create_account()

    arranged = service.list_accounts()

    assert _slots(arranged) == [1, 2, 3]  # exactly the order `accounts list` always had
    assert _positions(arranged) == [1, 2, 3]  # dense, 1 first
    assert not any(a.is_default for a in arranged)  # nobody chose, so nothing is chosen
    assert all(a.alias is None and not a.disabled for a in arranged)
    with store_session() as store:
        assert [record.slot for record in store.claude_accounts()] == [1, 2, 3]
    assert service.machine_default() is None


def test_a_vanished_directory_drops_its_row_and_closes_the_gap(fake_home: Path) -> None:
    """The directories are the record; a row whose directory is gone is not trusted."""
    second = core.create_account()
    core.create_account()
    service.set_default("2")
    assert service.machine_default() is not None

    shutil.rmtree(second.config_dir)  # moved by hand, not through `remove`

    arranged = service.list_accounts()
    assert _slots(arranged) == [1, 3]
    assert _positions(arranged) == [1, 2]
    assert service.machine_default() is None  # the default went with the directory
    with store_session() as store:
        assert [record.slot for record in store.claude_accounts()] == [1, 3]


def test_set_default_marks_exactly_one_slot_and_clear_unmarks_it(fake_home: Path) -> None:
    core.create_account()
    core.create_account()

    assert service.set_default("2") is not None
    assert [a.slot for a in service.list_accounts() if a.is_default] == [2]
    service.set_default("3")
    assert [a.slot for a in service.list_accounts() if a.is_default] == [3]  # moved, not added
    assert service.set_default(None) is None
    assert not any(a.is_default for a in service.list_accounts())
    with pytest.raises(service.NoSuchAccount, match="slot 9"):
        service.set_default("9")


def test_an_alias_resolves_case_insensitively_and_a_bad_or_taken_one_is_refused(
    fake_home: Path,
) -> None:
    second = core.create_account()
    third = core.create_account()

    named = service.set_alias("2", "Work")
    assert named.alias == "work" and core.label(named) == "work"  # stored lowercase
    assert service.resolve("WORK").slot == second.slot
    assert service.resolve("work").config_dir == second.config_dir
    with pytest.raises(service.AccountsError, match="already taken"):
        service.set_alias("3", "work")
    assert service.resolve("3").alias is None  # the refused write changed nothing
    for bad in ("", "  ", "123", "me@example.com", "-x", "Has Space", "a" * 33):
        with pytest.raises(ValueError):
            service.set_alias("3", bad)
    assert service.set_alias("2", None).alias is None
    with pytest.raises(service.NoSuchAccount, match="not a slot, an alias or a signed-in email"):
        service.resolve("work")
    assert third.slot == 3  # nothing above touched the other slot


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
    core.create_account()
    service.set_default("2")

    disabled = service.set_disabled("2", True)
    assert disabled.disabled and disabled.is_default  # still the default on paper…

    choice = service.choose(role="coder", project=None)
    assert choice.account is None  # …but never picked
    assert any("disabled" in note for note in choice.notes), choice.notes
    assert service.choose("2").account is not None  # the flag is the operator insisting
    assert service.set_disabled("2", False).disabled is False
    assert service.choose(role="coder", project=None).account is not None


# --------------------------------------------------------------------------- the resolver


def test_choose_walks_the_ladder_and_the_top_rung_wins(fake_home: Path, work: ProjectInfo) -> None:
    core.create_account()
    core.create_account()

    # Nothing arranged: no choice, and no source — the launch leaves its environment alone.
    nothing = service.choose(role="coder", project=work)
    assert nothing.account is None and nothing.source is None and nothing.notes == []

    service.set_default("2")
    machine = service.choose(role="coder", project=work)
    assert machine.account is not None and machine.account.slot == 2
    assert machine.source == "machine default"

    service.set_default("3", project=work)
    per_project = service.choose(role="coder", project=work)
    assert per_project.account is not None and per_project.account.slot == 3
    assert per_project.source == "project default"
    # Another project is untouched by this one's default.
    elsewhere = ProjectInfo(id="prj_other", root=Path("/elsewhere"))
    assert service.choose(role="coder", project=elsewhere).source == "machine default"

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
    core.create_account()
    core.create_account()
    settings_service.bind_role("coder", account="2")
    service.set_default("3")
    service.set_disabled("2", True)

    choice = service.choose(role="coder", project=work)

    assert choice.account is not None and choice.account.slot == 3  # fell through to the default
    assert choice.source == "machine default"
    assert choice.notes == ["account 2 (bound to coder) is disabled — skipped"]


# --------------------------------------------------------------------------- launch and spawn


def test_launch_runs_on_the_machine_default_and_stays_byte_identical_without_one(
    fake_home: Path, work: ProjectInfo, handover: dict[str, Any], runner: CliRunner
) -> None:
    second = core.create_account()

    # The control first: nothing arranged, nothing touched — the pre-#145 launch.
    plain = runner.invoke(app, ["launch", "coder"])
    assert plain.exit_code == 0, plain.output
    assert core.CONFIG_DIR_VAR not in handover["env"]
    assert "[" not in plain.stderr.split("Launching", 1)[1].split(" as ")[0]

    service.set_default("2")
    handover.clear()
    result = runner.invoke(app, ["launch", "coder"])
    assert result.exit_code == 0, result.output
    assert handover["env"][core.CONFIG_DIR_VAR] == str(second.config_dir)
    assert handover["env"][core.TMPDIR_VAR] == str(second.tmp_dir)
    assert "[account 2 · machine default]" in result.stderr

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


def test_launch_says_why_a_rung_was_skipped_and_refuses_a_dangling_one(
    fake_home: Path, work: ProjectInfo, handover: dict[str, Any], runner: CliRunner
) -> None:
    core.create_account()
    service.set_default("2")
    service.set_disabled("2", True)

    skipped = runner.invoke(app, ["launch", "coder"])
    assert skipped.exit_code == 0, skipped.output
    assert "accounts: account 2 (machine default) is disabled — skipped" in skipped.stderr
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
    service.set_default("2")
    service.set_default("2", project=work)
    service.set_alias("2", "work")
    core.create_account()  # slot 3, so the order has something left to renumber

    service.remove(second)

    assert service.machine_default() is None
    assert service.project_default(work) is None
    assert _slots(service.list_accounts()) == [1, 3]
    assert _positions(service.list_accounts()) == [1, 2]
    # THE reuse hazard: the next add takes slot 2 again and must inherit nothing.
    again = core.create_account()
    assert again.slot == 2
    fresh = service.resolve("2")
    assert fresh.alias is None and not fresh.is_default and not fresh.disabled
    with pytest.raises(service.NoSuchAccount):
        service.resolve("work")


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
    service.set_default("2")  # a real arrangement, about to become unreadable
    paths.db_path().write_bytes(CORRUPT)

    listing = runner.invoke(app, ["--json", "accounts", "list"])
    assert listing.exit_code == 0, listing.output
    rows = json.loads(listing.stdout)["accounts"]
    assert [row["account"]["slot"] for row in rows] == [1, 2]  # the directories still list
    assert not any(row["account"]["is_default"] for row in rows)  # the arrangement is what is lost

    launched = runner.invoke(app, ["launch", "coder"])
    assert launched.exit_code == 0, launched.output
    assert handover, "a damaged registry stopped the agent from starting"
    assert core.CONFIG_DIR_VAR not in handover["env"]  # no default could be read, so none applied
    assert "accounts registry unreadable" in launched.stderr
    assert "Traceback" not in launched.output

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

    machine = runner.invoke(app, ["accounts", "default", "2"])
    assert machine.exit_code == 0, machine.output
    assert "✓ machine default: slot 2" in machine.stdout

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
    starred = [line for line in listed.stdout.splitlines() if line.lstrip().startswith("*")]
    assert len(starred) == 1 and " 2 " in starred[0]  # exactly one star, on slot 2

    bindings = runner.invoke(app, ["team", "bind"])
    assert "coder" in bindings.stdout and "account=1" in bindings.stdout

    for args in (
        ["accounts", "default", "--clear", "--role", "coder"],
        ["accounts", "default", "--clear", "--project", "."],
        ["accounts", "default", "--clear"],
    ):
        cleared = runner.invoke(app, args)
        assert cleared.exit_code == 0, cleared.output
    assert (
        "coder" not in load_config().team.profiles
        or not load_config().team.profiles["coder"].account
    )
    assert service.machine_default() is None and service.project_default(work) is None

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


def test_accounts_alias_order_move_disable_and_enable_commands(
    fake_home: Path, runner: CliRunner
) -> None:
    core.create_account()
    core.create_account()

    alias = runner.invoke(app, ["accounts", "alias", "2", "Work"])
    assert alias.exit_code == 0 and "slot 2 is now called work" in alias.stdout
    bad = runner.invoke(app, ["--json", "accounts", "alias", "3", "42"])
    assert bad.exit_code == 1 and json.loads(bad.stdout)["error"] == "bad_alias"
    taken = runner.invoke(app, ["accounts", "alias", "3", "work"])
    assert taken.exit_code == 1 and "already taken" in taken.stderr

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

    empty = runner.invoke(app, ["--json", "team", "bind", "tester"])
    assert empty.exit_code == 1 and json.loads(empty.stdout)["error"] == "nothing_to_bind"


# --------------------------------------------------------------------------- doctor


def test_doctor_warns_when_the_default_cannot_launch_and_is_silent_when_nothing_is_arranged(
    fake_home: Path, work: ProjectInfo
) -> None:
    core.create_account()  # slot 2, signed in below
    third = core.create_account()  # slot 3, never signed in
    _sign_in(core.find_account(2) or third, "two@example.com")

    names = {check.name for check in diagnostics._claude_accounts_checks()}
    assert names == {"claude-accounts"}  # the store exists, nothing is arranged: no new lines

    service.set_default("3")
    checks = {check.name: check for check in diagnostics._claude_accounts_checks()}
    default = checks["claude-account-default"]
    assert default.status.value == "warn" and "not signed in" in default.detail
    assert default.fix is not None and "aisquare accounts run 3" in default.fix

    service.set_default("2")
    checks = {check.name: check for check in diagnostics._claude_accounts_checks()}
    assert checks["claude-account-default"].status.value == "ok"
    assert "slot 2" in checks["claude-account-default"].detail

    service.set_disabled("2", True)
    checks = {check.name: check for check in diagnostics._claude_accounts_checks()}
    assert checks["claude-account-default"].status.value == "warn"
    assert "disabled" in checks["claude-account-default"].detail

    settings_service.bind_role("coder", account="9")  # straight to config: `team bind` would refuse
    service.set_default("3", project=work)
    shutil.rmtree(third.config_dir)
    checks = {check.name: check for check in diagnostics._claude_accounts_checks()}
    dangling = checks["claude-account-bindings"]
    assert dangling.status.value == "warn"
    assert "role coder → 9" in dangling.detail
    assert f"project {work.root.name} → slot 3" in dangling.detail


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
