"""Runtime handlers for the Claude Code hooks installed by ``agents connect``.

- ``session_start_context`` builds what aisquare injects when a session starts:
  the curated context block, a directive pointing Claude at the codebase
  snapshot and prompt history (route, don't dump) — and, when the team bus is
  active for the project, the team board plus protocol.
- ``prompt_submitted`` records how the user prompts (``aisquare log``),
  heartbeats the session on the team bus, and returns the teammate delta to
  inject (empty when the team has been quiet).
- ``session_ended`` retires the session from the team bus.
"""

from __future__ import annotations

from pathlib import Path

from aisquare.core import snapshot as snapshot_core
from aisquare.core.injection import build_block
from aisquare.core.store import store_session
from aisquare.core.workspace import active_project
from aisquare.services import team as team_service


def session_start_context(
    cwd: Path | None, *, session_id: str | None = None, source: str | None = None
) -> str:
    """Context to inject at Claude Code ``SessionStart`` (empty if nothing useful)."""
    with store_session() as store:
        project = active_project(store, cwd)
        entries = store.entries(project_id=project.id)
        has_prompts = bool(store.recent_prompts(project.id, limit=1))
    directive = _directive(project.id, has_prompts=has_prompts)
    block = build_block(entries, project) if entries else ""
    team_block = team_service.hook_session_start(session_id, cwd, source) if session_id else ""
    return "\n\n".join(part for part in (directive, block, team_block) if part)


def prompt_submitted(prompt: str | None, cwd: Path | None, *, session_id: str | None = None) -> str:
    """Record a submitted prompt; return the team delta to add to context."""
    if prompt is not None and prompt.strip():
        capture_prompt(prompt, cwd)
    if session_id is None:
        return ""
    return team_service.hook_prompt_heartbeat(session_id, cwd)


def session_ended(cwd: Path | None, *, session_id: str | None = None) -> None:
    """Retire the session from the team bus and release its claims."""
    if session_id is not None:
        team_service.hook_session_end(session_id, cwd)


def turn_stopped(cwd: Path | None, *, session_id: str | None = None) -> None:
    """Mark the session as waiting for input (its turn just ended)."""
    if session_id is not None:
        team_service.hook_stop(session_id, cwd)


def needs_attention(
    cwd: Path | None, *, session_id: str | None = None, message: str | None = None
) -> None:
    """Mark the session as needing the user, and put it on the feed."""
    if session_id is not None:
        team_service.hook_notification(session_id, cwd, message)


def capture_prompt(prompt: str, cwd: Path | None) -> None:
    """Record a submitted user prompt against the active project."""
    if not prompt.strip():
        return
    with store_session() as store:
        project = active_project(store, cwd)
        store.ensure_project(project)
        store.add_prompt(prompt, project.id, source="claude-code")


def _directive(project_id: str, *, has_prompts: bool) -> str:
    lines: list[str] = []
    snap = snapshot_core.load(project_id)
    if snap is not None and snap.status == "ready" and snap.pack_path.exists():
        skeleton = snap.skeleton_path if snap.skeleton_path.exists() else snap.pack_path
        lines += [
            "aisquare has a packed snapshot of this codebase — use it to understand the",
            "project fast and cheaply instead of grepping or listing files:",
            f"- Skeleton (structure + signatures, read this FIRST): {skeleton}",
            f"- Full pack (every file's contents, open on demand): {snap.pack_path}",
            f"- Per-file index (char offsets + token counts): {snap.index_path}",
            "Orient from the skeleton; open the full pack only for implementation detail.",
        ]
    if has_prompts:
        lines.append(
            "Past user prompts here are captured — run `aisquare log` to see how the user "
            "tends to ask, and honour that intent."
        )
    if not lines:
        return ""
    return "<aisquare-context>\n" + "\n".join(lines) + "\n</aisquare-context>"
