"""Runtime handlers for the Claude Code hooks installed by ``agents connect``.

- ``session_start_context`` builds what aisquare injects when a session starts:
  the curated context block — and, when the orchestrator is active for the
  project and the session has a team, the team board plus protocol.
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

import contextlib
import os
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aisquare.core import agents as agents_core
from aisquare.core import claude_accounts as claude_accounts_core
from aisquare.core import insights, launch_seal, orchestrator, paths, selfcli
from aisquare.core import spawn as spawn_core
from aisquare.core.config import load_config
from aisquare.core.injection import build_block, without_loaded_preferences
from aisquare.core.store import store_session
from aisquare.core.workspace import active_project
from aisquare.models import FleetAgent, ProjectInfo, TeamSession
from aisquare.services import auto_mode, ci_augment, quiet_compact
from aisquare.services import claude_accounts as claude_accounts_service
from aisquare.services import explainability as explainability_service
from aisquare.services import metrics as metrics_service
from aisquare.services import project as project_service
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
    agent: str = "claude-code",
    native_session_id: str | None = None,
    account: str | None = None,
    config_dir: Path | None = None,
) -> str:
    """Context to inject at an agent's ``SessionStart`` (empty if nothing useful).

    ``config_dir`` is the home the session runs on (the hook's ``--config-dir``), whose
    instruction files the agent loads itself; else the agent's own as this shell names it."""
    from aisquare.core.agent_adapters import recorded_adapter

    began = datetime.now(tz=UTC)  # the row's started_at: when the hook was entered
    record_trace_join(session_id)
    adapter = recorded_adapter(agent)
    if session_id:
        _observe(session_id, agent, native_session_id, transcript_path, account)
    # One store session for the launch's binding and the context's reads: each session a hook
    # opens takes the store's lock again, on every agent's start (review of #259, round 20).
    with store_session() as store:
        if session_id:
            from aisquare.core.agent_sessions import bind_launch_session, own_process

            bind_launch_session(
                store,
                session_id,
                started=True,
                source=source,
                own=own_process(adapter) if adapter is not None else None,
            )
        project = active_project(store, cwd)
        entries = store.entries(project_id=project.id)
        recorded = project_service.tracked(store, project)
        stored = store.get_project(project.id) is not None
    # Not what the agent loads itself: a CLAUDE.md section `agents connect` imported rode
    # every request of Claude Code's twice, an AGENTS.md one Codex's (SCOPE #113 274, L3).
    if adapter is not None:
        home = config_dir or adapter.config_dir(agents_core.home(), os.environ)
        entries = without_loaded_preferences(entries, adapter.instruction_files(home, cwd))
    block = build_block(entries, project) if entries else ""
    team_block = (
        team_service.hook_session_start(
            session_id,
            cwd,
            source,
            transcript_path=transcript_path,
            model=model,
            effort=effort,
            agent=agent,
            native_session_id=native_session_id,
            account=account,
        )
        if session_id
        else ""
    )
    # Last, and only when the experiment is on: the standing instruction to
    # consult the recall tool, then any retrieved material — closest to what
    # the agent is about to do, and the part it should weigh least. Not in a
    # directory nobody added while capture is off: its row would be a turn
    # tied to a project that does not exist.
    instruction, retrieved = (
        _session_start_ci(project, session_id, cwd, began=began, stored=stored)
        if recorded
        else ("", "")
    )
    return "\n\n".join(part for part in (block, team_block, instruction, retrieved) if part)


def _observe(
    session_id: str,
    agent: str,
    native_session_id: str | None,
    transcript_path: str | None,
    account: str | None,
) -> None:
    """What the agent handed this hook of its session, for its fleet row's launch record
    (``core.launch_seal.observe``): written there only from a process outside any
    sandbox, and only for the row's own session. Never raises: the record is a
    replay's, never the hook's to fail over."""
    with suppress(Exception):
        from aisquare.core.agent_adapters import recorded_adapter
        from aisquare.core.agent_sessions import own_process

        adapter = recorded_adapter(agent)
        launch_seal.observe(
            session_id,
            agent=agent,
            native=native_session_id or session_id,
            transcript=transcript_path,
            home=account or _home_of(agent),
            own=own_process(adapter) if adapter is not None else None,
        )


def _home_of(agent: str) -> str | None:
    """The config dir this hook's agent runs under, as its launch exported it."""
    from aisquare.core.agent_adapters import recorded_adapter

    adapter = recorded_adapter(agent)
    value = os.environ.get(adapter.home_env, "").strip() if adapter is not None else ""
    return value or None


def _session_start_ci(
    project: ProjectInfo,
    session_id: str | None,
    cwd: Path | None,
    *,
    began: datetime,
    stored: bool,
) -> tuple[str, str]:
    """Consult CI at session start and RECORD the outcome; never raises.

    The row is closed at creation — a session start is a call, not a turn.
    Nothing is written while the experiment is off or unconfigured: those
    machines record their baseline per prompt, and a row per session start
    would only say "off" again. A failure anywhere here costs the CI part of
    the context and nothing else — the saved entries and the board must reach
    the agent whatever the test bed does.

    The directory is captured before its rows are written, with ``[projects] capture`` on
    (the caller asked :func:`~aisquare.services.project.tracked`). The team hook captured
    it for them until it captured only for a board (review of #259, round 18), and a
    session start in a directory with no board then wrote a turn and an insights row for
    a project with no row, which ``metrics show --project`` could not reach (round 19).
    Not when the hook found its row there already (``stored``): a store session and a write
    of its own on every session start, the team hook's capture moments before included, was
    a second write lock as a fleet started its agents (round 20).
    """
    try:
        augmentation = ci_augment.for_session_start(
            project=project, session_id=session_id, cwd=cwd, began=began
        )
        if not augmentation.configured:
            return "", ""
        if not stored:
            with store_session() as store:
                project_service.capture(store, project)
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
    agent: str = "claude-code",
    native_session_id: str | None = None,
    account: str | None = None,
    source: str = "claude-code",
) -> str:
    """Record a submitted prompt; return the team delta to add to context."""
    retrieved = capture_prompt(prompt, cwd, session_id=session_id, source=source)
    if session_id is None:
        return retrieved
    _observe(session_id, agent, native_session_id, transcript_path, account)
    delta = team_service.hook_prompt_heartbeat(
        session_id,
        cwd,
        transcript_path=transcript_path,
        model=model,
        effort=effort,
        agent=agent,
        native_session_id=native_session_id,
        account=account,
    )
    return "\n\n".join(part for part in (delta, retrieved) if part)


