"""The file operations behind ``aisquare persona`` — every persona write goes through here.

Import copies bytes (docs/plans/spawn-personas.md §3.3, §3.9): aisquare never
rewrites a file it did not author, and the one file it does author beside a
skill — the provenance sidecar — is ``.persona.json``, a dotfile Claude Code
ignores. Every write lands in a dot-named staging directory inside the target's
parent and is renamed into place, so a reader never sees half a persona and a
failed write leaves the old one untouched.

A source that is already a skill takes the RECOGNISED path — bytes copied. Anything
else (or ``--llm``/``--condense``) takes the LLM path: an engine from
``services.persona_import`` drafts a skill, the same validator as the recognised path
checks it (one retry with the rule), the draft is kept under ``.drafts`` before
anyone is asked, and only a confirmed draft becomes a persona (§3.9).
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
from typing import Literal, cast

from pydantic import BaseModel

from aisquare.core import personas as core
from aisquare.core.agents import _claude_home
from aisquare.core.config import PersonaImportSettings, load_config
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
DRAFTS_DIR = ".drafts"
"""Under the user layer: every LLM draft, kept before anyone is asked (§3.7)."""
_ENGINES = ("auto", "manager", "api", "off")
_FETCH_TIMEOUT_SECONDS = 20.0
_FETCH_MAX_BYTES = 2 * 1024 * 1024


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


class DraftKept(PersonaError):
    """An LLM import that did not become a persona — but its draft is on disk.

    ``code`` says why: ``import_invalid`` (failed validation twice), ``not_confirmed``
    (the confirmation said no, or could not be asked) or ``persona_exists``.
    ``draft_path`` is the kept SKILL.md; ``aisquare persona import <draft_path>``
    finishes it on the recognised path.
    """

    def __init__(self, rule: str, *, draft_path: Path, code: str) -> None:
        super().__init__(rule, code=code)
        self.draft_path = draft_path


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
    stdin: bytes | None = None,
) -> ImportResult:
    """Import ``source`` into ``layer``: ``-`` (stdin — or ``stdin``'s bytes, for a UI
    that has no stdin to hand over), a skill directory, a SKILL.md or other file, an
    ``https://`` URL, or the name of a skill in Claude Code's skill directories.

    A recognised skill is copied. Anything else — or ``llm="always"``, or
    ``condense`` — goes through an engine (``engine``/``model`` default to
    ``[persona.import]``); ``llm="never"`` refuses instead. ``confirm`` and
    ``progress`` are the UI's seam (§4.3): the CLI passes y/N and a stderr printer,
    the TUI passes modals.
    """
    base = _layer_dir(layer, root)
    if engine is not None and engine not in _ENGINES:
        raise PersonaError(
            f"unknown engine {engine!r} — one of: {', '.join(_ENGINES)}", code="usage"
        )
    src = _read_source(source, root, stdin=stdin)
    where: Path | str = src.directory or (src.origin if src.origin != "stdin" else "stdin")
    text: str | None
    try:
        text = src.skill_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    probe: Persona | None = None
    refusal: PersonaError | None = None
    if text is None:
        refusal = PersonaError("not UTF-8 text", code="not_recognised")
    else:
        try:
            probe = core.parse_skill(text, name=_PROBE_NAME, path=base / _PROBE_NAME, layer=layer)
        except PersonaError as exc:
            # A skill over the hard cap is exactly what --condense is for.
            if exc.code != "not_recognised" and not (condense and exc.code == "too_large"):
                raise PersonaError(exc.rule, path=where, line=exc.line, code=exc.code) from None
            refusal = exc
    if probe is None or condense or llm == "always":
        if llm == "never":
            if refusal is None:
                raise PersonaError(
                    "--llm and --condense need the LLM path, and --no-llm forbids it",
                    path=where,
                    code="no_import_engine",
                )
            raise _not_recognised(where, refusal.rule, refusal.line)
        if text is None:
            raise PersonaError(
                "not UTF-8 text — an import engine reads text", path=where, code="not_recognised"
            )
        return _import_with_llm(
            src,
            text,
            where=where,
            base=base,
            layer=layer,
            name=name,
            force=force,
            condense=condense,
            engine=engine,
            model=model,
            confirm=confirm,
            progress=progress,
        )
    assert text is not None

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
        carried = _engine_provenance(src)
        if carried is not None:
            # A kept LLM draft finished here still says which engine wrote it.
            (staged / core.PROVENANCE_FILE).write_text(
                carried.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
        else:
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


def _not_recognised(where: Path | str, rule: str, line: int | None) -> PersonaError:
    return PersonaError(
        f"not a recognised skill: {rule}; converting it takes the LLM import path, and "
        "--no-llm forbids it",
        path=where,
        line=line,
        code="not_recognised",
    )


def _import_with_llm(
    src: _Source,
    text: str,
    *,
    where: Path | str,
    base: Path,
    layer: Layer,
    name: str | None,
    force: bool,
    condense: bool,
    engine: str | None,
    model: str | None,
    confirm: Callable[[PersonaDraftView], bool],
    progress: Callable[[str], None] | None,
) -> ImportResult:
    """§3.9's LLM path: draft → render → the recognised path's validator (one retry
    with its rule) → keep the draft → confirm → save with provenance."""
    from aisquare.services import persona_import

    if name is not None and _occupied(base / name) and not force:
        raise PersonaError(
            f"a {layer} persona named '{name}' already exists at {base / name} — --force "
            "replaces it, --name picks another",
            code="persona_exists",
        )
    settings = _import_settings()
    chosen_engine = cast(persona_import.Engine, engine or settings.engine)
    api_model = model or settings.api_model
    feedback: str | None = None
    attempt = 0
    while True:
        attempt += 1
        try:
            drafted, ran, ran_model = persona_import.draft(
                text,
                engine=chosen_engine,
                condense=condense,
                model=api_model,
                feedback=feedback,
                progress=progress,
            )
        except persona_import.ImportRefused as exc:
            raise PersonaError(exc.message, path=where, code=exc.code) from None
        skill_name = name or drafted.name.strip()
        rendered = core.render(
            skill_name, drafted.description, drafted.body, metadata={"persona-source": src.origin}
        )
        kept = _keep_draft(
            skill_name, rendered, src=src, engine=ran, model=ran_model, condensed=condense
        )
        problem = _draft_problem(rendered, skill_name, base=base, layer=layer, condense=condense)
        if problem is None:
            break
        if attempt == 2:
            raise DraftKept(
                f"the {ran} engine's draft failed validation twice ({problem}) — the draft is "
                f"kept at {kept}",
                draft_path=kept,
                code="import_invalid",
            )
        feedback = problem

    dest = base / skill_name
    if _occupied(dest) and not force:
        raise DraftKept(
            f"a {layer} persona named '{skill_name}' already exists at {dest} — --force "
            f"replaces it, --name picks another; the draft is kept at {kept}",
            draft_path=kept,
            code="persona_exists",
        )
    view = PersonaDraftView(
        name=skill_name,
        description=drafted.description.strip(),
        body=drafted.body.strip(),
        skill_md=rendered,
        engine=ran,
        model=ran_model,
        notes=drafted.notes,
        draft_path=kept,
    )
    if not confirm(view):
        raise DraftKept(
            f"not saved — the draft is kept at {kept}; `aisquare persona import {kept}` saves it",
            draft_path=kept,
            code="not_confirmed",
        )

    def fill(staged: Path) -> None:
        (staged / core.SKILL_FILE).write_text(rendered, encoding="utf-8")
        _write_provenance(
            staged,
            source=src.origin,
            skill_bytes=src.skill_bytes,
            engine=ran,
            model=ran_model,
            condensed=condense,
        )

    replaced = _publish(dest, fill)
    _discard(kept.parent)
    persona = core.load(dest, layer=layer)
    return ImportResult(
        persona=persona,
        engine=ran,
        model=ran_model,
        source=src.origin,
        replaced=replaced,
        warnings=core.warnings(persona),
    )


def _draft_problem(
    rendered: str, name: str, *, base: Path, layer: Layer, condense: bool
) -> str | None:
    """The recognised path's rules, plus ``--condense``'s promise of a short body."""
    try:
        persona = core.parse_skill(rendered, name=name, path=base / name, layer=layer)
    except PersonaError as exc:
        return exc.rule
    if condense and len(persona.body) > core.BODY_SOFT_CAP:
        return (
            f"the condensed body is {len(persona.body):,} characters, over the "
            f"{core.BODY_SOFT_CAP:,} asked for"
        )
    return None


def _keep_draft(
    name: str,
    rendered: str,
    *,
    src: _Source,
    engine: Literal["manager", "api"],
    model: str | None,
    condensed: bool,
) -> Path:
    """``$AISQUARE_HOME/personas/.drafts/<name>/SKILL.md`` — written before anyone is
    asked, so nothing an engine produced is lost (§3.7). Returns the SKILL.md."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[: core.SKILL_NAME_MAX].rstrip("-")
    dest = dict(core.layer_dirs(None))["user"] / DRAFTS_DIR / (slug or "draft")

    def fill(staged: Path) -> None:
        (staged / core.SKILL_FILE).write_text(rendered, encoding="utf-8")
        _write_provenance(
            staged,
            source=src.origin,
            skill_bytes=src.skill_bytes,
            engine=engine,
            model=model,
            condensed=condensed,
        )

    _publish(dest, fill)
    return dest / core.SKILL_FILE


