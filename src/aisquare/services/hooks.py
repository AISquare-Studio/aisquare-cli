"""Runtime handlers for the Claude Code hooks installed by ``agents connect``.

- ``session_start_context`` builds what aisquare injects when a session starts:
  the curated context block plus a directive pointing Claude at the codebase
  snapshot and prompt history (route, don't dump).
- ``capture_prompt`` records how the user prompts, for replay via ``aisquare log``.
"""

from __future__ import annotations

from pathlib import Path

from aisquare.core import snapshot as snapshot_core
from aisquare.core.injection import build_block
from aisquare.core.store import store_session
from aisquare.core.workspace import active_project


def session_start_context(cwd: Path | None) -> str:
    """Context to inject at Claude Code ``SessionStart`` (empty if nothing useful)."""
    with store_session() as store:
        project = active_project(store, cwd)
        entries = store.entries(project_id=project.id)
        has_prompts = bool(store.recent_prompts(project.id, limit=1))
    directive = _directive(project.id, has_prompts=has_prompts)
    if not entries and not directive:
        return ""
    block = build_block(entries, project) if entries else ""
    return "\n\n".join(part for part in (directive, block) if part)


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
