"""The file operations behind ``aisquare persona`` — every persona write goes through here.

Import copies bytes (docs/plans/spawn-personas.md §3.3, §3.9): aisquare never
rewrites a file it did not author, and the one file it does author beside a
skill — the provenance sidecar — is ``.persona.json``, a dotfile Claude Code
ignores. Every write lands in a dot-named staging directory inside the target's
parent and is renamed into place, so a reader never sees half a persona and a
failed write leaves the old one untouched.

This module holds the RECOGNISED path. The LLM engines (P5) arrive behind
``import_source``'s ``llm``/``condense``/``engine``/``model``/``confirm`` seam;
until then a source that fails the recognised test is refused as
``not_recognised``, naming that path.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from aisquare.core import personas as core
from aisquare.core.agents import _claude_home
from aisquare.core.editor import edit_text
from aisquare.core.personas import Layer, Persona, PersonaError, Provenance

SkillScope = Literal["user", "project"]

_TEMPLATE_DESCRIPTION = (
    "One line on how this persona works — persona list and the Spawn dialog show it."
)
_TEMPLATE_BODY = """\
You are … — say, in the second person, how this agent works and communicates.

- What it always does first.
- What it refuses to skip.
- How it reports what it did, and what it did not do.
"""
_PROBE_NAME = "persona"
"""A valid name to run the recognised test under before the real name is known."""


class SkillRef(BaseModel):
    """A skill in one of Claude Code's own skill directories — an import source (§3.4)."""

    name: str
    description: str
    path: Path
    scope: SkillScope
    recognised: bool
    reason: str | None = None
    imported: bool = False
    """A persona of this name already exists in some layer."""


class PersonaDraftView(BaseModel):
    """What a confirmation shows before an LLM-made persona is saved (§3.9, §4.3).

    Built by the LLM path (P5); the CLI's y/N and the UI's ConfirmDraftScreen
    render it, and ``draft_path`` is where a refused draft was kept.
    """

    name: str
    description: str
    body: str
    skill_md: str
    engine: str
    model: str | None = None
    notes: list[str] = []
    draft_path: Path | None = None


class ImportResult(BaseModel):
    """A saved import."""

    persona: Persona
    engine: str
    model: str | None = None
    source: str
    replaced: bool = False
    warnings: list[str] = []


@dataclass(frozen=True)
class _Source:
    origin: str
    """What ``.persona.json`` records: an absolute path, or ``stdin``."""
    skill_bytes: bytes
    directory: Path | None
    """Copied whole when the source is a skill directory."""
    stem: str | None
    """A bare file's stem, the last resort for a name."""


def import_source(
    source: str,
    *,
    layer: Layer,
    root: Path | None,
    name: str | None,
    force: bool,
    llm: Literal["auto", "always", "never"],
    condense: bool,
    engine: str | None,
    model: str | None,
    confirm: Callable[[PersonaDraftView], bool],
    progress: Callable[[str], None] | None = None,
) -> ImportResult:
    """Import ``source`` into ``layer``: ``-`` (stdin), a skill directory, a
    SKILL.md or Markdown file, or the name of a skill in Claude Code's skill
    directories. ``confirm`` and ``progress`` are the UI's seam (§4.3)."""
    base = _layer_dir(layer, root)
    src = _read_source(source, root)
    where: Path | str = src.directory or (src.origin if src.origin != "stdin" else "stdin")
    if llm == "always" or condense:
        raise PersonaError(
            "the LLM import path (--llm, --condense) is not in this build yet",
            path=where,
            code="no_import_engine",
        )
    try:
        text = src.skill_bytes.decode("utf-8")
        probe = core.parse_skill(text, name=_PROBE_NAME, path=base / _PROBE_NAME, layer=layer)
    except UnicodeDecodeError:
        raise _not_recognised(where, "not UTF-8 text", None, llm) from None
    except PersonaError as exc:
        if exc.code != "not_recognised":
            raise PersonaError(exc.rule, path=where, line=exc.line, code=exc.code) from None
        raise _not_recognised(where, exc.rule, exc.line, llm) from None

    chosen = name or _implied_name(src, probe)
    try:
        core.parse_skill(text, name=chosen, path=base / chosen, layer=layer)
    except PersonaError as exc:
        hint = "" if name else " — pass --name"
        raise PersonaError(f"{exc.rule}{hint}", path=where, code=exc.code) from None
    dest = base / chosen
    if _occupied(dest) and not force:
        raise PersonaError(
            f"a {layer} persona named '{chosen}' already exists at {dest} — --force replaces "
            "it, --name picks another",
            code="persona_exists",
        )
    if progress is not None:
        progress("recognised skill — copying")

    def fill(staged: Path) -> None:
        if src.directory is not None:
            origin = src.directory
            shutil.copytree(
                origin,
                staged,
                dirs_exist_ok=True,
                ignore=lambda folder, _names: (
                    [core.PROVENANCE_FILE] if Path(folder) == origin else []
                ),
            )
        (staged / core.SKILL_FILE).write_bytes(src.skill_bytes)
        _write_provenance(staged, source=src.origin, skill_bytes=src.skill_bytes)

    replaced = _publish(dest, fill)
    persona = core.load(dest, layer=layer)
    return ImportResult(
        persona=persona,
        engine="copy",
        source=src.origin,
        replaced=replaced,
        warnings=core.warnings(persona),
    )


