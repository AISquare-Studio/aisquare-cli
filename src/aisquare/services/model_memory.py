"""The model and effort the owner last chose, which every fleet spawn runs.

#338 (v0.9.0) made ``fleet spawn`` pass the role ladder's top rung, so a manager always
started on ``--model fable --effort high`` over the default the owner had saved with
``/model``, and a restart or a switch replayed that pick over a ``/model`` made since. A
spawn now runs the owner's last pick, and nothing when there is none: Claude Code then runs
the account's own default, as before 0.9.0 (``fleet.spawn``).

ONE pick per home (the store's ``ui_state``), for every role and persona: the owner's last
preference, not a role's. A role is customised in its settings alone (``[fleet.roles.<role>]
extra_args``, a binding's ``--arg``), which win over the pick for that role and never write
it. The model and the effort are kept apart: either can be chosen without the other. Only
the owner's choices write the pick (manager decision, board seq 3428):

- an owner's new spawn that chooses one, in its own arguments or the Spawn dialog's fields
  (:func:`remember`, from ``fleet.spawn``; ``fleet._the_owners_spawn``); a cleared field
  forgets it, unless the role's settings pin that flag. A spawn an agent makes, and every
  restart, switch, hand-over and fork, runs its flags for that agent alone;
- a ``/model`` inside any running fleet agent, as its next turn end reads it
  (:func:`observe`, from the Stop hook). An agent cannot type one into another
  (``fleet.tell`` refuses it).

The latest write wins.
"""

from __future__ import annotations

import dataclasses
import json
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


@dataclasses.dataclass(frozen=True)
class Running:
    """What a fleet agent runs now and what its launch ran, as its session reported each
    (``None`` for not reported): a replay starts ``now`` when it is not ``launched``."""

    now: Pick
    launched: Pick


@dataclasses.dataclass(frozen=True)
class _Record:
    """A fleet row's own readings: what its launch ran (``launched``: the model its session
    start reported, the effort its first Stop did, or the launch's own ``--effort``), and
    the effort its last Stop read (``last_effort``; ``stopped`` once one did)."""

    launched: Pick = Pick()
    stopped: bool = False
    last_effort: str | None = None


def started(session_id: str, *, model: str | None, agent_id: str | None) -> None:
    """At a fleet agent's session start: the model its launch runs, as the payload reports it
    (``model``). Kept for the row, once: the id an alias like ``opus`` resolved to, and the
    account's own default for an agent launched with no pick, which a replay compares what
    it runs later with (:func:`replay_from`). Never an effort: Claude Code 2.1.310's
    SessionStart carries none, and its hooks there see only the process's inherited
    ``CLAUDE_EFFORT``, a fleet window's being the tmux server's (seq 3513); the first Stop
    reports it (:func:`observe`). Only for the session the row is bound to (:func:`_own_row`).
    Raises on a store that cannot be read: the caller fails open."""
    with store_session() as store:
        agent = _own_row(store, session_id, agent_id)
        if agent is None or _record(agent, store) is not None:
            return
        _keep(store, agent, _Record(Pick(harness.clean_model_id(model), None)))


def launched(agent: FleetAgent, *, store: ContextStore) -> Pick:
    """What ``agent``'s launch ran, as its session reported it; empty when nothing was
    reported for this row (the label's record is another row's)."""
    record = _record(agent, store)
    return record.launched if record is not None else Pick()


def replay_from(agent: FleetAgent, session: TeamSession | None) -> Running | None:
    """What a replay of ``agent`` starts from (``fleet._model_choice``): the model and
    effort it runs now, its hooks' last reading (the effort a Stop's alone), and what its
    launch ran. ``None`` with no session; fail-open: a store that cannot be read says nothing
    was read, and the replay compares with the recorded flags."""
    if session is None:
        return None
    try:
        with store_session() as store:
            record = _record(agent, store)
    except Exception:  # a replay is never refused for this
        record = None
    effort = record.last_effort if record is not None and record.stopped else None
    first = record.launched if record is not None else Pick()
    return Running(Pick(session.model, effort), first)


