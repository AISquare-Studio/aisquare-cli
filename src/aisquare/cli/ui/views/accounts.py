"""The Accounts page: the AISquare sign-in on top, the Claude Code accounts under it.

docs/plans/claude-accounts.md. Two halves, one page:

- **AISquare** — the session ``aisquare login`` stores, read through
  ``services.iam``. *Sign in* runs the same device grant the terminal runs,
  as a native card rather than a pane: the one-time code and the link appear
  here, the browser is opened when one can reach the user, and a thread
  worker polls (``services.device_flow``) until the approval lands or *Cancel*
  is pressed. *Sign out* revokes and forgets, as ``aisquare logout`` does.
- **Claude Code** — a table (:data:`COLUMNS`), one aligned row per slot that
  ``services.claude_accounts`` knows: the default's ★, the slot, its name, who
  is signed in, the plan, the five-hour and seven-day windows (a bar and the
  percent USED), a status with *Log in* beside it, then when each window resets.
  *Add* makes a fresh slot and opens Claude Code's own login in a tmux window
  rendered right here (a ``TerminalPane``); the view polls the slot's
  directory and, the moment Claude Code has written a login into it, records
  the account, installs aisquare's hooks and closes the window. *Log in* on a row does the same for
  that slot when it has no login or its token is missing, expired or rejected
  (:func:`needs_login`) — then the window waits for a NEW login, not the stale
  one on disk. Nothing is typed for the user and nothing is written into
  Claude Code's files.
- **Arranging** (#145) — each row carries *Default*, *↑*/*↓* and
  *Disable*/*Enable*: the machine default a launch picks when nothing more
  specific says, the priority order, and whether the slot may be picked at
  all. The name — the alias ``--account``, the board and the row say, any text
  the operator likes — is renamed in place: double-click it (or Enter / F2 on
  the row) and it becomes a field prefilled with it; Enter or leaving the
  field saves (blank clears it), Esc closes it untouched, and a name the
  service refuses keeps the field open to correct. All of them write the
  registry through ``services.claude_accounts`` exactly as ``aisquare accounts
  default|move|disable|alias`` do; the page lists the slots in priority order.

**The view holds no state that matters** (fleet-tui plan §2). The shell hands
it a fresh ``AccountsOverview`` on every refresh; what the view owns is the
transient — a sign-in in flight, a usage fetch — and the usage numbers, which
are the one thing here that costs a request: they are read through the shared
five-minute cache (``cached_usage``), so an account is asked at most once every
:data:`USAGE_SECONDS` however often the page is opened, and only while it is on
screen; *↻ Refresh usage* asks every account now, and the line beside it says
how old the readings are — the last ones actually READ, and when a later try
failed, that too.

Every visible string is ``rich.text.Text`` built with ``append``: an email,
a path and a server's reason are DATA (CONTRIBUTING: no markup in data).
"""

from __future__ import annotations

import contextlib
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, NamedTuple

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.timer import Timer
from textual.widgets import Button, Input, Static
from textual.worker import Worker, WorkerState

from aisquare.cli.common import format_reset, local_time
from aisquare.cli.ui.terminal import InPlaceField, TerminalPane
from aisquare.cli.ui.ticks import while_running
from aisquare.core import browser, paths
from aisquare.core import claude_accounts as core
from aisquare.core.config import FleetSettings
from aisquare.core.store import store_session
from aisquare.core.tmux import TmuxError, TmuxServer
from aisquare.models import (
    AccountsOverview,
    ClaudeAccount,
    ClaudeAccountStatus,
    ClaudeIdentity,
    ClaudeUsage,
    UsageTrend,
)
from aisquare.services import auth as auth_service
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import credits as credits_service
from aisquare.services import destinations, device_flow, iam
from aisquare.services import fleet as fleet_service
from aisquare.services.credits import WorkspaceCredits

USAGE_SECONDS = accounts_service.USAGE_MAX_AGE_SECONDS
"""How old a usage reading gets before the page asks again: the shared cache's five minutes."""
TICK_SECONDS = 60.0
"""How often the page looks while on screen: the "last refreshed" line, the credits, and
the usage through the cache, which asks an account only once its reading is
:data:`USAGE_SECONDS` old. A tick of :data:`USAGE_SECONDS` against a cache of the same
age drifts: it lands a moment before the reading turns five minutes old, is served it,
and asks five minutes later — ten between readings."""
LOGIN_POLL_SECONDS = 1.0
"""How often a sign-in window's directory is checked for a landed login."""

USAGE_WORKER = "accounts-usage"
CREDITS_WORKER = "workspace-credits"
SIGN_IN_WORKER = "aisquare-sign-in"
SIGN_OUT_WORKER = "aisquare-sign-out"
COMPLETE_WORKER = "claude-complete-sign-in"
LOGIN_OPEN_WORKER = "claude-sign-in-open"
"""Reads the login a *Log in* is opened over (``login_fingerprint``) before its window opens:
on macOS a Keychain read, a subprocess, and never one on the UI thread."""
LOGIN_POLL_WORKER = "claude-sign-in-poll"
"""A sign-in window's poll: whether the login landed (``sign_in_landed``), off the UI thread."""
ABANDON_WORKER = "claude-abandon-sign-in"
"""Settles a fresh slot whose sign-in was cancelled: kept and wired if its login landed after
all (asked afresh), else discarded (``settle_sign_in``)."""
REMOVE_WORKER = "claude-remove"
ARRANGE_WORKER = "claude-arrange"
RENAME_WORKER = "claude-rename"

_BAR_CELLS = 5
_WARN_AT = 50.0
_HOT_AT = 80.0

SessionReader = Callable[[], "iam.Session | None"]


class _RenameWrite(NamedTuple):
    """A *Rename* write in flight: the slot, the text the field said when Enter sent it, and
    which opening of that field sent it (``AccountRow.openings``)."""

    slot: int
    submitted: str
    opening: int


class _Renamed(NamedTuple):
    """What a *Rename* write reports: the account as the service stored it, and the notice."""

    account: ClaudeAccount
    said: str


@dataclass
class _Saved:
    """A name this page saved that the frames may not show yet (``AccountsView._settle_saved``)."""

    alias: str | None
    outlived: bool = False
    """A new frame has disagreed once: the read in flight when the write landed."""


class SignOutOutcome(NamedTuple):
    """What *Sign out* did: the keys the CLI had minted (#142), then the session."""

    keys: destinations.MintedKeysForgotten
    session: auth_service.SignedOut


def _sign_out(session: iam.Session) -> SignOutOutcome:
    """*Sign out*, off the UI thread: ``aisquare logout``'s two steps, in its order.

    The ingest keys the CLI minted go with the sign-in that obtained them, as
    ``logout`` takes them, and first: their revoke takes the session's Bearer.
    This button revoked the session alone, so every minted key outlived it —
    in its file, bound, and live on the server (review of #172).
    """
    keys = destinations.forget_minted_keys(session)
    return SignOutOutcome(keys=keys, session=auth_service.sign_out(session))


class AccountsChanged(Message):
    """Something on the page changed what the sidebar summarises — an account added, a sign-in."""


class UsageRead(Message):
    """The page's usage read landed (a tick, or *↻ Refresh usage*): the sidebar's status line
    reads the same cache again now, at no request, rather than at its next minute."""


# --- pure helpers (unit-testable without a running app) ----------------------------------


def read_session() -> iam.Session | None:
    """The stored (or environment) AISquare session; ``None`` when absent or unreadable."""
    try:
        return iam.current_session()
    except iam.IamError:
        return None


def aisquare_status_text(session: iam.Session | None) -> Text:
    """``✓ Signed in as me@… · api.aisquare.studio · expires Dec 3, 2026``, or why not."""
    if session is None:
        return Text("Not signed in — sign in to link this machine to your AISquare account.")
    text = Text()
    text.append("✓ ", style="green")
    if session.source == "env":
        text.append(f"token from {iam.TOKEN_ENV_VAR}")
    else:
        text.append("Signed in as ")
        text.append(session.email or session.sub or "you", style="bold")
    text.append(f" · {_host(session.api_url)}", style="dim")
    if session.expires_at is not None:
        text.append(f" · expires {local_time(session.expires_at):%b %d, %Y}", style="dim")
    return text


def grant_text(grant: iam.DeviceAuthorization, *, opened: bool | None) -> Text:
    """The device-flow card while waiting: the code first, then the link, then what happened."""
    text = Text()
    text.append("Your one-time code: ")
    text.append(grant.user_code, style="bold")
    text.append("\nCheck that the browser shows the same code before you approve.\n")
    text.append(grant.verification_uri_complete, style="underline")
    if opened is True:
        text.append("\nOpening your browser… waiting for the approval.", style="dim")
    elif opened is False:
        text.append("\nNo browser opened here — visit the link on any device.", style="dim")
    else:
        text.append("\nWaiting for the approval in the browser.", style="dim")
    return text


def usage_bar(percent: float) -> Text:
    """``▮▮▯▯▯ 12%`` — five cells, coloured by how close the window is to its limit."""
    clamped = max(0.0, min(100.0, percent))
    # Half rounds UP (50 % is three cells, 90 % is five): a bar that under-reads
    # near the limit is the one reading that matters, and Python's round() would
    # give 4.5 → 4.
    filled = math.floor(clamped / 100 * _BAR_CELLS + 0.5)
    style = "green" if clamped < _WARN_AT else "yellow" if clamped < _HOT_AT else "bold red"
    text = Text()
    text.append("▮" * filled + "▯" * (_BAR_CELLS - filled), style=style)
    text.append(f" {clamped:.0f}%", style=style)
    return text


