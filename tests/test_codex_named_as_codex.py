"""R16-F (live findings F1 and F2 of R16-D, seq 11884): Codex rows and a Codex manager are named
as Codex, never as Claude, and Claude's text stays byte-identical.

- F2: ``accounts usage`` text has no agent column. A Codex row said `` · codex`` only once it
  had a reading, so before its first session ``account 4`` signed in as Claude account 2's
  email read as a third Claude account (the owner: "both are the same account"). Every row
  of an agent other than Claude Code now names its agent, read or not.
- F1: the Manager tab told a Codex manager to "type to the manager as you would to any Claude
  session". The hint names the manager's own agent (``agent_noun``). So does the line a
  resumed agent is typed after a switch ("moved to another Claude account"), the one other
  Claude-only wording found that a Codex agent reads.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from textual.geometry import Region
from textual.pilot import Pilot
from textual.widget import Widget
from textual.widgets import Static
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.cli.ui.sidebar import ProjectTitle
from aisquare.cli.ui.views.project import MANAGER_HINT, manager_hint
from aisquare.core import claude_accounts as core
from aisquare.models import FleetAgent
from aisquare.services import claude_accounts as service
from aisquare.services import fleet as fleet_service
from tests import test_ui_shell as ui_suite
from tests.test_codex_accounts import _codex_slot, _installed
from tests.test_ui_shell import Script, card_for, drive, fleet_app, on_slot, seed, settle
from tests.test_usage_aware_accounts import _payload, _slot, _Usage

# The tmux guard and the scripted listing of the UI suite (``no_real_tmux`` is autouse there:
# every tmux call is held to a private socket).
no_real_tmux = ui_suite.no_real_tmux
script = ui_suite.script

CLAUDE_HINT = "type to the manager as you would to any Claude session · F12 returns to the sidebar"
"""0.9.2's line, word for word: what a Claude Code manager's tab still says."""
CODEX_HINT = "type to the manager as you would to any Codex session · F12 returns to the sidebar"


def _usage_rows(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """``accounts usage``'s text rows, keyed by the slot reference that starts each (wide
    enough that no row wraps)."""
    monkeypatch.setenv("COLUMNS", "200")
    result = runner.invoke(app, ["accounts", "usage"])
    assert result.exit_code == 0, result.output
    rows = {}
    for line in result.stdout.splitlines()[1:]:
        if line[:1].strip() and not line.startswith(("all", "Fleet", "usage as of")):
            rows[line.split()[0]] = line
    return rows


# --------------------------------------------------------------------------- F2


def test_a_codex_row_names_its_agent_before_its_first_reading(
    fake_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's rig before any Codex session: a Codex account signed in as the email of a
    Claude account, with no rollout yet. Its row says ``· codex``; the Claude rows say
    nothing of an agent, as they always have."""
    _installed(monkeypatch, claude="/opt/bin/claude", codex="/opt/bin/codex")
    claude = _slot("same@example.com", "tok-two")  # slot 2
    codex = _codex_slot("same@example.com")  # slot 3, no session yet
    plain_home = core.kind("codex").plain_home(core._home(), {})  # CODEX_HOME unset
    plain_home.mkdir(parents=True, exist_ok=True)  # the rig's ~/.codex: no auth.json in it
    monkeypatch.setattr(service, "_http_get", _Usage({"tok-two": _payload(3)}))

    rows = _usage_rows(runner, monkeypatch)

    codex_row = rows[str(codex.slot)]
    assert re.match(rf"{codex.slot}\s+account {codex.slot} · codex\s+same@example\.com", codex_row)
    assert "no Codex session on this account has recorded" in codex_row, codex_row
    claude_row = rows[str(claude.slot)]
    assert re.match(rf"{claude.slot}\s+account {claude.slot}\s+same@example\.com", claude_row)
    assert "·" not in claude_row.split("same@example.com")[0], claude_row
    plain = rows["1"]
    assert plain.split()[1:3] == ["plain", "claude"] and "codex" not in plain, plain
    # The plain codex home with no login: its own row, never a copy of account 3's.
    [plain_codex] = [line for ref, line in rows.items() if "plain codex" in line]
    assert re.match(r"\S+\s+plain codex · codex\s+not signed in", plain_codex), plain_codex
    assert "same@example.com" not in plain_codex