def session_ended(
    cwd: Path | None,
    *,
    session_id: str | None = None,
    reason: str | None = None,
    transcript_path: str | None = None,
    agent: str | None = None,
) -> None:
    """Retire the session from the orchestrator and, unless it is a fleet agent's
    ``/clear`` (``reason``), release its claims — see ``team.hook_session_end``.

    Then the session's last stretch of usage — whatever followed its last Stop,
    an ``/exit`` mid-turn included — and its row marked ended.
    """
    if session_id is None:
        return
    try:
        team_service.hook_session_end(session_id, cwd, reason=reason)
    finally:
        metrics_service.record_usage(
            session_id, transcript_path, cwd, trigger="session_end", ended=True, agent=agent
        )


def turn_stopped(
    cwd: Path | None,
    *,
    session_id: str | None = None,
    stop_hook_active: bool = False,
    model: str | None = None,
    effort: str | None = None,
    last_message: str | None = None,
    permission_mode: str | None = None,
    transcript_path: str | None = None,
    agent: str | None = None,
) -> team_service.StopDecision | None:
    """Mark the session as waiting for input, and close this turn's metrics row.

    A manager with fresh board decisions gets a :class:`~aisquare.services.team.StopDecision`
    back instead, which the hook prints so Claude Code keeps its turn going
    (docs/plans/fleet-tui.md §7.3). Everyone else: ``None``, as before.

    A manager left waiting arms the quiet compaction (P1 WS-4, #281) with its turn's last
    message and permission mode: :func:`quiet_compact.arm` refuses a question or plan mode
    itself, and otherwise starts its worker. Off by default; never costs the turn its row.

    The turn's token usage is read off ``transcript_path`` from where the last
    record stopped, by ``agent``'s reader, and lands on the row ``close_turn`` closed.
    """
    if session_id is None:
        return None
    try:
        decision = team_service.hook_stop(
            session_id, cwd, stop_hook_active=stop_hook_active, model=model, effort=effort
        )
    finally:
        # After the team update, never before: a metrics failure must not cost the
        # board its state change (or the manager its decisions), and close_turn
        # swallows its own errors. In a finally: a manager's failed wake-up raises
        # after the row says waiting, and the turn is over all the same (review
        # of the #205 fold, round 1; see turn_failed). record_usage swallows its own
        # errors too.
        trace_id = metrics_service.close_turn(session_id)
        metrics_service.record_usage(
            session_id, transcript_path, cwd, trigger="stop", trace_id=trace_id, agent=agent
        )
    # Last, and it swallows its own errors too: auto-mode refusals in the
    # transcript's tail put the row in attention with one board line (#150).
    auto_mode.record_refusals(session_id)
    from aisquare.core.agent_adapters import recorded_adapter

    # Only an agent with a command that compacts it (`capabilities.compact`): what the quiet
    # compaction types.
    kind = recorded_adapter(agent)
    if decision is None and kind is not None and kind.capabilities.compact is not None:
        with contextlib.suppress(Exception):  # an optimisation may never cost a Stop
            quiet_compact.arm(
                session_id,
                last_message=last_message,
                permission_mode=permission_mode,
                detach=_detach,
            )
    return decision


