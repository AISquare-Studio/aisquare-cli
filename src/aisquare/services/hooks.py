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
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aisquare.core import insights, selfcli
from aisquare.core import snapshot as snapshot_core
from aisquare.core import spawn as spawn_core
from aisquare.core.injection import build_block
from aisquare.core.store import store_session
from aisquare.core.workspace import active_project
from aisquare.models import ProjectInfo
from aisquare.services import auto_mode, ci_augment
from aisquare.services import claude_accounts as claude_accounts_service
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
            trace_id=explainability_service.run_trace_id(),
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
    began = datetime.now(tz=UTC)  # the row's started_at: when the hook was entered
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
    instruction, retrieved = _session_start_ci(project, session_id, cwd, began=began)
    return "\n\n".join(
        part for part in (directive, block, team_block, instruction, retrieved) if part
    )


def _session_start_ci(
    project: ProjectInfo, session_id: str | None, cwd: Path | None, *, began: datetime
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
        augmentation = ci_augment.for_session_start(
            project=project, session_id=session_id, cwd=cwd, began=began
        )
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


def session_ended(
    cwd: Path | None, *, session_id: str | None = None, reason: str | None = None
) -> None:
    """Retire the session from the orchestrator and, unless it is a fleet agent's
    ``/clear`` (``reason``), release its claims — see ``team.hook_session_end``."""
    if session_id is not None:
        team_service.hook_session_end(session_id, cwd, reason=reason)


def turn_stopped(
    cwd: Path | None, *, session_id: str | None = None, stop_hook_active: bool = False
) -> team_service.StopDecision | None:
    """Mark the session as waiting for input, and close this turn's metrics row.

    A manager with fresh board decisions gets a :class:`~aisquare.services.team.StopDecision`
    back instead, which the hook prints so Claude Code keeps its turn going
    (docs/plans/fleet-tui.md §7.3). Everyone else: ``None``, as before.
    """
    if session_id is None:
        return None
    try:
        decision = team_service.hook_stop(session_id, cwd, stop_hook_active=stop_hook_active)
    finally:
        # After the team update, never before: a metrics failure must not cost the
        # board its state change (or the manager its decisions), and close_turn
        # swallows its own errors. In a finally: a manager's failed wake-up raises
        # after the row says waiting, and the turn is over all the same (review
        # of the #205 fold, round 1; see turn_failed).
        metrics_service.close_turn(session_id)
    # Last, and it swallows its own errors too: auto-mode refusals in the
    # transcript's tail put the row in attention with one board line (#150).
    auto_mode.record_refusals(session_id)
    return decision


def needs_attention(
    cwd: Path | None,
    *,
    session_id: str | None = None,
    message: str | None = None,
    notification_type: str | None = None,
) -> None:
    """Route a Claude Code notification by its TYPE: a bell, a feed line, or nothing (#153)."""
    if session_id is not None:
        team_service.hook_notification(
            session_id, cwd, message, notification_type=notification_type
        )


def turn_failed(
    *,
    session_id: str | None = None,
    error: str | None = None,
    message: str | None = None,
    details: str | None = None,
) -> None:
    """The turn ended on an API error (Claude Code's ``StopFailure``), not a Stop (#146).

    Three steps. The board first: a ``rate_limit`` parks the session as
    ``limited`` with the reset time the message named and wakes the manager;
    any other error returns it to ``waiting`` with a feed line. Then this
    turn's metrics row is closed, whatever the board write did — the turn is
    over however it ended, and ``close_turn`` closes only the NEWEST open row,
    so one left open here stayed open for good (review of the #205 fold,
    round 1). Last, and only when ``[accounts] on_limit = "switch"`` names it,
    the hand-over: a limited FLEET agent is moved to the account with the most
    headroom (``services.fleet.switch``), unless the limit lifts within
    ``wait_if_reset_within_minutes`` — a reset ten minutes away is cheaper than a
    cold start elsewhere, and Claude Code's own wait-and-continue covers it. It
    acts on the board's record, so a board write that failed skips it.

    The hand-over is DECIDED here and PERFORMED elsewhere: this hook is a child
    of the very pane ``switch`` is about to kill, so the work goes to a worker
    in its own session (:func:`_detach` → ``aisquare hook hand-over`` →
    :func:`hand_over`), and the hook returns at once. A hand-over that cannot
    find headroom leaves the agent limited, its own wait intact, and says so on
    the board — which is the ``wait`` behaviour, and correct.
    """
    if session_id is None:
        return
    try:
        failure = team_service.hook_stop_failure(
            session_id, error=error, message=message, details=details
        )
    finally:
        metrics_service.close_turn(session_id)
    if failure is not None and failure.limited and not failure.already_limited:
        # A re-fire for the same window (Claude Code does that) is not a second
        # hand-over: the first worker is at work, or has already moved the agent.
        _hand_over_if_configured(failure)


def _hand_over_if_configured(failure: team_service.TurnFailure) -> None:
    settings = claude_accounts_service.accounts_settings()
    if settings.on_limit != "switch":
        return
    session = failure.session
    with store_session() as store:
        agent = store.fleet_agent_for_session(session.project_id, session.id)
        project = store.get_project(session.project_id)
    if agent is None or project is None:
        return  # a hand-typed session is the operator's to move
    resets_at = failure.notice.resets_at if failure.notice is not None else None
    if resets_at is not None:
        wait = timedelta(minutes=settings.wait_if_reset_within_minutes)
        if resets_at - datetime.now(tz=UTC) <= wait:
            team_service.hook_note(
                session.project_id,
                f"{agent.label}: usage limit lifts within {settings.wait_if_reset_within_minutes} "
                "min — waiting for the reset instead of switching",
                session_id=session.id,
            )
            return
    window = failure.notice.window if failure.notice is not None else "usage"
    argv = selfcli.argv_for(
        ["--quiet", "hook", "hand-over", session.id, "--reason", f"{window} limit"]
    )
    try:
        _detach(argv)
    except OSError as exc:
        team_service.hook_note(
            session.project_id,
            f"{agent.label}: not switched — could not start the hand-over worker ({exc})",
            session_id=session.id,
        )


def _detach(argv: list[str]) -> None:
    """Start ``argv`` in its own session, no terminal, without this agent's own identity.

    Run inline, the hand-over died with its caller: the hook is a child of the
    pane, ``switch``'s ``/exit`` queued behind the still-running hook, the
    grace elapsed, and the window kill took the hook down before ``spawn`` ever
    ran — a dead agent, a live row on a gone pane, no replacement, no board
    line (review of #205, finding 1). ``start_new_session`` puts the worker
    outside the pane's session, so tmux's kill and its SIGHUP do not reach it
    (the idiom ``distill.spawn_drain`` uses). The tracing identity and the
    fleet row's name are dropped from its environment: the worker is neither
    this agent nor a session of its own. Raises ``OSError`` when the worker
    cannot be started, for the caller to put on the board.
    """
    env = spawn_core.untraced_env()
    env.pop("AISQUARE_FLEET_AGENT", None)
    subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )


