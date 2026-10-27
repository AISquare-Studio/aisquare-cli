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
- the built-in ``/model`` or ``/effort`` the owner runs inside any fleet agent (or
  ``/config`` → Model, seq 3828), which its next turn end, or a replay before it, finds in
  Claude Code's own record of the command (:func:`observe`, from the Stop hook;
  :func:`caught_up`; :func:`command_values`). A model or effort that merely differs from the last
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
    """What a recorded ``/model`` or ``/effort`` set (:func:`command_values`)."""

    setting: str
    value: str | None
    """The value; ``""`` the account's own default (the picker's ``Default (recommended)``,
    ``/effort auto``: forget the pick); ``None`` nothing (an answer this reader cannot read)."""
    pick: bool
    """``False`` for a choice "for this session only": the agent runs it, the pick is left."""
    understood: bool = True
    """``False`` when Claude Code's answer is in words this reader does not know: it writes
    nothing, and is said once (:func:`observe`) and shown by doctor, as a reworded answer
    must not fail silently."""


#: Claude Code's answer to ``/model``: ``Set model to `Haiku 5.5` and saved as your default for
#: new sessions``, ``… for this session only``; 2.1.311's picker adds ``… with `xhigh` effort``;
#: ``Default (recommended)`` answers ``Set model to `Opus 5.5 (default)` …``.
_MODEL_ANSWER = re.compile(r"^Set model to\s+`([^`]+)`(.*)$", re.S)
_WITH_EFFORT = re.compile(r"\bwith\s+`?([A-Za-z]+)`?\s+effort\b")
#: Its answer to ``/effort``: ``Set effort level to low (saved as your default …): …``, or
#: ``(this session only)``; to ``/effort ultracode`` and the picker's Tab (2.1.311): ``Ultracode
#: on (this session only): … Effort stays high.``, ``Ultracode off. Effort stays high.``
_EFFORT_ANSWER = re.compile(r"^Set effort level to\s+([A-Za-z]+)\b(.*)$", re.S)
_ULTRACODE_ANSWER = re.compile(r"^Ultracode (on|off)\b(.*)$", re.S)
_EFFORT_STAYS = re.compile(r"\bEffort stays\s+([A-Za-z]+)\b")
#: The /effort picker's Tab-on with a level (2.1.311): ``Set effort level to high (saved as
#: your default for new sessions): … · Ultracode on``. The level is saved, and the session
#: runs ultracode (seq 3693 (3)).
_ULTRACODE_TOO = re.compile(r"·\s*Ultracode on\b")
#: Its answer to ``/effort auto`` (2.1.311), which clears the saved effort.
_EFFORT_AUTO = re.compile(r"^Effort level set to auto\b(.*)$", re.S)
#: Answers that set nothing: the pickers' Esc (opening a picker to look is no choice), and
#: Claude Code's refusals (2.1.311: ``Model 'sonet' not found``, ``Invalid argument: bogus.
#: Valid options are: …``), as an owner's typo must not become the pick (seq 3690).
_NO_CHOICE = re.compile(r"^(Kept model as|Cancelled|Model '.*' not found|Invalid argument\b)", re.S)
#: A model's display name (``Sonnet 5.5``), whose id Claude Code spells ``claude-sonnet-5-5``.
_DISPLAY_NAME = re.compile(r"^(Fable|Opus|Sonnet|Haiku)\s+(\d+)(?:\.(\d+))?$")