def compact_check(session_id: str, *, cursor: int, token: str) -> str:
    """The quiet compaction's worker (``aisquare hook compact-check``): see
    :func:`quiet_compact.check`."""
    return quiet_compact.check(session_id, cursor, token)


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
    notice: claude_accounts_core.LimitNotice | None = None,
    hand_over: bool = True,
    transcript_path: str | None = None,
    cwd: Path | None = None,
    agent: str | None = None,
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

    Codex has no ``StopFailure``: its hooks read the limit off the end of the
    session's rollout and come here with the ``notice`` already read
    (``services.agent_events``). ``hand_over=False`` records the limit without
    moving the agent — a session that is ending has nothing left to move.

    One switch per limit (#277): whether the hand-over will run is decided BEFORE the
    board write (:func:`_plan_hand_over`), so the ``limited`` line can say the fleet is
    moving the agent and wake no manager; woken, a manager following its cycle switched
    the replacement again. Every ``rate_limit`` also leaves a limit mark on the account
    the agent was on (``claude_accounts.remember_limit``), so no pick sends an agent back
    to it before the reset it named.
    """
    if session_id is None:
        return
    plan = _plan_hand_over(session_id, error=error, message=message, notice=notice)
    if plan is not None and plan.go and not hand_over:
        plan = _HandOverPlan(plan.agent, go=False)
    try:
        failure = (
            team_service.hook_stop_failure(
                session_id,
                error=error,
                message=message,
                details=details,
                handing_over=plan is not None and plan.go,
            )
            if notice is None
            else team_service.hook_stop_failure(
                session_id,
                error=error,
                message=message,
                details=details,
                notice=notice,
                handing_over=plan is not None and plan.go,
            )
        )
    finally:
        trace_id = metrics_service.close_turn(session_id)
        metrics_service.record_usage(
            session_id,
            transcript_path,
            cwd,
            trigger="stop_failure",
            trace_id=trace_id,
            agent=agent,
        )
    # A re-fire for the same window (Claude Code does that) is not a second
    # hand-over: the first worker is at work, or has already moved the agent.
    if plan is not None and failure is not None and failure.limited and not failure.already_limited:
        _hand_over_if_configured(failure, plan)


@dataclass(frozen=True)
class _HandOverPlan:
    """What :func:`_plan_hand_over` decided for one ``rate_limit``: ``go`` starts the worker;
    otherwise ``wait_note`` (a reset within the wait window) goes on the board."""

    agent: FleetAgent
    go: bool
    wait_note: str | None = None


def _plan_hand_over(
    session_id: str,
    *,
    error: str | None,
    message: str | None,
    notice: claude_accounts_core.LimitNotice | None = None,
) -> _HandOverPlan | None:
    """Whether a ``rate_limit`` on ``session_id`` will be handed over — asked before the board
    write, so the ``limited`` line can say so — and the limit mark it leaves either way.

    ``None`` for anything that is not a fresh limit on a fleet agent: another error, a
    session already ``limited`` or mid-hand-over (a re-fire), a hand-typed session (the
    operator's to move). A plan with ``go`` False is ``on_limit = "wait"``, a reset close
    enough to wait for, or no other account to go to.
    """
    if (error or "").strip() != "rate_limit" or not orchestrator.team_enabled():
        return None
    if notice is None:  # Codex's is read off its rollout (``services.agent_events``)
        notice = claude_accounts_core.parse_limit_notice(message, now=datetime.now(tz=UTC))
    with store_session() as store:
        session = store.get_session(session_id)
        if session is None:
            return None
        # The row's own project, not the session's: under AISQUARE_TEAM_HUB the session is on
        # the hub's board and the row is in its spawn's project, and asked by the board, no
        # limited agent was ever handed over (review of #259, round 23).
        agent = store.fleet_agent_for_session(session.id)
        project = store.get_project(agent.project_id) if agent is not None else None
    # As `fleet.switch` reads where the agent is, and Remote's scan asks the same.
    leaving = claude_accounts_service.leaving_slot(
        agent.account_slot if agent is not None else None, session.account
    )
    if notice is not None:
        claude_accounts_service.remember_limit(leaving, notice.window, notice.resets_at)
    if agent is None or project is None:
        return None  # a hand-typed session is the operator's to move
    if session.state in ("limited", team_service.HANDOVER_STATE):
        return None  # a re-fire: `hook_stop_failure` writes nothing new either
    settings = claude_accounts_service.accounts_settings()
    if settings.on_limit != "switch":
        return _HandOverPlan(agent, go=False)
    resets_at = notice.resets_at if notice is not None else None
    if resets_at is not None:
        wait = timedelta(minutes=settings.wait_if_reset_within_minutes)
        if resets_at - datetime.now(tz=UTC) <= wait:
            # Asked before whether there is an account to go to, which reads every
            # account's `.claude.json` (tens of MB) and, on macOS, its Keychain item, each
            # with a 5 s timeout and perhaps an unlock dialog from a background hook: none
            # of it changes a wait (review of #259, round 16). The note is true on one
            # account too — `on_limit = "switch"` is what is not done — and comes only with
            # a limit this close to its reset.
            return _HandOverPlan(
                agent,
                go=False,
                wait_note=f"{agent.label}: usage limit lifts within "
                f"{settings.wait_if_reset_within_minutes} min — waiting for the reset instead "
                "of switching",
            )
    # A row with no agent recorded is Claude Code's, as its launch record says
    # (`recorded_adapter`): asked of every agent's accounts, a signed-in Codex one started a
    # worker that could only refuse (review r16 of #113, 3/8).
    from aisquare.core.agent_adapters import DEFAULT_AGENT
    from aisquare.services import handoff  # lazy: only a limit reads it, never every hook

    whose = agent.agent or DEFAULT_AGENT
    # One account: `wait`, with no board note on every limit that it is. A registry
    # that cannot be read is no answer, and goes on to the worker, which says so. An
    # account of another installed agent is one to go to while `cross_agent_handover`
    # is on: the worker crosses once every own account is read full (task #15).
    return _HandOverPlan(
        agent,
        go=claude_accounts_service.somewhere_to_hand_over(leaving, agent=whose)
        or handoff.somewhere_across(leaving, whose),
    )


def _hand_over_if_configured(failure: team_service.TurnFailure, plan: _HandOverPlan) -> None:
    session = failure.session
    if plan.wait_note is not None:
        team_service.hook_note(session.project_id, plan.wait_note, session_id=session.id)
    if not plan.go:
        return
    window = failure.notice.window if failure.notice is not None else "usage"
    argv = selfcli.isolated_argv_for(
        ["--quiet", "hook", "hand-over", session.id, "--reason", f"{window} limit"]
    )
    try:
        _detach(argv)
    except OSError as exc:
        team_service.hook_handover_failed(
            session.project_id,
            f"{plan.agent.label}: not switched — could not start the hand-over worker ({exc})",
            session_id=session.id,
            label=plan.agent.label,
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


def _own_reset(session: TeamSession) -> str:
    """``; its own limit resets in 2d 4h (Mon 00:00)`` — the other answer to a hand-over that
    failed, beside the ``fleet switch`` it names (#277); ``""`` when the limit named none."""
    if session.limit_resets_at is None:
        return ""
    return f"; its own limit resets {claude_accounts_core.format_reset(session.limit_resets_at)}"


def fresh_replacement_refusal(agent: FleetAgent, *, now: datetime | None = None) -> str | None:
    """Why ``fleet switch`` refuses ``agent`` without ``--force``, or ``None`` (#277): it is a
    replacement the automatic hand-over spawned less than :data:`HANDOVER_COOLDOWN` ago.

    One limit, one switch: a manager woken by the same limit used to switch the replacement
    the hand-over had just started. Here, beside the cooldown it shares with the automatic
    path's own brake (:func:`hand_over`), rather than in ``services/fleet.py``.
    """
    since = (now or datetime.now(tz=UTC)) - agent.created_at
    if agent.spawned_by != HANDOVER_SPAWNER or since >= HANDOVER_COOLDOWN:
        return None
    minutes = max(1, int(since.total_seconds() // 60))
    return (
        f"{agent.label!r} was moved {minutes} min ago by the usage-limit hand-over; "
        "`--force` moves it again"
    )


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

    A switch that goes ahead returns its receipt, and its ``failures`` are what
    the move could not do: a hand-off prompt not typed, claims not moved, an old
    session not marked ended, a mark not taken back. By hand the CLI prints them
    with the other notes; this worker has no terminal (:func:`_detach`), so they
    go on the board as one note beside ``switched``. Dropped, the replacement
    sat idle at an empty prompt while the board said it had been given one
    (review of #203, final round, FLEET-5). The receipt's other notes stay off
    the board: every switch has them (the headroom each account had, the launch
    replayed, the worktree kept), and posting them put a note on the feed at
    nearly every hand-over, account advice included, for the other agents to
    read (review of the #203 final-round fixes, F2).
    """
    with store_session() as store:
        session = store.get_session(session_id)
        if session is None:
            return
        agent = store.fleet_agent_for_session(session.id)  # in its own project, as above
        project = store.get_project(agent.project_id) if agent is not None else None
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
        team_service.hook_handover_failed(
            session.project_id,
            f"{agent.label}: not switched — moved {max(1, int(since.total_seconds() // 60))} "
            "min ago; waiting for the reset instead — "
            f"`{project_service.fleet_command('switch', project, agent.label)} --force` "
            f"moves it again{_own_reset(session)}",
            session_id=session.id,
            label=agent.label,
        )
        return
    # Lazy: services.fleet imports this module's neighbours; a cycle at import
    # time would cost every hook, and this runs in the worker alone.
    from aisquare.services import fleet as fleet_service

    try:
        receipt = fleet_service.switch(
            project, agent.label, reason=reason, spawned_by=HANDOVER_SPAWNER, automatic=True
        )
    except fleet_service.LaunchRefused:
        return  # its reason is on the board already, once for the row
    except fleet_service.FleetError as exc:
        # The manager's to act on (#277): the refusal names `fleet switch` or the reset.
        team_service.hook_handover_failed(
            session.project_id,
            f"{agent.label}: not switched — {exc}{_own_reset(session)}",
            session_id=session.id,
            label=agent.label,
        )
        return
    except Exception as exc:
        # Anything else — a locked store, a tmux error, a bug. The `limited` line woke
        # nobody because this worker was coming, and its stderr is DEVNULL (`_detach`), so
        # unsaid here the agent sat parked until the reset (review of #304, finding 1). Said
        # best effort, then raised for the CLI wrapper to fail open as before.
        with contextlib.suppress(Exception):
            team_service.hook_handover_failed(
                session.project_id,
                f"{agent.label}: not switched — hand-over worker failed "
                f"({type(exc).__name__}: {exc}); "
                f"`{project_service.fleet_command('switch', project, agent.label)}` moves it "
                f"by hand{_own_reset(session)}",
                session_id=session.id,
                label=agent.label,
            )
        raise
    if receipt.failures:
        team_service.hook_note(
            session.project_id,
            f"{agent.label}: switched — {'; '.join(receipt.failures)}",
            session_id=receipt.started.session_id,
        )


def capture_prompt(
    prompt: str | None,
    cwd: Path | None,
    *,
    session_id: str | None = None,
    source: str = "claude-code",
) -> str:
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
    # ``capture.enabled``, read once a prompt (#268). Off, the prompt is neither stored nor
    # spooled; the turn row below still opens, without text, and the teammate delta is the
    # caller's, not capture. A config that cannot be read may have said off, so it records
    # no prompt either: privacy fails closed, unlike every other setting's house default.
    try:
        record = load_config(paths.config_path(), copy=False).capture.enabled
    except Exception as exc:
        sys.stderr.write(
            f"aisquare: prompt not recorded — the config could not be read to check "
            f"capture.enabled ({type(exc).__name__}); run: aisquare doctor\n"
        )
        record = False
    text = prompt if record and prompt is not None and prompt.strip() else None
    try:
        with store_session() as store:
            project = active_project(store, cwd)
            # Before anything is written, even for an empty prompt: a metric row is
            # written for every turn below, and a row whose project_id has no
            # `project` row is unreachable from `metrics show --project <name>`,
            # which resolves names through that table. With `[projects] capture`
            # off, a directory nobody added records nothing at all: no prompt, no
            # spool, no CI consult, no turn row.
            if not project_service.tracked(store, project):
                return ""
            project_service.capture(store, project)
            if text is not None:
                store.add_prompt(text, project.id, source=source)
        if text is not None:
            insights.record_prompt(text, session_id=session_id, project_id=project.id)
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
