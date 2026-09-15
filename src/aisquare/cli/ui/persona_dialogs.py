"""The persona dialogs the Personas tab opens — Import, Confirm draft, New, Edit, Export, Remove.

docs/plans/spawn-personas.md §4.3 to §4.5. Each is a ``ModalScreen`` over ONE
``services.personas`` call — the seam ``aisquare persona`` uses — so no rule is
re-implemented here: the service refuses, and the refusal lands in the dialog's
status line with its reason (a ``Text``, never markup: a reason names a path).
Every call is looked up on the module at call time, so a test replaces it with a
recorder. The rules checked live in a form (the skill-name rule, "does this
SKILL.md parse") are ``core.personas``' own, called, not copied.

Import is the one that can take minutes. ``import_source`` runs in a THREAD
worker and reaches back through the two callbacks the CLI passes too:
``progress`` lines land in the status line through ``call_from_thread``, and
``confirm`` opens :class:`ConfirmDraftScreen` from the worker with
``app.call_from_thread(app.push_screen_wait, …)`` and returns its answer — the
CLI's ``y/N`` (verified on Textual 8.2.8 before this was written). Cancelling a
running import cancels the worker: a later ``confirm`` answers ``False`` and its
progress is dropped; the service's own timeout bounds the process (§9).

The Import dialog's engine defaults are the plan's documented ones (§3.9) until
``[persona.import]`` exists (P5): an untouched engine is sent as ``None`` and an
empty model as ``None``, so ``import_source`` — which owns the ladder — decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import (
    Button,
    Input,
    Label,
    RadioButton,
    RadioSet,
    Select,
    Static,
    Switch,
    TextArea,
)
from textual.worker import Worker, WorkerState, get_current_worker

from aisquare.core import personas as core
from aisquare.core.agents import _claude_home
from aisquare.core.personas import Layer, PersonaError
from aisquare.services import personas as personas_service

IMPORT_ENGINES: tuple[str, ...] = ("auto", "manager", "api")
DEFAULT_ENGINE = "auto"
"""``[persona.import].engine``'s documented default (plan §3.9); the section itself is P5's."""
DEFAULT_API_MODEL = "claude-opus-5"
"""``[persona.import].api_model``'s documented default — the Model field's placeholder."""

IMPORT_WORKER = "persona-import"
SKILLS_WORKER = "persona-importable-skills"

PARSE_DEBOUNCE = 0.25
"""Seconds of quiet typing before the editor re-parses the SKILL.md."""

NAME_RULE = (
    "a persona name is 1 to 64 characters of lowercase letters, digits and single "
    "hyphens — and never 'synced'"
)

_DIALOG_CSS = """
{name} {{ align: center middle; }}
{name} > Vertical {{ width: 96; max-width: 96%; height: auto; max-height: 94%;
                     border: heavy $accent; background: $surface; padding: 0 1; }}
{name} .dialog-header {{ height: auto; padding: 1 0; }}
{name} .dialog-fields {{ height: auto; max-height: 30; }}
{name} .dialog-row {{ height: auto; }}
{name} .dialog-row > Label {{ width: 22; padding-top: 1; }}
{name} .dialog-row > Input {{ width: 1fr; }}
{name} .dialog-row > Select {{ width: 1fr; }}
{name} .dialog-note {{ height: auto; padding-left: 22; color: $text-muted; }}
{name} .dialog-status {{ height: auto; }}
{name} .dialog-buttons {{ height: auto; align-horizontal: right; padding-bottom: 1; }}
{name} .dialog-buttons Button {{ margin-left: 2; }}
"""


def dialog_css(name: str, extra: str = "") -> str:
    return _DIALOG_CSS.format(name=name) + extra


def name_problem(name: str) -> str | None:
    """The skill-name rule (§3.5), from ``core.personas``' own constants."""
    if (
        len(name) > core.SKILL_NAME_MAX
        or core.SKILL_NAME.fullmatch(name) is None
        or name in core.RESERVED_NAMES
    ):
        return NAME_RULE
    return None


