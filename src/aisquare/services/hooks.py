"""Runtime handlers for the Claude Code hooks installed by ``agents connect``.

- ``session_start_context`` builds what aisquare injects when a session starts:
  the curated context block, a directive pointing Claude at the codebase
  snapshot and prompt history (route, don't dump) — and, when the orchestrator is
  active for the project, the team board plus protocol.
- ``prompt_submitted`` records how the user prompts (``aisquare log``),
  heartbeats the session on the orchestrator, and returns the teammate delta to
  inject (empty when the team has been quiet).
- ``session_ended`` retires the session from the orchestrator.

``session_start`` is also where the explainability join is closed. It is the
one place that holds BOTH halves of the correlation spine — Claude Code hands
it the session id the board row uses, and a traced launcher left the pipeline
id in this process's environment — and it needs nothing from the binary that
was launched, which is why a role bound to a wrapper joins exactly like the
default agent does.
"""

from __future__ import annotations

import os
from pathlib import Path

from aisquare.core import insights
from aisquare.core import snapshot as snapshot_core
from aisquare.core.injection import build_block
from aisquare.core.store import store_session
from aisquare.core.workspace import active_project
from aisquare.services import explainability as explainability_service
from aisquare.services import team as team_service


def record_trace_join(session_id: str | None) -> str | None:
    """Pair this session's board id with the Run its launcher opened for it.

    Returns the reason it could not be written, or ``None`` — including when
    there was nothing to write, which is the ordinary case: an untraced
    session carries no marker and leaves after one lookup.

    Deliberately silent about failures rather than loud. Every other fail-open
    in the tracing path prints to stderr because a human is watching a launch;
    this one runs inside the agent, where stderr is the hook's own channel and
    noise there is paid on every single session start. An unwritten join is
    recoverable — the Run still carries the agent name — so it is not worth
    spending that.
    """
    if not session_id:
        return None
    try:
        marker = explainability_service.traced_by()
        if marker is None:
            return None
        pipeline_id, agent_name = marker
        return explainability_service.record_join(
            session_id=session_id,
            pipeline_id=pipeline_id,
            agent_name=agent_name,
            role=os.environ.get("AISQUARE_ROLE") or None,
        )
    except Exception as exc:  # an observer may never disrupt a session start
        return f"join record not written ({exc})"


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
    record_trace_join(session_id)
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
    return "\n\n".join(part for part in (directive, block, team_block) if part)


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
    if prompt is not None and prompt.strip():
        capture_prompt(prompt, cwd, session_id=session_id)
    if session_id is None:
        return ""
    return team_service.hook_prompt_heartbeat(
        session_id, cwd, transcript_path=transcript_path, model=model, effort=effort
    )


def session_ended(cwd: Path | None, *, session_id: str | None = None) -> None:
    """Retire the session from the orchestrator and release its claims."""
    if session_id is not None:
        team_service.hook_session_end(session_id, cwd)


def turn_stopped(
    cwd: Path | None, *, session_id: str | None = None, stop_hook_active: bool = False
) -> team_service.StopDecision | None:
    """Mark the session as waiting for input (its turn just ended).

    A manager with fresh board decisions gets a :class:`~aisquare.services.team.StopDecision`
    back instead, which the hook prints so Claude Code keeps its turn going
    (docs/plans/fleet-tui.md §7.3). Everyone else: ``None``, as before.
    """
    if session_id is None:
        return None
    return team_service.hook_stop(session_id, cwd, stop_hook_active=stop_hook_active)


def needs_attention(
    cwd: Path | None, *, session_id: str | None = None, message: str | None = None
) -> None:
    """Mark the session as needing the user, and put it on the feed."""
    if session_id is not None:
        team_service.hook_notification(session_id, cwd, message)


def capture_prompt(prompt: str, cwd: Path | None, *, session_id: str | None = None) -> None:
    """Record a submitted user prompt against the active project.

    Spooling for the gateway comes last and cannot raise: recording the prompt
    locally is the job, shipping it is an observer of the job.
    """
    if not prompt.strip():
        return
    with store_session() as store:
        project = active_project(store, cwd)
        store.ensure_project(project)
        store.add_prompt(prompt, project.id, source="claude-code")
    insights.record_prompt(prompt, session_id=session_id, project_id=project.id)


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
    elif snap is not None and snap.status == "skeleton_only" and snap.skeleton_path.exists():
        # Over budget even compressed: the skeleton and its index are still the
        # cheapest orientation there is. No full pack to offer, so none is named.
        lines += [
            "aisquare has a packed skeleton of this codebase (structure + signatures; the",
            "full pack was skipped as over budget) — use it to understand the project fast",
            "and cheaply instead of grepping or listing files:",
            f"- Skeleton (read this FIRST): {snap.skeleton_path}",
            f"- Per-file index into the skeleton (char offsets + token counts): {snap.index_path}",
            "Orient from the skeleton; open source files directly for implementation detail.",
        ]
    if has_prompts:
        lines.append(
            "Past user prompts here are captured — run `aisquare log` to see how the user "
            "tends to ask, and honour that intent."
        )
    if not lines:
        return ""
    return "<aisquare-context>\n" + "\n".join(lines) + "\n</aisquare-context>"