def credits_text(readings: list[WorkspaceCredits], *, now: datetime | None = None) -> Text:
    """One line per destination workspace (#143): ``acme [low]  run today ▮▮▮▮▯ 76% · resets …``.

    The same bars as the Claude rows below — used, not remaining, so the two
    halves of the page read alike — and ``unlimited`` where the API says ``-1``.
    The server's band follows the name when it is not ``ok``: FIRST, because
    the line does not wrap and the page's width cuts what comes last — as a
    suffix it was the part a 140-column terminal never showed (review of
    #173, round 1).
    """
    text = Text(no_wrap=True, overflow="ellipsis")
    for index, reading in enumerate(readings):
        if index:
            text.append("\n")
        text.append(reading.workspace_name, style="bold")
        if reading.state and reading.state != "ok":
            tone = "bold red" if reading.state == "exhausted" else "yellow"
            text.append(f" [{reading.state}]", style=tone)
        text.append("  ")
        if not reading.available:
            text.append(f"credits: {reading.reason or 'unavailable'}", style="dim")
            continue
        spans: list[Text] = []
        for pool in ("run", "build"):
            for span, word in (("daily", "today"), ("monthly", "month")):
                window = reading.window(pool, span)
                if window is None:
                    continue
                # The label is dim, the piece is not: a style handed to
                # ``Text(...)`` covers everything appended after it, and the
                # bar would draw faded beside the Claude rows' bars.
                piece = Text()
                piece.append(f"{pool} {word} ", style="dim")
                percent = window.percent
                if percent is None:  # the API's -1 only; a zero allowance is a full bar
                    piece.append("unlimited", style="dim")
                else:
                    piece.append_text(usage_bar(percent))
                    piece.append(_resets(window.resets_at, now=now), style="dim")
                spans.append(piece)
        if spans:
            text.append_text(Text("  ").join(spans))
        else:
            text.append("no pools reported", style="dim")
    return text


def _read_credits(session: iam.Session) -> list[WorkspaceCredits]:
    """Off the UI thread: every destination workspace of this session's host, one reading each.

    Distinct by workspace — two projects pointed at the same workspace share a
    balance and a request. A forgotten project's destination is read too, as
    ``doctor --live`` reads it: a launch in that root still traces into its
    workspace. Fails open: a store that cannot be read shows no credits line,
    and the rest of the page is untouched.
    """
    if not paths.db_path().exists():
        return []
    try:
        with store_session() as store:
            destinations = store.project_destinations()
    except Exception:
        return []
    readings: list[WorkspaceCredits] = []
    seen: set[int] = set()
    for destination in destinations:
        if destination.workspace_id in seen:
            continue
        reading = credits_service.for_destination(session, destination)
        if reading is None:
            continue  # another host than the session's: not this session's to ask
        seen.add(destination.workspace_id)
        readings.append(reading)
    return readings


def _resets(when: datetime | None, *, now: datetime | None = None) -> str:
    """`` · resets in 3h 10m (18:00)`` — ``cli.common.format_reset``, the one formatter (#152)."""
    return "" if when is None else f" · resets {format_reset(when, now=now)}"


DEFAULT_BADGE = "★"
"""Marks the machine default (#145) — the account a launch picks when nothing more specific says."""


class Column(NamedTuple):
    """One column of the Claude accounts table: its key, its header and its width in cells."""

    key: str
    title: str
    width: int


COLUMNS: tuple[Column, ...] = (
    Column("default", "", 1),
    Column("slot", "#", 2),
    Column("name", "name", 16),
    Column("email", "signed in as", 24),
    Column("plan", "plan", 7),
    Column("session", "session used", 12),
    Column("week", "week used", 10),
    Column("status", "status", 20),
    Column("session-resets", "session resets", 21),
    Column("week-resets", "week resets", 21),
)
"""The table's data columns, left to right; the header and every row are cut from them.

Fixed widths, so a column starts at the same x on every row whatever its values:
a value too long for its cell ends in ``…`` and never wraps (the whole text is the
cell's tooltip). The resets columns fit :func:`format_reset`'s longest answer,
``in 6d 12h (Tue 02:00)``; the usage columns fit ``▮▮▮▮▮ 100%``.

The status comes before the resets, and *Log in* right after it (:data:`LAYOUT`): what
needs doing reads, and can be done, without scrolling sideways at 160 columns with the
sidebar at its default width. When each window resets is the detail that can wait.
"""

LOGIN_COLUMN = Column("login", "", 8)
"""*Log in*'s column, beside the status that says why it is there."""

LAYOUT: tuple[Column, ...] = (
    *(column for column in COLUMNS if not column.key.endswith("-resets")),
    LOGIN_COLUMN,
    *(column for column in COLUMNS if column.key.endswith("-resets")),
)
"""Every column of a line as laid out — :data:`COLUMNS` with *Log in* after the status —
then the arranging buttons (:data:`_ACTIONS`)."""


def needs_login(status: ClaudeAccountStatus, usage: ClaudeUsage | None) -> bool:
    """Whether the row offers *Log in*: no login, no token, an expired one, or one refused (401).

    An expired or rejected token still counts as signed in (``signed_in`` is "a login is
    recorded"), so the old *Sign in* button never showed for the two states a person most
    often has to fix by hand.
    """
    if not status.signed_in or status.token_state == "expired":
        return True
    return usage is not None and usage.needs_login


def _window_cells(
    usage: ClaudeUsage | None, window: str, *, now: datetime | None
) -> tuple[Text, Text]:
    """One window's two cells (``session`` or ``week``): its bar and percent USED, and its reset.

    ``…`` while a reading is owed, ``—`` when there is none to show.
    """
    if usage is None:
        return Text("…", style="dim"), Text()
    percent: float | None = getattr(usage, f"{window}_percent")
    if not usage.available or percent is None:
        return Text("—", style="dim"), Text()
    resets_at: datetime | None = getattr(usage, f"{window}_resets_at")
    return usage_bar(percent), Text(format_reset(resets_at, now=now), style="dim")


def account_cells(
    status: ClaudeAccountStatus,
    usage: ClaudeUsage | None,
    trend: UsageTrend | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Text]:
    """One slot's cells, keyed like :data:`COLUMNS`: ``★``, ``2``, ``work``, ``me@…``, ``max 5x``,
    ``▮▯▯▯▯ 12%``, ``in 3h 10m (15:29)``, ``▮▮▯▯▯ 40%``, ``in 6d 12h (Tue 02:00)``, status.

    The name is the alias when there is one (``core.label``), so the row reads the way
    the operator named it. The status says the one thing to know, worst first: no
    login, a missing, expired or rejected token, why the usage cannot be read, a
    reading still owed, or where the session window is heading at the current pace
    (#146). ``usage`` is ``None`` while a reading is owed; a slot with no login shows
    no bars at all.
    """
    account = status.account
    cells = {column.key: Text() for column in COLUMNS}
    if account.is_default:
        cells["default"] = Text(DEFAULT_BADGE, style="bold green")
    cells["slot"] = Text(str(account.slot), style="bold", justify="right")
    cells["name"] = Text(status.label, style="cyan")
    notes: list[Text] = []
    if status.identity is None:
        notes.append(Text("not signed in", style="yellow"))
    else:
        cells["email"] = Text(status.identity.email)
        if status.subscription:
            cells["plan"] = Text(status.subscription, style="dim")
        # No bars for a login whose token is gone, whatever an older reading said.
        reading = usage if status.signed_in else ClaudeUsage(available=False)
        for window in ("session", "week"):
            cells[window], cells[f"{window}-resets"] = _window_cells(reading, window, now=now)
        if not status.signed_in:
            notes.append(Text("⚠ token missing", style="yellow"))
        elif status.token_state == "expired":
            notes.append(Text("⚠ token expired", style="yellow"))
        elif usage is not None and usage.needs_login:
            notes.append(Text("⚠ token rejected", style="yellow"))
        elif usage is None:
            notes.append(Text("reading usage…", style="dim"))
        elif not usage.available:
            notes.append(Text(f"usage: {usage.reason or 'unavailable'}", style="dim"))
        else:
            pace = accounts_service.describe_trend(trend)
            if pace:
                notes.append(Text(pace, style="dim italic"))  # a projection, so dim
    if account.disabled:
        notes.append(Text("disabled", style="dim italic"))
    cells["status"] = Text(" · ").join(notes)
    for text in cells.values():
        text.no_wrap = True
        text.overflow = "ellipsis"
    return cells


def claude_title_text(overview: AccountsOverview) -> Text:
    claude = overview.claude
    text = Text()
    if not claude.installed:
        text.append("Claude Code is not installed", style="bold red")
        text.append(f"\n  install it: {core.INSTALL_COMMAND}", style="dim")
        text.append(f"\n  or: {core.INSTALL_ALTERNATIVE}", style="dim")
        return text
    text.append("Claude Code", style="bold")
    if claude.version:
        text.append(f" {claude.version}")
    if claude.binary:
        text.append(f" · {claude.binary}", style="dim")
    return text


def _host(url: str) -> str:
    return url.split("://", 1)[-1].rstrip("/")


# --- transient state -------------------------------------------------------------------------