def layer_radios(root: Path | None, prefix: str) -> RadioSet:
    """User (default) or project — the project layer only inside a git repository."""
    return RadioSet(
        RadioButton("user", value=True, id=f"{prefix}-user"),
        RadioButton(
            "project" if root is not None else "project — needs a git repository",
            id=f"{prefix}-project",
            disabled=root is None,
        ),
        id=f"{prefix}-layer",
    )


def chosen_layer(radios: RadioSet) -> Layer:
    pressed = radios.pressed_button
    return "project" if pressed is not None and (pressed.id or "").endswith("-project") else "user"


def failure(exc: Exception) -> str:
    """What a refused write says: a persona rule as written, anything else with its class.

    A ``PersonaError`` is already a sentence. An ``OSError`` from the filesystem (an
    unwritable directory, a ``PermissionError`` from ``rmtree``) is not, and without
    its class it reads as a bare path.
    """
    return str(exc) if isinstance(exc, PersonaError) else f"{type(exc).__name__}: {exc}"


class _Dialog(ModalScreen[Any]):
    """What every dialog here shares: ``Esc`` answers the cancel value, and a Text-only note."""

    BINDINGS: ClassVar = [Binding("escape", "cancel", "cancel")]
    CANCEL: ClassVar[Any] = None

    def action_cancel(self) -> None:
        self.dismiss(self.CANCEL)

    def note(self, selector: str, text: str | None, *, style: str = "") -> None:
        widget = self.query_one(selector, Static)
        widget.update(Text(text, style=style) if text else "")
        widget.display = bool(text)


# --- Import -------------------------------------------------------------------------------


