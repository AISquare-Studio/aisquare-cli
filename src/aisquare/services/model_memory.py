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
- the built-in ``/model`` or ``/effort`` the owner runs inside any fleet agent, which its
  next turn end finds in Claude Code's own record of the command (:func:`observe`, from the
  Stop hook; :func:`command_value`). A model or effort that merely differs from the last
  turn's is no evidence: a custom command's or a skill's ``model:``/``effort:`` frontmatter
  changes one turn (seq 3609). An agent cannot hand another a ``/model`` or ``/effort``
  (``fleet.setting_command``: ``fleet.tell`` refuses it, and so does the spawn whose first
  message opens with one).

The latest write wins.
"""

from __future__ import annotations

import dataclasses
import json
import re
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
    """What a fleet agent runs now and what its launch ran (``None`` for not known): a
    replay starts ``now`` when it is not ``launched``."""

    now: Pick
    launched: Pick


@dataclasses.dataclass(frozen=True)
class CommandValue:
    """What a recorded ``/model`` or ``/effort`` set (:func:`command_value`)."""

    setting: str
    value: str | None
    """``None``: its answer names no model (``Default (recommended)``), and the next plain
    turn's reading is the value."""
    pick: bool
    """``False`` for a choice "for this session only": the agent runs it, the pick is left."""


#: Claude Code's answer to ``/model``: ``Set model to `Haiku 5.5` and saved as your default
#: for new sessions``, ``… for this session only``, the picker's Esc ``Kept model as `Opus 5.5```.
_MODEL_ANSWER = re.compile(
    r"^(?:Set model to|Kept model as)\s+`?([^`]+?)`?(?:\s+(?:and|for)\b.*)?$", re.S
)
#: Its answer to ``/effort``: ``Set effort level to low (saved as your default …): …``.
_EFFORT_ANSWER = re.compile(r"^Set effort level to\s+`?([A-Za-z]+)`?", re.S)
#: A model's display name (``Sonnet 5.5``), whose id Claude Code spells ``claude-sonnet-5-5``.
_DISPLAY_NAME = re.compile(r"^(Fable|Opus|Sonnet|Haiku)\s+(\d+)(?:\.(\d+))?$")


def command_value(command: transcripts.SettingCommand) -> CommandValue | None:
    """What ``command`` set, ``None`` when it set nothing (``Cancelled``, a refusal): its args
    (``/model haiku``, verbatim), else its answer's (the picker's ``Set model to `Haiku 5.5```
    is ``claude-haiku-5-5``; ``Set effort level to high``), else nothing named, for the next
    plain turn to say. A choice "for this session only" is the agent's, no pick."""
    answer = command.stdout
    pattern = _MODEL_ANSWER if command.setting == "model" else _EFFORT_ANSWER
    answered = pattern.match(answer) if answer is not None else None
    if answer is not None and answered is None:
        return None
    pick = answer is None or "this session only" not in answer
    if command.setting == "model":
        value = harness.clean_model_id(command.args) if command.args else None
        if value is None and answered is not None:
            value = _model_id(answered.group(1).strip())
    else:
        value = harness.clean_effort(command.args) if command.args else None
        if value is None and answered is not None:
            value = harness.clean_effort(answered.group(1))
    return CommandValue(command.setting, value, pick)


def _model_id(display: str) -> str | None:
    """The model id a display name stands for (``Opus 5.5`` → ``claude-opus-5-5``), or the
    name as given when it is one already; ``None`` for one that names none (``Default
    (recommended)``)."""
    named = _DISPLAY_NAME.match(display)
    if named is not None:
        family, major, minor = named.groups()
        return f"claude-{family.lower()}-{major}" + (f"-{minor}" if minor else "")
    return harness.clean_model_id(display) if " " not in display else None


