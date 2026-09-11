"""The Python types must agree with the pinned contract, field for field.

The artifact under ``tests/fixtures/office-contract/`` is P00's frozen pack at
contract revision 1.4, vendored whole and pinned by digest. Nothing here
fetches anything: a test that resolved a moving branch would go green against a
contract nobody agreed to, which is the failure this file exists to make
impossible.

Agreement is asserted three ways, because each catches what the others miss:

1. **The artifact is the one that was accepted** — every vendored file hashes
   to what ``manifest.json`` says, and the manifest's own digest recomputes to
   the value the coordinator pinned.
2. **Every model's wire surface equals its schema's** — ``__wire_required__``
   is exactly the schema's ``required`` list and the full wire surface is
   exactly its ``properties``. A field added to a model without a schema
   change, or the reverse, fails here.
3. **The contract's own examples round-trip** — the fixtures P00 ships are
   parsed by these models and re-emitted, and the result validates against the
   schemas with a real JSON Schema validator rather than against a second
   reading of the rules in Python.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from aisquare.office import models
from aisquare.office.models import (
    EVENT_MODELS,
    Agent,
    ApiError,
    Capabilities,
    ContextPreview,
    Event,
    Feature,
    InsightSummary,
    InterruptRequest,
    KeysRequest,
    Operation,
    OperationReservation,
    OperationTarget,
    Page,
    PageCoverage,
    PlatformBinding,
    Project,
    ProjectContext,
    ProvenanceSummary,
    Question,
    Receipt,
    ResolveAllRequest,
    ResolveRequest,
    RespawnRequest,
    RunDetail,
    RunJoin,
    RunSummary,
    ServiceError,
    ServiceResult,
    ServicesDocument,
    Snapshot,
    SpawnRequest,
    StopRequest,
    Task,
    TeachAck,
    TeachRequest,
    TeamOSMeta,
    TeamOSView,
    TellRequest,
    TransportResponse,
    TransportResult,
    service_failed,
    service_ok,
)

ARTIFACT = Path(__file__).resolve().parents[1] / "fixtures" / "office-contract"
SCHEMAS = ARTIFACT / "schema"

#: ``manifest.json`` records paths relative to the Office repository root. The
#: pack is vendored here with that structure flattened, so the digest can be
#: recomputed offline from exactly the bytes that were hashed there.
_PREFIXES = (
    ("docs/contract/schema/", "schema/"),
    ("docs/contract/fixtures/", "fixtures/"),
    ("web/fixtures/", "web/fixtures/"),
)


def _local(manifest_path: str) -> Path:
    for prefix, local in _PREFIXES:
        if manifest_path.startswith(prefix):
            return ARTIFACT / (local + manifest_path[len(prefix) :])
    raise AssertionError(f"manifest path outside the vendored pack: {manifest_path}")


@lru_cache(maxsize=1)
def manifest() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((ARTIFACT / "manifest.json").read_text(encoding="utf-8"))
    return data


@cache
def schema(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((SCHEMAS / f"{name}.json").read_text(encoding="utf-8"))
    return data


@lru_cache(maxsize=1)
def registry() -> Registry:
    """Every vendored schema by ``$id``, so cross-file ``$ref`` never hits the network."""
    built: Registry = Registry()
    for path in sorted(SCHEMAS.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        built = built.with_resource(
            document["$id"], Resource.from_contents(document, default_specification=DRAFT202012)
        )
    return built


def subschema(name: str, *pointer: str) -> dict[str, Any]:
    """A ``$defs`` entry (or the document root when no pointer is given)."""
    node = schema(name)
    for part in pointer:
        node = node["$defs"][part] if part != "#" else node
    return node


def validate(instance: object, name: str, *pointer: str) -> None:
    """Validate against the vendored schema, resolving refs through the registry.

    The schema handed to the validator is a bare ``$ref`` into the registry
    rather than the extracted subschema. Extracting it and re-labelling it with
    the document's ``$id`` looks equivalent and is not: a local pointer such as
    ``#/$defs/Id`` would then resolve against the *fragment*, which has no
    ``$defs``, and every cross-reference inside the document would fail to
    resolve. Going through the ref keeps the parent document as the base.
    """
    location = schema(name)["$id"]
    if pointer:
        location = f"{location}#/$defs/{'/'.join(pointer)}"
    errors = sorted(
        Draft202012Validator({"$ref": location}, registry=registry()).iter_errors(instance),
        key=str,
    )
    assert not errors, "\n  ".join([f"does not satisfy {name}{list(pointer)}:", *map(str, errors)])


# --------------------------------------------------------------------------
# 1. The artifact is the one that was accepted
# --------------------------------------------------------------------------


def test_artifact_digest_is_the_pinned_one() -> None:
    """Recompute the manifest digest from the vendored bytes.

    ``contract-pack.mjs`` hashes the canonical ``"<path>  <sha256>\\n"`` lines
    over its sorted file list, excluding the manifest itself. Reimplementing
    that here rather than trusting the recorded value is the whole point: it
    proves these files *are* the pack, not merely that a string was copied.
    """
    recorded = manifest()

    assert recorded["contract_revision"] == models.CONTRACT_REVISION == "1.4"
    assert recorded["algorithm"] == "sha256"
    assert recorded["file_count"] == models.CONTRACT_ARTIFACT_FILE_COUNT == 36
    assert len(recorded["files"]) == recorded["file_count"]

    canonical = "".join(f"{entry['path']}  {entry['sha256']}\n" for entry in recorded["files"])
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    assert digest == recorded["digest"]
    assert digest == models.CONTRACT_ARTIFACT_SHA256


def test_artifact_every_vendored_file_matches_its_recorded_hash() -> None:
    """A file edited in place after vendoring must fail loudly, not drift quietly."""
    for entry in manifest()["files"]:
        path = _local(entry["path"])
        assert path.is_file(), f"vendored pack is missing {entry['path']}"
        payload = path.read_bytes()
        assert len(payload) == entry["bytes"], entry["path"]
        assert hashlib.sha256(payload).hexdigest() == entry["sha256"], entry["path"]


# --------------------------------------------------------------------------
# 2. Every model's wire surface equals its schema's
# --------------------------------------------------------------------------

#: model → (schema file, ``$defs`` pointer). An empty pointer means the root.
MODEL_SCHEMAS: tuple[tuple[type[models.OfficeModel], str, tuple[str, ...]], ...] = (
    (Snapshot, "snapshot", ()),
    (models.Project, "snapshot", ("Project",)),
    (models.Question, "snapshot", ("Question",)),
    (models.QuestionOption, "snapshot", ("QuestionOption",)),
    (models.AskQuestion, "snapshot", ("AskQuestion",)),
    (models.Agent, "snapshot", ("Agent",)),
    (models.Task, "snapshot", ("Task",)),
    (models.Turn, "snapshot", ("Turn",)),
    (models.Worktree, "snapshot", ("Worktree",)),
    (models.Stats, "snapshot", ("Stats",)),
    (models.TokenCounts, "snapshot", ("TokenCounts",)),
    (models.Cost, "snapshot", ("Cost",)),
    (models.OfficeCost, "snapshot", ("OfficeCost",)),
    (models.HistoryItem, "snapshot", ("HistoryItem",)),
    (models.Diff, "snapshot", ("Diff",)),
    (models.Receipt, "receipt", ()),
    (models.ApiError, "error", ()),
    (models.Plan, "plan", ()),
    (models.AgentHistory, "history", ()),
    (models.AgentOutput, "output", ()),
    (models.ServiceError, "service", ("ServiceError",)),
    (models.PageCoverage, "service", ("PageCoverage",)),
    (models.Operation, "operation", ()),
    (models.OperationTarget, "operation", ("OperationTarget",)),
    (models.Capabilities, "capabilities", ()),
    (models.Feature, "capabilities", ("Feature",)),
    (models.Provider, "capabilities", ("Provider",)),
    (models.Service, "capabilities", ("Service",)),
    (models.ServicesDocument, "services", ()),
    (models.ResolveRequest, "request", ("ResolveRequest",)),
    (models.ResolveAllRequest, "request", ("ResolveAllRequest",)),
    (models.TellRequest, "request", ("TellRequest",)),
    (models.KeysRequest, "request", ("KeysRequest",)),
    (models.InterruptRequest, "request", ("InterruptRequest",)),
    (models.StopRequest, "request", ("StopRequest",)),
    (models.SpawnRequest, "request", ("SpawnRequest",)),
    (models.RespawnRequest, "request", ("RespawnRequest",)),
    (models.TeamOSMeta, "team-os", ("TeamOSMeta",)),
    (models.TeamOSRosterEntry, "team-os", ("TeamOSRosterEntry",)),
    (models.TeamOSViewSection, "team-os", ("TeamOSViewSection",)),
    (models.TeamOSView, "team-os", ("TeamOSView",)),
    (models.CliContext, "project-context", ("CliContext",)),
    (models.ContextSection, "project-context", ("ContextSection",)),
    (models.ProjectContext, "project-context", ("ProjectContext",)),
    (models.RunSummary, "explainability", ("RunSummary",)),
    (models.RunDetail, "explainability", ("RunDetail",)),
    (models.PolicyRow, "explainability", ("PolicyRow",)),
    (models.PolicySummary, "explainability", ("PolicySummary",)),
    (models.ReasoningSection, "explainability", ("ReasoningSection",)),
    (models.ReasoningSummary, "explainability", ("ReasoningSummary",)),
    (models.InsightSummary, "praxis", ("InsightSummary",)),
    (models.ContextPreview, "praxis", ("ContextPreview",)),
    (models.InjectionRecord, "praxis", ("InjectionRecord",)),
    (models.ProvenanceNode, "praxis", ("ProvenanceNode",)),
    (models.ProvenanceEdge, "praxis", ("ProvenanceEdge",)),
    (models.ProvenanceSummary, "praxis", ("ProvenanceSummary",)),
    (models.TeachRequest, "praxis", ("TeachRequest",)),
    (models.TeachEvidence, "praxis", ("TeachEvidence",)),
)


@pytest.mark.parametrize(
    ("model", "document", "pointer"),
    MODEL_SCHEMAS,
    ids=[f"{model.__name__}" for model, _, _ in MODEL_SCHEMAS],
)
def test_strict_wire_surface_matches_the_schema(
    model: type[models.OfficeModel], document: str, pointer: tuple[str, ...]
) -> None:
    """Required keys and the whole property set, compared to the frozen schema."""
    node = subschema(document, *pointer)

    assert set(model.__wire_required__) == set(node.get("required", []))
    assert model.wire_fields() == set(node.get("properties", {}))
    assert node.get("additionalProperties") is False or not node.get("properties"), (
        f"{model.__name__}'s schema is expected to be strict"
    )


def test_strict_every_stream_event_kind_is_modelled_once() -> None:
    """The 27 variants of ``event.json``, no more and no fewer."""
    by_title = {variant["title"]: variant for variant in schema("event")["oneOf"]}
    modelled = {str(model.model_fields["kind"].default) for model in EVENT_MODELS}

    assert modelled == set(by_title)
    assert len(EVENT_MODELS) == len(by_title) == 27

    for model in EVENT_MODELS:
        variant = by_title[str(model.model_fields["kind"].default)]
        assert set(model.__wire_required__) == set(variant["required"])
        assert model.wire_fields() == set(variant["properties"])


def test_strict_an_undocumented_property_is_rejected_not_ignored() -> None:
    """``extra="forbid"`` is the chosen policy, and it is the schemas' policy.

    A security or semantic invariant must never be inferred from an unknown
    field, so the rejection is asserted rather than assumed.
    """
    with pytest.raises(ValidationError):
        Project(id="p1", name="office", root="/srv/office", surprise=True)  # type: ignore[call-arg]

    with pytest.raises(ValidationError):
        ResolveRequest(prompt_id="pr_1", answer="y")  # type: ignore[call-arg]


def test_strict_models_are_frozen_so_one_snapshot_can_be_shared() -> None:
    project = Project(id="p1", name="office", root="/srv/office")

    with pytest.raises(ValidationError):
        project.name = "renamed"  # type: ignore[misc]


def test_strict_a_naive_timestamp_is_refused() -> None:
    """Naive means "some zone"; the freshness arithmetic needs a real offset."""
    with pytest.raises(ValidationError):
        Receipt(delivered="typed", detail="typed y", at=datetime(2026, 9, 11, 8, 41))


def test_strict_serialization_omits_an_unset_optional_but_keeps_an_explicit_null() -> None:
    """The distinction a blanket ``model_dump()`` cannot express.

    ``Cost.by_tool[].avg_ms`` is typed ``number`` with no null member, so an
    unset value must be absent; ``ServiceResult.data`` is required, so a null
    must be present. Both appear here.
    """
    row = models.CostByTool(tool="Bash", calls=3, resp_tokens=120)
    assert row.to_wire() == {"tool": "Bash", "calls": 3, "resp_tokens": 120}

    failed: ServiceResult[RunSummary] = service_failed(
        "unavailable", ServiceError(code="service_unavailable", detail="peer down", retryable=True)
    )
    assert failed.to_wire()["data"] is None
    assert "data" in failed.to_wire()


def test_strict_the_only_serialization_is_the_allow_list() -> None:
    """``to_wire`` recurses through nested allow-lists rather than dumping."""
    counts = models.TokenCounts(**{"in": 10, "out": 5, "cache_write": 1, "cache_read": 2})

    assert counts.to_wire() == {"in": 10, "out": 5, "cache_write": 1, "cache_read": 2}


# --------------------------------------------------------------------------
# 3. The contract's own examples round-trip
# --------------------------------------------------------------------------


def _normalise(value: object) -> object:
    """Compare payloads without tripping over timestamp spelling.

    ``2026-09-11T08:41:03.512+00:00`` and ``...512000+00:00`` are the same
    instant and both match the contract's pattern; only one of them survives a
    trip through :class:`datetime`.
    """
    if isinstance(value, dict):
        return {key: _normalise(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalise(item) for item in value]
    if isinstance(value, str) and len(value) >= 20:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return value
    return value


def test_the_contract_snapshot_fixture_round_trips_through_the_models() -> None:
    """P00's own example parses, re-emits and still validates.

    This is the check that would catch a model which is merely *self*-
    consistent: the payload is the frontend's fixture, written against the
    schemas by another packet.
    """
    payload = json.loads(
        (ARTIFACT / "web" / "fixtures" / "snapshot.json").read_text(encoding="utf-8")
    )

    snapshot = Snapshot.model_validate(payload)
    emitted = snapshot.to_wire()

    validate(emitted, "snapshot")
    assert _normalise(emitted) == _normalise(payload)


def test_the_contract_event_fixtures_round_trip_through_the_event_union() -> None:
    """Every line of ``events.jsonl``, through the discriminated union."""
    adapter: TypeAdapter[Any] = TypeAdapter(Event)
    lines = (ARTIFACT / "web" / "fixtures" / "events.jsonl").read_text(encoding="utf-8")

    seen: set[str] = set()
    for line in lines.splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        event = adapter.validate_python(payload)
        emitted = event.to_wire()

        validate(emitted, "event")
        assert _normalise(emitted) == _normalise(payload), payload["kind"]
        seen.add(payload["kind"])

    assert seen, "the vendored events fixture is empty"


# --------------------------------------------------------------------------
# The service envelope
# --------------------------------------------------------------------------


NOW = datetime(2026, 9, 11, 8, 41, 3, tzinfo=UTC)


def _run() -> RunSummary:
    return RunSummary(
        run_id="run_1",
        workspace_id="ws_1",
        studio_id=None,
        stable_agent_id=None,
        local_agent_id=None,
        join="unjoined",
        state="running",
        upstream_state_label="IN_PROGRESS",
        started_at=NOW,
        updated_at=NOW,
        platform_usd=None,
        duration_s=None,
    )


def test_service_result_round_trips_data_null_and_error() -> None:
    page = Page[RunSummary](items=(_run(),), next_cursor=None, partial=False, coverage=None)
    result = service_ok(page, observed_at=NOW)

    wire = result.to_wire()
    assert set(wire) == {"status", "data", "stale", "observed_at", "last_success_at", "error"}
    assert wire["error"] is None
    validate(wire, "explainability", "RunsResult")


def test_service_a_null_payload_is_explicit_and_validates() -> None:
    result: ServiceResult[Page[RunSummary]] = service_failed(
        "unavailable",
        ServiceError(code="service_unavailable", detail="the peer did not answer", retryable=True),
    )

    wire = result.to_wire()
    assert wire["data"] is None
    assert wire["observed_at"] is None
    validate(wire, "explainability", "RunsResult")


def test_service_status_ok_requires_data_and_no_error() -> None:
    with pytest.raises(ValidationError):
        ServiceResult[RunSummary](status="ok", data=None, stale=False)

    with pytest.raises(ValidationError):
        ServiceResult[RunSummary](
            status="ok",
            data=_run(),
            error=ServiceError(code="internal", detail="x", retryable=False),
            observed_at=NOW,
        )


def test_service_a_failed_status_must_carry_an_error() -> None:
    with pytest.raises(ValidationError):
        ServiceResult[RunSummary](status="unavailable", data=None, error=None)


@pytest.mark.parametrize("status", ["unauthorized", "forbidden", "unconfigured"])
def test_service_losing_authorisation_drops_the_cached_body(status: str) -> None:
    """A revoked binding must never keep painting a previously authorised
    workspace's data. That is an invariant the JSON Schema cannot state."""
    error = ServiceError(code="unauthorized", detail="the key was revoked", retryable=False)

    with pytest.raises(ValidationError):
        ServiceResult[RunSummary](
            status=status,
            data=_run(),
            error=error,
            observed_at=NOW,
        )

    with pytest.raises(ValidationError):
        ServiceResult[RunSummary](status=status, data=None, stale=True, error=error)


