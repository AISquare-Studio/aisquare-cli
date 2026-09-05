"""The left pane: Fleet ▸ projects (alternating background) ▸ agents ▸ Doctor.

docs/plans/fleet-tui.md §4.1. One ``ProjectCard`` per registered project, each
with a disclosure, the basename, the codename as a dim badge, chips (agents
alive · 🔔 count) and — when two projects share a basename — the path as a dim
subtitle; under it one ``AgentRow`` per fleet agent (role icon, label, state
chip, exit status) and a spawn-agent row; a Doctor section at the bottom.

Two rules shape the code more than the layout does:

- **Rebuilds update in place.** The app re-reads the store every two seconds
  and calls ``show_projects`` with the whole frame. Cards and rows are keyed by
  project and agent id and mutated, not re-created, so the highlight, the
  keyboard cursor, a collapsed card and the scroll position survive every
  tick, and nothing flickers. Only a project or agent that appeared or vanished
  mounts or unmounts a widget.
- **Rows post messages; the app decides.** ``AddProject``, ``ProjectSelected``,
  ``AgentSelected``, ``SpawnAgent`` and ``DoctorSelected`` are the whole
  contract between this pane and the shell. Nothing here opens a view.

Every visible string is a ``rich.text.Text`` built with ``append`` — a project
called ``[archive]`` must reach the screen as ``[archive]`` (CONTRIBUTING: no
markup in data).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

from aisquare.models import FleetAgentStatus, ProjectInfo

ROLE_ICON: dict[str, str] = {
    "manager": "🧭",
    "planner": "🧭",
    "coder": "🔨",
    "tester": "🧪",
    "runner": "🧪",
    "reviewer": "👀",
    "validator": "🛡",
    "remote": "📡",
}
STATE_CHIP: dict[str, tuple[str, str]] = {
    "working": ("▶", "green"),
    "waiting": ("⏸", "yellow"),
    "attention": ("🔔", "bold red"),
    "exited": ("💤", "dim"),
    "lost": ("✗", "red"),
    "unknown": ("·", "dim"),
}
CUSTOM_ROLE_ICON = "🤖"
"""The icon for a role the table above does not know (a `team bind` role, say)."""

ALIVE_STATES: frozenset[str] = frozenset({"working", "waiting", "attention", "unknown"})
"""States that count toward the card's "agents alive" chip."""

DOCTOR_LINES = 3
"""How many ⚠/✗ lines the Doctor section shows under its counts (§4.1)."""


class AddProject(Message):
    """The + beside Fleet."""


class ProjectSelected(Message):
    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        super().__init__()


class AgentSelected(Message):
    def __init__(self, project_id: str, agent_id: str) -> None:
        self.project_id = project_id
        self.agent_id = agent_id
        super().__init__()


class SpawnAgent(Message):
    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        super().__init__()


class DoctorSelected(Message):
    def __init__(self, project_id: str | None) -> None:
        self.project_id = project_id
        super().__init__()


# --- pure helpers (unit-testable without a running app) -----------------------------


def project_name(project: ProjectInfo) -> str:
    """The display name: the root's basename, or the id when the basename is empty (§5.7)."""
    return project.root.name or project.id


def short_path(path: Path, home: Path | None = None) -> str:
    """``~/work/api`` for a path under the home directory; the path itself otherwise."""
    home = Path.home() if home is None else home
    try:
        return "~/" + path.relative_to(home).as_posix()
    except ValueError:
        return str(path)


def ordered_agents(statuses: Iterable[FleetAgentStatus]) -> list[FleetAgentStatus]:
    """Manager first, then by ``created_at`` (§4.1) — whatever order the service used."""
    return sorted(
        statuses, key=lambda s: (s.agent.role != "manager", s.agent.created_at, s.agent.label)
    )


def agent_row_text(status: FleetAgentStatus) -> Text:
    """``🧭 manager       ⏸`` — icon, label, state chip, exit status when exited."""
    agent = status.agent
    chip, style = STATE_CHIP.get(status.state, STATE_CHIP["unknown"])
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(f"{ROLE_ICON.get(agent.role, CUSTOM_ROLE_ICON)} ")
    text.append(f"{agent.label:<13} ")
    text.append(chip, style=style)
    if status.state == "exited" and agent.exit_status is not None:
        text.append(f"({agent.exit_status})", style="dim")
    return text


