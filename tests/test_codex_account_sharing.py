"""Codex accounts share what Claude Code's do, through one adapter slot (#113's #22, 0.9.0's
P2-W7 ``[accounts] share``): ``AgentAccounts.share_parts``.

Which parts an agent shares is its adapter's. Codex's are its ``AGENTS.md`` and its
``skills`` folder (``agent_adapters/codex_shared.py``), measured on Codex 0.164.0 and 0.162.1
to be read through a link; ``prompts`` is no part, since neither release reads
``$CODEX_HOME/prompts`` at all. Linking is every agent's (``core.claude_sharing.link_part``), so
what ``tests/test_claude_sharing.py`` pins for Claude Code is pinned here for Codex over
synthetic homes under ``tmp_path``: link with the account's own kept beside it, a part linked
elsewhere refused, unshare restoring exactly, a slot made later linked at its launch, and the
doctor's ``codex-accounts sharing`` row. No ``codex`` runs.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import claude_accounts as core
from aisquare.core.config import load_config
from aisquare.models import ClaudeAccount
from aisquare.services import claude_accounts as service
from aisquare.services import diagnostics
from tests.fsperms import can_symlink
from tests.test_claude_accounts import _sign_in as claude_sign_in
from tests.test_codex_accounts import _sign_in

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def _plain() -> ClaudeAccount:
    """The plain Codex home, made: it is listed once it exists."""
    plain = core.default_account(agent="codex")
    plain.config_dir.mkdir(parents=True, exist_ok=True)
    return plain


def _slot(email: str) -> ClaudeAccount:
    account = core.create_account("codex")
    _sign_in(account.config_dir, email)
    return account


def _tree(home: Path) -> dict[str, str | None]:
    """Every file under a slot's home that is not its login, by relative path (a folder is
    ``None``): what ``unshare`` has to give back exactly."""
    found: dict[str, str | None] = {}
    for path in sorted(home.rglob("*")):
        relative = path.relative_to(home).as_posix()
        if relative.startswith(".aisquare") or relative == "auth.json":
            continue
        found[relative] = path.read_text(encoding="utf-8") if path.is_file() else None
    return found


@pytest.fixture
def two(fake_home: Path) -> tuple[ClaudeAccount, ClaudeAccount, ClaudeAccount]:
    """The plain Codex home with a skill and an AGENTS.md, a managed slot with a skill and
    an AGENTS.md of its own, and one with neither."""
    if not can_symlink():  # pragma: no cover - a privilege CI holds, a laptop may not
        pytest.skip("this machine cannot create symlinks")
    plain, second, third = _plain(), _slot("two@x.dev"), _slot("three@x.dev")
    (plain.config_dir / "skills" / "shared-skill").mkdir(parents=True)
    (plain.config_dir / "skills" / "shared-skill" / "SKILL.md").write_text("shared\n")
    (plain.config_dir / "AGENTS.md").write_text("plain rules\n")
    (second.config_dir / "skills" / "mine").mkdir(parents=True)
    (second.config_dir / "skills" / "mine" / "SKILL.md").write_text("slot two's\n")
    (second.config_dir / "AGENTS.md").write_text("slot two rules\n")
    return plain, second, third


def test_codex_declares_its_agents_md_and_its_skills_and_no_prompts_or_memory() -> None:
    from aisquare.core.agent_adapters import get_adapter

    accounts = get_adapter("codex").accounts
    assert accounts is not None and accounts.memory is None
    assert [(p.name, p.entry, p.is_dir, p.how) for p in accounts.share_parts] == [
        ("agents_md", "AGENTS.md", False, "link"),
        ("skills", "skills", True, "link"),
    ]
    assert "agents_md" in service.share_part_names() and "prompts" not in service.share_part_names()


def test_enable_links_every_codex_slot_keeps_its_own_and_disable_restores_it_exactly(
    two: tuple[ClaudeAccount, ClaudeAccount, ClaudeAccount],
) -> None:
    _plain, second, third = two
    before = {account.slot: _tree(account.config_dir) for account in (second, third)}

    changes = service.enable_share(["skills", "agents_md"], now=NOW)

    outcome = {(c.account.slot, c.part): (c.outcome, c.detail) for c in changes}
    assert outcome == {
        (second.slot, "skills"): ("linked", "skills.own-20261023T120000Z"),
        (third.slot, "skills"): ("linked", None),
        (second.slot, "agents_md"): ("linked", "AGENTS.md.own-20261023T120000Z"),
        (third.slot, "agents_md"): ("linked", None),
    }
    for account in (second, third):
        assert (account.config_dir / "skills" / "shared-skill" / "SKILL.md").read_text() == (
            "shared\n"
        )
        assert (account.config_dir / "AGENTS.md").read_text() == "plain rules\n"
    kept = second.config_dir / "skills.own-20261023T120000Z"
    assert (kept / "mine" / "SKILL.md").read_text() == "slot two's\n"
    assert (second.config_dir / "skills" / "mine" / "SKILL.md").read_text() == "slot two's\n", (
        "what the slot had and the plain home lacked went there"
    )
    assert load_config().accounts.share == ["skills", "agents_md"]

    again = service.enable_share(["skills", "agents_md"], now=NOW)
    assert {c.outcome for c in again} == {"already"}, "idempotent"

    service.disable_share(["skills", "agents_md"])

    assert (second.config_dir / "AGENTS.md").read_text() == "slot two rules\n"
    assert (second.config_dir / "skills" / "mine" / "SKILL.md").read_text() == "slot two's\n"
    assert not (second.config_dir / "skills" / "shared-skill").exists()
    assert (
        not (third.config_dir / "AGENTS.md").exists() and not (third.config_dir / "skills").exists()
    )
    assert load_config().accounts.share == []
    assert {account.slot: _tree(account.config_dir) for account in (second, third)} == before, (
        "every file and folder of each slot is back, byte for byte"
    )
    again_off = service.disable_share(["skills"])
    assert [(c.account.slot, c.outcome) for c in again_off] == [
        (second.slot, "own"),
        (third.slot, "none"),
    ]


def test_a_part_linked_elsewhere_is_refused_and_left_alone(
    two: tuple[ClaudeAccount, ClaudeAccount, ClaudeAccount], tmp_path: Path
) -> None:
    _plain, second, _third = two
    theirs = tmp_path / "someone-elses-skills"
    theirs.mkdir()
    (second.config_dir / "skills").rename(second.config_dir / "was-skills")
    (second.config_dir / "skills").symlink_to(theirs, target_is_directory=True)

    [refused] = [
        c for c in service.enable_share(["skills"], now=NOW) if c.account.slot == second.slot
    ]

    assert refused.outcome == "refused" and "not ours to replace" in (refused.detail or "")
    assert (second.config_dir / "skills").resolve() == theirs.resolve()
    held = {(c.account.slot, c.part): c.outcome for c in service.share_status()}
    assert held[(second.slot, "skills")] == "elsewhere"


def test_the_file_the_plain_home_has_none_of_is_refused_not_hidden_behind_an_empty_one(
    two: tuple[ClaudeAccount, ClaudeAccount, ClaudeAccount],
) -> None:
    plain, second, _third = two
    (plain.config_dir / "AGENTS.md").unlink()

    [refused] = [
        c for c in service.enable_share(["agents_md"], now=NOW) if c.account.slot == second.slot
    ]

    assert refused.outcome == "refused" and "has no AGENTS.md to share" in (refused.detail or "")
    assert (second.config_dir / "AGENTS.md").read_text() == "slot two rules\n"


def test_a_part_codex_does_not_declare_is_skipped_for_it_and_claudes_alone_is_linked(
    two: tuple[ClaudeAccount, ClaudeAccount, ClaudeAccount],
) -> None:
    """``claude_md`` is Claude Code's name for its file: a Codex slot has no such part."""
    claude = core.create_account()
    (core.default_account().config_dir / "CLAUDE.md").write_text("claude rules\n")

    changes = service.enable_share(["claude_md"], now=NOW)

    assert {(c.account.agent, c.account.slot) for c in changes} == {("claude-code", claude.slot)}
    assert (claude.config_dir / "CLAUDE.md").read_text() == "claude rules\n"
    assert all(c.part != "claude_md" for c in service.share_status() if c.account.agent == "codex")


