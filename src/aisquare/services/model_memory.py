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
import sys
from datetime import UTC, datetime
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
    """The value; ``""`` the account's own default (the picker's ``Default (recommended)``,
    which forgets the pick); ``None`` nothing (an answer this reader cannot read, and no
    args)."""
    pick: bool
    """``False`` for a choice "for this session only": the agent runs it, the pick is left."""
    understood: bool = True
    """``False`` when Claude Code's answer is in words this reader does not know: said once
    (:func:`observe`) and shown by doctor, as a reworded answer must not fail silently."""


#: Claude Code's answer to ``/model``: ``Set model to `Haiku 5.5` and saved as your default for
#: new sessions``, ``… for this session only``; ``Default (recommended)`` answers ``Set model to
#: `Opus 5.5 (default)` …``. The picker's Esc, ``Kept model as …``, sets nothing.
_MODEL_ANSWER = re.compile(r"^Set model to\s+`([^`]+)`(.*)$", re.S)
#: Its answer to ``/effort``: ``Set effort level to low (saved as your default …): …``, or
#: ``(this session only)``. The picker's Esc, ``Cancelled``, sets nothing.
_EFFORT_ANSWER = re.compile(r"^Set effort level to\s+([A-Za-z]+)\b(.*)$", re.S)
#: Answers that set nothing: the pickers' Esc.
_NO_CHOICE = ("Kept model as", "Cancelled")
#: A model's display name (``Sonnet 5.5``), whose id Claude Code spells ``claude-sonnet-5-5``.
_DISPLAY_NAME = re.compile(r"^(Fable|Opus|Sonnet|Haiku)\s+(\d+)(?:\.(\d+))?$")


def command_value(command: transcripts.SettingCommand) -> CommandValue | None:
    """What ``command`` set, ``None`` when it set nothing (the pickers' Esc: ``Kept model as``,
    ``Cancelled``; opening a picker to look is no choice, seq 3628). The value is its args
    (``/model haiku``, verbatim), else the answer's (the picker's ``Set model to `Haiku 5.5```
    is ``claude-haiku-5-5``; ``Set effort level to high``); the picker's ``Default
    (recommended)`` is ``""``, the account's default, which forgets the pick. A choice "for this
    session only" is the agent's, no pick. An answer in other words is not understood: its
    explicit args still count, and nothing else does."""
    answer = command.stdout
    if answer.startswith(_NO_CHOICE):
        return None
    args = command.args.strip()
    if command.setting == "model":
        answered = _MODEL_ANSWER.match(answer)
        if answered is None:
            return CommandValue("model", harness.clean_model_id(args), True, understood=False)
        named, rest = answered.group(1).strip(), answered.group(2)
        if named.endswith("(default)") or args.lower() == "default":
            value: str | None = ""
        else:
            value = harness.clean_model_id(args) if args else _model_id(named)
        return CommandValue("model", value, "this session only" not in rest, value is not None)
    answered = _EFFORT_ANSWER.match(answer)
    if answered is None:
        return CommandValue("effort", harness.clean_effort(args), True, understood=False)
    value = harness.clean_effort(args) if args else harness.clean_effort(answered.group(1))
    return CommandValue(
        "effort", value, "this session only" not in answered.group(2), value is not None
    )


def _model_id(display: str) -> str | None:
    """The model id a display name stands for (``Opus 5.5`` → ``claude-opus-5-5``), or the
    name as given when it is one already; ``None`` for one it cannot tell."""
    named = _DISPLAY_NAME.match(display)
    if named is not None:
        family, major, minor = named.groups()
        return f"claude-{family.lower()}-{major}" + (f"-{minor}" if minor else "")
    return harness.clean_model_id(display) if " " not in display else None


@dataclasses.dataclass(frozen=True)
class _Record:
    """A fleet row's own record of what it runs.

    ``launch`` is what its launch runs: the model its session start reported (else its first
    plain turn's), the effort its own ``--effort`` gave it (else its first plain turn's Stop).
    ``live`` is what a recorded ``/model`` or ``/effort`` moved it to (``None``: the launch's;
    ``""``: the account's default). ``transcript`` and ``offset`` are where the next read of
    its commands starts, ``turn`` the kind of the turn read last (``plain`` or ``command``)."""

    launch: Pick = Pick()
    live: Pick = Pick()
    transcript: str | None = None
    offset: int = 0
    turn: str | None = None


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
        if agent is not None and _record(agent, store) is None:
            _first_record(store, agent, transcript_path, model=model)


def _first_record(
    store: ContextStore, agent: FleetAgent, transcript: str | Path | None, *, model: str | None
) -> None:
    """A row's record, first: its launch's values as known now, its commands read from the
    transcript's end on (nothing written before is this row's)."""
    path = Path(transcript) if transcript else None
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


def replay_from(agent: FleetAgent, session: TeamSession | None) -> Running | None:
    """What a replay of ``agent`` starts from (``fleet._model_choice``): what it runs, its
    launch's values as moved by a recorded ``/model`` or ``/effort`` and by nothing else (a
    command's ``model:`` frontmatter is one turn's, seq 3609), and what its launch ran.
    ``None`` with no session or no record; fail-open: a store that cannot be read says
    nothing, and the replay starts the recorded flags."""
    if session is None:
        return None
    try:
        with store_session() as store:
            record = _record(agent, store)
    except Exception:  # a replay is never refused for this
        record = None
    if record is None:
        return None
    live, launch = record.live, record.launch
    now = Pick(
        live.model if live.model is not None else launch.model,
        live.effort if live.effort is not None else launch.effort,
    )
    return Running(now, launch)


