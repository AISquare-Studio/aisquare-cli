"""Projected snapshots and events, validated against the pinned contract.

The schemas here are the vendored 1.4 artifact under
``tests/fixtures/office-contract/``, pinned by digest. Nothing fetches anything:
a test that resolved a moving branch would go green against a contract nobody
agreed to. Validation runs through a real JSON Schema validator rather than a
second reading of the rules in Python, and the schemas are strict —
``additionalProperties: false`` — so a field this projector invents fails here
rather than reaching a browser.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from functools import cache, lru_cache
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from aisquare.office.models import EVENT_MODELS, OfficeModel
from aisquare.office.projector import OfficeProjector
from tests.office.test_projection import (
    NOW,
    batch,
    evidence,
    fleet_row,
    pane,
    session,
    signal_event,
    task,
)

ARTIFACT = Path(__file__).resolve().parents[1] / "fixtures" / "office-contract"
SCHEMAS = ARTIFACT / "schema"


@cache
def schema(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((SCHEMAS / f"{name}.json").read_text(encoding="utf-8"))
    return data


@lru_cache(maxsize=1)
def registry() -> Registry:
    """Every vendored schema by ``$id``, so a cross-file ``$ref`` never hits the network."""
    built: Registry = Registry()
    for path in sorted(SCHEMAS.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        built = built.with_resource(
            document["$id"], Resource.from_contents(document, default_specification=DRAFT202012)
        )
    return built


def errors_for(instance: object, name: str) -> list[str]:
    """Every schema violation, as strings.

    The validator is handed a bare ``$ref`` into the registry rather than the
    extracted document: extracting it looks equivalent and is not, because a
    local pointer such as ``#/$defs/Id`` would then resolve against a fragment
    with no ``$defs`` and every cross-reference inside the file would fail.
    """
    validator = Draft202012Validator({"$ref": schema(name)["$id"]}, registry=registry())
    return sorted(str(error) for error in validator.iter_errors(instance))


def agents_of(wire: Mapping[str, object]) -> list[dict[str, Any]]:
    """The snapshot's agent objects, typed for the assertions below.

    ``to_wire`` is deliberately typed ``dict[str, object]`` — it is an allow-list
    serializer, not a schema — so the narrowing happens here once rather than as
    an ignore comment at every assertion.
    """
    agents = wire["agents"]
    assert isinstance(agents, list)
    return cast(list[dict[str, Any]], agents)


def projected() -> tuple[OfficeProjector, Any, Any]:
    """Two projections that between them exercise most of the wire surface."""
    projector = OfficeProjector()
    before = projector.project(
        batch(
            fleet=(
                fleet_row(joined=session(state="attention")),
                fleet_row(
                    "agt_rook",
                    label="rook",
                    pane_id="%21",
                    session_id="ses_rook",
                    joined=session("ses_rook", model="claude-sonnet-5"),
                ),
            ),
            panes=(pane(), pane("agt_rook")),
            prompts=(evidence(),),
            tasks=(task("tsk_02be", claimed_by="agt_nova", status="doing"),),
            events=(signal_event("off", seq=700),),
        ),
        None,
        NOW,
    )
    after = projector.project(
        batch(
            fleet=(
                fleet_row(joined=session(state="working")),
                fleet_row(
                    "agt_rook",
                    label="rook",
                    pane_id="%21",
                    session_id="ses_rook",
                    joined=session("ses_rook", ended_at=NOW - timedelta(minutes=2)),
                ),
            ),
            panes=(pane(), pane("agt_rook", health="dead", exit_status=1)),
            tasks=(task("tsk_02be", claimed_by="agt_nova", status="done"),),
            events=(signal_event("on", seq=701),),
        ),
        before,
        NOW,
    )
    return projector, before, after


def test_a_projected_snapshot_satisfies_the_pinned_snapshot_schema() -> None:
    _projector, before, after = projected()

    assert errors_for(before.snapshot.to_wire(), "snapshot") == []
    assert errors_for(after.snapshot.to_wire(), "snapshot") == []


def test_every_projected_event_satisfies_the_pinned_event_schema() -> None:
    """Each event separately, so a failure names the variant that broke."""
    projector, before, after = projected()

    events = projector.diff(before, after)
    assert events, "the fixture produced no events to validate"
    for event in events:
        assert errors_for(event.to_wire(), "event") == [], event.kind


def test_the_projection_emits_only_event_variants_the_schema_knows() -> None:
    """No historical ``decision``/``multi`` shape, and no invented kind."""
    projector, before, after = projected()

    known = {model.model_fields["kind"].default for model in EVENT_MODELS}
    produced = {event.kind for event in projector.diff(before, after)}

    assert produced, "nothing was produced to compare"
    assert produced <= known, f"unknown event kinds: {sorted(produced - known)}"


def test_an_ended_row_validates_and_stays_out_of_the_queue() -> None:
    """The retained row is a full, schema-valid Agent — not a stub."""
    _projector, _before, after = projected()

    wire = after.snapshot.to_wire()
    ended = [agent for agent in wire["agents"] if agent["state"] == "ended"]

    assert len(ended) == 1
    assert errors_for(wire, "snapshot") == []
    assert ended[0]["id"] not in wire["queue"]
    assert ended[0]["ended_reason"] == "exit"


def test_a_question_without_tool_or_summary_still_satisfies_the_schema() -> None:
    """The fields this CLI cannot source are optional, so absence is valid.

    Asserted against the schema rather than against this back-end's opinion:
    the point is that omitting them is *contract-legal*, not merely our choice.
    """
    projector = OfficeProjector()
    projection = projector.project(
        batch(
            fleet=(fleet_row(joined=session(state="attention")),),
            panes=(pane(),),
            prompts=(evidence(),),
        ),
        None,
        NOW,
    )

    wire = projection.snapshot.to_wire()
    question = agents_of(wire)[0]["question"]

    assert errors_for(wire, "snapshot") == []
    assert "tool" not in question
    assert "summary" not in question


def test_a_self_session_without_options_satisfies_the_schema() -> None:
    projector = OfficeProjector()
    projection = projector.project(
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
        ),
        None,
        NOW,
    )

    wire = projection.snapshot.to_wire()

    assert errors_for(wire, "snapshot") == []
    assert "options" not in agents_of(wire)[0]["question"]
    assert agents_of(wire)[0]["origin"] == "self"


def test_the_snapshot_carries_no_key_the_schema_does_not_declare() -> None:
    """The strict-mode check, stated as a set difference rather than trusted.

    ``additionalProperties: false`` already rejects an extra key, but this names
    which one, and it also catches the reverse mistake of a required key going
    missing while some other error masks it.
    """
    _projector, _before, after = projected()
    declared = set(schema("snapshot")["properties"])
    required = set(schema("snapshot")["required"])

    wire = after.snapshot.to_wire()

    assert set(wire) <= declared, f"undeclared keys: {sorted(set(wire) - declared)}"
    assert required <= set(wire), f"missing required keys: {sorted(required - set(wire))}"


def test_no_projected_value_carries_a_filesystem_path_or_pane_target() -> None:
    """``Project.root`` is the one path the contract carries. Nothing else is.

    A transcript path, a cwd or a tmux socket reaching a browser is the leak this
    whole chain is built to prevent, so it is asserted on the serialized bytes
    rather than on the intent of the mapping.
    """
    _projector, _before, after = projected()
    wire = after.snapshot.to_wire()
    roots = {project["root"] for project in wire["projects"]}

    blob = json.dumps({key: value for key, value in wire.items() if key != "projects"})

    for needle in ("/code/aisquare-cli", ".jsonl", "tmux", "asq-socket"):
        assert needle not in blob, f"{needle!r} reached the wire"
    assert roots == {"/code/aisquare-cli"}


def test_the_wire_surface_round_trips_through_json() -> None:
    """What P05 will actually write to the stream: JSON, not Python objects."""
    projector, before, after = projected()

    payload = json.loads(json.dumps(after.snapshot.to_wire()))
    events = [json.loads(json.dumps(event.to_wire())) for event in projector.diff(before, after)]

    assert errors_for(payload, "snapshot") == []
    assert all(errors_for(event, "event") == [] for event in events)
    assert isinstance(payload["seq"], int)


def test_every_event_model_this_module_emits_is_an_office_model() -> None:
    """Events are allow-list serialized, never ``model_dump``ed."""
    projector, before, after = projected()

    for event in projector.diff(before, after):
        assert isinstance(event, OfficeModel), event
        assert set(event.to_wire()) <= event.wire_fields()


def test_a_naive_timestamp_never_reaches_the_wire() -> None:
    """Every emitted timestamp carries an offset, which the pattern enforces."""
    _projector, _before, after = projected()
    wire = after.snapshot.to_wire()

    taken_at = datetime.fromisoformat(str(wire["taken_at"]))

    assert taken_at.tzinfo is not None
    assert taken_at.utcoffset() == UTC.utcoffset(None)
    for agent in wire["agents"]:
        assert datetime.fromisoformat(str(agent["last_seen_at"])).tzinfo is not None
