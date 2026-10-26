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
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aisquare.core import claude_accounts as claude_accounts_core
from aisquare.core import code_index, harness, insights, orchestrator, paths, selfcli, transcripts
from aisquare.core import spawn as spawn_core
from aisquare.core.config import AccountsSettings, load_config
from aisquare.core.injection import build_block, claude_md_files, without_loaded_preferences
from aisquare.core.store import ContextStore, store_session
from aisquare.core.workspace import active_project
from aisquare.models import ClaudeAccount, FleetAgent, FleetAgentStatus, ProjectInfo, TeamSession
from aisquare.services import auto_mode, ci_augment, model_memory, quiet_compact
from aisquare.services import claude_accounts as claude_accounts_service
from aisquare.services import code as code_service
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
) -> str:
    """Context to inject at Claude Code ``SessionStart`` (empty if nothing useful)."""
    began = datetime.now(tz=UTC)  # the row's started_at: when the hook was entered
    record_trace_join(session_id)
    with store_session() as store:
        project = active_project(store, cwd)
        entries = store.entries(project_id=project.id)
        recorded = project_service.tracked(store, project)
        stored = store.get_project(project.id) is not None
    # Not what Claude Code loads itself: a CLAUDE.md section `agents connect` imported
    # rode every request twice.
    entries = without_loaded_preferences(entries, claude_md_files(cwd))
    _refresh_code_index(project)
    block = build_block(entries, project) if entries else ""
    team_block = (
        team_service.hook_session_start(
            session_id,
            cwd,
            source,
            transcript_path=transcript_path,
            model=model,
            effort=effort,
            reserve=len(block) + 2 if block else 0,
        )
        if session_id
        else ""
    )
    if session_id:
        # After the team hook, which binds a fleet row to this session where it may. The
        # model alone: no effort a session start can read is the session's (seq 3513).
        remember_the_start(session_id, model=model)
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
    joined = "\n\n".join(part for part in (block, team_block, instruction, retrieved) if part)
    if len(joined) > team_service.HOOK_OUTPUT_CAP:
        # The CI parts come after the board and were not known when the requirements
        # block was sized: if the whole goes over the cap, the block goes, rather than
        # Claude Code cutting the tail. The board and the rest stay as main had them.
        joined = _REQUIREMENTS_BLOCK.sub("", joined)
    return joined


#: The session start's requirements block (``briefs.session_requirements``), whole.
_REQUIREMENTS_BLOCK = re.compile(r"\n<aisquare-requirements>\n.*?\n</aisquare-requirements>", re.S)


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
    cwd: Path | None,
    *,
    session_id: str | None = None,
    reason: str | None = None,
    transcript_path: str | None = None,
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
            session_id, transcript_path, cwd, trigger="session_end", ended=True
        )


def turn_stopped(
    cwd: Path | None,
    *,
    session_id: str | None = None,
    stop_hook_active: bool = False,
    last_message: str | None = None,
    permission_mode: str | None = None,
    transcript_path: str | None = None,
    effort: str | None = None,
) -> team_service.StopDecision | None:
    """Mark the session as waiting for input, and close this turn's metrics row.

    A manager with fresh board decisions gets a :class:`~aisquare.services.team.StopDecision`
    back instead, which the hook prints so Claude Code keeps its turn going
    (docs/plans/fleet-tui.md §7.3). Everyone else: ``None``, as before.

    A manager left waiting arms the quiet compaction (P1 WS-4, #281) with its turn's last
    message and permission mode: :func:`quiet_compact.arm` refuses a question or plan mode
    itself, and otherwise starts its worker. Off by default; never costs the turn its row.

    The turn's token usage is read off ``transcript_path`` from where the last
    record stopped, and lands on the row ``close_turn`` closed. And a fleet agent's model and
    effort as the turn ran them (``effort``: the payload's ``effort.level``), which a ``/model``
    may have changed: :func:`remember_the_model`.
    """
    if session_id is None:
        return None
    try:
        decision = team_service.hook_stop(session_id, cwd, stop_hook_active=stop_hook_active)
    finally:
        remember_the_model(session_id, transcript_path, effort=effort)
        # After the team update, never before: a metrics failure must not cost the
        # board its state change (or the manager its decisions), and close_turn
        # swallows its own errors. In a finally: a manager's failed wake-up raises
        # after the row says waiting, and the turn is over all the same (review
        # of the #205 fold, round 1; see turn_failed). record_usage swallows its own
        # errors too.
        trace_id = metrics_service.close_turn(session_id)
        metrics_service.record_usage(
            session_id, transcript_path, cwd, trigger="stop", trace_id=trace_id
        )
    # Last, and it swallows its own errors too: auto-mode refusals in the
    # transcript's tail put the row in attention with one board line (#150).
    auto_mode.record_refusals(session_id)
    if decision is None:
        _arm_auto_stop(session_id)
        with contextlib.suppress(Exception):  # an optimisation may never cost a Stop
            quiet_compact.arm(
                session_id,
                last_message=last_message,
                permission_mode=permission_mode,
                detach=_detach,
            )
        # The turn is over: a decision keeps it going, and mid-turn is no place to move it.
        check_before_limit(session_id, transcript_path=transcript_path)
    return decision


