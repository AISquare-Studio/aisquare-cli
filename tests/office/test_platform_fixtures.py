"""The captured fixtures: deterministic, value-free, and still true of the code.

``tests/fixtures/office-platform/`` records what the deployed Explainability and
Praxis routes answer, as *shapes* — key names and type names, never values. Two
independent things are checked here, and they are what make the directory worth
trusting:

**It contains no data.** Every leaf of every recorded shape is one of a small
set of type tokens, and none of the synthetic bodies' own values appears
anywhere in the files. That is checked mechanically rather than by review,
because "I looked and there was no key in it" does not survive the next person
adding a family.

**It has not drifted from the code.** The shapes are re-derived here from the
sample bodies and compared; the recorded classification is re-derived from
:func:`classify_status`; and the capability list is compared against the tuple
the transport actually enforces. A fixture that quietly stops describing the
adapter is worse than no fixture, because P13 and P14 build fakes from it.

**No live call produced any of this.** P12 contacted neither deployed service.
The shapes come from source-mined evidence recorded in the Office repository,
each entry marked ``literal`` with a citation or ``inferred`` with its basis.
An ``inferred`` variant is a hypothesis for a future authorised preflight to
confirm, not a contract.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from aisquare.office.adapters.platform_transport import classify_status
from aisquare.office.platform_config import WORKSPACE_KEY_CAPABILITIES, route_capability
from aisquare.office.platform_redaction import body_shape

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "office-platform"

SHAPE_TOKENS = frozenset(
    {"null", "boolean", "integer", "number", "string", "absent", "empty", "truncated", "mixed"}
)
"""Everything a shape leaf is allowed to be. A value is not on this list."""


# --------------------------------------------------------------------------
# Synthetic bodies
#
# Every value below is invented. The KEYS, the TYPES and the NULLABILITY come
# from source-mined evidence recorded in the Office repository; the ids,
# timestamps, counts and costs do not, and must never be read as measurements.
# Nothing here was obtained from a live call — P12 made none.
#
# They live in the test rather than in the fixtures because a fixture carries a
# shape and a shape carries no values. Bodies here, shapes on disk: if either
# drifts, the re-derivation below stops matching.
#
# Fields that carry prompts, lesson prose or traces in the real API are
# bracketed placeholders, so even this block has nothing worth redacting.
# --------------------------------------------------------------------------

RUN_ITEM: dict[str, Any] = {
    "studio_id": "000",
    "run_id": "00000000000000000000000000000000",
    "status": "ready",
    "run_verdict": "completed",
    "agent_name": "synthetic-agent",
    "title": None,
    "decision": None,
    "customer_name": None,
    "customer_id": None,
    "session_id": None,
    "parent_run_id": None,
    "run_kind": "one_shot",
    "human_taught_count": None,
    "replay_mode": None,
    "replay_source_run_id": None,
    "adherence_policies_version_id": None,
    "domain": None,
    "started_at": "2000-01-01T00:00:00+00:00",
    "ended_at": "2000-01-01T00:00:00+00:00",
    "updated_at": "2000-01-01T00:00:00+00:00",
    "duration_ms": 0.0,
    "node_count": 0,
    "token_count": 0,
    "cost_usd": 0.0,
    "error_count": 0,
    "flagged_count": 0,
    "policy_passed_count": 0,
    "policy_failed_count": 0,
    "policy_skipped_count": 0,
    "policy_unverified_count": 0,
    "policy_needs_review_count": 0,
    "policy_blocking_count": 0,
    "policy_warning_count": 0,
    "policy_improving_count": 0,
    "runtime_blocked_count": 0,
    "runtime_improved_count": 0,
    "runtime_warned_count": 0,
    "runtime_decision_count": 0,
    "is_governed": True,
    "last_error": None,
    "summary_counts": {"spans": 0, "events": 0, "artifacts": 0, "policies": 0},
}

PROCESSING_RUN_ITEM: dict[str, Any] = {
    **RUN_ITEM,
    "status": "processing",
    "run_verdict": None,
    "duration_ms": None,
    "node_count": None,
    "token_count": None,
    "cost_usd": None,
    "error_count": None,
    "flagged_count": None,
    "is_governed": None,
    "summary_counts": None,
}
"""A row still in the pipeline: nullable metrics null rather than zero. The
gateway's own model comment records what fudging that cost — 83 matching runs
dropped from an errors query, and graph-less runs sorted as the fastest."""

WORKSPACE_RUNS_LIST: dict[str, Any] = {
    "status": "ok",
    "workspace_id": "000",
    "since": None,
    "total_count": 0,
    "studios_read": ["000"],
    "studios_failed": [],
    "studios_omitted": 0,
    "total_is_exact": True,
    "page_limit": 0,
    "runs_reachable": 0,
    "next_offset": None,
    "runs": [RUN_ITEM, PROCESSING_RUN_ITEM],
}

WORKSPACE_RUNS_EMPTY: dict[str, Any] = {
    **WORKSPACE_RUNS_LIST,
    "studios_read": [],
    "runs": [],
}
"""A workspace owning no studios: a success with nothing in it."""

WORKSPACE_RUNS_PARTIAL: dict[str, Any] = {
    **WORKSPACE_RUNS_LIST,
    "studios_failed": ["000"],
    "total_is_exact": False,
}
"""One studio failed and the rest answered — still a 200, still carrying the
runs that were read."""

INSIGHT_ROW: dict[str, Any] = {
    "id": "00000000-0000-4000-8000-000000000000",
    "studio_id": "000",
    "scope": "agent",
    "agent_uid": "00000000-0000-4000-8000-000000000000",
    "run_id": None,
    "text": "[placeholder: lesson prose, never captured]",
    "confidence": 0.0,
    "status": "active",
    "ttl_expires_at": None,
    "signal_ids": ["00000000-0000-4000-8000-000000000000"],
    "retired_reason": None,
    "source_ref": None,
    "created_at": "2000-01-01T00:00:00+00:00",
    "updated_at": "2000-01-01T00:00:00+00:00",
    "valid_from": "2000-01-01T00:00:00+00:00",
    "valid_to": None,
    "recorded_at": "2000-01-01T00:00:00+00:00",
    "supersedes_id": None,
    "root_id": "00000000-0000-4000-8000-000000000000",
    "is_current": True,
}

AGENT_INSIGHTS: dict[str, Any] = {
    "agent_uid": "00000000-0000-4000-8000-000000000000",
    "source": "praxis",
    "insights": [INSIGHT_ROW],
}

AGENT_INSIGHTS_EMPTY: dict[str, Any] = {
    "agent_uid": "00000000-0000-4000-8000-000000000000",
    "source": "praxis",
    "insights": [],
}
"""Indistinguishable at the wire from "this workspace owns no studios", which
short-circuits before Praxis is called. An empty list is therefore not evidence
that Praxis is reachable."""

PRAXIS_CONTEXT: dict[str, Any] = {
    "source": "praxis",
    "insights": [INSIGHT_ROW],
    "rule_ids": ["rule_000000"],
    "vault_excerpts": [],
    "tokens_used": 0,
    "bundle_id": "0000000000000000",
}

PRAXIS_CONTEXT_EMPTY: dict[str, Any] = {**PRAXIS_CONTEXT, "insights": [], "rule_ids": []}

DETAIL_STRING: dict[str, Any] = {"detail": "[placeholder: upstream sentence]"}
"""FastAPI's ordinary error body, where ``detail`` is a string."""

