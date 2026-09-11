"""The projection, asserted one derivation at a time.

Every observation here is hand-built and every timestamp is fixed. Nothing in
this file starts a server, opens a store, reaches tmux or reads the wall clock:
the projector is a pure function, so a test states the machine it means and
compares the result to a value it wrote down.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from aisquare.models import (
    FleetAgent,
    FleetAgentStatus,
    ProjectInfo,
    TeamEvent,
    TeamSession,
    TeamTask,
)
from aisquare.office.models import (
    AgentLeftEvent,
    LocalObservationBatch,
    PaneHealth,
    PaneObservation,
    ProjectFrozenEvent,
    PromptEvidence,
    QuestionOption,
)
from aisquare.office.projector import (
    NEUTRAL_MORALE,
    OfficeProjector,
    ProjectionError,
    ProjectionLimits,
    model_family,
)

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
PROJECT = ProjectInfo(id="prj_ab64", root=Path("/code/aisquare-cli"), codename="amber-otter")

CURSOR = "\u276f"
"""The pane's selection marker, escaped rather than pasted so it stays legible."""

PERMISSION_RAW = "\n".join(
    (
        "Do you want to proceed?",
        f"{CURSOR} 1. Yes",
        "  2. Yes, and don't ask again for similar commands",
        "  3. No, and tell Claude what to do differently (esc)",
    )
)

PERMISSION_OPTIONS = (
    QuestionOption(key="allow", label="Yes"),
    QuestionOption(key="allow-remember", label="Yes, and don't ask again", consequence="remembers"),
    QuestionOption(key="deny", label="No, and tell Claude what to do differently (esc)"),
)


def session(
    session_id: str = "ses_nova",
    *,
    state: str = "working",
    last_seen: datetime | None = None,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    model: str | None = "claude-opus-5",
    role: str = "coder",
    focus: str | None = None,
) -> TeamSession:
    return TeamSession(
        id=session_id,
        project_id=PROJECT.id,
        role=role,
        focus=focus,
        started_at=started_at or NOW - timedelta(hours=1),
        last_seen_at=last_seen or NOW,
        ended_at=ended_at,
        state=state,
        model=model,
    )


def fleet_row(
    agent_id: str = "agt_nova",
    *,
    label: str = "nova",
    pane_id: str = "%14",
    session_id: str | None = "ses_nova",
    joined: TeamSession | None = None,
    ended_at: datetime | None = None,
    task_id: str | None = None,
) -> FleetAgentStatus:
    agent = FleetAgent(
        id=agent_id,
        project_id=PROJECT.id,
        label=label,
        role="coder",
        pane_id=pane_id,
        session_id=session_id,
        cwd=PROJECT.root,
        created_at=NOW - timedelta(hours=1),
        ended_at=ended_at,
        task_id=task_id,
    )
    return FleetAgentStatus(agent=agent, state="working", session=joined)


def pane(
    agent_id: str = "agt_nova",
    *,
    health: PaneHealth = "live",
    lines: tuple[str, ...] = ("● Ran 3 shell commands", "  ⏵⏵ auto mode on · ← for agents"),
    observed_at: datetime | None = None,
    exit_status: int | None = None,
) -> PaneObservation:
    return PaneObservation(
        agent_id=agent_id,
        alive=health == "live",
        health=health,
        observed_at=observed_at or NOW,
        lines=lines if health == "live" else (),
        exit_status=exit_status,
    )


def evidence(
    agent_id: str = "agt_nova",
    *,
    generation: int = 1,
    prompt_id: str = "pmt_first",
    stale: bool = False,
    raw: str = PERMISSION_RAW,
    kind: str = "permission",
    detected_by: str = "frame",
    options: tuple[QuestionOption, ...] = PERMISSION_OPTIONS,
) -> PromptEvidence:
    return PromptEvidence(
        agent_id=agent_id,
        provider="claude-code",
        prompt_id=prompt_id,
        generation=generation,
        kind=kind,  # type: ignore[arg-type]
        detected_by=detected_by,  # type: ignore[arg-type]
        observed_at=NOW,
        options=options,
        raw=raw,
        stale=stale,
    )