def remember_the_model(
    session_id: str, transcript_path: str | None, *, effort: str | None = None
) -> None:
    """A fleet agent's turn end: the model and effort it runs now, for its restart and every
    next spawn (``model_memory.observe``). ``effort`` is the payload's, else the
    ``CLAUDE_EFFORT`` Claude Code gives its hooks (the CLI reads both). Nothing for a session
    no fleet window runs (no ``AISQUARE_FLEET_AGENT``): no store, no transcript read. An
    extra on the turn end, never part of it: it fails open, said on stderr."""
    agent_id = orchestrator.env_fleet_agent()
    if agent_id is None:
        return
    try:
        model_memory.observe(session_id, transcript_path, effort=effort, agent_id=agent_id)
    except Exception as exc:  # the turn ended all the same; only the reading is lost
        sys.stderr.write(
            f"aisquare: the agent's model was not read ({type(exc).__name__}: {exc}) — a "
            "restart starts the model it was launched with; run: aisquare doctor\n"
        )


def remember_the_start(session_id: str, *, model: str | None) -> None:
    """A fleet agent's session start: the model its launch runs, as the payload reports it,
    which a restart compares what it runs later with (``model_memory.started``). Nothing for
    a session no fleet window runs. Fails open, said on stderr: stdout is the agent's
    context."""
    agent_id = orchestrator.env_fleet_agent()
    if agent_id is None:
        return
    try:
        model_memory.started(session_id, model=model, agent_id=agent_id)
    except Exception as exc:  # the session starts all the same; only the reading is lost
        sys.stderr.write(
            f"aisquare: the agent's launch model was not read ({type(exc).__name__}: {exc}) "
            "— a restart compares with its launch flags; run: aisquare doctor\n"
        )


def _arm_auto_stop(session_id: str) -> None:
    """A fleet worker's Stop arms its auto-stop (P2-W1b, #283): ``autostop.arm`` writes the
    turn's token and starts the worker when the agent may have finished. Never costs a
    Stop: it swallows its own errors. Imported here, not at the top: a Stop of any agent
    imports this module, and the auto-stop is only a fleet worker's."""
    with contextlib.suppress(Exception):
        from aisquare.services import autostop

        autostop.arm(session_id, detach=_detach)


def auto_stop(session_id: str, *, after: float, token: str) -> str:
    """The auto-stop's worker (``aisquare hook auto-stop``): see :func:`autostop.check`."""
    from aisquare.services import autostop

    return autostop.check(session_id, after=after, token=token)


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
    transcript_path: str | None = None,
) -> None:
    """Route a Claude Code notification by its TYPE: a bell, a feed line, or nothing (#153).

    The idle notice is also the other safe point ``[accounts] before_limit`` acts at, after
    a turn end (:func:`check_before_limit`)."""
    if session_id is not None:
        team_service.hook_notification(
            session_id, cwd, message, notification_type=notification_type
        )
        if team_service.is_idle_notice(notification_type, message):
            check_before_limit(session_id, transcript_path=transcript_path)


