"""``aisquare persona`` — personas are Claude Code skill directories.

A persona is ``<name>/SKILL.md`` in one of three layers — project
(``<repo>/.aisquare/personas``), user (``$AISQUARE_HOME/personas``), bundled —
and the same directory is ``/name`` in Claude Code. Guide: docs/personas.md.
Every write goes through ``services.personas``; this module parses flags and
reports.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

import typer

from aisquare.cli.common import fail, resolve_pool
from aisquare.cli.fleet import ProjectRef, SessionRef, _fail_fleet, _project
from aisquare.core import personas as core
from aisquare.core.personas import Layer, Persona, PersonaError
from aisquare.core.state import get_state
from aisquare.core.workspace import git_common_root
from aisquare.services import fleet as fleet_service
from aisquare.services import personas as persona_service
from aisquare.services.personas import PersonaDraftView

app = typer.Typer(
    help="Personas: Claude Code skills an agent can be spawned as.", no_args_is_help=True
)

PersonaName = Annotated[str, typer.Argument(help="Persona name (its directory name).")]
UserFlag = Annotated[bool, typer.Option("--user", help="The user layer ($AISQUARE_HOME).")]
ProjectFlag = Annotated[
    bool, typer.Option("--project", help="The project layer (<repo>/.aisquare/personas).")
]


def _root() -> Path | None:
    """The project layer's root: the git repository around the working directory.

    Git rather than ``workspace.find_project_root``: that walk falls back to the
    working directory and counts ``~/.aisquare`` as a project marker, so run from
    ``$HOME`` it would read the user layer a second time as the project layer.
    """
    return git_common_root(Path.cwd())


@contextmanager
def _reported() -> Iterator[None]:
    """A persona refusal or a filesystem error is one line and exit 1, never a traceback."""
    try:
        yield
    except PersonaError as exc:
        # detail carries the sentence under --json too: the rule (and the line)
        # is what a script or the UI has to show, and `fail` prints it only to
        # a human otherwise.
        fail(str(exc), error=exc.code, detail=str(exc))
    except OSError as exc:
        where = f"{exc.filename}: " if exc.filename else ""
        fail(f"{where}{exc.strerror or exc}", error="io_error")


def _json() -> bool:
    return get_state().json_output


def _emit(payload: dict[str, Any]) -> None:
    typer.echo(json.dumps(payload, default=str))


def _warn(messages: list[str]) -> None:
    for message in messages:
        typer.echo(f"⚠ {message}", err=True)


def _editor_available() -> bool:
    """An editor was named, or there is a terminal for the ``vi`` fallback.

    Without either — a pipe, a test runner, the command sweeps — opening ``vi``
    would hang on no input, so the commands say how to edit instead.
    """
    return bool(os.environ.get("VISUAL") or os.environ.get("EDITOR")) or sys.stdin.isatty()


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _entry(persona: Persona, root: Path | None) -> dict[str, Any]:
    return {
        "name": persona.name,
        "description": persona.description,
        "roles": persona.roles,
        "tags": persona.tags,
        "layer": persona.layer,
        "path": str(persona.path),
        "files": persona.files,
        "shadows": persona_service.shadows(persona, root),
    }


@app.command("list")
def list_(
    tag: Annotated[
        str | None, typer.Option("--tag", help="Only personas carrying this persona-tags entry.")
    ] = None,
) -> None:
    """List personas in every layer, and any directory that did not load."""
    root = _root()
    personas, invalid = core.catalogue(root)
    if tag is not None:
        personas = [persona for persona in personas if tag in persona.tags]
    if _json():
        _emit(
            {
                "personas": [_entry(persona, root) for persona in personas],
                "invalid": [{"path": str(path), "reason": reason} for path, reason in invalid],
            }
        )
        return
    if not personas:
        typer.echo("No personas." if tag is None else f"No persona carries the tag '{tag}'.")
    width = max((len(persona.name) for persona in personas), default=0)
    for persona in personas:
        shadowed = persona_service.shadows(persona, root)
        mark = f"  (shadows {', '.join(shadowed)})" if shadowed else ""
        typer.echo(
            f"{persona.name:<{width}}  {persona.layer:<7}  {_one_line(persona.description)}{mark}"
        )
    for path, reason in invalid:
        typer.echo(f"✗ {path}: {reason}")


@app.command("show")
def show(name: PersonaName) -> None:
    """Print exactly what a session would be briefed with, then its provenance and files."""
    with _reported():
        persona = core.resolve(name, _root())
    lines = core.briefing(persona)
    warnings = core.warnings(persona)
    provenance = persona.provenance
    if _json():
        _emit(
            {
                **_entry(persona, None),
                "frontmatter": persona.frontmatter,
                "body": persona.body,
                "briefing": "\n".join(lines),
                "provenance": None if provenance is None else provenance.model_dump(mode="json"),
                "warnings": warnings,
            }
        )
        return
    typer.echo("\n".join(lines))
    typer.echo("")
    if provenance is None:
        typer.echo(f"provenance: none — {persona.layer} persona, not imported")
    else:
        model = f" · model {provenance.model}" if provenance.model else ""
        condensed = " · condensed" if provenance.condensed else ""
        typer.echo(
            f"provenance: {provenance.engine} from {provenance.source} · "
            f"sha256 {provenance.source_sha256[:12]} · {provenance.imported_at:%Y-%m-%d %H:%M}Z"
            f"{model}{condensed}"
        )
    typer.echo(f"files: {', '.join(persona.files) if persona.files else 'SKILL.md only'}")
    typer.echo(f"path: {persona.path}")
    _warn(warnings)


@app.command("new")
def new(name: PersonaName, user: UserFlag = False, project: ProjectFlag = False) -> None:
    """Scaffold a persona from the template and open it in $EDITOR (default: --user)."""
    root = _root()
    layer: Layer = resolve_pool(user, project) or "user"
    with _reported():
        path = persona_service.new(name, layer=layer, root=root)
    if not _json():
        typer.echo(f"✓ created {layer} persona {name}: {path / core.SKILL_FILE}")
    edited: Persona | None = None
    if _editor_available():
        with _reported():
            edited = persona_service.edit(name, root=root, layer=layer)
    elif not _json():
        typer.echo(f"  edit it with: aisquare persona edit {name}")
    if _json():
        _emit({"name": name, "layer": layer, "path": str(path), "edited": edited is not None})
    elif edited is not None:
        _warn(core.warnings(edited))


@app.command("edit")
def edit(name: PersonaName) -> None:
    """Edit a project or user persona's SKILL.md in $EDITOR; invalid text is not saved."""
    root = _root()
    with _reported():
        layer, directory = persona_service.locate(name, root)
        if layer != "bundled" and not _editor_available():
            fail(
                "no editor to open — set $EDITOR (or $VISUAL), or run this at a terminal",
                error="no_editor",
            )
        persona = persona_service.edit(name, root=root)
    if _json():
        warnings = [] if persona is None else core.warnings(persona)
        _emit(
            {
                "name": name,
                "layer": layer,
                "path": str(directory),
                "changed": persona is not None,
                "warnings": warnings,
            }
        )
        return
    if persona is None:
        typer.echo(f"unchanged: {directory / core.SKILL_FILE}")
        return
    typer.echo(f"✓ updated {layer} persona {name}: {directory / core.SKILL_FILE}")
    _warn(core.warnings(persona))