def test_service_stale_data_is_only_possible_while_available() -> None:
    error = ServiceError(code="timeout", detail="slow", retryable=True)

    with pytest.raises(ValidationError):
        ServiceResult[RunSummary](status="unavailable", data=None, stale=True, error=error)


def test_service_page_coverage_never_invents_a_total() -> None:
    """An upstream that reports no total yields null, not a count of what was
    fetched — and ``partial`` is about a failed scope, not about more rows."""
    coverage = PageCoverage(
        reported_total=None,
        total_is_exact=None,
        reachable_scope_count=2,
        failed_scope_count=1,
        omitted_scope_count=0,
    )
    page = Page[RunSummary](items=(_run(),), next_cursor="abc", partial=True, coverage=coverage)

    wire = page.to_wire()
    assert wire["coverage"]["reported_total"] is None  # type: ignore[index]
    assert wire["partial"] is True
    validate(wire, "explainability", "RunPage")


def test_service_a_domain_state_is_not_an_availability_state() -> None:
    """``processing`` lives in the data model; it is a successful read."""
    summary = models.PolicySummary(state="processing", rows=())
    result = service_ok(summary, observed_at=NOW)

    assert result.status == "ok"
    validate(result.to_wire(), "explainability", "PolicyResult")


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------


def _operation(status: models.OperationStatus, **extra: Any) -> Operation:
    return Operation(
        operation_id="op_1",
        kind="agent.tell",
        target=OperationTarget(agent_id="ses_1"),
        status=status,
        submitted_at=NOW,
        updated_at=NOW + timedelta(seconds=1),
        **extra,
    )