def _engine_provenance(src: _Source) -> Provenance | None:
    """An engine's sidecar on a source directory — a kept draft being finished."""
    if src.directory is None:
        return None
    try:
        carried = Provenance.model_validate_json(
            (src.directory / core.PROVENANCE_FILE).read_bytes()
        )
    except (OSError, ValueError):
        return None
    return carried if carried.engine != "copy" else None


def _import_settings() -> PersonaImportSettings:
    """``[persona.import]``. A config that will not load costs the customisation,
    never the import — the defaults apply."""
    try:
        return load_config().persona.import_
    except Exception:
        return PersonaImportSettings()


def _read_source(source: str, root: Path | None, *, stdin: bytes | None = None) -> _Source:
    """Resolved in §3.9's order: stdin, an existing path, a URL, a skill name."""
    if source == "-":
        data = stdin if stdin is not None else sys.stdin.buffer.read()
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
                "to convert something else, import the file itself",
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
    if source.startswith("http://"):
        raise PersonaError(
            "http:// is refused — a persona is always-injected context; import it over https://",
            path=source,
            code="unsupported_source",
        )
    if source.startswith("https://"):
        return _fetch(source)
    for ref in importable_skills(root):
        if ref.name == source:
            return _read_source(str(ref.path), root)
    raise PersonaError(
        f"no such file, directory or Claude Code skill: {source} "
        "(`aisquare persona import --list` shows the skills)",
        code="source_not_found",
    )