DETAIL_STRUCTURED: dict[str, Any] = {
    "detail": [{"loc": ["query", "sort"], "msg": "[placeholder: validation message]"}]
}
"""The other shape ``detail`` takes. Adapters must tolerate both and surface
neither verbatim."""

SAMPLES: dict[str, dict[str, Any]] = {
    "workspace-runs-list": WORKSPACE_RUNS_LIST,
    "workspace-runs-empty": WORKSPACE_RUNS_EMPTY,
    "workspace-runs-partial": WORKSPACE_RUNS_PARTIAL,
    "praxis-agent-insights": AGENT_INSIGHTS,
    "praxis-agent-insights-empty": AGENT_INSIGHTS_EMPTY,
    "praxis-context": PRAXIS_CONTEXT,
    "praxis-context-empty": PRAXIS_CONTEXT_EMPTY,
    "error-detail-string": DETAIL_STRING,
    "error-detail-structured": DETAIL_STRUCTURED,
}


def load(name: str) -> dict[str, Any]:
    text = (FIXTURES / f"{name}.json").read_text(encoding="utf-8")
    document: dict[str, Any] = json.loads(text)
    return document


def fixture_names() -> list[str]:
    return sorted(path.stem for path in FIXTURES.glob("*.json") if path.name != "manifest.json")