def project_title_text(project: ProjectInfo, statuses: list[FleetAgentStatus]) -> Text:
    """``🗂 api  amber-otter   3 · 🔔1`` — name, codename badge, chips."""
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(f"🗂 {project_name(project)}")
    if project.codename:
        text.append(f"  {project.codename}", style="dim")
    alive = sum(1 for s in statuses if s.state in ALIVE_STATES)
    bells = sum(1 for s in statuses if s.state == "attention")
    if alive or bells:
        text.append("  ")
    if alive:
        text.append(str(alive), style="bold")
    if bells:
        if alive:
            text.append(" · ", style="dim")
        text.append(f"🔔{bells}", style="bold red")
    return text


def doctor_summary_text(ok: int, warn: int, fail: int) -> Text:
    text = Text("Doctor  ", style="bold", no_wrap=True, overflow="ellipsis")
    text.append(f"✓ {ok}  ", style="green")
    text.append(f"⚠ {warn}  ", style="yellow")
    text.append(f"✗ {fail}", style="red")
    return text


# --- rows -------------------------------------------------------------------------


class Activatable(Static):
    """A sidebar line the user can click or press Enter on.

    ``selection_key`` names what it stands for (``project:<id>``,
    ``agent:<id>``, ``doctor``) so the sidebar can re-apply the highlight
    after a rebuild without holding widget references.
    """

    # ``text-wrap`` / ``text-overflow`` live HERE, not on the Rich ``Text`` the rows
    # are handed. Every row's Text is built ``no_wrap=True, overflow="ellipsis"``
    # and Textual drops both: ``Content.from_rich_text`` keeps the plain text and
    # the spans, and the widget's CSS decides how the line is fitted. The default
    # is ``text-wrap: wrap``, so a name wider than the row broke onto a second line
    # that ``height: 1`` then clipped — the selected project row showed its glyph
    # and an empty highlighted band. Measured 2026-09-05 against the reporter's
    # store: a 27-cell basename in the 25-cell title composited as
    # ``'🗂                        '`` while ``visual.plain`` held the whole title,
    # in textual-dark, textual-light, nord and gruvbox alike.
    DEFAULT_CSS = """
    Activatable { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    Activatable.selected { text-style: bold; background: $primary 35%; }
    Activatable.cursor { background: $accent 30%; }
    """

    selection_key: str = ""

    def message(self) -> Message:  # pragma: no cover - every subclass overrides
        """The message this line stands for."""
        raise NotImplementedError

    def activate(self) -> None:
        """Post the row's message, from the row.

        The sender is pinned to this row on purpose. Textual stops a message
        from bubbling past the widget that SENT it (``MessagePump._dispatch_message``:
        "parent is sender, so we stop propagation after parent"), and activation
        arrives from three places — a click on the row, Enter in the sidebar, a
        click on a containing section. Left implicit, the sender would be
        whichever of those was handling its own event, and a message posted by a
        section onto its title would never reach the app.
        """
        self.post_message(self.message().set_sender(self))

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.activate()

    def on_mount(self) -> None:
        sidebar = self._sidebar()
        if sidebar is not None:
            self.set_class(self.selection_key == sidebar.selected_key, "selected")

    def _sidebar(self) -> Sidebar | None:
        for node in self.ancestors:
            if isinstance(node, Sidebar):
                return node
        return None


class AddButton(Activatable):
    """The ``+`` beside Fleet: onboard a project."""

    DEFAULT_CSS = """
    AddButton { width: 3; text-style: bold; color: $accent; }
    """

    def __init__(self) -> None:
        super().__init__(Text(" + "), id="add-project")
        self.selection_key = "add"  # reachable from the keyboard; never highlighted as selected

    def message(self) -> Message:
        return AddProject()