def observe(
    session_id: str, transcript_path: str | None, *, effort: str | None, agent_id: str | None
) -> Pick | None:
    """At a fleet agent's turn end: write the pick ONLY on positive evidence, the owner's
    ``/model`` or ``/effort`` in Claude Code's own frame (``transcripts.commands_since``, read
    forward from the last turn end), and move what the agent runs (which a restart, a switch
    and a fork start: :func:`replay_from`) by that alone. Returns what was written to the
    pick, ``None`` for nothing.

    A model or an effort that merely differs from the last turn's is no evidence: a custom
    command's or a skill's ``model:``/``effort:`` frontmatter changes one turn and no more
    (seq 3609), whoever typed it; nor is text that quotes a command, which cannot make the
    frame (seq 3628). The value is the command's (:func:`command_value`); "this session only"
    moves the agent and writes no pick; ``Default (recommended)`` forgets the model pick.
    An answer this reader cannot read is said once on stderr and kept for doctor
    (:func:`unread`). A plain turn's readings (the latest reply's model, the Stop's effort:
    ``effort``, the payload's ``effort.level``, else ``CLAUDE_EFFORT``) fill the launch's
    values a session start did not report, and the board's row, and nothing else.

    The first time a row is seen with no session start read, its record begins at the
    transcript's end: nothing written before is its own. Only the session the row is bound to
    speaks for it (:func:`_own_row`): a ``claude -p`` run from the agent's shell inherits
    ``AISQUARE_FLEET_AGENT`` and is not the agent. Cheap enough for every Stop: one store
    session, one forward read of what the turn wrote, one bounded tail read, no subprocess.
    A transcript that cannot be read changes nothing. Raises on a store that cannot be read:
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
        live_model = harness.clean_model_id(transcripts.latest_model(path))
        record = _record(agent, store)
        if record is None:
            _first_record(store, agent, path, model=None)
            _board(store, session, live_model, live_effort)
            return None
        same_file = record.transcript == str(path)
        scan = transcripts.commands_since(path, record.offset if same_file else 0)
        if scan is None:
            return None
        live, turn = record.live, record.turn
        picks: dict[str, str] = {}
        unread: list[transcripts.SettingCommand] = []
        understood = False
        for event in scan.events:
            if isinstance(event, str):
                turn = event
                continue
            found = command_value(event)
            understood = understood or found is None or found.understood
            if found is None:
                continue
            if not found.understood:
                unread.append(event)
            if found.value is None:
                continue
            live = dataclasses.replace(live, **{found.setting: found.value})
            if found.pick:
                picks[found.setting] = found.value
        launch = record.launch
        if turn == "plain":
            launch = Pick(
                launch.model or (live_model if live.model is None else None),
                launch.effort or (live_effort if live.effort is None else None),
            )
        _keep(store, agent, _Record(launch, live, str(path), scan.end, turn))
        if unread:
            _say_unread(store, unread)
        elif understood and store.ui_state(_UNREAD_KEY) is not None:
            store.set_ui_state(_UNREAD_KEY, None)  # the answers read again: doctor stands down
        _board(store, session, live_model, live_effort)
        if not picks:
            return None
        written = Pick(picks.get("model"), picks.get("effort"))
        remember(model=written.model, effort=written.effort, store=store)
        return written


def _board(
    store: ContextStore, session: TeamSession, model: str | None, effort: str | None
) -> None:
    """The board row's model and effort: the turn's readings, shown, never evidence."""
    if model is not None and (model, effort or session.effort) != (session.model, session.effort):
        store.set_session_model(session.id, model=model, effort=effort)


_UNREAD_KEY = "fleet-model-unread"
"""The last answers to ``/model`` or ``/effort`` this reader could not read, for doctor."""


def _say_unread(store: ContextStore, commands: list[transcripts.SettingCommand]) -> None:
    """Say, once for the turn, that Claude Code answered in words this reader cannot read, and
    keep it for doctor (:func:`unread`): the pick was not written from them, and a restart
    starts the launch's values, the #338 symptom this reader exists to end."""
    said = "; ".join(f"/{c.setting} {c.args}".rstrip() + f": {c.stdout[:120]!r}" for c in commands)
    version = next((c.version for c in commands if c.version), None) or "this Claude Code"
    store.set_ui_state(
        _UNREAD_KEY,
        json.dumps({"at": datetime.now(tz=UTC).isoformat(), "version": version, "said": said}),
    )
    sys.stderr.write(
        f"aisquare: {version} answered {said} in words this aisquare cannot read — that is no "
        "pick (explicit args still are); run: aisquare doctor\n"
    )


def unread() -> dict[str, str] | None:
    """The last answers this reader could not read (``at``, ``version``, ``said``), or
    ``None``; for doctor's ``fleet model pick`` row. Fail-open: nothing read is nothing."""
    try:
        with store_session() as store:
            raw = store.ui_state(_UNREAD_KEY)
        data = json.loads(raw) if raw else None
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return {key: str(value) for key, value in data.items() if key in ("at", "version", "said")}


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
            ""
            if model == ""
            else harness.clean_model_id(model)
            if isinstance(model, str)
            else None,
            ""
            if effort == ""
            else harness.clean_effort(effort)
            if isinstance(effort, str)
            else None,
        )

    offset, transcript, turn = data.get("offset"), data.get("transcript"), data.get("turn")
    return _Record(
        launch=pick("launch"),
        live=pick("live"),
        transcript=transcript if isinstance(transcript, str) else None,
        offset=offset if isinstance(offset, int) and not isinstance(offset, bool) else 0,
        turn=turn if turn in ("plain", "command") else None,
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
                "turn": record.turn,
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