def observe(
    session_id: str, transcript_path: str | None, *, effort: str | None, agent_id: str | None
) -> Pick | None:
    """At a fleet agent's turn end: what it runs now, kept on its board session (a restart,
    a switch and a fork start it: :func:`replay_from`) and, when that is a model or effort
    other than it ran before, as the pick: a ``/model``. Returns what changed, ``None`` for
    nothing.

    The model is the latest reply's in the transcript (one tail read,
    :func:`transcripts.latest_model`): ``transcript_path``, else the one the session
    recorded (a session resumed under another account keeps writing its first); the effort
    the Stop hook's (``effort``: the payload's ``effort.level``, else ``CLAUDE_EFFORT``,
    which Claude Code sets for its Stop). A transcript with no reply that can be read says
    nothing of the turn, and nothing is written.

    The model it ran before is the session's last reading (its start's or a turn end's; else
    what its launch reported, :func:`started`): another id is a ``/model``, and only the
    very id is the same model (``harness.same_reading``). With no reading before, the
    launch's flag decides (``harness.ran_another``): nothing is a pick on the first reading
    of an agent launched with no pick, which can only be the account's default. An agent
    launched on a mode alias (``opusplan``) runs two models by mode: a reply of either
    (``harness.runs_under``) is no pick, one of another family is.

    The effort it ran before is the last Stop's; before its first Stop, the launch's own
    ``--effort``, and with none, nothing: the first Stop's effort is the baseline, and a
    change made inside the very first turn of an agent launched with no ``--effort`` is not
    remembered (no session start reports an effort, seq 3513); a later one is.

    Only the session the row is bound to speaks for it (:func:`_own_row`): a ``claude -p``
    run from the agent's shell inherits ``AISQUARE_FLEET_AGENT`` and is no ``/model`` of the
    agent's. Any fleet agent, whatever its role or persona. Cheap enough for every Stop: one
    store session, one bounded read, no subprocess. Raises on a store that cannot be read:
    the caller fails open.
    """
    live_effort = harness.clean_effort(effort)
    with store_session() as store:
        session = store.get_session(session_id)
        agent = _own_row(store, session_id, agent_id)
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
        flags = _launch_flags(agent)
        record = _record(agent, store) or _Record()
        was_model = session.model or record.launched.model
        if harness.runs_under(flags.model, live_model) and (
            was_model is None or harness.runs_under(flags.model, was_model)
        ):
            new_model = False  # a reply of the mode alias it was launched on (opusplan)
        elif was_model is not None:
            new_model = not harness.same_reading(live_model, was_model)
        else:
            new_model = harness.ran_another(live_model, flags.model)
        was_effort = record.last_effort if record.stopped else flags.effort
        new_effort = (
            live_effort is not None
            and was_effort is not None
            and not harness.same_effort(live_effort, was_effort)
        )
        launch = record.launched
        if not record.stopped:
            # This row's first Stop: what its launch ran, unless this reading is already
            # another model or effort than the launch's (then that stays unknown, and a
            # replay asks the launch's flag instead).
            launch = Pick(
                launch.model or was_model or (None if new_model else live_model),
                flags.effort or (None if new_effort else live_effort),
            )
        _keep(
            store,
            agent,
            _Record(launch, stopped=True, last_effort=live_effort or record.last_effort),
        )
        if (live_model, live_effort or session.effort) != (session.model, session.effort):
            store.set_session_model(session.id, model=live_model, effort=live_effort)
        changed = Pick(live_model if new_model else None, live_effort if new_effort else None)
        if changed == Pick():
            return None
        remember(model=changed.model, effort=changed.effort, store=store)
        return changed


_LAUNCHED_KEY = "fleet-launched:{project}:{label}"
"""A fleet row's own readings (:class:`_Record`), per label (one row holds a label at a time),
with the id of the row it is for. Never deleted: a follow-up of #441."""


def _record(agent: FleetAgent, store: ContextStore) -> _Record | None:
    """``agent``'s :class:`_Record`, ``None`` when this row has none yet (the label's record is
    another row's, or nothing was read)."""
    raw = store.ui_state(_LAUNCHED_KEY.format(project=agent.project_id, label=agent.label))
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("agent") != agent.id:
        return None

    def text(key: str) -> str | None:
        value = data.get(key)
        return value if isinstance(value, str) else None

    return _Record(
        Pick(harness.clean_model_id(text("model")), harness.clean_effort(text("effort"))),
        stopped=data.get("stopped") is True,
        last_effort=harness.clean_effort(text("last_effort")),
    )


def _keep(store: ContextStore, agent: FleetAgent, record: _Record) -> None:
    store.set_ui_state(
        _LAUNCHED_KEY.format(project=agent.project_id, label=agent.label),
        json.dumps(
            {
                "agent": agent.id,
                "model": record.launched.model,
                "effort": record.launched.effort,
                "stopped": record.stopped,
                "last_effort": record.last_effort,
            }
        ),
    )


def _own_row(store: ContextStore, session_id: str, agent_id: str | None) -> FleetAgent | None:
    """The live fleet row the session runs as, as ``team._fleet_row`` finds it: the row
    ``agent_id`` (the hook's ``AISQUARE_FLEET_AGENT``) names when it is bound to this very
    session, else the row bound to the session. A nested ``claude -p`` inherits the name
    and is bound to nothing: it gets no row."""
    if agent_id:
        named = store.get_fleet_agent(agent_id)
        if named is not None and named.ended_at is None and named.session_id == session_id:
            return named
    return store.fleet_agent_for_session(session_id)


def chosen_models(session: TeamSession) -> list[str]:
    """The models the owner chose for ``session``'s agent, which the board's ``⚠ off-ladder``
    chip stands down for (#16, review of #441, 6): the pick, the agent's own launch flags
    (its spawn's args, its role's ``extra_args`` as recorded, or what a replay started) and
    its role's settings (``[fleet.roles.<role>] extra_args``, a binding's ``--arg``).
    Fail-open: what cannot be read chose nothing, and the chip says what the ladder says."""
    chosen: list[str | None] = []
    try:
        with store_session() as store:
            agent = store.fleet_agent_for_session(session.id)
            chosen += [remembered(store=store).model, _launch_flags(agent).model if agent else None]
        from aisquare.core.config import load_config  # config imports harness back

        roles = load_config().fleet.roles
        settings = roles.get(session.role) or roles.get(harness.base_role(session.role))
        own = settings.extra_args if settings is not None else []
        chosen.append(harness.model_arg([*own, *harness.binding_args(session.role)]))
    except Exception:  # an advisory chip: what cannot be read chose nothing
        pass
    return [model for model in chosen if model is not None]


def _launch_flags(agent: FleetAgent) -> Pick:
    """The ``--model`` and ``--effort`` the agent was launched with: its arguments', else what
    the spawn added beside them."""
    spec = agent.launch_spec
    if spec is None:
        return Pick()
    return Pick(
        harness.model_arg(spec.extra_args) or spec.model,
        harness.flag_arg(spec.extra_args, "--effort") or spec.effort,
    )
