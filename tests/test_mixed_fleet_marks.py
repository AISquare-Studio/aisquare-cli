"""In a fleet or account table that mixes agents, every row says which agent it is (R16-E).

The live mixed Claude + Codex run (LIVE PASS 11884, the owner's rig at 5e652590) found:

- F3: with one Claude Code and one Codex agent spawned, the text ``fleet ls`` rows said
  ``account 3`` and ``account 4`` and not whose agent, and asq's sidebar rows drew the same
  role icons with no ``✻`` or ``>_``: "hard to tell apart", the owner's complaint (#127).
  Now, in a fleet of more than one agent's alone, ``fleet ls`` names each row's agent in
  the words ``aisquare accounts`` uses (``Claude Code``, ``Codex (beta)``), and each sidebar
  row carries its agent's mark after its icon. A fleet of one agent's lists as before.
- F4: a signed-in Codex account with no reading yet (no Codex session has recorded a
  rate-limit window on it) showed the ``!`` of a broken login. It reads ``no reading yet``,
  dim, with the reason on hover; a real problem (no login, an incomplete one, a failed
  read) keeps the ``!``.

The one-agent screens' byte-for-byte equality with the head before is proven by a headless
before/after capture (the DONE note of #129); these pin the rules.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from aisquare.cli import fleet as fleet_cli
from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.sidebar import AgentRow, agent_row_text
from aisquare.cli.ui.views import accounts as accounts_view
from aisquare.core import claude_accounts as accounts_core
from aisquare.core.agent_adapters import adapters
from aisquare.core.orchestrator import team_project
from aisquare.models import (
    AccountsOverview,
    ClaudeAccount,
    ClaudeAccountStatus,
    ClaudeIdentity,
    ClaudeInstall,
    ClaudeUsage,
    FleetAgent,
    FleetAgentStatus,
    UsageCause,
)
from tests.ui_workers import settle_page, settle_until_held

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
GLYPHS = {adapter.mark.glyph for adapter in adapters() if adapter.mark is not None}


def _row(
    label: str, agent: str | None, *, slot: int | None = None, project: str = "prj_1"
) -> FleetAgentStatus:
    return FleetAgentStatus(
        agent=FleetAgent(
            id=f"agt_{label}",
            project_id=project,
            label=label,
            role="coder",
            pane_id="%4",
            cwd=Path("/tmp/proj"),
            created_at=NOW,
            account_slot=slot,
            agent=agent,
        ),
        state="working",
    )


# --- F3: `fleet ls` --------------------------------------------------------------------------


def test_a_mixed_fleets_ls_names_each_rows_agent_in_the_accounts_words_aligned() -> None:
    rows = [
        _row("hello-claude", "claude-code", slot=3),
        _row("hello-codex", "codex", slot=4),
        _row("old-row", None, slot=2),  # a record from before adapters: Claude Code's
    ]
    words = fleet_cli.agent_words(rows)
    assert words == {
        "agt_hello-claude": "Claude Code ",
        "agt_hello-codex": "Codex (beta)",
        "agt_old-row": "Claude Code ",
    }
    lines = [fleet_cli._agent_line(row, {}, words[row.agent.id]) for row in rows]
    for line, said in zip(lines, ("Claude Code", "Codex (beta)", "Claude Code"), strict=True):
        assert f" coder      {said}" in line, line
    # The words are ASCII: a pipe and a narrow terminal read them as they are.
    assert all(said.isascii() for said in words.values())
    # Every row's state starts in one column, after the widest agent's words.
    assert len({line.index("▶") if "▶" in line else line.index("working") for line in lines}) == 1


def test_a_fleet_of_one_agents_lists_as_it_did() -> None:
    """No words, and each line exactly the line with none: a Claude-only fleet's ``fleet ls``
    is the head's before R16-E (the CHANGELOG's bar)."""
    rows = [_row("coder-a", "claude-code", slot=1), _row("coder-b", None, slot=2)]
    assert fleet_cli.agent_words(rows) == {}
    assert fleet_cli.agent_words([_row("codex-only", "codex")]) == {}


def test_fleet_ls_prints_the_agents_words_only_for_a_mixed_fleet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = team_project(tmp_path)
    fleet_cli._emit_agents(project, [_row("a", "claude-code"), _row("b", "claude-code")])
    alone = capsys.readouterr().out
    fleet_cli._emit_agents(project, [_row("a", "claude-code"), _row("b", "codex")])
    mixed = capsys.readouterr().out
    assert "Claude Code" not in alone and "Codex" not in alone
    assert "Claude Code" in mixed and "Codex (beta)" in mixed


# --- F3: the sidebar's agent rows ----------------------------------------------------------


def test_a_rows_mark_follows_its_icon_and_comes_off_the_labels_room() -> None:
    status = _row("coder-with-a-long-label", "codex", slot=4)
    mark = accounts_view.agent_mark("codex")
    plain = agent_row_text(status, 42.0, width=30).plain
    marked = agent_row_text(status, 42.0, width=30, mark=mark).plain
    assert ">_ coder" in marked and ">_" not in plain
    assert len(marked) == len(plain)  # the label gave way, the row is as wide
    assert marked.endswith("#4·42%") and plain.endswith("#4·42%")
    unmarked = agent_row_text(status, 42.0, width=30, mark=None)
    assert unmarked == agent_row_text(status, 42.0, width=30)


def _overview() -> AccountsOverview:
    return AccountsOverview(claude=ClaudeInstall(installed=True), accounts=[])


def test_the_sidebar_marks_its_rows_only_while_the_fleet_mixes_agents(tmp_path: Path) -> None:
    """One agent's fleet: no mark. A Codex agent joins: every row, the unchanged Claude one
    too, carries its agent's mark. It leaves: the marks go."""
    project = team_project(tmp_path)
    claude = _row("coder-claude", "claude-code", slot=3, project=project.id)
    codex = _row("coder-codex", "codex", slot=4, project=project.id)

    async def run() -> list[dict[str, str]]:
        app = FleetApp(refresh_seconds=3600, doctor=lambda: [], accounts=_overview)
        seen: list[dict[str, str]] = []
        async with app.run_test(size=(120, 40)):
            await settle_page(app)
            for frame in ([claude], [claude, codex], [claude]):
                app.sidebar.show_projects([project], {project.id: frame})
                await settle_page(app)
                ids = {status.agent.id for status in frame}
                await settle_until_held(
                    app,
                    lambda ids=ids: {row.status.agent.id for row in app.query(AgentRow)} >= ids,
                    what="the frame's rows",
                )
                seen.append(
                    {
                        row.status.agent.label: str(row.render())
                        for row in app.query(AgentRow)
                        if row.status.agent.id in ids
                    }
                )
        return seen

    alone, mixed, alone_again = asyncio.run(run())
    assert not any(glyph in alone["coder-claude"] for glyph in GLYPHS), alone
    assert "✻  coder-claude" in mixed["coder-claude"], mixed  # ✻ padded to >_'s two cells
    assert ">_ coder-codex" in mixed["coder-codex"], mixed
    assert alone_again == alone


