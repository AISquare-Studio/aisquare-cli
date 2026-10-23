"""#292 — which instructions a fleet agent follows, written down where people look.

A user's own CLAUDE.md can contradict a role's cycle ("use subagents for parallel work"
against the manager's "spawn coder agents"), and nothing said which one an agent follows
or how to settle it. ``docs/instructions.md`` says so; this pins that it exists, is
reachable from the three places a reader starts, and that its commands are guarded like
every other page's.
"""

from __future__ import annotations

from pathlib import Path

from tests import test_documented_commands as documented

REPO = Path(__file__).resolve().parents[1]
PAGE = "docs/instructions.md"


def test_the_instruction_precedence_page_exists_and_is_linked() -> None:
    page = REPO / PAGE
    assert page.is_file(), PAGE
    text = page.read_text(encoding="utf-8")
    for must in ("CLAUDE.md", "<aisquare-team>", "10,000", "prompt-audit", "subagent"):
        assert must in text, must
    for source, link in (
        ("README.md", "docs/instructions.md"),
        ("docs/orchestration.md", "instructions.md"),
        ("docs/personas.md", "instructions.md"),
    ):
        assert link in (REPO / source).read_text(encoding="utf-8"), source


def test_its_commands_are_checked_against_the_cli() -> None:
    assert PAGE in documented.DOCUMENTED