def _not_recognised(
    where: Path | str, rule: str, line: int | None, llm: Literal["auto", "always", "never"]
) -> PersonaError:
    if llm == "never":
        tail = "and the LLM path is not allowed for this import"
    else:
        tail = (
            "converting it takes the LLM import path, which is not in this build yet — "
            "until then, give it frontmatter with a `description` and a body"
        )
    return PersonaError(
        f"not a recognised skill: {rule}; {tail}", path=where, line=line, code="not_recognised"
    )


def _read_source(source: str, root: Path | None) -> _Source:
    """Resolved in §3.9's order: stdin, an existing path, a URL, a skill name."""
    if source == "-":
        data = sys.stdin.buffer.read()
        if not data.strip():
            raise PersonaError("stdin is empty", code="source_empty")
        return _Source(origin="stdin", skill_bytes=data, directory=None, stem=None)
    path = Path(source).expanduser()
    if path.is_file() and path.name == core.SKILL_FILE:
        path = path.parent
    if path.is_dir():
        directory = path.resolve()
        skill = directory / core.SKILL_FILE
        if not skill.is_file():
            raise PersonaError(
                f"no {core.SKILL_FILE} — a skill is a directory holding <name>/{core.SKILL_FILE}; "
                "converting anything else takes the LLM import path, which is not in this "
                "build yet",
                path=directory,
                code="not_recognised",
            )
        return _Source(
            origin=str(directory), skill_bytes=skill.read_bytes(), directory=directory, stem=None
        )
    if path.is_file():
        resolved = path.resolve()
        return _Source(
            origin=str(resolved), skill_bytes=resolved.read_bytes(), directory=None, stem=path.stem
        )
    if source.startswith(("https://", "http://")):
        raise PersonaError(
            "importing from a URL arrives with the LLM import path — download the file and "
            "import it from disk",
            path=source,
            code="unsupported_source",
        )
    for ref in importable_skills(root):
        if ref.name == source:
            return _read_source(str(ref.path), root)
    raise PersonaError(
        f"no such file, directory or Claude Code skill: {source} "
        "(`aisquare persona import --list` shows the skills)",
        code="source_not_found",
    )


def _implied_name(src: _Source, probe: Persona) -> str:
    """§3.5: a directory's own name; for a bare file the frontmatter ``name`` when
    it satisfies the rule, else the file's stem slugified."""
    if src.directory is not None:
        return src.directory.name
    label = probe.frontmatter.get("name")
    if isinstance(label, str) and _is_skill_name(label):
        return label
    if src.stem is not None:
        slug = re.sub(r"[^a-z0-9]+", "-", src.stem.lower()).strip("-")
        return slug[: core.SKILL_NAME_MAX].rstrip("-") or src.stem
    raise PersonaError(
        "stdin carries no usable frontmatter name — pass --name", code="invalid_name"
    )


def _is_skill_name(name: str) -> bool:
    return (
        len(name) <= core.SKILL_NAME_MAX
        and core.SKILL_NAME.fullmatch(name) is not None
        and name.lower() not in core.RESERVED_NAMES
    )