@pytest.mark.parametrize("status", ["queued", "running", "succeeded", "failed", "outcome_unknown"])
def test_operation_every_status_validates(status: models.OperationStatus) -> None:
    extra: dict[str, Any] = {}
    if status == "succeeded":
        extra["receipt"] = Receipt(delivered="typed", detail="typed into pane %14")
    if status in ("failed", "outcome_unknown"):
        extra["error"] = ServiceError(
            code="outcome_unknown", detail="the keys may have landed", retryable=False
        )

    operation = _operation(status, **extra)
    wire = operation.to_wire()

    assert set(wire) >= {"operation_id", "kind", "target", "status", "receipt", "error"}
    validate(wire, "operation")


def test_operation_an_unknown_outcome_never_carries_a_receipt() -> None:
    """Inventing one would claim a delivery nobody observed."""
    with pytest.raises(ValidationError):
        _operation("outcome_unknown", receipt=Receipt(delivered="typed", detail="typed"))


def test_operation_a_pending_status_carries_no_error() -> None:
    with pytest.raises(ValidationError):
        _operation("queued", error=ServiceError(code="internal", detail="x", retryable=False))


def test_operation_a_receipt_and_error_are_nullable_and_always_present() -> None:
    wire = _operation("queued").to_wire()

    assert wire["receipt"] is None
    assert wire["error"] is None


