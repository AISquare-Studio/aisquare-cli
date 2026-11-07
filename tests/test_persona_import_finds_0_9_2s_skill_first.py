"""``persona import <name>`` finds 0.9.2's match first (review r40 of #113, finding 5, as the
owner ruled).

0.9.2 searched Claude Code's ``~/.claude/skills`` (this environment's config dir), then
``<repo>/.claude/skills``, and imported the first skill of that name. The head put every
agent's user folders (``~/.agents/skills``, ``$CODEX_HOME/skills``, each slot's) before any
project folder, so with ``~/.agents/skills/review`` and ``<repo>/.claude/skills/review``,
``persona import review`` took the ``~/.agents`` one where 0.9.2 took the repo's. Now 0.9.2's
two places come first, in its order, and the other agents' folders after.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import agents as agents_core
from tests.test_persona_cli import _skill, _tree, _user_layer, claude_dir, repo  # noqa: F401

REVIEW = "review"


def _imported(runner: CliRunner) -> dict[str, bytes]:
    result = runner.invoke(app, ["persona", "import", REVIEW])
    assert result.exit_code == 0, result.output
    return _tree(_user_layer() / REVIEW)


def test_the_repos_claude_skill_comes_before_another_agents_user_skill(
    runner: CliRunner,
    repo: Path,  # noqa: F811
    claude_dir: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r40's repro: 0.9.2 took the repo's; the head took ``~/.agents``'."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    _skill(agents_core.home() / ".agents" / "skills", REVIEW, description="the agents' one")
    _skill(tmp_path / "codex-home" / "skills", REVIEW, description="Codex's own")
    repos = _skill(repo / ".claude" / "skills", REVIEW, description="the repo's one")

    assert _imported(runner) == _tree(repos)


def test_claude_codes_user_skill_comes_before_the_repos_as_on_0_9_2(
    runner: CliRunner,
    repo: Path,  # noqa: F811
    claude_dir: Path,  # noqa: F811
) -> None:
    users = _skill(claude_dir / "skills", REVIEW, description="the user's one")
    _skill(repo / ".claude" / "skills", REVIEW, description="the repo's one")
    _skill(agents_core.home() / ".agents" / "skills", REVIEW, description="the agents' one")

    assert _imported(runner) == _tree(users)


def test_another_agents_skill_is_still_found_after_them(
    runner: CliRunner,
    repo: Path,  # noqa: F811
    claude_dir: Path,  # noqa: F811
) -> None:
    agents = _skill(agents_core.home() / ".agents" / "skills", REVIEW, description="only here")

    assert _imported(runner) == _tree(agents)