def leaves(shape: object) -> list[str]:
    """Every scalar token in ``shape``, flattened."""
    if isinstance(shape, str):
        return shape.split("|")
    if isinstance(shape, dict):
        found: list[str] = []
        for value in shape.values():
            if isinstance(value, dict):
                for inner in value.values():
                    found.extend(leaves(inner))
            else:
                found.extend(leaves(value))
        return found
    return []


LEAK_MIN_CHARS = 12
"""How long a sample string must be to be worth hunting for.

Shorter than this and the check reports English: ``"sort"`` is a value in one
sample body *and* a word in a variant name, so a three-character floor flags a
coincidence and teaches everyone to ignore the result. Every string that would
actually matter — a uuid, a timestamp, a run id, a bundle digest, a bracketed
placeholder — is comfortably longer. The shapes themselves are guarded
separately and absolutely by the type-token sweep, which admits no value of any
length.
"""


def sample_values(value: object) -> list[str]:
    """Every non-trivial string a sample body carries, for the leak check."""
    if isinstance(value, str):
        return [value] if len(value) >= LEAK_MIN_CHARS else []
    if isinstance(value, dict):
        return [text for item in value.values() for text in sample_values(item)]
    if isinstance(value, list):
        return [text for item in value for text in sample_values(item)]
    return []


def test_the_manifest_digest_matches_the_files_on_disk() -> None:
    """Drift detection. A fixture edited without regenerating the manifest is a
    fixture nobody re-derived, which is how a stale shape reaches P13."""
    manifest = load("manifest")

    digests = {
        f"{name}.json": hashlib.sha256(
            (FIXTURES / f"{name}.json").read_text(encoding="utf-8").encode("utf-8")
        ).hexdigest()
        for name in fixture_names()
    }
    combined = "\n".join(f"{name}:{digest}" for name, digest in sorted(digests.items()))

    assert manifest["files"] == digests
    assert manifest["file_count"] == len(digests)
    assert manifest["digest"] == hashlib.sha256(combined.encode("utf-8")).hexdigest()


def test_the_fixtures_record_that_no_live_call_produced_them() -> None:
    assert load("manifest")["live_calls_made"] == 0
    for name in fixture_names():
        assert load(name)["live_calls_made"] == 0, name


def test_every_recorded_shape_is_types_and_keys_and_never_a_value() -> None:
    checked = 0
    for name in fixture_names():
        for entry in load(name).get("variants", []):
            for token in leaves(entry["shape"]):
                assert token in SHAPE_TOKENS, f"{name}/{entry['name']}: {token!r}"
                checked += 1
    assert checked > 0, "the sweep found no shapes to check"


def test_no_sample_value_survives_into_any_fixture() -> None:
    """The bodies carry synthetic ids, timestamps and bracketed placeholders. If
    the capture path ever started copying values, they would appear here."""
    forbidden = {
        text
        for sample in SAMPLES.values()
        for text in sample_values(sample)
        if text not in ("ok", "ready", "active", "agent", "praxis", "one_shot", "processing")
    }
    body = "\n".join(
        (FIXTURES / path.name).read_text(encoding="utf-8") for path in FIXTURES.glob("*.json")
    )

    leaked = sorted(text for text in forbidden if text in body)

    assert forbidden, "the leak check had nothing to look for"
    assert leaked == []


def test_each_recorded_shape_still_describes_its_sample_body() -> None:
    compared = 0
    for name in fixture_names():
        for entry in load(name).get("variants", []):
            sample = SAMPLES[entry["sample"]]
            assert entry["shape"] == body_shape(sample), f"{name}/{entry['name']}"
            compared += 1
    assert compared > 0, "no variant declared a sample to re-derive from"