HANDOVER_SPAWNER = "usage-limit"
"""``FleetAgent.spawned_by`` of a replacement the automatic hand-over started."""
HANDOVER_COOLDOWN = timedelta(minutes=10)
"""How soon after a ``switched`` event the same label may be handed over again by the
automatic path: a cap on the ping-pong a wrong reading could otherwise drive."""


def hand_over(session_id: str, *, reason: str | None = None) -> None:
    """Move the limited fleet agent of ``session_id`` — the detached half of the hand-over.

    Runs in the worker :func:`_detach` started, after the hook that decided it
    has returned. Silent for a session that is not a fleet agent's (the
    operator's to move) or is gone. Three brakes, each a board note rather than
    a move (review of #205, second round): a hand-over already in flight for
    this session (``team.HANDOVER_STATE``), one that moved this label within
    :data:`HANDOVER_COOLDOWN`, and — inside ``switch(automatic=True)`` — no
    account actually under the line. In every case the agent stays parked with
    Claude Code's own wait intact.
    """
    with store_session() as store:
        session = store.get_session(session_id)
        if session is None:
            return
        agent = store.fleet_agent_for_session(session.project_id, session.id)
        project = store.get_project(session.project_id)
    if agent is None or project is None:
        return
    if session.state == team_service.HANDOVER_STATE:
        team_service.hook_note(
            session.project_id,
            f"{agent.label}: not switched — a hand-over is already in flight",
            session_id=session.id,
        )
        return
    # The row bound to the session IS the last hand-over's replacement when it
    # was spawned by one: its age is the cooldown clock, no event lookup needed.
    since = datetime.now(tz=UTC) - agent.created_at
    if agent.spawned_by == HANDOVER_SPAWNER and since < HANDOVER_COOLDOWN:
        team_service.hook_note(
            session.project_id,
            f"{agent.label}: not switched — moved {max(1, int(since.total_seconds() // 60))} "
            "min ago; waiting for the reset instead",
            session_id=session.id,
        )
        return
    # Lazy: services.fleet imports this module's neighbours; a cycle at import
    # time would cost every hook, and this runs in the worker alone.
    from aisquare.services import fleet as fleet_service

    try:
        fleet_service.switch(
            project, agent.label, reason=reason, spawned_by=HANDOVER_SPAWNER, automatic=True
        )
    except fleet_service.FleetError as exc:
        team_service.hook_note(
            session.project_id, f"{agent.label}: not switched — {exc}", session_id=session.id
        )


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

    Recording the prompt — locally AND into the Explainability spool — happens
    in the first block, before the server is consulted. Spooling it after the CI
    call, inside the same ``try``, put an observer of the job downstream of the
    job: a raise anywhere in the CI path (``metric()`` is a bare pydantic
    construction) dropped the prompt from the spool, and even without a raise it
    waited out the descriptor's whole ceiling first.

    ``started_at`` for the turn's row is taken HERE, first, before the store is
    opened: it is the moment the developer hit enter, and the record-and-spool
    block below is work the turn already contains, not a delay before it.
    """
    began = datetime.now(tz=UTC)
    try:
        with store_session() as store:
            project = active_project(store, cwd)
            # Unconditionally, even for an empty prompt: a metric row is written
            # for every turn below, and a row whose project_id has no `project`
            # row is unreachable from `metrics show --project <name>`, which
            # resolves names through that table.
            store.ensure_project(project)
            if prompt is not None and prompt.strip():
                store.add_prompt(prompt, project.id, source="claude-code")
        if prompt is not None and prompt.strip():
            insights.record_prompt(prompt, session_id=session_id, project_id=project.id)
    except Exception as exc:  # never disrupt the session to record it — but say what it cost
        # Swallowed here so the teammate delta still reaches the session; on
        # stderr, never stdout, for the reason cli/hook.py gives — stdout is
        # the agent's context. Silence was the regression that doctrine fixed.
        sys.stderr.write(
            f"aisquare: prompt not recorded ({type(exc).__name__}: {exc}) — this turn has no "
            "prompt log and no CI row; run: aisquare doctor\n"
        )
        return ""
    block = ""
    try:
        augmentation = ci_augment.for_prompt(
            prompt, project=project, session_id=session_id, cwd=cwd, began=began
        )
        block = augmentation.block
        metrics_service.open_turn(augmentation.metric(project.id, session_id, closed=False))
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
