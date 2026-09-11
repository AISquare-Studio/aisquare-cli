"""Local character management. No persona data enters working-agent context.

Files are versioned and selections are separate from the CLI work configuration.
Writers serialize on a small local lock; validated files publish atomically.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from importlib import import_module
from importlib.resources import files
from pathlib import Path
from typing import Any, NoReturn

from pydantic import BaseModel, ConfigDict, Field

from aisquare.core import paths
from aisquare.core.personas import (
    MAX_PACK_BYTES,
    ROLES,
    PersonaPack,
    base_role,
    caption,
    clean_text,
    pack_bytes,
    parse_pack,
    plain_text,
    single_line,
    validate_identifier,
    validate_version,
    voice_text,
)
from aisquare.models import ProjectInfo, TeamEvent


class PersonaReceipt(BaseModel):
    action: str
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    editor: PersonaPack | None = None


class PackSummary(BaseModel):
    id: str
    version: str
    name: str
    description: str
    bundled: bool
    reference: str


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool | None = None
    default: str | None = None
    roles: dict[str, str] = Field(default_factory=dict)
    #: Opt-in: the selected pack's voice is appended to NEW agents' system prompts.
    voice: bool = False


class Selections(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: int = 1
    global_default: str | None = None
    projects: dict[str, Selection] = Field(default_factory=dict)


def _root() -> Path:
    home = paths.aisquare_home().expanduser().resolve()
    root = home / "personas"
    if root.is_symlink():
        raise ValueError("persona directory must not be a symlink")
    return root


def _safe(path: Path) -> Path:
    root = _root()
    if not path.is_relative_to(root):
        raise ValueError("persona path escaped its home")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("persona paths must not contain symlinks")
        if part == root:
            break
    return path


@contextmanager
def _locked() -> Iterator[None]:
    root = _root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = _safe(root / ".lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if os.name == "nt":
            msvcrt = import_module("msvcrt")

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
        else:
            import fcntl
        deadline = time.monotonic() + 5
        while True:
            try:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, PermissionError):
                if time.monotonic() >= deadline:
                    raise ValueError("persona settings are busy; retry shortly") from None
                time.sleep(0.02)
        yield
    finally:
        os.close(fd)


def _atomic(path: Path, content: bytes) -> None:
    _safe(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _safe(path)
    fd, temporary = tempfile.mkstemp(prefix=".persona-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _safe(path)
        os.replace(temporary, path)
        if os.name != "nt":
            parent_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _settings() -> Selections:
    path = _safe(_root() / "selections.json")
    if not path.exists():
        return Selections()
    with path.open("rb") as stream:
        raw = stream.read(1_048_577)
    if len(raw) > 1_048_576:
        raise ValueError(f"persona selections file is too large: {path}")
    try:
        selections = Selections.model_validate_json(raw)
    except (ValueError, RecursionError) as exc:
        # Name the file: the recovery is to fix or delete it, and `persona off` /
        # `persona reset` below start from empty choices rather than staying bricked.
        raise ValueError(
            f"persona selections file is damaged: {path} ({type(exc).__name__}); "
            "`asq persona reset` or `asq persona off` rewrites it from empty choices"
        ) from None
    if selections.schema_version != 1:
        raise ValueError(f"unsupported persona selections schema in {path}")
    return selections


def _settings_for_write(action: str) -> tuple[Selections, str | None]:
    """Damaged selections block `use` (a choice would be lost) but not `off`/`reset`."""
    try:
        return _settings(), None
    except ValueError as exc:
        if action not in {"off", "reset"}:
            raise
        return Selections(), f"Warning: {exc}. Rewritten from empty choices."


def _save_settings(settings: Selections) -> None:
    _atomic(_root() / "selections.json", settings.model_dump_json(indent=2).encode())


def _bundled() -> list[PersonaPack]:
    directory = files("aisquare").joinpath("personas")
    return [
        parse_pack(directory.joinpath(f"{name}.json").read_bytes())
        for name in ("studio", "mission-control")
    ]


def _all_packs() -> list[tuple[PersonaPack, bool]]:
    found = [(pack, True) for pack in _bundled()]
    root = _root()
    if root.exists():
        for path in sorted(root.glob("*/*/pack.json")):
            try:
                with _safe(path).open("rb") as stream:
                    pack = parse_pack(stream.read(MAX_PACK_BYTES + 1))
                if path.parent.name != pack.version or path.parent.parent.name != pack.id:
                    continue
                found.append((pack, False))
            except (OSError, ValueError):
                # A damaged optional pack cannot suppress the original activity.
                continue
    return found


def list_packs() -> list[PackSummary]:
    """Bundled and valid installed versions, usable without a registered project."""
    return [
        PackSummary(
            id=p.id,
            version=p.version,
            name=p.name,
            description=p.description,
            bundled=bundled,
            reference=p.reference,
        )
        for p, bundled in _all_packs()
    ]


def load_pack(reference: str) -> PersonaPack:
    parts = reference.split("@")
    validate_identifier(parts[0])
    if len(parts) > 2:
        raise ValueError("use an ID or ID@version")
    if len(parts) == 2:
        validate_version(parts[1])
    matches = [
        p
        for p, _ in _all_packs()
        if p.id == parts[0] and (len(parts) == 1 or p.version == parts[1])
    ]
    if not matches:
        raise ValueError(f"persona {reference!r} is missing or damaged")
    return max(matches, key=lambda p: tuple(int(n) for n in p.version.split(".")))


def _pack_error(exc: Exception, what: str) -> ValueError:
    """Describe WHERE a document failed validation without echoing its content."""
    from pydantic import ValidationError

    if isinstance(exc, ValidationError):
        places = sorted(
            {".".join(str(part) for part in error["loc"]) or "<root>" for error in exc.errors()}
        )
        shown = ", ".join(places[:6]) + (", …" if len(places) > 6 else "")
        return ValueError(f"{what} is not a valid persona pack (invalid: {shown})")
    return ValueError(f"{what} is not a valid persona pack: {exc}")


def install_pack(pack: PersonaPack, *, source: str = "local") -> PersonaReceipt:
    """Copy validated data; different content can never overwrite a saved version."""
    pack = parse_pack(pack_bytes(pack))
    content = pack_bytes(pack)
    digest = hashlib.sha256(content).hexdigest()
    with _locked():
        for existing, _ in _all_packs():
            if existing.reference == pack.reference:
                if pack_bytes(existing) != content:
                    raise ValueError(
                        "different content already uses this ID/version; choose a new version"
                    )
                return PersonaReceipt(
                    action="add",
                    message=f"{pack.reference} is already installed.",
                    data={"reference": pack.reference, "sha256": digest},
                )
        target = _safe(_root() / pack.id / pack.version / "pack.json")
        if target.exists():
            raise ValueError("this ID/version exists but is damaged; choose a new version")
        metadata = json.dumps(
            {"source": source, "sha256": digest, "installed_at": time.time()}
        ).encode()
        _atomic(target.with_name("provenance.json"), metadata)
        _atomic(target, content)
    return PersonaReceipt(
        action="add",
        message=f"Installed {pack.reference}; not activated.",
        data={"reference": pack.reference, "sha256": digest},
    )


def import_pack(path: Path) -> PersonaReceipt:
    source = path.expanduser()
    if not source.exists():
        raise ValueError(f"persona file not found: {source}")
    if source.is_symlink() or not source.is_file():
        raise ValueError("import needs a regular JSON file, not a symlink or directory")
    with source.open("rb") as stream:
        raw = stream.read(MAX_PACK_BYTES + 1)
    try:
        pack = parse_pack(raw)
    except ValueError as exc:
        raise _pack_error(exc, str(source)) from None
    return install_pack(pack, source=f"file:{source.resolve()}")


def download_pack(url: str) -> PersonaReceipt:
    # Network modules load only here: nothing else in the CLI pays for ssl/http.
    import http.client
    import urllib.error
    import urllib.parse
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(
            self,
            req: urllib.request.Request,
            fp: Any,
            code: int,
            msg: str,
            headers: Any,
            newurl: str,
        ) -> None:
            raise ValueError("persona downloads do not follow redirects; use the final HTTPS URL")

    address = urllib.parse.urlsplit(url)
    if address.scheme != "https" or not address.hostname or address.username or address.password:
        raise ValueError("persona downloads require an HTTPS URL without embedded credentials")
    if len(url) > 2048 or address.fragment:
        raise ValueError("download URL is too long or contains a fragment")
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=10) as response:
            length = response.headers.get("Content-Length")
            if length is not None and int(length) > MAX_PACK_BYTES:
                raise ValueError("persona download exceeds the size limit")
            started = time.monotonic()
            content = bytearray()
            while True:
                chunk = response.read1(min(8192, MAX_PACK_BYTES + 1 - len(content)))
                content.extend(chunk)
                if len(content) > MAX_PACK_BYTES:
                    raise ValueError("persona download exceeds the size limit")
                if time.monotonic() - started > 30:
                    raise ValueError("persona download exceeded 30 seconds")
                if not chunk:
                    break
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
        raise ValueError(f"persona download failed: {exc}") from exc
    try:
        pack = parse_pack(bytes(content))
    except ValueError as exc:
        # Never echo a remote document into the operator's terminal.
        raise _pack_error(exc, "the downloaded document") from None
    return install_pack(pack, source=url)


def resolve_project(project: ProjectInfo | None = None, ref: str | None = None) -> ProjectInfo:
    """Explicit UI project wins; shell uses registered containing project, never a pin."""
    from aisquare.services import project as projects

    if ref:
        try:
            return projects.resolve(ref)
        except (KeyError, ValueError) as exc:
            raise ValueError(
                f"unknown project {ref!r}; `asq project list` shows registered projects"
            ) from exc
    if project is not None:
        return project
    cwd = Path.cwd().resolve()
    matches = [p for p in projects.list_projects() if cwd.is_relative_to(p.root.resolve())]
    if matches:
        return max(matches, key=lambda p: len(p.root.parts))
    raise ValueError(
        "no registered project contains this directory; use --project PROJECT or --global"
    )


def _effective(settings: Selections, project: ProjectInfo | None, role: str) -> str | None:
    selection = settings.projects.get(project.id, Selection()) if project else Selection()
    if selection.enabled is False:
        return None
    reference = selection.roles.get(base_role(role), selection.default or settings.global_default)
    return None if reference == "off" else reference


def persona_status(project: ProjectInfo | None = None) -> dict[str, Any]:
    settings = _settings()
    choice = settings.projects.get(project.id, Selection()) if project else Selection()
    effective: dict[str, Any] = {}
    active = False
    for role in ROLES:
        reference = _effective(settings, project, role)
        try:
            if reference:
                load_pack(reference)
                active = True
            effective[role] = reference or "off"
        except ValueError:
            effective[role] = f"off (missing or damaged: {reference})"
    voiced: list[str] = []
    if choice.voice:
        for role in ROLES:
            reference = _effective(settings, project, role)
            try:
                if reference and voice_text(load_pack(reference), role):
                    voiced.append(role)
            except ValueError:
                continue
    return {
        "scope": project.id if project else "global",
        "enabled": active,
        "project_off": choice.enabled is False,
        "global_default": settings.global_default or "off",
        "default": choice.default,
        "role_overrides": choice.roles,
        "effective": effective,
        # The one persona setting that reaches an agent, and only new sessions.
        "voice": choice.voice,
        "voice_roles": voiced,
    }


def set_voice(project: ProjectInfo, enabled: bool) -> PersonaReceipt:
    """Opt a project's NEW agent sessions in or out of the pack's speaking style.

    Panel narration stays display-only either way. Voice is the deliberate
    exception: when on, `asq launch` appends the selected pack's instruction to
    the agent's system prompt. Running sessions are untouched.
    """
    with _locked():
        settings, warning = _settings_for_write("voice")
        choice = settings.projects.setdefault(project.id, Selection())
        choice.voice = enabled
        _save_settings(settings)
    report = persona_status(project)
    state = "on" if enabled else "off"
    message = f"Persona voice {state} for new sessions in this project."
    if enabled and not report["voice_roles"]:
        message += " No selected pack has voice text yet, so nothing is appended."
    if warning:
        message += f" {warning}"
    return PersonaReceipt(action="voice", message=message, data=report)


def voice_instruction(project: ProjectInfo | None, role: str) -> str | None:
    """What `asq launch` appends to a new agent's system prompt, or None.

    None unless the project opted in AND the role's effective pack has voice
    text. The frame around the pack's words confines the effect to the wording
    of replies to the human: never code, records, evidence or commands.
    """
    if project is None:
        return None
    settings = _settings()
    if not settings.projects.get(project.id, Selection()).voice:
        return None
    reference = _effective(settings, project, role)
    if not reference:
        return None
    pack = load_pack(reference)
    text = voice_text(pack, role)
    if not text:
        return None
    return (
        f"AI Square persona voice ({pack.name}, {base_role(role)} role): {text} "
        "This shapes only the WORDING of your conversational replies to the human. "
        "It never changes code, file contents, commit messages, board notes, task "
        "text, evidence, commands or their output, and you never mention it there. "
        "Facts, failures and results stay exact and unsoftened."
    )


def select(
    action: str,
    project: ProjectInfo | None,
    *,
    reference: str | None = None,
    role: str | None = None,
    reset_roles: bool = False,
) -> PersonaReceipt:
    if action not in {"use", "off", "reset"}:
        raise ValueError("selection action must be use, off or reset")
    if action == "use" and not reference:
        raise ValueError("persona use needs a pack reference")
    if reset_roles and (role or project is None):
        raise ValueError("--reset-roles needs a project and cannot be combined with --role")
    if role:
        role = base_role(validate_identifier(role))
    if project is None and role:
        raise ValueError("global role overrides are not supported; choose a project")
    with _locked():
        # Resolve under the lock: a concurrent `remove` cannot slip between the
        # existence check and the saved reference.
        pack_ref = load_pack(reference).reference if reference else None
        settings, warning = _settings_for_write(action)
        if project is None:
            settings.global_default = pack_ref if action == "use" else None
        else:
            choice = settings.projects.setdefault(project.id, Selection())
            if action == "use":
                if role:
                    assert pack_ref is not None
                    choice.roles[role] = pack_ref
                else:
                    choice.default = pack_ref
                    choice.enabled = True
            elif action == "off":
                if role:
                    choice.roles[role] = "off"
                else:
                    choice.enabled = False
            elif action == "reset":
                if role:
                    choice.roles.pop(role, None)
                elif not reset_roles:
                    settings.projects.pop(project.id, None)
            if reset_roles:
                choice.roles.clear()
        _save_settings(settings)
    report = persona_status(project)
    inactive = project is not None and report["project_off"] is True and action == "use"
    message = (
        "Choice saved but inactive: this project is Off."
        if inactive
        else f"Persona {action} saved."
    )
    if report["role_overrides"]:
        message += f" Role overrides retained: {report['role_overrides']}."
    if warning:
        message += f" {warning}"
    return PersonaReceipt(action=action, message=message, data=report)


def remove_pack(reference: str) -> PersonaReceipt:
    """Remove one version (``ID@version``) or, for a bare ``ID``, every installed version."""
    parts = reference.split("@")
    validate_identifier(parts[0])
    if len(parts) > 2:
        raise ValueError("use an ID or ID@version")
    if any(p.id == parts[0] for p in _bundled()):
        raise ValueError("bundled packs cannot be removed; use persona off instead")
    affected: list[str] = []
    removed: list[str] = []
    with _locked():
        targets = [
            p
            for p, bundled in _all_packs()
            if not bundled and p.id == parts[0] and (len(parts) == 1 or p.version == parts[1])
        ]
        if not targets:
            raise ValueError(f"persona {reference!r} is missing or damaged")
        references = {p.reference for p in targets}
        settings = _settings()
        if settings.global_default in references:
            settings.global_default = None
            affected.append("global")
        for project_id, selection in settings.projects.items():
            if selection.default in references:
                selection.default = "off"
                affected.append(project_id)
            for role, selected in selection.roles.items():
                if selected in references:
                    selection.roles[role] = "off"
                    affected.append(f"{project_id}:{role}")
        # First resolve selections to plain output; then remove pack data.
        _save_settings(settings)
        for pack in targets:
            target = _safe(_root() / pack.id / pack.version / "pack.json")
            target.unlink()
            _safe(target.with_name("provenance.json")).unlink(missing_ok=True)
            removed.append(pack.reference)
            with suppress(OSError):
                target.parent.rmdir()
        with suppress(OSError):
            _safe(_root() / parts[0]).rmdir()
    return PersonaReceipt(
        action="remove",
        message=f"Removed {', '.join(removed)}; affected choices are Off.",
        data={"affected": affected, "removed": removed},
    )


def export_pack(reference: str, output: Path) -> PersonaReceipt:
    content = pack_bytes(load_pack(reference))
    destination = output.expanduser()
    if destination.is_symlink():
        raise ValueError("export destination must not be a symlink")
    # Exclusive create prevents overwriting user files; exports contain only pack data.
    with destination.open("xb") as stream:
        stream.write(content)
    return PersonaReceipt(action="export", message=f"Exported to {destination}.")


def author_draft(name: str, description: str, starter: str = "studio") -> PersonaPack:
    validate_identifier(name)
    clean_text(description)
    data = load_pack(starter).model_dump()
    data.update(
        id=name,
        name=name.replace("-", " ").title(),
        version="1.0.0",
        description=description,
        author="Local user",
        license="All rights reserved",
        # The description IS the speaking style; the starter's phrases stay for
        # the panel until the author edits them.
        voice={"default": description},
    )
    return PersonaPack.model_validate(data)


def edit_draft(reference: str) -> PersonaPack:
    pack = load_pack(reference)
    versions = [p.version for p, _ in _all_packs() if p.id == pack.id]
    major, minor, patch = (int(n) for n in pack.version.split("."))
    while f"{major}.{minor}.{patch}" in versions:
        patch += 1
    data = pack.model_dump()
    data["version"] = f"{major}.{minor}.{patch}"
    return PersonaPack.model_validate(data)


def save_draft(pack: PersonaPack | str) -> PersonaReceipt:
    try:
        parsed = (
            parse_pack(pack.encode()) if isinstance(pack, str) else parse_pack(pack_bytes(pack))
        )
    except ValueError as exc:
        raise _pack_error(exc, "the edited draft") from None
    return install_pack(parsed, source="local-editor")


def event_role(event: TeamEvent, project: ProjectInfo | None) -> str:
    if event.session_id and project is not None:
        from aisquare.core.store import store_session

        try:
            with store_session() as store:
                session = store.get_session(event.session_id)
            if session and session.project_id == project.id:
                return session.role
        except (OSError, sqlite3.Error):
            pass
    return "team"


def render_caption(event: TeamEvent, project: ProjectInfo | None, role: str | None = None) -> str:
    """Display only; a missing/damaged optional pack always falls back to plain output."""
    try:
        if project is not None and event.project_id != project.id:
            return ""
        actual_role = role or event_role(event, project)
        reference = _effective(_settings(), project, actual_role)
        if not reference:
            return ""
        pack = load_pack(reference)
        line = caption(
            pack,
            role=actual_role,
            kind=event.kind,
            event_id=event.id,
            task_id=event.task_id,
            session_id=event.session_id,
        )
        # The role is agent-controlled board data: one line, no controls.
        return f"Role narration · {pack.name} · {single_line(actual_role)}: {line}" if line else ""
    except (ValueError, OSError):
        return ""


def render_event(event: TeamEvent, project: ProjectInfo | None, role: str | None = None) -> str:
    narration = render_caption(event, project, role)
    original = plain_text(
        f"Original record · {event.kind} · task={event.task_id or '-'} · "
        f"session={event.session_id or '-'} · event={event.id}\n{event.text}"
    )
    return f"{narration}\n{original}" if narration else original


class PersonaUsageError(ValueError):
    """The command line itself is wrong: exit 2, like any other usage error."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise PersonaUsageError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="persona", add_help=False, exit_on_error=False)
    parser.add_argument(
        "action",
        nargs="?",
        default="picker",
        choices=[
            "picker",
            "list",
            "status",
            "preview",
            "use",
            "off",
            "reset",
            "add",
            "edit",
            "export",
            "remove",
            "voice",
        ],
    )
    parser.add_argument("value", nargs="?")
    parser.add_argument("--role")
    parser.add_argument("--project")
    parser.add_argument("--global", dest="global_scope", action="store_true")
    parser.add_argument("--reset-roles", action="store_true")
    parser.add_argument("--url")
    parser.add_argument("--name")
    parser.add_argument("--text")
    parser.add_argument("--text-file")
    parser.add_argument("--output")
    return parser