def _fetch(url: str) -> _Source:
    """An ``https://`` source, with the stdlib, inside this function only (the CLI's
    startup never pays for it): 20 s, 2 MB, refused past either."""
    from urllib.error import URLError
    from urllib.parse import urlsplit
    from urllib.request import Request, urlopen

    request = Request(url, headers={"User-Agent": "aisquare-cli persona import"})
    try:
        with urlopen(request, timeout=_FETCH_TIMEOUT_SECONDS) as response:
            data = response.read(_FETCH_MAX_BYTES + 1)
    except (URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise PersonaError(
            f"could not fetch it ({reason})", path=url, code="source_unreadable"
        ) from None
    if len(data) > _FETCH_MAX_BYTES:
        raise PersonaError(
            f"the response is over {_FETCH_MAX_BYTES // (1024 * 1024)} MB",
            path=url,
            code="source_too_large",
        )
    if not data.strip():
        raise PersonaError("the response is empty", path=url, code="source_empty")
    stem = Path(urlsplit(url).path).stem or None
    return _Source(origin=url, skill_bytes=data, directory=None, stem=stem)


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


def _target(name: str, root: Path | None, layer: Layer | None) -> tuple[Layer, Path]:
    """The directory an edit acts on: ``layer``'s copy, or by default the winner's."""
    if layer is None:
        return locate(name, root)
    if layer == "bundled":
        return layer, core.BUNDLED_DIR / name
    return layer, _layer_dir(layer, root) / name


def save(name: str, text: str, *, root: Path | None, layer: Layer | None = None) -> Persona:
    """Write ``text`` as the persona's SKILL.md — the one writer behind
    ``persona edit`` and the UI's editor (§4.4).

    The text is held to the recognised test and the caps first: a
    :class:`PersonaError` leaves the old file byte-identical. Bundled personas
    are refused. The write is staged beside the file and renamed over it.
    """
    layer, directory = _target(name, root, layer)
    if layer == "bundled":
        raise _bundled_refusal(name, "edited")
    if not (_is_skill_name(name) and directory.is_dir()):
        raise PersonaError(
            f"no {layer} persona named '{name}' ({directory.parent})", code="unknown_persona"
        )
    skill = directory / core.SKILL_FILE
    try:
        core.parse_skill(text, name=name, path=directory, layer=layer)
    except PersonaError as exc:
        raise PersonaError(
            f"{exc.rule} — not saved; {skill} is unchanged",
            path=skill,
            line=exc.line,
            code=exc.code,
        ) from None
    _replace_file(skill, text.encode("utf-8"))
    return core.load(directory, layer=layer)


def edit(name: str, *, root: Path | None, layer: Layer | None = None) -> Persona | None:
    """Open the persona's SKILL.md in ``$EDITOR`` via ``core.editor.edit_text``,
    then :func:`save` the result.

    ``None`` when nothing changed (or the editor exited non-zero). Text that
    fails the recognised test or a cap raises :class:`PersonaError` and the old
    file stays byte-identical. ``layer`` picks a layer's copy; by default, the
    winner's.
    """
    layer, directory = _target(name, root, layer)
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
    return save(name, after, root=root, layer=layer)


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


def _write_provenance(
    directory: Path,
    *,
    source: str,
    skill_bytes: bytes,
    engine: Literal["copy", "manager", "api"] = "copy",
    model: str | None = None,
    condensed: bool = False,
) -> None:
    provenance = Provenance(
        source=source,
        source_sha256=hashlib.sha256(skill_bytes).hexdigest(),
        engine=engine,
        model=model,
        imported_at=datetime.now(UTC),
        condensed=condensed,
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
