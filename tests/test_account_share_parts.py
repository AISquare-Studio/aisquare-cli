"""The guarantees under ``AgentAccounts.share_parts`` that no one agent's test owns (#113's #22,
the tester's reopen).

0.9.0 held only linked parts in ``LINKED_PARTS``, so ``memory`` could never reach
``core.claude_sharing.link_part``. Now every part is a :class:`SharePart` and a merged one
(``how="merge"``, an entry of ``""``) is one filter away from it: linked, its "entry" is the
slot's whole config dir, which would be renamed aside and replaced by a link to the plain
home. So the guarantee is pinned twice, where each part is told apart (``part_state``,
``link_part``, ``unlink_part``) and where a launch asks for it (``share_for_launch``), and what
a launch must never move is pinned too: the plain home's own entries. Then the doctor's rows
on top of it: the part linked elsewhere, the names an agent does not declare, and a name no
agent declares.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli import accounts as accounts_cli
from aisquare.cli.app import app
from aisquare.core import claude_accounts as core
from aisquare.core import claude_sharing as sharing
from aisquare.core.agent_adapters.types import SharePart
from aisquare.core.config import load_config, save_config
from aisquare.models import ClaudeAccount
from aisquare.services import claude_accounts as service
from aisquare.services import diagnostics
from tests.fsperms import can_symlink
from tests.test_claude_accounts import _sign_in as claude_sign_in
from tests.test_codex_accounts import _sign_in as codex_sign_in

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)

MERGED = SharePart("memory", how="merge")
"""A merged part as an adapter declares one: no entry, nothing to link."""
MERGED_WITH_ENTRY = SharePart("notes", "notes.d", how="merge")
"""A merged part that also names a folder: the entry is there, the part is still not linked."""
NO_ENTRY = SharePart("nowhere", "")
"""A linked part that names no entry: what a declaration left empty would be."""


@pytest.fixture
def homes(fake_home: Path) -> dict[str, tuple[ClaudeAccount, ClaudeAccount, ClaudeAccount]]:
    """Each agent's plain home with a ``skills/`` and a managed slot of its own, signed in:
    the plain home, a slot with a skill of its own, and one with none."""
    if not can_symlink():  # pragma: no cover - a privilege CI holds, a laptop may not
        pytest.skip("this machine cannot create symlinks")
    found: dict[str, tuple[ClaudeAccount, ClaudeAccount, ClaudeAccount]] = {}
    for agent in ("claude-code", "codex"):
        plain = core.default_account(agent=agent)
        plain.config_dir.mkdir(parents=True, exist_ok=True)
        second, third = core.create_account(agent), core.create_account(agent)
        for account, email in ((second, "two@x.dev"), (third, "three@x.dev")):
            _sign_in(agent, account, email)
        plain_skill = plain.config_dir / "skills" / "plain-skill"
        plain_skill.mkdir(parents=True)
        (plain_skill / "SKILL.md").write_text("plain's\n")
        (second.config_dir / "skills" / "mine").mkdir(parents=True)
        (second.config_dir / "skills" / "mine" / "SKILL.md").write_text("slot two's\n")
        found[agent] = (plain, second, third)
    return found


def _sign_in(agent: str, account: ClaudeAccount, email: str) -> None:
    if agent == "claude-code":
        claude_sign_in(account, email)
    else:
        codex_sign_in(account.config_dir, email)


def _listing(home: Path) -> list[str]:
    """What ``home`` holds at its top, each name once: the whole of a slot's config dir."""
    return sorted(p.name for p in home.iterdir())


# --- a merged part is no entry: every function that reads or moves one refuses it ----------


@pytest.mark.parametrize(
    "act",
    [
        pytest.param(lambda d, p, s: sharing.part_entry(d, p), id="part_entry"),
        pytest.param(lambda d, p, s: sharing.part_state(d, p, s), id="part_state"),
        pytest.param(lambda d, p, s: sharing.link_part(d, p, s, now=NOW), id="link_part"),
        pytest.param(lambda d, p, s: sharing.unlink_part(d, p, s), id="unlink_part"),
    ],
)
@pytest.mark.parametrize(
    "part", [MERGED, MERGED_WITH_ENTRY, NO_ENTRY], ids=["merged", "merged-with-entry", "no-entry"]
)
def test_a_part_with_nothing_to_link_is_refused_by_every_function_that_takes_one(
    tmp_path: Path, act: Callable[[Path, SharePart, Path], object], part: SharePart
) -> None:
    config, plain = tmp_path / "slot", tmp_path / "plain"
    (config / "skills").mkdir(parents=True)
    (config / "auth.json").write_text("{}")
    plain.mkdir()
    before = _listing(config)

    with pytest.raises(sharing.SharingError, match=part.name):
        act(config, part, plain)

    assert _listing(config) == before, "the slot's home is where it was"
    assert not config.is_symlink() and _listing(plain) == []
    assert not list(tmp_path.glob("slot.own-*")), "no copy of the whole home was kept aside"