def test_one_switch_shares_a_part_two_agents_both_declare(
    two: tuple[ClaudeAccount, ClaudeAccount, ClaudeAccount],
) -> None:
    """``skills`` is Claude Code's and Codex's: one name, each linked into its own entry."""
    claude = core.create_account()
    (core.default_account().config_dir / "skills").mkdir()

    changes = service.enable_share(["skills"], now=NOW)

    agents = {c.account.agent for c in changes if c.outcome == "linked"}
    assert agents == {"claude-code", "codex"}
    plain_claude = core.default_account().config_dir / "skills"
    assert (claude.config_dir / "skills").resolve() == plain_claude.resolve()
    plain_codex = core.default_account(agent="codex").config_dir / "skills"
    assert (two[2].config_dir / "skills").resolve() == plain_codex.resolve()


def test_a_codex_launch_links_a_slot_made_after_the_part_was_turned_on(
    two: tuple[ClaudeAccount, ClaudeAccount, ClaudeAccount],
) -> None:
    service.enable_share(["skills", "agents_md"], now=NOW)
    fourth = _slot("four@x.dev")

    notes = service.share_for_launch(account=fourth)

    assert (fourth.config_dir / "skills").resolve() == (
        core.default_account(agent="codex").config_dir / "skills"
    ).resolve()
    assert (fourth.config_dir / "AGENTS.md").read_text() == "plain rules\n"
    assert any("now shares the plain codex's skills" in note for note in notes), notes
    assert service.share_for_launch(account=fourth) == [] or all(
        "now shares" not in note for note in service.share_for_launch(account=fourth)
    ), "the second launch has nothing to link"


