"""Shared plumbing for the CI test bed's tests: wiring a stub, building requests.

Not a test module. Fixtures stay local to each test file (pytest wants them
there or in conftest); this holds the pieces those fixtures share so the wiring
— four environment variables, in the order the client reads them — is written
once.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from aisquare.core.ids import new_trace_id
from aisquare.services import ci_client
from aisquare.services.ci_contract import HookRequest
from tests.stub_ci_server import StubCI

RUN = "run_kernel0001"
SESSION = "3f2b6c2e-6d1a-4c5e-9c4b-1a2b3c4d5e6f"
"""A Claude Code session id: a UUID, as the hooks receive it."""


def wire(monkeypatch: pytest.MonkeyPatch, stub: StubCI, *, run: str = RUN, key: str = "k") -> None:
    """Switch the experiment on and point it at ``stub``."""
    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "1")
    monkeypatch.setenv(ci_client.URL_ENV_VAR, stub.url)
    monkeypatch.setenv(ci_client.KEY_ENV_VAR, key)
    monkeypatch.setenv(ci_client.RUN_ENV_VAR, run)


def request(**overrides: object) -> HookRequest:
    """A schema-valid ``prompt_submit`` request, with ``overrides`` applied."""
    fields: dict[str, object] = {
        "trigger": "prompt_submit",
        "run_id": RUN,
        "session_id": "ses_test",
        "trace_id": new_trace_id(),
        "project_ref": "AISquare-Studio/aisquare-cli@main",
        "snapshot_ref": None,
        "prompt": "why does the brain lock use msvcrt",
        "client_safety_ms": 5_000,
        "client_observed_at": "2026-08-28T10:00:00Z",
    }
    fields.update(overrides)
    return HookRequest.model_validate(fields)


def git(root: Path, *args: str) -> str:
    """Run git in ``root`` with a fixed identity and no global config in the way."""
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    result = subprocess.run(
        ["git", "-C", str(root), "-c", "commit.gpgsign=false", *args],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return result.stdout.strip()


def repo(root: Path) -> Path:
    """A fresh repository with one commit, at ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    (root / "tracked.txt").write_text("one\n", encoding="utf-8")
    git(root, "add", "tracked.txt")
    git(root, "commit", "-q", "-m", "first")
    return root