def test_the_name_of_a_part_that_is_merged_is_refused_too(tmp_path: Path) -> None:
    """Claude Code's ``memory``, said as the operator writes it (``claude_sharing`` takes a name
    for the callers that spell Claude Code's parts)."""
    config, plain = tmp_path / "slot", tmp_path / "plain"
    config.mkdir()
    plain.mkdir()

    with pytest.raises(sharing.SharingError, match="memory"):
        sharing.part_entry(config, "memory")
    with pytest.raises(sharing.SharingError, match="memory"):
        sharing.part_state(config, "memory", plain)
    with pytest.raises(sharing.SharingError, match="memory"):
        sharing.link_part(config, "memory", plain, now=NOW)
    with pytest.raises(sharing.SharingError, match="memory"):
        sharing.unlink_part(config, "memory", plain)
    assert config.is_dir() and not config.is_symlink() and not list(tmp_path.glob("slot.own-*"))


def test_a_merged_part_the_service_is_asked_to_link_is_refused_and_moves_nothing(
    homes: dict[str, tuple[ClaudeAccount, ClaudeAccount, ClaudeAccount]],
) -> None:
    """``services._link_part`` is what a launch and ``enable_share`` call: handed the merged
    part by a caller that forgot to ask its ``how``, it answers ``refused``."""
    _plain, second, _third = homes["claude-code"]
    memory = next(p for p in core.kind("claude-code").share_parts if p.how == "merge")
    before = _listing(second.config_dir)

    change = service._link_part(second, memory, core.default_account().config_dir, NOW)

    assert change.outcome == "refused" and "memory" in (change.detail or "")
    assert _listing(second.config_dir) == before
    assert not second.config_dir.is_symlink()
    assert not list(second.config_dir.parent.glob(f"{second.config_dir.name}.own-*"))


# --- a launch with memory on moves nothing of the slot's --------------------------------


@pytest.mark.parametrize("agent", ["claude-code", "codex"])
def test_a_launch_with_the_merged_part_on_leaves_a_managed_slots_home_as_it_was(
    homes: dict[str, tuple[ClaudeAccount, ClaudeAccount, ClaudeAccount]], agent: str
) -> None:
    """``memory`` on is a setting every Claude launch is handed (``AgentAccounts.memory``);
    ``share_for_launch`` links the parts that are linked and no other. With the part on for a
    managed slot, its whole config dir is still its own: no link, no ``.own-`` copy of it, no
    note that anything was shared or refused."""
    plain, second, third = homes[agent]
    config = load_config()
    config.accounts.share = ["memory"]
    save_config(config)
    before = {slot.slot: _listing(slot.config_dir) for slot in (second, third)}
    plain_before = _listing(plain.config_dir)

    for slot in (second, third):
        assert service.share_for_launch(account=slot) == []

    for slot in (second, third):
        assert _listing(slot.config_dir) == before[slot.slot]
        assert not slot.config_dir.is_symlink()
        assert not list(slot.config_dir.parent.glob(f"{slot.config_dir.name}.own-*"))
    assert _listing(plain.config_dir) == plain_before, "nothing of a slot was seeded into it"


# --- the plain home is the source: a launch on it moves none of its own ---------------------


@pytest.mark.parametrize("agent", ["claude-code", "codex"])
def test_a_launch_on_the_plain_home_never_moves_its_own_entries_aside(
    homes: dict[str, tuple[ClaudeAccount, ClaudeAccount, ClaudeAccount]], agent: str
) -> None:
    """``[accounts] share`` links the MANAGED slots' entries to the plain home's. A launch on
    the plain home itself, with ``skills`` on, would otherwise rename its own ``skills/``
    aside and link it to itself: the source, gone, for every slot that follows it."""
    plain, _second, _third = homes[agent]
    service.enable_share(["skills"], now=NOW)
    skills = core.kind(agent).config_dir(plain.config_dir) / "skills"
    before = sorted(p.name for p in skills.rglob("*"))

    assert service.share_for_launch(account=plain) == []

    assert skills.is_dir() and not skills.is_symlink()
    assert sorted(p.name for p in skills.rglob("*")) == before
    assert not list(skills.parent.glob("skills.own-*"))


# --- the doctor's row, over what an adapter declares --------------------------------------


@pytest.mark.parametrize("agent", ["claude-code", "codex"])
def test_the_doctor_row_warns_on_a_slot_whose_part_links_elsewhere(
    homes: dict[str, tuple[ClaudeAccount, ClaudeAccount, ClaudeAccount]],
    agent: str,
    tmp_path: Path,
) -> None:
    _plain, second, third = homes[agent]
    service.enable_share(["skills"], now=NOW)
    elsewhere = tmp_path / "someone-elses-skills"
    elsewhere.mkdir()
    here = core.kind(agent).config_dir(third.config_dir) / "skills"
    here.unlink()
    here.symlink_to(elsewhere, target_is_directory=True)

    row = diagnostics._accounts_sharing_check(agent, core.managed_accounts(agent))

    assert row is not None and row.status.value == "warn"
    assert f"slot {third.slot}'s skills links elsewhere" in row.detail
    assert f"slot {second.slot}" not in row.detail, "the slot that does share is no problem"
    assert row.name == f"{core.kind(agent).dir_name} sharing"


