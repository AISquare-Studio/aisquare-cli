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
        {"t": "transcript", "session": "ses_1", "seq": 41, "text": "hello", "final": True},
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
            "summary": "note wiring JWT",
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


def test_the_schema_says_which_close_code_must_not_be_retried() -> None:
    """4401 is the one close a client must not reconnect through.

    Every other close this server can produce is a transport close, where
    reconnecting with backoff is correct — and reconnect-on-close is what the
    client is specified to do. A client author who cannot tell the two apart
    from the schema has to guess, and the guess that costs nothing to write is
    an infinite retry loop against a token that will never be accepted.
    """
    codes = wire.schema_document()["closeCodes"]
    entry = codes[str(wire.CLOSE_AUTH_FAILED)]
    assert wire.CLOSE_AUTH_FAILED == 4401
    assert entry["retry"] is False, "the machine-readable half is what a client branches on"
    assert "transport close" in entry["description"], "and it must say what the others are"


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


def test_summary_is_at_most_six_words_of_the_latest_board_event(work_dir: Path) -> None:
    project = team_project(work_dir)
    with store_session() as store:
        store.ensure_project(project)
        _session(store, CODER, project.id, role="coder")
        _event(store, project.id, CODER, "note", "one two three four five six seven eight")
        _event(store, project.id, CODER, "result", "the latest event is the one that shows")
        snapshot = projector.snapshot(store, project.id)
    summary = snapshot.sessions[0].summary
    assert summary.split()[0] == "result", "the newest event wins"
    # Six, the literal from plan §5 — not `projector.SUMMARY_WORDS`. Asserting
    # against the constant would make this test agree with any value the
    # constant is later given, which is the one thing it exists to prevent.
    assert len(summary.split()) <= 6, summary
    assert projector.SUMMARY_WORDS == 6, "the cap is the plan's number, not a preference"


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
