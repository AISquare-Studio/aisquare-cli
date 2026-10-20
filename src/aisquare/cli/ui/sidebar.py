"""The left pane, two sections: Fleet ▸ projects (alternating background) ▸ agents, then ⚙ Settings.

docs/plans/fleet-tui.md §4.1. One ``ProjectCard`` per registered project, each
with a disclosure, its name (the nickname, else the basename — renamed in place),
chips (agents alive · 🔔 count), a ⋯ menu, and — when two projects share a name —
the path as a dim subtitle; under it one ``AgentRow`` per fleet agent (role icon,
label, state chip, exit status) and a spawn-agent row. Below the separator, one
row, *⚙ Settings* (the Settings hub: App settings, Accounts, Doctor), and under it
one dim status line (:func:`status_line_text`): the AISquare sign-in and the
Claude accounts' headroom as two dots. A click on that line opens Accounts.

Two rules shape the code more than the layout does:

- **Rebuilds update in place.** The app re-reads the store every two seconds
  and calls ``show_projects`` with the whole frame. Cards and rows are keyed by
  project and agent id and mutated, not re-created, so the highlight, the
  keyboard cursor, a collapsed card and the scroll position survive every
  tick, and nothing flickers. Only a project or agent that appeared or vanished
  mounts or unmounts a widget.
- **Rows post messages; the app decides.** ``AddProject``, ``ProjectSelected``,
  ``AgentSelected``, ``SpawnAgent``, ``SettingsSelected``, ``AccountsSelected``,
  ``RenameProject`` and ``OpenProjectMenu`` are the whole contract between this
  pane and the shell (the Settings hub posts ``AppSettingsSelected``,
  ``AccountsSelected`` and ``DoctorSelected``). Nothing here opens a view or
  writes the store.

Every visible string is a ``rich.text.Text`` built with ``append`` — a project
called ``[archive]`` must reach the screen as ``[archive]`` (CONTRIBUTING: no
markup in data).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import ClassVar

from rich.cells import cell_len
from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.geometry import Region
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, Static

from aisquare.cli.ui.groups import (
    DragState,
    DropGroup,
    DropProject,
    GroupProjects,
    MoveRow,
    ToggleCollapse,
    TogglePin,
    UndoLayout,
)
from aisquare.cli.ui.terminal import InPlaceField
from aisquare.models import FleetAgentStatus, HeadroomSummary, ProjectGroup, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services.project import NICKNAME_MAX, display_name
from aisquare.services.project_groups import Arrangement, GroupEntry, arrange

ROLE_ICON: dict[str, str] = {
    "manager": "🧭",
    "planner": "🧭",
    "coder": "🔨",
    "tester": "🧪",
    "runner": "🧪",
    "reviewer": "👀",
    "ui-tester": "🌐",
    "validator": "🛡",
    "remote": "📡",
}
STATE_CHIP: dict[str, tuple[str, str]] = {
    "working": ("▶", "green"),
    "waiting": ("⏸", "yellow"),
    "attention": ("🔔", "bold red"),
    "limited": ("⏳", "magenta"),
    "exited": ("💤", "dim"),
    "lost": ("✗", "red"),
    "unknown": ("·", "dim"),
}
CUSTOM_ROLE_ICON = "🤖"
"""The icon for a role the table above does not know (a `team bind` role, say)."""

ALIVE_STATES: frozenset[str] = fleet_service.PROCESS_STATES
"""States that count toward the card's "agents alive" chip — a limited agent is
alive and parked (#146), not gone. The fleet's own set, which Stop and ``--all`` act on:
a third copy of it could count as alive what they leave, or the reverse (review of #266,
round 3)."""


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


class AccountsSelected(Message):
    """The Accounts page: the AISquare sign-in and the Claude Code accounts."""


class SettingsSelected(Message):
    """*⚙ Settings*: the Settings hub, with its links to App settings, Accounts and Doctor."""


class AppSettingsSelected(Message):
    """The hub's *App settings*: the machine-wide settings page."""


class RenameProject(Message):
    """A card's name field was saved: call ``project_id`` ``typed`` (blank: its folder's name)."""

    def __init__(self, project_id: str, typed: str) -> None:
        self.project_id = project_id
        self.typed = typed
        super().__init__()


class ProjectRenamed(Message):
    """A project's nickname was written — by a card, its menu or a Settings tab: repaint."""

    def __init__(self, project: ProjectInfo) -> None:
        self.project = project
        super().__init__()


class OpenProjectMenu(Message):
    """⋯ on a card, ``.`` or a right-click on its title: the card's menu, under ``anchor``."""

    def __init__(self, project_id: str, anchor: Region) -> None:
        self.project_id = project_id
        self.anchor = anchor
        super().__init__()


RESIZE_STEP = 4
"""Columns one keyboard step moves the partition (#137)."""


class ResizeSidebar(Message):
    """The keyboard asks for the partition to move ``delta`` columns; ``None`` puts it back.

    Posted by the sidebar, handled by the container that owns both it and the
    divider (``app.Panes``): the two are siblings, and a bubbled message reaches
    their parent, never each other.
    """

    def __init__(self, delta: int | None) -> None:
        self.delta = delta
        super().__init__()


# --- pure helpers (unit-testable without a running app) -----------------------------


def project_name(project: ProjectInfo) -> str:
    """The display name: the nickname, the root's basename, else the id (§5.7)."""
    return display_name(project)


def agent_name(label: str, project: ProjectInfo | None) -> str:
    """``coder-1 (alpha)``: an agent's label, with the project that tells one ``manager``
    from another; the label alone when the project is not known. The one way a toast names
    an agent across projects: the agent view's restart and Settings ▸ Fleet's alike."""
    return f"{label} ({project_name(project)})" if project is not None else label


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


LABEL_WIDTH = 13
"""The cells an agent row gives its label when there is room, so the chips line up on rows
that fit. A row that would not fit gives cells back from its label, the ``#N·NN%`` held
whole at the right: a wide chip (``🔔``, ``💤 exited(N)``) or ``100%`` sits further left."""


def agent_row_text(
    status: FleetAgentStatus, percent: float | None = None, *, width: int | None = None
) -> Text:
    """``🧭 manager       ⏸ #1·42%`` — icon, label, state chip, the account's slot and the
    session use on it, exit status when exited.

    ``#<slot>`` is ``FleetAgent.account_slot`` and ``percent`` that account's five-hour
    window, percent USED (:func:`session_percent_text`); with no percent the slot alone,
    with no slot neither. An exited row shows ``💤 exited(N)`` instead: it no longer draws
    on the account, and at the default width the two did not fit beside each other.

    ``width`` is the row's content width once it has one. The label gives way first,
    ellipsised, so the chip and the ``#N·NN%`` after it are never what is cut.
    """
    agent = status.agent
    chip, style = STATE_CHIP.get(status.state, STATE_CHIP["unknown"])
    icon = f"{ROLE_ICON.get(agent.role, CUSTOM_ROLE_ICON)} "
    tail = Text()
    tail.append(chip, style=style)
    if status.state == "exited":
        # 💤 alone read as "sleeping" (#138); the word says what happened.
        tail.append(" exited", style="dim")
        if agent.exit_status is not None:
            tail.append(f"({agent.exit_status})", style="dim")
    elif agent.account_slot is not None:
        tail.append(f" #{agent.account_slot}" + ("·" if percent is not None else ""), style="dim")
        if percent is not None:
            tail.append_text(session_percent_text(percent))
    label = Text(agent.label)
    room = LABEL_WIDTH if width is None else max(2, width - cell_len(icon) - 1 - tail.cell_len)
    if width is None or label.cell_len <= room:
        label.pad_right(min(LABEL_WIDTH, room) - label.cell_len)
    else:
        label.truncate(room, overflow="ellipsis")
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(icon)
    text.append_text(label)
    text.append(" ")
    text.append_text(tail)
    return text


def project_title_text(project: ProjectInfo) -> Text:
    """``🗂 Auth rewrite`` — the name a person gave it (else the folder's), and nothing else.

    The codename is not shown: it names the tmux session and the branches, and a
    nickname says what the project is. The chips are their own widget
    (:func:`project_chips_text`), so a long name is cut before them, not them.
    """
    return Text(f"🗂 {project_name(project)}", no_wrap=True, overflow="ellipsis")


def project_chips_text(project: ProjectInfo, statuses: list[FleetAgentStatus]) -> Text:
    """``  captured  3 · 🔔1`` — what follows the name: captured, agents alive, 🔔 count."""
    text = Text(no_wrap=True)
    if project.onboarded_at is None:
        # Listed only while the shell shows captured directories (#139).
        text.append("  captured", style="dim italic")
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


SETTINGS_KEY = "settings"
"""The Settings row's selection key: highlighted while the hub or any page it opens shows."""

SETTINGS_ICON = "\u2699\ufe0e"
"""⚙ in its text presentation (VS15): a monotone gear, never the coloured emoji."""

GREEN, YELLOW, ORANGE_RED, RED = "#3fb950", "#e6c200", "#ff5f00", "#dc143c"
"""The status line's dot colours, as truecolor: an ANSI name is the THEME's to paint.
Textual paints ``yellow`` through the theme's ANSI palette — textual-dark's is an
orange, #fd971f — so the 25-50 % dot sat beside the 5-25 % one's #ff8700 and the
two read as one (validator of PR #261). Neighbouring bands are ΔE 42 or more apart."""

HEADROOM_DOT: dict[str, tuple[str, str]] = {
    "good": ("●", GREEN),
    "fair": ("●", YELLOW),
    "low": ("●", ORANGE_RED),
    "critical": ("●", RED),
    "empty": ("💀", ""),
    "unknown": ("●", "grey50"),
}
"""The Claude dot per ``HeadroomSummary.level``: over 50 % left green, 25-50 % yellow,
5-25 % orange-red, under 5 % red, nothing left a skull, not read yet grey."""

SESSION_WARN_FROM = 70
"""From this share of an account's five-hour window USED, its figure is yellow."""
SESSION_HIGH_FROM = 90
"""From this share USED, red: the account is close to its session limit."""


def session_percent_text(percent: float) -> Text:
    """``42%`` — an account's five-hour window, percent USED, in the colour of how close it is.

    Green below :data:`SESSION_WARN_FROM`, yellow up to :data:`SESSION_HIGH_FROM`, red from
    there: the one format the sidebar's agent rows and the agent info line share. The
    colour is the shown figure's, so ``70%`` is never green for a 69.6 read. The SESSION
    window only (``HeadroomSummary.session_by_slot``), never the weekly one.
    """
    shown = round(percent)
    colour = RED if shown >= SESSION_HIGH_FROM else YELLOW if shown >= SESSION_WARN_FROM else GREEN
    return Text(f"{shown}%", style=colour)


TIMES = "\N{MULTIPLICATION SIGN}"
"""The ``x`` in ``3x claude``."""


def headroom_dot(
    headroom: HeadroomSummary | None, *, every_needs_login: bool = False
) -> tuple[str, str]:
    """The Claude dot and its colour. Red when no account is usable because EVERY enabled
    account needs a login (signed out, its token expired or refused) — ``every_needs_login``,
    which the shell counts against its frame: something to do, not "not read". Grey, never
    the skull, when nothing could be READ otherwise: ``empty`` with no ``percent_left`` is
    offline, a locked Keychain or no usable account — not a fleet at 0 %, and not red because
    a spare beside them is signed out (review of #261, N2).
    """
    if headroom is not None and headroom.usable == 0 and every_needs_login:
        return ("●", RED)
    if headroom is None or (headroom.level == "empty" and headroom.percent_left is None):
        return HEADROOM_DOT["unknown"]
    return HEADROOM_DOT[headroom.level]


def status_line_text(
    aisquare: bool | None, headroom: HeadroomSummary | None, *, every_needs_login: bool = False
) -> Text:
    """``● aisquare  ● 3x claude`` (the x a multiplication sign) — the line under *⚙ Settings*.

    The AISquare dot is green signed in, red signed out, grey while unknown. The
    Claude dot is :func:`headroom_dot`; the count is how many accounts are usable,
    and a ``⚠`` follows when an account needs a login. ``headroom`` is ``None`` until
    the first summary lands.
    """
    text = Text(no_wrap=True, overflow="ellipsis")
    colour = "grey50" if aisquare is None else GREEN if aisquare else RED
    text.append("● ", style=colour)
    text.append("aisquare", style="dim")
    text.append("  ")
    dot, colour = headroom_dot(headroom, every_needs_login=every_needs_login)
    text.append(f"{dot} ", style=colour)
    if headroom is not None:
        text.append(f"{headroom.usable}{TIMES} ", style="dim")
    text.append("claude", style="dim")
    if headroom is not None and headroom.needs_login:
        text.append(" ⚠", style="yellow")
    return text


# --- rows -------------------------------------------------------------------------


class Activatable(Static):
    """A sidebar line the user can click or press Enter on.

    ``selection_key`` names what it stands for (``project:<id>``,
    ``agent:<id>``, ``settings``) so the sidebar can re-apply the highlight
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


class DragHandle(Activatable):
    """A row that is also a drag handle (#140): a card's title, a group header.

    The HANDLE holds the mouse from the press to the release, so the whole
    gesture comes back to it, in order: the moves and the release are handed to
    the sidebar's drag, and a press released without motion is still the row's
    click. It must be the handle, not the sidebar: the app turns a MouseUp over
    the pressed widget into a Click in the same call that queues the MouseUp,
    and routes it by the capture standing then. Held by the sidebar, every
    Click went to the sidebar — a plain click on a title opened nothing and one
    on a header folded nothing (review of #171, round 1; ``pilot.click`` pauses
    between its events, so the suite never saw it).
    """

    ALLOW_SELECT: ClassVar[bool] = False
    """A drag handle is not text, as the divider is not (``divider.py`` says why).

    The screen opens a text selection on the press BEFORE it forwards it, so the
    capture ``on_mouse_down`` takes is too late to stop it, and every move of
    the drag extended it: a regroup left a highlight across the sidebar, and a
    card released over an agent's pane copied the pane's rows and left them
    standing, so the pane's next ctrl+c copied instead of interrupting the
    agent (final review of #203, F1).
    """

    _dragged = False
    """Whether the gesture that just ended here was a drag (so its Click is not a click)."""

    def drag_state(self, sidebar: Sidebar) -> DragState | None:  # pragma: no cover - overridden
        """What a press here would drag; ``None`` when nothing here moves (a pinned row)."""
        raise NotImplementedError

    def dragged_row(self) -> Widget:
        """The row a drag from here moves: it dims while it moves."""
        return self

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self._dragged = False
        sidebar = self._sidebar()
        if sidebar is None:
            return
        if event.button == 1 and sidebar.dragging(self):
            # Button 1 pressed while a press of it still holds a drag from here:
            # that release was lost, on a terminal that reports no motion without
            # a button, so no buttonless move ended the drag (``drag_over`` does
            # where one is reported; DUPLICATE_PRESS_WINDOW is SelectionHost's
            # side of the same case). This press reached the handle only because
            # the handle still held the mouse. The old drag ends and snaps back,
            # the handle lets go, and this press starts nothing: its release is a
            # click on the row under the pointer. Re-armed here, the handle kept
            # the mouse, the Click came to it, and a click on api opened docs
            # (review of #171, round 2).
            self.release_mouse()
            sidebar.cancel_drag(self)
            return
        if event.button != 1 or event.shift:
            return  # a shift+click is a mark, decided on the click
        state = self.drag_state(sidebar)
        if state is None:
            return  # nothing to drag: the press stays a click
        state.origin_y = event.screen_y
        sidebar.begin_drag(state, self)

    def on_mouse_move(self, event: events.MouseMove) -> None:
        sidebar = self._sidebar()
        if sidebar is not None:
            sidebar.drag_over(self, event)

    def on_mouse_up(self, event: events.MouseUp) -> None:
        sidebar = self._sidebar()
        if sidebar is None:
            return
        if event.button != 1:
            # Only button 1 drags, so another button's release ends nothing here.
            # Under FleetApp it arrives only as a lone click of that button: while
            # button 1 is down, SelectionHost drops a second button's press and
            # release at the app, before the screen sees either. Should one reach
            # a held drag all the same, the drag goes on, and the Click the app
            # builds from the release is no click.
            self._dragged = sidebar.dragging(self)
            return
        self._dragged = sidebar.end_drag(self)

    def on_click(self, event: events.Click) -> None:
        if self._dragged:
            # The release of a drag is not a click. Textual runs ``on_click`` of
            # every class in the MRO, so returning alone would still let
            # ``Activatable.on_click`` open the row: ``prevent_default`` is the stop.
            self._dragged = False
            event.stop()
            event.prevent_default()

    def on_unmount(self) -> None:
        # Removed mid-drag (its project forgotten, its group deleted from a
        # shell): Textual keeps a capture on a widget that is gone, and every
        # later mouse event would be delivered nowhere. The drag goes with it:
        # its release now lands on another widget, which ends no drag, so the
        # drop mark stayed on the card under the pointer.
        self.release_mouse()
        sidebar = self._sidebar()
        if sidebar is not None:
            sidebar.cancel_drag(self)


class ProjectTitle(DragHandle):
    """The card's name. Click → Project view; double-click → rename it in place.

    Also the card's drag handle (#140): press and hold, move, drop on a group
    header or between cards. A shift+click marks the card for a multi-selection
    instead of opening it, and a right-click opens the card's menu.
    """

    DEFAULT_CSS = """
    ProjectTitle { width: 1fr; }
    """

    def __init__(self, project: ProjectInfo) -> None:
        super().__init__(project_title_text(project))
        self.project_id = project.id
        self.selection_key = f"project:{project.id}"
        self.tooltip = "double-click or F2 to rename · right-click or . for the menu"

    def message(self) -> Message:
        return ProjectSelected(self.project_id)

    def drag_state(self, sidebar: Sidebar) -> DragState | None:
        """The card, or the selection it is in — never a pinned card.

        A pinned card's place is the pin order, which is ``p``'s: ``step`` calls
        it "nothing to move", and no card or empty space is a place for it.
        Dragged, it left its group with its card still under Pinned — nothing on
        screen changed, and ``u`` had a step to undo (review of #171, round 1).
        """
        pinned = sidebar.pinned_ids()
        if self.project_id in pinned:
            return None
        ids = sidebar.marked_ids()
        if self.project_id not in ids:
            ids = [self.project_id]
        return DragState("project", self.project_id, [pid for pid in ids if pid not in pinned])

    def dragged_row(self) -> Widget:
        # The whole card moves, not this line of it: only the card's dimming is styled.
        for node in self.ancestors:
            if isinstance(node, ProjectCard):
                return node
        return self

    _opened = False
    """Whether the click before this one was a plain click that opened this title: only
    then is a second one at the same place a double-click (a rename). Textual chains
    any two clicks at one spot, a shift+click's mark and the click after it too."""

    def on_click(self, event: events.Click) -> None:
        sidebar = self._sidebar()
        opened, self._opened = self._opened, False
        if sidebar is None or self._dragged:
            return  # a drag's release: ``DragHandle.on_click`` says it is no click
        # Each a gesture of its own, not an open: ``prevent_default`` keeps the
        # handlers of the base classes (which open the row) from running after this one.
        if event.button == 3:
            event.stop()
            event.prevent_default()
            sidebar.open_menu(self.project_id)
        elif event.shift:
            event.stop()
            event.prevent_default()
            sidebar.toggle_mark(self.project_id)
        elif event.button == 1 and event.chain >= 2 and opened:
            # The first click of the two opened the project; the second renames it.
            event.stop()
            event.prevent_default()
            sidebar.open_rename(self.project_id)
        elif event.button == 1:
            self._opened = True  # and ``Activatable.on_click`` opens it


class NicknameInput(InPlaceField, Input):
    """The card's name as a field: Enter submits, Esc posts ``Cancelled``, and leaving it —
    a press anywhere outside it too (:class:`InPlaceField`) — posts ``Left``: focus-out
    saves, as Enter does."""

    BINDINGS: ClassVar = [Binding("escape", "cancel_rename", "cancel", show=False)]

    class Cancelled(Message):
        """Esc in the field: close it and write nothing."""

    class Left(Message):
        """The field lost the focus while open."""

    def action_cancel_rename(self) -> None:
        self.post_message(self.Cancelled())

    def on_blur(self) -> None:
        self.post_message(self.Left())

    def _on_paste(self, event: events.Paste) -> None:
        """A paste longer than the room left keeps what fits: ``Input``'s own refuses one
        that would pass ``max_length`` whole, without a word, and a long name pasted in
        left the field as it was."""
        line = event.text.splitlines()[0] if event.text else ""
        limit = self.max_length or 0
        room = limit - len(self.value) + len(self.selected_text)
        if not limit or len(line) <= room:
            return  # ``Input._on_paste`` takes it as it is
        event.prevent_default()
        event.stop()
        fitted = line[: max(0, room)]
        if self.selection.is_empty:
            self.insert_text_at_cursor(fitted)
        else:
            self.replace(fitted, *self.selection)


class CardChips(Static):
    """What follows the name on the card's header: captured, agents alive, the 🔔 count.

    A widget of its own so the name is what a narrow card cuts, never the chips. A
    click on it is a click on the name.
    """

    DEFAULT_CSS = """
    CardChips { width: auto; height: 1; }
    """

    def on_click(self, event: events.Click) -> None:
        event.stop()
        card = next((node for node in self.ancestors if isinstance(node, ProjectCard)), None)
        if card is not None:
            card.query_one(ProjectTitle).activate()


class CardMenuButton(Static):
    """⋯ — the card's menu: Rename, Pin / Unpin, Open settings, Remove from fleet…"""

    DEFAULT_CSS = """
    CardMenuButton { width: 2; height: 1; content-align: right middle; color: $text-muted; }
    CardMenuButton:hover { color: $accent; text-style: bold; }
    """

    def __init__(self) -> None:
        super().__init__(Text("⋯"), classes="card-menu")
        self.tooltip = "rename, pin, settings, remove (. or right-click)"

    def on_click(self, event: events.Click) -> None:
        event.stop()
        card = next((node for node in self.ancestors if isinstance(node, ProjectCard)), None)
        sidebar = next((node for node in self.ancestors if isinstance(node, Sidebar)), None)
        if card is not None and sidebar is not None:
            sidebar.open_menu(card.project.id)


class AgentRow(Activatable):
    """One fleet agent under its project. Click → Agent view."""

    DEFAULT_CSS = """
    AgentRow { padding-left: 3; }
    """

    def __init__(self, status: FleetAgentStatus) -> None:
        super().__init__(agent_row_text(status), id=f"agent-row-{status.agent.id}")
        self.status = status
        self.selection_key = f"agent:{status.agent.id}"
        self._painted: tuple[FleetAgentStatus, float | None, int] | None = None

    def show(self, status: FleetAgentStatus) -> None:
        self.status = status
        self.paint()

    def paint(self) -> None:
        """Draw the row for its status, its account's session use and its width.

        The use is the sidebar's (:attr:`Sidebar.session_usage`, from the slow headroom
        read), so a row mounted between two reads shows the last figure at once.
        """
        sidebar = self._sidebar()
        slot = self.status.agent.account_slot
        usage = sidebar.session_usage if sidebar is not None else {}
        percent = usage.get(slot) if slot is not None else None
        width = self.content_size.width
        wanted = (self.status, percent, width)
        if wanted != self._painted:
            self._painted = wanted
            self.update(agent_row_text(self.status, percent, width=width or None), layout=False)

    def on_resize(self) -> None:
        self.paint()

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


def group_header_text(entry: GroupEntry, agents: Mapping[str, list[FleetAgentStatus]]) -> Text:
    """``▾ 📁 frontend  3 · 🔔1`` — the disclosure, the name, and the roll-up over its members.

    The roll-up sums ``ALIVE_STATES`` and the bells over EVERY member, the
    pinned ones included (they are listed under Pinned but they are still the
    group's), so a collapsed group still says something is running in it.
    """
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append("▸ " if entry.group.collapsed else "▾ ")
    text.append(f"📁 {entry.group.name}", style="bold")
    if entry.group.pinned_at is not None:
        text.append(" 📌", style="dim")
    members = [*entry.members, *entry.pinned_members]
    statuses = [s for member in members for s in agents.get(member.id, [])]
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


class GroupHeader(DragHandle):
    """A group's line: Enter or a click folds and unfolds it; drag it to reorder groups (#140)."""

    # Two rows while it is the drop target: the accent line takes one, and on a
    # one-row header it took the only row — the name went blank under the pointer.
    DEFAULT_CSS = """
    GroupHeader { padding: 0 1; color: $text; background: $boost; }
    GroupHeader.-dragging { opacity: 60%; }
    GroupHeader.-drop-before { height: 2; border-top: solid $accent; }
    """

    def __init__(self, entry: GroupEntry, agents: Mapping[str, list[FleetAgentStatus]]) -> None:
        super().__init__(group_header_text(entry, agents), id=f"group-{entry.group.id}")
        self.group: ProjectGroup = entry.group
        self.selection_key = f"group:{entry.group.id}"

    def show(self, entry: GroupEntry, agents: Mapping[str, list[FleetAgentStatus]]) -> None:
        self.group = entry.group
        self.update(group_header_text(entry, agents))

    def message(self) -> Message:
        return ToggleCollapse(self.group.id)

    def drag_state(self, sidebar: Sidebar) -> DragState | None:
        # A pinned group keeps its pin order, as a pinned card does (``step_group``
        # has nothing to move either): dropped, it renumbered the unpinned groups.
        if self.group.pinned_at is not None:
            return None
        return DragState("group", self.group.id)


class SectionLabel(Static):
    """``📌 Pinned`` — a heading that is not a row (the cursor skips it)."""

    DEFAULT_CSS = """
    SectionLabel { height: 1; padding: 0 1; color: $text-muted; text-style: italic; }
    """


class ProjectCard(Vertical):
    """One project: header row, optional path subtitle, agent rows, spawn row.

    Everything the card shows is held on the instance and (re)painted from
    there, so ``show`` can be called before the card has composed (the data is
    picked up by ``compose``) and after (the children are updated in place).

    The name is renamed in place: double-click it, press F2 on the card, or
    choose Rename in its menu, and it becomes a field prefilled with the name,
    all selected. Enter or leaving the field saves (``RenameProject``; blank is
    the folder's name again), Esc closes it untouched, and a frame never touches
    what is being typed. The card shows the new name at once; the shell writes
    it and says so if the store refused, reopening the field with what was typed.
    """

    DEFAULT_CSS = """
    ProjectCard { height: auto; padding: 0 1; }
    ProjectCard.even { background: $surface; }
    ProjectCard.odd { background: $panel; }
    ProjectCard.grouped { padding-left: 2; }
    ProjectCard.marked #card-header { background: $secondary 30%; }
    ProjectCard.-dragging { opacity: 60%; }
    ProjectCard.-drop-before { border-top: solid $accent; }
    ProjectCard #card-header { height: 1; }
    ProjectCard NicknameInput { width: 1fr; height: 1; }
    ProjectCard .card-menu { visibility: hidden; }
    ProjectCard:hover .card-menu, ProjectCard.-active .card-menu { visibility: visible; }
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
        self.scope: str | None = None
        """The group this card is shown under, or ``None`` at the top level / under Pinned."""
        self.prefill = ""
        """What the name field said when it opened: leaving it so is no change."""

    def compose(self) -> ComposeResult:
        with Horizontal(id="card-header"):
            yield Disclosure(self.collapsed)
            yield ProjectTitle(self.project)
            field = NicknameInput(
                max_length=NICKNAME_MAX, compact=True, id=f"nickname-{self.project.id}"
            )
            field.tooltip = "any text · Enter saves, empty is the folder's name, Esc cancels"
            field.display = False
            yield field
            yield CardChips(project_chips_text(self.project, self.statuses))
            yield CardMenuButton()
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
            self.query_one(ProjectTitle).update(project_title_text(project))
            self.query_one(CardChips).update(project_chips_text(project, statuses))
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

    # --- the name, renamed in place ---------------------------------------------------

    @property
    def name_field(self) -> NicknameInput:
        return self.query_one(NicknameInput)

    @property
    def renaming(self) -> bool:
        """Whether the name's field is open on this card."""
        return self.name_field.display

    def open_rename(self, typed: str | None = None) -> None:
        """Open the field over the name, prefilled with it (or ``typed``), all selected.

        Already open, it only takes the focus back: what was typed is kept.
        """
        field = self.name_field
        if not field.display:
            self.prefill = project_name(self.project)
            field.value = self.prefill if typed is None else typed
            self.query_one(ProjectTitle).display = False
            self.query_one(CardChips).display = False
            field.display = True
        field.focus()
        field.select_all()

    def close_rename(self, *, refocus: bool) -> None:
        """Close the field and show the name; ``refocus`` hands the keys back to the sidebar
        (Enter, Esc) — not when the field closed because a click took the focus elsewhere."""
        field = self.name_field
        if not field.display:
            return
        field.display = False
        self.query_one(ProjectTitle).display = True
        self.query_one(CardChips).display = True
        sidebar = next((node for node in self.ancestors if isinstance(node, Sidebar)), None)
        if refocus and sidebar is not None:
            sidebar.focus()

    def _commit(self, typed: str, *, refocus: bool) -> None:
        """Save what the field says — or, when it is the name it opened with, just close it.

        The card says the new name at once; the shell writes it (``RenameProject``).
        """
        self.close_rename(refocus=refocus)
        if typed.strip() == self.prefill.strip():
            return
        shown = self.project.model_copy(update={"nickname": typed.strip() or None})
        self.query_one(ProjectTitle).update(project_title_text(shown))
        self.post_message(RenameProject(self.project.id, typed))

    @on(Input.Submitted, "NicknameInput")
    def _submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._commit(event.value, refocus=True)

    @on(NicknameInput.Left)
    def _left(self, event: NicknameInput.Left) -> None:
        """Focus-out saves, as Enter does — while the field is still open, and not when the
        terminal itself lost the focus (Textual blurs the focused widget then)."""
        event.stop()
        if self.renaming and self.app.app_focus:
            self._commit(self.name_field.value, refocus=False)

    @on(NicknameInput.Cancelled)
    def _cancelled(self, event: NicknameInput.Cancelled) -> None:
        event.stop()
        self.close_rename(refocus=True)

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


class SettingsRow(Activatable):
    """*⚙ Settings*: the one row below the separator. Click or Enter → the Settings hub."""

    DEFAULT_CSS = """
    SettingsRow { text-style: bold; }
    """

    def __init__(self) -> None:
        super().__init__(Text(f"{SETTINGS_ICON} Settings"), id="settings-row")
        self.selection_key = SETTINGS_KEY

    def message(self) -> Message:
        return SettingsSelected()

    def show_findings(self, failing: int, warning: int) -> None:
        """The newest doctor report's ✗/⚠ counts beside the name, muted; none when all clear.

        The doctor runs at start behind a hidden page: without this nothing said what it
        found until someone opened Doctor (review of #261, m9).
        """
        text = Text(f"{SETTINGS_ICON} Settings")
        badge = doctor_badge(failing, warning)
        if badge:
            text.append(f"  {badge}", style="dim not bold")
        self.update(text)


def doctor_badge(failing: int, warning: int) -> str:
    """A doctor report's counts, the nonzero ones: ``✗ 1  ⚠ 2``; ``""`` when nothing failed
    or warned."""
    return "  ".join(
        f"{glyph} {count}" for glyph, count in (("✗", failing), ("⚠", warning)) if count
    )


class StatusLine(Static):
    """The dim line under *⚙ Settings* (:func:`status_line_text`). A click opens Accounts.

    Not a row: it is never focused, never under the keyboard cursor and never the
    highlighted selection — the Accounts page it opens is one of Settings' pages, so
    *⚙ Settings* stays the highlighted row (the human: "clicking on that should
    directly take me to accounts page but still that line wont be highlighted").
    """

    DEFAULT_CSS = """
    StatusLine { height: 1; color: $text-muted; text-wrap: nowrap; text-overflow: ellipsis; }
    """

    ALLOW_SELECT = False

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(AccountsSelected().set_sender(self))


class OthersSection(Vertical):
    """Everything that is not a project, under the separator: *⚙ Settings* and its status."""

    DEFAULT_CSS = """
    OthersSection { height: auto; border-top: solid $primary; padding: 0 1; }
    """

    def compose(self) -> ComposeResult:
        yield SettingsRow()
        yield StatusLine(status_line_text(None, None), id="status-line")


# --- the pane ---------------------------------------------------------------------


class Sidebar(Vertical):
    """Header and the scrolling project list; under the separator, *⚙ Settings* and its status.

    Focusable, because §4.3 puts focus either here or in a terminal pane:
    ↑/↓ move a cursor over the rows, Enter activates the row under it, and the
    escape hatch from a pane lands here.
    """

    DEFAULT_CSS = """
    Sidebar { width: 30; min-width: 24; }
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
        # The partition, for terminals without mouse reporting (#137): live only
        # while the sidebar has focus, so a pane still receives < > = as text.
        # Out of the footer (it is full); the help screen (?) lists them.
        Binding(
            "greater_than_sign", f"resize({RESIZE_STEP})", "wider", show=False, key_display=">"
        ),
        Binding(
            "less_than_sign", f"resize(-{RESIZE_STEP})", "narrower", show=False, key_display="<"
        ),
        Binding("equals_sign", "resize(None)", "reset width", show=False, key_display="="),
        # Groups, pins and order (#140) — every gesture here has a CLI twin.
        Binding("shift+up", "move_up", "move up", show=False),
        Binding("shift+down", "move_down", "move down", show=False),
        Binding("p", "toggle_pin", "pin", show=False),
        Binding("g", "group_picker", "group", show=False),
        # Shift+G arrives as `G` from a legacy terminal, and from the kitty
        # protocol at the flags Textual enables (the shift is dropped when the
        # key carries text); only a kitty report with no text is `shift+g`.
        # Bound to `shift+g` alone, the gesture the help screen names did
        # nothing in practically every terminal (final review of #203, F4).
        Binding("G,shift+g", "group_marked", "group selection", show=False),
        Binding("m", "toggle_mark", "mark", show=False),
        Binding("space", "toggle_collapse", "fold", show=False),
        Binding("u", "undo_layout", "undo", show=False),
        Binding("escape", "clear_marks", "clear marks", show=False),
        # The card under the cursor: its name in place, and its menu (⋯).
        Binding("f2", "rename", "rename", show=False),
        Binding("full_stop", "project_menu", "menu", show=False, key_display="."),
    ]

    can_focus = True

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.selected_key: str | None = None
        """What is highlighted: ``project:<id>``, ``agent:<id>``, or ``settings`` while the hub
        or a page it opens (App settings, Accounts, Doctor) shows."""
        self.aisquare: bool | None = None
        """The AISquare sign-in the status line shows: signed in, out, or unknown."""
        self.headroom: HeadroomSummary | None = None
        """The Claude accounts' headroom the status line shows; ``None`` until the first."""
        self.every_needs_login = False
        """Whether every enabled account needs a login: the one case the dot is red for it."""
        self.session_usage: Mapping[int, float] = {}
        """Slot → its five-hour window, percent USED, from the last headroom read: the
        ``#N·NN%`` on the agent rows (``HeadroomSummary.session_by_slot``)."""
        self._prev_states: dict[str, str] = {}
        self._cursor_key: str | None = None
        self.last_frame: tuple[list[ProjectInfo], dict[str, list[FleetAgentStatus]]] | None = None
        self.arrangement: Arrangement | None = None
        """The order the last frame was painted in (groups, pins, loose) — #140."""
        self._marked: list[str] = []
        """Project ids marked into a multi-selection (shift+click or ``m``), in marking order."""
        self._drag: DragState | None = None
        self._drag_source: DragHandle | None = None
        self._drop_target: Widget | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="fleet-header"):
            yield Static(Text("Fleet"), id="fleet-title")
            yield AddButton()
        yield Static("", id="projects-notice")
        # can_focus=False: the rows are Statics, so a mouse-down on one focuses
        # the nearest focusable ancestor. Left focusable, this scroll would take
        # that focus and — once the list overflows — its own up/down bindings
        # would eat the arrows, so ↑/↓ scrolled the list instead of moving the
        # cursor and the sidebar never read as focused. Focus belongs to the
        # Sidebar (§4.3); the cursor scrolls the list through ``scroll_visible``.
        with VerticalScroll(id="projects", can_focus=False):
            yield Static(
                Text("No projects yet — press + to onboard one.", style="dim"),
                id="projects-empty",
            )
        yield OthersSection(id="others")

    # --- data in -----------------------------------------------------------------

    def show_projects(
        self,
        projects: list[ProjectInfo],
        agents: dict[str, list[FleetAgentStatus]],
        *,
        notices: Mapping[str, str] | None = None,
        groups: list[ProjectGroup] | None = None,
    ) -> None:
        """Paint one frame. Cards and rows are updated in place, keyed by id.

        ``notices`` says, per project, why its agent rows are missing (the
        cost of a fleet call that failed open); it is shown as a dim line
        where the rows would be, so an empty card is never mistaken for an
        idle fleet.

        The ORDER is the arrangement's (#140): a **Pinned** section, then each
        group's header with its members indented under it (hidden while the
        group is collapsed), then the loose projects — pins and manual order
        from the store, nothing sorted behind the user's back. Children are
        reconciled into that order by id, so a frame costs moves, not rebuilds.
        """
        notices = notices or {}
        # A mark is on a card the user can see. One whose project left the list
        # (forgotten from a shell, a captured directory hidden again with `a`)
        # lost its highlight with its card, and the next drag, `g` or shift+g
        # still carried it: a forgotten id refused the whole drop, and a hidden
        # directory was moved into the group without a word (final review of
        # #203, F5). A card folded away in its group is still listed, and keeps it.
        listed = {project.id for project in projects}
        self._marked = [pid for pid in self._marked if pid in listed]
        arrangement = arrange(projects, groups or [])
        self.arrangement = arrangement
        holder = self.query_one("#projects", VerticalScroll)
        self.query_one("#projects-empty", Static).display = not projects
        existing_cards = {card.project.id: card for card in holder.query(ProjectCard)}
        existing_headers = {header.group.id: header for header in holder.query(GroupHeader)}
        existing_labels = {label.id: label for label in holder.query(SectionLabel)}
        names = Counter(project_name(p) for p in projects)
        ordered: list[Widget] = []
        stripe = 0

        def card_for(project: ProjectInfo, *, scope: str | None, hidden: bool) -> None:
            nonlocal stripe
            statuses = ordered_agents(agents.get(project.id, []))
            subtitle = short_path(project.root) if names[project_name(project)] > 1 else None
            notice = notices.get(project.id)
            card = existing_cards.pop(project.id, None)
            if card is None:
                card = ProjectCard(
                    project, statuses, subtitle=subtitle, notice=notice, id=f"card-{project.id}"
                )
            else:
                card.show(project, statuses, subtitle=subtitle, notice=notice)
            card.scope = scope
            card.set_class(scope is not None, "grouped")
            card.set_class(project.id in self._marked, "marked")
            card.set_class(stripe % 2 == 0, "even")
            card.set_class(stripe % 2 == 1, "odd")
            card.display = not hidden
            stripe += 1
            ordered.append(card)

        def header_for(entry: GroupEntry) -> None:
            header = existing_headers.pop(entry.group.id, None)
            if header is None:
                header = GroupHeader(entry, agents)
            else:
                header.show(entry, agents)
            ordered.append(header)
            for member in entry.members:
                card_for(member, scope=entry.group.id, hidden=entry.group.collapsed)

        if arrangement.pinned:
            label = existing_labels.pop("pinned-label", None) or SectionLabel(
                Text("📌 Pinned"), id="pinned-label"
            )
            ordered.append(label)
            for entry in arrangement.pinned:
                if isinstance(entry, GroupEntry):
                    header_for(entry)
                else:
                    card_for(entry, scope=None, hidden=False)
        for entry in arrangement.groups:
            header_for(entry)
        for project in arrangement.loose:
            card_for(project, scope=None, hidden=False)

        stale: list[Widget] = [
            *existing_cards.values(),
            *existing_headers.values(),
            *existing_labels.values(),
        ]
        for widget in stale:
            widget.remove()
        # Slot 0 is the (hidden) empty-state line; everything else follows the order.
        for index, widget in enumerate(ordered):
            slot = index + 1
            if not widget.is_mounted:
                if slot < len(holder.children):
                    holder.mount(widget, before=slot)
                else:
                    holder.mount(widget)
            elif slot < len(holder.children) and holder.children[slot] is not widget:
                holder.move_child(widget, before=slot)
        self.last_frame = (projects, agents)
        self._ring_on_attention(status for statuses in agents.values() for status in statuses)
        self._apply_selection()

    # --- groups, pins and order (#140) ----------------------------------------------

    def marked_ids(self) -> list[str]:
        return list(self._marked)

    def pinned_ids(self) -> set[str]:
        """The projects the frame on screen lists under Pinned."""
        pinned = self.arrangement.pinned if self.arrangement is not None else []
        return {entry.id for entry in pinned if isinstance(entry, ProjectInfo)}

    def toggle_mark(self, project_id: str) -> None:
        """shift+click or ``m``: add the card to (or drop it from) the multi-selection."""
        if project_id in self._marked:
            self._marked.remove(project_id)
        else:
            self._marked.append(project_id)
        for card in self.query(ProjectCard):
            card.set_class(card.project.id in self._marked, "marked")

    def action_toggle_mark(self) -> None:
        """``m``: shift+click's twin, on the card under the cursor (or the card of its row).

        Most terminals keep Shift+click for their own text selection while an
        app reports the mouse (kitty, the VTE terminals, Konsole, Alacritty,
        WezTerm, xterm, Windows Terminal), so there the click never reached the
        card and nothing could be marked: shift+g and a drag of several cards
        had nothing to carry (final review of #203, F7).
        """
        target = self._cursor_target()
        if target is not None and target[0] == "project":
            self.toggle_mark(target[1])

    def action_clear_marks(self) -> None:
        self._marked.clear()
        for card in self.query(ProjectCard):
            card.remove_class("marked")

    def _cursor_target(self) -> tuple[str, str] | None:
        """``("project", id)`` or ``("group", id)`` for the row under the cursor (or selected)."""
        key = self._cursor_key or self.selected_key
        if not key:
            return None
        kind, _, ident = key.partition(":")
        if kind == "project":
            return ("project", ident)
        if kind == "group":
            return ("group", ident)
        if kind in ("agent", "spawn"):
            for card in self.query(ProjectCard):
                if any(s.agent.id == ident for s in card.statuses) or (
                    kind == "spawn" and card.project.id == ident
                ):
                    return ("project", card.project.id)
        return None

    def action_move_up(self) -> None:
        self._step(-1)

    def action_move_down(self) -> None:
        self._step(1)

    def _step(self, delta: int) -> None:
        target = self._cursor_target()
        if target is not None:
            self.post_message(MoveRow(target[0], target[1], delta))  # type: ignore[arg-type]

    def action_toggle_pin(self) -> None:
        target = self._cursor_target()
        if target is not None:
            self.post_message(TogglePin(target[0], target[1]))  # type: ignore[arg-type]

    def action_toggle_collapse(self) -> None:
        target = self._cursor_target()
        if target is None:
            return
        if target[0] == "group":
            self.post_message(ToggleCollapse(target[1]))
            return
        # Space on a project folds its card, as the disclosure glyph does.
        for card in self.query(ProjectCard):
            if card.project.id == target[1]:
                card.toggle()

    def action_group_picker(self) -> None:
        target = self._cursor_target()
        if self._marked:
            self.post_message(GroupProjects(list(self._marked)))
        elif target is not None and target[0] == "project":
            self.post_message(GroupProjects([target[1]]))

    def action_group_marked(self) -> None:
        if self._marked:
            self.post_message(GroupProjects(list(self._marked)))

    def action_undo_layout(self) -> None:
        self.post_message(UndoLayout())

    # a card's name and its menu

    def _card(self, project_id: str) -> ProjectCard | None:
        return next(
            (card for card in self.query(ProjectCard) if card.project.id == project_id), None
        )

    def open_rename(self, project_id: str, typed: str | None = None) -> None:
        """Open ``project_id``'s name field (``typed`` in it); another card's closes unsaved."""
        for other in self.query(ProjectCard):
            if other.project.id != project_id and other.renaming:
                other.close_rename(refocus=False)
        card = self._card(project_id)
        if card is not None and card.display:
            card.open_rename(typed)

    def open_menu(self, project_id: str) -> None:
        """Ask the shell for ``project_id``'s menu, under its card."""
        card = self._card(project_id)
        if card is not None:
            self.post_message(OpenProjectMenu(project_id, card.region))

    def action_rename(self) -> None:
        """F2: rename the card under the cursor (or the card of the row under it)."""
        target = self._cursor_target()
        if target is not None and target[0] == "project":
            self.open_rename(target[1])

    def action_project_menu(self) -> None:
        """``.``: the menu of the card under the cursor, as its ⋯ opens it."""
        target = self._cursor_target()
        if target is not None and target[0] == "project":
            self.open_menu(target[1])

    # drag and drop: press on a title or a group header, move past its row, release on a target

    def begin_drag(self, state: DragState, source: DragHandle) -> None:
        """A press on a drag handle: nothing moves until the pointer leaves the row.

        The handle takes the mouse, not the sidebar (:class:`DragHandle` says why),
        and hands every move and the release back here.
        """
        self._drag, self._drag_source = state, source
        source.capture_mouse()

    def dragging(self, source: DragHandle) -> bool:
        """Is a press on ``source`` still held — its drag begun and not yet released?"""
        return self._drag is not None and source is self._drag_source

    def cancel_drag(self, source: DragHandle) -> None:
        """``source`` went away mid-drag, or its release was lost: close its drag and clear
        its marks; nothing moves."""
        if not self.dragging(source):
            return
        self._drag, self._drag_source = None, None
        source.dragged_row().remove_class("-dragging")
        self._mark_drop(None)

    def drag_over(self, source: DragHandle, event: events.MouseMove) -> None:
        drag = self._drag
        if drag is None or source is not self._drag_source:
            return
        if event.button == 0:
            # A move with NO button held: the release was lost — let go outside the
            # terminal, or dropped by the driver — and this is the first report that
            # says so (SelectionHost ends a pane's gesture by the same rule). The drag
            # ends here and snaps back: where the button came up is nowhere this list
            # saw, so nowhere is a place. Left held, the card stayed dimmed, the
            # drop mark stayed on, and the handle kept the mouse — the next press
            # anywhere was this handle's, and a click on another title opened this
            # one's project (review of #171, round 1).
            source.release_mouse()
            self.cancel_drag(source)
            return
        if not drag.started:
            if abs(event.screen_y - drag.origin_y) < 1:
                return
            drag.started = True
            source.dragged_row().add_class("-dragging")
        self._mark_drop(self._target_at(event.screen_x, event.screen_y))

    def end_drag(self, source: DragHandle) -> bool:
        """The release: drop onto what is under the pointer; ``True`` when it was a drag."""
        drag = self._drag
        source.release_mouse()
        if drag is None or source is not self._drag_source:
            return False
        self._drag, self._drag_source = None, None
        source.dragged_row().remove_class("-dragging")
        target = self._drop_target
        self._mark_drop(None)
        if not drag.started:
            return False
        if target is None:
            return True  # released where nothing is a place: snap back
        if drag.kind == "group":
            before = target.group.id if isinstance(target, GroupHeader) else None
            if before != drag.ident:  # dropped on itself: snap back
                self.post_message(DropGroup(drag.ident, before=before))
            return True
        if isinstance(target, GroupHeader):
            self.post_message(DropProject(drag.project_ids, scope=target.group.id, before=None))
        elif isinstance(target, ProjectCard):
            if target.project.id not in drag.project_ids:  # dropped on itself: snap back
                self.post_message(
                    DropProject(drag.project_ids, scope=target.scope, before=target.project.id)
                )
        else:
            # The list's own empty space below the last row: the top level's end (ungroup).
            self.post_message(DropProject(drag.project_ids, scope=None, before=None))
        return True

    def _target_at(self, x: int, y: int) -> Widget | None:
        """What a release here would drop onto, or ``None`` where it would snap back.

        A project drops onto a card (before it, in its scope) or a group header
        (into that group, last); a group, onto an unpinned group's header (before
        it). The list's own empty space below the last row is the end: the top
        level for a project, the last group for a group. Nothing else is a place
        — the main pane, the header, Settings and its status, the Pinned label, a
        pinned card (pin order is ``p``'s), a card for a group, the rows being
        dragged — and a release there changes nothing. Read as "below the list",
        a drag abandoned over the main pane ungrouped its project and moved it
        last, and one onto a pinned card took a pinned member out of its group
        (review of #171).
        """
        drag = self._drag
        try:
            widget, _ = self.screen.get_widget_at(x, y)
        except Exception:
            return None
        if drag is None:
            return None
        holder = self.query_one("#projects", VerticalScroll)
        if widget is holder:
            return holder
        for node in (widget, *widget.ancestors):
            if isinstance(node, GroupHeader):
                if drag.kind == "group" and (
                    node.group.pinned_at is not None or node.group.id == drag.ident
                ):
                    return None
                return node
            if isinstance(node, ProjectCard):
                if (
                    drag.kind == "group"
                    or node.project.pinned_at is not None
                    or node.project.id in drag.project_ids
                ):
                    return None
                return node
        return None

    def _mark_drop(self, target: Widget | None) -> None:
        if self._drop_target is target:
            return
        if self._drop_target is not None:
            self._drop_target.remove_class("-drop-before")
        self._drop_target = target
        if isinstance(target, ProjectCard | GroupHeader):
            target.add_class("-drop-before")

    def show_notice(self, text: str | None) -> None:
        """A one-line warning above the list (a stale frame, say); ``None`` clears it."""
        notice = self.query_one("#projects-notice", Static)
        notice.update(Text(text or "", style="bold yellow"))
        notice.display = bool(text)

    def show_aisquare(self, signed_in: bool | None) -> None:
        """The status line's AISquare dot: signed in, out, or ``None`` when it cannot be read."""
        self.aisquare = signed_in
        self._paint_status()

    def show_headroom(self, headroom: HeadroomSummary, *, every_needs_login: bool = False) -> None:
        """The status line's Claude dot, from the usage cache (``FleetApp.refresh_headroom``),
        and the agent rows' session use from the same read — never from the two-second tick."""
        self.headroom = headroom
        self.every_needs_login = every_needs_login
        self._paint_status()
        if headroom.session_by_slot != self.session_usage:
            self.session_usage = headroom.session_by_slot
            for row in self.query(AgentRow):
                row.paint()

    def _paint_status(self) -> None:
        line = status_line_text(
            self.aisquare, self.headroom, every_needs_login=self.every_needs_login
        )
        self.query_one(StatusLine).update(line)

    # --- selection ------------------------------------------------------------------

    def select(self, key: str | None) -> None:
        """Highlight the row for ``key`` (a project, an agent or ``settings``)."""
        self.selected_key = key
        self._apply_selection()

    def _apply_selection(self) -> None:
        for row in self.query(Activatable):
            row.set_class(row.selection_key == self.selected_key, "selected")
        self._show_active_menus()

    def _show_active_menus(self) -> None:
        """A card's ⋯ shows on hover, and while its name is selected or under the cursor."""
        for card in self.query(ProjectCard):
            try:
                title = card.query_one(ProjectTitle)
            except NoMatches:  # not composed yet: the next frame paints it
                continue
            card.set_class(title.has_class("selected") or title.has_class("cursor"), "-active")

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
        self._show_active_menus()

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

    def action_resize(self, delta: int | None) -> None:
        """Ask for the partition to move ``delta`` columns (``None``: reset); ``Panes`` answers."""
        self.post_message(ResizeSidebar(delta))

    def action_cursor_down(self) -> None:
        self._move_cursor(1)

    def action_cursor_up(self) -> None:
        self._move_cursor(-1)

    def action_activate(self) -> None:
        for row in self._rows():
            if row.selection_key == self._cursor_key:
                row.activate()
                return