def test_a_codex_row_with_a_reading_reads_as_it_did_and_says_its_agent_once(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the wall clock, as Codex's offline reading is aged (no ``fake_home`` clock)."""
    from datetime import UTC, datetime, timedelta

    from tests.test_codex_accounts import _rollout, _token_count, _window

    _installed(monkeypatch, claude="/opt/bin/claude", codex="/opt/bin/codex")
    codex = _codex_slot("me@example.com")
    now = datetime.now(tz=UTC)
    _rollout(
        codex.config_dir,
        [
            _token_count(
                now - timedelta(hours=3),
                _window(12, 300, now + timedelta(hours=2)),
                _window(48, 10080, now + timedelta(days=2)),
            )
        ],
    )
    monkeypatch.setattr(service, "_http_get", _Usage({}))

    row = _usage_rows(runner, monkeypatch)[str(codex.slot)]

    assert re.match(rf"{codex.slot}\s+account {codex.slot} · codex\s+me@example\.com", row), row
    assert row.count("· codex") == 1, row
    assert "12%" in row and "48%" in row and "as of 3h ago" in row, row


def test_a_claude_only_machine_prints_no_agent_words_and_json_gains_no_key(
    fake_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Claude-only machine reads byte for byte as before: no row names an agent. The JSON
    of a Codex row has the keys a Claude row has, and no key was added for F2."""
    _installed(monkeypatch, claude="/opt/bin/claude", codex=None)
    two = _slot("two@example.com", "tok-two")
    three = _slot("three@example.com", "tok-three")
    service.set_alias(str(three.slot), "work")
    monkeypatch.setattr(
        service, "_http_get", _Usage({"tok-two": _payload(3), "tok-three": _payload(40)})
    )

    monkeypatch.setenv("COLUMNS", "200")
    result = runner.invoke(app, ["accounts", "usage"])

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].startswith("slot  label         signed in as"), lines[0]
    assert re.match(r"1\s+plain claude\s+not signed in", lines[1]), lines[1]
    assert re.match(rf"{two.slot}\s+account {two.slot}\s+two@example\.com\s+3%", lines[2])
    assert re.match(rf"{three.slot}\s+work\s+three@example\.com\s+40%", lines[3]), lines[3]
    assert "· claude" not in result.stdout and "· codex" not in result.stdout

    codex = _codex_slot("me@example.com")
    raw = runner.invoke(app, ["--json", "accounts", "usage"])
    listed = {row["account"]["slot"]: row for row in json.loads(raw.stdout)}
    assert sorted(listed[codex.slot]) == sorted(listed[two.slot])


# --------------------------------------------------------------------------- F1


def test_the_manager_hint_names_the_managers_own_agent() -> None:
    assert manager_hint("claude-code") == manager_hint(None) == CLAUDE_HINT
    assert manager_hint("codex") == CODEX_HINT and "Claude" not in manager_hint("codex")
    assert "any gemini-x session" in manager_hint("gemini-x")  # unknown here: by its id
    assert "{agent}" in MANAGER_HINT and "Claude" not in MANAGER_HINT  # no agent literal


@pytest.mark.parametrize("width", [38, 45, 60, 90, 200])
def test_the_manager_tab_says_each_managers_agent_at_every_width(
    tmp_path: Path, script: Script, width: int
) -> None:
    """Mounted, as the live run read it: a Claude Code manager's hint is its 0.9.2 line at every
    width the owner uses (shown whole, wrapped where it must be), a Codex manager's names
    Codex, and switching the tab's manager from one to the other repaints it."""
    seed(tmp_path, ("prj_a", "alpha", "amber-otter"))
    claude = on_slot(ui_suite.status("prj_a", "manager", "manager", "waiting"), 2)
    codex = claude.model_copy(
        update={"agent": claude.agent.model_copy(update={"agent": "codex", "binary": "codex"})}
    )

    def rows(widget: Widget) -> str:
        w, h = widget.outer_size
        return " ".join(s.text.strip() for s in widget.render_lines(Region(0, 0, w, h)))

    async def go(pilot: Pilot[None]) -> tuple[str, str, str]:
        app = fleet_app(pilot)
        await settle(app)
        await pilot.click(card_for(app, "prj_a").query_one(ProjectTitle))
        await settle(app)
        hint = app.query_one("#project-prj_a #manager-hint", Static)
        seen = [rows(hint)]
        for row in (codex, claude):
            script["prj_a"] = [row]
            app.refresh_data()
            await settle(app)
            seen.append(rows(hint))
        return seen[0], seen[1], seen[2]

    script["prj_a"] = [claude]
    first, as_codex, back = drive(go, size=(width, 40))

    squeeze = re.compile(r"\s+")
    assert squeeze.sub(" ", first).strip() == CLAUDE_HINT, first
    assert squeeze.sub(" ", as_codex).strip() == CODEX_HINT, as_codex
    assert back == first, "the Claude manager's hint, as it was"


def test_a_resumed_agent_is_told_its_own_agents_account() -> None:
    """The line typed into a resumed agent after a switch names the account as its agent's:
    a Codex agent moved between Codex accounts read "another Claude account"."""
    from datetime import UTC, datetime

    def row(agent: str | None) -> FleetAgent:
        return FleetAgent(
            id="agt_1",
            project_id="prj_1",
            label="coder-1",
            role="coder",
            agent=agent,
            pane_id="%1",
            cwd=Path("/w"),
            created_at=datetime(2026, 11, 13, tzinfo=UTC),
        )

    claude = fleet_service._resume_prompt(row("claude-code"), "session limit")
    assert claude.startswith("You are coder-1, moved to another Claude account after session")
    assert fleet_service._resume_prompt(row(None), "session limit") == claude
    codex = fleet_service._resume_prompt(row("codex"), "session limit")
    assert codex == claude.replace("another Claude account", "another Codex account")
    assert core.agent_noun("codex") == "Codex" and core.agent_noun(None) == "Claude"