@app.command("rm")
def rm(name: PersonaName, user: UserFlag = False, project: ProjectFlag = False) -> None:
    """Remove a project or user persona (default: the layer it resolves from)."""
    root = _root()
    with _reported():
        layer: Layer = resolve_pool(user, project) or persona_service.locate(name, root)[0]
        path = persona_service.remove(name, layer=layer, root=root)
    if _json():
        _emit({"name": name, "layer": layer, "path": str(path)})
    else:
        typer.echo(f"✓ removed {layer} persona {name} ({path})")


@app.command("validate")
def validate(
    path: Annotated[Path, typer.Argument(help="A persona directory or its SKILL.md.")],
) -> None:
    """Check a directory against the persona rules: errors exit 1, warnings do not."""
    with _reported():
        persona, warnings = persona_service.validate(path)
    if _json():
        _emit(
            {"name": persona.name, "path": str(persona.path), "valid": True, "warnings": warnings}
        )
        return
    typer.echo(f"✓ {persona.name}: a valid persona ({len(persona.body):,}-character body)")
    _warn(warnings)


def _progress(text: str) -> None:
    if not (_json() or get_state().quiet):
        typer.echo(text, err=True)


def _confirmer(yes: bool, asked: list[bool]) -> Callable[[PersonaDraftView], bool]:
    """y/N at a terminal; ``--yes`` answers for you. Under ``--json`` or without a
    terminal nothing can be asked, so the draft stays kept and the import exits
    ``needs_confirmation`` (§3.7). ``asked`` records whether a human said no."""

    def confirm(draft: PersonaDraftView) -> bool:
        if yes:
            return True
        if _json() or not sys.stdin.isatty():
            return False
        asked.append(True)
        _show_draft(draft)
        return typer.confirm(f"Save persona '{draft.name}'?", default=False, err=True)

    return confirm