def batch(
    *,
    fleet: tuple[FleetAgentStatus, ...] = (),
    sessions: tuple[TeamSession, ...] = (),
    panes: tuple[PaneObservation, ...] = (),
    prompts: tuple[PromptEvidence, ...] = (),
    tasks: tuple[TeamTask, ...] = (),
    events: tuple[TeamEvent, ...] = (),
    projects: tuple[ProjectInfo, ...] = (PROJECT,),
    seq: int = 4471,
    partial: bool = False,
    collected_at: datetime | None = None,
) -> LocalObservationBatch:
    return LocalObservationBatch(
        collected_at=collected_at or NOW,
        board_seq=seq,
        projects=projects,
        sessions=sessions,
        tasks=tasks,
        events=events,
        fleet=fleet,
        panes=panes,
        prompts=prompts,
        partial=partial,
    )


def attention_event(
    session_id: str = "ses_nova", *, at: datetime, seq: int = 900, text: str = "needs you"
) -> TeamEvent:
    return TeamEvent(
        seq=seq,
        id=f"evt_{seq}",
        project_id=PROJECT.id,
        session_id=session_id,
        kind="attention",
        text=text,
        created_at=at,
    )


def signal_event(value: str, *, seq: int = 800) -> TeamEvent:
    return TeamEvent(
        seq=seq,
        id=f"evt_{seq}",
        project_id=PROJECT.id,
        session_id="ses_nova",
        kind="signal",
        text=f"fleet-paused: {value}",
        created_at=NOW - timedelta(minutes=5),
    )


# --------------------------------------------------------------------------
# deterministic
# --------------------------------------------------------------------------


def test_deterministic_the_same_inputs_produce_a_byte_equivalent_snapshot() -> None:
    """Referential transparency, asserted on the serialized bytes.

    Compared as wire JSON rather than as objects because that is what a client
    receives: an ordering that differs only inside a tuple would compare equal
    field by field and still reach two browsers as two different documents.
    """
    projector = OfficeProjector()
    observed = batch(
        fleet=(fleet_row(joined=session()), fleet_row("agt_rook", label="rook", pane_id="%21")),
        panes=(pane(), pane("agt_rook")),
        tasks=(task("tsk_1"), task("tsk_0")),
    )

    first = projector.project(observed, None, NOW)
    second = projector.project(observed, None, NOW)

    assert first.snapshot.to_wire() == second.snapshot.to_wire()
    assert [agent.id for agent in first.snapshot.agents] == ["agt_nova", "agt_rook"]
    assert [t.id for t in first.snapshot.tasks] == ["tsk_0", "tsk_1"]


def task(task_id: str, *, claimed_by: str | None = None, status: str = "todo") -> TeamTask:
    return TeamTask(
        id=task_id,
        project_id=PROJECT.id,
        key=task_id,
        title="Checkout intent",
        status=status,
        claimed_by=claimed_by,
        created_at=NOW - timedelta(hours=2),
        updated_at=NOW - timedelta(hours=1),
    )


def test_deterministic_projection_never_consults_a_clock_or_the_environment() -> None:
    """The purity claim, read off the module's own imports.

    An import list is the cheapest place this can be enforced and the only one
    that stays true when someone adds a convenience call three months from now:
    a projector that reached for ``datetime.now()``, a socket or the collector
    would stop being reproducible from its arguments, and the derived states
    would stop being testable at all.
    """
    source = Path(__file__).resolve().parents[2] / "src" / "aisquare" / "office" / "projector.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden = {
        "os",
        "time",
        "socket",
        "sqlite3",
        "subprocess",
        "threading",
        "asyncio",
        "httpx",
        "starlette",
        "aisquare.office.observe",
        "aisquare.office.storage",
        "aisquare.core.tmux",
        "aisquare.core.store",
        "aisquare.services.fleet",
    }
    assert not (imported & forbidden), f"the pure projector imports I/O: {imported & forbidden}"
    assert "aisquare.office.models" in imported