class ImportPersonaScreen(_Dialog):
    """``persona import`` as a form; dismisses with the :class:`ImportResult`, or ``None``."""

    DEFAULT_CSS = dialog_css("ImportPersonaScreen")

    def __init__(self, root: Path | None) -> None:
        super().__init__()
        self.root = root
        self._worker: Worker[Any] | None = None
        self._importing = False
        self._progress_lines: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(Text("Import a persona", style="bold"), classes="dialog-header")
            with VerticalScroll(classes="dialog-fields"):
                with Horizontal(classes="dialog-row"):
                    yield Label("Source")
                    yield Input(
                        placeholder="a skill directory, a SKILL.md or .md file, or a skill name",
                        id="import-source",
                    )
                with Horizontal(classes="dialog-row"):
                    yield Label("Browse skills")
                    yield Select[str](
                        [], prompt="Claude Code skills — reading…", id="import-browse"
                    )
                with Horizontal(classes="dialog-row"):
                    yield Label("Layer")
                    yield layer_radios(self.root, "import")
                with Horizontal(classes="dialog-row"):
                    yield Label("Name")
                    yield Input(placeholder="(from the source)", id="import-name")
                yield Static(id="import-name-rule", classes="dialog-note")
                with Horizontal(classes="dialog-row"):
                    yield Label("Use the LLM")
                    yield Switch(True, id="import-llm")
                    yield Static(
                        " when the source is not a recognised skill", classes="dialog-hint"
                    )
                with Horizontal(classes="dialog-row"):
                    yield Label("Condense")
                    yield Switch(False, id="import-condense")
                with Horizontal(classes="dialog-row"):
                    yield Label("Engine")
                    yield Select(
                        [(engine, engine) for engine in IMPORT_ENGINES],
                        value=DEFAULT_ENGINE,
                        allow_blank=False,
                        id="import-engine",
                    )
                with Horizontal(classes="dialog-row"):
                    yield Label("Model (api)")
                    yield Input(placeholder=DEFAULT_API_MODEL, id="import-model", disabled=True)
                with Horizontal(classes="dialog-row"):
                    yield Label("Force")
                    yield Switch(False, id="import-force")
            yield Static(id="import-status", classes="dialog-status")
            yield Static(id="import-draft", classes="dialog-status")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Import", id="import-submit", variant="primary")
                yield Button("Cancel", id="import-cancel")

    def on_mount(self) -> None:
        self.note("#import-name-rule", None)
        self.note("#import-status", None)
        self.note("#import-draft", None)
        self._validate()
        root = self.root
        self.run_worker(
            lambda: personas_service.importable_skills(root),
            name=SKILLS_WORKER,
            group=SKILLS_WORKER,
            thread=True,
            exit_on_error=False,
        )
        self.query_one("#import-source", Input).focus()

    def _validate(self) -> bool:
        name = self.query_one("#import-name", Input).value
        problem = name_problem(name) if name else None
        self.note("#import-name-rule", problem, style="red")
        source = self.query_one("#import-source", Input).value.strip()
        ok = problem is None and bool(source)
        self.query_one("#import-submit", Button).disabled = self._importing or not ok
        return ok

    @on(Input.Changed, "#import-source")
    @on(Input.Changed, "#import-name")
    def _field_changed(self) -> None:
        self._validate()

    @on(Select.Changed, "#import-browse")
    def _browsed(self, event: Select.Changed) -> None:
        if isinstance(event.value, str):
            self.query_one("#import-source", Input).value = event.value

    @on(Select.Changed, "#import-engine")
    def _engine_changed(self, event: Select.Changed) -> None:
        self.query_one("#import-model", Input).disabled = event.value != "api"

    def import_kwargs(self) -> dict[str, Any]:
        """Exactly what the form sends to ``import_source`` (the callbacks aside)."""
        engine = self.query_one("#import-engine", Select).value
        model = self.query_one("#import-model", Input)
        name = self.query_one("#import-name", Input).value
        return {
            "source": self.query_one("#import-source", Input).value.strip(),
            "layer": chosen_layer(self.query_one("#import-layer", RadioSet)),
            "root": self.root,
            "name": name or None,
            "force": self.query_one("#import-force", Switch).value,
            "llm": "auto" if self.query_one("#import-llm", Switch).value else "never",
            "condense": self.query_one("#import-condense", Switch).value,
            "engine": None if engine == DEFAULT_ENGINE else engine,
            "model": (model.value.strip() or None) if engine == "api" else None,
        }

    @on(Button.Pressed, "#import-submit")
    def _submit(self) -> None:
        if self._importing or not self._validate():
            return
        kwargs = self.import_kwargs()
        self._progress_lines = []
        self.note("#import-draft", None)
        self._set_importing(True)
        self._worker = self.run_worker(
            lambda: personas_service.import_source(
                **kwargs, confirm=self._confirm, progress=self._progress
            ),
            name=IMPORT_WORKER,
            group=IMPORT_WORKER,
            thread=True,
            exit_on_error=False,  # a refusal is an answer to show, not a crash
        )

    def _set_importing(self, importing: bool) -> None:
        self._importing = importing
        if importing:
            self.note("#import-status", "importing …", style="dim")
        self._validate()

    # Both callbacks run on the WORKER's thread.

    def _progress(self, text: str) -> None:
        if get_current_worker().is_cancelled:
            return
        self.app.call_from_thread(self._show_progress, text)

    def _show_progress(self, text: str) -> None:
        self._progress_lines.append(text)
        self.note("#import-status", "\n".join(self._progress_lines), style="dim")

    def _confirm(self, view: personas_service.PersonaDraftView) -> bool:
        if get_current_worker().is_cancelled:
            return False
        answer = bool(
            self.app.call_from_thread(self.app.push_screen_wait, ConfirmDraftScreen(view))
        )
        if not answer and view.draft_path is not None:
            self.app.call_from_thread(
                self.note, "#import-draft", f"discarded — the draft stays at {view.draft_path}"
            )
        return answer

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        worker = event.worker
        if worker.name == SKILLS_WORKER:
            self._skills_read(worker, event.state)
            return
        if worker.name != IMPORT_WORKER or worker is not self._worker:
            return
        if event.state is WorkerState.SUCCESS:
            if isinstance(worker.result, personas_service.ImportResult):
                self.dismiss(worker.result)
                return
            self._refused(f"the import answered without a result ({type(worker.result).__name__})")
        elif event.state is WorkerState.ERROR:
            error = worker.error
            if isinstance(error, PersonaError):
                self._refused(str(error))
            else:
                self._refused(f"{type(error).__name__}: {error}")

    def _refused(self, reason: str) -> None:
        self._set_importing(False)
        self.note("#import-status", reason, style="bold red")

    def _skills_read(self, worker: Worker[Any], state: WorkerState) -> None:
        select = self.query_one("#import-browse", Select)
        if state is WorkerState.SUCCESS and isinstance(worker.result, list):
            options: list[tuple[Text, str]] = []
            for ref in worker.result:
                if not isinstance(ref, personas_service.SkillRef):
                    continue
                what = ref.description if ref.recognised else f"✗ {ref.reason}"
                imported = " · imported" if ref.imported else ""
                # Text, not str: Select parses a str label as markup, so a description
                # holding "[/]" raised MarkupError and one holding "[docs]" lost it.
                label = Text(f"{ref.name} · {ref.scope} · {what}{imported}")
                options.append((label, str(ref.path)))
            select.set_options(options)
            select.prompt = "Claude Code skills" if options else "no Claude Code skills found"
        elif state is WorkerState.ERROR:
            select.prompt = f"skills unavailable — {type(worker.error).__name__}"

    def action_cancel(self) -> None:
        if self._worker is not None and self._importing:
            self._worker.cancel()  # its confirm answers False from here on
        self.dismiss(None)

    @on(Button.Pressed, "#import-cancel")
    def _cancel(self) -> None:
        self.action_cancel()