def _show_draft(draft: PersonaDraftView) -> None:
    """The frontmatter, the first twelve body lines, the character count, engine and
    model — and the engine's notes on what it dropped (§3.9)."""
    frontmatter = draft.skill_md.split("\n---\n", 1)[0]
    lines = draft.body.splitlines()
    typer.echo(f"{frontmatter}\n---", err=True)
    typer.echo("\n".join(lines[:12]), err=True)
    if len(lines) > 12:
        typer.echo(f"… {len(lines) - 12} more lines", err=True)
    model = f" · {draft.model}" if draft.model else ""
    typer.echo(f"{len(draft.body):,} characters · {draft.engine}{model}", err=True)
    for note in draft.notes:
        typer.echo(f"  note: {note}", err=True)


@app.command("import")
def import_(
    source: Annotated[
        str | None,
        typer.Argument(
            help="A skill directory, a SKILL.md or any text file, - for stdin, an https:// "
            "URL, or a Claude Code skill's name."
        ),
    ] = None,
    user: UserFlag = False,
    project: ProjectFlag = False,
    name: Annotated[
        str | None, typer.Option("--name", help="Save under this name (the directory only).")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Replace a persona of the same name in that layer.")
    ] = False,
    list_skills: Annotated[
        bool, typer.Option("--list", help="List the skills in Claude Code's skill directories.")
    ] = False,
    llm: Annotated[
        bool | None,
        typer.Option(
            "--llm/--no-llm",
            help="Force the LLM path (to reshape a skill), or forbid it (never spend a token).",
        ),
    ] = None,
    condense: Annotated[
        bool,
        typer.Option("--condense", help="Rewrite the body shorter through an engine."),
    ] = False,
    engine: Annotated[
        str | None,
        typer.Option(
            "--engine",
            help="auto, manager, api or off (default: [persona.import] engine).",
            metavar="ENGINE",
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(
            "--model", help="The api engine's model (default: [persona.import] api_model)."
        ),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Save an engine's draft without asking.")
    ] = False,
) -> None:
    """Import a persona: a skill is copied byte for byte; anything else is converted
    by an engine and shown before it is saved (default: --user)."""
    root = _root()
    if list_skills:
        _emit_skills(persona_service.importable_skills(root))
        return
    if source is None:
        fail(
            "name a source: a skill directory, a SKILL.md or Markdown file, - for stdin, or a "
            "skill from `aisquare persona import --list`",
            error="missing_source",
        )
    layer: Layer = resolve_pool(user, project) or "user"
    mode: Literal["auto", "always", "never"] = (
        "auto" if llm is None else "always" if llm else "never"
    )
    asked: list[bool] = []
    with _reported():
        try:
            result = persona_service.import_source(
                source,
                layer=layer,
                root=root,
                name=name,
                force=force,
                llm=mode,
                condense=condense,
                engine=engine,
                model=model,
                confirm=_confirmer(yes, asked),
                progress=_progress,
            )
        except persona_service.DraftKept as exc:
            # "n" at a terminal is not_confirmed; no terminal to ask is needs_confirmation.
            code = "needs_confirmation" if exc.code == "not_confirmed" and not asked else exc.code
            fail(str(exc), error=code, ref=str(exc.draft_path), detail=str(exc))
    persona = result.persona
    if _json():
        _emit(
            {
                "name": persona.name,
                "layer": persona.layer,
                "path": str(persona.path),
                "engine": result.engine,
                "model": result.model,
                "source": result.source,
                "replaced": result.replaced,
                "warnings": result.warnings,
            }
        )
        return
    replaced = " (replaced)" if result.replaced else ""
    how = f"{result.engine}, {result.model}" if result.model else result.engine
    typer.echo(f"✓ imported {persona.name} ({how}) into {persona.layer}: {persona.path}{replaced}")
    _warn(result.warnings)


def _emit_skills(skills: list[persona_service.SkillRef]) -> None:
    if _json():
        _emit({"skills": [skill.model_dump(mode="json") for skill in skills]})
        return
    if not skills:
        typer.echo("No Claude Code skills found in the personal or project skill directories.")
        return
    width = max(len(skill.name) for skill in skills)
    for skill in skills:
        mark = "imported" if skill.imported else " " * len("imported")
        about = (
            _one_line(skill.description) if skill.recognised else f"✗ {skill.reason or 'invalid'}"
        )
        typer.echo(f"{skill.name:<{width}}  {skill.scope:<7}  {mark}  {about}")


@app.command("export")
def export(
    name: PersonaName,
    to: Annotated[
        Path | None, typer.Option("--to", help="Write the directory as DIR/<name>/.")
    ] = None,
    skill: Annotated[
        bool,
        typer.Option("--skill", help="Write into Claude Code's skills (with --user or --project)."),
    ] = False,
    user: UserFlag = False,
    project: ProjectFlag = False,
    force: Annotated[bool, typer.Option("--force", help="Replace an existing target.")] = False,
) -> None:
    """Print a persona's SKILL.md, or write its directory elsewhere (default: stdout)."""
    scope = resolve_pool(user, project)
    if skill and scope is None:
        fail(
            "--skill needs --user (<config dir>/skills) or --project (<repo>/.claude/skills)",
            error="usage",
        )
    if scope is not None and not skill:
        fail("--user and --project pick Claude Code's skill directory — add --skill", error="usage")
    with _reported():
        result = persona_service.export(
            name, root=_root(), to=to, skill=scope if skill else None, force=force
        )
    if isinstance(result, str):
        if _json():
            _emit({"name": name, "skill_md": result})
        else:
            typer.echo(result, nl=False)
        return
    if _json():
        _emit({"name": name, "path": str(result), "skill": scope if skill else None})
        return
    slash = f" — it is /{name} in Claude Code now" if skill else ""
    typer.echo(f"✓ exported {name} to {result}{slash}")


@app.command("attach")
def attach(
    name: PersonaName,
    to: Annotated[
        str,
        typer.Option(
            "--to", help="The running agent's label (see `aisquare fleet ls`).", metavar="LABEL"
        ),
    ],
    project: ProjectRef = None,
    as_session: SessionRef = None,
) -> None:
    """Give a running fleet agent a persona now — delivered as `fleet tell` delivers,
    kept on its rows so a /clear or a restart briefs it again."""
    target = _project(project)
    try:
        receipt = fleet_service.attach_persona(target, to, name, sender=as_session)
    except fleet_service.FleetError as exc:
        _fail_fleet(exc)
    if _json():
        _emit(
            {
                "persona": receipt.persona,
                "label": receipt.agent.label,
                "agent": receipt.agent.id,
                "delivered": receipt.delivered,
                "replaced": receipt.replaced,
                "how": receipt.how,
            }
        )
        return
    typer.echo(f"✓ attached {receipt.persona} to {receipt.agent.label} ({receipt.delivered})")
    typer.echo(f"  {receipt.how}")