def command_values(command: transcripts.SettingCommand) -> tuple[CommandValue, ...]:
    """What ``command`` set, read ONLY off an answer that says it succeeded (seq 3690):
    nothing for the pickers' Esc (``Kept model as``, ``Cancelled``; opening a picker to look
    is no choice, seq 3629) or a refusal (``Model 'sonet' not found``); else each setting
    the answer names, the 2.1.311 picker's model with its effort. A value is the command's
    args (``/model haiku``, verbatim), else the answer's (``Set model to `Haiku 5.5``` is
    ``claude-haiku-5-5``); the picker's ``Default (recommended)`` and ``/effort auto`` are
    ``""``, the account's default, which forgets the pick. A choice "for this session only",
    and an ultracode toggle that is not saved, is the agent's and no pick. An answer in other
    words is not understood: it writes nothing, args or not (they cannot tell a success from
    a refusal), and is said (the canary)."""
    answer, args = command.stdout, command.args.strip()
    if command.via == "config":
        return _config_values(answer)
    if _NO_CHOICE.match(answer):
        return ()
    if command.setting == "model":
        answered = _MODEL_ANSWER.match(answer)
        if answered is None:
            return (CommandValue("model", None, False, understood=False),)
        named, rest = answered.group(1).strip(), answered.group(2)
        pick = "this session only" not in rest
        if named.endswith("(default)") or args.lower() == "default":
            model: str | None = ""
        else:
            model = harness.clean_model_id(args) if args else _model_id(named)
        found = [CommandValue("model", model, pick, model is not None)]
        if (effort := _WITH_EFFORT.search(rest)) is not None:
            level = harness.clean_effort(effort.group(1))
            found.append(CommandValue("effort", level, pick, level is not None))
        return tuple(found)
    if (toggled := _ULTRACODE_ANSWER.match(answer)) is not None:
        rest = toggled.group(2)
        stays = _EFFORT_STAYS.search(rest)
        level = (
            harness.ULTRACODE
            if toggled.group(1) == "on"
            else harness.clean_effort(stays.group(1))
            if stays is not None
            else None
        )
        saved = "saved as your default" in rest
        return (CommandValue("effort", level, saved, level is not None),)
    if (auto := _EFFORT_AUTO.match(answer)) is not None:
        return (CommandValue("effort", "", "this session only" not in auto.group(1)),)
    answered = _EFFORT_ANSWER.match(answer)
    if answered is None:
        return (CommandValue("effort", None, False, understood=False),)
    rest = answered.group(2)
    level = harness.clean_effort(args) if args else harness.clean_effort(answered.group(1))
    chosen = CommandValue("effort", level, "this session only" not in rest, level is not None)
    if _ULTRACODE_TOO.search(rest):  # the picker's Tab-on with a level: the session's own
        return (chosen, CommandValue("effort", harness.ULTRACODE, False))
    return (chosen,)


#: A ``/config`` answer's model line, its value as the Model panel names it (2.1.311): an alias
#: with its id (``haiku (claude-haiku-5-5)``), an id alone (``claude-haiku-4-5-20251001``), or
#: ``Default (Opus 5.5 · …)``, the account's default.
_CONFIG_MODEL = re.compile(r"^Set model to\s+(.+?)\s*$")
_ALIAS_AND_ID = re.compile(r"^\S+\s+\(([^()\s]+)\)$")