@dataclass
class _ClaudeLogin:
    """A Claude Code sign-in window that is open right now."""

    account: ClaudeAccount
    pane_id: str
    server: TmuxServer
    fresh: bool
    """True when *Add* created the slot for this sign-in — an abandoned one is discarded."""
    since: str | None = None
    """The stored login as the window opened (``login_fingerprint``), for a log-in over an
    expired or rejected token: the window waits for a different one, not that one."""
    poll_failed: bool = False
    """A poll of this window raised, and the page has said so: once, as each tick fails the
    same way (:meth:`AccountsView._login_polled`)."""


def _hooks_did_not_install(error: object) -> str:
    """What the page says of a slot signed in whose hooks did not install: after its sign-in
    (``complete_sign_in``), or after a cancel that found its login landed (``settle_sign_in``)."""
    return (
        f"⚠ signed in, but aisquare's hooks did not install: {error} — "
        "run: aisquare agents connect claude-code --config-dir <the slot's directory>"
    )


# --- widgets -----------------------------------------------------------------------------------


def _column_css() -> str:
    """Every column's width, for the header and the rows alike: one rule per column."""
    return "\n".join(
        f"AccountsTable .col-{column.key} {{ width: {column.width}; }}" for column in LAYOUT
    )


_ACTIONS: tuple[tuple[str, str, int], ...] = (
    ("default", "Default", 9),
    ("up", "↑", 3),
    ("down", "↓", 3),
    ("toggle", "Disable", 9),
    ("remove", "Remove", 8),
)
"""The arranging buttons at the end of a line, left to right: id stem, label and width.
*Log in* is not one of them: it has its column beside the status (:data:`LAYOUT`)."""


class AccountsTable(Vertical):
    """The Claude accounts table: a header line, then one :class:`AccountRow` per slot.

    The whole table scrolls sideways as one when the page is narrower than it, so the
    header stays over its columns and the action buttons stay reachable at any width
    (a focused button is scrolled into view).
    """

    DEFAULT_CSS = """
    AccountsTable { height: auto; overflow-x: auto; overflow-y: hidden; }
    AccountsTable #claude-rows { width: auto; height: auto; }
    AccountsTable .cell { height: 1; margin: 0 1 0 0; text-wrap: nowrap; text-overflow: ellipsis; }
    AccountsTable .account-actions { width: auto; height: 1; }
    """ + _column_css()


class AccountsHeader(Horizontal):
    """The table's header line, cut from :data:`COLUMNS` like every row."""

    DEFAULT_CSS = """
    AccountsHeader { width: auto; height: 1; text-style: bold; color: $text-muted; }
    """

    def compose(self) -> ComposeResult:
        for column in LAYOUT:
            yield Static(Text(column.title, no_wrap=True), classes=f"cell col-{column.key}")


class AliasInput(InPlaceField, Input):
    """The name cell's field: Enter submits (``Input.Submitted``), Esc posts ``Cancelled``,
    and leaving it — a press anywhere outside it too (:class:`InPlaceField`) — posts
    ``Left``: focus-out saves, as Enter does."""

    BINDINGS: ClassVar = [Binding("escape", "cancel_rename", "cancel", show=False)]

    class Cancelled(Message):
        """Esc in the field: close it and write nothing."""

        def __init__(self, field: AliasInput) -> None:
            super().__init__()
            self.field = field

    class Left(Message):
        """The field lost the focus while open."""

        def __init__(self, field: AliasInput) -> None:
            super().__init__()
            self.field = field

    def action_cancel_rename(self) -> None:
        self.post_message(self.Cancelled(self))

    def on_blur(self) -> None:
        self.post_message(self.Left(self))


class NameCell(Static):
    """The name, where a double click renames the slot in place."""

    ALLOW_SELECT = False

    def on_click(self, event: events.Click) -> None:
        if event.button == 1 and event.chain >= 2:
            event.prevent_default()  # Textual's own multi-click would select the text
            event.stop()
            row = next((node for node in self.ancestors if isinstance(node, AccountRow)), None)
            if row is not None:
                row.post_message(AccountRow.RenameRequested(row.slot))


