"""Codex parity for main's #417 evidence gate (SCOPE #113 090): the requirements linked to a
task reach a Codex session at its start, through the one session-start path every agent takes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aisquare.services import team
from tests.test_evidence_gate import (  # noqa: F401 - autouse fixtures, by name
    CODER,
    _linked,
    no_nudge,
    repo,
)

CODEX = "codex"

# --- #417: the evidence gate --------------------------------------------------------------


def test_a_codex_session_start_shows_its_tasks_linked_requirements(
    repo: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, task = _linked("R one", "R two")
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    team.hook_session_start(CODER, repo, "startup", agent=CODEX)
    team.claim_task(task.id, session_ref=CODER)

    started = team.hook_session_start(CODER, repo, "startup", agent=CODEX)

    block = started.split("<aisquare-requirements>")[1].split("</aisquare-requirements>")[0]
    assert "R1 [missing] R one" in block and "R2 [missing] R two" in block
    assert "<aisquare-team>" in started, "the board is still there"