def turn_failed(
    *,
    session_id: str | None = None,
    error: str | None = None,
    message: str | None = None,
    details: str | None = None,
    transcript_path: str | None = None,
    cwd: Path | None = None,
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

    One switch per limit (#277): whether the hand-over will run is decided BEFORE the
    board write (:func:`_plan_hand_over`), so the ``limited`` line can say the fleet is
    moving the agent and wake no manager; woken, a manager following its cycle switched
    the replacement again. Every ``rate_limit`` also leaves a limit mark on the account
    the agent was on (``claude_accounts.remember_limit``), so no pick sends an agent back
    to it before the reset it named.
    """
    if session_id is None:
        return
    plan = _plan_hand_over(session_id, error=error, message=message)
    try:
        failure = team_service.hook_stop_failure(
            session_id,
            error=error,
            message=message,
            details=details,
            handing_over=plan is not None and plan.go,
        )
    finally:
        trace_id = metrics_service.close_turn(session_id)
        metrics_service.record_usage(
            session_id, transcript_path, cwd, trigger="stop_failure", trace_id=trace_id
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
    session_id: str, *, error: str | None, message: str | None
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
    # One account: `wait`, with no board note on every limit that it is. A registry
    # that cannot be read is no answer, and goes on to the worker, which says so.
    return _HandOverPlan(agent, go=claude_accounts_service.somewhere_to_hand_over(leaving))


def _hand_over_if_configured(failure: team_service.TurnFailure, plan: _HandOverPlan) -> None:
    session = failure.session
    if plan.wait_note is not None:
        team_service.hook_note(session.project_id, plan.wait_note, session_id=session.id)
    if not plan.go:
        return
    window = failure.notice.window if failure.notice is not None else "usage"
    argv = selfcli.argv_for(
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


CODE_REFRESH_EVERY_SECONDS = 60.0
"""A session start refreshes a project's symbol index at most this often (#285)."""


def _refresh_code_index(project: ProjectInfo) -> None:
    """Start a detached incremental ``aisquare code index`` in ``project``'s root (#285).

    Never inline: the refresh reads only files whose size or time moved, but it lists
    the checkout with git, and this is the session-start path. Only where an index
    exists (building one is ``onboard``'s or the operator's) and ``[code] enabled``,
    and at most once a minute per project, by the mtime of a stamp beside the index:
    a fleet starting ten agents starts one refresh. Silent on any failure.
    """
    try:
        if not code_index.exists(project.id) or not code_service.settings().enabled:
            return
        stamp = code_index.code_dir(project.id) / "refreshed"
        try:
            if time.time() - stamp.stat().st_mtime < CODE_REFRESH_EVERY_SECONDS:
                return
        except FileNotFoundError:
            pass
        start_code_index(project)
    except Exception:  # an index is optional; a session start never fails for it
        return


def start_code_index(project: ProjectInfo, *, full: bool = False) -> None:
    """Start ``aisquare code index`` (``--full``: from nothing) detached in ``project``'s root.

    How ``onboard`` and ``init`` build the index too, never inline: a first index of a
    large checkout takes minutes, and the onboard returns at once (``doctor`` says while it
    builds). Touches the refresh stamp, so a session start right after starts no second
    build. Raises ``OSError`` when the worker cannot be started.
    """
    stamp = code_index.code_dir(project.id) / "refreshed"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()
    argv = ["--quiet", "code", "index", *(["--full"] if full else [])]
    _detach(selfcli.argv_for(argv), cwd=project.root)


def _detach(argv: list[str], *, cwd: Path | None = None) -> None:
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
        cwd=cwd,
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


_BEFORE_LIMIT_KEY = "before-limit:{project}:{label}"
"""The ``ui_state`` row holding the turn end that last started the before-limit worker for a
label (:class:`_TurnEnd`, as JSON): the clock of its :data:`HANDOVER_COOLDOWN`, and what the
move checks the agent has not moved past. Claimed compare-and-set, so two hooks of one agent
start one worker; by label, so the replacement a move starts inherits it. The worker gives it
back when it ends on something the next turn end can decide from the cache alone."""

BEFORE_LIMIT_SETTLE_SECONDS = 5.0
"""How long after the turn end a before-limit move waits before its checks: past the start-up
of the hook a prompt typed at the turn end runs (``UserPromptSubmit``, a process of its own,
1.4 to 1.9 s under load on #423's tester's machine), so its ``prompted_at`` is on the board
before anything is marked or stopped. A prompt typed later still is the pane's to show
(``after_mark``): while its hook runs, Claude Code draws its spinner over an input box that
is not idle."""

_sleep = time.sleep
"""The settle's wait (:data:`BEFORE_LIMIT_SETTLE_SECONDS`), a name of its own for a test to
replace."""

_NOT_MOVED_EARLY = frozenset({team_service.MANAGER_ROLE, team_service.CAPTAIN_ROLE})
"""The roles ``before_limit`` leaves alone. The manager runs the fleet's loop — woken by board
writes, its Stop often kept going — and a move resumes it on another account between two of
its decisions, for no limit hit; the captain is the owner's own. A limit still moves either
(``on_limit``)."""

_WARNED_KEY = "before-limit-warned:{project}:{label}"
"""The ``ui_state`` row saying ``before_limit = "warn"`` has named this crossing of the line for a
label: the slot, and when its windows over the line reset. Re-armed — the row forgotten — when
the account reads under the line, those windows have reset, or the agent runs elsewhere."""


@dataclass(frozen=True)
class _TurnEnd:
    """The turn end a before-limit look started from: when, and how long the transcript was.
    Anything the agent began after it — a prompt stamped (``prompted_at``), a record written
    (``transcripts.activity_since``) — means it is no longer at that safe point."""

    at: datetime
    transcript: Path | None
    offset: int | None

    def dump(self) -> str:
        return json.dumps(
            {
                "at": self.at.isoformat(),
                "transcript": str(self.transcript) if self.transcript else None,
                "offset": self.offset,
            }
        )

    @classmethod
    def load(cls, raw: str | None) -> _TurnEnd | None:
        try:
            data = json.loads(raw) if raw else None
            if not isinstance(data, dict):
                return None
            where = data.get("transcript")
            offset = data.get("offset")
            return cls(
                datetime.fromisoformat(data["at"]),
                Path(where) if isinstance(where, str) and where else None,
                offset if isinstance(offset, int) else None,
            )
        except (ValueError, TypeError, KeyError):
            return None

    @classmethod
    def now(cls, transcript: Path | None) -> _TurnEnd:
        """This moment, with the transcript's length now (``None`` when it cannot be read)."""
        try:
            offset = transcript.stat().st_size if transcript is not None else None
        except OSError:
            offset = None
        return cls(datetime.now(tz=UTC), transcript, offset)

    def begun_since(self, session: TeamSession) -> bool:
        """Whether ``session`` began a turn after this turn end, or is about to: its prompt
        stamped since (``UserPromptSubmit``), or its transcript grown by a prompt, a notice, a
        queued message taken or the agent's own words, or a message queued and not yet taken.
        A typed prompt reaches the transcript only once its hook has passed, so for one the
        stamp comes first; a queued message or a task's notice is on disk at once. No
        transcript or offset to read cannot say it has not: it has."""
        if session.prompted_at is not None and session.prompted_at > self.at:
            return True
        if self.transcript is None or self.offset is None:
            return True
        try:
            return transcripts.activity_since(self.transcript, self.offset)
        except OSError:
            return True


@dataclass(frozen=True)
class _BeforeLimitAgent:
    """The fleet agent a before-limit check is about, read once: idle at its prompt, in a fleet
    no pause holds, not finished for auto-stop, on an account nobody named for it."""

    session: TeamSession
    agent: FleetAgent
    project: ProjectInfo
    slot: int
    family: str | None
    transcript: Path | None


def check_before_limit(session_id: str, *, transcript_path: str | None = None) -> None:
    """``[accounts] before_limit`` at a safe point (F2 of #400): a turn end, or the idle notice.

    ``off``, the default, reads nothing. Otherwise the hook DECIDES WHETHER TO LOOK, from what
    is on record and nothing else — no usage request goes out from a turn end, so a hung
    endpoint never holds one — and a detached worker (``aisquare hook before-limit``,
    :func:`before_limit_worker`) reads what it must and acts: under ``switch`` it hands the
    agent over the way ``on_limit`` hands a limited one (:func:`hand_over`), and the board's
    ``switched`` line says ``(before limit)``; under ``warn`` it puts one ``before_limit`` line
    on the board per crossing of the line. Neither wakes the manager; a move that fails does
    (``handover_failed``).

    The hook looks no further for an agent that is not waiting at its prompt (mid-turn,
    asking, parked, being moved), a hand-typed session, a project ``fleet pause`` holds, a
    finished worker auto-stop is about to stop (:func:`_auto_stop_has_it`), an agent put on
    its account by name (``claude_accounts.account_pin``), one started within
    :data:`HANDOVER_COOLDOWN` or looked at within it, an account the cache reads under the
    line (or over it with every window over it resetting within
    ``wait_if_reset_within_minutes``), a crossing ``warn`` has named, background work its turn
    left running (``transcript.background_running``: an ``/exit`` would kill it), or when the
    cache says no other account is under the line (``claude_accounts.room_on_record``).

    An extra on the turn end, never part of it: it swallows its own failures, said on stderr,
    so a Stop is never reported as lost for it.
    """
    settings = claude_accounts_service.accounts_settings()
    if settings.before_limit == "off" or not orchestrator.team_enabled():
        return
    try:
        found = _worth_a_look(session_id, settings, transcript_path)
        if found is not None:
            _start_the_worker(found, settings)
    except Exception as exc:  # the turn ended all the same; only the early move is lost
        sys.stderr.write(
            f"aisquare: before-limit check failed ({type(exc).__name__}: {exc}) — the agent "
            "was not moved before its limit; run: aisquare doctor\n"
        )


def _key(template: str, agent: FleetAgent) -> str:
    return template.format(project=agent.project_id, label=agent.label)


def _before_limit_agent(
    session_id: str, transcript_path: str | None = None
) -> _BeforeLimitAgent | None:
    """The agent of ``session_id`` when it is one ``before_limit`` may act on, or ``None``."""
    with store_session() as store:
        session = store.get_session(session_id)
        if session is None or session.state != "waiting":
            return None  # mid-turn, parked on a limit, or mid-hand-over: no safe point
        agent = store.fleet_agent_for_session(session.id)  # in its own project, as above
        project = store.get_project(agent.project_id) if agent is not None else None
        if agent is None or project is None:
            return None  # a hand-typed session is the operator's to move
        if harness.base_role(agent.role) in _NOT_MOVED_EARLY:
            return None
        if team_service.fleet_paused(store, agent):
            return None
        if _auto_stop_has_it(store, agent, session):
            return None
    named = agent.launch_spec is not None and agent.launch_spec.account_pinned
    if claude_accounts_service.account_pin(agent.role, named=named) is not None:
        return None
    slot = claude_accounts_service.leaving_slot(agent.account_slot, session.account)
    if slot is None:
        return None  # which account it runs on is not known: there is nothing to read
    return _BeforeLimitAgent(
        session,
        agent,
        project,
        slot,
        harness.model_family(session.model),
        _transcript_of(session, transcript_path),
    )


def _auto_stop_has_it(store: ContextStore, agent: FleetAgent, session: TeamSession) -> bool:
    """Whether auto-stop is about to stop ``agent`` (P2-W1b, #283): a role it stops that has
    finished but for the idle window, as its own Stop asks before it arms the stop
    (``autostop.finished(idle=False)``). Moved, it would take the resume prompt, run a whole
    turn on a fresh account, and be stopped five minutes later. Asked of the board alone: its
    row already reads ``waiting``, and the pane is the move's last check (:func:`_pane_shows`)."""
    from aisquare.services import autostop  # lazy, as in `_arm_auto_stop`

    settings = load_config().fleet
    if not settings.auto_stop_finished:
        return False
    status = FleetAgentStatus(agent=agent, state="waiting", session=session)
    return autostop.finished(status, store, settings, idle=False).finished


def _transcript_of(session: TeamSession, given: str | None) -> Path | None:
    """The transcript the agent writes: the hook's, else the board's (a session resumed under
    another account keeps writing its first one, ``fleet.switch``); ``None`` with neither."""
    for candidate in (given, session.transcript_path):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def _background_work(transcript: Path | None, since: datetime) -> bool:
    """Whether the agent's turn left background work running (a ``run_in_background`` Bash, a
    background subagent) that an ``/exit`` would kill; ``since`` is the start of the process
    running it, its fleet row's. A transcript that cannot be read cannot say it left none: it
    counts as running."""
    if transcript is None:
        return True
    try:
        return bool(transcripts.background_running(transcript, since=since))
    except OSError:
        return True


def _cooling(agent: FleetAgent, stamp: str | None, now: datetime) -> bool:
    """Whether ``agent`` is inside its cool-down: its row started less than
    :data:`HANDOVER_COOLDOWN` ago (a spawn, a restart, a switch: where a launch just put it),
    or ``stamp``, its label's :data:`_BEFORE_LIMIT_KEY` row, is that recent."""
    if now - agent.created_at < HANDOVER_COOLDOWN:
        return True
    looked = _TurnEnd.load(stamp)
    return looked is not None and now - looked.at < HANDOVER_COOLDOWN


def _warned(raw: str | None, slot: int) -> bool:
    """Whether :data:`_WARNED_KEY`'s row ``raw`` names this crossing: the same slot, and its
    windows over the line not reset since."""
    try:
        data = json.loads(raw) if raw else None
        until = datetime.fromisoformat(data["until"]) if data and data.get("until") else None
    except (ValueError, TypeError, KeyError):
        return False
    if not isinstance(data, dict) or data.get("slot") != slot:
        return False
    return until is None or not claude_accounts_service.has_passed(until)


def _worth_a_look(
    session_id: str, settings: AccountsSettings, transcript_path: str | None
) -> _BeforeLimitAgent | None:
    """The hook's half: the agent to start the worker for, its cool-down CLAIMED, or ``None``.
    From what is on record alone; the cheap checks first, the transcript only for an account
    over the line, or one the cache cannot tell."""
    found = _before_limit_agent(session_id, transcript_path)
    if found is None:
        return None
    agent = found.agent
    now = datetime.now(tz=UTC)
    with store_session() as store:
        stamp = store.ui_state(_key(_BEFORE_LIMIT_KEY, agent))
        warned = store.ui_state(_key(_WARNED_KEY, agent))
    if _cooling(agent, stamp, now):
        return None
    line = claude_accounts_service.line_reading(
        found.slot, switch_at=settings.switch_at, family=found.family, on_record=True
    )
    if line is not None and not line.over:
        if warned is not None:  # back under the line: the next crossing is warned again
            with store_session() as store:
                store.replace_ui_state(_key(_WARNED_KEY, agent), warned, None)
        return None
    if line is not None and line.lifts_within(settings.wait_if_reset_within_minutes):
        return None
    if settings.before_limit == "warn" and _warned(warned, found.slot):
        return None
    if _background_work(found.transcript, agent.created_at):
        return None  # its next turn end, once the work has reported, asks again
    turn_end = _TurnEnd.now(found.transcript)
    if turn_end.begun_since(found.session):
        return None  # a message queued during the turn starts the next one now
    if line is not None and not claude_accounts_service.room_on_record(
        found.slot, switch_at=settings.switch_at, family=found.family
    ):
        return None  # every other account is over the line: no read, no worker
    with store_session() as store:
        if not store.replace_ui_state(_key(_BEFORE_LIMIT_KEY, agent), stamp, turn_end.dump()):
            return None  # another hook of this agent claimed it first
    return found


def _start_the_worker(found: _BeforeLimitAgent, settings: AccountsSettings) -> None:
    argv = selfcli.argv_for(["--quiet", "hook", "before-limit", found.session.id])
    try:
        _detach(argv)
    except OSError as exc:
        _give_back(found.agent)
        if settings.before_limit == "switch":
            team_service.hook_handover_failed(
                found.session.project_id,
                f"{found.agent.label}: not moved before its limit — could not start the "
                f"hand-over worker ({exc})",
                session_id=found.session.id,
                label=found.agent.label,
            )


def _give_back(agent: FleetAgent) -> None:
    """Give back the cool-down a look took and did not use: the next turn end may look again."""
    with store_session() as store:
        stamp = store.ui_state(_key(_BEFORE_LIMIT_KEY, agent))
        if stamp is not None:
            store.replace_ui_state(_key(_BEFORE_LIMIT_KEY, agent), stamp, None)


def before_limit_worker(session_id: str) -> None:
    """The detached half of ``[accounts] before_limit`` (``aisquare hook before-limit``).

    Started by a turn end's hook (:func:`check_before_limit`) that found the agent worth a
    look; it asks again what that hook asked, reads what the hook would not (the agent's
    account through the cache, five minutes deep, then the accounts it could go to a minute
    deep, as the move's pick does: ``choose_for_handover(automatic=True)``), and warns or
    moves (:func:`hand_over`). It wakes nobody unless a move that went ahead fails.

    The cool-down the hook claimed is given back when it ends on what the next turn end can
    decide from the cache — the agent busy again, background work started, the account under
    the line or about to reset — and kept when a read failed or no account had room, so a
    failing endpoint or a full fleet is asked at most once a cool-down per agent.
    """
    settings = claude_accounts_service.accounts_settings()
    if settings.before_limit == "off" or not orchestrator.team_enabled():
        return
    found = _before_limit_agent(session_id)
    if found is None:
        with store_session() as store:
            agent = store.fleet_agent_for_session(session_id)
        if agent is not None:
            _give_back(agent)
        return
    agent = found.agent
    if datetime.now(tz=UTC) - agent.created_at < HANDOVER_COOLDOWN:
        return  # started or moved within the cool-down: the hook would not have asked
    if _background_work(found.transcript, agent.created_at):
        _give_back(agent)
        return
    line = claude_accounts_service.line_reading(
        found.slot, switch_at=settings.switch_at, family=found.family
    )
    if line is None:
        return  # no live reading: the cool-down is spent, so a failing read is not retried
    if not line.over or line.lifts_within(settings.wait_if_reset_within_minutes):
        _give_back(agent)
        return
    with store_session() as store:
        warned = store.ui_state(_key(_WARNED_KEY, agent))
    if settings.before_limit == "warn" and _warned(warned, found.slot):
        return
    choice = claude_accounts_service.choose_for_handover(
        role=agent.role,
        project=found.project,
        exclude=(found.slot,),
        automatic=True,
        family=found.family,
        max_age=claude_accounts_service.PICK_MAX_AGE_SECONDS,
    )
    if choice.account is None:
        return  # no account under the line: it carries on where it is, and nobody is told
    if settings.before_limit == "warn":
        _warn(found, line, choice.account, settings, warned)
        return
    with store_session() as store:
        claimed = _TurnEnd.load(store.ui_state(_key(_BEFORE_LIMIT_KEY, agent)))
    hand_over(session_id, before_limit=True, turn_end=claimed or _TurnEnd.now(found.transcript))


def _warn(
    found: _BeforeLimitAgent,
    line: claude_accounts_service.LineReading,
    target: ClaudeAccount,
    settings: AccountsSettings,
    warned: str | None,
) -> None:
    """``warn``'s one line for this crossing, and the row that says it was said."""
    agent = found.agent
    crossing = json.dumps(
        {"slot": found.slot, "until": line.lifts_at.isoformat() if line.lifts_at else None}
    )
    with store_session() as store:
        if not store.replace_ui_state(_key(_WARNED_KEY, agent), warned, crossing):
            return  # another worker said it
    used = claude_accounts_service.describe_headroom(line.account, line.reading, found.family)
    team_service.hook_before_limit(
        found.session.project_id,
        f"{agent.label}: {used} — over the {settings.switch_at}%-used line; "
        f"{claude_accounts_core.label(target)} has room — "
        f"`{project_service.fleet_command('switch', found.project, agent.label)}` moves it "
        '(`before_limit = "warn"`)',
        session_id=found.session.id,
    )


class _StartedATurn(Exception):
    """Raised by a before-limit hand-over's checks: the agent took a prompt meanwhile, or its
    turn left background work. ``prompted``: a prompt it took, which the hand-over mark hid."""

    def __init__(self, session_id: str, *, prompted: bool = False) -> None:
        super().__init__(session_id)
        self.prompted = prompted


def _still_waiting(session_id: str, turn_end: _TurnEnd) -> Callable[[], None]:
    """``fleet.switch``'s ``before_stop`` for a move before the limit, asked once the turn end
    has settled (:data:`BEFORE_LIMIT_SETTLE_SECONDS`): the agent is still one ``before_limit``
    may move — waiting at its prompt, no pause, not finished, no pin
    (:func:`_before_limit_agent`) — has begun nothing since ``turn_end``
    (:meth:`_TurnEnd.begun_since`) and left no background work, or :class:`_StartedATurn`
    refuses before anything is marked or stopped."""

    def check() -> None:
        found = _before_limit_agent(session_id)
        if found is None:
            raise _StartedATurn(session_id)
        if turn_end.begun_since(found.session):
            raise _StartedATurn(session_id, prompted=True)
        if _background_work(found.transcript, found.agent.created_at):
            raise _StartedATurn(session_id)

    return check


def _still_idle_after_mark(session_id: str, turn_end: _TurnEnd) -> Callable[[], None]:
    """``fleet.switch``'s ``after_mark``: asked once the hand-over mark is on, the last check
    before the ``/exit``. The mark hides a turn's ``working`` (``touch_session`` keeps
    ``switching``), not its ``prompted_at`` nor its transcript records, so those are asked
    again; and then THE PANE, which is what closes the window a prompt typed after the settle
    leaves: its record reaches the transcript only once its ``UserPromptSubmit`` hook has
    passed, and its stamp only once that hook's process has started, but from the moment it
    is submitted Claude Code draws its spinner (``· Nucleating… (running UserPromptSubmit hook
    · 1s)``), and the input box is not idle (:func:`_pane_shows`). Only Claude Code idle at its
    prompt passes; a capture that fails or a dialog refuses too, without saying a turn began."""

    def check() -> None:
        with store_session() as store:
            session = store.get_session(session_id)
            agent = store.fleet_agent_for_session(session_id)
        if session is None or agent is None or turn_end.begun_since(session):
            raise _StartedATurn(session_id, prompted=True)
        shown = _pane_shows(agent)
        if shown != "idle":
            raise _StartedATurn(session_id, prompted=shown == "busy")
        if _background_work(_transcript_of(session, None), agent.created_at):
            raise _StartedATurn(session_id)

    return check


def _pane_shows(agent: FleetAgent) -> str:
    """``idle``: Claude Code's input box drawn and idle at its prompt. ``busy``: the box drawn
    under a spinner or ``esc to interrupt`` — a turn, a prompt's hook included. ``unknown``: no
    capture, a dialog, no box; one look, which fails closed to anything but ``idle``."""
    from aisquare.core import claude_screen
    from aisquare.services import fleet as fleet_service  # lazy, as in `hand_over`

    try:
        lines = fleet_service.server_of(agent).capture(agent.pane_id).lines
    except Exception:
        return "unknown"
    if claude_screen.box_idle(lines):
        return "idle"
    return (
        "busy" if claude_screen.input_box_at(claude_screen.tail(lines)) is not None else "unknown"
    )


def hand_over(
    session_id: str,
    *,
    reason: str | None = None,
    before_limit: bool = False,
    turn_end: _TurnEnd | None = None,
) -> None:
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

    ``before_limit`` is the move ``[accounts] before_limit`` makes (F2 of #400), called in
    the worker that decided it (:func:`before_limit_worker`): the reason is ``before limit``,
    the row is the one it read, the pick reads the cache a minute deep, as that worker just
    did, and it is made only while the agent still waits at its prompt with no background
    work — asked before the hand-over mark (``before_stop``) and again after it
    (``after_mark``: its ``prompted_at``, its pane). A turn begun since is left alone and the
    next turn end asks again. Its brakes are silent, as the worker asked them already; a
    refusal from the switch is ``handover_failed``, the one line that wakes the manager.
    """
    with store_session() as store:
        session = store.get_session(session_id)
        if session is None:
            return
        agent = store.fleet_agent_for_session(session.id)  # in its own project, as above
        project = store.get_project(agent.project_id) if agent is not None else None
    if agent is None or project is None:
        return
    if before_limit and session.state != "waiting":
        # In flight or limited: another path has it. At work again since the hook asked: no
        # move now, and the next turn end asks again, the cool-down not spent on it.
        if session.state not in (team_service.HANDOVER_STATE, "limited"):
            _give_back(agent)
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
    if before_limit and since < HANDOVER_COOLDOWN:
        return  # started or moved within the cool-down: the hook would not have asked
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

    not_moved = f"{agent.label}: not switched"
    own_reset = _own_reset(session)
    checks: tuple[Callable[[], None] | None, Callable[[], None] | None] = (None, None)
    if before_limit:
        reason = claude_accounts_core.BEFORE_LIMIT_REASON
        not_moved, own_reset = f"{agent.label}: not moved before its limit", ""
        end = turn_end or _TurnEnd.now(_transcript_of(session, None))
        settled = end.at + timedelta(seconds=BEFORE_LIMIT_SETTLE_SECONDS)
        _sleep(max(0.0, (settled - datetime.now(tz=UTC)).total_seconds()))
        checks = (_still_waiting(session.id, end), _still_idle_after_mark(session.id, end))
    try:
        receipt = fleet_service.switch(
            project,
            agent.label,
            reason=reason,
            spawned_by=HANDOVER_SPAWNER,
            automatic=True,
            agent_id=agent.id if before_limit else None,
            before_stop=checks[0],
            after_mark=checks[1],
            max_age=claude_accounts_service.PICK_MAX_AGE_SECONDS if before_limit else None,
        )
    except _StartedATurn as started:
        # Not a failure: nobody is told, nobody woken. A prompt the mark hid has its turn
        # said, as its own hook would have said it (the mark is taken back to `waiting`).
        if started.prompted:
            with store_session() as store:
                store.touch_session(session.id, state="working")
        _give_back(agent)
        return
    except fleet_service.FleetError as exc:
        # The manager's to act on (#277): the refusal names `fleet switch` or the reset.
        team_service.hook_handover_failed(
            session.project_id,
            f"{not_moved} — {exc}{own_reset}",
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
                f"{not_moved} — hand-over worker failed "
                f"({type(exc).__name__}: {exc}); "
                f"`{project_service.fleet_command('switch', project, agent.label)}` moves it "
                f"by hand{own_reset}",
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
                store.add_prompt(text, project.id, source="claude-code")
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
