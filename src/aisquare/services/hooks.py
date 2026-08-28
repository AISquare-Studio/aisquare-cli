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
from aisquare.models import ProjectInfo
from aisquare.services import ci_augment
from aisquare.services import explainability as explainability_service
from aisquare.services import metrics as metrics_service
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
    # Last, and only when the experiment is on: the standing instruction to
    # consult the recall tool, then any retrieved material — closest to what
    # the agent is about to do, and the part it should weigh least.
    instruction, retrieved = _session_start_ci(project, session_id, cwd)
    return "\n\n".join(
        part for part in (directive, block, team_block, instruction, retrieved) if part
    )


def _session_start_ci(
    project: ProjectInfo, session_id: str | None, cwd: Path | None
) -> tuple[str, str]:
    """Consult CI at session start and RECORD the outcome; never raises.

    The row is closed at creation — a session start is a call, not a turn.
    Nothing is written while the experiment is off or unconfigured: those
    machines record their baseline per prompt, and a row per session start
    would only say "off" again. A failure anywhere here costs the CI part of
    the context and nothing else — the saved entries and the board must reach
    the agent whatever the test bed does.
    """
    try:
        augmentation = ci_augment.for_session_start(project=project, session_id=session_id, cwd=cwd)
        if not augmentation.configured:
            return "", ""
        metrics_service.open_turn(augmentation.metric(project.id, session_id, closed=True))
        if augmentation.run_id:
            insights.record_turn(
                augmentation.join_facts(session_id), session_id=session_id, project_id=project.id
            )
        instruction = ""
        if session_id and augmentation.descriptor and augmentation.descriptor.mcp_pull:
            instruction = ci_augment.instruction_for(session_id)
        return instruction, augmentation.block
    except Exception:  # the experiment may never cost a session its context
        return "", ""


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
    """Record the prompt, consult CI, open this turn's row.

    Three steps, deliberately separated. The store is opened for the prompt
    record and closed again BEFORE the server is consulted, so a slow endpoint
    holds no database handle and a CI-side failure cannot take the store work
    with it; the row is written afterwards in its own short transaction. A turn
    is opened even when the prompt is empty and even when CI never ran: a row
    per turn from the day this ships is what turns the stretch before the
    endpoint goes live into a baseline rather than a gap.

    Failures are swallowed here rather than in the caller so that a store
    problem costs the record, not the teammate delta the hook still owes the
    session. Returns the retrieved block to inject, or ``""`` — which is what
    every turn returns while the experiment is off.
    """
    try:
        with store_session() as store:
            project = active_project(store, cwd)
            if prompt is not None and prompt.strip():
                store.ensure_project(project)
                store.add_prompt(prompt, project.id, source="claude-code")
    except Exception:  # never disrupt the session to record it
        return ""
    block = ""
    try:
        augmentation = ci_augment.for_prompt(
            prompt, project=project, session_id=session_id, cwd=cwd
        )
        block = augmentation.block
        metrics_service.open_turn(augmentation.metric(project.id, session_id, closed=False))
        if prompt is not None and prompt.strip():
            insights.record_prompt(prompt, session_id=session_id, project_id=project.id)
        if augmentation.run_id:
            insights.record_turn(
                augmentation.join_facts(session_id), session_id=session_id, project_id=project.id
            )
    except Exception:  # recording may never cost the agent its context
        return block
    return block


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