def test_operation_target_carries_only_resolved_local_ids() -> None:
    """No pane id, no tmux target, no path, no part of the body."""
    assert set(OperationTarget.wire_fields()) == {"project_id", "agent_id"}

    with pytest.raises(ValidationError):
        OperationTarget(pane_id="%14")  # type: ignore[call-arg]


def test_operation_reservation_reports_a_conflict_without_a_result() -> None:
    """A reused key with a different fingerprint has nothing to return."""
    reservation = OperationReservation(disposition="conflict")
    assert reservation.operation is None

    with pytest.raises(ValueError, match="conflicting reservation"):
        OperationReservation(disposition="conflict", operation=_operation("queued"))

    with pytest.raises(ValueError, match="must carry its Operation"):
        OperationReservation(disposition="new")


def test_operation_kind_stays_exactly_the_schema_and_legacy_kinds_stay_out() -> None:
    """``OperationKind`` is the wire enum; retained legacy mutations never
    become an Operation a browser can poll."""
    from typing import get_args

    assert set(get_args(models.OperationKind)) == set(
        subschema("operation", "OperationKind")["enum"]
    )
    assert not set(get_args(models.LegacyActionKind)) & set(get_args(models.OperationKind))

    # Constructed, not copied: ``model_copy`` does not re-validate, so a test
    # written that way would pass whatever the model does.
    with pytest.raises(ValidationError):
        Operation(
            operation_id="op_1",
            kind="agent.note",
            target=OperationTarget(agent_id="ses_1"),
            status="queued",
            submitted_at=NOW,
            updated_at=NOW,
        )