# --------------------------------------------------------------------------
# precedence
# --------------------------------------------------------------------------


def test_precedence_attention_outranks_idle_however_old_the_prompt() -> None:
    """A 71-minute-old unanswered prompt is still attention, still in the queue.

    If idle outranked attention, a prompt older than the 15-minute threshold
    would silently drop out of the queue — the exact failure the product exists
    to prevent.
    """
    parked = session(last_seen=NOW - timedelta(minutes=71), state="attention")
    projection = OfficeProjector().project(
        batch(fleet=(fleet_row(joined=parked),), panes=(pane(),), prompts=(evidence(),)),
        None,
        NOW,
    )

    agent = projection.snapshot.agents[0]
    assert agent.state == "attention"
    assert agent.question is not None
    assert projection.snapshot.queue == ("agt_nova",)


def test_precedence_a_stale_source_cannot_clear_an_active_prompt() -> None:
    """An unreachable pane is evidence about the observer, not about the agent.

    The prompt and the state are both carried forward from the previous
    projection: nothing contradicted them, and a tmux that could not be asked is
    not an answer the user gave.
    """
    projector = OfficeProjector()
    live = projector.project(
        batch(
            fleet=(fleet_row(joined=session(state="attention")),),
            panes=(pane(),),
            prompts=(evidence(),),
        ),
        None,
        NOW,
    )

    later = NOW + timedelta(seconds=30)
    blind = projector.project(
        batch(
            fleet=(fleet_row(joined=session(state="attention", last_seen=later)),),
            panes=(pane(health="unknown"),),
        ),
        live,
        later,
    )

    agent = blind.snapshot.agents[0]
    assert agent.state == "attention"
    assert agent.question is not None
    assert agent.question.prompt_id == "pmt_first"
    assert blind.snapshot.queue == ("agt_nova",)


def test_precedence_a_pane_that_was_read_and_showed_no_dialog_releases_attention() -> None:
    """The other half of the same distinction.

    P03 marks evidence ``stale`` when it read the pane and found no dialog — the
    user answered in the terminal. That is a real observation and it wins over
    the board row, which ``mark_attention`` only ever writes on the transition in
    and cannot retract.
    """
    parked = session(state="attention", last_seen=NOW - timedelta(seconds=30))
    projection = OfficeProjector().project(
        batch(
            fleet=(fleet_row(joined=parked),),
            panes=(pane(),),
            prompts=(evidence(stale=True, raw="needs your attention", options=()),),
        ),
        None,
        NOW,
    )

    agent = projection.snapshot.agents[0]
    assert agent.state == "working"
    assert agent.question is None
    assert projection.snapshot.queue == ()


def test_precedence_a_dead_pane_ends_the_row_and_an_unreachable_one_does_not() -> None:
    """``dead`` is a process that exited; ``unknown`` is a question nobody answered."""
    projector = OfficeProjector()
    working = session(state="working")

    dead = projector.project(
        batch(fleet=(fleet_row(joined=working),), panes=(pane(health="dead", exit_status=1),)),
        None,
        NOW,
    )
    unreachable = projector.project(
        batch(fleet=(fleet_row(joined=working),), panes=(pane(health="unknown"),)),
        None,
        NOW,
    )

    assert dead.snapshot.agents[0].state == "ended"
    assert dead.snapshot.agents[0].ended_reason == "crash"
    assert unreachable.snapshot.agents[0].state != "ended"
    assert unreachable.snapshot.agents[0].health == "unknown"


def test_precedence_waiting_comes_from_the_stop_hook_not_from_a_live_pane() -> None:
    """``working`` is never derived from an alive pane alone."""
    projection = OfficeProjector().project(
        batch(fleet=(fleet_row(joined=session(state="waiting")),), panes=(pane(),)),
        None,
        NOW,
    )

    assert projection.snapshot.agents[0].state == "waiting"
    assert projection.snapshot.queue == ("agt_nova",)


