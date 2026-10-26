"""The model and effort the owner last chose, which every fleet spawn runs.

#338 (v0.9.0) made ``fleet spawn`` pass the role ladder's top rung, so a manager always
started on ``--model fable --effort high`` over the default the owner had saved with
``/model``, and a restart or a switch replayed that pick over a ``/model`` made since. A
spawn now runs the owner's last pick, and nothing when there is none: Claude Code then runs
the account's own default, as before 0.9.0 (``fleet.spawn``).

ONE pick per home (the store's ``ui_state``), for every role and persona: the owner's last
preference, not a role's. A role is customised in its settings alone (``[fleet.roles.<role>]
extra_args``, a binding's ``--arg``), which win over the pick for that role and never write
it. The model and the effort are kept apart: either can be chosen without the other. The
pick is written by

- a spawn that chooses one itself, in its own arguments or the Spawn dialog's fields
  (:func:`remember`, from ``fleet.spawn``); a cleared field forgets it;
- a ``/model`` inside any running fleet agent, as its next turn end reads it
  (:func:`observe`, from the Stop hook).

The latest write wins.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from aisquare.core import harness, transcripts
from aisquare.core.store import ContextStore, store_session
from aisquare.models import FleetAgent, TeamSession

_MODEL_KEY = "fleet-model"
_EFFORT_KEY = "fleet-effort"


@dataclasses.dataclass(frozen=True)
class Pick:
    """A model and an effort, each ``None`` for none: the account's own default."""

    model: str | None = None
    effort: str | None = None


def remembered(*, store: ContextStore | None = None) -> Pick:
    """What a spawn runs when neither it nor its role's settings choose; an empty
    :class:`Pick` with nothing kept, or a store that cannot be read (the account's default
    is never wrong)."""
    if store is None:
        try:
            with store_session() as opened:
                return remembered(store=opened)
        except Exception:  # a pick is a default; a store that cannot say has none
            return Pick()
    return Pick(
        harness.clean_model_id(store.ui_state(_MODEL_KEY)),
        harness.clean_effort(store.ui_state(_EFFORT_KEY)),
    )


def remember(
    *,
    model: str | None = None,
    effort: str | None = None,
    store: ContextStore | None = None,
) -> None:
    """Keep ``model`` and ``effort`` as the pick: ``None`` leaves that one as it is, ``""``
    forgets it (the account's default). A value Claude Code could not have been given
    (``harness.clean_model_id``, ``clean_effort``) is not kept."""
    if model is None and effort is None:
        return
    if store is None:
        with store_session() as opened:
            remember(model=model, effort=effort, store=opened)
        return
    for key, value, clean in (
        (_MODEL_KEY, model, harness.clean_model_id),
        (_EFFORT_KEY, effort, harness.clean_effort),
    ):
        if value is None:
            continue
        if value == "":
            store.set_ui_state(key, None)
        elif clean(value) is not None:
            store.set_ui_state(key, clean(value))


def observe(
    session_id: str, transcript_path: str | None, *, effort: str | None, agent_id: str | None
) -> Pick | None:
    """At a fleet agent's turn end: what it runs now, kept on its board session (which a
    restart, a switch and a fork start: ``fleet.spawn``'s ``running``) and, when that is not
    what it was running, as the pick. Returns what changed, ``None`` for nothing.

    The model is the latest reply's in the transcript (one tail read,
    :func:`transcripts.latest_model`): ``transcript_path``, else the one the session
    recorded (a session resumed under another account keeps writing its first); the effort
    the hook's (``effort``: the payload's ``effort.level``, else ``CLAUDE_EFFORT``). A
    transcript with no reply that can be read says nothing of the turn, and nothing is
    written. What it was running is the session's
    last reading (its start's, or this function's), else the launch's own pick: an alias
    and the id it resolves to are one model (``harness.same_model``), and nothing is
    remembered on a first reading of an agent launched with no pick, which can only be
    the account's default.

    ``agent_id`` is the hook's ``AISQUARE_FLEET_AGENT``; the session's own row is asked when
    it names none. Any fleet agent, whatever its role or persona. Cheap enough for every
    Stop: one store session, one bounded read, no subprocess. Raises on a store that
    cannot be read: the caller fails open.
    """
    live_effort = harness.clean_effort(effort)
    with store_session() as store:
        session = store.get_session(session_id)
        agent = _agent_of(store, session_id, agent_id)
        if session is None or agent is None:
            return None
        path = next(
            (Path(given) for given in (transcript_path, session.transcript_path)
             if given and Path(given).is_file()),
            None,
        )  # fmt: skip
        live_model = harness.clean_model_id(transcripts.latest_model(path)) if path else None
        if live_model is None:
            return None
        was = _running(session, agent)
        changed = Pick(
            live_model
            if was.model is not None and not harness.same_model(live_model, was.model)
            else None,
            live_effort
            if live_effort is not None
            and was.effort is not None
            and not harness.same_effort(live_effort, was.effort)
            else None,
        )
        if (live_model, live_effort or session.effort) != (session.model, session.effort):
            store.set_session_model(session.id, model=live_model, effort=live_effort)
        if changed == Pick():
            return None
        remember(model=changed.model, effort=changed.effort, store=store)
        return changed


def _agent_of(store: ContextStore, session_id: str, agent_id: str | None) -> FleetAgent | None:
    """The live fleet row the hook runs for: ``agent_id``'s, else the session's own."""
    if agent_id:
        agent = store.get_fleet_agent(agent_id)
        if agent is not None and agent.ended_at is None:
            return agent
    return store.fleet_agent_for_session(session_id)


def _running(session: TeamSession, agent: FleetAgent) -> Pick:
    """What the agent ran before this turn end, as far as anything says: the session's last
    reading, else its launch's pick (its arguments, then what the spawn added)."""
    spec = agent.launch_spec
    launched = (
        Pick()
        if spec is None
        else Pick(
            harness.flag_arg(spec.extra_args, "--model") or spec.model,
            harness.flag_arg(spec.extra_args, "--effort") or spec.effort,
        )
    )
    return Pick(session.model or launched.model, session.effort or launched.effort)