class AccountRow(Horizontal, can_focus=True):
    """One slot as a table row: its cells (:data:`LAYOUT`), then its arranging buttons.

    *Log in*, beside the status, shows when the slot has no login or its token is
    missing, expired or rejected (:func:`needs_login`); *Default* makes it the
    machine default (not on the default's own row); *↑*/*↓* move it in the
    priority order (the end stops are disabled); *Disable*/*Enable* take it out of
    or back into automatic selection; *Remove* shows when the CLI owns the
    directory. A button that does not apply keeps its place, so the buttons stay
    in columns too. Every
    button is a command (``aisquare accounts
    default|move|disable|enable|run|remove``), and the row shows what the shell's
    next frame says rather than guessing.

    The name is renamed in place: double-click it, or press Enter or F2 on the
    row, and it becomes a field prefilled with the name. Enter or leaving the
    field saves (``aisquare accounts alias``; blank clears it back to the
    built-in name), Esc closes it untouched, and a name the service refuses
    keeps the field open to correct. A frame never touches what is being typed.
    """

    DEFAULT_CSS = """
    AccountRow { width: auto; height: 1; }
    AccountRow:focus { background: $boost; }
    AccountRow:focus-within { background: $boost; }
    AccountRow .col-name { layout: horizontal; }
    AccountRow NameCell { width: 1fr; height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    AccountRow AliasInput { width: 1fr; height: 1; }
    AccountRow Button { height: 1; min-width: 1; margin: 0 1 0 0; }
    """

    BINDINGS: ClassVar = [
        Binding("enter", "rename", "rename", show=False),
        Binding("f2", "rename", "rename", show=False),
    ]

    class RenameRequested(Message):
        """Double-click on the name, or Enter / F2 on the row: open its field."""

        def __init__(self, slot: int) -> None:
            super().__init__()
            self.slot = slot

    def __init__(self, status: ClaudeAccountStatus, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.status = status
        self.usage: ClaudeUsage | None = None
        self.trend: UsageTrend | None = None
        self.first = True
        self.last = True
        self.openings = 0
        """How often the name's field has opened: a write closes only the opening it came from."""
        self.prefill = ""
        """What the field said when it opened: leaving it so is no change, and writes nothing."""

    @property
    def slot(self) -> int:
        return self.status.account.slot

    def compose(self) -> ComposeResult:
        for column in LAYOUT:
            classes = f"cell col-{column.key}"
            if column is LOGIN_COLUMN:
                yield Button(
                    "Log in",
                    id=f"account-login-{self.slot}",
                    classes=classes,
                    variant="primary",
                    compact=True,
                )
                continue
            if column.key != "name":
                yield Static(classes=classes, id=f"account-cell-{column.key}-{self.slot}")
                continue
            with Horizontal(classes=classes):
                name = NameCell(id=f"account-name-{self.slot}")
                name.tooltip = "double-click (or Enter / F2) to rename"
                yield name
                field = AliasInput(
                    placeholder=core.ALIAS_HINT,
                    id=f"account-alias-{self.slot}",
                    classes="account-alias",
                    max_length=core.ALIAS_MAX,
                    compact=True,
                )
                field.tooltip = f"{core.ALIAS_HINT} · Enter saves, empty clears, Esc cancels"
                field.display = False
                yield field
        with Horizontal(classes="account-actions"):
            for stem, label, width in _ACTIONS:
                button = Button(label, id=f"account-{stem}-{self.slot}", compact=True)
                button.styles.width = width
                yield button

    def on_mount(self) -> None:
        self._paint()

    def action_rename(self) -> None:
        self.post_message(self.RenameRequested(self.slot))

    @property
    def alias_field(self) -> AliasInput:
        return self.query_one(AliasInput)

    @property
    def renaming(self) -> bool:
        """Whether the name's field is open on this row."""
        return self.alias_field.display

    def open_rename(self, on_its_way: str | None = None) -> None:
        """Open the field over the name, prefilled with it, focused.

        ``on_its_way`` is the name a write still in flight is saving, and it
        wins: the frame's name predates that write, and a field reopened
        before it lands said the old name — an Enter taken as "confirm" then
        undid the name just saved (review of #258, round 3). Already open, it
        only takes the focus back: what was typed is kept.
        """
        field = self.alias_field
        if not field.display:
            self.openings += 1
            if on_its_way is None:
                field.value = self.status.label
            else:  # a blank on its way clears the name: the built-in one is what it will say
                field.value = on_its_way.strip() or core.label(
                    self.status.account.model_copy(update={"alias": None})
                )
            self.prefill = field.value
            self.query_one(NameCell).display = False
            field.display = True
        field.focus()

    def unchanged(self, typed: str) -> bool:
        """Whether ``typed`` leaves the name as the field found it: nothing to write."""
        name = typed.strip()
        return name == self.prefill.strip() or (not name and self.status.account.alias is None)

    def close_rename(self) -> None:
        """Close the field; if it held the focus, the row gets it back.

        Only then: a write can finish after the user has gone to the sidebar or
        another page, and pulling the focus back to this row took it from
        wherever they had moved it (review of #258).
        """
        field = self.alias_field
        had_focus = field.has_focus
        field.display = False
        field.value = ""
        self.query_one(NameCell).display = True
        if had_focus:
            self.focus()

    def show(
        self,
        status: ClaudeAccountStatus,
        usage: ClaudeUsage | None,
        *,
        first: bool | None = None,
        last: bool | None = None,
        trend: UsageTrend | None = None,
    ) -> None:
        self.status = status
        self.usage = usage
        self.trend = trend
        if first is not None:
            self.first = first
        if last is not None:
            self.last = last
        if self.is_mounted:
            self._paint()

    def cell(self, key: str) -> Static:
        """The cell of column ``key`` (``name`` is the label the field covers while open)."""
        if key == "name":
            return self.query_one(NameCell)
        return self.query_one(f"#account-cell-{key}-{self.slot}", Static)

    def _paint(self) -> None:
        account = self.status.account
        cells = account_cells(self.status, self.usage, self.trend)
        for column in COLUMNS:
            cell = self.cell(column.key)
            cell.update(cells[column.key])
            if column.key in ("name", "email", "status"):
                # The whole value, for the cell that cut it short with an ellipsis.
                cell.tooltip = self._cell_tooltip(column.key, cells[column.key])
        login = self.query_one(f"#account-login-{self.slot}", Button)
        login.tooltip = "log in again: Claude Code opens below; type /login if it does not ask"
        self._offer(login, needs_login(self.status, self.usage))
        self._offer(self.query_one(f"#account-default-{self.slot}", Button), not account.is_default)
        self.query_one(f"#account-up-{self.slot}", Button).disabled = self.first
        self.query_one(f"#account-down-{self.slot}", Button).disabled = self.last
        toggle = self.query_one(f"#account-toggle-{self.slot}", Button)
        toggle.label = "Enable" if account.disabled else "Disable"
        toggle.variant = "warning" if account.disabled else "default"
        self._offer(self.query_one(f"#account-remove-{self.slot}", Button), account.managed)

    def _cell_tooltip(self, key: str, text: Text) -> str | None:
        if key == "name":
            return f"{text.plain} — double-click (or Enter / F2) to rename"
        if key == "status" and self.usage is not None and self.usage.reason:
            return f"{text.plain}\n{self.usage.reason}" if text.plain else self.usage.reason
        return text.plain or None

    @staticmethod
    def _offer(button: Button, offered: bool) -> None:
        """Show ``button`` or leave its place empty — never collapse it, so columns stay put."""
        button.visible = offered
        button.disabled = not offered


class AccountsView(Vertical):
    """The page. ``show`` paints an overview; the buttons run the flows in the module docstring."""

    DEFAULT_CSS = """
    AccountsView { padding: 1 2; }
    AccountsView #accounts-body { height: 1fr; scrollbar-gutter: stable; }
    AccountsView .section-title { height: auto; margin-top: 1; }
    AccountsView #aisquare-status { height: auto; margin-top: 1; }
    AccountsView #aisquare-code { height: auto; margin-top: 1; }
    AccountsView .actions { height: auto; margin-top: 1; }
    AccountsView .actions Button { margin: 0 1 0 0; }
    AccountsView #claude-table { margin-top: 1; }
    AccountsView #usage-bar Button { margin: 0 2 0 0; }
    AccountsView #usage-age { width: auto; height: 1; color: $text-muted; }
    AccountsView #accounts-notice { height: auto; margin-top: 1; }
    AccountsView #login-box { height: 1fr; min-height: 12; border-top: solid $primary; }
    AccountsView #login-header { height: auto; padding: 0 1; }
    AccountsView #login-pane { height: 1fr; }
    AccountsView #login-actions { height: auto; padding: 0 1; }
    """

    BINDINGS: ClassVar = []

    def __init__(
        self,
        *,
        escape_key: str | None = None,
        server: TmuxServer | None = None,
        session_reader: SessionReader = read_session,
        sign_in_cwd: Path | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.escape_key = escape_key or fleet_service.settings().escape_key
        self._server = server
        self._read_session = session_reader
        self.sign_in_cwd = sign_in_cwd
        self.overview: AccountsOverview | None = None
        self.session: iam.Session | None = None
        self._credits_for: iam.Session | None = None
        """The session the credits line was drawn (or emptied) for."""
        self.usage: dict[int, ClaudeUsage] = {}
        self.trends: dict[int, UsageTrend | None] = {}
        self._read_at: dict[int, datetime] = {}
        """When each slot's usage was last READ: a failed try replaces its reading, not this."""
        self._asked: set[int] = set()
        """The signed-in slots a usage reading has been started for (``show``)."""
        self.login: _ClaudeLogin | None = None
        self._login_opening: ClaudeAccount | None = None
        """The slot a *Log in* is reading the stored login of, before its window opens."""
        self._login_poll: Worker[Any] | None = None
        """The sign-in window's poll in flight, or the last one."""
        self._abandoning: str | None = None
        """Why the fresh slot being settled (:data:`ABANDON_WORKER`) was given up: kept beside
        the worker, as one that raises hands back no result to carry it."""
        self._login_timer: Timer | None = None
        self._usage_timer: Timer | None = None
        self._usage_worker: Worker[Any] | None = None
        """The usage read in flight, or the last one."""
        self._usage_owed = False
        """A read was asked for while one ran: read once more after it."""
        self._usage_forced = False
        """The read in flight is *↻ Refresh usage*'s, which asks every slot: "refreshing…"."""
        self._cancel_sign_in: threading.Event | None = None
        self._arranging = threading.Lock()  # one registry write at a time (arrange_accounts)
        self._renames: dict[Worker[Any], _RenameWrite] = {}
        """*Rename* writes sent and not yet reported, by worker (``rename_account``)."""
        self._saved: dict[int, _Saved] = {}
        """Names *Rename* saved, by slot, until the frames have caught up (``_settle_saved``)."""
        self._on_screen = False

    # --- layout ------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="accounts-body"):
            yield Static(Text("AISquare", style="bold"), classes="section-title")
            yield Static(aisquare_status_text(None), id="aisquare-status")
            yield Static("", id="aisquare-credits")
            yield Static("", id="aisquare-code")
            with Horizontal(classes="actions", id="aisquare-actions"):
                yield Button("Sign in", id="aisquare-sign-in", variant="primary")
                yield Button("Sign out", id="aisquare-sign-out")
                yield Button("Cancel", id="aisquare-cancel", variant="warning")
            yield Static(
                Text("Claude Code", style="bold"), id="claude-title", classes="section-title"
            )
            with Horizontal(classes="actions", id="usage-bar"):
                refresh = Button("↻ Refresh usage", id="usage-refresh", compact=True)
                refresh.tooltip = "ask every account now — otherwise at most once every 5 minutes"
                yield refresh
                yield Static(Text("last refreshed never", style="dim"), id="usage-age")
            with AccountsTable(id="claude-table"):
                yield AccountsHeader(id="claude-header")
                yield Vertical(id="claude-rows")
            with Horizontal(classes="actions", id="claude-actions"):
                yield Button("+ Add Claude account", id="claude-add", variant="primary")
            yield Static("", id="accounts-notice")
        with Vertical(id="login-box"):
            yield Static("", id="login-header")
            yield TerminalPane(None, escape_key=self.escape_key, id="login-pane")
            with Horizontal(id="login-actions"):
                yield Button("Cancel sign-in", id="login-cancel", variant="warning")

    def on_mount(self) -> None:
        self.query_one("#aisquare-code", Static).display = False
        self.query_one("#aisquare-cancel", Button).display = False
        self.query_one("#login-box", Vertical).display = False
        self._paint_aisquare()
        if self.overview is not None:
            self._paint_claude(self.overview)
        self._usage_timer = self.set_interval(TICK_SECONDS, self.refresh_readings)

    def on_show(self) -> None:
        """The page is opened: read it, and ask a Keychain that refused (locked, an unlock
        cancelled) once more, which no tick does by itself (``core.read_keychain``)."""
        self._on_screen = True
        core.forget_keychain_reads()
        self.refresh_readings()

    def on_hide(self) -> None:
        self._on_screen = False

    def on_unmount(self) -> None:
        """The page is leaving (``q``, usually): nothing transient may outlive it.

        Textual cancelling a thread worker does not stop its callable, so the
        device flow's own cancel flag is set — the wait returns within half a
        second and stores nothing. A Claude sign-in window is closed and its
        slot settled the way a poll would have: recorded if the login landed in
        the meantime, discarded if it was fresh and did not. No widget is
        touched here; they are being torn down under us. The one read made here
        rather than in a worker (on macOS a Keychain read): nothing is left on
        screen to keep answering, and a worker would not outlive the quit.
        """
        for timer in (self._login_timer, self._usage_timer):
            if timer is not None:
                timer.stop()
        if self._cancel_sign_in is not None:
            self._cancel_sign_in.set()
        self._settle_login_quietly()

    def _settle_login_quietly(self) -> None:
        login = self.login
        self.login = None
        if login is None:
            return
        with contextlib.suppress(TmuxError):
            login.server.kill_window(login.pane_id)
        with contextlib.suppress(Exception):
            if accounts_service.sign_in_landed(login.account, since=login.since) is not None:
                accounts_service.complete_sign_in(login.account)
            elif login.fresh:
                accounts_service.abandon_sign_in(login.account)

    def server(self, config: FleetSettings | None = None) -> TmuxServer:
        """The server given to the page, else the fleet's as ``config`` names it now."""
        return self._server if self._server is not None else fleet_service.server(config)

    # --- data in ----------------------------------------------------------------------------

    def show(self, overview: AccountsOverview) -> None:
        """A fresh frame from the shell: paint it, and re-read the AISquare session beside it.

        A session that changed since the last frame — a ``login`` or ``logout``
        in another terminal — re-reads the credits under the card at once
        rather than on the next minute tick: they are that session's.

        A frame that brings a signed-in slot the page has not asked about reads
        the usage too, when the page is on screen. Only ``on_show`` and the minute
        tick asked, so such a row said ``usage: …`` for up to a minute: the first
        frame, when the ``Show`` was handled before it (the shell hands the page
        its first frame after the switch to the page has been awaited), and a
        later frame with a slot just signed in, by ``+ Add`` here or in another
        terminal (review of #252). Keyed on the slots ASKED, not answered: this
        runs on every shell tick, and a reading that failed leaves its slots out
        of ``usage``, so keying on answers would ask again every tick. Off screen
        this does nothing, and ``on_show`` reads as before.
        """
        if overview is not self.overview:  # the shell re-hands its last frame on a click
            self._settle_saved(overview)
        self.overview = overview
        previous, self.session = self.session, self._read_session()
        if not self.is_mounted:
            return
        self._paint_aisquare()
        self._paint_claude(overview)
        if self.session != previous:
            self.refresh_credits()
        signed_in = {status.account.slot for status in overview.accounts if status.signed_in}
        self._asked &= signed_in  # a slot that left or signed out is asked again on its return
        if signed_in - self._asked:
            self.refresh_usage()

    def _env_token(self) -> bool:
        """Whether ``AISQUARE_TOKEN`` is what aisquare is using — not a session this page owns."""
        return self.session is not None and self.session.source == "env"

    def _paint_aisquare(self) -> None:
        busy = self._cancel_sign_in is not None
        self.query_one("#aisquare-status", Static).update(aisquare_status_text(self.session))
        signed_in = self.session is not None
        env_token = self._env_token()
        sign_in = self.query_one("#aisquare-sign-in", Button)
        sign_in.display = not busy
        sign_in.label = "Sign in again" if signed_in else "Sign in"
        # The same refusal `aisquare login` makes (`env_token_set`): a browser
        # sign-in would store a session the variable keeps overriding, and
        # retire the one on file for nothing.
        sign_in.disabled = env_token
        sign_out = self.query_one("#aisquare-sign-out", Button)
        sign_out.display = signed_in and not busy
        sign_out.disabled = env_token
        hint = f"the token comes from {iam.TOKEN_ENV_VAR}; unset it first" if env_token else None
        sign_in.tooltip = hint
        sign_out.tooltip = hint

    def _settle_saved(self, frame: AccountsOverview) -> None:
        """Weigh a new frame against the names *Rename* saved that the frames have not shown.

        A saved name is the service's answer (``set_alias`` returns the stored
        account), and the row says it from the moment the write lands: the row
        that kept the frame's older name prefilled *Rename* with it, and an
        Enter taken as "confirm" put the old name back (review of #258, round 4).

        One frame read BEFORE a write can still arrive AFTER it: the shell's
        read in flight when the write landed (``FleetApp.refresh_accounts``
        reads one at a time and owes one more). Every read after that one
        starts after the write. So a saved name stands over the first new frame
        that disagrees and yields to the next: by then the frame is the
        registry's word, another terminal's rename included. A frame that
        agrees, or that no longer has the slot, settles it at once.
        """
        aliases = {status.account.slot: status.account.alias for status in frame.accounts}
        for slot, saved in list(self._saved.items()):
            if slot not in aliases or aliases[slot] == saved.alias or saved.outlived:
                del self._saved[slot]
            else:
                saved.outlived = True

    def _as_saved(self, status: ClaudeAccountStatus) -> ClaudeAccountStatus:
        """``status`` as the row shows it: with the name *Rename* saved, while one stands."""
        saved = self._saved.get(status.account.slot)
        if saved is None or saved.alias == status.account.alias:
            return status
        account = status.account.model_copy(update={"alias": saved.alias})
        return status.model_copy(update={"account": account, "label": core.label(account)})

    def _paint_claude(self, overview: AccountsOverview) -> None:
        self.query_one("#claude-title", Static).update(claude_title_text(overview))
        self.query_one("#claude-add", Button).disabled = not overview.claude.installed
        holder = self.query_one("#claude-rows", Vertical)
        existing = {row.slot: row for row in holder.query(AccountRow)}
        count = len(overview.accounts)
        for index, frame_status in enumerate(overview.accounts):
            status = self._as_saved(frame_status)
            slot = status.account.slot
            row = existing.pop(slot, None)
            first, last = index == 0, index == count - 1
            if row is None:
                row = AccountRow(status, id=f"account-row-{slot}")
                row.usage = self.usage.get(slot)
                row.trend = self.trends.get(slot)
                row.first, row.last = first, last
                if index < len(holder.children):
                    holder.mount(row, before=index)
                else:
                    holder.mount(row)
            else:
                row.show(
                    status,
                    self.usage.get(slot),
                    first=first,
                    last=last,
                    trend=self.trends.get(slot),
                )
                if index < len(holder.children) and holder.children[index] is not row:
                    holder.move_child(row, before=index)
        for stale in existing.values():
            self.usage.pop(stale.slot, None)
            self.trends.pop(stale.slot, None)  # a re-used slot must not inherit a projection
            self._read_at.pop(stale.slot, None)
            stale.remove()

    def _name_of(self, account: ClaudeAccount) -> str:
        """What the page calls ``account``: its row's name (the alias, which a plain
        directory record does not carry), else the built-in one."""
        row = self.row_of(account.slot)
        return row.status.label if row is not None else core.label(account)

    def rows(self) -> list[AccountRow]:
        return list(self.query(AccountRow))

    def row_of(self, slot: int) -> AccountRow | None:
        return next((row for row in self.rows() if row.slot == slot), None)

    def _notice(self, text: str, tone: str = "dim") -> None:
        style = {"ok": "green", "warn": "yellow", "error": "bold red"}.get(tone, "dim")
        self.query_one("#accounts-notice", Static).update(Text(text, style=style))

    # --- usage (the one thing here that costs a request) ---------------------------------------

    @while_running
    def refresh_readings(self) -> None:
        """The page's minute tick (and its ``on_show``): the Claude slots' usage, through the
        cache, and the workspaces' credits — and the "last refreshed" line, which ages.

        Two refreshes side by side, not one riding the other's tail: each has
        its own reason to stop short (no signed-in Claude slot; no AISquare
        session), and the credits used to inherit the usage refresh's — a
        user signed in to AISquare with no Claude slot signed in never saw
        them (review of #173, round 1).
        """
        self.refresh_usage()
        self.refresh_credits()
        self._paint_usage_age()

    def refresh_usage(self, *, force: bool = False) -> None:
        """Read every signed-in slot's usage off the UI thread, if the page is on screen.

        Through the shared five-minute cache (``cached_usage_with_trends``): a
        slot is asked only when its newest reading is :data:`USAGE_SECONDS` old,
        so opening the page, its minute tick and a frame with a new slot cost no
        request while the readings are fresh. ``force`` is *↻ Refresh usage*: it
        asks every slot now, and takes over from a read already running. A read
        asked for while one runs is owed, and made once that one ends.

        Every signed-in slot, not only those with a readable token: the service
        answers an unreadable (a locked Keychain) or expired token with its reason and
        no request, and that reason is what the row must show instead of a
        ``usage: …`` that never resolves.
        """
        if not self._on_screen or self.overview is None:
            return
        accounts = [status.account for status in self.overview.accounts if status.signed_in]
        if not accounts:
            return
        if not force and self._usage_running():
            self._usage_owed = True
            return
        self._asked.update(account.slot for account in accounts)
        self._usage_forced = force
        # Each reading fetched is RECORDED (#146), and the trends are read from that
        # history in one store open: a store that cannot be written costs the trends;
        # the readings still paint.
        self._usage_worker = self.run_worker(
            lambda: accounts_service.cached_usage_with_trends(accounts, force=force),
            name=USAGE_WORKER,
            group=USAGE_WORKER,
            exclusive=True,
            thread=True,
            exit_on_error=False,
        )
        self._paint_usage_age()

    def _usage_running(self) -> bool:
        return self._usage_worker is not None and not self._usage_worker.is_finished

    @on(Button.Pressed, "#usage-refresh")
    def _refresh_usage_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.refresh_usage(force=True)

    def _paint_usage_age(self) -> None:
        """``last refreshed 3m ago`` — the OLDEST reading the usable rows hold that was
        actually read; ``never`` before the first. A usable row whose latest try read nothing
        (an unreachable endpoint, an HTTP error), or whose figure a later try failed to
        renew (``last_failed_at``: the cache keeps the figure on the row), adds ``· last try
        failed just now``, the newest such try: a failure is never passed off as a refresh,
        nor hidden by the figure before it. ``refreshing…`` — and *↻ Refresh usage* off —
        while a read that asks runs: the button's, or the first; a tick's read is the
        cache's, and the line stays as it is through it.

        A row that needs a login (signed out, its token expired or refused) or whose token
        this CLI cannot read (missing, a locked Keychain) counts in neither half
        (``usage_is_readable``, the CLI's rule too): its own status says so, and an account
        left expired would otherwise say "failed" every tick.
        """
        # A tick's read is the cache's: it asks nothing while the readings are fresh, and
        # said "refreshing…" (the button off) every minute for no request. Said for a read
        # that asks — *↻ Refresh usage*'s, or the first, with nothing read yet.
        running = self._usage_running()
        slots = [
            row.slot
            for row in self.rows()
            if accounts_service.usage_is_readable(row.status, self.usage.get(row.slot))
        ]
        read = [self._read_at[slot] for slot in slots if slot in self._read_at]
        failed = [
            when
            for slot in slots
            if (reading := self.usage.get(slot)) is not None
            and (when := reading.last_failed_at if reading.available else reading.fetched_at)
            is not None
        ]
        refreshing = running and (self._usage_forced or not read)
        self.query_one("#usage-refresh", Button).disabled = refreshing
        if refreshing:
            said = "refreshing…"
        else:
            age = accounts_service.describe_age(min(read)) if read else "never"
            said = f"last refreshed {age}"
            if failed:
                said += f" · last try failed {accounts_service.describe_age(max(failed))}"
        self.query_one("#usage-age", Static).update(Text(said, style="dim"))

    def refresh_credits(self) -> None:
        """The destination workspaces' credits (#143), off the UI thread, for this session.

        Called on the minute tick and whenever the session changes (a sign-in
        or sign-out here, or a frame that read a different one). A session
        other than the one the line was drawn for — none, or another sign-in —
        empties the line at once and cancels a reading still in flight for the
        previous one, on screen or not: the page keeps its line while hidden,
        and showing it again must not put the last session's bars under this
        session's card (review of #173, round 2). The new session's are read
        when the page is on screen.
        """
        session = self.session
        if session != self._credits_for:
            self.workers.cancel_group(self, CREDITS_WORKER)
            self.query_one("#aisquare-credits", Static).update("")
            self._credits_for = session
        if session is None or not self._on_screen:
            return
        self.run_worker(
            lambda: (session, _read_credits(session)),
            name=CREDITS_WORKER,
            group=CREDITS_WORKER,
            exclusive=True,
            thread=True,
            exit_on_error=False,
        )

    def _show_credits(self, session: iam.Session, readings: list[WorkspaceCredits]) -> None:
        if session != self.session:
            return  # read for a session that has since gone or changed; not this card's
        self.query_one("#aisquare-credits", Static).update(credits_text(readings))

    def _show_usage(self, fetched: dict[int, tuple[ClaudeUsage, UsageTrend | None]]) -> None:
        for slot, (usage, trend) in fetched.items():
            self.usage[slot] = usage
            self.trends[slot] = trend
            if usage.available and usage.fetched_at is not None:
                self._read_at[slot] = usage.fetched_at
        for row in self.rows():
            if row.slot in fetched:
                usage, trend = fetched[row.slot]
                row.show(row.status, usage, trend=trend)

    # --- AISquare: the device flow as a card ------------------------------------------------------

    @on(Button.Pressed, "#aisquare-sign-in")
    def _start_sign_in(self) -> None:
        if self._cancel_sign_in is not None:
            return
        if self._env_token():
            self._notice(
                f"{iam.TOKEN_ENV_VAR} is set, so aisquare is using that token. "
                "Unset it to sign in with the browser.",
                "warn",
            )
            return
        cancel = threading.Event()
        self._cancel_sign_in = cancel
        self._notice("")
        code = self.query_one("#aisquare-code", Static)
        code.update(Text("Contacting the identity provider…", style="dim"))
        code.display = True
        self.query_one("#aisquare-cancel", Button).display = True
        self._paint_aisquare()
        self.run_worker(
            lambda: self._device_flow(cancel),
            name=SIGN_IN_WORKER,
            group=SIGN_IN_WORKER,
            exclusive=True,
            thread=True,
            exit_on_error=False,
        )

    def _device_flow(self, cancel: threading.Event) -> iam.Session:
        """Runs in a thread: discovery, the grant, the browser, the wait, the stored session."""
        api_url = iam.resolve_api_url()
        endpoints = iam.discover(api_url)
        grant = iam.start_device_authorization(endpoints)
        self.app.call_from_thread(self._show_grant, grant, None)
        # The helper owns the decision: an explicit BROWSER wins over the
        # headless heuristics (an SSH session with a bridge browser configured),
        # exactly as the terminal sign-in lets it.
        opened = browser.open_url(grant.verification_uri_complete)
        self.app.call_from_thread(self._show_grant, grant, opened)
        token = device_flow.wait_for_token(endpoints, grant, cancelled=cancel.is_set)
        # The commit checks the flag once more after the userinfo request, the
        # last thing before the session is written: nothing is stored past a Cancel.
        return device_flow.commit_sign_in(api_url, endpoints, token, cancelled=cancel.is_set)

    def _show_grant(self, grant: iam.DeviceAuthorization, opened: bool | None) -> None:
        self.query_one("#aisquare-code", Static).update(grant_text(grant, opened=opened))

    @on(Button.Pressed, "#aisquare-cancel")
    def _cancel_aisquare_sign_in(self) -> None:
        if self._cancel_sign_in is not None:
            self._cancel_sign_in.set()

    def _sign_in_finished(self, worker: Worker[Any], state: WorkerState) -> None:
        self._cancel_sign_in = None
        self.query_one("#aisquare-code", Static).display = False
        self.query_one("#aisquare-cancel", Button).display = False
        if state is WorkerState.SUCCESS and isinstance(worker.result, iam.Session):
            self.session = worker.result
            who = self.session.email or self.session.sub or "you"
            if self.session.unrestricted:
                # The service warns on stderr, which Textual captures while it runs.
                self._notice(
                    f"✓ Signed in to AISquare as {who}, but {iam.unrestricted_warning()}", "warn"
                )
            else:
                self._notice(f"✓ Signed in to AISquare as {who}", "ok")
            self.post_message(AccountsChanged())
            self.refresh_credits()  # the new session's workspaces, not a minute from now
        elif state is WorkerState.ERROR:
            error = worker.error
            if isinstance(error, iam.IamError) and error.code == "cancelled":
                self._notice("Sign-in cancelled. Nothing was stored.")
            elif isinstance(error, iam.IamError):
                self._notice(f"✗ {error.message}", "error")
            else:
                self._notice(f"✗ sign-in failed: {type(error).__name__}: {error}", "error")
        self._paint_aisquare()

    @on(Button.Pressed, "#aisquare-sign-out")
    def _start_sign_out(self) -> None:
        session = self.session
        if session is None or session.source == "env":
            return
        self.query_one("#aisquare-sign-out", Button).disabled = True
        self.run_worker(
            lambda: _sign_out(session),
            name=SIGN_OUT_WORKER,
            group=SIGN_OUT_WORKER,
            exclusive=True,
            thread=True,
            exit_on_error=False,
        )

    def _sign_out_finished(self, worker: Worker[Any], state: WorkerState) -> None:
        self.session = self._read_session()
        if state is WorkerState.SUCCESS and isinstance(worker.result, SignOutOutcome):
            outcome = worker.result.session
            said = "✓ Signed out of AISquare" + (
                "" if outcome.revoked else " (locally — the server could not be reached to revoke)"
            )
            tone = "ok"
            if not outcome.restricted:
                # As for a sign-in: the service warns on stderr, which Textual captures.
                said, tone = f"{said}, but {iam.unrestricted_warning(signed_out=True)}", "warn"
            owed = worker.result.keys.revocations.owed
            if owed:
                said = f"{said}; {destinations.describe_owed(owed)} — {destinations.REVOKE_RETRY}"
                tone = "warn"
            self._notice(said, tone)
            self.post_message(AccountsChanged())
        elif state is WorkerState.ERROR:
            self._notice(f"✗ sign-out failed: {worker.error}", "error")
        self._paint_aisquare()
        self.refresh_credits()

    # --- Claude Code: a sign-in window, watched --------------------------------------------------

    @on(Button.Pressed, "#claude-add")
    def _add_claude_account(self) -> None:
        self.begin_claude_sign_in(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.startswith("account-login-"):
            event.stop()
            self.begin_claude_sign_in(int(button_id.rsplit("-", 1)[1]))
        elif button_id.startswith("account-remove-"):
            event.stop()
            self.remove_claude_account(int(button_id.rsplit("-", 1)[1]))
        elif button_id.startswith("account-default-"):
            event.stop()
            slot = int(button_id.rsplit("-", 1)[1])
            self.arrange_accounts(
                lambda: accounts_service.set_default(str(slot)),
                done=f"✓ slot {slot} is the machine default",
            )
        elif button_id.startswith("account-up-") or button_id.startswith("account-down-"):
            event.stop()
            slot = int(button_id.rsplit("-", 1)[1])
            direction: accounts_service.Direction = (
                "up" if button_id.startswith("account-up-") else "down"
            )
            self.arrange_accounts(
                lambda: accounts_service.move(str(slot), direction),
                done=f"✓ slot {slot} moved {direction} in the priority order",
            )
        elif button_id.startswith("account-toggle-"):
            event.stop()
            slot = int(button_id.rsplit("-", 1)[1])
            current = self.row_of(slot)
            disabling = current is None or not current.status.account.disabled
            outcome = "disabled — never picked automatically" if disabling else "enabled"
            self.arrange_accounts(
                lambda: accounts_service.set_disabled(str(slot), disabling),
                done=f"✓ slot {slot} {outcome}",
            )

    # --- arranging (#145): default, order, disabled ----------------------------------------------

    def arrange_accounts(self, change: Callable[[], object], *, done: str) -> None:
        """Run one registry write off the UI thread, then let the shell re-read the page.

        The write is a local SQLite statement, but the store's busy timeout is
        seconds, not milliseconds, and a wedged ``context.db`` must not freeze
        the UI — so it runs as a thread worker like *Remove* does. The row is
        NOT updated optimistically: the shell's next frame (``AccountsChanged``)
        is what the page shows, so what the operator sees is what was written.

        ``done`` travels WITH the work, as the worker's result: kept on the page,
        a second click overwrote it before the first worker reported — a thread
        worker ``exclusive`` cancels still finishes — and the notice named the
        wrong action (review of #205, fourth round).

        One write at a time, and every one reported. ``exclusive`` cancelled the
        earlier worker on paper only, since a thread cannot be stopped: two
        quick ▲ clicks ran two read-then-write ``move`` calls at once and one
        step was lost, and the cancelled worker's notice never showed (review of
        the #205 fold, round 1). The lock takes the writes in turn instead, and
        each worker reports its own ``done``.
        """

        def work() -> str:
            with self._arranging:
                change()
            return done

        self.run_worker(
            work,
            name=ARRANGE_WORKER,
            group=ARRANGE_WORKER,
            thread=True,
            exit_on_error=False,
        )

    def _arrange_finished(self, worker: Worker[Any], state: WorkerState) -> None:
        if state is WorkerState.SUCCESS and isinstance(worker.result, str):
            self._notice(worker.result, "ok")
        elif state is WorkerState.ERROR:
            self._notice(f"✗ {worker.error}", "error")
        self.post_message(AccountsChanged())

    # --- naming: the alias `aisquare accounts alias` sets ---------------------------------------

    @on(AccountRow.RenameRequested)
    def _rename_requested(self, event: AccountRow.RenameRequested) -> None:
        event.stop()
        self.open_rename(event.slot)

    def open_rename(self, slot: int) -> None:
        """Open the name's field on ``slot``'s row; another row's closes unsaved: one at a time."""
        for row in self.rows():
            if row.slot != slot and row.renaming:
                row.close_rename()
        target = self.row_of(slot)
        if target is not None:
            on_its_way = [write.submitted for write in self._renames.values() if write.slot == slot]
            target.open_rename(on_its_way[-1] if on_its_way else None)  # the latest sent

    @on(Input.Submitted, ".account-alias")
    def _rename_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.commit_rename(int((event.input.id or "").rsplit("-", 1)[1]), event.value)

    @on(AliasInput.Left)
    def _rename_left(self, event: AliasInput.Left) -> None:
        """Focus-out saves, as Enter does — once, and only while the field is still open.

        Not when the terminal itself lost the focus (Textual blurs the focused widget
        then and gives it back on return), and not for the text an Enter already sent:
        that write's own result closes the field.
        """
        event.stop()
        slot = int((event.field.id or "").rsplit("-", 1)[1])
        row = self.row_of(slot)
        if row is None or not row.renaming or not self.app.app_focus:
            return
        sent = [
            write.submitted
            for write in self._renames.values()
            if write.slot == slot and write.opening == row.openings
        ]
        if sent and sent[-1] == event.field.value:
            return
        self.commit_rename(slot, event.field.value)

    def commit_rename(self, slot: int, typed: str) -> None:
        """Save what the field says — or, when that is the name it opened with, just close it."""
        row = self.row_of(slot)
        if row is not None and row.unchanged(typed):
            row.close_rename()
            return
        self.rename_account(slot, typed)

    @on(AliasInput.Cancelled)
    def _rename_cancelled(self, event: AliasInput.Cancelled) -> None:
        event.stop()
        slot = int((event.field.id or "").rsplit("-", 1)[1])
        row = self.row_of(slot)
        if row is not None:
            row.close_rename()
        if any(write.slot == slot for write in self._renames.values()):
            # Esc closes the field; it cannot stop a write already sent, so it must not
            # say nothing changed — that write's own notice follows (review of #258).
            self._notice(
                f"slot {slot}: field closed — the name already sent is still being saved; "
                "its result follows"
            )
        else:
            self._notice(f"slot {slot}: rename cancelled — nothing changed")

    def rename_account(self, slot: int, typed: str) -> None:
        """Name ``slot`` ``typed`` (blank: unname it) off the UI thread, as ``accounts alias`` does.

        The service judges the name — :func:`core.normalise_alias`'s rules, and
        whether another slot has it — so nothing is checked here. A refusal is
        the worker's error: the notice says it and the field stays open with
        what was typed, to be corrected. The write takes the arranging lock, so
        it waits its turn behind a *Default* or *↑* still writing. On success
        the row says the name the service stored at once, and the page re-reads
        as an arrangement does; :meth:`_settle_saved` decides when the frames
        have the last word again.

        The write is kept in :attr:`_renames` until it reports, with what the
        field said when it was sent and which opening of the field sent it. So
        success closes the field only while it is that opening and still says
        that: text typed after Enter, while the write waited, is the next name,
        and closing on it threw it away unsaved; a second Enter saves it. A field
        closed and reopened meanwhile is the user's new edit, prefilled with the
        name on its way, and stays open. And Esc meanwhile does not claim
        nothing changed (review of #258).
        """
        alias = typed.strip() or None

        def work() -> _Renamed:
            with self._arranging:
                account = accounts_service.set_alias(str(slot), alias)
            if alias is None:
                return _Renamed(account, f"✓ slot {account.slot}: alias cleared")
            return _Renamed(account, f"✓ slot {account.slot} is now called {account.alias}")

        worker = self.run_worker(
            work,
            name=RENAME_WORKER,
            group=RENAME_WORKER,
            thread=True,
            exit_on_error=False,
        )
        # Before the worker can report: its state change is handled on this thread.
        row = self.row_of(slot)
        self._renames[worker] = _RenameWrite(slot, typed, row.openings if row is not None else 0)

    def _rename_finished(self, worker: Worker[Any], state: WorkerState) -> None:
        write = self._renames.pop(worker, None)
        if state is WorkerState.SUCCESS and isinstance(worker.result, _Renamed):
            stored = worker.result.account
            self._notice(worker.result.said, "ok")
            self._saved[stored.slot] = _Saved(stored.alias)
            if self.overview is not None:
                self._paint_claude(self.overview)  # the label and the next prefill say it now
            row = self.row_of(write.slot) if write is not None else None
            # Closed only if it is the opening that sent the write and still says what it
            # saved: anything typed since, or a field reopened since, is the next edit.
            sent_from_here = (
                write is not None
                and row is not None
                and row.openings == write.opening
                and row.alias_field.value == write.submitted
            )
            if row is not None and row.renaming and sent_from_here:
                row.close_rename()
        elif state is WorkerState.ERROR:
            self._notice(f"✗ {worker.error}", "error")
            row = self.row_of(write.slot) if write is not None else None
            # Refused, the field stays open to correct — while the user is still in it.
            # A name saved by leaving the field is not left open behind them, unfocused.
            if (
                write is not None
                and row is not None
                and row.renaming
                and row.openings == write.opening
                and not row.alias_field.has_focus
            ):
                row.close_rename()
        self.post_message(AccountsChanged())

    def begin_claude_sign_in(self, slot: int | None) -> None:
        """Open Claude Code on ``slot`` (a fresh slot when ``None``) in a pane, watched.

        *Add* and *Log in* both come here. A slot still holding a login — an
        expired or rejected token — is watched for a DIFFERENT login
        (``login_fingerprint``): the stale one is a signed-in directory, and the
        window closed at the first poll, before Claude Code had refreshed it. That
        login is read before the window opens (a Claude Code starting there may
        refresh the token at once, which is the new login), off the UI thread
        (:data:`LOGIN_OPEN_WORKER`): on macOS it is a Keychain read.
        """
        opening = self.login.account if self.login is not None else self._login_opening
        if opening is not None:
            self._notice(f"a sign-in for {self._name_of(opening)} is already open below", "warn")
            return
        try:
            account = accounts_service.begin_sign_in(slot)
        except accounts_service.AccountsError as exc:
            self._notice(f"✗ {exc}", "error")
            return
        if slot is None:
            self._open_login_window(account, fresh=True, since=None)
            return
        self._login_opening = account
        self.run_worker(
            lambda: (account, accounts_service.login_fingerprint(account)),
            name=LOGIN_OPEN_WORKER,
            group=LOGIN_OPEN_WORKER,
            thread=True,
            exit_on_error=False,
        )

    def _login_read(self, worker: Worker[Any], state: WorkerState) -> None:
        """The login a *Log in* opens over was read: open its window now. One that could not
        be read (a locked Keychain) opens none: the window could not tell the login already
        stored from a new one, and would close on it at the first poll as signed in."""
        account, self._login_opening = self._login_opening, None
        if account is None:
            return
        if state is not WorkerState.SUCCESS or not isinstance(worker.result, tuple):
            error = worker.error
            why = (
                str(error)
                if isinstance(error, accounts_service.LoginUnreadable)
                else f"it could not be read ({error or 'cancelled'})"
            )
            self._notice(f"✗ {self._name_of(account)}: no log-in opened — {why}", "error")
            return
        self._open_login_window(account, fresh=False, since=worker.result[1])

    def _open_login_window(self, account: ClaudeAccount, *, fresh: bool, since: str | None) -> None:
        try:
            # One read of [fleet] for the server and for the check before a server starts
            # there. A server kept from the page's first sign-in kept the socket of that day,
            # while the check read today's (review of #259, round 5).
            config = fleet_service.settings()
            server = self.server(config)
            window = accounts_service.open_sign_in_window(
                account, server, cwd=self.sign_in_cwd, config=config
            )
        except (TmuxError, fleet_service.FleetError) as exc:
            if fresh:
                accounts_service.abandon_sign_in(account)
            self._notice(f"✗ could not open a sign-in window: {exc}", "error")
            return
        self.login = _ClaudeLogin(account, window.pane_id, server, fresh, since)
        header = Text()
        header.append(f"Logging in to {self._name_of(account)}", style="bold")
        header.append(
            " — type /login if Claude Code does not prompt you. This pane closes by itself "
            f"once the login is recorded; {self.escape_key.upper()} hands focus back to the "
            "sidebar.",
            style="dim",
        )
        self.query_one("#login-header", Static).update(header)
        box = self.query_one("#login-box", Vertical)
        box.display = True
        pane = self.query_one("#login-pane", TerminalPane)
        pane.server = server
        pane.attach(window.pane_id)
        pane.focus()
        self._notice(f"{self._name_of(account)}: waiting for the log-in below…")
        self._login_timer = self.set_interval(LOGIN_POLL_SECONDS, self._poll_login)

    @while_running
    def _poll_login(self) -> None:
        """The timer's tick: ask whether the login landed, off the UI thread (on macOS the
        token is a Keychain read). One poll at a time; a tick while one runs is skipped."""
        login = self.login
        if login is None or (self._login_poll is not None and not self._login_poll.is_finished):
            return
        self._login_poll = self.run_worker(
            lambda: (login, accounts_service.sign_in_landed(login.account, since=login.since)),
            name=LOGIN_POLL_WORKER,
            group=LOGIN_POLL_WORKER,
            thread=True,
            exit_on_error=False,
        )

    def _login_polled(self, worker: Worker[Any], state: WorkerState) -> None:
        """A poll answered: close the window on a landed login, or once Claude Code has gone.
        An answer for a window closed meanwhile is nobody's.

        A poll that raised (a ``.claude.json`` it may not read, or one half written) is said,
        once a window: read as "not landed" without a word, a user who had signed in watched
        "waiting for the log-in below…" until they cancelled (review of #259, round 16)."""
        result = worker.result if state is WorkerState.SUCCESS else None
        polled, landed = result if isinstance(result, tuple) else (None, None)
        login = self.login
        if login is None or (polled is not None and polled is not login):
            return
        if isinstance(landed, ClaudeIdentity):
            self._finish_login(landed.email)
            return
        if state is WorkerState.ERROR and not login.poll_failed:
            login.poll_failed = True
            self._notice(
                f"⚠ {self._name_of(login.account)}: could not tell whether the log-in landed "
                f"({worker.error}) — still waiting; finish it below, or Cancel",
                "warn",
            )
        facts = login.server.pane_facts(login.pane_id)
        if facts is None or facts.dead:
            self._abandon_login("Claude Code closed before a sign-in landed")

    def _close_login_window(self) -> _ClaudeLogin | None:
        login = self.login
        self.login = None
        if self._login_timer is not None:
            self._login_timer.stop()
            self._login_timer = None
        if login is not None:
            # Already gone is the state we want, so a refusal is not a failure.
            with contextlib.suppress(TmuxError):
                login.server.kill_window(login.pane_id)
        pane = self.query_one("#login-pane", TerminalPane)
        pane.attach(None)
        self.query_one("#login-box", Vertical).display = False
        return login

    def _finish_login(self, email: str) -> None:
        login = self._close_login_window()
        if login is None:
            return
        account = login.account
        # The reading taken over the old token (expired, rejected) is not this login's:
        # the row says "reading usage…" until the next frame asks again (``show``).
        self.usage.pop(account.slot, None)
        self._asked.discard(account.slot)
        row = self.row_of(account.slot)
        if row is not None:
            row.show(row.status, None, trend=row.trend)
        if login.fresh:
            self._notice(
                f"✓ Added Claude account {account.slot}: {email} — wiring its hooks…", "ok"
            )
        else:
            self._notice(
                f"✓ {self._name_of(account)} signed in as {email} — wiring its hooks…", "ok"
            )
        self.run_worker(
            lambda: accounts_service.complete_sign_in(account),
            name=COMPLETE_WORKER,
            group=COMPLETE_WORKER,
            thread=True,
            exit_on_error=False,
        )

    def _complete_finished(self, worker: Worker[Any], state: WorkerState) -> None:
        if state is WorkerState.SUCCESS and isinstance(worker.result, ClaudeAccountStatus):
            status = worker.result
            name = self._name_of(status.account)  # the record handed back carries no alias
            who = status.identity.email if status.identity else name
            hooks = "hooks installed" if status.hooks_installed else "hooks NOT installed"
            self._notice(f"✓ {name}: {who} · {hooks}", "ok" if status.hooks_installed else "warn")
        elif state is WorkerState.ERROR:
            self._notice(_hooks_did_not_install(worker.error), "warn")
        self.post_message(AccountsChanged())

    def _abandon_login(self, reason: str) -> None:
        """Close the window; a fresh slot is settled off the UI thread (:data:`ABANDON_WORKER`):
        a login that landed after the last poll keeps it, wired, and nothing is deleted."""
        login = self._close_login_window()
        if login is None:
            return
        if not login.fresh:
            self._notice(f"sign-in cancelled — {reason}; nothing changed", "warn")
            self.post_message(AccountsChanged())
            return
        account = login.account
        self._abandoning = reason
        self.run_worker(
            lambda: (account, accounts_service.settle_sign_in(account)),
            name=ABANDON_WORKER,
            group=ABANDON_WORKER,
            thread=True,
            exit_on_error=False,
        )

    def _abandon_finished(self, worker: Worker[Any], state: WorkerState) -> None:
        """The fresh slot of a cancelled sign-in, settled. A worker that raised said "nothing
        changed" with no reason, of a slot whose login had landed and was kept: one whose
        hooks then failed reads as ``_complete_finished`` reads it, and any other failure
        says the slot was left as it is (review of #259, round 16)."""
        reason, self._abandoning = self._abandoning or "cancelled", None
        result = worker.result if state is WorkerState.SUCCESS else None
        settled = result[1] if isinstance(result, tuple) else None
        if isinstance(result, tuple) and isinstance(settled, ClaudeAccountStatus):
            account = result[0]
            who = settled.identity.email if settled.identity else self._name_of(account)
            self._notice(f"✓ {self._name_of(account)}: the login had landed — kept as {who}", "ok")
        elif isinstance(worker.error, accounts_service.LandedNotWired):
            self._notice(_hooks_did_not_install(worker.error), "warn")
        elif state is WorkerState.ERROR:
            self._notice(
                f"✗ sign-in cancelled — {reason}; whether its login landed could not be told "
                f"({worker.error}), so the new slot is left as it is",
                "error",
            )
        else:
            what = "the new slot was discarded" if settled is True else "nothing changed"
            self._notice(f"sign-in cancelled — {reason}; {what}", "warn")
        self.post_message(AccountsChanged())

    @on(Button.Pressed, "#login-cancel")
    def _cancel_login(self) -> None:
        self._abandon_login("cancelled")

    def remove_claude_account(self, slot: int) -> None:
        account = core.find_account(slot)
        if account is None:
            self._notice(f"✗ no account in slot {slot} any more", "error")
            return
        if self.login is not None and self.login.account.slot == slot:
            self._notice("✗ finish or cancel the sign-in below first", "error")
            return

        def remove_with_notes() -> tuple[Path, list[str]]:
            notes: list[str] = []  # what happened to bindings that named the slot
            return accounts_service.remove(account, notes=notes), notes

        self.run_worker(
            remove_with_notes,
            name=REMOVE_WORKER,
            group=REMOVE_WORKER,
            thread=True,
            exit_on_error=False,
        )

    def _remove_finished(self, worker: Worker[Any], state: WorkerState) -> None:
        if state is WorkerState.SUCCESS and isinstance(worker.result, tuple):
            moved, notes = worker.result
            line = f"✓ removed — its directory is kept at {moved}; delete it when sure"
            if (
                notes
            ):  # a role binding re-pointed or cleared: the operator must hear it (third round)
                line += " · " + " · ".join(notes)
            self._notice(line, "warn" if notes else "ok")
        elif state is WorkerState.ERROR:
            self._notice(f"✗ {worker.error}", "error")
        self.post_message(AccountsChanged())

    # --- worker results -----------------------------------------------------------------------

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state not in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
            return
        worker, state = event.worker, event.state
        if worker.name == USAGE_WORKER:
            if state is WorkerState.SUCCESS and isinstance(worker.result, dict):
                self._show_usage(worker.result)
                self.post_message(UsageRead())
            if worker is self._usage_worker:
                self._paint_usage_age()
                if self._usage_owed:
                    self._usage_owed = False
                    self.refresh_usage()
        elif worker.name == CREDITS_WORKER:
            if state is WorkerState.SUCCESS and isinstance(worker.result, tuple):
                self._show_credits(*worker.result)
        elif worker.name == SIGN_IN_WORKER:
            self._sign_in_finished(worker, state)
        elif worker.name == SIGN_OUT_WORKER:
            self._sign_out_finished(worker, state)
        elif worker.name == COMPLETE_WORKER:
            self._complete_finished(worker, state)
        elif worker.name == LOGIN_OPEN_WORKER:
            self._login_read(worker, state)
        elif worker.name == LOGIN_POLL_WORKER:
            self._login_polled(worker, state)
        elif worker.name == ABANDON_WORKER:
            self._abandon_finished(worker, state)
        elif worker.name == REMOVE_WORKER:
            self._remove_finished(worker, state)
        elif worker.name == ARRANGE_WORKER:
            self._arrange_finished(worker, state)
        elif worker.name == RENAME_WORKER:
            self._rename_finished(worker, state)
