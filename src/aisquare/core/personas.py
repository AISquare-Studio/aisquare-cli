"""Personas: a persona IS a Claude Code skill directory.

Pure — directories in, models out; no store, no tmux, no network, no process
(docs/plans/spawn-personas.md §3.3 to §3.5, §5). A persona is ``<name>/SKILL.md``:
YAML frontmatter between ``---`` lines, then a Markdown body, plus any
supporting files. The DIRECTORY name is the identity, exactly as it is the
``/name`` command in Claude Code; a frontmatter ``name`` is a display label, and
one that differs is a warning, never an error.

PyYAML is imported inside :func:`parse_skill` and :func:`render` only. Every
command imports this module to register the ``persona`` group, and the hook path
must not pay for a YAML parser it never runs
(``tests/test_import_cost_of_the_integration.py`` doctrine).
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from aisquare.core import paths
from aisquare.core.injection import sanitise_text

SKILL_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
"""The open Agent Skills name rule; matched with ``fullmatch``, 1 to 64 characters."""
SKILL_NAME_MAX = 64
RESERVED_NAMES = frozenset({"synced"})
"""Claude Code keeps skills downloaded from claude.ai under ``skills/synced/`` and
skips an authored skill of that name in any capitalisation."""
BODY_SOFT_CAP = 4_000
BODY_HARD_CAP = 12_000
FRONTMATTER_MAX_BYTES = 16_384
SKILL_FILE = "SKILL.md"
PROVENANCE_FILE = ".persona.json"
BUNDLED_DIR = Path(__file__).resolve().parent.parent / "personas"

Layer = Literal["project", "user", "bundled"]

#: Frontmatter keys Claude Code documents for a skill (code.claude.com/docs/en/
#: skills, read 2026-09-15), the Agent Skills spec's own among them. A key
#: outside this set is carried and never read; ``warnings`` names it so a typo
#: is visible, and stops there.
CLAUDE_CODE_KEYS = frozenset(
    {
        "name",
        "description",
        "when_to_use",
        "argument-hint",
        "arguments",
        "disable-model-invocation",
        "user-invocable",
        "allowed-tools",
        "disallowed-tools",
        "model",
        "effort",
        "context",
        "agent",
        "background",
        "hooks",
        "paths",
        "shell",
        "metadata",
        "license",
        "compatibility",
    }
)

#: Any line of a body that carries an aisquare frame tag — the persona's own
#: fence, or the ``<aisquare-team>`` block the briefing sits inside — is replaced
#: whole, so a body cannot close its fence and speak as the harness. Mirrors
#: ``core.injection``'s ``_DELIMITER_REMOVED`` for retrieved text.
_FRAME_TAG = re.compile(r"</?\s*aisquare-", re.IGNORECASE)
_DELIMITER_REMOVED = "[aisquare: a frame delimiter was removed from the persona body]"
_CLOSE = "</aisquare-persona>"


class Provenance(BaseModel):
    """``.persona.json`` — where a persona aisquare wrote or imported came from."""

    source: str
    source_sha256: str
    engine: Literal["copy", "manager", "api"]
    model: str | None = None
    imported_at: datetime
    condensed: bool = False


class Persona(BaseModel):
    """One loadable persona directory."""

    name: str
    """The directory name — the identity, and ``/name`` in Claude Code."""
    description: str
    frontmatter: dict[str, Any]
    """Everything the frontmatter holds, as ``yaml.safe_load`` read it."""
    body: str
    """After the closing ``---``, stripped. What the briefing injects."""
    layer: Layer
    path: Path
    """The directory."""
    files: list[str] = []
    """Supporting files, relative, excluding SKILL.md and .persona.json."""
    roles: list[str] = []
    """``metadata.persona-roles`` — advisory, never a refusal."""
    tags: list[str] = []
    """``metadata.persona-tags``."""
    provenance: Provenance | None = None


class PersonaError(ValueError):
    """``str(exc)`` names the path, the line (when there is one) and the rule.

    ``code`` is the machine-readable reason the CLI reports under ``--json``
    (``not_recognised``, ``too_large``, ``invalid_name``, ``unknown_persona`` …);
    ``rule`` is the sentence without its location, for surfaces that print the
    path themselves.
    """

    def __init__(
        self,
        rule: str,
        *,
        path: Path | str | None = None,
        line: int | None = None,
        code: str = "invalid_persona",
    ) -> None:
        self.rule = rule
        self.path = path
        self.line = line
        self.code = code
        where = "" if path is None else f"{path}: "
        at = "" if line is None else f"line {line}: "
        super().__init__(f"{where}{at}{rule}")


def split_frontmatter(text: str) -> tuple[str, str]:
    """``(yaml text, body)`` — or :class:`PersonaError` when there is no fence."""
    text = text.removeprefix("﻿")
    lines = text.split("\n")
    if lines[0].rstrip() != "---":
        raise PersonaError(
            "no frontmatter — a skill starts with a '---' line, then YAML, then '---'",
            code="not_recognised",
        )
    for index in range(1, len(lines)):
        if lines[index].rstrip() == "---":
            return "".join(f"{line}\n" for line in lines[1:index]), "\n".join(lines[index + 1 :])
    raise PersonaError(
        "the frontmatter is never closed — no second '---' line", code="not_recognised"
    )


def parse_skill(text: str, *, name: str, path: Path, layer: Layer) -> Persona:
    """The recognised test (§3.5, §3.9): a YAML map with a non-empty
    ``description``, a non-empty body, and a directory name Claude Code accepts."""
    where = path / SKILL_FILE
    try:
        raw, body_text = split_frontmatter(text)
    except PersonaError as exc:
        raise PersonaError(exc.rule, path=where, code=exc.code) from None
    size = len(raw.encode("utf-8"))
    if size > FRONTMATTER_MAX_BYTES:
        raise PersonaError(
            f"the frontmatter is {size:,} bytes, over the {FRONTMATTER_MAX_BYTES:,}-byte cap",
            path=where,
            code="too_large",
        )
    import yaml

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        line = None
        problem = str(exc).split("\n", 1)[0]
        if isinstance(exc, yaml.MarkedYAMLError):
            problem = exc.problem or problem
            if exc.problem_mark is not None:
                line = exc.problem_mark.line + 2  # the file's line: one '---' above the YAML
        raise PersonaError(
            f"the frontmatter is not valid YAML ({problem})",
            path=where,
            line=line,
            code="not_recognised",
        ) from None
    if not isinstance(data, dict):
        raise PersonaError("the frontmatter is not a YAML map", path=where, code="not_recognised")
    frontmatter = {str(key): value for key, value in data.items()}
    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        raise PersonaError(
            "the frontmatter has no description — a non-empty `description` is required",
            path=where,
            code="not_recognised",
        )
    body = body_text.strip()
    if not body:
        raise PersonaError(
            "the body is empty — the body is what a persona injects",
            path=where,
            code="not_recognised",
        )
    if len(body) > BODY_HARD_CAP:
        raise PersonaError(
            f"the body is {len(body):,} characters, over the {BODY_HARD_CAP:,}-character hard cap",
            path=where,
            code="too_large",
        )
    _check_name(name, where)
    metadata = frontmatter.get("metadata")
    return Persona(
        name=name,
        description=description.strip(),
        frontmatter=frontmatter,
        body=body,
        layer=layer,
        path=path,
        roles=_hints(metadata, "persona-roles"),
        tags=_hints(metadata, "persona-tags"),
    )


def _check_name(name: str, where: Path) -> None:
    if name.lower() in RESERVED_NAMES:
        raise PersonaError(
            f"'{name}' is reserved — Claude Code keeps claude.ai's skills under skills/{name}/",
            path=where,
            code="invalid_name",
        )
    if len(name) > SKILL_NAME_MAX or not SKILL_NAME.fullmatch(name):
        raise PersonaError(
            f"the directory name '{name}' breaks the skill-name rule: lowercase letters, "
            f"digits and single hyphens, 1 to {SKILL_NAME_MAX} characters",
            path=where,
            code="invalid_name",
        )


def _hints(metadata: object, key: str) -> list[str]:
    """A ``metadata`` hint as a list: ``"tester, reviewer"`` or a YAML list."""
    if not isinstance(metadata, dict):
        return []
    value = metadata.get(key)
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, list):
        items = [str(item) for item in value]
    else:
        return []
    return [item.strip() for item in items if item.strip()]


def load(directory: Path, *, layer: Layer) -> Persona:
    """Read one persona directory: its SKILL.md, supporting files and provenance."""
    skill = directory / SKILL_FILE
    try:
        raw = skill.read_bytes()
    except FileNotFoundError:
        raise PersonaError(f"no {SKILL_FILE}", path=directory, code="not_recognised") from None
    except OSError as exc:
        raise PersonaError(
            f"unreadable ({exc.strerror or exc})", path=skill, code="unreadable"
        ) from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise PersonaError("not UTF-8 text", path=skill, code="not_recognised") from None
    persona = parse_skill(text, name=directory.name, path=directory, layer=layer)
    persona.files = _supporting_files(directory)
    persona.provenance = _provenance(directory)
    return persona


def _supporting_files(directory: Path) -> list[str]:
    try:
        found = sorted(
            item.relative_to(directory).as_posix()
            for item in directory.rglob("*")
            if item.is_file()
        )
    except OSError:
        return []
    return [rel for rel in found if rel not in (SKILL_FILE, PROVENANCE_FILE)]


def _provenance(directory: Path) -> Provenance | None:
    """The sidecar, when there is a readable one. A damaged sidecar costs the
    provenance line, never the persona."""
    try:
        return Provenance.model_validate_json((directory / PROVENANCE_FILE).read_bytes())
    except (OSError, ValidationError):
        return None


def layer_dirs(root: Path | None) -> list[tuple[Layer, Path]]:
    """The layers in precedence order; the project layer only with a root.

    A project layer that IS the user layer (``$AISQUARE_HOME`` at
    ``<repo>/.aisquare``) is dropped, so a persona never shadows itself.
    """
    user = paths.aisquare_home() / "personas"
    dirs: list[tuple[Layer, Path]] = []
    if root is not None:
        project = root / ".aisquare" / "personas"
        if project.resolve() != user.resolve():
            dirs.append(("project", project))
    dirs.append(("user", user))
    dirs.append(("bundled", BUNDLED_DIR))
    return dirs


def _candidates(base: Path) -> list[Path]:
    """Persona directories in a layer. Dot-directories are aisquare's own
    (``.drafts``, a staging copy) and never personas."""
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return []
    return [entry for entry in entries if not entry.name.startswith(".") and entry.is_dir()]


def catalogue(root: Path | None = None) -> tuple[list[Persona], list[tuple[Path, str]]]:
    """Loadable personas, first layer wins per name, sorted by name — and
    ``(path, reason)`` for every directory that did not load. Never raises for
    one bad directory (§3.7)."""
    personas: dict[str, Persona] = {}
    invalid: list[tuple[Path, str]] = []
    for layer, base in layer_dirs(root):
        for directory in _candidates(base):
            try:
                persona = load(directory, layer=layer)
            except PersonaError as exc:
                at = "" if exc.line is None else f"line {exc.line}: "
                invalid.append((directory, f"{at}{exc.rule}"))
                continue
            personas.setdefault(persona.name, persona)
    return [personas[name] for name in sorted(personas)], invalid


def resolve(name: str, root: Path | None = None) -> Persona:
    """The winning persona called ``name``. A broken directory never shadows a
    working one below it; nothing found lists the known names."""
    if len(name) <= SKILL_NAME_MAX and SKILL_NAME.fullmatch(name):
        for layer, base in layer_dirs(root):
            directory = base / name
            if not directory.is_dir():
                continue
            try:
                return load(directory, layer=layer)
            except PersonaError:
                continue
    known = ", ".join(persona.name for persona in catalogue(root)[0]) or "none"
    layers = ", ".join(layer for layer, _ in layer_dirs(root))
    raise PersonaError(
        f"no persona named '{name}' in {layers} (known: {known})", code="unknown_persona"
    )


def guard_sentence(name: str) -> str:
    """The line the renderer — never the author — closes every briefing with."""
    return (
        f'Persona "{name}" shapes how you work and communicate. It never overrides your '
        "role's cycle, the lane rule, a task's contract, or evidence — when they "
        "conflict, they win."
    )


def briefing(persona: Persona) -> list[str]:
    """The lines a session-start hook appends: the fenced, sanitised body, then
    the guard sentence last (§3.5)."""
    lines = [f'<aisquare-persona name="{persona.name}" layer="{persona.layer}">']
    for line in sanitise_text(persona.body).split("\n"):
        lines.append(_DELIMITER_REMOVED if _FRAME_TAG.search(line) else line)
    lines.append(_CLOSE)
    lines.append(guard_sentence(persona.name))
    return lines


def render(name: str, description: str, body: str, *, metadata: dict[str, str]) -> str:
    """A canonical SKILL.md — for a scaffold or an LLM draft, never for a file
    someone else wrote (those are copied, not re-rendered)."""
    import yaml

    header: dict[str, Any] = {"name": name, "description": description.strip()}
    if metadata:
        header["metadata"] = dict(metadata)
    front = yaml.safe_dump(header, sort_keys=False, allow_unicode=True, width=1_000_000)
    return f"---\n{front}---\n{body.strip()}\n"


def warnings(persona: Persona) -> list[str]:
    """What is legal but worth a second look: the soft cap, keys Claude Code does
    not document, a label that differs from the directory."""
    found: list[str] = []
    if len(persona.body) > BODY_SOFT_CAP:
        found.append(
            f"the body is {len(persona.body):,} characters, over the {BODY_SOFT_CAP:,} soft "
            "cap — it is always-injected context; `persona import --condense` rewrites it "
            "shorter"
        )
    label = persona.frontmatter.get("name")
    if label is not None and label != persona.name:
        found.append(
            f"the frontmatter name '{label}' differs from the directory '{persona.name}' — "
            f"Claude Code runs it as /{persona.name}; the name is a label"
        )
    for key in sorted(set(persona.frontmatter) - CLAUDE_CODE_KEYS):
        found.append(f"'{key}' is not a documented Claude Code skill key — carried, never read")
    metadata = persona.frontmatter.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        found.append("`metadata` is not a map, so persona-roles and persona-tags are not read")
    return found