# --- F4: a Codex account with no reading yet -----------------------------------------------


def _status(agent: str, *, identity: bool = True, signed_in: bool = True) -> ClaudeAccountStatus:
    account = ClaudeAccount(
        slot=4,
        config_dir=accounts_core.accounts_root(agent) / "4",
        managed=True,
        agent=agent,
    )
    return ClaudeAccountStatus(
        account=account,
        label="account 4",
        identity=ClaudeIdentity(email="me@example.com") if identity else None,
        signed_in=signed_in,
        token_state="ok" if signed_in else "missing",
        subscription="team",
    )


READ_FAILED = ClaudeUsage(available=False, reason="unreadable", cause=UsageCause.read_failed)
UNREACHABLE = ClaudeUsage(available=False, reason="unreachable", cause=UsageCause.unreachable)
NOTHING_RECORDED = ClaudeUsage(
    available=False,
    reason="no Codex session on this account has recorded a rate-limit window yet",
)


def test_a_signed_in_codex_account_with_no_reading_yet_reads_so_quietly() -> None:
    status = _status("codex")
    cells = accounts_view.account_cells(status, NOTHING_RECORDED, now=NOW)
    assert cells["session"].plain == accounts_view.NO_READING_YET == "no reading yet"
    assert str(cells["session"].style) == "dim"
    tip = accounts_view.problem_tooltip(status, NOTHING_RECORDED, now=NOW)
    assert tip is not None and tip.startswith(NOTHING_RECORDED.reason or "")
    assert not accounts_view.needs_login(status, NOTHING_RECORDED)


@pytest.mark.parametrize(
    ("agent", "status_kwargs", "usage"),
    [
        ("codex", {"identity": False, "signed_in": False}, NOTHING_RECORDED),  # no login
        ("codex", {"signed_in": False}, NOTHING_RECORDED),  # an incomplete login
        ("codex", {}, READ_FAILED),
        ("claude-code", {}, UNREACHABLE),
        ("claude-code", {}, NOTHING_RECORDED),  # a server's reading with no cause is no "yet"
    ],
    ids=[
        "codex-no-login",
        "codex-incomplete",
        "codex-read-failed",
        "claude-unreachable",
        "claude-no-cause",
    ],
)
def test_a_real_problem_keeps_its_mark(
    agent: str, status_kwargs: dict[str, Any], usage: ClaudeUsage
) -> None:
    status = _status(agent, **status_kwargs)
    assert not accounts_view.awaits_first_reading(status, usage)
    cells = accounts_view.account_cells(status, usage, now=NOW)
    assert cells["session"].plain == accounts_view.PROBLEM_MARK