def _config_values(answer: str) -> tuple[CommandValue, ...]:
    """What a ``/config`` set (seq 3828 (2)): its answer's line per setting changed. The Model
    panel's pick "becomes the default for new sessions": a pick, the account's default
    (``""``) for ``Default``. A model line in other words is not understood (the canary)."""
    found: list[CommandValue] = []
    for line in answer.splitlines():
        if (model := _CONFIG_MODEL.match(line)) is not None:
            named = model.group(1)
            paired = _ALIAS_AND_ID.match(named)
            value = (
                ""
                if named.lower().startswith("default")
                else harness.clean_model_id(paired.group(1) if paired else named)
                if paired or " " not in named
                else None
            )
            found.append(CommandValue("model", value, True, value is not None))
        elif (effort := _EFFORT_ANSWER.match(line)) is not None:
            level = harness.clean_effort(effort.group(1))
            pick = "this session only" not in effort.group(2)
            found.append(CommandValue("effort", level, pick, level is not None))
    return tuple(found)


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
    its commands starts: where the row was bound to that transcript (:func:`started`), as
    long as the file was then, or where the last read ended. ``since`` is when it was bound
    (the row's creation, else the moment a rebind or a path change was seen): only frames
    stamped then or later count, as what a fork, a ``/branch``, a ``/resume`` or a switch
    brings in keeps its old stamps (manager decision, seq 3697). ``turn`` is the kind of
    the turn read last (``plain`` or ``command``)."""

    launch: Pick = Pick()
    live: Pick = Pick()
    transcript: str | None = None
    offset: int = 0
    turn: str | None = None
    since: datetime | None = None


def started(
    session_id: str, *, model: str | None, transcript_path: str | None, agent_id: str | None
) -> None:
    """At a fleet agent's session start: bind its row's record to the transcript, so only
    commands made after the binding count (seq 3697). A row's first record is bound from its
    creation and keeps the model its launch runs, as the payload reports it (``model``); a
    new file later (a ``/resume``, a ``/clear``, a ``/branch``) is bound from now, read
    from the end of what it holds. The same file (a compaction) reads on from where it was,
    a ``/model`` typed just before ``/compact`` included. Never an effort: Claude Code
    2.1.310's SessionStart carries none, and its hooks there see only the inherited
    ``CLAUDE_EFFORT`` (seq 3513). Only for the session the row is bound to
    (:func:`_own_row`). Raises on a store that cannot be read: the caller fails open."""
    with store_session() as store:
        agent = _own_row(store, session_id, agent_id)
        if agent is None:
            return
        record = _record(agent, store)
        if record is None:
            launch = Pick(harness.clean_model_id(model), _launch_flags(agent).effort)
            _bind(store, agent, _Record(launch=launch), transcript_path, since=_created(agent))
        elif transcript_path and record.transcript != transcript_path:
            _bind(store, agent, record, transcript_path, since=datetime.now(tz=UTC))


def _created(agent: FleetAgent) -> datetime:
    """When ``agent``'s row was made, in UTC: its first binding, before its session began."""
    made = agent.created_at
    return made if made.tzinfo is not None else made.replace(tzinfo=UTC)


def _bind(
    store: ContextStore,
    agent: FleetAgent,
    record: _Record,
    transcript: str | Path | None,
    *,
    since: datetime,
) -> None:
    """Keep ``record`` bound to ``transcript`` from ``since``, its commands read from the
    file's end as it is now (from its start when it is not there yet: a fork's copy lands
    after its session start, and ``since`` alone keeps it out)."""
    path = Path(transcript) if transcript else None
    try:
        offset = path.stat().st_size if path is not None else 0
    except OSError:
        offset = 0
    _keep(
        store,
        agent,
        dataclasses.replace(
            record,
            transcript=str(path) if path is not None else None,
            offset=offset,
            turn=None,
            since=since,
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
    frame (seq 3628). The value is the command's (:func:`command_values`); "this session only"
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
    with store_session() as store:
        session = store.get_session(session_id)
        agent = _own_row(store, session_id, agent_id)
        if session is None or agent is None:
            return None
        return _read(store, agent, session, transcript_path, effort=harness.clean_effort(effort))


def caught_up(agent: FleetAgent, session: TeamSession | None) -> None:
    """Before a replay of ``agent`` (a restart, a switch, a hand-over, a fork:
    ``fleet._running``): read what its transcript holds since the last read, as its Stop
    would, so a ``/model`` or ``/effort`` the owner ran after its last turn and before the
    replay is kept (seq 3797 (2)): only a Stop read it, and the replacement's record begins
    after it. ``agent`` is the row fleet replays (stopped by then, for all but a fork's
    source), not a session's say. No turn ended, so nothing of a turn's readings fills the
    launch or the board. Fail-open: a store or a transcript that cannot be read leaves the
    replay as it was."""
    if session is None:
        return
    try:
        with store_session() as store:
            record = _record(agent, store)
            if record is not None:
                _read(store, agent, session, record.transcript, effort=None, turn_end=False)
    except Exception:  # a replay is never refused for this
        return


def _read(
    store: ContextStore,
    agent: FleetAgent,
    session: TeamSession,
    transcript_path: str | None,
    *,
    effort: str | None,
    turn_end: bool = True,
) -> Pick | None:
    """:func:`observe`'s read of ``agent``'s commands, for a turn end (``effort`` the Stop's,
    the turn's readings fill the launch and the board) or a replay (:func:`caught_up`)."""
    live_effort = effort
    path = next(
        (Path(given) for given in (transcript_path, session.transcript_path)
         if given and Path(given).is_file()),
        None,
    )  # fmt: skip
    if path is None:
        return None
    live_model = harness.clean_model_id(transcripts.latest_model(path)) if turn_end else None
    record = _record(agent, store)
    if record is None:  # its session start unread: bound from the row's creation
        record = _Record(launch=Pick(None, _launch_flags(agent).effort), since=_created(agent))
    if record.transcript != str(path):
        if record.since is not None and _read_nothing(record):
            # A row that has read nothing (its session start unread, or bound to a file
            # that never came: a switch's slot-2 path, seq 3693 c) reads the file it finds
            # from 0, by its own stamp: what the owner typed since counts, the older
            # conversation's commands do not (review of #441 round 8, minor 1).
            record = dataclasses.replace(record, transcript=str(path), offset=0, turn=None)
        else:
            # Another file than the one it read (a `/branch` whose session start went
            # unread): its copy holds this row's own past commands, stamped after its
            # creation, so it is bound from the moment it is seen (seq 3697).
            _bind(store, agent, record, path, since=datetime.now(tz=UTC))
            _board(store, session, live_model, live_effort)
            return None
    scan = transcripts.commands_since(path, record.offset, since=record.since)
    if scan is None:
        return None
    live, turn = record.live, record.turn
    picks: dict[str, str] = {}
    unread: list[str] = []
    understood = False
    for event in scan.events:
        if isinstance(event, str):
            turn = event
            continue
        if isinstance(event, transcripts.UnframedCommand):
            unread.append(
                f"{_version(event.version)} recorded a /{event.setting} outside its frame"
            )
            continue
        values = command_values(event)
        if any(not found.understood for found in values):
            said = f"/{event.via or event.setting} {event.args}".rstrip()
            unread.append(f"{_version(event.version)} answered {said} with {event.stdout[:120]!r}")
        elif values:  # a success read; a refusal or an Esc says nothing of those words
            understood = True
        for found in values:
            if found.value is None:
                continue
            live = dataclasses.replace(live, **{found.setting: found.value})
            if found.pick:
                picks[found.setting] = found.value
    launch = record.launch
    if turn == "plain" and turn_end:
        launch = Pick(
            launch.model or (live_model if live.model is None else None),
            launch.effort or (live_effort if live.effort is None else None),
        )
    _keep(store, agent, _Record(launch, live, str(path), scan.end, turn, record.since))
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


def _read_nothing(record: _Record) -> bool:
    """Whether ``record`` has read nothing yet: bound to no file, or to one that never came
    (``offset`` 0, nothing there now)."""
    if record.transcript is None:
        return True
    return record.offset == 0 and record.turn is None and not Path(record.transcript).exists()


def _board(
    store: ContextStore, session: TeamSession, model: str | None, effort: str | None
) -> None:
    """The board row's model and effort: the turn's readings, shown, never evidence."""
    if model is not None and (model, effort or session.effort) != (session.model, session.effort):
        store.set_session_model(session.id, model=model, effort=effort)


_UNREAD_KEY = "fleet-model-unread"
"""The last answers to ``/model`` or ``/effort`` this reader could not read, for doctor."""


def _version(version: str | None) -> str:
    return f"Claude Code {version}" if version else "Claude Code"


def _say_unread(store: ContextStore, unread: list[str]) -> None:
    """Say, once for the turn, what this reader could not read as a choice (an answer in
    other words, a ``/model`` or ``/effort`` record outside Claude Code's frame), and keep
    it for doctor (:func:`unread`): no pick was written from it, and a restart starts the
    model the agent ran before it, the #338 symptom this reader exists to end. Doctor stands
    down at the next answer read as a success (a refusal or an Esc says nothing of those
    words)."""
    said = "; ".join(unread)
    store.set_ui_state(
        _UNREAD_KEY, json.dumps({"at": datetime.now(tz=UTC).isoformat(), "said": said})
    )
    sys.stderr.write(
        f"aisquare: {said}, which this aisquare cannot read as a choice — no pick was "
        "written; run: aisquare doctor\n"
    )


def unread() -> dict[str, str] | None:
    """What this reader last could not read as a choice (``at``, ``said``), or ``None``; for
    doctor's ``fleet model pick`` row. Fail-open: nothing read is nothing."""
    try:
        with store_session() as store:
            raw = store.ui_state(_UNREAD_KEY)
        data = json.loads(raw) if raw else None
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return {key: str(value) for key, value in data.items() if key in ("at", "said")}


_LAUNCHED_KEY = "fleet-launched:{project}:{label}"
"""A fleet row's own record (:class:`_Record`), per label (one row holds a label at a time),
with the id of the row it is for. Kept when the row ends (a restart of it reads it); a
project's go with Start fresh (``store._FLEET_LABEL_STATE``)."""


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
    try:
        since = datetime.fromisoformat(data["since"]) if data.get("since") else None
    except (TypeError, ValueError):
        since = None
    return _Record(
        launch=pick("launch"),
        live=pick("live"),
        transcript=transcript if isinstance(transcript, str) else None,
        offset=offset if isinstance(offset, int) and not isinstance(offset, bool) else 0,
        turn=turn if turn in ("plain", "command") else None,
        since=since if since is None or since.tzinfo is not None else None,
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
                "since": record.since.isoformat() if record.since is not None else None,
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
    """The ``--model`` and ``--effort`` the agent was launched with, by the rule ``aisquare
    launch`` applies: its window's own (its arguments', else what the spawn added beside
    them), else its role's binding's (``team bind --arg``), which launch adds to every window
    and no spec records (seq 3797 (1): a bound ultracode read as the Stop's ``xhigh``)."""
    spec = agent.launch_spec
    if spec is None:
        return Pick()
    bound = harness.binding_args(agent.role)
    return Pick(
        harness.model_arg(spec.extra_args) or spec.model or harness.model_arg(bound),
        harness.flag_arg(spec.extra_args, "--effort")
        or spec.effort
        or harness.flag_arg(bound, "--effort"),
    )