# --------------------------------------------------------------------------
# Capabilities
# --------------------------------------------------------------------------


def _capabilities() -> Capabilities:
    return Capabilities(
        contract_revision="1.4",
        supported_contract_revisions=("1.3", "1.4"),
        artifact_revision=models.CONTRACT_ARTIFACT_SHA256,
        features=tuple(
            Feature(id=feature_id, enabled=True, reason=None) for feature_id in models.FEATURE_IDS
        ),
        providers=(
            models.Provider(
                id="claude", observation="hooks_and_pane", structured_input=True, reason=None
            ),
        ),
        services=tuple(
            models.Service(id=service_id, status="ok", detail=None, checked_at=NOW)
            for service_id in models.SERVICE_IDS
        ),
    )


def test_capabilities_lists_every_feature_and_service_exactly_once() -> None:
    validate(_capabilities().to_wire(), "capabilities")

    complete = _capabilities()
    with pytest.raises(ValidationError):
        Capabilities(
            contract_revision="1.4",
            supported_contract_revisions=("1.3", "1.4"),
            artifact_revision=None,
            features=tuple(complete.features[:-1]),
            providers=complete.providers,
            services=complete.services,
        )


def test_capabilities_a_disabled_feature_says_why_and_is_never_omitted() -> None:
    with pytest.raises(ValidationError):
        Feature(id="platform.teach", enabled=False, reason=None)

    disabled = Feature(id="platform.teach", enabled=False, reason="no platform binding configured")
    assert disabled.to_wire()["reason"]


