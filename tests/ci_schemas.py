"""The server's JSON Schemas, vendored, and a validator that resolves between them.

Everything the CLI emits or accepts under hook contract v2 is checked here
against the schema files copied byte for byte from ``aisquare-ci`` — never
against a second reading of them in Python. A scorer with its own predicate can
only ever agree with itself; this one borrows the server's.

``referencing`` rather than the deprecated ``RefResolver``: the hook-response
schema ``$ref``s the briefing and error schemas by absolute ``$id``, and a
registry that holds every vendored document resolves them without ever
touching the network.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

FIXTURES = Path(__file__).parent / "fixtures" / "ci_contract" / "v2"
SCHEMAS = FIXTURES / "schemas"

#: Every vendored contract, by name. The suite fails if a file is missing.
CONTRACTS = (
    "hook-request.experimental-v2",
    "hook-response.experimental-v2",
    "mcp-tool-input.v1",
    "mcp-tool-output.v1",
    "client-delivery-descriptor.v1",
    "delivery-capability-manifest.v1",
    "error.v1",
)


def schema(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))
    return data


def fixture(name: str) -> Any:
    """``<contract>.valid`` or ``<contract>.invalid``, parsed."""
    return json.loads(fixture_text(name))


def fixture_text(name: str) -> str:
    return (FIXTURES / f"{name}.json").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def registry() -> Registry:
    built: Registry = Registry()
    for name in CONTRACTS:
        document = schema(name)
        resource = Resource.from_contents(document, default_specification=DRAFT202012)
        built = built.with_resource(document["$id"], resource)
    return built


def errors(name: str, instance: object) -> list[str]:
    """Every validation error of ``instance`` against contract ``name``, as
    ``$.path: message`` lines. Empty means valid."""
    validator = Draft202012Validator(schema(name), registry=registry())
    return [
        f"{error.json_path}: {error.message}"
        for error in sorted(validator.iter_errors(instance), key=str)
    ]


def assert_valid(name: str, instance: object) -> None:
    found = errors(name, instance)
    assert not found, f"does not satisfy {name}:\n  " + "\n  ".join(found)
