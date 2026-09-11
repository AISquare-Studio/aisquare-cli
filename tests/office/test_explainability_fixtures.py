"""P13's captured fixtures: shapes only, still true of the adapter, no values.

``tests/fixtures/office-explainability/`` records what the four Explainability
read routes this packet consumes answer — as *shapes*: key names and type
names, never values. Three things are checked mechanically, because "I looked
and it was fine" does not survive the next person adding a family:

**They contain no data.** Every leaf of every recorded shape is one of a small
set of type tokens, and no string from the sample bodies appears anywhere in
the files.

**They have not drifted from the code.** Shapes are re-derived from the sample
bodies; each variant's availability is re-derived from P12's
:func:`classify_status`; and each 404's meaning is re-derived from this
adapter's :func:`not_found_meaning`, which is the function that decides whether
a caller polls, stops, or reports a masked object.

**No live call produced any of this.** P13 contacted neither deployed service.
The keys, types, nullability and status codes come from the source-mined
evidence recorded in the Office repository's platform fixture inventory, each
family citing the file and line range it was read from. Values are invented —
trust the shapes, not the numbers.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from aisquare.office.adapters.explainability import not_found_meaning
from aisquare.office.adapters.platform_transport import classify_status
from aisquare.office.platform_redaction import body_shape

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "office-explainability"

SHAPE_TOKENS = frozenset(
    {"null", "boolean", "integer", "number", "string", "absent", "empty", "truncated", "mixed"}
)

# --------------------------------------------------------------------------
# Synthetic bodies
#
# Every value below is invented. The KEYS, the TYPES and the NULLABILITY are
# source-mined evidence; the ids, timestamps, costs and counts are not, and
# must never be read as measurements. Fields that carry prompt, tool or model
# text in the real API are bracketed placeholders, so even this block has
# nothing worth redacting — and the adapter admits none of them anyway.
# --------------------------------------------------------------------------

RUN_ROW: dict[str, Any] = {
    "run_id": "00000000000000000000000000000000",
    "status": "ready",
    "agent_name": "synthetic-agent",
    "started_at": "2000-01-01T00:00:00+00:00",
    "ended_at": "2000-01-01T00:00:00+00:00",
    "updated_at": "2000-01-01T00:00:00+00:00",
    "duration_ms": 0.0,
    "node_count": 0,
    "token_count": 0,
    "cost_usd": 0.0,
    "error_count": 0,
    "flagged_count": 0,
    "last_error": None,
    "graph_available": True,
    "summary_counts": {"spans": 0, "events": 0, "artifacts": 0, "policies": 0},
    "root_input": {"text": "[placeholder: root span input, never admitted]"},
    "root_output": {"text": "[placeholder: root span output, never admitted]"},
    "root_output_missing_reason": None,
}

RUN_DETAIL: dict[str, Any] = {
    "status": "ready",
    "updated_at": "2000-01-01T00:00:00+00:00",
    "poll_after_ms": None,
    "run": RUN_ROW,
}

RUN_DETAIL_PROCESSING: dict[str, Any] = {
    "status": "processing",
    "updated_at": "2000-01-01T00:00:00+00:00",
    "poll_after_ms": 0,
    "run": {
        **RUN_ROW,
        "status": "processing",
        "ended_at": None,
        "duration_ms": None,
        "node_count": None,
        "token_count": None,
        "cost_usd": None,
        "error_count": None,
        "flagged_count": None,
        "graph_available": False,
        "summary_counts": None,
        "root_input": None,
        "root_output": None,
    },
}
"""An unfinished run: nullable metrics null rather than zero. Null is
unmeasured and 0 is measured as zero, and the two never merge."""

POLICY_GATE: dict[str, Any] = {
    "gate": "synthetic_gate",
    "name": "[placeholder: rule title]",
    "rule_id": "synthetic_rule",
    "passed": True,
    "triggered": True,
    "detail": "[placeholder: gate detail prose]",
    "outcome": "pass",
    "evaluation_mode": "deterministic",
    "severity": "critical",
    "action": "block",
    "tag": "blocking",
    "category": "Blocking",
    "lifecycle_state": "active",
    "span_id": "0000000000000000",
    "span_name": "synthetic_span",
    "degraded_reason": None,
}

POLICY_GATE_UNVERIFIED: dict[str, Any] = {
    **POLICY_GATE,
    "outcome": "unverified",
    "passed": True,
    "degraded_reason": "[placeholder: why no verdict could be obtained]",
}
"""``passed`` stays true for legacy readers while ``outcome`` says no verdict
was obtained. Folding that into a pass is a false green."""

RUN_POLICIES: dict[str, Any] = {
    "run_id": "00000000000000000000000000000000",
    "gates": [POLICY_GATE, POLICY_GATE_UNVERIFIED],
    "deductions": [],
    "enforcements": [
        {
            "span_id": "0000000000000000",
            "span_name": "synthetic_span",
            "outcome": "allow",
            "action": "warn",
            "violation_count": 0,
            "evaluation_mode": "llm",
            "degraded": False,
            "before": "[placeholder: raw model output, never admitted]",
            "after": "[placeholder: rewritten model output, never admitted]",
        }
    ],
    "enforcement_summary": {"blocked": 0, "improved": 0, "warned": 0, "decisions": 0},
    "aisquare_audit_at": "2000-01-01T00:00:00+00:00",
    "adherence_policies_version_id": "000000000000",
    "is_governed": True,
}

RUN_POLICIES_NEVER_AUDITED: dict[str, Any] = {
    "run_id": "00000000000000000000000000000000",
    "gates": [],
    "deductions": [],
    "enforcements": [],
    "enforcement_summary": None,
    "aisquare_audit_at": None,
    "is_governed": None,
}
"""Empty lists are forced by the handler when the stored JSON is null, so an
empty ``gates`` alone cannot say "never graded" — these two nulls can."""

RUN_POLICIES_UNGOVERNED: dict[str, Any] = {
    **RUN_POLICIES_NEVER_AUDITED,
    "is_governed": False,
    "aisquare_audit_at": "2000-01-01T00:00:00+00:00",
}

RUN_STORY: dict[str, Any] = {
    "studio_id": "000",
    "run_id": "00000000000000000000000000000000",
    "moments": [
        {
            "id": "m0",
            "type": "intake",
            "title": "[placeholder: moment title]",
            "summary": "[placeholder: moment summary]",
            "timestamp_ns": 0,
            "duration_ms": None,
            "span_ids": ["0000000000000000"],
            "metadata": {
                "user_prompt": "[placeholder: the human prompt, never admitted]",
                "system_prompt": "[placeholder: the system prompt, never admitted]",
            },
            "severity": "info",
            "is_climax": False,
        }
    ],
    "enriched_moments": None,
    "enrichment_model": None,
    "is_enriching": False,
    "coverage": {"spans_total": 0, "spans_projected": 0, "by_kind": {"llm": 0, "tool": 0}},
    "updated_at": "2000-01-01T00:00:00+00:00",
}

RUN_STORY_ENRICHING: dict[str, Any] = {**RUN_STORY, "is_enriching": True}

RUN_RML: dict[str, Any] = {
    "analysis_id": "rml_000000000000",
    "run_id": "00000000000000000000000000000000",
    "studio_id": "000",
    "rml_version": "3",
    "extracted_at": "2000-01-01T00:00:00+00:00",
    "extractor_model": "[placeholder: extraction model name]",
    "extraction_confidence": 0.0,
    "low_confidence": False,
    "claims": ["[placeholder: extracted claim]"],
    "assumptions": ["[placeholder: extracted assumption]"],
    "evidence_attribution": [],
    "inference_chain": [],
    "policy_triggers": [],
}

DETAIL_STRING: dict[str, Any] = {"detail": "[placeholder: upstream sentence]"}

SAMPLES: dict[str, dict[str, Any]] = {
    "run-detail": RUN_DETAIL,
    "run-detail-processing": RUN_DETAIL_PROCESSING,
    "run-policies": RUN_POLICIES,
    "run-policies-never-audited": RUN_POLICIES_NEVER_AUDITED,
    "run-policies-ungoverned": RUN_POLICIES_UNGOVERNED,
    "run-story": RUN_STORY,
    "run-story-enriching": RUN_STORY_ENRICHING,
    "run-rml": RUN_RML,
    "error-detail-string": DETAIL_STRING,
}


def load(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return document


def fixture_names() -> list[str]:
    return sorted(path.stem for path in FIXTURES.glob("*.json") if path.name != "manifest.json")


def leaves(shape: object) -> list[str]:
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


def sample_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if len(value) >= LEAK_MIN_CHARS else []
    if isinstance(value, dict):
        return [text for item in value.values() for text in sample_values(item)]
    if isinstance(value, list):
        return [text for item in value for text in sample_values(item)]
    return []


def test_the_manifest_digest_matches_the_files_on_disk() -> None:
    """A fixture edited without regenerating the manifest is a fixture nobody
    re-derived, which is how a stale shape reaches P15."""
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
        for entry in load(name)["variants"]:
            for token in leaves(entry["shape"]):
                assert token in SHAPE_TOKENS, f"{name}/{entry['name']}: {token!r}"
                checked += 1
    assert checked > 0, "the sweep found no shapes to check"


def test_no_sample_value_survives_into_any_fixture() -> None:
    forbidden = {
        text
        for sample in SAMPLES.values()
        for text in sample_values(sample)
        if text not in ("synthetic_gate", "synthetic_rule", "synthetic_span")
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
        for entry in load(name)["variants"]:
            sample = SAMPLES[entry["sample"]]
            assert entry["shape"] == body_shape(sample), f"{name}/{entry['name']}"
            compared += 1
    assert compared > 0, "no variant declared a sample to re-derive from"


def test_the_recorded_availability_matches_what_the_transport_computes() -> None:
    compared = 0
    for name in fixture_names():
        for entry in load(name)["variants"]:
            status, error = classify_status(entry["status"], detail=entry.get("detail_contains"))
            assert status == entry["service_status"], f"{name}/{entry['name']}"
            assert (error.code if error else None) == entry["error_code"]
            assert (error.retryable if error else None) == entry["retryable"]
            compared += 1
    assert compared > 0


def test_every_recorded_four_oh_four_still_reads_the_way_the_adapter_reads_it() -> None:
    """The fixture records which of this API's four 404s a variant is. If the
    adapter's reading of that wording ever changes, this stops matching — which
    is the point, because that reading decides whether a caller polls."""
    compared = 0
    for name in fixture_names():
        for entry in load(name)["variants"]:
            if entry["status"] != 404:
                continue
            assert not_found_meaning(entry["detail_contains"]) == entry["meaning"], entry["name"]
            compared += 1
    assert compared >= 3, "the 404 families are the ones most worth pinning"


def test_a_nullable_metric_is_recorded_as_nullable_rather_than_as_one_row_s_type() -> None:
    """``cost_usd`` is a number on a finished run and null on one still
    processing. An adapter that read the first row only would type a required
    float and break on the second — and, worse, might fabricate a zero."""
    processing = next(
        entry for entry in load("run-detail")["variants"] if entry["name"] == "still_processing"
    )

    run = processing["shape"]["object"]["run"]["object"]
    assert run["cost_usd"] == "null"
    assert run["summary_counts"] == "null"
    assert processing["shape"]["object"]["poll_after_ms"] == "integer"


def test_every_family_states_its_evidence_and_cites_a_source() -> None:
    """``literal`` is evidence; ``inferred`` is a hypothesis. A family that
    said neither would be read as contract."""
    for name in fixture_names():
        document = load(name)
        assert document["evidence"] in ("literal", "inferred"), name
        assert document["source"]["repo"], name
        assert document["source"]["lines"], name
        for entry in document["variants"]:
            assert entry["evidence"] in ("literal", "inferred"), f"{name}/{entry['name']}"


def test_the_text_bearing_fields_are_recorded_as_admitted_by_nothing() -> None:
    """Each family names the fields that carry prompt, tool or model text. The
    adapter admits none of them, and the fixture is where that promise is
    written down next to the shape that contains them."""
    named = {field for name in fixture_names() for field in load(name).get("never_admitted", [])}

    assert "root_input" in named
    assert "root_output" in named
    assert "metadata" in named
    assert "before" in named