def test_a_row_reads_only_the_names_its_own_agent_declares(
    homes: dict[str, tuple[ClaudeAccount, ClaudeAccount, ClaudeAccount]],
) -> None:
    """``[accounts] share`` is one list for every agent. ``agents_md`` is Codex's alone and
    ``claude_md`` Claude Code's: each row names, and checks, what its own agent has."""
    config = load_config()
    config.accounts.share = ["agents_md", "claude_md", "skills"]
    save_config(config)
    managed = {agent: core.managed_accounts(agent) for agent in homes}

    claude = diagnostics._accounts_sharing_check("claude-code", managed["claude-code"])
    codex = diagnostics._accounts_sharing_check("codex", managed["codex"])

    assert claude is not None and codex is not None
    assert claude.detail.startswith("sharing claude_md, skills"), claude.detail
    assert "agents_md" not in claude.detail
    assert codex.detail.startswith("sharing agents_md, skills"), codex.detail
    assert "claude_md" not in codex.detail

    config.accounts.share = ["agents_md"]
    save_config(config)
    assert diagnostics._accounts_sharing_check("claude-code", managed["claude-code"]) is None, (
        "a name only Codex declares is nothing to say about Claude Code's slots"
    )


def test_a_name_no_agent_declares_is_a_warn_the_per_agent_rows_cannot_give(
    homes: dict[str, tuple[ClaudeAccount, ClaudeAccount, ClaudeAccount]],
) -> None:
    """0.9.0's ``share`` was a ``Literal``, which rejected ``skils``; it is a list of names now
    and each agent's row reads its own, so a typo is no one's. The row for the machine says so."""
    assert diagnostics._undeclared_share_check() is None, "nothing on: no row"
    config = load_config()
    config.accounts.share = ["skills", "agents_md", "claude_md", "memory"]
    save_config(config)
    assert diagnostics._undeclared_share_check() is None, "every name is some agent's"

    config.accounts.share = ["skils", "skills", "agentz"]
    save_config(config)
    row = diagnostics._undeclared_share_check()

    assert row is not None and row.name == "accounts sharing" and row.status.value == "warn"
    assert "names skils, agentz," in row.detail, "the typos, and not skills, which is some agent's"
    assert "parts are memory, skills" in row.detail
    assert "aisquare config set accounts.share" in (row.fix or "")
    assert "accounts sharing" in [check.name for check in diagnostics.doctor()], (
        "it is part of the doctor"
    )


# --- the words of ``accounts share`` come from the slot -----------------------------------


def test_a_link_is_worded_by_the_plain_home_of_its_own_agent(
    homes: dict[str, tuple[ClaudeAccount, ClaudeAccount, ClaudeAccount]], runner: CliRunner
) -> None:
    """Every link was "linked to the plain claude's", whatever the agent: the Codex slot's
    line named another agent's home."""
    result = runner.invoke(app, ["accounts", "share", "--enable", "skills"])

    assert result.exit_code == 0, result.output
    lines = [line.strip() for line in result.output.splitlines() if " skills: " in line]
    by_slot = {slot: line for slot in range(2, 8) for line in lines if f"account {slot} " in line}
    claude_slot, codex_slot = homes["claude-code"][2], homes["codex"][2]
    assert "linked to the plain claude's" in by_slot[claude_slot.slot], lines
    assert "linked to the plain codex's" in by_slot[codex_slot.slot], lines
    assert "plain claude" not in by_slot[codex_slot.slot]
    assert "plain codex" not in by_slot[claude_slot.slot]


def test_the_help_names_the_parts_each_agent_declares_and_the_experimental_ones() -> None:
    """The help is built from ``AgentAccounts.share_parts``: ``plugins`` is experimental
    because its part says so (``SharePart.experimental``), and no other is called that."""
    help_text = accounts_cli._share_help()

    for agent in core.account_agents():
        for part in core.kind(agent).share_parts:
            assert part.name in help_text
    assert "`plugins` is experimental" in help_text
    experimental = [
        part.name
        for agent in core.account_agents()
        for part in core.kind(agent).share_parts
        if part.experimental
    ]
    assert experimental == ["plugins"]
    for name in ("skills", "memory", "agents_md", "claude_md"):
        assert f"`{name}` is experimental" not in help_text
    memory = core.kind("claude-code").memory
    assert memory is not None and f"~/.aisquare/{memory.root_name}/memory" in help_text


def test_json_form_is_unchanged_by_the_wording(
    homes: dict[str, tuple[ClaudeAccount, ClaudeAccount, ClaudeAccount]], runner: CliRunner
) -> None:
    on = runner.invoke(app, ["--json", "accounts", "share", "--enable", "skills"])
    assert on.exit_code == 0, on.output
    outcomes = {(c["slot"], c["part"]): c["outcome"] for c in json.loads(on.output)["changes"]}
    assert set(outcomes.values()) == {"linked"} and len(outcomes) == 4