def test_accounts_share_reads_the_parts_off_the_adapters(
    two: tuple[ClaudeAccount, ClaudeAccount, ClaudeAccount], runner: CliRunner
) -> None:
    on = runner.invoke(app, ["--json", "accounts", "share", "--enable", "agents_md,skills"])
    assert on.exit_code == 0, on.output
    report = json.loads(on.output)
    assert report["share"] == ["skills", "agents_md"], "in the order the parts are declared"
    assert {(c["slot"], c["part"], c["outcome"]) for c in report["changes"]} == {
        (two[1].slot, "agents_md", "linked"),
        (two[2].slot, "agents_md", "linked"),
        (two[1].slot, "skills", "linked"),
        (two[2].slot, "skills", "linked"),
    }
    status = json.loads(runner.invoke(app, ["--json", "accounts", "share", "--status"]).output)
    held = {(c["slot"], c["part"]): c["outcome"] for c in status["status"]}
    assert held[(two[1].slot, "agents_md")] == "shared"
    bad = runner.invoke(app, ["accounts", "share", "--enable", "prompts"])
    assert bad.exit_code == 2 and "parts are memory" in bad.output and "agents_md" in bad.output


# --- the doctor ---------------------------------------------------------------------------------


def test_the_doctor_row_for_a_codex_slot_names_its_agent_and_warns_on_a_part_it_does_not_share(
    two: tuple[ClaudeAccount, ClaudeAccount, ClaudeAccount],
) -> None:
    from aisquare.core.agent_adapters.types import SharePart  # noqa: F401 - the slot's type

    managed = core.managed_accounts("codex")
    assert diagnostics._accounts_sharing_check("codex", managed) is None, "nothing shared: no row"

    service.enable_share(["skills"], now=NOW)
    row = diagnostics._accounts_sharing_check("codex", managed)
    assert row is not None and row.name == "codex-accounts sharing" and row.status.value == "ok"
    assert row.detail == "sharing skills"

    (two[2].config_dir / "skills").unlink()
    (two[2].config_dir / "skills").mkdir()
    row = diagnostics._accounts_sharing_check("codex", managed)
    assert row is not None and row.status.value == "warn"
    assert f"slot {two[2].slot}'s skills is its own" in row.detail
    assert "keep transcripts apart" not in (row.fix or ""), "Codex has no projects/ to keep apart"


def test_the_codex_doctor_row_is_part_of_the_doctor_and_claudes_keeps_its_name(
    two: tuple[ClaudeAccount, ClaudeAccount, ClaudeAccount],
) -> None:
    claude_sign_in(core.create_account(), "claude@x.dev")
    service.enable_share(["skills"], now=NOW)

    names = [check.name for check in diagnostics._claude_accounts_checks()]
    other = [check.name for check in diagnostics._other_accounts_checks()]

    assert "claude-accounts sharing" in names
    assert "codex-accounts sharing" in other


def test_a_codex_launch_gets_no_memory_setting_however_memory_is_shared(
    two: tuple[ClaudeAccount, ClaudeAccount, ClaudeAccount],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Codex has no setting that points its memory anywhere (``AgentAccounts.memory`` is
    ``None``): with ``memory`` on for Claude Code, its launch arguments are what they were,
    and an ``--settings`` on the operator's line is nothing it stands down for."""
    from aisquare.cli import launch as launch_cli

    service.enable_share(["memory"], now=NOW)

    assert launch_cli._with_shared_memory("codex", ["-c", "x=1"], ["--settings", "{}"]) == [
        "-c",
        "x=1",
    ]
    assert "memory is not shared" not in capsys.readouterr().err
    assert service.shared_memory_directory("codex", Path.cwd()) is None