# --------------------------------------------------------------------------
# since_s — the two derivations
# --------------------------------------------------------------------------


def test_a_re_notification_does_not_reset_the_attention_wait() -> None:
    """The measured defect, pinned.

    ``mark_attention`` bumps ``last_seen_at`` on every re-notification and emits
    its event only on the transition, so a wait derived from the column would
    report this 600-second park as 0 and the queue's longest-first ordering would
    degenerate into time-since-last-re-fire.
    """
    projector = OfficeProjector()
    entered = NOW - timedelta(seconds=600)
    first = projector.project(
        batch(
            fleet=(fleet_row(joined=session(state="attention", last_seen=entered)),),
            panes=(pane(),),
            prompts=(evidence(),),
            events=(attention_event(at=entered),),
        ),
        None,
        entered,
    )

    # Re-notified four times since; the column is fresh, the prompt is not new.
    refreshed = projector.project(
        batch(
            fleet=(fleet_row(joined=session(state="attention", last_seen=NOW)),),
            panes=(pane(),),
            prompts=(evidence(),),
            events=(attention_event(at=entered),),
        ),
        first,
        NOW,
    )

    assert refreshed.snapshot.agents[0].since_s == 600
    assert refreshed.snapshot.agents[0].last_seen_at == NOW


def test_the_attention_wait_starts_at_the_transition_event_not_at_the_poll() -> None:
    """With no previous projection, the transition-guarded event is the clock."""
    entered = NOW - timedelta(seconds=420)
    projection = OfficeProjector().project(
        batch(
            fleet=(fleet_row(joined=session(state="attention", last_seen=NOW)),),
            panes=(pane(),),
            prompts=(evidence(),),
            events=(attention_event(at=entered),),
        ),
        None,
        NOW,
    )

    assert projection.snapshot.agents[0].since_s == 420


def test_waiting_counts_from_the_last_seen_the_stop_hook_wrote() -> None:
    """The other derivation, where the plain column is already correct."""
    projection = OfficeProjector().project(
        batch(
            fleet=(
                fleet_row(joined=session(state="waiting", last_seen=NOW - timedelta(seconds=190))),
            ),
            panes=(pane(),),
        ),
        None,
        NOW,
    )

    assert projection.snapshot.agents[0].since_s == 190


def test_since_s_is_zero_in_every_other_state() -> None:
    projection = OfficeProjector().project(
        batch(fleet=(fleet_row(joined=session(state="working")),), panes=(pane(),)), None, NOW
    )

    assert projection.snapshot.agents[0].state == "working"
    assert projection.snapshot.agents[0].since_s == 0


def test_the_queue_orders_attention_first_then_the_longest_wait() -> None:
    old = NOW - timedelta(seconds=600)
    recent = NOW - timedelta(seconds=60)
    projector = OfficeProjector()
    projection = projector.project(
        batch(
            fleet=(
                fleet_row(
                    "agt_a",
                    label="a",
                    pane_id="%1",
                    session_id="ses_a",
                    joined=session("ses_a", state="attention", last_seen=recent),
                ),
                fleet_row(
                    "agt_b",
                    label="b",
                    pane_id="%2",
                    session_id="ses_b",
                    joined=session("ses_b", state="attention", last_seen=old),
                ),
                fleet_row(
                    "agt_c",
                    label="c",
                    pane_id="%3",
                    session_id="ses_c",
                    joined=session("ses_c", state="waiting", last_seen=old),
                ),
            ),
            panes=(pane("agt_a"), pane("agt_b"), pane("agt_c")),
            prompts=(evidence("agt_a"), evidence("agt_b")),
            events=(
                attention_event("ses_a", at=recent, seq=901),
                attention_event("ses_b", at=old, seq=900),
            ),
        ),
        None,
        NOW,
    )

    assert projection.snapshot.queue == ("agt_b", "agt_a", "agt_c")


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------


