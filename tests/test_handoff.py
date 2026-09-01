"""``handoff``: specific past sessions become briefs another agent starts from."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths
from aisquare.services import handoff as handoff_service


def _event(kind: str, content: Any) -> str:
    return json.dumps({"type": kind, "message": {"role": kind, "content": content}})


def _transcript_lines() -> list[str]:
    return [
        _event("user", "fix the credit metering rounding bug"),
        _event(
            "assistant",
            [
                {"type": "text", "text": "Looking at billing/ledger.py first."},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "billing/ledger.py"}},
            ],
        ),
        _event(
            "user",
            [{"type": "tool_result", "content": [{"type": "text", "text": "def accrue(): ..."}]}],
        ),
        _event(
            "assistant",
            [{"type": "text", "text": "Decision: round half-even, per the MetricStream contract."}],
        ),
        "{not json — a torn line mid-append",
        _event("user", "ship it and note the API key sk-ant-api03-" + "a" * 93),
    ]


@pytest.fixture()
def claude_dir(tmp_path: Path) -> Path:
    projects = tmp_path / "claude-projects"
    slug = projects / "-home-work-repo"
    slug.mkdir(parents=True)
    (slug / "aaaa1111-2222-3333-4444-555566667777.jsonl").write_text(
        "\n".join(_transcript_lines()) + "\n", encoding="utf-8"
    )
    (slug / "bbbb1111-2222-3333-4444-555566667777.jsonl").write_text(
        _event("user", "second session, unrelated work") + "\n", encoding="utf-8"
    )
    return projects


def test_handoff_writes_a_brief_per_session_and_a_bundle(
    runner: CliRunner, claude_dir: Path, tmp_path: Path
) -> None:
    out = tmp_path / "bundle.md"
    result = runner.invoke(
        app,
        ["handoff", "aaaa", "bbbb", "--no-llm", "--out", str(out), "--claude-dir", str(claude_dir)],
    )
    assert result.exit_code == 0, result.output
    bundle = out.read_text(encoding="utf-8")
    assert "2 session(s) distilled" in bundle
    assert "credit metering rounding bug" in bundle
    assert "second session, unrelated work" in bundle
    assert "Decision: round half-even" in bundle, "assistant decisions are the payload"


def test_handoff_redacts_credentials_from_the_digest(
    runner: CliRunner, claude_dir: Path, tmp_path: Path
) -> None:
    out = tmp_path / "bundle.md"
    result = runner.invoke(
        app,
        [
            "handoff",
            "aaaa",
            "--no-llm",
            "--raw",
            "--out",
            str(out),
            "--claude-dir",
            str(claude_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    everything = out.read_text(encoding="utf-8")
    raw_files = list((paths.aisquare_home() / "handoffs").glob("*/transcript-*.md"))
    assert raw_files, "--raw promised the redacted digest files"
    for text in [everything, *(p.read_text(encoding="utf-8") for p in raw_files)]:
        assert "sk-ant-api03-" not in text, "a pasted key must never reach a handoff artifact"


def test_ambiguous_and_missing_session_ids_are_clean_errors(
    runner: CliRunner, claude_dir: Path
) -> None:
    result = runner.invoke(app, ["handoff", "cccc", "--no-llm", "--claude-dir", str(claude_dir)])
    assert result.exit_code == 1
    assert "no session transcript matches" in result.output

    # Make the shared prefix ambiguous: both fixtures end with the same tail.
    result = runner.invoke(app, ["handoff", "", "--no-llm", "--claude-dir", str(claude_dir)])
    assert result.exit_code == 1
    assert "ambiguous" in result.output


def test_llm_failure_degrades_to_a_structural_brief(
    runner: CliRunner, claude_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No claude binary (or a broken one) must still ship a usable brief."""
    monkeypatch.setattr(handoff_service, "_distill_llm", lambda digest: None)
    out = tmp_path / "bundle.md"
    result = runner.invoke(
        app, ["handoff", "aaaa", "--out", str(out), "--claude-dir", str(claude_dir)]
    )
    assert result.exit_code == 0, result.output
    assert "structural" in result.output
    assert "credit metering rounding bug" in out.read_text(encoding="utf-8")


def test_the_llm_seam_receives_the_digest_and_its_answer_becomes_the_brief(
    runner: CliRunner, claude_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def fake_distill(digest: str) -> str:
        seen.append(digest)
        return "## Goal\nRound half-even.\n"

    monkeypatch.setattr(handoff_service, "_distill_llm", fake_distill)
    out = tmp_path / "bundle.md"
    result = runner.invoke(
        app, ["handoff", "aaaa", "--out", str(out), "--claude-dir", str(claude_dir)]
    )
    assert result.exit_code == 0, result.output
    assert seen and "credit metering rounding bug" in seen[0]
    assert "Round half-even." in out.read_text(encoding="utf-8")


def test_a_note_request_without_team_mode_reports_not_posted(
    runner: CliRunner, claude_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pipe must never cost the briefs: they are written either way.

    The orchestrator is on unless ``AISQUARE_TEAM=0``, so the disabled pipe is
    forced explicitly — the point is the degradation, not the default.
    """
    monkeypatch.setenv("AISQUARE_TEAM", "0")
    out = tmp_path / "bundle.md"
    result = runner.invoke(
        app,
        [
            "handoff",
            "aaaa",
            "--no-llm",
            "--to",
            "manager",
            "--out",
            str(out),
            "--claude-dir",
            str(claude_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "team mode is off" in result.output
    assert out.exists(), "the note failing must not cost the bundle"
