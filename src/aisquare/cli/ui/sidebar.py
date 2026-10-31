"""The left pane, two sections: Fleet ▸ projects (alternating background) ▸ agents, then ⚙ Settings.

docs/plans/fleet-tui.md §4.1. One ``ProjectCard`` per registered project, each
with a disclosure, its name (the nickname, else the basename — renamed in place),
chips (agents alive · 🔔 count), a ⋯ menu, and a dim subtitle — the path when two projects
share a name, and the fleet codename when it is not the one the name gives
(:func:`project_subtitle`); under it one ``AgentRow`` per fleet agent (role icon,
label, state chip, exit status). Above the projects, one *actions* block under the Fleet
header — *▶ Start captain* (the captain's own row while one is live) and *Spawn agent*, whose
dialog asks which project — and under the separator, one
row, *⚙ Settings* (the Settings hub: App settings, Accounts, Doctor), under it the
⬆ row while a newer aisquare is out (:func:`update_row_text`), and under that
one dim status line (:func:`status_line_text`): the AISquare sign-in and the
Claude accounts' headroom as two dots. A click on that line opens Accounts.

Two rules shape the code more than the layout does:

- **Rebuilds update in place.** The app re-reads the store every two seconds
  and calls ``show_projects`` with the whole frame. Cards and rows are keyed by
  project and agent id and mutated, not re-created, so the one highlight (the
  mouse's and the keyboard's), a collapsed card and the scroll position survive
  every tick, and nothing flickers. Only a project or agent that appeared or
  vanished mounts or unmounts a widget.
- **Rows post messages; the app decides.** ``AddProject``, ``ProjectSelected``,
  ``AgentSelected``, ``SpawnAgent``, ``SettingsSelected``, ``AccountsSelected``,
  ``UpdateSelected``, ``RenameProject``, ``RenameAgent`` and ``OpenProjectMenu`` are the whole
  contract between this pane and the shell (the Settings hub posts ``AppSettingsSelected``,
  ``AccountsSelected`` and ``DoctorSelected``). Nothing here opens a view or
  writes the store.

Every visible string is a ``rich.text.Text`` built with ``append`` — a project
called ``[archive]`` must reach the screen as ``[archive]`` (CONTRIBUTING: no
markup in data).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from rich.cells import cell_len
from rich.text import Text
from textual import events, on
from textual.app import ComposeResult, RenderResult
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
from aisquare.cli.ui.instances_chip import InstancesChip
from aisquare.cli.ui.terminal import InPlaceField
from aisquare.core import codenames
from aisquare.models import (
    FleetAgent,
    FleetAgentStatus,
    HeadroomSummary,
    ProjectGroup,
    ProjectInfo,
)
from aisquare.services import fleet as fleet_service
from aisquare.services.project import NICKNAME_MAX, display_name
from aisquare.services.project_groups import Arrangement, GroupEntry, arrange

if TYPE_CHECKING:
    from aisquare.cli.ui.attach import ReturnTo
    from aisquare.core.instances import Instance

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
    "paused": ("⏯", "cyan"),
    "unknown": ("·", "dim"),
}
CUSTOM_ROLE_ICON = "🤖"
"""The icon for a role the table above does not know (a `team bind` role, say)."""

ALIVE_STATES: frozenset[str] = fleet_service.PROCESS_STATES
"""States that count toward the card's "agents alive" chip — a limited agent is
alive and parked (#146), not gone. The fleet's own set, which Stop and ``--all`` act on:
a third copy of it could count as alive what they leave, or the reverse (review of #266,
round 3)."""
STOP_STATES: frozenset[str] = ALIVE_STATES | {"exited", "paused"}
"""Where **Stop** is offered — the agent view's button and the sidebar's ``x`` ask
this one constant, never a copy of it: a process to stop (``ALIVE_STATES``), or an
exited agent's dead window — `remain-on-exit` keeps it for the last screen, and Stop
on the 💤 row removes it, which takes the row off the listing (``fleet stop`` on an
ended row, #138). A ✗ lost row has neither: it is ``fleet reap``'s business. A ⏯ paused
agent's Stop ends its pause for good: its kept claims go back to the board, and
``fleet resume`` leaves it (``fleet stop`` of a paused agent)."""


class AddProject(Message):
    """The + beside Fleet."""


class CaptainRequested(Message):
    """*▶ Start captain* in the actions block: the captain's view, or a start when there is none
    (T4)."""


class ViewSelected(Message):
    """A view asked for by its row: a project's, an agent's, or the Settings hub.

    Posted by the row itself (a click, Enter, an arrow landing on it) or by the shell on the
    row's behalf (a restored selection, the ui receiver, the captain's insignia).
    """

    from_row = False
    """The row posted it, and the sidebar put its one highlight on that row as it did. The
    shell leaves the highlight alone then — and opens nothing once the highlight has moved
    on to another row, whose own message is queued behind this one: an answer landing late
    pulled the highlight back, and each row the arrows passed built its view, an agent's
    pane attach included."""

    browsing = False
    """An arrow landed on the row: its view opens as a click opens it, but the keyboard
    stays in the sidebar, so the next arrow moves on rather than typing into a pane."""


class ProjectSelected(ViewSelected):
    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        super().__init__()


class AgentSelected(ViewSelected):
    def __init__(self, project_id: str, agent_id: str) -> None:
        self.project_id = project_id
        self.agent_id = agent_id
        super().__init__()


class SpawnAgent(Message):
    """*Spawn agent*, or the receiver's ``open_spawn``: the Spawn dialog.

    ``project_id`` is the project the dialog opens on — the sidebar's selection, which the
    dialog's Project field can change — or ``None``: the open view's project, else the first
    listed (the shell's to say: the sidebar does not know the view).
    """

    def __init__(self, project_id: str | None = None) -> None:
        self.project_id = project_id
        super().__init__()


class StopAgent(Message):
    """Stop this agent, please: the agent view's button, or ``x`` on the selected row.

    Both controls ask the SHELL rather than calling the service, because the
    shell is what holds the :class:`ProjectInfo` a stop needs (``on_spawn_agent``
    resolves a project the same way) and what owns the dialog.
    """

    def __init__(self, project_id: str, agent_id: str) -> None:
        self.project_id = project_id
        self.agent_id = agent_id
        super().__init__()


class DoctorSelected(Message):
    def __init__(self, project_id: str | None) -> None:
        self.project_id = project_id
        super().__init__()


class AccountsSelected(Message):
    """The Accounts page: the AISquare sign-in and the Claude Code accounts.

    ``return_to`` is where to go back once a sign-in started there lands: Welcome's
    *Sign in* asks to come back to Welcome (#286). Every other way in carries none."""

    def __init__(self, return_to: ReturnTo | None = None) -> None:
        self.return_to = return_to
        super().__init__()


class UsageStatsSelected(Message):
    """The Usage & stats page: what this machine's Claude Code sessions spent, by range."""


class SettingsSelected(ViewSelected):
    """*⚙ Settings*: the Settings hub, with its links to App settings, Accounts and Doctor."""


class AppSettingsSelected(Message):
    """The hub's *App settings*: the machine-wide settings page — on ``tab`` when given (the
    hub's *Remote control* and *Captain* open it on theirs), else on the tab it shows."""

    def __init__(self, tab: str | None = None) -> None:
        self.tab = tab
        super().__init__()


class UpdateSelected(Message):
    """The ⬆ row: the dialog that updates aisquare to the release it offers, or says how."""


class RenameProject(Message):
    """A card's name field was saved: call ``project_id`` ``typed`` (blank: its folder's name)."""

    def __init__(self, project_id: str, typed: str) -> None:
        self.project_id = project_id
        self.typed = typed
        super().__init__()


class RenameAgent(Message):
    """An agent row's name field was saved: rename ``agent_id``, labelled ``label``, to the label
    ``typed`` becomes (``fleet relabel``)."""

    def __init__(self, project_id: str, agent_id: str, label: str, typed: str) -> None:
        self.project_id = project_id
        self.agent_id = agent_id
        self.label = label
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


def codename_aside(project: ProjectInfo) -> str | None:
    """The project's fleet codename when it is not the one its name gives
    (:func:`codenames.codename_from_text` of :func:`project_name`), else ``None``.

    A manager or a captain names a project by its codename (``--project earnest-hawk``), and
    one never renamed has a generated one (``earnest-hawk``) its card does not otherwise
    say; so does one set with ``fleet rename``, or one suffixed because another project
    held the name's (``aisquare-cli-2``). One its name gives is said by the name itself."""
    codename = project.codename
    if codename is None or codename == codenames.codename_from_text(project_name(project)):
        return None
    return codename


def project_subtitle(project: ProjectInfo, names: Counter[str], *, always: bool) -> str | None:
    """The dim line under a card's name: the project's directory (:func:`short_path`) when
    ``always`` — ``[ui] always_show_project_path`` — or when another listed project shares
    its name (``names`` counts the listed projects by :func:`project_name`); then
    `` · <codename>`` when its codename is not the one its name gives
    (:func:`codename_aside`); ``None``, no line, when neither is said."""
    path = short_path(project.root) if always or names[project_name(project)] > 1 else None
    aside = codename_aside(project)
    if aside is None:
        return path
    return f"{path} · {aside}" if path else f"· {aside}"


def project_title_tooltip(project: ProjectInfo) -> str:
    """The card name's tooltip: its fleet codename, whether the card says it or not, with the
    tmux session it names, then the gestures."""
    gestures = "double-click or F2 to rename · right-click or . for the menu"
    codename = project.codename
    if codename is None:
        return f"no fleet codename yet (the first agent gives it one) · {gestures}"
    return (
        f"fleet codename {codename} (tmux {fleet_service.session_name(codename)}, "
        f"--project {codename}) · {gestures}"
    )


def ordered_agents(statuses: Iterable[FleetAgentStatus]) -> list[FleetAgentStatus]:
    """Manager first, then by ``created_at`` (§4.1) — whatever order the service used."""
    return sorted(
        statuses, key=lambda s: (s.agent.role != "manager", s.agent.created_at, s.agent.label)
    )


LABEL_WIDTH = 13
"""The cells an agent row gives its label when there is room, so the chips line up on rows
that fit. A row that would not fit gives cells back from its label, the ``#N·NN%`` held
whole at the right: a wide chip (``🔔``, ``💤 exited(N)``) or ``100%`` sits further left."""
BADGE_KEEP = 5
"""The fewest cells a cut persona badge keeps — `` · s…``, one letter of the name; with
fewer left it is dropped rather than shown as a bare ``·``."""


def persona_badge(status: FleetAgentStatus) -> Text:
    """`` · skeptic``, dim: the persona the row was spawned with, or else the one its session
    runs as; empty for none. A sidebar row and the agent info line both end with it, and
    both drop it first, by :data:`BADGE_KEEP`."""
    agent = status.agent
    persona = agent.persona or (status.session.persona if status.session is not None else None)
    return Text(f" · {persona}", style="dim") if persona else Text()


def agent_row_text(
    status: FleetAgentStatus, percent: float | None = None, *, width: int | None = None
) -> Text:
    """``🧭 manager       ⏸ #1·42% · skeptic`` — icon, label, state chip, the account's slot
    and the session use on it, exit status when exited, persona.

    ``#<slot>`` is ``FleetAgent.account_slot`` and ``percent`` that account's five-hour
    window, percent USED (:func:`session_percent_text`); with no percent the slot alone,
    with no slot neither. An exited row shows ``💤 exited(N)`` instead: it no longer draws
    on the account, and at the default width the two did not fit beside each other.

    ``width`` is the row's content width once it has one. The label gives way first,
    ellipsised, so the chip and the ``#N·NN%`` after it are never what is cut.

    The persona badge is the row's (what the agent was spawned with) or, for an
    agent the row does not know one for, its session's (what it launched as) —
    docs/plans/spawn-personas.md §0 item 9. It comes last, and on a row too narrow for
    everything the owner's order holds (review of #267's merge, M-F1): the label's padding
    gives way first, then the badge is ellipsised, and dropped once fewer than
    :data:`BADGE_KEEP` cells are left for it; only then is the label cut. The row is built
    to its width, so its own ellipsis never reaches the chip or the ``#N·NN%`` while the
    width holds the icon, two cells of label and the tail; a row narrower than that (under
    16 cells, with ``🔔 #1·100%``) is cut at its end, figure included (review of #267's
    delta, m1).
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
    elif status.state == "paused":
        # Not exited: `fleet pause` stopped it, and `fleet resume` brings it back.
        tail.append(" paused", style="dim")
    elif agent.account_slot is not None:
        tail.append(f" #{agent.account_slot}" + ("·" if percent is not None else ""), style="dim")
        if percent is not None:
            tail.append_text(session_percent_text(percent))
    badge = persona_badge(status)
    label = Text(agent.label)
    room = LABEL_WIDTH if width is None else max(2, width - cell_len(icon) - 1 - tail.cell_len)
    if width is None:
        label.pad_right(LABEL_WIDTH - label.cell_len)
    elif label.cell_len <= room:
        left = room - label.cell_len  # the cells after the label's own, the badge's to take
        if badge.cell_len > left:
            if left >= BADGE_KEEP:
                badge.truncate(left, overflow="ellipsis")
            else:
                badge = Text()
        label.pad_right(min(LABEL_WIDTH, room - badge.cell_len) - label.cell_len)
    else:
        label.truncate(room, overflow="ellipsis")
        badge = Text()
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(icon)
    text.append_text(label)
    text.append(" ")
    text.append_text(tail)
    text.append_text(badge)
    return text


LABEL_FIELD_MAX = 80
"""What an agent row's name field holds: free text, which the label it becomes cuts to 24
(``fleet_service.label_from_text``)."""


def relabel_preview(typed: str, agent: FleetAgent, taken: Collection[str] = ()) -> Text:
    """``→ login-fix``: the label an agent row's field makes of ``typed``, shown under it while
    it is typed — what ``fleet relabel`` will call the agent. ``taken`` is the labels of the
    other live agents the card shows: one of them is suffixed, as the rename suffixes it
    (``login-fix-2``). Text that makes no label, or a reserved one, says why instead."""
    try:
        wanted = fleet_service.label_from_text(typed, agent.role)
    except fleet_service.FleetError:
        return Text("✗ needs 2 letters or digits", style="red")
    if wanted in (fleet_service.MANAGER_LABEL, fleet_service.CAPTAIN_LABEL):
        return Text(f"✗ {wanted} is reserved", style="red")
    if wanted == agent.label:
        return Text(f"→ {wanted} (as it is)", style="dim")
    picked = fleet_service.free_label(wanted, taken)
    text = Text("→ ", style="dim")
    text.append(picked, style="bold")
    if picked != wanted:
        text.append(f" ({wanted} is taken)", style="dim")
    return text


def project_title_text(project: ProjectInfo) -> Text:
    """``🗂 Auth rewrite`` — the name a person gave it (else the folder's), and nothing else.

    The codename is not on this line: a rename gives the project the codename its name
    gives, and one that is not is said under it (:func:`project_subtitle`) and in the
    title's tooltip. The chips are their own widget (:func:`project_chips_text`), so a long
    name is cut before them, not them.
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

UPDATE_KEY = "update"
"""The ⬆ row's key: the arrows highlight it and open nothing — it opens a dialog, not a
page, on Enter or a click, which leaves the highlight where it was."""

UPDATE_ICON = "\u2b06\ufe0e"
"""⬆ in its text presentation (VS15), as :data:`SETTINGS_ICON` is."""

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
    aisquare: bool | None,
    headroom: HeadroomSummary | None,
    *,
    every_needs_login: bool = False,
    width: int | None = None,
) -> Text:
    """``● aisquare  ● 3x claude 58%`` (the x a multiplication sign), under *⚙ Settings*.

    The AISquare dot is green signed in, red signed out, grey while unknown. The
    Claude dot is :func:`headroom_dot`; the count is how many accounts are usable;
    the figure is the fleet's share LEFT (``HeadroomSummary.percent_left``, #287), in
    the dot's colour, and absent until a reading; a ``⚠`` follows when an account
    needs a login. ``headroom`` is ``None`` until the first summary lands.

    ``width``, the cells the line has: the figure is left out where the line would not
    fit it, so the ellipsis never cuts the ``⚠`` (something to do) nor a number in two.
    In the 28 cells of the sidebar, ``2x claude 3% ⚠`` fits and ``2x claude 58% ⚠``
    does not (review of #357).
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
    warning = " ⚠" if headroom is not None and headroom.needs_login else ""
    if headroom is not None and headroom.percent_left is not None:
        # The figure the dot's band stands for (#287): percent LEFT, in the dot's colour.
        figure = f" {round(headroom.percent_left)}%"
        if width is None or text.cell_len + cell_len(figure + warning) <= width:
            text.append(figure, style=colour)
    if warning:
        text.append(warning, style="yellow")
    return text


def update_row_text(version: str, *, runnable: bool = True) -> Text:
    """``⬆ 0.8.3 · restart to update`` — the row under *⚙ Settings* while a newer release is
    out. An install asq does not update itself (Homebrew, pip) is told how instead, so the
    row says that: its dialog shows the command and no restart."""
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(f"{UPDATE_ICON} {version}", style=f"bold {GREEN}")
    text.append(" · restart to update" if runnable else " · how to update", style="dim")
    return text


# --- rows -------------------------------------------------------------------------


class Activatable(Static):
    """A sidebar line the user can click or press Enter on.

    ``selection_key`` names what it stands for (``project:<id>``,
    ``agent:<id>``, ``settings``) so the sidebar can re-apply the highlight
    after a rebuild without holding widget references.

    The sidebar has ONE highlight (``.selected``), and the mouse and the keyboard move the
    same one. A row that stands for a view (its message a :class:`ViewSelected`) takes it
    when clicked or entered, and the arrows open it as they land on it. The others act —
    ``+`` onboards, *Spawn agent*, *Start captain* and ⬆ open a dialog, a group header
    folds: the arrows only highlight them, and Enter (or a click) acts. Passing over one
    never pops a dialog.
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

        A row that opens a view takes the sidebar's highlight here, before the shell has
        read its message (:attr:`ViewSelected.from_row`): an arrow pressed in between goes
        on from this row, not from the one highlighted before the click.
        """
        self._post(self.message(), browsing=False)

    @property
    def opens_view(self) -> bool:
        """Whether the row stands for a view (its message a :class:`ViewSelected`): the arrows
        open it as they land on it, and its message follows whenever it takes the highlight."""
        return isinstance(self.message(), ViewSelected)

    def arrive(self) -> None:
        """An arrow landed here: open the row's view, as a click would, the keyboard staying
        in the sidebar. A row that acts rather than shows does nothing more than take the
        highlight the sidebar has put on it."""
        message = self.message()
        if isinstance(message, ViewSelected):
            self._post(message, browsing=True)

    def _post(self, message: Message, *, browsing: bool) -> None:
        if isinstance(message, ViewSelected):
            sidebar = self._sidebar()
            if sidebar is not None:
                sidebar.select(self.selection_key)
                message.from_row = True
            message.browsing = browsing
        self.post_message(message.set_sender(self))

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
        # The arrows highlight it like any row; once onboarding opens, no row is.
        self.selection_key = "add"

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
        self.tooltip = project_title_tooltip(project)

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
    """⋯ — the card's menu: Rename, Pin / Unpin, Open settings, Start fresh…, Remove from fleet"""

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


class AgentLabelInput(InPlaceField, Input):
    """An agent row's name as a field: Enter submits, Esc posts ``Cancelled``, and leaving it —
    a press anywhere outside it too (:class:`InPlaceField`) — posts ``Left``: focus-out saves,
    as Enter does."""

    BINDINGS: ClassVar = [Binding("escape", "cancel_rename", "cancel", show=False)]

    class Cancelled(Message):
        """Esc in the field: close it and write nothing."""

    class Left(Message):
        """The field lost the focus while open."""

    def action_cancel_rename(self) -> None:
        self.post_message(self.Cancelled())

    def on_blur(self) -> None:
        self.post_message(self.Left())


RENAME_TIP = "double-click or F2 to rename"


class AgentRow(Activatable):
    """One fleet agent under its project. Click → Agent view; a second click → rename it.

    The agent is renamed in place (``fleet relabel``): click its row twice, or press F2 on it,
    and a field opens over the row, prefilled with the label, all selected, with the label
    it will become under it (:func:`relabel_preview`). Enter or leaving the field saves
    (``RenameAgent``), Esc closes it untouched, and a frame repaints the row beneath, never
    the field. The manager's and the captain's rows, and an ended agent's, offer none
    (``fleet_service.relabel_refusal``).
    """

    DEFAULT_CSS = """
    AgentRow { padding-left: 3; }
    AgentRow.-renaming { height: 2; }
    AgentRow AgentLabelInput { width: 1fr; height: 1; }
    AgentRow .agent-label-preview {
        width: 1fr; height: 1; text-wrap: nowrap; text-overflow: ellipsis;
    }
    """

    def __init__(self, status: FleetAgentStatus) -> None:
        super().__init__(agent_row_text(status), id=f"agent-row-{status.agent.id}")
        self.status = status
        self.selection_key = f"agent:{status.agent.id}"
        self._painted: tuple[FleetAgentStatus, float | None, int] | None = None
        self.prefill = ""
        """What the name field said when it opened: leaving it so is no change."""
        self._field: AgentLabelInput | None = None
        self._preview: Static | None = None
        self._tipped: FleetAgent | None = None
        """The row the tooltip was last decided for: a frame that changed nothing of it asks
        nothing again."""
        self._tip()

    def show(self, status: FleetAgentStatus) -> None:
        self.status = status
        self._tip()
        self.paint()
        if self._field is not None:
            # The frame may have changed what is taken (a spawn, another rename): the label
            # it will become is said again, the field itself untouched.
            self._paint_preview(self._field.value)

    def _tip(self) -> None:
        """The rename gesture, on a row that offers one: not the manager's, not the captain's,
        not an ended agent's. ``relabel_refusal``, the one rule a second click and F2 ask too,
        so the tooltip never offers what they refuse — asked again only when the row changed."""
        agent = self.status.agent
        if agent == self._tipped:
            return
        self._tipped = agent
        tip = RENAME_TIP if fleet_service.relabel_refusal(agent) is None else None
        if self.tooltip != tip:
            self.tooltip = tip

    # --- the label, renamed in place ---------------------------------------------------

    @property
    def renaming(self) -> bool:
        """Whether the label's field is open on this row."""
        return self._field is not None

    @property
    def field(self) -> AgentLabelInput | None:
        return self._field

    def open_rename(self, typed: str | None = None) -> None:
        """Open the field over the label, prefilled with it (or ``typed``), all selected.

        Already open, it only takes the focus back: what was typed is kept."""
        if self._field is None:
            self.prefill = self.status.agent.label
            value = self.prefill if typed is None else typed
            field = AgentLabelInput(
                value=value,
                max_length=LABEL_FIELD_MAX,
                compact=True,
                id=f"agent-label-{self.status.agent.id}",
            )
            field.tooltip = "any text · Enter saves, Esc cancels"
            preview = Static(classes="agent-label-preview")
            self._field, self._preview = field, preview
            self._paint_preview(value)
            self.add_class("-renaming")
            self.mount(field, preview)
        self.call_after_refresh(self._focus_field)

    def _focus_field(self) -> None:
        if self._field is not None and self._field.is_mounted:
            self._field.focus()
            self._field.select_all()

    def close_rename(self, *, refocus: bool) -> None:
        """Close the field and show the row; ``refocus`` hands the keys back to the sidebar
        (Enter, Esc) — not when the field closed because a click took the focus elsewhere."""
        field, preview = self._field, self._preview
        if field is None:
            return
        self._field = self._preview = None
        self.remove_class("-renaming")
        field.remove()
        if preview is not None:
            preview.remove()
        sidebar = self._sidebar()
        if refocus and sidebar is not None:
            sidebar.focus()

    def _taken(self) -> set[str]:
        """The labels of the other live agents its card shows."""
        card = next((node for node in self.ancestors if isinstance(node, ProjectCard)), None)
        statuses = card.statuses if card is not None else []
        return {
            status.agent.label
            for status in statuses
            if status.agent.id != self.status.agent.id and status.agent.ended_at is None
        }

    def _paint_preview(self, typed: str) -> None:
        if self._preview is not None:
            self._preview.update(relabel_preview(typed, self.status.agent, self._taken()))

    def _commit(self, typed: str, *, refocus: bool) -> None:
        """Save what the field says — or, when it makes the label it already has, just close it.

        The shell writes it (``RenameAgent``) and says what the agent is called now."""
        self.close_rename(refocus=refocus)
        agent = self.status.agent
        if typed.strip() == self.prefill.strip():
            return
        try:
            if fleet_service.label_from_text(typed, agent.role) == agent.label:
                return
        except fleet_service.FleetError:
            pass  # the shell's rename refuses it, says the rule and reopens the field
        self.post_message(RenameAgent(agent.project_id, agent.id, agent.label, typed))

    @on(Input.Changed, "AgentLabelInput")
    def _typed(self, event: Input.Changed) -> None:
        event.stop()
        self._paint_preview(event.value)

    @on(Input.Submitted, "AgentLabelInput")
    def _submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._commit(event.value, refocus=True)

    @on(AgentLabelInput.Left)
    def _left(self, event: AgentLabelInput.Left) -> None:
        """Focus-out saves, as Enter does — while the field is still open, and not when the
        terminal itself lost the focus (Textual blurs the focused widget then)."""
        event.stop()
        if self._field is not None and self.app.app_focus:
            self._commit(self._field.value, refocus=False)

    @on(AgentLabelInput.Cancelled)
    def _cancelled(self, event: AgentLabelInput.Cancelled) -> None:
        event.stop()
        self.close_rename(refocus=True)

    _opened = False
    """Whether the click before this one was a plain click that opened this row: only then is
    a second one at the same place a rename (``ProjectTitle._opened``'s rule)."""

    def on_click(self, event: events.Click) -> None:
        opened, self._opened = self._opened, False
        if self.renaming:
            # A click in its field (or on the line under it) is the field's: no open.
            event.stop()
            event.prevent_default()
            return
        sidebar = self._sidebar()
        if (
            sidebar is not None
            and event.button == 1
            and event.chain >= 2
            and opened
            and fleet_service.relabel_refusal(self.status.agent) is None
        ):
            # The first click of the two opened the agent; the second renames it.
            event.stop()
            event.prevent_default()
            sidebar.open_agent_rename(self.status.agent.id)
        elif event.button == 1:
            self._opened = True  # and ``Activatable.on_click`` opens it

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

    def render(self) -> RenderResult:
        """The row for the width it is drawn at. Built in ``__init__`` before it had one, the
        first frame was the whole-width text until ``on_resize`` repainted it, and the
        row's own ellipsis cut its end: the badge, or a ``#1·42%`` behind a long label
        (review of #267's delta, m2). Painted here, a frame is never drawn at no width;
        :meth:`paint` changes nothing when nothing it reads has changed."""
        self.paint()
        return super().render()

    def message(self) -> Message:
        return AgentSelected(self.status.agent.project_id, self.status.agent.id)


def group_header_text(
    entry: GroupEntry,
    agents: Mapping[str, list[FleetAgentStatus]],
    live: Mapping[str, int] | None = None,
) -> Text:
    """``▾ 📁 frontend  3 · 🔔1`` — the disclosure, the name, and the roll-up over its members.

    The roll-up sums ``ALIVE_STATES`` and the bells over EVERY member, the
    pinned ones included (they are listed under Pinned but they are still the
    group's), so a collapsed group still says something is running in it. A
    member folded away is not read on the tick (#83): it counts its live rows from
    ``live`` instead, and rings no bell until it is unfolded.
    """
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append("▸ " if entry.group.collapsed else "▾ ")
    text.append(f"📁 {entry.group.name}", style="bold")
    if entry.group.pinned_at is not None:
        text.append(" 📌", style="dim")
    members = [*entry.members, *entry.pinned_members]
    statuses = [s for member in members for s in agents.get(member.id, [])]
    alive = sum(1 for s in statuses if s.state in ALIVE_STATES)
    alive += sum((live or {}).get(member.id, 0) for member in members if member.id not in agents)
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

    def __init__(
        self,
        entry: GroupEntry,
        agents: Mapping[str, list[FleetAgentStatus]],
        live: Mapping[str, int] | None = None,
    ) -> None:
        super().__init__(group_header_text(entry, agents, live), id=f"group-{entry.group.id}")
        self.group: ProjectGroup = entry.group
        self.selection_key = f"group:{entry.group.id}"

    def show(
        self,
        entry: GroupEntry,
        agents: Mapping[str, list[FleetAgentStatus]],
        live: Mapping[str, int] | None = None,
    ) -> None:
        self.group = entry.group
        self.update(group_header_text(entry, agents, live))

    def message(self) -> Message:
        return ToggleCollapse(self.group.id)

    def drag_state(self, sidebar: Sidebar) -> DragState | None:
        # A pinned group keeps its pin order, as a pinned card does (``step_group``
        # has nothing to move either): dropped, it renumbered the unpinned groups.
        if self.group.pinned_at is not None:
            return None
        return DragState("group", self.group.id)


class SectionLabel(Static):
    """``📌 Pinned`` — a heading that is not a row (the arrows skip it)."""

    DEFAULT_CSS = """
    SectionLabel { height: 1; padding: 0 1; color: $text-muted; text-style: italic; }
    """


class ProjectCard(Vertical):
    """One project: header row, optional path subtitle, agent rows.

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
        self._painted: tuple[object, ...] | None = None
        """What the last mounted :meth:`show` painted, which the next skips when it is unchanged."""

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
        """Update in place: rows are keyed by agent id and reused.

        A card shown what it already shows costs nothing: with 50 listed projects every tick
        repainted every card, and the sidebar's paint took 15 ms of the UI thread (#83)."""
        self.project = project
        self.statuses = statuses
        self.subtitle = subtitle
        self.notice = notice
        if not self.is_mounted:
            return  # compose() will read the fields above
        painting = (project, statuses, subtitle, notice)
        if painting == self._painted:
            return
        try:
            title = self.query_one(ProjectTitle)
            title.update(project_title_text(project))
            title.tooltip = project_title_tooltip(project)
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
        self._painted = painting

    def show_subtitle(self, subtitle: str | None) -> None:
        """The dim line under the name alone, the rest of the card as it is."""
        self.subtitle = subtitle
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


class UpdateRow(Activatable):
    """The ⬆ row under *⚙ Settings* (:func:`update_row_text`), shown while a newer aisquare is
    out. Click or Enter → the update dialog; the arrows only highlight it."""

    DEFAULT_CSS = """
    UpdateRow { display: none; }
    UpdateRow.offered { display: block; }
    """

    def __init__(self) -> None:
        super().__init__(Text(""), id="update-row")
        self.selection_key = UPDATE_KEY
        self.version: str | None = None
        """The release offered; ``None`` while none is."""

    def message(self) -> Message:
        return UpdateSelected()

    def offer(self, version: str | None, *, runnable: bool = True) -> None:
        """Show the row for ``version``; ``None`` takes it away."""
        self.version = version
        if version is not None:
            self.update(update_row_text(version, runnable=runnable))
        self.set_class(version is not None, "offered")


CAPTAIN_READING = "reading the captain's board…"
"""The captain slot's line from the moment Settings switches the captain on until a read
of the fleet that asked for it answers: until then nothing says there is no captain."""

START_CAPTAIN_KEY = "start-captain"
SPAWN_AGENT_KEY = "spawn-agent"
"""The two actions' keys: the arrows highlight them and open nothing (each opens a dialog,
or a view that has its own row); Enter or a click acts, a click leaving the highlight be."""


ACTION_ICON_CELLS = 2
"""The cells an action's icon column takes: the fullwidth plus and the captain row's emoji are two
cells wide, so ``▶`` is padded to it and the three labels start in one column. The padding is
measured (``cell_len``), not assumed: ``▶`` (U+25B6) is East-Asian-ambiguous."""


def action_text(icon: str, label: str) -> Text:
    """``▶  Start captain`` / ``+ Spawn agent`` (the fullwidth plus): the icon padded to
    :data:`ACTION_ICON_CELLS`, a space, the label."""
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(icon + " " * (ACTION_ICON_CELLS - cell_len(icon)) + " " + label)
    return text


class ActionRow(Activatable):
    """One action at the top of the sidebar: a full-width, one-line button in the accent.

    The two actions share this look, so *Start captain* and *Spawn agent* read as a pair; the
    captain's own row, which takes the first one's place while it is live, is an ordinary
    :class:`AgentRow` on the same band (``ActionsSection``).
    """

    DEFAULT_CSS = """
    ActionRow { color: $accent; text-style: bold; background: $boost; }
    """


class StartCaptain(ActionRow):
    """*▶ Start captain*, in the captain slot while there is no live captain: the Spawn dialog
    with the captain chosen. It said "`aisquare captain` starts one", a command to go and
    type, and the owner did not find how to start one (#33)."""

    def __init__(self) -> None:
        super().__init__(action_text("▶", "Start captain"), id="captain-empty")
        self.selection_key = START_CAPTAIN_KEY
        self.tooltip = "No captain running — start one (aisquare captain)"

    def message(self) -> Message:
        return CaptainRequested()


class SpawnButton(ActionRow):
    """*Spawn agent*: the Spawn dialog, whose Project field says where the agent goes."""

    def __init__(self) -> None:
        # U+FF0B, the fullwidth plus of the plan's mockup (§4): visibly not the header's +.
        super().__init__(action_text("\uff0b", "Spawn agent"), id="spawn-agent")
        self.selection_key = SPAWN_AGENT_KEY
        self.tooltip = "Spawn an agent — you choose the project in the dialog"

    def message(self) -> Message:
        sidebar = self._sidebar()
        return SpawnAgent(sidebar.spawn_project_id() if sidebar is not None else None)


class CaptainSection(Vertical):
    """The captain's slot, first in the actions block: its row, or the line that starts one.

    The captain lives on the home board, which is never a project, so it never gets
    a :class:`ProjectCard`; its row is an ordinary :class:`AgentRow` — selecting it
    opens the same agent view, with Stop and Restart. While it is live the row takes the
    place of *▶ Start captain*; an exited or lost one keeps its row with the start under it,
    so it can be restarted from either.
    """

    DEFAULT_CSS = """
    CaptainSection { height: auto; }
    CaptainSection #captain-notice { height: auto; color: $text-muted; }
    """

    def __init__(self, *, notice: str | None = None, id: str | None = None) -> None:
        super().__init__(id=id)
        self._notice = notice
        """The line it is built with, as :meth:`show` would say it with no row."""

    def compose(self) -> ComposeResult:
        start = StartCaptain()
        start.display = self._notice is None
        yield start
        notice = Static(Text(self._notice or "", style="dim"), id="captain-notice")
        notice.display = bool(self._notice)
        yield notice

    def show(self, status: FleetAgentStatus | None, *, notice: str | None = None) -> None:
        """Show the captain's row and/or the line that starts one.

        ``notice`` says the read behind this frame failed; ``status`` is then the
        last frame's row, or ``None`` when there was none. It takes the place of
        the start line, which is true only when the fleet ANSWERED no live captain — the
        project cards' rule (``Sidebar.show_projects``), for the captain.
        """
        start = self.query_one("#captain-empty", StartCaptain)
        said = self.query_one("#captain-notice", Static)
        said.update(Text(notice or "", style="dim"))
        said.display = bool(notice)
        rows = list(self.query(AgentRow))
        keep = next(
            (row for row in rows if status and row.status.agent.id == status.agent.id), None
        )
        for row in rows:
            if row is not keep:
                row.remove()
        live = status is not None and status.state in ALIVE_STATES
        start.display = not live and not notice
        if status is None:
            return
        if keep is None:
            # First in the slot, above the start line it stands in for.
            self.mount(AgentRow(status), before=0)
        else:
            keep.show(status)


class ActionsSection(Vertical):
    """The block under the Fleet header: the captain's slot (while the experimental captain is
    on) and *Spawn agent*, stacked, full width, and a rule under them before the projects.

    The spawn action used to be a row under every project card; one row up here says the same
    and the dialog asks the project. The captain's heading and its ★ in the header are gone:
    *Start captain* names itself, and beside *Spawn agent* a "Captain" heading would have read
    as if that belonged to it.
    """

    DEFAULT_CSS = """
    ActionsSection { height: auto; padding: 0 1; border-bottom: solid $primary; }
    ActionsSection AgentRow { padding-left: 0; background: $boost; }
    """

    def __init__(self, *, captain: bool, id: str | None = None) -> None:
        super().__init__(id=id)
        self._captain = captain

    def compose(self) -> ComposeResult:
        if self._captain:
            yield CaptainSection(id="captain-section")
        yield SpawnButton()


class StatusLine(Static):
    """The dim line under *⚙ Settings* (:func:`status_line_text`). A click opens Accounts.

    Not a row: it is never focused, never reached by the arrows and never the
    highlighted row — the Accounts page it opens is one of Settings' pages, so
    *⚙ Settings* stays the highlighted row (the human: "clicking on that should
    directly take me to accounts page but still that line wont be highlighted").
    """

    DEFAULT_CSS = """
    StatusLine { height: 1; color: $text-muted; text-wrap: nowrap; text-overflow: ellipsis; }
    """

    ALLOW_SELECT = False

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.facts: tuple[bool | None, HeadroomSummary | None, bool] = (None, None, False)
        """What the line says: the AISquare sign-in, the headroom, ``every_needs_login``."""

    def show_facts(
        self, aisquare: bool | None, headroom: HeadroomSummary | None, every_needs_login: bool
    ) -> None:
        self.facts = (aisquare, headroom, every_needs_login)
        self.fit_line()

    def on_resize(self) -> None:
        self.fit_line()

    def fit_line(self) -> None:
        """The line for :attr:`facts`, fitted to the cells it has now (:func:`status_line_text`'s
        ``width``); before the first layout, whole."""
        aisquare, headroom, every_needs_login = self.facts
        self.update(
            status_line_text(
                aisquare,
                headroom,
                every_needs_login=every_needs_login,
                width=self.content_size.width or None,
            )
        )

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(AccountsSelected().set_sender(self))


class OthersSection(Vertical):
    """Everything that is not a project, under the separator: *⚙ Settings*, the ⬆ row while a
    release is offered, the status line, and the chip while more than one asq runs (#20)."""

    DEFAULT_CSS = """
    OthersSection { height: auto; border-top: solid $primary; padding: 0 1; }
    """

    def compose(self) -> ComposeResult:
        yield SettingsRow()
        yield UpdateRow()
        yield StatusLine(id="status-line")
        yield InstancesChip(id="instances-chip")


# --- the pane ---------------------------------------------------------------------


MOVE_KEYS = Binding.Group("move", compact=True)
"""The sidebar's ↓ and ↑, as the footer shows them: one entry."""


class Sidebar(Vertical):
    """Header and the scrolling project list; under the separator, *⚙ Settings* and its status.

    Focusable, because §4.3 puts focus either here or in a terminal pane:
    ↑/↓ move the one highlight over the rows and open the view of the row they
    land on, Enter activates the highlighted row, and the escape hatch from a
    pane lands here.
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
        # One footer entry, "↓↑ move": two took the room "R remote" needs at 80 columns (#33).
        Binding("down", "cursor_down", "next", group=MOVE_KEYS),
        Binding("up", "cursor_up", "previous", group=MOVE_KEYS),
        ("enter", "activate", "open"),
        # Out of the footer, which is full at 80 columns; the help screen (?) lists it.
        Binding("x", "stop_agent", "stop", show=False),
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
        # The highlighted card: its name in place, and its menu (⋯).
        Binding("f2", "rename", "rename", show=False),
        Binding("full_stop", "project_menu", "menu", show=False, key_display="."),
    ]

    can_focus = True

    def __init__(self, *, id: str | None = None, captain: bool = True) -> None:
        super().__init__(id=id)
        self.captain = captain
        """Whether the experimental captain is on: off, the actions block has no captain slot."""
        self.selected_key: str | None = None
        """The one highlighted row: ``project:<id>``, ``agent:<id>``, or ``settings`` while the
        hub or a page it opens (App settings, Accounts, Doctor) shows — or, put there by the
        arrows, a row that acts (``add``, ``group:<id>``, *Spawn agent*, *Start captain*, ⬆).
        A click, the arrows and the shell all move this one key; there is no second,
        keyboard-only highlight."""
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
        self._anchor_key: str | None = None
        """The last row highlighted: where the arrows go on from while none is. Clearing the
        highlight (the Welcome page, onboarding) leaves the place it was."""
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
        yield ActionsSection(captain=self.captain, id="actions")
        yield Static("", id="projects-notice")
        # can_focus=False: the rows are Statics, so a mouse-down on one focuses
        # the nearest focusable ancestor. Left focusable, this scroll would take
        # that focus and — once the list overflows — its own up/down bindings
        # would eat the arrows, so ↑/↓ scrolled the list instead of moving the
        # highlight and the sidebar never read as focused. Focus belongs to the
        # Sidebar (§4.3); the arrows scroll the list through ``scroll_visible``.
        with VerticalScroll(id="projects", can_focus=False):
            yield Static(
                Text("No projects yet — press + to onboard one.", style="dim"),
                id="projects-empty",
            )
        yield OthersSection(id="others")

    # --- data in -----------------------------------------------------------------

    def show_captain(self, status: FleetAgentStatus | None, *, notice: str | None = None) -> None:
        """The home's captain row, first in the actions block (``None``: there is none).

        ``notice``: why the read failed — the row is then the last frame's, kept.
        With the captain off there is no slot, and nothing to show.
        """
        if not self.captain:
            return
        self.query_one(CaptainSection).show(status, notice=notice)

    async def set_captain(self, on: bool) -> None:
        """The captain switched on or off while asq runs (Settings, #33): its slot mounted in the
        actions block, saying the captain is being read until a frame answers, or removed. As at
        a start with the switch so (:meth:`compose`), never a restart."""
        if on == self.captain:
            return
        self.captain = on
        if not on:
            for widget in list(self.query(CaptainSection)):
                await widget.remove()
            return
        # Built saying so: shown after the mount, the slot said *Start captain* for as
        # long as the mount took, to a frame and to a read in that window (review of #274, m7).
        section = CaptainSection(notice=CAPTAIN_READING, id="captain-section")
        await self.query_one(ActionsSection).mount(section, before=0)

    def show_projects(
        self,
        projects: list[ProjectInfo],
        agents: dict[str, list[FleetAgentStatus]],
        *,
        notices: Mapping[str, str] | None = None,
        groups: list[ProjectGroup] | None = None,
        live: Mapping[str, int] | None = None,
        always_show_paths: bool = False,
    ) -> None:
        """Paint one frame. Cards and rows are updated in place, keyed by id.

        ``notices`` says, per project, why its agent rows are missing (the
        cost of a fleet call that failed open); it is shown as a dim line
        where the rows would be, so an empty card is never mistaken for an
        idle fleet. ``always_show_paths`` is ``[ui] always_show_project_path``, which
        the app holds (:func:`project_subtitle`).

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
        # Its own children, not a query: a query walks every row of every card beneath them.
        shown = holder.children
        existing_cards = {c.project.id: c for c in shown if isinstance(c, ProjectCard)}
        existing_headers = {h.group.id: h for h in shown if isinstance(h, GroupHeader)}
        existing_labels = {label.id: label for label in shown if isinstance(label, SectionLabel)}
        names = Counter(project_name(p) for p in projects)
        ordered: list[Widget] = []
        stripe = 0

        def card_for(project: ProjectInfo, *, scope: str | None, hidden: bool) -> None:
            nonlocal stripe
            statuses = ordered_agents(agents.get(project.id, []))
            subtitle = project_subtitle(project, names, always=always_show_paths)
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
                header = GroupHeader(entry, agents, live)
            else:
                header.show(entry, agents, live)
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

    def show_paths(self, always: bool) -> None:
        """``[ui] always_show_project_path`` changed (App settings saved, reloaded or reset):
        every card's directory line follows now, from the frame on screen, rather than at the
        next frame's read."""
        if self.last_frame is None:
            return
        names = Counter(project_name(project) for project in self.last_frame[0])
        for card in self.query_one("#projects", VerticalScroll).query(ProjectCard):
            card.show_subtitle(project_subtitle(card.project, names, always=always))

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
        """``m``: shift+click's twin, on the highlighted card (or the highlighted row's card).

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
        """``("project", id)`` or ``("group", id)`` for the highlighted row."""
        key = self.selected_key
        if not key:
            return None
        kind, _, ident = key.partition(":")
        if kind == "project":
            return ("project", ident)
        if kind == "group":
            return ("group", ident)
        if kind == "agent":
            for card in self.query(ProjectCard):
                if any(s.agent.id == ident for s in card.statuses):
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
        self._close_agent_fields()
        card = self._card(project_id)
        if card is not None and card.display:
            card.open_rename(typed)

    def _agent_row(self, agent_id: str) -> AgentRow | None:
        return next((row for row in self.query(AgentRow) if row.status.agent.id == agent_id), None)

    def _close_agent_fields(self, but: AgentRow | None = None) -> None:
        """Close every agent row's open name field, unsaved, but ``but``'s: one at a time."""
        for row in self.query(AgentRow):
            if row is not but and row.renaming:
                row.close_rename(refocus=False)

    def open_agent_rename(self, agent_id: str, typed: str | None = None) -> str | None:
        """Open ``agent_id``'s name field on its row (``typed`` in it), every other field
        closing unsaved; or, for a row that offers no rename, why not
        (``fleet_service.relabel_refusal``), and nothing opens. ``None`` once it is open, or
        when the row is not on screen."""
        row = self._agent_row(agent_id)
        if row is None or not self._on_screen(row):
            return None
        refusal = fleet_service.relabel_refusal(row.status.agent)
        if refusal is not None:
            return refusal
        for card in self.query(ProjectCard):
            if card.renaming:
                card.close_rename(refocus=False)
        self._close_agent_fields(but=row)
        row.open_rename(typed)
        return None

    def open_menu(self, project_id: str) -> None:
        """Ask the shell for ``project_id``'s menu, under its card."""
        card = self._card(project_id)
        if card is not None:
            self.post_message(OpenProjectMenu(project_id, card.region))

    def action_rename(self) -> None:
        """F2: rename the highlighted row — an agent row's agent (``fleet relabel``), a card
        title's project (or the card of another highlighted row). On an agent's row that offers
        no rename (the manager's, the captain's, an ended agent's), say why."""
        kind, _, ident = (self.selected_key or "").partition(":")
        row = self._agent_row(ident) if kind == "agent" else None
        if row is not None and self._on_screen(row):
            refusal = self.open_agent_rename(ident)
            if refusal is not None:
                self.notify(f"not renamed: {refusal}", severity="warning", timeout=6, markup=False)
            return
        target = self._cursor_target()
        if target is not None and target[0] == "project":
            self.open_rename(target[1])

    def action_project_menu(self) -> None:
        """``.``: the menu of the highlighted card, as its ⋯ opens it."""
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

    def show_instances(self, live: Sequence[Instance], this_id: str | None) -> None:
        """The asqs live on this home (#20): the chip under the status line, while there are
        two or more; ``this_id`` is this asq's record."""
        self.query_one(InstancesChip).show_instances(live, this_id)

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

    def show_update(self, version: str | None, *, runnable: bool = True) -> None:
        """The ⬆ row for a newer release (``services.updates.check_for_release``); ``None``
        hides it."""
        self.query_one(UpdateRow).offer(version, runnable=runnable)

    def _paint_status(self) -> None:
        self.query_one(StatusLine).show_facts(self.aisquare, self.headroom, self.every_needs_login)

    # --- what the Spawn dialog asks of the sidebar ------------------------------------

    def ordered_projects(self) -> list[ProjectInfo]:
        """Every listed project once, in the order the sidebar shows them (pinned, groups,
        loose): the Spawn dialog's Project field offers them so. Empty before the first frame."""
        return self.arrangement.ordered_projects() if self.arrangement is not None else []

    def spawn_project_id(self) -> str | None:
        """The project *Spawn agent* opens on: the selected card's, or the selected agent's
        project; else ``None``, and the shell opens it on the open view's project (else the
        first listed). The captain's row, whose board is no listed project, and a group,
        Settings, *Spawn agent* itself (reached by the arrows) or nothing selected name none."""
        kind, _, ident = (self.selected_key or "").partition(":")
        listed = {project.id for project in self.ordered_projects()}
        if kind == "project":
            return ident if ident in listed else None
        if kind == "agent" and self.last_frame is not None:
            _projects, agents = self.last_frame
            for statuses in agents.values():
                for status in statuses:
                    if status.agent.id == ident and status.agent.project_id in listed:
                        return status.agent.project_id
        return None

    # --- selection ------------------------------------------------------------------

    def select(self, key: str | None) -> None:
        """Put the one highlight on the row for ``key``; ``None`` highlights no row.

        A key already highlighted costs nothing: an arrow selects its row, the row selects
        itself as it posts its message, and the shell's answer selects it again — three
        passes over every row (and a title lookup per card) for each key pressed.
        """
        if key == self.selected_key:
            return
        self.selected_key = key
        if key is not None:
            self._anchor_key = key
        self._apply_selection()

    def highlight_opens_a_view(self) -> bool:
        """Whether the highlighted row stands for a view, so that its own message — posted as it
        took the highlight — is on its way to open it (:attr:`Activatable.opens_view`)."""
        key = self.selected_key
        return key is not None and any(
            row.selection_key == key and row.opens_view for row in self.query(Activatable)
        )

    def _apply_selection(self) -> None:
        # Over EVERY row, the ones hidden in a folded card or group too: a row that kept
        # the class while hidden was a second highlight the moment it came back.
        for row in self.query(Activatable):
            row.set_class(row.selection_key == self.selected_key, "selected")
        self._show_active_menus()

    def _show_active_menus(self) -> None:
        """A card's ⋯ shows on hover, and while its name is the highlighted row."""
        for card in self.query(ProjectCard):
            try:
                title = card.query_one(ProjectTitle)
            except NoMatches:  # not composed yet: the next frame paints it
                continue
            card.set_class(title.has_class("selected"), "-active")

    # --- bell -----------------------------------------------------------------------

    def _ring_on_attention(self, statuses: Iterable[FleetAgentStatus]) -> None:
        """Terminal bell when an agent newly flips to needing the user.

        Mirrors ``cli.watch._ring_on_attention``: one bell per frame, only on a
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
        """The rows the arrows may land on: activatable and actually on screen."""
        return [
            row for row in self.query(Activatable) if row.selection_key and self._on_screen(row)
        ]

    def _on_screen(self, row: Activatable) -> bool:
        """Is ``row`` visible — including every container between it and here?

        A collapsed card hides its rows by hiding their ``#agents`` holder
        (``_paint_decorations``); the rows' own ``display`` stays True. Filtering
        on that alone walked the arrows onto invisible rows, so ↑/↓ lost the
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
        """↑/↓: the highlight goes one row on from the highlighted one, and that row opens.

        It goes on from THE highlight — the row a click put it on, or the shell — since
        there is no other: a keyboard cursor of its own stayed behind on the row the
        arrows last left, and the next ↓ jumped back to it from the row just clicked.
        A row that acts rather than shows (``+``, a group header, *Spawn agent*, *Start
        captain*, ⬆) is only highlighted (:meth:`Activatable.arrive`); Enter acts.
        """
        every = [row for row in self.query(Activatable) if row.selection_key]
        on_screen = [index for index, row in enumerate(every) if self._on_screen(row)]
        if not on_screen:
            return
        keys = [every[index].selection_key for index in on_screen]
        anchor = self.selected_key or self._anchor_key
        if anchor in keys:
            slot = max(0, min(len(on_screen) - 1, keys.index(anchor) + step))
        else:
            slot = self._nearest_on_screen(every, on_screen, anchor, step)
        row = every[on_screen[slot]]
        row.scroll_visible()
        if row.selection_key == self.selected_key:
            return  # at an end of the list: still there, and already open
        self.select(row.selection_key)
        row.arrive()

    @staticmethod
    def _nearest_on_screen(
        every: list[Activatable], on_screen: list[int], anchor: str | None, step: int
    ) -> int:
        """Which visible row takes the highlight when the anchor row is not one.

        A collapsed card takes its rows off screen with the highlight still on one
        of them: continuing from the top of the pane (index 0) is a jump the user
        did not ask for, so the highlight resumes at the nearest row that IS on
        screen, ahead of the old one when moving down and behind it when moving
        up. Nothing highlighted yet is the other case, and there the first row is
        right — there is no "above" to move to.
        """
        home = next((index for index, row in enumerate(every) if row.selection_key == anchor), None)
        if home is None:
            return 0
        ahead = [slot for slot, index in enumerate(on_screen) if index >= home]
        behind = [slot for slot, index in enumerate(on_screen) if index <= home]
        if step >= 0:
            return ahead[0] if ahead else behind[-1]
        return behind[-1] if behind else ahead[0]

    def put_cursor(self, key: str) -> bool:
        """Put the highlight on the row for ``key`` and scroll to it; whether it is on screen.

        For a caller that hands the keyboard to a row on the user's behalf (the ui
        receiver's ``focus_project``): ↑/↓ and Enter then go on from that row, as
        they would had the arrows brought the highlight there. Its view is the
        caller's to open. A row that is not on screen — a card inside a folded group —
        is not one to land on (the arrows ask ``_on_screen`` too): nothing moves here.
        """
        target = next(
            (
                row
                for row in self.query(Activatable)
                if row.selection_key == key and self._on_screen(row)
            ),
            None,
        )
        if target is None:
            return False
        self.select(key)
        target.scroll_visible()
        return True

    def action_resize(self, delta: int | None) -> None:
        """Ask for the partition to move ``delta`` columns (``None``: reset); ``Panes`` answers."""
        self.post_message(ResizeSidebar(delta))

    def action_cursor_down(self) -> None:
        self._move_cursor(1)

    def action_cursor_up(self) -> None:
        self._move_cursor(-1)

    def action_activate(self) -> None:
        """Enter: what a click on the highlighted row does — an agent's pane takes the keyboard,
        ``+`` onboards, *Spawn agent* opens its dialog, a group header folds."""
        for row in self._rows():
            if row.selection_key == self.selected_key:
                row.activate()
                return

    def action_stop_agent(self) -> None:
        """``x``: the Stop dialog for the SELECTED agent; any other selection does nothing.

        The binding is the sidebar's, not the row's — the rows are Statics with
        ``can_focus=False`` on purpose (see ``#projects`` above), so there is no
        focused row to bind to. It acts on the highlighted row: an agent's row the
        arrows or a click put it on, whose view is the one open. With the highlight
        on a row that opens no view (``+``, a group header, *Spawn agent*) while an
        agent's view still shows, it does nothing — the key stops what is lit.

        The agent names its own project, so a row selected under one card cannot
        be stopped against another's ``ProjectInfo``.

        It asks ``STOP_STATES`` too — the rule the agent view's button asks, the
        same constant, not a copy of it. A lost row is an ordinary selectable
        row, so without this the key offered exactly the Stop the button
        refuses; and ``stop`` does not decline such a row, it ends it outright
        with no ``/exit`` and no grace, which is ``fleet reap``'s outcome and
        outside this task (found by coder3a-1 before the PR opened). An exited
        row IS offered: its dead window is what Stop removes there (#138).
        """
        key = self.selected_key
        if key is None or not key.startswith("agent:") or self.last_frame is None:
            return
        agent_id = key.removeprefix("agent:")
        _projects, agents = self.last_frame
        for statuses in agents.values():
            for status in statuses:
                if status.agent.id == agent_id:
                    if status.state in STOP_STATES:
                        self.post_message(StopAgent(status.agent.project_id, agent_id))
                    return