def test_lifecycle_a_fleet_pane_with_no_session_row_is_starting() -> None:
    """The row exists before hook registration, which is the point of the state."""
    projection = OfficeProjector().project(
        batch(fleet=(fleet_row(session_id=None, joined=None),), panes=(pane(),)), None, NOW
    )

    agent = projection.snapshot.agents[0]
    assert agent.state == "starting"
    assert agent.pane_id == "%14"
    assert projection.snapshot.queue == ()


def test_lifecycle_the_row_id_survives_hook_registration() -> None:
    """The starting row and the registered row are the same row.

    Keyed on the fleet agent id rather than the session id precisely so that
    registration is a state change and not a death and a birth: an id swap here
    would emit ``agent.left`` and ``agent.entered`` for something the user
    watched continuously.
    """
    projector = OfficeProjector()
    starting = projector.project(
        batch(fleet=(fleet_row(session_id=None, joined=None),), panes=(pane(),)), None, NOW
    )
    registered = projector.project(
        batch(fleet=(fleet_row(joined=session()),), panes=(pane(),)), starting, NOW
    )

    assert starting.snapshot.agents[0].id == registered.snapshot.agents[0].id == "agt_nova"
    kinds = [event.kind for event in projector.diff(starting, registered)]
    assert "agent.entered" not in kinds
    assert "agent.left" not in kinds
    assert "agent.state" in kinds


def test_lifecycle_an_ended_row_is_retained_but_never_queued() -> None:
    """Ten minutes of visibility, and no place in a queue nobody can answer."""
    ended_at = NOW - timedelta(minutes=4)
    projection = OfficeProjector().project(
        batch(
            fleet=(fleet_row(joined=session(state="attention", ended_at=ended_at)),),
            panes=(pane(health="dead", exit_status=1),),
            prompts=(evidence(),),
        ),
        None,
        NOW,
    )

    agent = projection.snapshot.agents[0]
    assert agent.state == "ended"
    assert agent.ended_at == ended_at
    assert agent.question is None
    assert projection.snapshot.queue == ()


def test_lifecycle_the_row_is_dropped_after_the_retention_window() -> None:
    ended_at = NOW - timedelta(minutes=11)
    projector = OfficeProjector()
    projection = projector.project(
        batch(fleet=(fleet_row(joined=session(ended_at=ended_at)),)), None, NOW
    )

    assert projection.snapshot.agents == ()


def test_lifecycle_a_pane_that_dies_without_a_session_end_is_a_crash() -> None:
    """A killed agent fires no ``SessionEnd``; the pane is the only witness."""
    projector = OfficeProjector()
    live = projector.project(
        batch(fleet=(fleet_row(joined=session()),), panes=(pane(),)), None, NOW
    )
    later = NOW + timedelta(seconds=5)
    died = projector.project(
        batch(
            fleet=(fleet_row(joined=session(last_seen=NOW)),),
            panes=(pane(health="dead", exit_status=1, observed_at=later),),
        ),
        live,
        later,
    )

    agent = died.snapshot.agents[0]
    assert agent.state == "ended"
    assert agent.ended_reason == "crash"
    assert agent.exit_status == 1
    assert agent.ended_at == later


def test_lifecycle_a_self_session_has_no_pane_and_no_options() -> None:
    """Verbatim option labels live in a pane, and a self session has none."""
    projection = OfficeProjector().project(
        batch(
            sessions=(session("1b070467", state="attention"),),
            prompts=(
                evidence(
                    "1b070467",
                    raw="needs your attention",
                    options=(),
                    detected_by="hook",
                    kind="question",
                ),
            ),
            tasks=(task("tsk_9", claimed_by="1b070467"),),
        ),
        None,
        NOW,
    )

    agent = projection.snapshot.agents[0]
    assert agent.origin == "self"
    assert agent.health == "unknown"
    assert agent.pane_id is None
    assert agent.task_id == "tsk_9"
    assert agent.question is not None
    assert agent.question.options is None


# --------------------------------------------------------------------------
# prompt
# --------------------------------------------------------------------------


