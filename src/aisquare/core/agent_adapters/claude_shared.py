"""What Claude Code's accounts share (P2-W7, #198), as its ``AgentAccounts.share_parts`` and
``memory`` say it: the parts, and how its auto-memory is pointed at one directory.

Linking the parts is every agent's (``core.account_sharing``); these are Claude Code's names,
its entries in a ``CLAUDE_CONFIG_DIR`` and the setting its memory is read from. ``plugins``
is experimental: MCP auth and the plugin store are per directory, so a linked ``plugins/``
can carry a login one slot made.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from aisquare.core import claude_sharing
from aisquare.core.agent_adapters.types import SharePart
from aisquare.models import ClaudeAccount

SHARE_PARTS: tuple[SharePart, ...] = (
    SharePart("memory", how="merge"),
    SharePart("skills", "skills"),
    SharePart("agents", "agents"),
    SharePart("commands", "commands"),
    SharePart("claude_md", "CLAUDE.md", is_dir=False),
    SharePart("plugins", "plugins", experimental=True),
)


class ClaudeSharedMemory:
    """Claude Code's auto-memory: ``projects/<key>/memory`` in each account, and
    ``autoMemoryDirectory`` in any settings scope, ``--settings`` included (Claude Code,
    Memory). Every launch gets it, the plain claude's too, so all of them read and write one
    directory per project."""

    root_name = "claude-shared"
    override = "its own --settings"

    def key(self, cwd: Path) -> str:
        return claude_sharing.memory_key(cwd)

    def projects(self, account: ClaudeAccount) -> Mapping[str, Path]:
        return {
            memory.parent.name: memory
            for memory in sorted((account.config_dir / "projects").glob("*/memory"))
            if memory.is_dir()
        }

    def apply(
        self, args: Sequence[str], operator_args: Sequence[str], directory: Path
    ) -> list[str] | None:
        """ONE ``--settings`` for every key aisquare sets: the role's skill listing (P1 WS-1,
        ``harness.tool_policy``) and the memory directory merge into one JSON. Two flags are
        not two settings: Claude Code reads one, and the memory stood down on the role's own
        flag, so a coder or a tester shared no memory at all (sync #367). One the operator's
        line carries still wins."""
        if any(arg == "--settings" or arg.startswith("--settings=") for arg in operator_args):
            return None
        memory = {"autoMemoryDirectory": str(directory)}
        out = list(args)
        for index, arg in enumerate(out[:-1]):
            if arg == "--settings":
                out[index + 1] = json.dumps({**json.loads(out[index + 1]), **memory})
                return out
        return [*out, "--settings", json.dumps(memory)]

    def warnings(self, account: ClaudeAccount) -> list[str]:
        """Since Claude Code 2.1.280 every auto-memory write under a symlinked ``projects/``
        asks for approval that no allow rule, hook or auto mode can give
        (anthropics/claude-code#98044), and a shared ``projects/`` is swept by the shortest
        ``cleanupPeriodDays``."""
        if os.path.islink(account.config_dir / "projects"):
            return [
                f"slot {account.slot}'s projects/ is a symlink: every auto-memory write asks "
                "for approval (Claude Code 2.1.280+)"
            ]
        return []
