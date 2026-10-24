"""Cross-account resume and shared memory against a real Claude Code (P2-W7, #198).

Skipped unless two signed-in Claude config directories are named:

    AISQUARE_E2E_CLAUDE_A=~/.aisquare/claude-accounts/2 \\
    AISQUARE_E2E_CLAUDE_B=~/.aisquare/claude-accounts/4 \\
    pytest -m e2e tests/test_cross_account_e2e.py

Each test spends a few short ``claude -p`` turns (about $0.05 each, measured 2026-10-23 on
Claude Code 2.1.309) in a scratch repository, and removes the project folders it made in
the two directories afterwards. Read before the suite clears ambient variables, which is
why the names are taken at import.

What they pin (docs/runbooks/cross-account-resume.md):

- **resume**: a session started on A and resumed on B by its absolute transcript path
  (what ``fleet switch`` does) keeps its session id, recalls what was said on A, and its
  new turns are appended to A's transcript; B gets no transcript of its own for it;
- **memory**: with ``autoMemoryDirectory`` given as ``--settings`` (``[accounts] share =
  ["memory"]``), a memory saved on A is read on B, and B without it does not see it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aisquare.core.claude_sharing import memory_key

A = os.environ.get("AISQUARE_E2E_CLAUDE_A", "")
B = os.environ.get("AISQUARE_E2E_CLAUDE_B", "")
#: The real binary, by absolute path: the suite's ``no_real_fleet`` puts a stand-in that
#: refuses every launch first on PATH for each test, which is right for every other test.
CLAUDE = shutil.which("claude") if A and B else None

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not (A and B and CLAUDE),
        reason="needs AISQUARE_E2E_CLAUDE_A/_B (two signed-in config dirs) and claude on PATH",
    ),
]


def _claude(config_dir: str, repo: Path, *args: str) -> dict[str, Any]:
    env = {k: os.environ[k] for k in ("HOME", "USER", "LANG", "PATH") if k in os.environ}
    env["CLAUDE_CONFIG_DIR"] = str(Path(config_dir).expanduser())
    done = subprocess.run(
        [str(CLAUDE), "-p", *args, "--output-format", "json", "--setting-sources", "project",
         "--strict-mcp-config"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=300, check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    answer: dict[str, Any] = json.loads(done.stdout)
    return answer


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "e2e-repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    yield root
    for config in (A, B):
        shutil.rmtree(Path(config).expanduser() / "projects" / memory_key(root), True)


def test_a_session_moved_to_another_account_resumes_by_its_path(repo: Path) -> None:
    session = str(uuid.uuid4())
    first = _claude(A, repo, "Remember the marker ZEBRA-7 for later. Reply OK.",
                    "--session-id", session, "--max-turns", "1")  # fmt: skip
    assert first["session_id"] == session
    transcript = Path(A).expanduser() / "projects" / memory_key(repo) / f"{session}.jsonl"
    before = len(transcript.read_text(encoding="utf-8").splitlines())

    second = _claude(B, repo, "What marker did I ask you to remember? Reply with it only.",
                     "--resume", str(transcript), "--max-turns", "1")  # fmt: skip

    assert second["session_id"] == session, "the session keeps its id"
    assert "ZEBRA-7" in second["result"]
    assert len(transcript.read_text(encoding="utf-8").splitlines()) > before, "appended on A"
    assert not (Path(B).expanduser() / "projects" / memory_key(repo) / f"{session}.jsonl").exists()


def test_a_memory_saved_on_one_account_is_read_on_another(repo: Path, tmp_path: Path) -> None:
    shared = tmp_path / "memory" / memory_key(repo)
    settings = json.dumps({"autoMemoryDirectory": str(shared)})
    _claude(A, repo, "Save to your auto memory for this project: the deploy codeword is "
            "PELICAN-42. Reply SAVED.", "--settings", settings, "--max-turns", "6",
            "--allowedTools", "Read,Write,Edit")  # fmt: skip
    assert any("PELICAN-42" in f.read_text(encoding="utf-8") for f in shared.glob("*.md"))

    asked = "What is the deploy codeword for this repository? Use your memory; do not search."
    remembered = _claude(B, repo, asked, "--settings", settings, "--max-turns", "4",
                         "--allowedTools", "Read")  # fmt: skip
    assert "PELICAN-42" in remembered["result"]
    without = _claude(B, repo, asked + " If you do not know, reply UNKNOWN.", "--max-turns", "2")
    assert "PELICAN-42" not in without["result"]