@dataclasses.dataclass(frozen=True)
class _Record:
    """A fleet row's own record of what it runs.

    ``launch`` is what its launch runs: the model its session start reported (else the first
    plain turn's), the effort its own ``--effort`` gave it (else the first plain turn's Stop).
    ``live`` is what a recorded ``/model`` or ``/effort`` moved it to (``None``: the launch's).
    ``transcript`` and ``offset`` are where the next read of its commands starts; ``kinds``
    the last two turns' prompts (``plain`` or ``command``); ``pending`` the commands whose
    value the next plain turn says, with whether each is a pick."""

    launch: Pick = Pick()
    live: Pick = Pick()
    transcript: str | None = None
    offset: int | None = None
    kinds: tuple[str, ...] = ()
    pending: tuple[tuple[str, bool], ...] = ()


def started(
    session_id: str, *, model: str | None, transcript_path: str | None, agent_id: str | None
) -> None:
    """At a fleet agent's session start: the model its launch runs, as the payload reports it
    (``model``), and where its commands start (``transcript_path``, as long as it is now).
    Once per row. Never an effort: Claude Code 2.1.310's SessionStart carries none, and its
    hooks there see only the inherited ``CLAUDE_EFFORT`` (seq 3513). Only for the session the
    row is bound to (:func:`_own_row`). Raises on a store that cannot be read: the caller
    fails open."""
    with store_session() as store:
        agent = _own_row(store, session_id, agent_id)
        if agent is None or _record(agent, store) is not None:
            return
        path = Path(transcript_path) if transcript_path else None
        try:
            offset = path.stat().st_size if path is not None else 0
        except OSError:
            offset = 0
        _keep(
            store,
            agent,
            _Record(
                launch=Pick(harness.clean_model_id(model), _launch_flags(agent).effort),
                transcript=str(path) if path is not None else None,
                offset=offset,
            ),
        )


def launched(agent: FleetAgent, *, store: ContextStore) -> Pick:
    """What ``agent``'s launch runs, as its own record says; empty with none for this row."""
    record = _record(agent, store)
    return record.launch if record is not None else Pick()


def replay_from(agent: FleetAgent, session: TeamSession | None) -> Running | None:
    """What a replay of ``agent`` starts from (``fleet._model_choice``): what it runs, its
    launch's values as moved by a recorded ``/model`` or ``/effort`` and by nothing else (a
    command's ``model:`` frontmatter is one turn's, seq 3609), and what its launch ran.
    ``None`` with no session; fail-open: a store that cannot be read says nothing, and the
    replay starts the recorded flags."""
    if session is None:
        return None
    try:
        with store_session() as store:
            record = _record(agent, store)
    except Exception:  # a replay is never refused for this
        record = None
    if record is None:
        return None
    now = Pick(record.live.model or record.launch.model, record.live.effort or record.launch.effort)
    return Running(now, record.launch)


#: How much of a transcript's tail the first read takes, for a row whose session start was not
#: seen: its commands are not trusted, only its readings.
_FIRST_READ_BYTES = 256_000


