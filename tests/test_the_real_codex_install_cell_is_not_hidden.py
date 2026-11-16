"""The real-Codex install cell fails the run, except for Codex's own installer failing.

It carried ``continue-on-error: true`` and was red on every run from the v0.9.0 merge until
r23 (review 5659751791 of #113, M1) without anyone seeing: the job read "success" while the
cell's Chrome DevTools MCP assertion failed. It is not allowed to be flaky any more; what a
third party's CDN does is the one thing it excuses, as a warning the run shows.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CELL = (ROOT / "tests" / "install" / "cell.sh").read_text(encoding="utf-8")


def _job() -> dict[str, Any]:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "install.yml").read_text("utf-8"))
    job: dict[str, Any] = workflow["jobs"]["with-codex"]
    return job


def test_the_job_has_no_continue_on_error() -> None:
    assert "continue-on-error" not in _job()


def test_only_the_cells_exit_75_becomes_a_warning() -> None:
    script = "\n".join(step.get("run", "") for step in _job()["steps"])

    assert "|| status=$?" in script, "the status is read, not swallowed"
    assert 'if [ "$status" = 75 ]; then' in script and "::warning" in script
    assert script.rstrip().endswith('exit "$status"'), "every other status is the job's"


def test_the_cell_exits_75_only_for_codexs_own_installer_failing() -> None:
    assert CELL.count("exit 75") == 1
    before = CELL.split("exit 75")[0].rsplit("if ", 1)[1]
    assert "the Codex installer failed" in before and '"$AGENT" = codex' in before
    # The text it reads is install.sh's own warning for that failure.
    assert "the Codex installer failed" in (ROOT / "install.sh").read_text(encoding="utf-8")


def test_the_cell_asserts_the_mcp_in_codexs_config_and_through_codex_itself() -> None:
    assert "[mcp_servers\\.chrome-devtools\\]" in CELL
    assert "codex mcp get chrome-devtools" in CELL
