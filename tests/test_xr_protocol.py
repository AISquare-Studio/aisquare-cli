"""The cliXR wire protocol and the projector that fills it.

The protocol half is a contract test in the literal sense: a browser client is
being written against ``web/xr/protocol.schema.json`` by someone else, and the
only thing tying that file to these models is the assertion below that the
committed schema is byte-for-byte what the models generate.

The projector half is the §5 rule that makes the ring glanceable —
``summary`` comes from a board event and never from a transcript — plus the
state, colour and title mapping a panel is drawn from.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aisquare.core.ids import new_event_id, new_task_id
from aisquare.core.orchestrator import team_project
from aisquare.core.store import ContextStore, store_session
from aisquare.models import FleetAgent, TeamEvent, TeamSession, TeamTask
from aisquare.services.team import _STALE_AFTER
from aisquare.services.xr import projector
from aisquare.services.xr import protocol as wire

PLANNER = "aaaa1111-0000-0000-0000-000000000000"
CODER = "bbbb2222-0000-0000-0000-000000000000"
RUNNER = "cccc3333-0000-0000-0000-000000000000"


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


# --- protocol -------------------------------------------------------------------

#: One instance of every message in plan §6, plus ``ack``. Built by hand rather
#: than generated from the models so that a field RENAMED in protocol.py fails
#: here — a round-trip through the same models would happily agree with itself.
MESSAGES: list[tuple[type[Any], dict[str, Any]]] = [
    (
        wire.Hello,
        {"t": "hello", "protocol": 1, "hub": "prj_1", "serverTime": "2026-09-12T00:00:00Z"},
    ),
    (wire.Snapshot, {"t": "snapshot", "sessions": [], "tasks": [], "groups": []}),
    (wire.Delta, {"t": "delta", "changed": [], "removed": ["ses_1"]}),
    (
        wire.Transcript,
        {
            "t": "transcript",
            "session": "ses_1",
            "seq": 41,
            "text": "hello",
            "final": True,
            "reset": False,
        },
    ),
    (wire.Stt, {"t": "stt", "text": "open the", "final": False}),
    (wire.Error, {"t": "error", "code": "auth_failed", "message": "no"}),
    (wire.Ack, {"t": "ack", "for": "prompt", "session": "ses_1", "ok": True, "detail": "typed"}),
    (wire.Auth, {"t": "auth", "token": "secret"}),
    (wire.Subscribe, {"t": "subscribe", "session": None}),
    (wire.Prompt, {"t": "prompt", "session": "ses_1", "text": "run the tests"}),
    (wire.Audio, {"t": "audio", "session": "ses_1", "seq": 0}),
    (wire.AudioEnd, {"t": "audioEnd", "session": "ses_1"}),
]


@pytest.mark.parametrize(("model", "payload"), MESSAGES, ids=lambda value: str(value)[:24])
def test_every_message_round_trips(model: type[Any], payload: dict[str, Any]) -> None:
    """Wire JSON -> model -> wire JSON is the identity, field names included."""
    built = model.model_validate(payload)
    assert json.loads(wire.to_wire(built)) == payload


def test_the_session_entity_carries_every_field_the_client_draws() -> None:
    session = wire.Session.model_validate(
        {
            "id": "ses_1",
            "role": "coder",
            "title": "auth refactor",
            "state": "needs_you",
            "summary": "wiring JWT",
            "taskId": "tsk_1",
            "colorKey": "coder",
            "lastActivityAt": "2026-09-12T00:00:00Z",
            "unread": 3,
        }
    )
    assert json.loads(wire.to_wire(session))["taskId"] == "tsk_1"
    assert session.task_id == "tsk_1", "the alias must populate the python name too"


def test_ack_serializes_its_keyword_field_as_for() -> None:
    """``for`` cannot be a python identifier; the wire name is what matters."""
    dumped = json.loads(wire.to_wire(wire.Ack(session="ses_1", ok=False, detail="filed")))
    assert dumped["for"] == "prompt"
    assert "for_" not in dumped


def test_a_client_frame_parses_to_its_own_model() -> None:
    parsed = wire.parse_client('{"t":"prompt","session":"ses_1","text":"go"}')
    assert isinstance(parsed, wire.Prompt)
    assert parsed.text == "go"


def test_an_unknown_field_is_refused_rather_than_dropped() -> None:
    """A client that misspells a field learns on the first frame, not never."""
    with pytest.raises(Exception, match=r"message|extra"):
        wire.parse_client('{"t":"prompt","session":"ses_1","message":"go"}')


def test_a_server_message_is_not_a_client_message() -> None:
    with pytest.raises(Exception, match=r"t|discriminator"):
        wire.parse_client('{"t":"hello","protocol":1,"hub":"h","serverTime":"x"}')


def test_the_committed_schema_matches_the_models() -> None:
    """``python -m aisquare.services.xr.protocol --check``, as an assertion.

    This is the whole reason the schema is generated: two people are building
    against it in parallel, and a field renamed in protocol.py without a
    regenerate is a bug that only shows up in a headset.
    """
    committed = Path(str(wire.schema_path())).read_text(encoding="utf-8")
    assert committed == wire.schema_text(), (
        "web/xr/protocol.schema.json has drifted from protocol.py — regenerate it: "
        "python -m aisquare.services.xr.protocol --write"
    )


def test_the_schema_names_both_directions_and_the_version() -> None:
    document = wire.schema_document()
    assert document["protocol"] == wire.PROTOCOL_VERSION
    assert set(document) >= {"server", "client", "protocol"}
    assert json.dumps(document), "the schema must be JSON-serializable as committed"


def test_the_schema_carries_the_binary_audio_format() -> None:
    """The audio half of the protocol must be implementable from the contract.

    ``protocol.py`` opens by calling itself the contract that this server and a
    browser client written by someone else are two implementations of. For the
    JSON frames that is delivered. For the binary ones it was not: sample rate,
    bit depth, channel count and endianness appeared NOWHERE in this tree —
    ``audio`` said only "binary frames follow", and the single mention of a
    format anywhere was a comment about a byte CAP ("~4 minutes of 16 kHz mono
    PCM"), which is an inference about a magnitude, not a specification.

    A second implementer could therefore read every line of the schema and
    still send 48 kHz float32, which this server accepts and hands to whisper.
    The two implementations that exist agreed out of band, in task
    descriptions — the channel that is gone in six months.
    """
    audio = wire.schema_document()["audio"]
    assert audio["encoding"] == "pcm_s16le"
    assert audio["sampleRateHz"] == 16_000
    assert audio["sampleBits"] == 16
    assert audio["signed"] is True
    assert audio["endianness"] == "little"
    assert audio["channels"] == 1
    assert audio["frameBytes"] == 640, "20 ms of 16 kHz mono PCM16 — 320 samples"
    assert "sample" in audio["alignment"], "an odd byte length shifts every sample after it"


def test_the_published_frame_size_counts_channels() -> None:
    """``frameBytes`` must include :data:`AUDIO_CHANNELS`, computed independently here.

    A pinned literal a reviewer reads, deliberately NOT ``schema_document``'s own
    formula: the drift test compares the generator with its committed output, so
    a ``frameBytes`` that dropped the channel factor would publish 640 for a
    stereo frame — half its real 1280 — and the two copies would still agree. The
    recomputation below multiplies the channel count back in, so it disagrees
    with any generator that leaves it out at any channel count other than one,
    and it happens to equal 640 today because the format is mono.
    """
    audio = wire.schema_document()["audio"]
    expected = (
        audio["sampleRateHz"]
        * (audio["sampleBits"] // 8)
        * audio["channels"]
        * audio["frameMs"]
        // 1000
    )
    assert audio["frameBytes"] == expected, (
        "frameBytes must be samples x bytes x CHANNELS x seconds"
    )
    assert wire.AUDIO_FRAME_BYTES == 16_000 * 2 * 1 * 20 // 1000 == 640
    # The constant is what the schema publishes, and it is the constant that
    # carries the channel factor — so the format has exactly one home.
    assert audio["frameBytes"] == wire.AUDIO_FRAME_BYTES


def test_the_schema_splits_the_terminal_close_from_the_retryable_one() -> None:
    """4401 (rejected token) is terminal; 4408 (stalled handshake) is retryable.

    The old contract published only 4401, ``retry:false``, and the server sent
    it for EVERY pre-auth failure — a valid token that arrived a half-second
    late, a first frame that was not JSON. A client that followed the contract
    then gave up permanently on a transient stall, and the ring stayed dark
    until the printed URL was reopened. The two must be distinguishable from the
    schema alone, because that is all a second implementer has: the terminal one
    is the ONLY close a client must not reconnect through.
    """
    codes = wire.schema_document()["closeCodes"]
    failed = codes[str(wire.CLOSE_AUTH_FAILED)]
    timeout = codes[str(wire.CLOSE_AUTH_TIMEOUT)]
    assert wire.CLOSE_AUTH_FAILED == 4401
    assert wire.CLOSE_AUTH_TIMEOUT == 4408
    assert failed["retry"] is False, "a rejected token will be rejected again — do not retry"
    assert timeout["retry"] is True, "no token was checked, so reconnecting is right"
    assert wire.CLOSE_AUTH_FAILED != wire.CLOSE_AUTH_TIMEOUT, "a client branches on the number"
    assert "transport close" in timeout["description"], "and it must say the others are transport"


def test_a_negative_burst_ordinal_is_refused() -> None:
    """``Audio.seq`` carries a constraint rather than only a default.

    It was published with neither: no description and no bound, in a schema a
    client author reads to decide what a field means. The reasonable readings
    — a per-CHUNK sequence number, a gap-detection cursor — are both wrong and
    neither exists anywhere in this server.
    """
    assert wire.parse_client('{"t":"audio","session":"ses_1","seq":7}').seq == 7
    with pytest.raises(Exception, match=r"greater than or equal|seq"):
        wire.parse_client('{"t":"audio","session":"ses_1","seq":-1}')


def test_a_subscribe_id_is_rejected_before_it_can_become_a_wildcard() -> None:
    """An empty or glob-metacharacter session id is refused at the wire boundary.

    ``store``'s prefix resolver STRIPS ``*``/``?``/``[`` before it globs, so an
    empty string reaches it as ``GLOB '*'`` over every session and the request
    acts on a real, wrong one. ``None`` is still the legitimate "stop following"
    value; a real id or prefix (hex, dashes, and the colons an MCP id carries)
    still parses.
    """
    assert wire.parse_client('{"t":"subscribe","session":null}').session is None
    assert wire.parse_client('{"t":"subscribe","session":"bbbb2222"}').session == "bbbb2222"
    assert wire.parse_client('{"t":"subscribe","session":"mcp:remote:abc123"}').session
    for bad in ('""', '"*"', '"?"', '"["', '"a b"'):
        with pytest.raises(Exception, match=r"pattern|at least 1|string"):
            wire.parse_client(f'{{"t":"subscribe","session":{bad}}}')


def test_the_schema_lists_the_connect_time_and_restart_closes_as_retryable() -> None:
    """1013 and 1012 are in the contract with ``retry:true``; 4401 stays the only terminal close.

    A client's reconnect policy is a function of the close code alone whenever
    the error frame before it was lost, so every close this server can produce
    — and the one uvicorn produces around it — must be published with its retry
    bit. 1013 follows ``board_unavailable`` (a locked or damaged store at
    connect); 1012 is uvicorn's shutdown close, which no code here sends but
    every client of this server receives on Ctrl-C. Both are transport closes:
    the token is still good, and the first version of this contract, which
    listed only 4401, left a client to guess at exactly these two.
    """
    codes = wire.schema_document()["closeCodes"]
    assert wire.CLOSE_TRY_AGAIN_LATER == 1013 and wire.CLOSE_SERVICE_RESTART == 1012
    again = codes[str(wire.CLOSE_TRY_AGAIN_LATER)]
    restart = codes[str(wire.CLOSE_SERVICE_RESTART)]
    assert again["retry"] is True and "board_unavailable" in again["description"]
    assert restart["retry"] is True and "uvicorn" in restart["description"]
    terminal = sorted(code for code, entry in codes.items() if entry["retry"] is False)
    assert terminal == [str(wire.CLOSE_AUTH_FAILED)], (
        "a rejected token is the ONLY close a client must not reconnect through"
    )


def test_an_audio_burst_id_is_validated_like_a_subscribe() -> None:
    """``audio.session`` and ``audioEnd.session`` are ``SessionRef`` too.

    The voice route resolves the header's session through the same on-this-board
    prefix resolver a typed prompt uses, so the glob hazard that made ``""`` and
    ``*`` a wildcard on ``subscribe`` existed on the header as well — with the
    operator's spoken sentence as the payload. Refused at the wire boundary, and
    published that way: the client schema carries the same ``minLength`` and
    ``pattern`` on all four fields, so a second implementer reads one rule.
    """
    assert wire.parse_client('{"t":"audio","session":"bbbb2222","seq":0}').session == "bbbb2222"
    assert wire.parse_client('{"t":"audioEnd","session":"mcp:remote:abc"}').session
    for frame in ("audio", "audioEnd"):
        for bad in ('""', '"*"', '"?"', '"a b"'):
            with pytest.raises(Exception, match=r"pattern|at least 1|string"):
                wire.parse_client(f'{{"t":"{frame}","session":{bad}}}')
    definitions = wire.schema_document()["client"]["$defs"]
    for name in ("Audio", "AudioEnd", "Prompt"):
        field = definitions[name]["properties"]["session"]
        assert field["minLength"] == 1, name
        assert field["pattern"] == wire._SESSION_REF_PATTERN, name


# --- projector ------------------------------------------------------------------


def _session(
    store: ContextStore,
    sid: str,
    project_id: str,
    *,
    role: str,
    state: str = "working",
    idle_min: int = 0,
    focus: str | None = None,
    transcript: str | None = None,
) -> TeamSession:
    seen = datetime.now(tz=UTC) - timedelta(minutes=idle_min)
    return store.upsert_session(
        TeamSession(
            id=sid,
            project_id=project_id,
            role=role,
            state=state,
            focus=focus,
            started_at=seen,
            last_seen_at=seen,
            transcript_path=transcript,
        )
    )


def _event(store: ContextStore, project_id: str, sid: str, kind: str, text: str) -> TeamEvent:
    return store.add_team_event(
        TeamEvent(
            id=new_event_id(),
            project_id=project_id,
            session_id=sid,
            kind=kind,
            text=text,
            created_at=datetime.now(tz=UTC),
        )
    )


def _seed(store: ContextStore, project_id: str) -> None:
    """Three sessions of three roles, the shape the ring is drawn from."""
    _session(store, PLANNER, project_id, role="manager", focus="steering the fleet")
    _session(store, CODER, project_id, role="coder1", state="attention")
    _session(store, RUNNER, project_id, role="tester", state="waiting")


def test_snapshot_maps_roles_states_colors_and_titles(work_dir: Path) -> None:
    project = team_project(work_dir)
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.ensure_project(project)
        _seed(store, project.id)
        task = TeamTask(
            id=new_task_id(),
            project_id=project.id,
            key="auth",
            title="auth refactor",
            status="doing",
            claimed_by=CODER,
            created_at=now,
            updated_at=now,
        )
        store.upsert_task(task)
        snapshot = projector.snapshot(store, project.id)

    by_id = {session.id: session for session in snapshot.sessions}
    assert set(by_id) == {PLANNER, CODER, RUNNER}

    # A manager is a planner on the ring; a numbered seat is its base role.
    assert (by_id[PLANNER].role, by_id[PLANNER].color_key) == ("planner", "planner")
    assert (by_id[CODER].role, by_id[CODER].color_key) == ("coder", "coder")
    assert (by_id[RUNNER].role, by_id[RUNNER].color_key) == ("runner", "runner")

    assert by_id[PLANNER].state == "working"
    assert by_id[CODER].state == "needs_you", "the board says attention; the ring says needs_you"
    assert by_id[RUNNER].state == "waiting"

    assert by_id[CODER].title == "auth refactor", "a claimed task titles its panel"
    assert by_id[CODER].task_id == task.id
    assert by_id[PLANNER].title == "steering the fleet", "else the session's own focus"
    assert by_id[RUNNER].title == "tester", "else its name — never empty"


def test_a_fleet_label_titles_a_panel_with_no_task_and_no_focus(work_dir: Path) -> None:
    project = team_project(work_dir)
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.ensure_project(project)
        _session(store, CODER, project.id, role="coder")
        store.upsert_fleet_agent(
            FleetAgent(
                id="flt_1",
                project_id=project.id,
                label="coder-xr-server",
                role="coder",
                pane_id="%1",
                session_id=CODER,
                cwd=work_dir,
                created_at=now,
            )
        )
        snapshot = projector.snapshot(store, project.id)
    assert snapshot.sessions[0].title == "coder-xr-server"


def test_a_remote_mcp_client_keeps_its_own_label_and_a_worker_colour(work_dir: Path) -> None:
    project = team_project(work_dir)
    with store_session() as store:
        store.ensure_project(project)
        _session(store, CODER, project.id, role="remote")
        snapshot = projector.snapshot(store, project.id)
    assert snapshot.sessions[0].role == "remote"
    assert snapshot.sessions[0].color_key == "runner"


def test_an_ended_or_stale_session_is_not_on_the_ring(work_dir: Path) -> None:
    project = team_project(work_dir)
    stale_minutes = int(_STALE_AFTER.total_seconds() // 60) + 1
    with store_session() as store:
        store.ensure_project(project)
        _session(store, PLANNER, project.id, role="planner")
        _session(store, CODER, project.id, role="coder", idle_min=stale_minutes)
        _session(store, RUNNER, project.id, role="runner")
        store.end_session(RUNNER)
        snapshot = projector.snapshot(store, project.id)
    assert [session.id for session in snapshot.sessions] == [PLANNER]


def test_a_session_waiting_on_the_operator_does_not_age_out_of_the_ring(work_dir: Path) -> None:
    """A ``needs_you`` session stays on the ring past the staleness horizon; an unflagged one goes.

    The Notification hook writes ``last_seen_at`` once and nothing else runs on
    a parked session until the operator answers, so a horizon applied before
    the attention check removed the alerting panel thirty minutes into the
    walk-away the alert exists for — ``delta.removed``, bar gone, nothing for
    **B** to jump to — while ``aisquare board`` still said NEEDS YOU. The ring
    follows the hook, not the clock: only a session with no attention flag
    ages out.
    """
    project = team_project(work_dir)
    stale_minutes = int(_STALE_AFTER.total_seconds() // 60) + 1
    with store_session() as store:
        store.ensure_project(project)
        _session(
            store, PLANNER, project.id, role="planner", state="attention", idle_min=stale_minutes
        )
        _session(store, CODER, project.id, role="coder", idle_min=stale_minutes)
        snapshot = projector.snapshot(store, project.id)
    by_id = {session.id: session for session in snapshot.sessions}
    assert PLANNER in by_id, "a session waiting on the operator was aged off the ring"
    assert by_id[PLANNER].state == "needs_you", "and its alert must still stand"
    assert CODER not in by_id, "a session with no attention flag still ages out on the clock"


def test_summary_is_at_most_six_words_of_the_latest_board_event(work_dir: Path) -> None:
    project = team_project(work_dir)
    with store_session() as store:
        store.ensure_project(project)
        _session(store, CODER, project.id, role="coder")
        _event(store, project.id, CODER, "note", "one two three four five six seven eight")
        _event(store, project.id, CODER, "result", "the latest event is the one that shows")
        snapshot = projector.snapshot(store, project.id)
    summary = snapshot.sessions[0].summary
    assert summary.startswith("the latest event"), "the newest event wins"
    # Six, the literal from plan §5 — not `projector.SUMMARY_WORDS`. Asserting
    # against the constant would make this test agree with any value the
    # constant is later given, which is the one thing it exists to prevent.
    assert len(summary.split()) <= 6, summary
    assert projector.SUMMARY_WORDS == 6, "the cap is the plan's number, not a preference"


def test_the_summary_spends_its_characters_on_a_verb_and_the_text_not_on_the_kind(
    work_dir: Path,
) -> None:
    """Board seq 293, and it is an arithmetic finding rather than a taste one.

    A panel at arm's length under §8's 1.5-degree cap-height floor fits about
    THIRTEEN legible characters of summary. The kind used to lead, so all
    thirteen went to it — ``task_claimed…`` — and a task id nobody reads off
    a wall. The first thirteen characters are the only ones the operator
    reads at that size, so this asserts on exactly that prefix: one short
    board-status word, then the title.
    """
    project = team_project(work_dir)
    with store_session() as store:
        store.ensure_project(project)
        _session(store, CODER, project.id, role="coder")
        _event(store, project.id, CODER, "task_claimed", "wiring JWT into the refresh path")
        snapshot = projector.snapshot(store, project.id)
    summary = snapshot.sessions[0].summary

    assert summary == "doing: wiring JWT into the refresh", summary
    assert "task_claimed" not in summary, "the kind is a verb, not an identifier"
    assert summary[:13] == "doing: wiring", "the thirteen the operator actually reads"
    # The cap is unchanged, verb included: the client truncates and the FOCUS
    # tier renders the field whole. Dropping the prefix was not a licence to
    # shrink the promise, and neither is adding the verb.
    assert len(summary.split()) == 6, summary


def test_what_happened_to_a_task_survives_in_the_summary(work_dir: Path) -> None:
    """A claim, a review, a release and a completion must be four different panels.

    ``team.py`` writes the task TITLE as the text of every ``task_*`` event,
    so a summary made of the text alone rendered a task sent to review the
    same as one just claimed, and a released one the same as a finished one;
    the operator read a finished task as still in progress. No other part of
    the ambient panel carries the kind — the state chip is the session's
    state, not the event's — so the summary must, and the board's own status
    words are what it uses.
    """
    project = team_project(work_dir)
    title = "wiring JWT into the refresh path"
    seen: dict[str, str] = {}
    with store_session() as store:
        store.ensure_project(project)
        _session(store, CODER, project.id, role="coder")
        for kind in ("task_claimed", "task_review", "task_released", "task_done"):
            _event(store, project.id, CODER, kind, title)
            seen[kind] = projector.snapshot(store, project.id).sessions[0].summary

    assert seen == {
        "task_claimed": "doing: wiring JWT into the refresh",
        "task_review": "review: wiring JWT into the refresh",
        "task_released": "released: wiring JWT into the refresh",
        "task_done": "done: wiring JWT into the refresh",
    }, seen
    assert len(set(seen.values())) == 4, "two kinds rendered the same panel"


def test_an_event_that_carries_its_own_words_gets_no_verb(work_dir: Path) -> None:
    """A note, a result or a question is its text; a verb there would only cost characters."""
    project = team_project(work_dir)
    with store_session() as store:
        store.ensure_project(project)
        _session(store, CODER, project.id, role="coder")
        _event(store, project.id, CODER, "note", "the suite is green on both extras")
        snapshot = projector.snapshot(store, project.id)

    assert snapshot.sessions[0].summary == "the suite is green on both"


def test_an_event_with_no_text_falls_back_to_its_kind_rather_than_a_blank(
    work_dir: Path,
) -> None:
    """The one case the kind still appears, and why it is not the old prefix.

    It can only show up when there is no content for it to push out, which is
    the whole objection to the prefix. A blank line under a panel title tells
    the operator less than ``heartbeat`` does.
    """
    project = team_project(work_dir)
    with store_session() as store:
        store.ensure_project(project)
        _session(store, CODER, project.id, role="coder")
        _event(store, project.id, CODER, "heartbeat", "   ")
        snapshot = projector.snapshot(store, project.id)
    assert snapshot.sessions[0].summary == "heartbeat"


def test_transcript_text_never_reaches_the_ambient_tier(work_dir: Path) -> None:
    """§5's rule, pinned with a sentinel.

    The ambient tier renders ten panels at once; transcript content there is a
    frame-budget problem and a privacy one. Transcript text reaches a client
    only for the ONE session it has explicitly subscribed to.
    """
    sentinel = "SENTINEL-TRANSCRIPT-MUST-NOT-PROJECT"
    project = team_project(work_dir)
    transcript = work_dir / "session.jsonl"
    transcript.write_text(
        json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": sentinel}]}}
        )
        + "\n",
        encoding="utf-8",
    )
    with store_session() as store:
        store.ensure_project(project)
        _session(store, CODER, project.id, role="coder", transcript=str(transcript))
        _event(store, project.id, CODER, "note", "claimed the xr task")
        before = projector.sessions(store, project.id)
        _event(store, project.id, CODER, "result", "landed the projector")
        snapshot = projector.snapshot(store, project.id)
        change = projector.delta(before, projector.sessions(store, project.id))

    assert change is not None
    rendered = wire.to_wire(snapshot) + wire.to_wire(change)
    assert sentinel not in rendered
    assert sentinel in transcript.read_text(encoding="utf-8"), "the sentinel was really there"


def test_delta_reports_added_changed_and_removed(work_dir: Path) -> None:
    project = team_project(work_dir)
    with store_session() as store:
        store.ensure_project(project)
        _session(store, PLANNER, project.id, role="planner")
        _session(store, CODER, project.id, role="coder")
        before = projector.sessions(store, project.id)

        store.mark_attention(CODER)  # changed
        _session(store, RUNNER, project.id, role="runner")  # added
        store.end_session(PLANNER)  # removed
        change = projector.delta(before, projector.sessions(store, project.id))

    assert change is not None
    assert change.removed == [PLANNER]
    changed = {session.id: session.state for session in change.changed}
    assert changed[CODER] == "needs_you"
    assert RUNNER in changed


def test_delta_is_none_when_the_board_has_not_moved(work_dir: Path) -> None:
    """An idle board puts nothing on the wire — twice a second, forever."""
    project = team_project(work_dir)
    with store_session() as store:
        store.ensure_project(project)
        _session(store, CODER, project.id, role="coder")
        sessions = projector.sessions(store, project.id)
        assert projector.delta(sessions, projector.sessions(store, project.id)) is None


def test_unread_counts_events_since_this_connection_looked(work_dir: Path) -> None:
    project = team_project(work_dir)
    with store_session() as store:
        store.ensure_project(project)
        _session(store, CODER, project.id, role="coder")
        _event(store, project.id, CODER, "note", "before the operator connected")
        watermark = {CODER: store.latest_seq(project.id)}
        fresh = projector.sessions(store, project.id, unread_since=watermark)
        assert fresh[0].unread == 0, "nothing has happened since they connected"

        _event(store, project.id, CODER, "note", "while they were looking elsewhere")
        _event(store, project.id, CODER, "result", "and again")
        later = projector.sessions(store, project.id, unread_since=watermark)
    assert later[0].unread == 2


def test_a_quiet_badge_survives_a_busy_session_past_the_scan_depth(work_dir: Path) -> None:
    """A busy session's traffic must not evict a quiet session's unread count.

    ``_unread_counts`` counts each session from its OWN watermark, not within a
    single newest-N-of-the-board window. It used to use such a window, and this
    test posts the events in the order that exposes it: the QUIET events FIRST,
    then a busy session past the scan depth. With a newest-500 window the quiet
    three are the oldest on the board and fall out of it, so the badge silently
    reads 0 though the watermark never moved (the earlier version of this test
    passed only because it posted the quiet events LAST, where the window still
    happened to hold them).
    """
    project = team_project(work_dir)
    with store_session() as store:
        store.ensure_project(project)
        _session(store, CODER, project.id, role="coder")
        _session(store, PLANNER, project.id, role="manager")
        for index in range(3):
            _event(store, project.id, PLANNER, "note", f"quiet {index}")
        for index in range(projector._EVENT_SCAN + 100):
            _event(store, project.id, CODER, "note", f"busy {index}")
        counts = store.unread_counts(project.id, {CODER: 0, PLANNER: 0}, cap=projector._EVENT_SCAN)

    assert counts.get(PLANNER) == 3, (
        "the planner's three events are its own unread, however loud another "
        "session got afterwards; a badge that cannot see them is reading a "
        "board-wide window that evicted them"
    )
    # The depth still bounds the ANSWER per session — a cap, reached only by a
    # session that genuinely has _EVENT_SCAN unread events.
    assert counts[CODER] == projector._EVENT_SCAN


def test_the_projector_counts_a_watermark_free_session_from_the_floor(work_dir: Path) -> None:
    """A session the connection never watermarked counts from ``unread_floor``.

    This is the contract that let the poll loop drop its per-tick re-seeding: a
    late joiner has no entry in ``unread_since``, and :func:`projector.sessions`
    defaults it to the connection's start position, so it gets a real badge for
    everything it has done since it appeared rather than reading 0 forever. (The
    previous contract was the opposite — an un-watermarked session was counted by
    nobody — and the server re-seeded every tick to work around it.)
    """
    project = team_project(work_dir)
    with store_session() as store:
        store.ensure_project(project)
        _session(store, CODER, project.id, role="coder")
        floor = store.latest_seq(project.id)
        _session(store, PLANNER, project.id, role="manager")
        for index in range(3):
            _event(store, project.id, PLANNER, "note", f"loud {index}")
        # CODER is watermarked at the floor; PLANNER (the late joiner) is not.
        ring = projector.sessions(
            store, project.id, unread_since={CODER: floor}, unread_floor=floor
        )
    badges = {session.id: session.unread for session in ring}
    assert badges[PLANNER] == 3, "a late joiner counts from the floor, not from never"
    assert badges[CODER] == 0, "a watermarked session with no new events stays at 0"


def test_sessions_reads_the_event_window_only_once(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Summaries and unread counting must not each issue ``recent_events``.

    The two used to make the identical ``recent_events(_EVENT_SCAN)`` call, so a
    board hydrated 1000 rows per poll where 500 would do. The unread pass no
    longer touches ``recent_events`` at all (it has its own indexed query), and
    the summary pass reads the window once and it is shared.
    """
    project = team_project(work_dir)
    with store_session() as store:
        store.ensure_project(project)
        _session(store, CODER, project.id, role="coder")
        _event(store, project.id, CODER, "note", "working")
        calls = {"n": 0}
        real = store.recent_events

        def counting(*args: Any, **kwargs: Any) -> list[Any]:
            calls["n"] += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(store, "recent_events", counting)
        projector.sessions(store, project.id, unread_since={CODER: 0}, unread_floor=0)
    assert calls["n"] == 1, f"recent_events must be read once per sessions(); it was {calls['n']}"


def test_a_prefix_is_resolved_against_this_board_not_the_whole_store(work_dir: Path) -> None:
    """``get_session_in_project`` scopes a prefix to one project.

    ``get_session``'s ``GLOB`` fallback spans every project sharing the store, so
    a prefix unique on one board is answered ambiguous because another board has
    a session starting the same way. Scoping the lookup fixes it, and an exact
    id from another board does not leak in.
    """
    from aisquare.core.store import AmbiguousIdError

    mine = team_project(work_dir)
    other = team_project(work_dir / "elsewhere")
    with store_session() as store:
        store.ensure_project(mine)
        store.ensure_project(other)
        _session(store, "bbbb2222-0000-0000-0000-000000000000", mine.id, role="coder")
        _session(store, "bbbb3333-0000-0000-0000-000000000000", other.id, role="coder")

        # `bbbb` is ambiguous across the whole store (two boards match)...
        with pytest.raises(AmbiguousIdError):
            store.get_session("bbbb")
        # ...but unique on each board, so the scoped resolver answers it.
        row = store.get_session_in_project(mine.id, "bbbb")
        assert row is not None and row.project_id == mine.id, "unique on this board — resolves"
        assert row.id == "bbbb2222-0000-0000-0000-000000000000"
        # The other board's exact id does not belong to this one.
        assert store.get_session_in_project(mine.id, "bbbb3333-0000-0000-0000-000000000000") is None
        # A true within-board tie still raises.
        _session(store, "bbbb2244-0000-0000-0000-000000000000", mine.id, role="coder")
        with pytest.raises(AmbiguousIdError):
            store.get_session_in_project(mine.id, "bbbb22")