def observe(
    session_id: str, transcript_path: str | None, *, effort: str | None, agent_id: str | None
) -> Pick | None:
    """At a fleet agent's turn end: write the pick ONLY on positive evidence, the owner's
    ``/model`` or ``/effort`` as Claude Code records it (``transcripts.commands_since``, read
    forward from where the last turn end stopped), and move what the agent runs (which a
    restart, a switch and a fork start: :func:`replay_from`) by that alone. Returns what was
    written to the pick, ``None`` for nothing.

    A model or an effort that merely differs from the last turn's is no evidence: a custom
    command or a skill with ``model:`` or ``effort:`` frontmatter changes one turn and no
    more (seq 3609), whoever typed it. The value is the command's own (:func:`command_value`:
    its args, else its answer), else, when its answer names none, the next PLAIN turn's
    reading: never a turn another slash command opened, nor the turn after one. A choice
    "for this session only" moves the agent and writes no pick. A turn's readings are the
    latest reply's model in the transcript's tail and the Stop hook's effort (``effort``:
    the payload's ``effort.level``, else ``CLAUDE_EFFORT``); they fill the launch's values a
    session start did not report, and the board's row, and nothing else.

    Only the session the row is bound to speaks for it (:func:`_own_row`): a ``claude -p``
    run from the agent's shell inherits ``AISQUARE_FLEET_AGENT`` and is not the agent. Any
    fleet agent, whatever its role or persona. Cheap enough for every Stop: one store session,
    one forward read of what the turn wrote, one bounded tail read, no subprocess. A
    transcript that cannot be read changes nothing. Raises on a store that cannot be read:
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
        if path is None:
            return None
        previous = _record(agent, store)
        record = previous or _Record(launch=Pick(None, _launch_flags(agent).effort))
        if record.transcript == str(path) and record.offset is not None:
            start = record.offset
        elif record.transcript is not None:
            start = 0  # another transcript (a /clear): read from its start
        else:
            start = max(0, path.stat().st_size - _FIRST_READ_BYTES)
        scan = transcripts.commands_since(path, start)
        live_model = harness.clean_model_id(transcripts.latest_model(path))
        if scan is None or (live_model is None and not scan.events):
            return None
        trusted = previous is not None  # a first read of a tail may hold another row's commands
        live = record.live
        pending = dict(record.pending)
        picks: dict[str, str] = {}
        kinds = record.kinds
        for event in scan.events:
            if isinstance(event, str):
                kinds = (*kinds, event)[-2:]
                continue
            found = command_value(event) if trusted else None
            if found is None:
                continue
            if found.value is None:
                pending[found.setting] = found.pick
                continue
            pending.pop(found.setting, None)
            live = dataclasses.replace(live, **{found.setting: found.value})
            if found.pick:
                picks[found.setting] = found.value
        current = kinds[-1] if kinds else "plain"
        before = kinds[-2] if len(kinds) > 1 else None
        launch = record.launch
        if current == "plain" and before != "command":
            readings = {"model": live_model, "effort": live_effort}
            for setting, pick in list(pending.items()):
                value = readings[setting]
                if value is None:
                    continue
                del pending[setting]
                live = dataclasses.replace(live, **{setting: value})
                if pick:
                    picks[setting] = value
            launch = Pick(
                launch.model or (live_model if live.model is None else None),
                launch.effort or (live_effort if live.effort is None else None),
            )
        _keep(
            store,
            agent,
            _Record(
                launch=launch,
                live=live,
                transcript=str(path),
                offset=scan.end,
                kinds=kinds,
                pending=tuple(pending.items()),
            ),
        )
        if live_model is not None and (live_model, live_effort or session.effort) != (
            session.model,
            session.effort,
        ):
            store.set_session_model(session.id, model=live_model, effort=live_effort)
        if not picks:
            return None
        written = Pick(picks.get("model"), picks.get("effort"))
        remember(model=written.model, effort=written.effort, store=store)
        return written


_LAUNCHED_KEY = "fleet-launched:{project}:{label}"
"""A fleet row's own record (:class:`_Record`), per label (one row holds a label at a time),
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

    def pick(key: str) -> Pick:
        value = data.get(key)
        value = value if isinstance(value, dict) else {}
        model, effort = value.get("model"), value.get("effort")
        return Pick(
            harness.clean_model_id(model) if isinstance(model, str) else None,
            harness.clean_effort(effort) if isinstance(effort, str) else None,
        )

    offset = data.get("offset")
    transcript = data.get("transcript")
    kinds = data.get("kinds")
    pending = data.get("pending")
    return _Record(
        launch=pick("launch"),
        live=pick("live"),
        transcript=transcript if isinstance(transcript, str) else None,
        offset=offset if isinstance(offset, int) and not isinstance(offset, bool) else None,
        kinds=tuple(k for k in kinds if k in ("plain", "command"))
        if isinstance(kinds, list)
        else (),
        pending=tuple(
            (item[0], bool(item[1]))
            for item in pending
            if isinstance(item, list) and len(item) == 2 and item[0] in transcripts.SETTINGS
        )
        if isinstance(pending, list)
        else (),
    )


def _keep(store: ContextStore, agent: FleetAgent, record: _Record) -> None:
    store.set_ui_state(
        _LAUNCHED_KEY.format(project=agent.project_id, label=agent.label),
        json.dumps(
            {
                "agent": agent.id,
                "launch": dataclasses.asdict(record.launch),
                "live": dataclasses.asdict(record.live),
                "transcript": record.transcript,
                "offset": record.offset,
                "kinds": list(record.kinds),
                "pending": [list(item) for item in record.pending],
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