def test_capabilities_reports_the_pinned_artifact_digest() -> None:
    assert _capabilities().to_wire()["artifact_revision"] == manifest()["digest"]


def test_services_document_probes_the_same_four_ids() -> None:
    document = ServicesDocument(
        services=tuple(
            models.Service(id=service_id, status="ok", detail=None, checked_at=NOW)
            for service_id in models.SERVICE_IDS
        ),
        checked_at=NOW,
    )

    wire = document.to_wire()
    assert [service.id for service in document.services] == list(models.SERVICE_IDS)
    validate(wire, "services")

    with pytest.raises(ValidationError):
        ServicesDocument(
            services=tuple(
                models.Service(id=service_id, status="ok", detail=None, checked_at=NOW)
                for service_id in models.SERVICE_IDS[:3]
            ),
            checked_at=NOW,
        )


# --------------------------------------------------------------------------
# Secrets and browser-supplied values
# --------------------------------------------------------------------------

_SECRET_WORDS = ("token", "secret", "password", "credential", "api_key", "apikey")

_MEASUREMENTS = frozenset({"token_count"})
"""Names that contain a secret-ish word but measure something.

``ContextPreview.token_count`` is a count of tokens in a text, not a
credential. Listed explicitly rather than loosening the scan, so a field named
``token`` still fails.
"""


@pytest.mark.parametrize(
    ("model", "document", "pointer"),
    MODEL_SCHEMAS,
    ids=[f"{model.__name__}" for model, _, _ in MODEL_SCHEMAS],
)
def test_secret_no_wire_model_has_a_credential_field(
    model: type[models.OfficeModel], document: str, pointer: tuple[str, ...]
) -> None:
    for name in model.wire_fields():
        lowered = name.lower()
        if lowered in _MEASUREMENTS:
            continue
        assert not any(word in lowered for word in _SECRET_WORDS), f"{model.__name__}.{name}"


def test_secret_the_platform_binding_holds_no_key_and_no_base_url() -> None:
    """Credentials are references resolved at request time; the normalised base
    URL belongs to the resolved profile, not to the binding."""
    binding = PlatformBinding(
        binding_id="b1",
        revision=3,
        project_id="prj_1",
        profile_name="prod",
        workspace_id="ws_1",
    )

    names = set(vars(binding)) if hasattr(binding, "__dict__") else set(binding.__slots__)
    assert names == {
        "binding_id",
        "revision",
        "project_id",
        "profile_name",
        "workspace_id",
        "studio_id",
        "agent_uid",
    }
    assert not any(word in name.lower() for name in names for word in _SECRET_WORDS)
    assert "base_url" not in names and "endpoint" not in names


def test_secret_an_action_spec_cannot_carry_raw_input() -> None:
    """Not a dict, not a string to run, not a browser callback."""
    spec = models.ActionSpec(
        kind="agent.tell",
        target=OperationTarget(agent_id="ses_1"),
        body=TellRequest(text="please rerun the tests"),
    )
    assert isinstance(spec.body, models.OfficeModel)

    with pytest.raises(TypeError):
        models.ActionSpec(
            kind="agent.tell",
            target=OperationTarget(agent_id="ses_1"),
            body={"text": "raw"},  # type: ignore[arg-type]
        )


def test_secret_a_terminal_target_names_no_pane_socket_or_path() -> None:
    target = models.TerminalTarget(agent_id="ses_1", generation=2)

    assert set(target.__slots__) == {"agent_id", "session_id", "generation"}


def test_secret_a_transport_result_carries_exactly_one_member() -> None:
    response = TransportResponse(
        status_code=200,
        allowed_headers={"content-type": "application/json"},
        json_body={},
        received_at=NOW,
    )

    assert TransportResult(response=response).error is None

    with pytest.raises(ValueError, match="exactly one"):
        TransportResult()
    with pytest.raises(ValueError, match="exactly one"):
        TransportResult(
            response=response,
            error=ServiceError(code="internal", detail="x", retryable=False),
        )


