"""The Personas tab of the Project view — search, catalogue, preview, and every action.

docs/plans/spawn-personas.md §4.2 (this tab) and §4.3 to §4.5 (the dialogs it opens,
in ``cli/ui/persona_dialogs.py``). Personas live where the fleet they serve
lives: the tab's project supplies the project layer the way ``aisquare persona``
takes it — the git repository around the directory (``git_common_root``) — so a
project that is not a git repository shows the user and bundled layers only,
exactly what the CLI shows from there.

**The tab holds no state the disk does not.** Every action ends by re-reading
``core.personas.catalogue``, and the selection is kept by row key (``layer:name``),
falling back to the name, so an imported or copied persona is the row selected
next. Reads are ``core.personas`` (pure: directories in, models out); every write
goes through ``services.personas`` — the seam the CLI uses — looked up on the
module at call time, so a test replaces each with a recorder.

**Attach to existing / Attach to new** post :class:`AttachRequested`. The target
picker that answers it is P7; until then the tab answers its own message with a
toast and lets it bubble on, so P7 handles it above and deletes one handler here.

Bundled rows disable Edit and Remove ("bundled — export to a layer first"); the
row itself still opens — ``Enter`` on any row opens the SKILL.md, and a bundled
one opens read-only with *Save as…* (§4.4). Keys, live while focus is inside the
tab (the search box keeps its letters): ``a`` attach to existing · ``n`` attach to
new · ``e`` edit · ``x`` export · ``Del`` remove · ``v`` validate · ``i`` import.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Checkbox, DataTable, Input, Static

from aisquare.cli.ui import persona_dialogs as dialogs
from aisquare.core import personas as core
from aisquare.core.personas import Layer, Persona, PersonaError
from aisquare.core.workspace import git_common_root
from aisquare.models import ProjectInfo
from aisquare.services import personas as personas_service

AttachIntent = Literal["existing", "new"]

LAYERS: tuple[Layer, ...] = ("project", "user", "bundled")

ATTACH_PENDING = "target picker arrives with P7"
"""What an Attach button says until the target picker (P7) answers :class:`AttachRequested`."""

BUNDLED_REASON = "bundled — export to a layer first"


class AttachRequested(Message):
    """Attach ``persona`` to a running agent (``existing``) or to a new spawn (``new``)."""

    def __init__(self, persona: str, intent: AttachIntent) -> None:
        self.persona = persona
        self.intent = intent
        super().__init__()


@dataclass(frozen=True)
class Entry:
    """One row: a loadable persona, or a directory in a layer that did not load."""

    name: str
    layer: Layer
    path: Path
    persona: Persona | None = None
    reason: str | None = None
    """Why the directory did not load; ``None`` for a persona."""
    shadows: tuple[Layer, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.layer}:{self.name}"


def read_entries(root: Path | None) -> list[Entry]:
    """The catalogue as rows: every winner with what it shadows, then every broken directory."""
    personas, invalid = core.catalogue(root)
    rows = [
        Entry(
            name=persona.name,
            layer=persona.layer,
            path=persona.path,
            persona=persona,
            shadows=tuple(personas_service.shadows(persona, root)),
        )
        for persona in personas
    ]
    bases = core.layer_dirs(root)
    for path, reason in invalid:
        layer = next((layer for layer, base in bases if path.parent == base), None)
        if layer is not None:
            rows.append(Entry(name=path.name, layer=layer, path=path, reason=reason))
    return sorted(rows, key=lambda row: (row.name, LAYERS.index(row.layer)))


def matches(entry: Entry, query: str, layers: set[Layer]) -> bool:
    """The search: the layer chips, then a substring of the name, description or a tag."""
    if entry.layer not in layers:
        return False
    needle = query.strip().lower()
    if not needle:
        return True
    haystack = [entry.name]
    if entry.persona is not None:
        haystack += [entry.persona.description, *entry.persona.tags]
    return any(needle in item.lower() for item in haystack)


def marks(entry: Entry) -> Text:
    if entry.persona is None:
        return Text("✗ invalid", style="red")
    if entry.shadows:
        return Text(f"⇧ shadows {', '.join(entry.shadows)}", style="cyan")
    return Text("")


def details_text(entry: Entry) -> Text:
    """Everything under the preview: where it is, what travels with it, where it came from."""
    text = Text()
    text.append("path      ", style="dim")
    text.append(f"{entry.path}\n")
    text.append("layer     ", style="dim")
    text.append(entry.layer)
    if entry.shadows:
        text.append(f"  (shadows {', '.join(entry.shadows)})", style="cyan")
    text.append("\n")
    persona = entry.persona
    if persona is None:
        return text
    text.append("files     ", style="dim")
    text.append(", ".join(persona.files) if persona.files else "(SKILL.md only)")
    text.append("\n")
    if persona.roles:
        text.append("roles     ", style="dim")
        text.append(", ".join(persona.roles) + "\n")
    provenance = persona.provenance
    text.append("source    ", style="dim")
    if provenance is None:
        text.append("(no .persona.json — bundled or written by hand)\n")
    else:
        model = f" · {provenance.model}" if provenance.model else ""
        condensed = " · condensed" if provenance.condensed else ""
        text.append(
            f"{provenance.source} · {provenance.engine}{model}{condensed} · "
            f"{provenance.imported_at:%Y-%m-%d %H:%M}\n"
        )
    for warning in core.warnings(persona):
        text.append(f"⚠ {warning}\n", style="yellow")
    return text


class PersonasTab(Vertical):
    """The project's personas: pick one, preview exactly what is injected, act on it."""

    DEFAULT_CSS = """
    PersonasTab { height: 1fr; padding: 0 1; }
    PersonasTab #personas-top { height: auto; }
    PersonasTab #persona-search { width: 1fr; }
    PersonasTab #personas-top Checkbox { width: auto; }
    PersonasTab #personas-top Button { margin-left: 1; }
    PersonasTab #personas-body { height: 1fr; }
    PersonasTab #persona-table { width: 3fr; height: 1fr; }
    PersonasTab #persona-preview { width: 2fr; height: 1fr; border-left: tall $panel;
                                   padding: 0 1; }
    PersonasTab #persona-details { margin-top: 1; height: auto; }
    PersonasTab #persona-actions { height: auto; }
    PersonasTab #persona-actions Button { margin-right: 1; }
    PersonasTab #persona-note { height: auto; color: $text-muted; }
    """
    BINDINGS: ClassVar = [
        Binding("a", "attach_existing", "attach to existing"),
        Binding("n", "attach_new", "attach to new"),
        Binding("e", "edit", "edit"),
        Binding("x", "export", "export"),
        Binding("delete", "remove", "remove"),
        Binding("v", "validate", "validate"),
        Binding("i", "import_persona", "import"),
    ]

    def __init__(self, project: ProjectInfo, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.project = project
        self.root: Path | None = git_common_root(project.root)
        """The project layer's root — ``None`` outside a git repository, as the CLI has it."""
        self.entries: list[Entry] = []
        self.selected_key: str | None = None
        self.unavailable: str | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="personas-top"):
            yield Input(placeholder="search name, description or tag", id="persona-search")
            for layer in LAYERS:
                yield Checkbox(layer, True, id=f"layer-{layer}")
            yield Button("+ Import…", id="persona-import")
            yield Button("+ New", id="persona-new")
        with Horizontal(id="personas-body"):
            yield DataTable(id="persona-table", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="persona-preview"):
                yield Static(id="persona-briefing")
                yield Static(id="persona-details")
        with Horizontal(id="persona-actions"):
            yield Button("Attach to existing", id="persona-attach-existing", variant="primary")
            yield Button("Attach to new", id="persona-attach-new", variant="primary")
            yield Button("Edit", id="persona-edit")
            yield Button("Export…", id="persona-export")
            yield Button("Remove", id="persona-remove", variant="error")
            yield Button("Validate", id="persona-validate")
        yield Static(id="persona-note")

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns("persona", "layer", "description", "roles", "marks")
        self.reload()

    # --- reading and painting ---------------------------------------------------------

    def reload(self, *, select: str | None = None) -> None:
        """Re-read the catalogue and repaint; ``select`` is a row key or a persona name."""
        try:
            self.entries = read_entries(self.root)
            self.unavailable = None
        except Exception as exc:  # a layer we cannot read costs the list, never the view
            self.entries = []
            self.unavailable = f"personas unavailable — {type(exc).__name__}: {exc}"
        if select is not None:
            self.selected_key = select
        self._paint()

    def visible_entries(self) -> list[Entry]:
        query = self.query_one("#persona-search", Input).value
        layers = {layer for layer in LAYERS if self.query_one(f"#layer-{layer}", Checkbox).value}
        return [entry for entry in self.entries if matches(entry, query, layers)]

    def _paint(self) -> None:
        table = self.query_one(DataTable)
        rows = self.visible_entries()
        table.clear()
        for entry in rows:
            description = (
                entry.persona.description if entry.persona is not None else entry.reason or ""
            )
            style = "" if entry.persona is not None else "dim"
            table.add_row(
                Text(entry.name, style=style),
                Text(entry.layer, style=style),
                Text(description, style=style),
                Text(", ".join(entry.persona.roles) if entry.persona else "", style=style),
                marks(entry),
                key=entry.key,
            )
        index = self._index_for(rows, self.selected_key)
        self.selected_key = rows[index].key if rows else None
        if rows:
            table.move_cursor(row=index)
        self._show_selected()

    @staticmethod
    def _index_for(rows: list[Entry], wanted: str | None) -> int:
        """The row a key names — else the first row whose name it is (a layer changed) — else 0."""
        if wanted is None:
            return 0
        for index, entry in enumerate(rows):
            if entry.key == wanted:
                return index
        name = wanted.split(":", 1)[-1]
        winners = [i for i, entry in enumerate(rows) if entry.name == name]
        valid = [i for i in winners if rows[i].persona is not None]
        return (valid or winners or [0])[0]

    def selected(self) -> Entry | None:
        return next((entry for entry in self.entries if entry.key == self.selected_key), None)

    @on(DataTable.RowHighlighted, "#persona-table")
    def _highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key.value is not None:
            self.selected_key = event.row_key.value
            self._show_selected()

    @on(Input.Changed, "#persona-search")
    @on(Checkbox.Changed)
    def _filter_changed(self) -> None:
        self._paint()

    def _show_selected(self) -> None:
        entry = self.selected()
        briefing = self.query_one("#persona-briefing", Static)
        details = self.query_one("#persona-details", Static)
        if entry is None:
            briefing.update(Text("no persona matches" if self.entries else "", style="dim"))
            details.update("")
        elif entry.persona is None:
            briefing.update(Text(f"✗ {entry.path}: {entry.reason}", style="red"))
            details.update(details_text(entry))
        else:
            # Byte-equal to what a session is briefed with (§4.2): the lines, joined.
            briefing.update(Text("\n".join(core.briefing(entry.persona))))
            details.update(details_text(entry))
        for action, button in (
            ("attach", "#persona-attach-existing"),
            ("attach", "#persona-attach-new"),
            ("edit", "#persona-edit"),
            ("export", "#persona-export"),
            ("remove", "#persona-remove"),
            ("validate", "#persona-validate"),
        ):
            self.query_one(button, Button).disabled = not self._allowed(action, entry)
        if self.unavailable:
            note: str | None = self.unavailable
        elif entry is not None and entry.layer == "bundled":
            note = BUNDLED_REASON
        elif entry is not None and entry.persona is None:
            note = "✗ invalid — Edit fixes it, Remove deletes it; it cannot be attached or exported"
        else:
            note = None
        self.query_one("#persona-note", Static).update(Text(note) if note else "")

    @staticmethod
    def _allowed(action: str, entry: Entry | None) -> bool:
        if entry is None:
            return False
        if action in ("attach", "export"):
            return entry.persona is not None
        if action in ("edit", "remove"):
            return entry.layer != "bundled"
        return True  # validate: a broken directory is exactly what it explains

    # --- attach (the picker's seam) ---------------------------------------------------

    def _request_attach(self, intent: AttachIntent) -> None:
        entry = self.selected()
        if entry is not None and self._allowed("attach", entry):
            self.post_message(AttachRequested(entry.name, intent))

    @on(Button.Pressed, "#persona-attach-existing")
    def action_attach_existing(self) -> None:
        self._request_attach("existing")

    @on(Button.Pressed, "#persona-attach-new")
    def action_attach_new(self) -> None:
        self._request_attach("new")

    def on_attach_requested(self, event: AttachRequested) -> None:
        # Not stopped: the picker (P7) answers above; until it exists, say so.
        self.notify(ATTACH_PENDING, timeout=4)

    # --- the dialogs ------------------------------------------------------------------

    @on(Button.Pressed, "#persona-import")
    def action_import_persona(self) -> None:
        self.app.push_screen(dialogs.ImportPersonaScreen(self.root), callback=self._imported)

    def _imported(self, result: personas_service.ImportResult | None) -> None:
        if result is None:
            self.reload()
            return
        persona = result.persona
        self.notify(f"✓ imported {persona.name} ({result.engine})", timeout=6, markup=False)
        for warning in result.warnings:
            self.notify(warning, severity="warning", timeout=8, markup=False)
        self.reload(select=f"{persona.layer}:{persona.name}")

    @on(Button.Pressed, "#persona-new")
    def _new(self) -> None:
        self.app.push_screen(dialogs.NewPersonaScreen(self.root), callback=self._created)

    def _created(self, created: dialogs.NewPersona | None) -> None:
        if created is None:
            return
        self.reload(select=f"{created.layer}:{created.name}")
        self.app.push_screen(
            dialogs.EditPersonaScreen(
                created.name, created.layer, created.path, self.root, text=created.text
            ),
            callback=lambda saved: self.reload(select=f"{created.layer}:{created.name}"),
        )

    @on(Button.Pressed, "#persona-edit")
    def action_edit(self) -> None:
        entry = self.selected()
        if entry is not None and self._allowed("edit", entry):
            self._open(entry)

    @on(DataTable.RowSelected, "#persona-table")
    def _row_selected(self) -> None:
        entry = self.selected()
        if entry is not None:
            self._open(entry)

    def _open(self, entry: Entry) -> None:
        """The SKILL.md in the editor — read-only with *Save as…* for a bundled persona."""
        self.app.push_screen(
            dialogs.EditPersonaScreen(entry.name, entry.layer, entry.path, self.root),
            callback=lambda saved: self.reload(select=entry.name if saved else entry.key),
        )

    @on(Button.Pressed, "#persona-export")
    def action_export(self) -> None:
        entry = self.selected()
        if entry is None or not self._allowed("export", entry):
            return
        self.app.push_screen(
            dialogs.ExportPersonaScreen(entry.name, self.root), callback=self._exported
        )

    def _exported(self, done: dialogs.ExportDone | None) -> None:
        if done is None:
            return
        where = f"✓ exported {done.name} to {done.path}"
        if done.skill is not None:
            where += f" — it is /{done.name} in Claude Code now"
        self.notify(where, timeout=8, markup=False)
        self.reload()

    @on(Button.Pressed, "#persona-remove")
    def action_remove(self) -> None:
        entry = self.selected()
        if entry is None or not self._allowed("remove", entry):
            return

        def confirmed(yes: bool | None) -> None:
            if not yes:
                return
            try:
                removed = personas_service.remove(entry.name, layer=entry.layer, root=self.root)
            except (PersonaError, OSError) as exc:
                self.notify(dialogs.failure(exc), severity="error", timeout=8, markup=False)
            else:
                self.notify(f"✓ removed {removed}", timeout=6, markup=False)
            self.reload()

        self.app.push_screen(
            dialogs.ConfirmRemoveScreen(entry.name, entry.layer, entry.path), callback=confirmed
        )

    @on(Button.Pressed, "#persona-validate")
    def action_validate(self) -> None:
        entry = self.selected()
        if entry is None:
            return
        try:
            persona, warnings = personas_service.validate(entry.path)
        except PersonaError as exc:
            self.notify(f"✗ {exc}", severity="error", timeout=10, markup=False)
        else:
            self.notify(f"✓ {persona.name} is a valid persona", timeout=5, markup=False)
            for warning in warnings:
                self.notify(warning, severity="warning", timeout=8, markup=False)
        self.reload()