def importable_skills(root: Path | None) -> list[SkillRef]:
    """Every skill in ``<config dir>/skills`` and ``<repo>/.claude/skills`` —
    personal first, as Claude Code ranks them — with an ``imported`` mark."""
    known = {persona.name for persona in core.catalogue(root)[0]}
    places: list[tuple[SkillScope, Path]] = [("user", _claude_home() / "skills")]
    if root is not None:
        places.append(("project", root / ".claude" / "skills"))
    refs: list[SkillRef] = []
    for scope, base in places:
        for directory in _skill_dirs(base):
            try:
                persona = core.load(directory, layer="user")
            except PersonaError as exc:
                refs.append(
                    SkillRef(
                        name=directory.name,
                        description="",
                        path=directory,
                        scope=scope,
                        recognised=False,
                        reason=exc.rule,
                        imported=directory.name in known,
                    )
                )
                continue
            refs.append(
                SkillRef(
                    name=persona.name,
                    description=persona.description,
                    path=directory,
                    scope=scope,
                    recognised=True,
                    imported=persona.name in known,
                )
            )
    return refs


def _skill_dirs(base: Path) -> list[Path]:
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return []
    return [
        entry
        for entry in entries
        if not entry.name.startswith(".")
        and entry.name.lower() not in core.RESERVED_NAMES
        and (entry / core.SKILL_FILE).is_file()
    ]


def new(name: str, *, layer: Layer, root: Path | None) -> Path:
    """Scaffold ``<layer>/<name>/SKILL.md`` from the template; the caller opens
    the editor."""
    base = _layer_dir(layer, root)
    text = core.render(name, _TEMPLATE_DESCRIPTION, _TEMPLATE_BODY, metadata={})
    core.parse_skill(text, name=name, path=base / name, layer=layer)
    dest = base / name
    if _occupied(dest):
        raise PersonaError(
            f"a {layer} persona named '{name}' already exists at {dest}", code="persona_exists"
        )
    _publish(dest, lambda staged: (staged / core.SKILL_FILE).write_text(text, encoding="utf-8"))
    return dest


def locate(name: str, root: Path | None) -> tuple[Layer, Path]:
    """The first directory called ``name`` in precedence order, loadable or not —
    the one that wins once it is valid, and so the one to edit or remove."""
    if _is_skill_name(name):
        for layer, base in core.layer_dirs(root):
            if (base / name).is_dir():
                return layer, base / name
    core.resolve(name, root)  # raises, listing the known names
    raise AssertionError("unreachable: resolve found a directory locate did not")


def shadows(persona: Persona, root: Path | None) -> list[Layer]:
    """The lower layers that also hold a directory of this persona's name."""
    below = False
    found: list[Layer] = []
    for layer, base in core.layer_dirs(root):
        if below and (base / persona.name).is_dir():
            found.append(layer)
        below = below or layer == persona.layer
    return found


def edit(name: str, *, root: Path | None, layer: Layer | None = None) -> Persona | None:
    """Open the persona's SKILL.md in ``$EDITOR`` via ``core.editor.edit_text``.

    ``None`` when nothing changed (or the editor exited non-zero). Text that
    fails the recognised test or a cap raises :class:`PersonaError` and the old
    file stays byte-identical. ``layer`` picks a layer's copy; by default, the
    winner's.
    """
    if layer is None:
        layer, directory = locate(name, root)
    else:
        directory = _layer_dir(layer, root) if layer != "bundled" else core.BUNDLED_DIR
        directory = directory / name
    if layer == "bundled":
        raise _bundled_refusal(name, "edited")
    skill = directory / core.SKILL_FILE
    try:
        before = skill.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise PersonaError("not UTF-8 text", path=skill, code="not_recognised") from None
    after = edit_text(before)
    if after is None or after == before:
        return None
    try:
        core.parse_skill(after, name=name, path=directory, layer=layer)
    except PersonaError as exc:
        raise PersonaError(
            f"{exc.rule} — the edit was not saved; {skill} is unchanged",
            path=skill,
            line=exc.line,
            code=exc.code,
        ) from None
    _replace_file(skill, after.encode("utf-8"))
    return core.load(directory, layer=layer)


def remove(name: str, *, layer: Layer, root: Path | None) -> Path:
    """Delete ``<layer>/<name>/``. Bundled personas cannot be removed."""
    if layer == "bundled":
        raise _bundled_refusal(name, "removed")
    directory = _layer_dir(layer, root) / name
    if not (_is_skill_name(name) and directory.is_dir()):
        raise PersonaError(
            f"no {layer} persona named '{name}' ({directory.parent})", code="unknown_persona"
        )
    _discard(directory)
    return directory