# --------------------------------------------------------------------------
# Contract invariants that live in the typed model
# --------------------------------------------------------------------------


def test_a_preview_is_a_candidate_and_has_no_run_id() -> None:
    """No ``run_id`` property exists, and none may be added: a preview carrying
    the session's run would read like an injection that happened."""
    assert "run_id" not in ContextPreview.wire_fields()

    preview = ContextPreview(
        context_text="lesson text",
        insight_ids=("ins_1",),
        token_count=None,
        studio_id="std_1",
        stable_agent_id=None,
    )
    assert preview.candidate_only is True

    with pytest.raises(ValidationError):
        ContextPreview(
            context_text="x",
            insight_ids=(),
            token_count=0,
            studio_id="std_1",
            stable_agent_id=None,
            run_id="run_1",  # type: ignore[call-arg]
        )


def test_an_injection_record_never_claims_a_model_consumed_anything() -> None:
    fields = models.InjectionRecord.wire_fields()

    assert "served_at" in fields
    assert not {"consumed", "applied", "used", "consumed_at"} & fields


def test_an_unjoined_run_names_no_local_agent() -> None:
    with pytest.raises(ValidationError):
        _run().model_copy(update={"local_agent_id": "ses_1"}).model_validate(
            _run().model_dump() | {"join": "unjoined", "local_agent_id": "ses_1"}
        )

    ambiguous = RunJoin(state="ambiguous", evidence="two candidates matched")
    assert ambiguous.wire_join == "unjoined"

    with pytest.raises(ValueError, match="only a joined RunJoin"):
        RunJoin(state="unjoined", run_id="run_1")


def test_a_teach_acknowledgement_is_not_a_lesson() -> None:
    ack = TeachAck(disposition="accepted", reference_id="sig_1")

    assert ack.disposition == "accepted"
    assert not hasattr(ack, "insight_id"), "ingestion is not lesson creation"


def test_a_null_cost_is_not_a_zero_cost() -> None:
    """Unknown stays None. Coercing it to 0 makes an unpriced run look free."""
    run = _run()

    assert run.platform_usd is None
    assert run.to_wire()["platform_usd"] is None
    assert run.duration_s is None


def test_a_question_may_carry_no_options_and_none_are_invented() -> None:
    """A self session has no pane to read labels from, so it has no options and
    the GUI must not render answer controls for it."""
    question = Question(kind="permission", text="Allow Bash?", detected_by="hook", tool="Bash")

    assert question.options is None
    assert "options" not in question.to_wire()
    validate(question.to_wire(), "snapshot", "Question")


def test_a_receipt_reports_only_what_was_observed_about_the_pane() -> None:
    """``closed`` is never a claim that the agent accepted the answer."""
    receipt = Receipt(
        delivered="typed", detail="typed y + Enter into pane %14", keys=("y", "Enter"), at=NOW
    )

    assert "closed" not in receipt.to_wire()
    validate(receipt.to_wire(), "receipt")

    observed = Receipt(delivered="board", detail="filed as a board note", closed=None)
    assert observed.closed is None


def test_the_api_error_shape_stays_distinct_from_the_service_error_shape() -> None:
    """``error.json`` is a non-2xx body; ``ServiceError`` lives inside a 200."""
    local = ApiError(error="stale_prompt", detail="the pane moved on")
    validate(local.to_wire(), "error")

    assert ApiError.wire_fields() == {"error", "detail"}
    assert ServiceError.wire_fields() == {"code", "detail", "retryable"}
    assert (
        local.to_wire().keys()
        != ServiceError(code="internal", detail="x", retryable=False).to_wire().keys()
    )


def test_request_bodies_reject_a_v1_shaped_resolve() -> None:
    """The v1 ``{answer}`` body is historical; a 1.4 call carrying it is refused
    rather than given a second interpretation."""
    with pytest.raises(ValidationError):
        ResolveRequest.model_validate({"answer": "y"})

    resolved = ResolveRequest(prompt_id="pr_1", option="allow")
    validate(resolved.to_wire(), "request", "ResolveRequest")


def test_resolve_all_is_permission_only_and_never_remembers() -> None:
    with pytest.raises(ValidationError):
        ResolveAllRequest.model_validate(
            {"kind": "plan", "option": "allow", "prompt_ids": ["pr_1"]}
        )

    with pytest.raises(ValidationError):
        ResolveAllRequest.model_validate(
            {"kind": "permission", "option": "allow-remember", "prompt_ids": ["pr_1"]}
        )

    sweep = ResolveAllRequest(kind="permission", option="deny", prompt_ids=("pr_1", "pr_2"))
    validate(sweep.to_wire(), "request", "ResolveAllRequest")