def test_a_nullable_column_is_recorded_as_nullable_rather_than_as_one_row_s_type() -> None:
    """``duration_ms`` is a float on a finished run and null on one still
    processing, and the fixture has to say both — an adapter that read the first
    row only would type a required float and break on the second."""
    runs = load("workspace-runs-list")
    ok = next(entry for entry in runs["variants"] if entry["name"] == "ok")

    row = ok["shape"]["object"]["runs"]["array"]["object"]
    assert set(row["duration_ms"].split("|")) == {"null", "number"}
    assert row["summary_counts"]["nullable"] is True
    assert set(row["summary_counts"]["object"]) == {"spans", "events", "artifacts", "policies"}
    assert row["is_governed"] == "boolean|null"


def test_the_recorded_classification_matches_what_the_transport_computes() -> None:
    compared = 0
    for name in fixture_names():
        for entry in load(name).get("variants", []):
            status, error = classify_status(entry["status"], detail=entry.get("detail_contains"))
            assert status == entry["service_status"], f"{name}/{entry['name']}"
            assert (error.code if error else None) == entry["error_code"]
            assert (error.retryable if error else None) == entry["retryable"]
            compared += 1
    assert compared > 0


def test_an_empty_page_is_recorded_as_a_successful_read() -> None:
    """Checklist item 6, recorded where P13 will read it."""
    runs = load("workspace-runs-list")
    empty = next(
        entry
        for entry in runs["variants"]
        if entry["name"] == "valid_empty_workspace_owns_no_studios"
    )

    assert empty["status"] == 200
    assert empty["service_status"] == "ok"
    assert empty["error_code"] is None


def test_a_partial_page_is_recorded_as_a_success_that_carries_coverage() -> None:
    runs = load("workspace-runs-list")
    partial = next(
        entry for entry in runs["variants"] if entry["name"] == "partial_one_studio_failed"
    )

    assert partial["status"] == 200
    assert partial["service_status"] == "ok"
    assert "studios_failed" in partial["shape"]["object"]
    assert "total_is_exact" in partial["shape"]["object"]


def test_the_not_configured_variant_is_recorded_as_capability_not_outage() -> None:
    insights = load("praxis-agent-insights")
    entry = next(
        item for item in insights["variants"] if item["name"] == "unsupported_praxis_not_configured"
    )

    assert entry["service_status"] == "unsupported"
    assert entry["retryable"] is False


def test_the_context_fixture_records_the_parameter_that_must_never_be_sent() -> None:
    context = load("praxis-context")

    assert context["never_send"]["parameter"] == "run_id"
    assert "InjectionRecord" in context["never_send"]["why"]
    assert context["reachable_with_workspace_key"] is True


def test_the_unreachable_fixture_agrees_with_what_the_transport_refuses() -> None:
    """If someone opens one of these routes in code without evidence, the
    fixture stops matching and this fails."""
    unreachable = load("studio-praxis-unreachable")

    assert unreachable["reachable_with_workspace_key"] is False
    assert unreachable["no_request_is_made"] is True
    for route in unreachable["routes"]:
        concrete = route["path"].replace("{studio_id}", "482")
        concrete = concrete.replace("{run_id}", "abc").replace("{insight_id}", "abc")
        capability = route_capability(concrete)
        assert capability is not None, concrete
        assert capability.reachable is False, concrete


def test_the_capability_fixture_mirrors_the_tuple_the_code_enforces() -> None:
    recorded = load("workspace-key-capabilities")["entries"]

    live = [
        {
            "pattern": capability.pattern.pattern,
            "reachable": capability.reachable,
            "reason": capability.reason,
        }
        for capability in WORKSPACE_KEY_CAPABILITIES
    ]

    assert recorded == live


def test_every_family_states_its_evidence_and_cites_a_source() -> None:
    """``literal`` is evidence; ``inferred`` is a hypothesis. A family that said
    neither would be read as contract, which is the mistake the fixture
    inventory's own README warns about."""
    for name in fixture_names():
        document = load(name)
        if name == "workspace-key-capabilities":
            continue
        assert document["evidence"] in ("literal", "inferred"), name
        for entry in document.get("variants", []):
            assert entry["evidence"] in ("literal", "inferred"), f"{name}/{entry['name']}"