def export(
    name: str,
    *,
    root: Path | None,
    to: Path | None,
    skill: Literal["user", "project"] | None,
    force: bool,
) -> Path | str:
    """The SKILL.md text (no destination), or the directory written: ``to/<name>/``,
    ``<config dir>/skills/<name>/`` or ``<repo>/.claude/skills/<name>/``."""
    persona = core.resolve(name, root)
    if to is not None and skill is not None:
        raise PersonaError("choose one destination: --to DIR or --skill", code="usage")
    if to is None and skill is None:
        return (persona.path / core.SKILL_FILE).read_bytes().decode("utf-8")
    if skill == "user":
        base = _claude_home() / "skills"
    elif skill == "project":
        if root is None:
            raise PersonaError(
                "--skill --project needs a git repository around the working directory",
                code="no_project",
            )
        base = root / ".claude" / "skills"
    else:
        assert to is not None
        base = to.expanduser()
    dest = base / persona.name
    if _occupied(dest) and not force:
        raise PersonaError(f"{dest} already exists — --force replaces it", code="target_exists")

    def fill(staged: Path) -> None:
        shutil.copytree(persona.path, staged, dirs_exist_ok=True)
        if not (staged / core.PROVENANCE_FILE).exists():
            # A bundled or hand-authored persona has no sidecar; the exported copy
            # still says where it came from (§7: DIR/<name>/ is SKILL.md + .persona.json).
            _write_provenance(
                staged,
                source=str(persona.path),
                skill_bytes=(staged / core.SKILL_FILE).read_bytes(),
            )

    _publish(dest, fill)
    return dest


def validate(path: Path) -> tuple[Persona, list[str]]:
    """Load one directory (or its SKILL.md) as a persona, with its warnings.

    The layer reported is where the directory sits: bundled, user, or — for
    anywhere else — project.
    """
    directory = path.parent if path.name == core.SKILL_FILE and path.is_file() else path
    if not directory.is_dir():
        raise PersonaError("no such persona directory", path=path, code="source_not_found")
    persona = core.load(directory, layer=_layer_of(directory))
    return persona, core.warnings(persona)


def _layer_of(directory: Path) -> Layer:
    parent = directory.resolve().parent
    if parent == core.BUNDLED_DIR:
        return "bundled"
    if parent == dict(core.layer_dirs(None))["user"].resolve():
        return "user"
    return "project"


def _layer_dir(layer: Layer, root: Path | None) -> Path:
    if layer == "bundled":
        raise PersonaError(
            "the bundled layer ships with aisquare and is read-only — use --user or --project",
            code="bundled_read_only",
        )
    dirs = dict(core.layer_dirs(root))
    if layer not in dirs:
        raise PersonaError(
            "the project layer needs a git repository around the working directory",
            code="no_project",
        )
    return dirs[layer]


def _bundled_refusal(name: str, verb: Literal["edited", "removed"]) -> PersonaError:
    if verb == "removed":
        rule = (
            f"'{name}' is a bundled persona — it ships with aisquare and cannot be removed; a "
            "user or project persona of the same name shadows it instead"
        )
    else:
        rule = (
            f"'{name}' is a bundled persona and cannot be edited in place — copy it into a "
            f"layer you own first: `aisquare persona export {name} --to DIR`, then "
            f"`aisquare persona import DIR/{name} --user` (or `persona new NAME`)"
        )
    return PersonaError(rule, code="bundled_read_only")


def _write_provenance(directory: Path, *, source: str, skill_bytes: bytes) -> None:
    provenance = Provenance(
        source=source,
        source_sha256=hashlib.sha256(skill_bytes).hexdigest(),
        engine="copy",
        imported_at=datetime.now(UTC),
    )
    (directory / core.PROVENANCE_FILE).write_text(
        provenance.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )


def _occupied(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _publish(dest: Path, fill: Callable[[Path], object]) -> bool:
    """Build ``dest`` in a staging directory beside it, then rename it into place,
    replacing what was there. Returns whether something was replaced."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(dir=dest.parent, prefix=f".{dest.name}.new-"))
    try:
        fill(staged)
        replaced = _occupied(dest)
        if replaced:
            retired = Path(tempfile.mkdtemp(dir=dest.parent, prefix=f".{dest.name}.old-"))
            retired.rmdir()
            dest.rename(retired)
            staged.rename(dest)
            _discard(retired)
        else:
            staged.rename(dest)
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    return replaced


def _replace_file(path: Path, data: bytes) -> None:
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    staged = Path(name)
    try:
        with open(fd, "wb") as handle:
            handle.write(data)
        staged.replace(path)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def _discard(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)