def test_bounded_request_bodies_have_the_schema_limits() -> None:
    with pytest.raises(ValidationError):
        KeysRequest(keys=tuple(f"k{i}" for i in range(33)))
    with pytest.raises(ValidationError):
        KeysRequest(keys=())
    with pytest.raises(ValidationError):
        TellRequest(text="")
    with pytest.raises(ValidationError):
        TellRequest(text="x" * 8001)

    validate(InterruptRequest(hard=True).to_wire(), "request", "InterruptRequest")
    validate(StopRequest().to_wire(), "request", "StopRequest")
    validate(SpawnRequest(role="coder", label="nova").to_wire(), "request", "SpawnRequest")
    validate(RespawnRequest().to_wire(), "request", "RespawnRequest")


def test_team_os_rows_are_labelled_as_peer_data() -> None:
    """A Team OS row can never be mistaken for local CLI fleet data."""
    meta = TeamOSMeta(available=False, views=(), display_name=None, peer_revision=None)

    assert meta.source == "team_os"
    validate(meta.to_wire(), "team-os", "TeamOSMeta")

    view = TeamOSView(name="overview", title="Overview", sections=())
    validate(view.to_wire(), "team-os", "TeamOSView")


def test_project_context_keeps_the_two_sources_separate() -> None:
    context = ProjectContext(
        project_id="prj_1",
        cli=models.CliContext(project_id="prj_1", sections=()),
        team_os=None,
    )

    wire = context.to_wire()
    assert wire["team_os"] is None
    assert wire["cli"]["source"] == "cli"  # type: ignore[index]
    validate(wire, "project-context", "ProjectContext")


def test_a_run_detail_lists_only_the_views_that_exist() -> None:
    detail = RunDetail(run=_run(), summary_text=None, available_details=("policies",))

    wire = detail.to_wire()
    assert wire["available_details"] == ["policies"], (
        "a run with no reasoning must not have the tab offered for it"
    )
    assert wire["summary_text"] is None
    validate(wire, "explainability", "RunDetail")

    # No raw trace tree is ever exposed, so no value names one.
    with pytest.raises(ValidationError):
        RunDetail(run=_run(), summary_text=None, available_details=("traces",))


def test_an_insight_and_its_provenance_stay_bounded() -> None:
    insight = InsightSummary(
        insight_id="ins_1",
        studio_id="std_1",
        stable_agent_id=None,
        status="active",
        text="prefer the narrow fix",
        summary=None,
        created_at=NOW,
        updated_at=None,
        source_reference=None,
    )
    wire = insight.to_wire()
    assert wire["updated_at"] is None, "unknown stays null rather than borrowing created_at"
    assert wire["source_reference"] is None
    validate(wire, "praxis", "InsightSummary")

    chain = ProvenanceSummary(insight_id="ins_1", nodes=(), edges=(), text=None)
    assert chain.to_wire()["nodes"] == []
    validate(chain.to_wire(), "praxis", "ProvenanceSummary")

    teach = TeachRequest(text="always run the scrubbed pytest", intent="convention")
    assert "evidence" not in teach.to_wire(), "an unset optional is absent, not null"
    validate(teach.to_wire(), "praxis", "TeachRequest")

    with pytest.raises(ValidationError):
        TeachRequest(text="x" * 8001, intent="convention")


def test_ids_are_opaque_and_an_empty_required_id_is_refused() -> None:
    with pytest.raises(ValidationError):
        Task(id="", project_id="prj_1", title="t", status="todo", needs=())

    with pytest.raises(ValidationError):
        Project(id="p1", name="", root="/srv")

    with pytest.raises(ValidationError):
        Project(id="p1", name="office", root="relative/path")


def test_an_agent_carries_its_v13_defaults_without_claiming_evidence() -> None:
    """``permission_mode: unknown`` says the footer was never read; ``health:
    unknown`` is what a self session always reports."""
    agent = Agent(
        id="ses_1",
        label="nova",
        project_id="prj_1",
        project_now="prj_1",
        projects=("prj_1",),
        role="coder",
        origin="self",
        state="working",
        activity="building",
        since_s=0,
        morale=50,
        model_family="opus",
        up_s=12,
        output_tail=(),
        last_seen_at=NOW,
        sub="none",
        subagents=0,
        permission_mode="unknown",
        health="unknown",
    )

    wire = agent.to_wire()
    assert wire["permission_mode"] == "unknown"
    assert "pane_id" not in wire
    assert "cost" not in wire
    validate(wire, "snapshot", "Agent")