def test_prompt_the_same_text_in_a_new_generation_gets_a_new_id() -> None:
    """A second ``rm -rf`` looks identical to the first and is not the first."""
    projector = OfficeProjector()
    parked = session(state="attention")
    first = projector.project(
        batch(fleet=(fleet_row(joined=parked),), panes=(pane(),), prompts=(evidence(),)), None, NOW
    )
    replaced = projector.project(
        batch(
            fleet=(fleet_row(joined=parked),),
            panes=(pane(),),
            prompts=(evidence(generation=2, prompt_id="pmt_second"),),
        ),
        first,
        NOW,
    )

    assert first.snapshot.agents[0].question is not None
    assert replaced.snapshot.agents[0].question is not None
    assert first.snapshot.agents[0].question.prompt_id == "pmt_first"
    assert replaced.snapshot.agents[0].question.prompt_id == "pmt_second"
    assert [e.kind for e in projector.diff(first, replaced)] == ["prompt.changed"]


def test_prompt_unchanged_evidence_keeps_its_id_and_emits_nothing() -> None:
    projector = OfficeProjector()
    parked = session(state="attention")
    observed = batch(fleet=(fleet_row(joined=parked),), panes=(pane(),), prompts=(evidence(),))
    first = projector.project(observed, None, NOW)
    again = projector.project(observed, first, NOW)

    assert again.snapshot.agents[0].question is not None
    assert again.snapshot.agents[0].question.prompt_id == "pmt_first"
    assert projector.diff(first, again) == ()


def test_prompt_a_permission_question_carries_no_tool_and_no_summary() -> None:
    """The contract-versus-reality gap, asserted on the wire.

    ``snapshot.json``'s prose assumes a hook carrying ``tool`` and ``summary``.
    This CLI installs five hooks and none of them carries either, and the frame
    that does carry the options does not name the tool. Absent is the honest
    answer; ``""`` would read as a known-empty value.
    """
    projection = OfficeProjector().project(
        batch(
            fleet=(fleet_row(joined=session(state="attention")),),
            panes=(pane(),),
            prompts=(evidence(),),
        ),
        None,
        NOW,
    )

    question = projection.snapshot.agents[0].question
    assert question is not None
    wire = question.to_wire()
    assert "tool" not in wire
    assert "summary" not in wire
    assert wire["text"] == "Do you want to proceed?"
    options = wire["options"]
    assert isinstance(options, list)
    assert [cast(dict[str, Any], option)["key"] for option in options] == [
        "allow",
        "allow-remember",
        "deny",
    ]
    assert wire["deny_reason"] is True


def test_prompt_options_are_never_synthesised_when_the_evidence_has_none() -> None:
    projection = OfficeProjector().project(
        batch(
            fleet=(fleet_row(joined=session(state="attention")),),
            panes=(pane(),),
            prompts=(evidence(options=(), raw="needs your attention", kind="question"),),
        ),
        None,
        NOW,
    )

    question = projection.snapshot.agents[0].question
    assert question is not None
    assert question.options is None
    assert "deny_reason" not in question.to_wire()


# --------------------------------------------------------------------------
# freeze
# --------------------------------------------------------------------------


def test_freeze_is_a_hiring_freeze_and_leaves_live_agents_working() -> None:
    projection = OfficeProjector().project(
        batch(
            fleet=(fleet_row(joined=session()),),
            panes=(pane(),),
            events=(signal_event("on"),),
        ),
        None,
        NOW,
    )

    assert projection.snapshot.projects[0].frozen is True
    assert projection.snapshot.agents[0].state == "working"
    assert projection.snapshot.agents[0].pause is None


def test_freeze_stays_unknown_when_no_signal_is_in_the_observed_window() -> None:
    """Absent, not false: "not observed" and "observed not frozen" differ."""
    projection = OfficeProjector().project(batch(), None, NOW)

    assert projection.snapshot.projects[0].frozen is None
    assert "frozen" not in projection.snapshot.projects[0].to_wire()