class Disclosure(Static):
    """▾ / ▸ — collapses or expands the agent rows of its card."""

    DEFAULT_CSS = """
    Disclosure { width: 2; height: 1; }
    """

    def __init__(self, collapsed: bool) -> None:
        super().__init__(Text("▸" if collapsed else "▾"))

    def show(self, collapsed: bool) -> None:
        self.update(Text("▸" if collapsed else "▾"))

    def on_click(self, event: events.Click) -> None:
        event.stop()
        card = self._card()
        if card is not None:
            card.toggle()

    def _card(self) -> ProjectCard | None:
        for node in self.ancestors:
            if isinstance(node, ProjectCard):
                return node
        return None


class ProjectTitle(Activatable):
    """The card's header line: name, codename badge, chips. Click → Project view."""

    DEFAULT_CSS = """
    ProjectTitle { width: 1fr; }
    """

    def __init__(self, project: ProjectInfo, statuses: list[FleetAgentStatus]) -> None:
        super().__init__(project_title_text(project, statuses))
        self.project_id = project.id
        self.selection_key = f"project:{project.id}"

    def message(self) -> Message:
        return ProjectSelected(self.project_id)


class AgentRow(Activatable):
    """One fleet agent under its project. Click → Agent view."""

    DEFAULT_CSS = """
    AgentRow { padding-left: 3; }
    """

    def __init__(self, status: FleetAgentStatus) -> None:
        super().__init__(agent_row_text(status), id=f"agent-row-{status.agent.id}")
        self.status = status
        self.selection_key = f"agent:{status.agent.id}"

    def show(self, status: FleetAgentStatus) -> None:
        if status != self.status:
            self.status = status
            self.update(agent_row_text(status))

    def message(self) -> Message:
        return AgentSelected(self.status.agent.project_id, self.status.agent.id)


class SpawnRow(Activatable):
    """The spawn-agent row — opens the Spawn dialog for this project."""

    DEFAULT_CSS = """
    SpawnRow { padding-left: 3; color: $text-muted; }
    """

    def __init__(self, project_id: str) -> None:
        # U+FF0B, the fullwidth plus of the plan's mockup (§4): visibly not the header's +.
        super().__init__(Text("\uff0b spawn agent"), classes="spawn-row")
        self.project_id = project_id
        self.selection_key = f"spawn:{project_id}"

    def message(self) -> Message:
        return SpawnAgent(self.project_id)


class ProjectCard(Vertical):
    """One project: header row, optional path subtitle, agent rows, spawn row.

    Everything the card shows is held on the instance and (re)painted from
    there, so ``show`` can be called before the card has composed (the data is
    picked up by ``compose``) and after (the children are updated in place).
    """

    DEFAULT_CSS = """
    ProjectCard { height: auto; padding: 0 1; }
    ProjectCard.even { background: $surface; }
    ProjectCard.odd { background: $panel; }
    ProjectCard #card-header { height: 1; }
    ProjectCard .card-subtitle {
        height: 1; padding-left: 3; color: $text-muted;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    ProjectCard .card-notice { height: auto; padding-left: 3; color: $text-muted; }
    ProjectCard #agents { height: auto; }
    """

    def __init__(
        self,
        project: ProjectInfo,
        statuses: list[FleetAgentStatus],
        *,
        subtitle: str | None = None,
        notice: str | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id, classes="project-card")
        self.project = project
        self.statuses = statuses
        self.subtitle = subtitle
        self.notice = notice
        self.collapsed = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="card-header"):
            yield Disclosure(self.collapsed)
            yield ProjectTitle(self.project, self.statuses)
        yield Static(Text(self.subtitle or "", style="dim"), classes="card-subtitle")
        with Vertical(id="agents"):
            for status in self.statuses:
                yield AgentRow(status)
            yield Static(Text(self.notice or "", style="dim"), classes="card-notice")
            yield SpawnRow(self.project.id)

    def on_mount(self) -> None:
        self._paint_decorations()

    def show(
        self,
        project: ProjectInfo,
        statuses: list[FleetAgentStatus],
        *,
        subtitle: str | None,
        notice: str | None,
    ) -> None:
        """Update in place: rows are keyed by agent id and reused."""
        self.project = project
        self.statuses = statuses
        self.subtitle = subtitle
        self.notice = notice
        if not self.is_mounted:
            return  # compose() will read the fields above
        try:
            self.query_one(ProjectTitle).update(project_title_text(project, statuses))
            holder = self.query_one("#agents", Vertical)
        except NoMatches:  # composing right now — compose reads the fields
            return
        rows = {row.status.agent.id: row for row in holder.query(AgentRow)}
        for index, status in enumerate(statuses):
            row = rows.pop(status.agent.id, None)
            if row is None:
                holder.mount(AgentRow(status), before=index)
            else:
                row.show(status)
                if holder.children[index] is not row:
                    holder.move_child(row, before=index)
        for stale in rows.values():
            stale.remove()
        self._paint_decorations()

    def toggle(self) -> None:
        """Collapse or expand the agent rows; the header stays."""
        self.collapsed = not self.collapsed
        self._paint_decorations()

    def _paint_decorations(self) -> None:
        try:
            self.query_one(Disclosure).show(self.collapsed)
            subtitle = self.query_one(".card-subtitle", Static)
            subtitle.update(Text(self.subtitle or "", style="dim"))
            subtitle.display = bool(self.subtitle)
            notice = self.query_one(".card-notice", Static)
            notice.update(Text(self.notice or "", style="dim"))
            notice.display = bool(self.notice)
            self.query_one("#agents", Vertical).display = not self.collapsed
        except NoMatches:
            return


