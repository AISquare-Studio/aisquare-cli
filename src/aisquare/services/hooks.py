"""Runtime handlers for the Claude Code hooks installed by ``agents connect``.

- ``session_start_context`` builds what aisquare injects when a session starts:
  the curated context block, a directive pointing Claude at the codebase
  snapshot and prompt history (route, don't dump) — and, when the orchestrator is
  active for the project, the team board plus protocol.
- ``prompt_submitted`` records how the user prompts (``aisquare log``),
  heartbeats the session on the orchestrator, and returns the teammate delta to
  inject (empty when the team has been quiet).
- ``session_ended`` retires the session from the orchestrator.
"""

from __future__ import annotations

from pathlib import Path

from aisquare.core import snapshot as snapshot_core
from aisquare.core.injection import build_block
from aisquare.core.store import store_session
from aisquare.core.workspace import active_project
from aisquare.services import ci_augment
from aisquare.services import metrics as metrics_service
from aisquare.services import team as team_service


def session_start_context(
    cwd: Path | None,
    *,
    session_id: str | None = None,
    source: str | None = None,
    transcript_path: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> str:
    """Context to inject at Claude Code ``SessionStart`` (empty if nothing useful)."""
    with store_session() as store:
        project = active_project(store, cwd)
        entries = store.entries(project_id=project.id)
        has_prompts = bool(store.recent_prompts(project.id, limit=1))
    directive = _directive(project.id, has_prompts=has_prompts)
    block = build_block(entries, project) if entries else ""
    team_block = (
        team_service.hook_session_start(
            session_id, cwd, source, transcript_path=transcript_path, model=model, effort=effort
        )
        if session_id
        else ""
    )
    # Last, and only when the experiment is on: retrieved material sits closest
    # to what the agent is about to do, and is the part it should weigh least.
    retrieved = ci_augment.for_session_start(project_id=project.id, session_id=session_id).block
    return "\n\n".join(part for part in (directive, block, team_block, retrieved) if part)


def prompt_submitted(
    prompt: str | None,
    cwd: Path | None,
    *,
    session_id: str | None = None,
    transcript_path: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> str:
    """Record a submitted prompt; return the team delta to add to context."""
    retrieved = capture_prompt(prompt, cwd, session_id=session_id)
    if session_id is None:
        return retrieved
    delta = team_service.hook_prompt_heartbeat(
        session_id, cwd, transcript_path=transcript_path, model=model, effort=effort
    )
    return "\n\n".join(part for part in (delta, retrieved) if part)


def session_ended(cwd: Path | None, *, session_id: str | None = None) -> None:
    """Retire the session from the orchestrator and release its claims."""
    if session_id is not None:
        team_service.hook_session_end(session_id, cwd)


def turn_stopped(cwd: Path | None, *, session_id: str | None = None) -> None:
    """Mark the session as waiting for input, and close this turn's metrics row."""
    if session_id is not None:
        team_service.hook_stop(session_id, cwd)
        # After the team update, never before: a metrics failure must not cost
        # the board its state change, and close_turn swallows its own errors.
        metrics_service.close_turn(session_id)


def needs_attention(
    cwd: Path | None, *, session_id: str | None = None, message: str | None = None
) -> None:
    """Mark the session as needing the user, and put it on the feed."""
    if session_id is not None:
        team_service.hook_notification(session_id, cwd, message)


def capture_prompt(prompt: str | None, cwd: Path | None, *, session_id: str | None = None) -> str:
    """Record the prompt, consult CI, open this turn's row — in one store open.

    Both halves need the active project, and this runs synchronously in front
    of a developer who has just hit enter — resolving it twice would double the
    only unavoidable cost on the path.

    A turn is opened even when the prompt is empty and even when CI never ran:
    a row per turn from the day this ships is what turns the stretch before the
    endpoint goes live into a baseline rather than a gap.

    Failures are swallowed here rather than in the caller so that a store
    problem costs the record, not the teammate delta the hook still owes the
    session.

    Returns the retrieved block to inject, or ``""`` — which is what every turn
    returns while the experiment is off.
    """
    try:
        with store_session() as store:
            project = active_project(store, cwd)
            if prompt is not None and prompt.strip():
                store.ensure_project(project)
                store.add_prompt(prompt, project.id, source="claude-code")
            augmentation = ci_augment.for_prompt(
                prompt, project_id=project.id, session_id=session_id
            )
            metrics_service.open_turn(
                project.id,
                session_id=session_id,
                call=augmentation.call,
                injected_chars=len(augmentation.block) or None,
                store=store,
            )
    except Exception:  # never disrupt the session to record it
        return ""
    return augmentation.block


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