def test_freeze_changes_emit_one_project_event() -> None:
    projector = OfficeProjector()
    before = projector.project(batch(events=(signal_event("off", seq=799),)), None, NOW)
    after = projector.project(batch(events=(signal_event("on", seq=800),)), before, NOW)

    events = projector.diff(before, after)
    assert [event.kind for event in events] == ["project.frozen"]
    frozen = events[0]
    assert isinstance(frozen, ProjectFrozenEvent)
    assert frozen.frozen is True


# --------------------------------------------------------------------------
# unknown
# --------------------------------------------------------------------------


def test_unknown_cost_usage_and_worktree_facts_are_absent_not_zero() -> None:
    """P10 owns cost. A zero here would be a measurement nobody made."""
    projection = OfficeProjector().project(
        batch(fleet=(fleet_row(joined=session()),), panes=(pane(),)), None, NOW
    )

    agent = projection.snapshot.agents[0]
    assert agent.cost is None
    assert agent.stats is None
    assert agent.worktree is None
    assert "cost" not in projection.snapshot.to_wire()


def test_unknown_model_reports_family_other_and_never_guesses() -> None:
    assert model_family(None) == "other"
    assert model_family("gpt-4o") == "other"
    assert model_family("claude-opus-5") == "opus"
    assert model_family("claude-haiku-4-5-20251001") == "haiku"


def test_unknown_source_marks_the_snapshot_stale_rather_than_thinning_it() -> None:
    """A partial collection is reported, never quietly served as complete."""
    projection = OfficeProjector().project(
        batch(fleet=(fleet_row(joined=session()),), panes=(pane(),), partial=True), None, NOW
    )

    assert projection.snapshot.stale is True
    assert len(projection.snapshot.agents) == 1


def test_unknown_permission_mode_is_reported_as_unknown() -> None:
    projection = OfficeProjector().project(
        batch(fleet=(fleet_row(joined=session()),), panes=(pane(lines=("working",)),)), None, NOW
    )

    assert projection.snapshot.agents[0].permission_mode == "unknown"


def test_morale_is_the_neutral_default_because_nothing_records_feedback() -> None:
    """No gift, whip or morale column exists in this CLI. 50 is the contract's
    neutral, emitted as a required field's safe default rather than a claim."""
    projection = OfficeProjector().project(
        batch(fleet=(fleet_row(joined=session()),), panes=(pane(),)), None, NOW
    )

    assert projection.snapshot.agents[0].morale == NEUTRAL_MORALE


# --------------------------------------------------------------------------
# order
# --------------------------------------------------------------------------


def test_order_entry_precedes_every_other_event_for_a_new_agent() -> None:
    projector = OfficeProjector()
    before = projector.project(batch(), None, NOW)
    after = projector.project(
        batch(
            fleet=(fleet_row(joined=session(state="attention")),),
            panes=(pane(),),
            prompts=(evidence(),),
        ),
        before,
        NOW,
    )

    kinds = [event.kind for event in projector.diff(before, after)]
    assert kinds[0] == "agent.entered"
    assert kinds == ["agent.entered", "queue.changed"]


def test_order_queue_follows_state_and_ended_and_left_come_last() -> None:
    """§6's ordering, asserted as one sequence rather than as four rules."""
    projector = OfficeProjector()
    parked = session(state="attention")
    before = projector.project(
        batch(
            fleet=(
                fleet_row(joined=parked),
                fleet_row(
                    "agt_old",
                    label="old",
                    pane_id="%9",
                    session_id="ses_old",
                    joined=session("ses_old"),
                ),
            ),
            panes=(pane(), pane("agt_old")),
            prompts=(evidence(),),
        ),
        None,
        NOW,
    )
    after = projector.project(
        batch(
            fleet=(
                fleet_row(joined=session(state="working")),
                fleet_row(
                    "agt_old",
                    label="old",
                    pane_id="%9",
                    session_id="ses_old",
                    joined=session("ses_old", ended_at=NOW - timedelta(minutes=1)),
                ),
            ),
            panes=(pane(), pane("agt_old", health="dead", exit_status=0)),
        ),
        before,
        NOW,
    )

    kinds = [event.kind for event in projector.diff(before, after)]
    assert kinds.index("agent.state") < kinds.index("queue.changed")
    assert kinds.index("agent.health") < kinds.index("agent.ended")
    assert kinds[-1] == "agent.ended"