class ConfirmDraftScreen(_Dialog):
    """An LLM draft, shown before anything is saved; dismisses with Save (True) or Discard."""

    DEFAULT_CSS = dialog_css(
        "ConfirmDraftScreen",
        "ConfirmDraftScreen #draft-body { height: 14; border: round $panel; padding: 0 1; }",
    )
    CANCEL: ClassVar[Any] = False

    def __init__(self, view: personas_service.PersonaDraftView) -> None:
        super().__init__()
        self.view = view

    def _warnings(self) -> Text:
        view = self.view
        try:
            persona = core.parse_skill(
                view.skill_md,
                name=view.name,
                path=view.draft_path or Path(view.name),
                layer="user",
            )
        except PersonaError as exc:
            return Text(f"✗ {exc.rule}", style="red")
        return Text("\n".join(f"⚠ {w}" for w in core.warnings(persona)), style="yellow")

    def compose(self) -> ComposeResult:
        view = self.view
        try:
            frontmatter, _ = core.split_frontmatter(view.skill_md)
        except PersonaError:
            # Display only: a draft whose frontmatter will not split is shown whole, and
            # _warnings() states the parse error in this same modal, so nothing is hidden.
            frontmatter = view.skill_md
        with Vertical():
            header = Text()
            header.append(f"Save the persona '{view.name}'?\n", style="bold")
            header.append(
                f"{view.engine} · {view.model or 'default model'} · {len(view.body):,} characters",
                style="cyan",
            )
            yield Static(header, classes="dialog-header", id="draft-header")
            yield Static(
                Text(f"---\n{frontmatter.rstrip()}\n---", style="dim"), id="draft-frontmatter"
            )
            with VerticalScroll(id="draft-body"):
                yield Static(Text(view.body), id="draft-body-text")
            yield Static(
                Text("\n".join(view.notes) if view.notes else "(the engine noted nothing)"),
                id="draft-notes",
            )
            yield Static(self._warnings(), id="draft-warnings")
            yield Static(
                Text(f"draft kept at {view.draft_path}", style="dim") if view.draft_path else "",
                id="draft-path",
            )
            with Horizontal(classes="dialog-buttons"):
                yield Button("Save", id="draft-save", variant="primary")
                yield Button("Discard", id="draft-discard")

    @on(Button.Pressed, "#draft-save")
    def _save(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#draft-discard")
    def _discard(self) -> None:
        self.dismiss(False)


# --- New and Edit -------------------------------------------------------------------------


@dataclass(frozen=True)
class NewPersona:
    """A scaffold ``services.personas.new`` wrote, and the text the editor opens on."""

    name: str
    layer: Layer
    path: Path
    text: str


class NewPersonaScreen(_Dialog):
    """Name, layer, description → ``services.personas.new``; the editor opens next."""

    DEFAULT_CSS = dialog_css("NewPersonaScreen")

    def __init__(self, root: Path | None) -> None:
        super().__init__()
        self.root = root

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(Text("New persona", style="bold"), classes="dialog-header")
            with Horizontal(classes="dialog-row"):
                yield Label("Name")
                yield Input(placeholder="pair-programmer", id="new-name")
            yield Static(id="new-name-rule", classes="dialog-note")
            with Horizontal(classes="dialog-row"):
                yield Label("Layer")
                yield layer_radios(self.root, "new")
            with Horizontal(classes="dialog-row"):
                yield Label("Description")
                yield Input(placeholder="one line: how this persona works", id="new-description")
            yield Static(id="new-status", classes="dialog-status")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Create", id="new-create", variant="primary")
                yield Button("Cancel", id="new-cancel")

    def on_mount(self) -> None:
        self.note("#new-status", None)
        self._validate()
        self.query_one("#new-name", Input).focus()

    def _validate(self) -> bool:
        name = self.query_one("#new-name", Input).value
        problem = name_problem(name) if name else None
        self.note("#new-name-rule", problem, style="red")
        ok = bool(name) and problem is None
        self.query_one("#new-create", Button).disabled = not ok
        return ok

    @on(Input.Changed, "#new-name")
    def _name_changed(self) -> None:
        self._validate()

    @on(Button.Pressed, "#new-create")
    def _create(self) -> None:
        if not self._validate():
            return
        name = self.query_one("#new-name", Input).value
        layer = chosen_layer(self.query_one("#new-layer", RadioSet))
        description = self.query_one("#new-description", Input).value.strip()
        try:
            directory = personas_service.new(name, layer=layer, root=self.root)
            text = (directory / core.SKILL_FILE).read_text(encoding="utf-8")
            if description:
                # The description goes into the editor's text, not onto disk: Save
                # writes it through services.personas.save like any other edit.
                scaffold = core.parse_skill(text, name=name, path=directory, layer=layer)
                text = core.render(name, description, scaffold.body, metadata={})
        except (PersonaError, OSError) as exc:
            self.note("#new-status", str(exc), style="bold red")
            return
        self.dismiss(NewPersona(name=name, layer=layer, path=directory, text=text))

    @on(Button.Pressed, "#new-cancel")
    def _cancel(self) -> None:
        self.action_cancel()


class EditPersonaScreen(_Dialog):
    """The whole SKILL.md in a TextArea; *Save* only while it parses, and only through the service.

    A bundled persona opens read-only: *Save as…* copies it into a layer the way
    ``persona export --to <layer>`` does, and the copy shadows the bundled one.
    Dismisses with ``True`` when something was written.
    """

    DEFAULT_CSS = dialog_css(
        "EditPersonaScreen",
        "EditPersonaScreen #edit-text { height: 20; }",
    )
    CANCEL: ClassVar[Any] = False

    def __init__(
        self,
        name: str,
        layer: Layer,
        directory: Path,
        root: Path | None,
        *,
        text: str | None = None,
    ) -> None:
        super().__init__()
        self.persona_name = name
        self.persona_layer: Layer = layer
        self.directory = directory
        self.root = root
        self.read_only = layer == "bundled"
        self._unreadable: str | None = None
        if text is None:
            try:
                text = (directory / core.SKILL_FILE).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                text, self._unreadable = "", f"{type(exc).__name__}: {exc}"
        self._initial = text
        self._timer: Timer | None = None

    def compose(self) -> ComposeResult:
        with Vertical():
            header = Text()
            header.append(f"{self.persona_name}", style="bold")
            header.append(
                f"  {self.persona_layer} · {self.directory / core.SKILL_FILE}", style="dim"
            )
            if self.read_only:
                header.append(f"\n{personas_bundled_hint()}", style="yellow")
            yield Static(header, classes="dialog-header")
            yield TextArea(self._initial, id="edit-text", read_only=self.read_only)
            yield Static(id="edit-status", classes="dialog-status")
            if self.read_only:
                with Horizontal(classes="dialog-row"):
                    yield Label("Save as… into")
                    yield layer_radios(self.root, "save-as")
                    yield Label("force", classes="dialog-hint")
                    yield Switch(False, id="save-as-force")
            with Horizontal(classes="dialog-buttons"):
                if self.read_only:
                    yield Button("Save as…", id="edit-save-as", variant="primary")
                else:
                    yield Button("Save", id="edit-save", variant="primary", disabled=True)
                yield Button("Cancel", id="edit-cancel")

    def on_mount(self) -> None:
        self.check()

    @on(TextArea.Changed, "#edit-text")
    def _changed(self) -> None:
        if self.read_only:
            return
        self.query_one("#edit-save", Button).disabled = True  # until the new text is checked
        if self._timer is not None:
            self._timer.stop()
        self._timer = self.set_timer(PARSE_DEBOUNCE, self.check)

    def check(self) -> bool:
        """Parse the text as the service will; show the first error, or the warnings."""
        if self._unreadable is not None:
            self.note(
                "#edit-status", f"✗ cannot read the SKILL.md — {self._unreadable}", style="red"
            )
            return False
        text = self.query_one("#edit-text", TextArea).text
        try:
            persona = core.parse_skill(
                text, name=self.persona_name, path=self.directory, layer=self.persona_layer
            )
        except PersonaError as exc:
            at = "" if exc.line is None else f"line {exc.line}: "
            self.note("#edit-status", f"✗ {at}{exc.rule}", style="red")
            ok = False
        else:
            warnings = core.warnings(persona)
            lines = ["✓ parses", *(f"⚠ {warning}" for warning in warnings)]
            self.note("#edit-status", "\n".join(lines), style="yellow" if warnings else "green")
            ok = True
        if not self.read_only:
            self.query_one("#edit-save", Button).disabled = not ok
        return ok

    @on(Button.Pressed, "#edit-save")
    def _save(self) -> None:
        if not self.check():
            return
        text = self.query_one("#edit-text", TextArea).text
        try:
            personas_service.save(self.persona_name, text, root=self.root, layer=self.persona_layer)
        except (PersonaError, OSError) as exc:
            self.note("#edit-status", failure(exc), style="bold red")
            return
        self.dismiss(True)

    @on(Button.Pressed, "#edit-save-as")
    def _save_as(self) -> None:
        layer = chosen_layer(self.query_one("#save-as-layer", RadioSet))
        try:
            base = dict(core.layer_dirs(self.root))[layer]
            personas_service.export(
                self.persona_name,
                root=self.root,
                to=base,
                skill=None,
                force=self.query_one("#save-as-force", Switch).value,
            )
        except (PersonaError, KeyError, OSError) as exc:
            self.note("#edit-status", failure(exc), style="bold red")
            return
        self.dismiss(True)

    @on(Button.Pressed, "#edit-cancel")
    def _cancel(self) -> None:
        self.action_cancel()


def personas_bundled_hint() -> str:
    return "bundled — read-only; Save as… copies it into a layer, where it shadows this one"


# --- Export and Remove ----------------------------------------------------------------------


@dataclass(frozen=True)
class ExportDone:
    name: str
    path: Path
    skill: Literal["user", "project"] | None
    """Set for the two Claude Code skill targets — after which it is ``/name`` there."""


class ExportPersonaScreen(_Dialog):
    """``persona export`` to Claude's skills, the project's skills, or a directory."""

    DEFAULT_CSS = dialog_css("ExportPersonaScreen")

    def __init__(self, name: str, root: Path | None) -> None:
        super().__init__()
        self.persona_name = name
        self.root = root

    def compose(self) -> ComposeResult:
        name = self.persona_name
        personal = _claude_home() / "skills" / name
        project = (
            f"{self.root / '.claude' / 'skills' / name}" if self.root else "needs a git repository"
        )
        with Vertical():
            yield Static(Text(f"Export {name}", style="bold"), classes="dialog-header")
            yield RadioSet(
                # Text, not str: a path is data, and a str label is parsed as markup.
                RadioButton(
                    Text(f"Claude personal skills — {personal}"),
                    value=True,
                    id="export-personal",
                ),
                RadioButton(
                    Text(f"Project skills — {project}"),
                    id="export-project",
                    disabled=self.root is None,
                ),
                RadioButton("Directory…", id="export-directory"),
                id="export-destination",
            )
            with Horizontal(classes="dialog-row"):
                yield Label("Directory")
                yield Input(placeholder="./persona-copies", id="export-dir", disabled=True)
            with Horizontal(classes="dialog-row"):
                yield Label("Force")
                yield Switch(False, id="export-force")
            yield Static(id="export-status", classes="dialog-status")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Export", id="export-submit", variant="primary")
                yield Button("Cancel", id="export-cancel")

    def on_mount(self) -> None:
        self.note("#export-status", None)
        self._validate()

    def _destination(self) -> str:
        pressed = self.query_one("#export-destination", RadioSet).pressed_button
        return (pressed.id or "export-personal") if pressed is not None else "export-personal"

    def _validate(self) -> bool:
        directory = self._destination() == "export-directory"
        field = self.query_one("#export-dir", Input)
        field.disabled = not directory
        ok = not directory or bool(field.value.strip())
        self.query_one("#export-submit", Button).disabled = not ok
        return ok

    @on(RadioSet.Changed, "#export-destination")
    @on(Input.Changed, "#export-dir")
    def _changed(self) -> None:
        self._validate()

    def export_kwargs(self) -> dict[str, Any]:
        destination = self._destination()
        skill: Literal["user", "project"] | None = None
        to: Path | None = None
        if destination == "export-personal":
            skill = "user"
        elif destination == "export-project":
            skill = "project"
        else:
            to = Path(self.query_one("#export-dir", Input).value.strip()).expanduser()
        return {
            "root": self.root,
            "to": to,
            "skill": skill,
            "force": self.query_one("#export-force", Switch).value,
        }

    @on(Button.Pressed, "#export-submit")
    def _export(self) -> None:
        if not self._validate():
            return
        kwargs = self.export_kwargs()
        try:
            written = personas_service.export(self.persona_name, **kwargs)
        except (PersonaError, OSError) as exc:
            self.note("#export-status", failure(exc), style="bold red")
            return
        self.dismiss(ExportDone(self.persona_name, Path(written), kwargs["skill"]))

    @on(Button.Pressed, "#export-cancel")
    def _cancel(self) -> None:
        self.action_cancel()


class ConfirmRemoveScreen(_Dialog):
    """One question, naming the directory that goes; dismisses ``True`` to remove."""

    DEFAULT_CSS = dialog_css("ConfirmRemoveScreen")
    CANCEL: ClassVar[Any] = False

    def __init__(self, name: str, layer: Layer, directory: Path) -> None:
        super().__init__()
        self.persona_name = name
        self.persona_layer: Layer = layer
        self.directory = directory

    def compose(self) -> ComposeResult:
        with Vertical():
            text = Text()
            text.append(
                f"Remove the {self.persona_layer} persona '{self.persona_name}'?\n", style="bold"
            )
            text.append(f"This deletes {self.directory} and everything in it.")
            yield Static(text, classes="dialog-header", id="remove-question")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Remove", id="remove-confirm", variant="error")
                yield Button("Cancel", id="remove-cancel")

    @on(Button.Pressed, "#remove-confirm")
    def _remove(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#remove-cancel")
    def _cancel(self) -> None:
        self.action_cancel()
