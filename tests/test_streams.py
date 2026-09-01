"""Streams: grouping projects, requires-edges, and stream-scoped injection.

The scenario throughout is the one that motivated the feature (see
docs/streams-and-transcript-distill.md on its own branch): two clones of the
same product, one pinned to an enterprise deployment that *requires* the
platform stream, plus a compliance stream with no code at all.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app


@pytest.fixture()
def repos(tmp_path: Path) -> dict[str, Path]:
    """Three fake 'repositories' (marker dirs — no git needed for identity)."""
    layout: dict[str, Path] = {}
    for name in ("platform-be", "enterprise-be", "soc2"):
        root = tmp_path / name
        (root / ".aisquare").mkdir(parents=True)
        layout[name] = root
    return layout


def _invoke(runner: CliRunner, *argv: str) -> str:
    result = runner.invoke(app, list(argv))
    assert result.exit_code == 0, result.output
    return result.output


def _json(runner: CliRunner, *argv: str) -> Any:
    result = runner.invoke(app, ["--json", *argv])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_stream_new_add_list_show(runner: CliRunner, repos: dict[str, Path]) -> None:
    _invoke(runner, "stream", "new", "platform")
    _invoke(runner, "stream", "add", "platform", str(repos["platform-be"]))

    listed = _json(runner, "stream", "list")
    assert [stream["name"] for stream in listed] == ["platform"]
    assert len(listed[0]["members"]) == 1

    shown = _json(runner, "stream", "show", "platform")
    assert shown["member_roots"] == [str(repos["platform-be"])]


def test_duplicate_stream_name_is_refused(runner: CliRunner) -> None:
    _invoke(runner, "stream", "new", "platform")
    result = runner.invoke(app, ["stream", "new", "platform"])
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_remember_into_a_stream_and_inject_from_a_member(
    runner: CliRunner, repos: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _invoke(runner, "stream", "new", "platform")
    _invoke(runner, "stream", "add", "platform", str(repos["platform-be"]))
    _invoke(runner, "remember", "api on :8080, fe on :3100", "--stream", "platform")

    monkeypatch.chdir(repos["platform-be"])
    block = _invoke(runner, "context", "preview")
    assert "## Stream: platform" in block
    assert "api on :8080" in block

    # A directory OUTSIDE the stream sees none of it.
    monkeypatch.chdir(repos["soc2"])
    outside = _invoke(runner, "context", "preview")
    assert "api on :8080" not in outside


def test_requires_edge_pulls_the_required_streams_entries_in(
    runner: CliRunner, repos: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """metricstream requires platform: a MetricStream repo sees platform rules."""
    _invoke(runner, "stream", "new", "platform")
    _invoke(runner, "stream", "new", "metricstream", "--requires", "platform")
    _invoke(runner, "stream", "add", "metricstream", str(repos["enterprise-be"]))
    _invoke(runner, "remember", "features land in platform BE first", "--stream", "platform")
    _invoke(runner, "remember", "PRs target the metricstream branch", "--stream", "metricstream")

    monkeypatch.chdir(repos["enterprise-be"])
    block = _invoke(runner, "context", "preview")
    assert "PRs target the metricstream branch" in block
    assert "features land in platform BE first" in block, "the requires-edge was not followed"

    # `why` reports the streams that were in scope, after a real injection.
    _invoke(runner, "inject")
    record = _json(runner, "why")
    assert set(record["streams"]) == {"metricstream", "platform"}
    assert record["stream_count"] == 2


def test_a_requires_cycle_is_refused_and_names_the_path(runner: CliRunner) -> None:
    _invoke(runner, "stream", "new", "a")
    _invoke(runner, "stream", "new", "b", "--requires", "a")
    result = runner.invoke(app, ["stream", "requires", "a", "b"])
    assert result.exit_code == 1
    assert "cycle" in result.output
    result = runner.invoke(app, ["stream", "requires", "a", "a"])
    assert result.exit_code == 1, "a self-edge is the smallest cycle"


def test_unknown_stream_says_how_to_create_it(runner: CliRunner) -> None:
    result = runner.invoke(app, ["remember", "x", "--stream", "nope"])
    assert result.exit_code == 1
    assert "aisquare stream new nope" in result.output


def test_stream_flag_conflicts_with_pool_flags(runner: CliRunner) -> None:
    result = runner.invoke(app, ["remember", "x", "--stream", "s", "--user"])
    assert result.exit_code != 0
    assert "--stream cannot be combined" in result.output


def test_env_var_forces_a_stream_into_scope(
    runner: CliRunner, repos: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """SOC 2 work from inside a platform repo: AISQUARE_STREAM adds, never replaces."""
    _invoke(runner, "stream", "new", "platform")
    _invoke(runner, "stream", "new", "soc2")
    _invoke(runner, "stream", "add", "platform", str(repos["platform-be"]))
    _invoke(runner, "remember", "port map lives here", "--stream", "platform")
    _invoke(runner, "remember", "every action gets a dated CHANGELOG entry", "--stream", "soc2")

    monkeypatch.chdir(repos["platform-be"])
    monkeypatch.setenv("AISQUARE_STREAM", "soc2")
    block = _invoke(runner, "context", "preview")
    assert "dated CHANGELOG entry" in block, "the forced stream did not join the scope"
    assert "port map lives here" in block, "forcing a stream must never replace cwd scope"


def test_worktrees_of_a_member_repo_share_its_stream(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stream scope rides project identity, and identity resolves worktrees."""
    principal = tmp_path / "repo"
    principal.mkdir()

    def git(*argv: str) -> None:
        subprocess.run(["git", "-C", str(principal), *argv], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (principal / "f").write_text("x", encoding="utf-8")
    git("add", "f")
    git("commit", "-qm", "x")
    worktree = tmp_path / "repo-wt"
    git("worktree", "add", "-q", str(worktree))

    _invoke(runner, "stream", "new", "platform")
    _invoke(runner, "stream", "add", "platform", str(principal))
    _invoke(runner, "remember", "shared through the principal", "--stream", "platform")

    monkeypatch.chdir(worktree)
    assert "shared through the principal" in _invoke(runner, "context", "preview")
