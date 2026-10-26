"""A machine's Claude Code history, built on disk for the usage-stats tests.

Not a test module. ``Machine`` is a home with Claude Code accounts (the plain ``claude``'s
``~/.claude`` and managed slots, each signed in on a subscription), git repositories to work
in, and transcripts written the way Claude Code writes them: one JSON object a line, an
``assistant`` entry per API request carrying ``message.usage``, a ``user`` entry per typed
prompt, a ``cwd`` on every entry. The tests then sweep or hook-record them and read the stats.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aisquare.core import claude_accounts as core

IST = timezone(timedelta(hours=5, minutes=30))
"""A zone whose local midnight is 18:30 UTC the day before: the cut the tests are about."""
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=IST)
"""The clock the stats tests read with: noon on Monday 26 Oct 2026, IST."""

MODEL = "claude-opus-5-5"


def stamp(moment: datetime) -> str:
    """``moment`` as Claude Code writes a timestamp: ISO-8601 UTC, milliseconds, ``Z``."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def request(
    moment: datetime,
    message_id: str,
    *,
    cwd: Path,
    model: str = MODEL,
    input: int = 10,
    cache_write: int = 100,
    cache_read: int = 1000,
    output: int = 20,
    tool: str | None = None,
) -> dict[str, Any]:
    """One API request: an ``assistant`` entry with its usage."""
    content: list[dict[str, Any]] = [{"type": "text", "text": "ok"}]
    if tool is not None:
        content = [{"type": "tool_use", "id": f"toolu_{message_id}", "name": tool, "input": {}}]
    return {
        "type": "assistant",
        "timestamp": stamp(moment),
        "cwd": str(cwd),
        "message": {
            "id": message_id,
            "model": model,
            "content": content,
            "usage": {
                "input_tokens": input,
                "cache_creation_input_tokens": cache_write,
                "cache_read_input_tokens": cache_read,
                "output_tokens": output,
            },
        },
    }


def prompt(moment: datetime, *, cwd: Path, text: str = "go on") -> dict[str, Any]:
    """A prompt the person typed: it opens a turn."""
    return {
        "type": "user",
        "timestamp": stamp(moment),
        "cwd": str(cwd),
        "message": {"role": "user", "content": text},
    }


def tool_result(moment: datetime, *, cwd: Path) -> dict[str, Any]:
    """A tool's answer: a ``user`` entry that is NOT a prompt."""
    return {
        "type": "user",
        "timestamp": stamp(moment),
        "cwd": str(cwd),
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_x", "content": "ok"}],
        },
    }


def write(path: Path, *entries: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")
    return path


def tokens(**kinds: int) -> int:
    """The total of one :func:`request` with ``kinds`` overriding its default usage."""
    base = {"input": 10, "cache_write": 100, "cache_read": 1000, "output": 20}
    return sum({**base, **kinds}.values())


@dataclass
class Machine:
    """A home with Claude Code accounts and repositories under ``root``."""

    root: Path
    home: Path

    def account(self, slot: int, subscription: str | None = None) -> Path:
        """The config directory of ``slot``, signed in on ``subscription`` (slot 1 is the plain
        ``claude``'s; the others are managed slots, made on the way)."""
        if slot == core.DEFAULT_SLOT:
            directory = core.default_config_dir()
        else:
            while core.find_account(slot) is None:
                core.create_account()
            directory = core.accounts_root() / str(slot)
        if subscription is not None:
            (directory / ".credentials.json").write_text(
                json.dumps(
                    {"claudeAiOauth": {"accessToken": "x", "subscriptionType": subscription}}
                ),
                encoding="utf-8",
            )
        return directory

    def repo(self, name: str, *, remote: str | None = None) -> Path:
        """A git repository called ``name``."""
        path = self.root / "repos" / name
        path.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        if remote is not None:
            subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote], check=True)
        return path

    def transcript(self, slot: int, repo: Path, session_id: str) -> Path:
        """Where Claude Code, run in ``repo`` under account ``slot``, writes ``session_id``."""
        directory = self.account(slot)
        folder = str(repo).replace("/", "-")
        return directory / "projects" / folder / f"{session_id}.jsonl"

    def session(self, slot: int, repo: Path, session_id: str, *entries: dict[str, Any]) -> Path:
        return write(self.transcript(slot, repo, session_id), *entries)
