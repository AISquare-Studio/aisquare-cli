"""Non-executable, display-only character packs and deterministic captions.

This module deliberately knows nothing about prompts, tools or agent launch.
Only human display code may call its renderer.
"""

from __future__ import annotations

import hashlib
import json
import re
import string
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_PACK_BYTES = 131_072
ROLES = ("manager", "planner", "coder", "runner", "tester", "reviewer", "validator", "ui-tester")
EVENTS = frozenset(
    {
        # Exactly the kinds the team store emits (services/team.py, work_briefs.py,
        # fleet.py); a pattern keyed on any other name would never be shown.
        "note",
        "result",
        "question",
        "attention",
        "signal",
        "activate",
        "focus",
        "task_added",
        "task_claimed",
        "task_released",
        "task_review",
        "task_reopened",
        "task_done",
        "task_blocked",
        "task_dropped",
        "agent_exited",
        "brief_created",
        "brief_updated",
        "brief_linked",
        "brief_evidence",
        "default",
    }
)
PLACEHOLDERS = frozenset({"role", "task_id", "session_id", "event_kind"})
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_VERSION = re.compile(r"^(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$")
_FORMATTER = string.Formatter()


def plain_text(value: str) -> str:
    """Neutralize terminal controls, including OSC/ANSI introducers and bidi controls."""
    return "".join(
        ch if ch in "\n\t" or unicodedata.category(ch)[0] != "C" else "�" for ch in value
    )


def single_line(value: str) -> str:
    """One display line: controls neutralized AND line breaks flattened.

    ``plain_text`` keeps newlines because event text is allowed to span lines.
    Facts that sit inside a caption or its header (the session role, ids) are
    not: an agent that names its role ``coder\\nOriginal record …`` must not be
    able to forge a second official-looking line in the operator's panel.
    """
    return " ".join(plain_text(value).replace("\t", " ").split("\n")).replace("\r", " ")


def clean_text(value: str) -> str:
    """Pack prose must be plain printable text, never terminal control sequences."""
    if plain_text(value) != value or "\t" in value or "\n" in value or "\r" in value:
        raise ValueError("persona text must be one line with no terminal/control characters")
    return value


_SEAT = re.compile(r"^(?P<base>[a-z][a-z-]*[a-z])-?(?P<seat>\d+)$")


def base_role(role: str) -> str:
    """Numbered seats inherit their role (coder2, coder-2, ui-tester3).

    Mirrors ``harness.base_role`` without importing the harness: only a seat of a
    role this module KNOWS collapses, so a declared role called ``bot7`` or
    ``gpt4`` stays itself instead of being promoted to a pack role it never had.
    """
    match = _SEAT.match(role)
    if match is None:
        return role
    base = match.group("base")
    return base if base in ROLES else role


def validate_identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError("IDs use lowercase letters, digits and hyphens, starting with a letter")
    return value


def validate_version(value: str) -> str:
    if not _VERSION.fullmatch(value):
        raise ValueError("version must be three bounded numbers, for example 1.0.0")
    return value


class PersonaPack(BaseModel):
    """Strict data schema: templates cannot call code or navigate Python objects."""

    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    id: str
    version: str
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    author: str = Field(default="Local user", max_length=120)
    license: str = Field(default="All rights reserved", max_length=120)
    generic: dict[str, list[str]]
    roles: dict[str, dict[str, list[str]]] = Field(default_factory=dict)

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be the number 1")
        return value

    _id = field_validator("id")(validate_identifier)
    _version = field_validator("version")(validate_version)
    _text = field_validator("name", "description", "author", "license")(clean_text)

    @model_validator(mode="after")
    def validate_patterns(self) -> PersonaPack:
        if len(self.roles) > 32 or len(self.generic) > len(EVENTS):
            raise ValueError("too many roles or generic events")
        if "default" not in self.generic:
            raise ValueError("generic.default is required")
        if not self.name.strip():
            raise ValueError("persona name must not be blank")
        total = 0
        for role, patterns in [("generic", self.generic), *self.roles.items()]:
            validate_identifier(role)
            if role != "generic" and base_role(role) != role:
                # The renderer looks patterns up by BASE role; a seat key could never match.
                raise ValueError(
                    f"use the base role name {base_role(role)!r}, not the seat {role!r}"
                )
            if len(patterns) > len(EVENTS):
                raise ValueError("too many event patterns")
            for event, alternatives in patterns.items():
                if event not in EVENTS:
                    raise ValueError(f"unsupported event: {event}")
                if not 1 <= len(alternatives) <= 5:
                    raise ValueError("each event needs between one and five patterns")
                for template in alternatives:
                    total += len(template)
                    if not 1 <= len(template) <= 400:
                        raise ValueError("each pattern must be between 1 and 400 characters")
                    if not template.strip():
                        raise ValueError("a pattern must not be blank")
                    clean_text(template)
                    for _, field, spec, conversion in _FORMATTER.parse(template):
                        if field is not None and (field not in PLACEHOLDERS or spec or conversion):
                            raise ValueError(f"unsupported placeholder: {field}")
        if total > 48_000:
            raise ValueError("persona patterns are too large")
        return self

    @property
    def reference(self) -> str:
        return f"{self.id}@{self.version}"


def parse_pack(raw: bytes) -> PersonaPack:
    if len(raw) > MAX_PACK_BYTES:
        raise ValueError(f"persona exceeds {MAX_PACK_BYTES} bytes")

    def unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate persona key: {key}")
            result[key] = value
        return result

    try:
        document = json.loads(raw, object_pairs_hook=unique_keys)
    except RecursionError:
        # json's C decoder recurses per nesting level; a hostile pack must be a
        # ValueError like every other damaged pack, never a crash that skips the
        # "damaged pack cannot suppress activity" guards.
        raise ValueError("persona JSON is nested too deeply") from None
    return PersonaPack.model_validate(document)


def pack_bytes(pack: PersonaPack) -> bytes:
    return (pack.model_dump_json(indent=2) + "\n").encode("utf-8")


def caption(
    pack: PersonaPack,
    *,
    role: str,
    kind: str,
    event_id: str,
    task_id: str | None = None,
    session_id: str | None = None,
) -> str:
    """One event keeps the same phrase over redraws; never guess missing facts."""
    role_patterns = pack.roles.get(base_role(role), {})
    patterns = role_patterns.get(kind) or pack.generic.get(kind) or pack.generic["default"]
    # Facts come from the board, i.e. from agents; they are display data, not markup.
    facts = {"role": single_line(role), "event_kind": single_line(kind)}
    if task_id:
        facts["task_id"] = single_line(task_id)
    if session_id:
        facts["session_id"] = single_line(session_id)
    eligible = [
        p
        for p in patterns
        if all(field is None or field in facts for _, field, _, _ in _FORMATTER.parse(p))
    ]
    if not eligible:
        return ""
    index = int(hashlib.sha256(event_id.encode()).hexdigest()[:8], 16) % len(eligible)
    return single_line(eligible[index].format_map(facts))