class DoctorTitle(Activatable):
    """The Doctor section's first line: the counts. Click → Doctor view."""

    def __init__(self) -> None:
        super().__init__(Text("Doctor", style="bold"))
        self.selection_key = "doctor"
        self.project_id: str | None = None

    def message(self) -> Message:
        return DoctorSelected(self.project_id)


class DoctorSection(Vertical):
    """Counts plus the top ⚠/✗ lines for the selected project (global when none)."""

    DEFAULT_CSS = """
    DoctorSection { height: auto; max-height: 6; border-top: solid $primary; padding: 0 1; }
    DoctorSection .doctor-line {
        height: 1; color: $text-muted; text-wrap: nowrap; text-overflow: ellipsis;
    }
    DoctorSection .doctor-notice { height: auto; color: $warning; }
    """

    def compose(self) -> ComposeResult:
        yield DoctorTitle()
        yield Static("", classes="doctor-notice")
        for _ in range(DOCTOR_LINES):
            yield Static("", classes="doctor-line")

    def on_click(self, event: events.Click) -> None:
        # A click anywhere in the section — a finding line, blank space — opens
        # the Doctor view, exactly as a click on the counts does.
        event.stop()
        self.query_one(DoctorTitle).activate()


# --- the pane ---------------------------------------------------------------------