def _input_path(value: str, project: ProjectInfo | None) -> Path:
    path = Path(value).expanduser()
    return project.root / path if project is not None and not path.is_absolute() else path


def run_persona_command(text: str, project: ProjectInfo | None = None) -> PersonaReceipt:
    """Parse shell-style words, never execute a shell or send text to an agent."""
    if len(text) > 8192:
        raise PersonaUsageError("persona command is too long")
    words = shlex.split(text)
    if words and words[0] in {"/persona", "persona"}:
        words = words[1:]
    try:
        args = _parser().parse_args(words)
    except argparse.ArgumentError as exc:
        raise ValueError(str(exc)) from exc
    if args.global_scope and args.project:
        raise PersonaUsageError("--global and --project cannot be combined")
    action = args.action
    allowed: dict[str, set[str]] = {
        "picker": {"project", "global_scope"},
        "list": set(),
        "status": {"project", "global_scope"},
        "preview": {"value", "role"},
        "use": {"value", "role", "project", "global_scope", "reset_roles"},
        "off": {"role", "project", "global_scope"},
        "reset": {"role", "project", "global_scope", "reset_roles"},
        "add": {"value", "url", "name", "text", "text_file"},
        "edit": {"value"},
        "export": {"value", "output"},
        "remove": {"value"},
        "voice": {"value", "project"},
    }
    for key, value in vars(args).items():
        if key != "action" and value not in (None, False) and key not in allowed[action]:
            raise PersonaUsageError(f"{key.replace('_', '-')} is not valid for persona {action}")
    if action in {"preview", "use", "edit", "export", "remove"} and not args.value:
        raise PersonaUsageError(f"persona {action} needs a pack ID or ID@version")
    if action == "voice":
        if args.value not in {"on", "off"}:
            raise PersonaUsageError("persona voice needs on or off")
        return set_voice(resolve_project(project, args.project), args.value == "on")
    if action == "list":
        return PersonaReceipt(
            action=action,
            message="Available persona packs.",
            data={"packs": [p.model_dump() for p in list_packs()]},
        )
    if action in {"picker", "status", "use", "off", "reset"}:
        target = None if args.global_scope else resolve_project(project, args.project)
        if action in {"picker", "status"}:
            return PersonaReceipt(
                action=action,
                message="Personality affects display only.",
                data=persona_status(target),
            )
        return select(
            action, target, reference=args.value, role=args.role, reset_roles=args.reset_roles
        )
    if action == "preview":
        pack = load_pack(args.value)
        role = args.role or "coder"
        samples = {
            kind: caption(pack, role=role, kind=kind, event_id=f"preview-{kind}", task_id="T42")
            for kind in ("task_claimed", "task_review", "task_blocked", "attention", "task_done")
        }
        return PersonaReceipt(
            action=action,
            message="Caption preview; official facts remain separate.",
            data={
                "pack": pack.reference,
                "role": role,
                "samples": samples,
                "original_failure": "T42 · blocked · phone check failed",
            },
        )
    if action == "remove":
        return remove_pack(args.value)
    if action == "export":
        if not args.output:
            raise PersonaUsageError("persona export needs --output FILE")
        return export_pack(args.value, _input_path(args.output, project))
    if action == "edit":
        return PersonaReceipt(
            action="edit",
            message="Edit wording locally; save creates a new version.",
            editor=edit_draft(args.value),
        )
    if action == "add":
        sources = [args.value, args.url, args.text, args.text_file]
        if sum(source is not None for source in sources) != 1:
            raise PersonaUsageError("add needs exactly one JSON file, --url, --text or --text-file")
        if args.name and (args.value or args.url):
            raise PersonaUsageError(
                "--name applies to text authoring; JSON files contain their own identity"
            )
        if args.value:
            return import_pack(_input_path(args.value, project))
        if args.url:
            return download_pack(args.url)
        if not args.name:
            raise ValueError("text authoring needs --name ID")
        description = args.text
        if args.text_file:
            path = _input_path(args.text_file, project)
            if path.is_symlink() or not path.is_file():
                raise ValueError("description must be a regular text file")
            with path.open("rb") as stream:
                raw = stream.read(8001)
            if len(raw) > 8000:
                raise ValueError("description file is too large")
            description = raw.decode("utf-8").strip()
        return PersonaReceipt(
            action="author",
            message=(
                "Starter wording from Studio is unchanged. "
                "Edit the message patterns to create your voice. "
                "Your description is saved as metadata; "
                "no AI generation or worker instruction is added."
            ),
            editor=author_draft(args.name, description),
        )
    raise ValueError(f"unsupported persona action: {action}")