def test_order_a_dropped_row_ends_with_agent_left() -> None:
    projector = OfficeProjector()
    before = projector.project(
        batch(fleet=(fleet_row(joined=session(ended_at=NOW - timedelta(minutes=9))),)), None, NOW
    )
    after = projector.project(
        batch(fleet=(fleet_row(joined=session(ended_at=NOW - timedelta(minutes=11))),)),
        before,
        NOW,
    )

    events = projector.diff(before, after)
    assert [event.kind for event in events] == ["agent.left"]
    left = events[0]
    assert isinstance(left, AgentLeftEvent)
    assert left.reason == "ended"


def test_the_initial_diff_is_empty_by_explicit_policy() -> None:
    """P05 sends a complete snapshot to a new subscriber.

    Synthesising an ``agent.entered`` per pre-existing row would arrive as a
    second, contradictory description of the same instant. Pinned here because
    the packet requires the choice to be a decision rather than an accident.
    """
    projector = OfficeProjector()
    first = projector.project(
        batch(fleet=(fleet_row(joined=session()),), panes=(pane(),)), None, NOW
    )

    assert projector.diff(None, first) == ()


# --------------------------------------------------------------------------
# seq
# --------------------------------------------------------------------------


def test_seq_a_pane_only_change_emits_events_with_the_board_sequence_unchanged() -> None:
    """The event that most often fires while the store stands still.

    A differ keyed on ``seq`` would drop exactly this. Duplicate public SSE ids
    are legitimate; what advanced is the private generation.
    """
    projector = OfficeProjector()
    before = projector.project(
        batch(fleet=(fleet_row(joined=session()),), panes=(pane(lines=("working",)),)), None, NOW
    )
    after = projector.project(
        batch(
            fleet=(fleet_row(joined=session()),),
            panes=(
                pane(
                    lines=("working", "  ⏵⏵ plan mode on"),
                ),
            ),
        ),
        before,
        NOW,
    )

    assert before.snapshot.seq == after.snapshot.seq == 4471
    kinds = [event.kind for event in projector.diff(before, after)]
    assert "agent.mode" in kinds
    assert "agent.output" in kinds
    assert after.generation > before.generation


def test_seq_is_the_board_sequence_and_is_never_manufactured() -> None:
    projection = OfficeProjector().project(batch(seq=5120), None, NOW)

    assert projection.snapshot.seq == 5120


# --------------------------------------------------------------------------
# failure and edge handling
# --------------------------------------------------------------------------


def test_malformed_identity_is_a_typed_projection_error() -> None:
    projector = OfficeProjector()

    with pytest.raises(ProjectionError):
        projector.project(
            batch(projects=(ProjectInfo(id="prj_rel", root=Path("relative/path")),)), None, NOW
        )

    with pytest.raises(ProjectionError):
        projector.project(batch(), None, datetime(2026, 9, 11, 12, 0))  # naive


def test_clock_skew_clamps_an_age_to_zero_without_moving_the_timestamp() -> None:
    """A corrected system clock must not produce a negative ``up_s``."""
    started = NOW + timedelta(minutes=5)
    projection = OfficeProjector().project(
        batch(sessions=(session(started_at=started, last_seen=started),)), None, NOW
    )

    agent = projection.snapshot.agents[0]
    assert agent.up_s == 0
    assert agent.last_seen_at == started


def test_bounds_are_applied_before_serialization() -> None:
    projector = OfficeProjector(ProjectionLimits(max_output_tail=2))
    projection = projector.project(
        batch(fleet=(fleet_row(joined=session()),), panes=(pane(lines=("a", "b", "c", "d")),)),
        None,
        NOW,
    )

    assert projection.snapshot.agents[0].output_tail == ("c", "d")