class Sidebar(Vertical):
    """Header, the scrolling project list, and the Doctor section at the bottom.

    Focusable, because §4.3 puts focus either here or in a terminal pane:
    ↑/↓ move a cursor over the rows, Enter activates the row under it, and the
    escape hatch from a pane lands here.
    """

    DEFAULT_CSS = """
    Sidebar { width: 30; min-width: 24; border-right: solid $primary; }
    Sidebar:focus { border-right: solid $accent; }
    Sidebar #fleet-header { height: 1; padding: 0 1; }
    Sidebar #fleet-title { width: 1fr; text-style: bold; }
    Sidebar #projects { height: 1fr; }
    Sidebar #projects-notice { height: auto; padding: 0 1; color: $warning; }
    Sidebar #projects-empty { height: auto; padding: 0 1; color: $text-muted; }
    """

    BINDINGS: ClassVar = [
        ("down", "cursor_down", "next"),
        ("up", "cursor_up", "previous"),
        ("enter", "activate", "open"),
    ]

    can_focus = True

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.selected_key: str | None = None
        """What is highlighted: ``project:<id>``, ``agent:<id>``, ``doctor`` or ``None``."""
        self._prev_states: dict[str, str] = {}
        self._cursor_key: str | None = None
        self.last_frame: tuple[list[ProjectInfo], dict[str, list[FleetAgentStatus]]] | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="fleet-header"):
            yield Static(Text("Fleet"), id="fleet-title")
            yield AddButton()
        yield Static("", id="projects-notice")
        # can_focus=False: the rows are Statics, so a mouse-down on one focuses
        # the nearest focusable ancestor. Left focusable, this scroll would take
        # that focus and — once the list overflows — its own up/down bindings
        # would eat the arrows, so ↑/↓ scrolled the list instead of moving the
        # cursor and the Sidebar:focus border never showed. Focus belongs to the
        # Sidebar (§4.3); the cursor scrolls the list through ``scroll_visible``.
        with VerticalScroll(id="projects", can_focus=False):
            yield Static(
                Text("No projects yet — press + to onboard one.", style="dim"),
                id="projects-empty",
            )
        yield DoctorSection(id="doctor-section")

    # --- data in -----------------------------------------------------------------

    def show_projects(
        self,
        projects: list[ProjectInfo],
        agents: dict[str, list[FleetAgentStatus]],
        *,
        notices: Mapping[str, str] | None = None,
    ) -> None:
        """Paint one frame. Cards and rows are updated in place, keyed by id.

        ``notices`` says, per project, why its agent rows are missing (the
        cost of a fleet call that failed open); it is shown as a dim line
        where the rows would be, so an empty card is never mistaken for an
        idle fleet.
        """
        notices = notices or {}
        holder = self.query_one("#projects", VerticalScroll)
        self.query_one("#projects-empty", Static).display = not projects
        existing = {card.project.id: card for card in holder.query(ProjectCard)}
        names = Counter(project_name(p) for p in projects)
        for index, project in enumerate(projects):
            statuses = ordered_agents(agents.get(project.id, []))
            subtitle = short_path(project.root) if names[project_name(project)] > 1 else None
            notice = notices.get(project.id)
            card = existing.pop(project.id, None)
            if card is None:
                card = ProjectCard(
                    project,
                    statuses,
                    subtitle=subtitle,
                    notice=notice,
                    id=f"card-{project.id}",
                )
                slot = index + 1  # after the (hidden) empty-state line
                if slot < len(holder.children):
                    holder.mount(card, before=slot)
                else:
                    holder.mount(card)
            else:
                card.show(project, statuses, subtitle=subtitle, notice=notice)
                slot = index + 1
                if slot < len(holder.children) and holder.children[slot] is not card:
                    holder.move_child(card, before=slot)
            card.set_class(index % 2 == 0, "even")
            card.set_class(index % 2 == 1, "odd")
        for stale in existing.values():
            stale.remove()
        self.last_frame = (projects, agents)
        self._ring_on_attention(status for statuses in agents.values() for status in statuses)
        self._apply_selection()

    def show_notice(self, text: str | None) -> None:
        """A one-line warning above the list (a stale frame, say); ``None`` clears it."""
        notice = self.query_one("#projects-notice", Static)
        notice.update(Text(text or "", style="bold yellow"))
        notice.display = bool(text)

    def show_doctor_summary(
        self, ok: int, warn: int, fail: int, *, lines: Iterable[Text] = ()
    ) -> None:
        """Counts, then up to ``DOCTOR_LINES`` of the worst findings (already ordered)."""
        section = self.query_one(DoctorSection)
        section.query_one(DoctorTitle).update(doctor_summary_text(ok, warn, fail))
        slots = list(section.query(".doctor-line").results(Static))
        pending = list(lines)[: len(slots)]
        for slot, line in zip(slots, pending, strict=False):
            line.no_wrap = True
            line.overflow = "ellipsis"
            slot.update(line)
            slot.display = True
        for slot in slots[len(pending) :]:
            slot.update("")
            slot.display = False

    def show_doctor_notice(self, text: str | None) -> None:
        """A line in the Doctor section for what doctor itself could not do."""
        notice = self.query_one(DoctorSection).query_one(".doctor-notice", Static)
        notice.update(Text(text or "", style="yellow"))
        notice.display = bool(text)

    def set_doctor_scope(self, project_id: str | None) -> None:
        """Which project the Doctor section (and a click on it) is about."""
        self.query_one(DoctorTitle).project_id = project_id

    # --- selection ------------------------------------------------------------------

    def select(self, key: str | None) -> None:
        """Highlight the row for ``key`` (``project:<id>``, ``agent:<id>``, ``doctor``)."""
        self.selected_key = key
        self._apply_selection()

    def _apply_selection(self) -> None:
        for row in self.query(Activatable):
            row.set_class(row.selection_key == self.selected_key, "selected")

    # --- bell -----------------------------------------------------------------------

    def _ring_on_attention(self, statuses: Iterable[FleetAgentStatus]) -> None:
        """Terminal bell when an agent newly flips to needing the user.

        Mirrors ``watch._ring_on_attention``: one bell per frame, only on a
        transition INTO attention from a known other state — never on the first
        frame (nothing changed; the user just opened the UI) and never while it
        stays there.
        """
        states: dict[str, str] = {s.agent.id: s.state for s in statuses}
        for agent_id, state in states.items():
            previous = self._prev_states.get(agent_id)
            if state == "attention" and previous not in (None, "attention"):
                self.app.bell()
                break
        self._prev_states = states

    # --- keyboard -------------------------------------------------------------------

    def _rows(self) -> list[Activatable]:
        """The rows the cursor may land on: activatable and actually on screen."""
        return [
            row for row in self.query(Activatable) if row.selection_key and self._on_screen(row)
        ]

    def _on_screen(self, row: Activatable) -> bool:
        """Is ``row`` visible — including every container between it and here?

        A collapsed card hides its rows by hiding their ``#agents`` holder
        (``_paint_decorations``); the rows' own ``display`` stays True. Filtering
        on that alone walked the cursor onto invisible rows, so ↑/↓ lost the
        highlight and Enter opened an agent the user could not see.
        """
        if not row.display:
            return False
        for node in row.ancestors:
            if node is self:
                return True
            if isinstance(node, Widget) and not node.display:
                return False
        return True

    def _move_cursor(self, step: int) -> None:
        every = [row for row in self.query(Activatable) if row.selection_key]
        on_screen = [index for index, row in enumerate(every) if self._on_screen(row)]
        # Cleared over EVERY row, not only the visible ones: a row hidden inside
        # a collapsed card kept the class forever, so two rows rendered as the
        # keyboard cursor as soon as the card came back.
        for row in every:
            row.remove_class("cursor")
        if not on_screen:
            return
        keys = [every[index].selection_key for index in on_screen]
        anchor = self._cursor_key or self.selected_key
        if anchor in keys:
            slot = max(0, min(len(on_screen) - 1, keys.index(anchor) + step))
        else:
            slot = self._nearest_on_screen(every, on_screen, anchor, step)
        row = every[on_screen[slot]]
        self._cursor_key = row.selection_key
        row.add_class("cursor")
        row.scroll_visible()

    @staticmethod
    def _nearest_on_screen(
        every: list[Activatable], on_screen: list[int], anchor: str | None, step: int
    ) -> int:
        """Which visible row takes the cursor when the anchor row is not one.

        A collapsed card takes its rows off screen with the cursor still on one
        of them: continuing from the top of the pane (index 0) is a jump the user
        did not ask for, so the cursor resumes at the nearest row that IS on
        screen, ahead of the old one when moving down and behind it when moving
        up. No cursor and no selection yet is the other case, and there the first
        row is right — there is no "above" to move to.
        """
        home = next((index for index, row in enumerate(every) if row.selection_key == anchor), None)
        if home is None:
            return 0
        ahead = [slot for slot, index in enumerate(on_screen) if index >= home]
        behind = [slot for slot, index in enumerate(on_screen) if index <= home]
        if step >= 0:
            return ahead[0] if ahead else behind[-1]
        return behind[-1] if behind else ahead[0]

    def action_cursor_down(self) -> None:
        self._move_cursor(1)

    def action_cursor_up(self) -> None:
        self._move_cursor(-1)

    def action_activate(self) -> None:
        for row in self._rows():
            if row.selection_key == self._cursor_key:
                row.activate()
                return
