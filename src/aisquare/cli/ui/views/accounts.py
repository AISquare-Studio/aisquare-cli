"""The Accounts page: the AISquare sign-in on top, the Claude Code accounts under it.

docs/plans/claude-accounts.md. Two halves, one page:

- **AISquare** — the session ``aisquare login`` stores, read through
  ``services.iam``. *Sign in* runs the same device grant the terminal runs,
  as a native card rather than a pane: the one-time code and the link appear
  here, the browser is opened when one can reach the user, and a thread
  worker polls (``services.device_flow``) until the approval lands or *Cancel*
  is pressed. *Sign out* revokes and forgets, as ``aisquare logout`` does.
- **Claude Code** — every slot ``services.claude_accounts`` knows, with who is
  signed in, the plan, and the account's five-hour and seven-day usage. *Add*
  makes a fresh slot and opens Claude Code's own login in a tmux window
  rendered right here (a ``TerminalPane``); the view polls the slot's
  directory and, the moment Claude Code has written a login into it, records
  the account, installs aisquare's hooks and closes the window. Nothing is
  typed for the user and nothing is written into Claude Code's files.

**The view holds no state that matters** (fleet-tui plan §2). The shell hands
it a fresh ``AccountsOverview`` on every refresh; what the view owns is the
transient — a sign-in in flight, a usage fetch — and the usage numbers, which
are the one thing here that costs a request and so are fetched on their own
slow cadence (:data:`USAGE_SECONDS`) and only while the page is on screen.

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
from typing import Any, ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.timer import Timer
from textual.widgets import Button, Static
from textual.worker import Worker, WorkerState

from aisquare.cli.common import local_time
from aisquare.cli.ui.terminal import TerminalPane
from aisquare.core import browser
from aisquare.core import claude_accounts as core
from aisquare.core.tmux import TmuxError, TmuxServer
from aisquare.models import AccountsOverview, ClaudeAccount, ClaudeAccountStatus, ClaudeUsage
from aisquare.services import auth as auth_service
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import device_flow, iam
from aisquare.services import fleet as fleet_service

USAGE_SECONDS = 60.0
"""How often the usage numbers are re-fetched while the page is on screen."""
LOGIN_POLL_SECONDS = 1.0
"""How often a sign-in window's directory is checked for a landed login."""

USAGE_WORKER = "accounts-usage"
SIGN_IN_WORKER = "aisquare-sign-in"
SIGN_OUT_WORKER = "aisquare-sign-out"
COMPLETE_WORKER = "claude-complete-sign-in"
REMOVE_WORKER = "claude-remove"

_BAR_CELLS = 5
_WARN_AT = 50.0
_HOT_AT = 80.0

SessionReader = Callable[[], "iam.Session | None"]


class AccountsChanged(Message):
    """Something on the page changed what the sidebar summarises — an account added, a sign-in."""


@dataclass(frozen=True)
class AccountsSummary:
    """What the sidebar's Accounts section says — built here so the words live in one place.

    The section is 28 columns wide, so the first line carries only the AISquare
    state beside the title and the second line carries the Claude count plus
    the one thing most worth knowing: a slot with no login, an AISquare sign-in
    still to do, or who slot 1 is.
    """

    aisquare: bool | None
    """Signed in, not, or ``None`` when the session could not be read."""
    line: Text | None
    """The detail line under the title, or nothing worth a line."""


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


def _resets(when: datetime | None) -> str:
    return "" if when is None else f" · resets {local_time(when):%H:%M}"


def account_line_text(status: ClaudeAccountStatus, usage: ClaudeUsage | None) -> Text:
    """One slot: ``2  account 2  me@…  max 5x   session ▮▯▯▯▯ 3% · resets 15:29   week 7%``."""
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(f"{status.account.slot}  ", style="bold")
    text.append(f"{status.label:<10}", style="cyan")
    if status.identity is None:
        text.append("not signed in", style="dim")
        return text
    text.append(status.identity.email)
    if status.subscription:
        text.append(f"  {status.subscription}", style="dim")
    if not status.signed_in:
        text.append("  ⚠ token missing — open a session to sign in again", style="yellow")
        return text
    if status.token_state == "expired":
        text.append("  ⚠ token expired — open a session to refresh it", style="yellow")
    if usage is None:
        text.append("  usage: …", style="dim")
    elif not usage.available:
        text.append(f"  usage: {usage.reason or 'unavailable'}", style="dim")
    else:
        if usage.session_percent is not None:
            text.append("  session ", style="dim")
            text.append_text(usage_bar(usage.session_percent))
            text.append(_resets(usage.session_resets_at), style="dim")
        if usage.week_percent is not None:
            text.append("  week ", style="dim")
            text.append_text(usage_bar(usage.week_percent))
            text.append(_resets(usage.week_resets_at), style="dim")
    return text


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


def summarise(
    overview: AccountsOverview | None, session: iam.Session | None, *, session_known: bool = True
) -> AccountsSummary:
    """What the sidebar shows, from the same facts the page shows."""
    aisquare: bool | None = (session is not None) if session_known else None
    if overview is None:
        return AccountsSummary(aisquare, Text("accounts unreadable", style="yellow"))
    if not overview.claude.installed:
        return AccountsSummary(aisquare, Text("⚠ Claude Code not installed", style="yellow"))
    signed = [status for status in overview.accounts if status.signed_in]
    unsigned = [status for status in overview.accounts if not status.signed_in]
    line = Text(f"{len(signed)} Claude", style="dim", no_wrap=True, overflow="ellipsis")
    if unsigned:
        line.append(" · ", style="dim")
        line.append(f"⚠ #{unsigned[0].account.slot} no login", style="yellow")
    elif aisquare is False:
        line.append(" · AISquare: sign in", style="dim")
    elif signed and signed[0].identity is not None:
        line.append(f" · {signed[0].identity.email}", style="dim")
    return AccountsSummary(aisquare, line)


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


# --- widgets -----------------------------------------------------------------------------------


class AccountRow(Horizontal):
    """One slot: its line, a *Sign in* when it has no login, a *Remove* when it is ours."""

    DEFAULT_CSS = """
    AccountRow { height: auto; margin: 0 0 1 0; }
    AccountRow .account-line { width: 1fr; height: auto; padding: 1 0 0 0; }
    AccountRow Button { min-width: 10; margin: 0 0 0 1; }
    """

    def __init__(self, status: ClaudeAccountStatus, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.status = status
        self.usage: ClaudeUsage | None = None

    @property
    def slot(self) -> int:
        return self.status.account.slot

    def compose(self) -> ComposeResult:
        yield Static(account_line_text(self.status, self.usage), classes="account-line")
        yield Button("Sign in", id=f"account-sign-in-{self.slot}", variant="primary")
        yield Button("Remove", id=f"account-remove-{self.slot}", variant="default")

    def on_mount(self) -> None:
        self._paint()

    def show(self, status: ClaudeAccountStatus, usage: ClaudeUsage | None) -> None:
        self.status = status
        self.usage = usage
        if self.is_mounted:
            self._paint()

    def _paint(self) -> None:
        self.query_one(".account-line", Static).update(account_line_text(self.status, self.usage))
        self.query_one(f"#account-sign-in-{self.slot}", Button).display = not self.status.signed_in
        self.query_one(f"#account-remove-{self.slot}", Button).display = self.status.account.managed


class AccountsView(Vertical):
    """The page. ``show`` paints an overview; the buttons run the flows in the module docstring."""

    DEFAULT_CSS = """
    AccountsView { padding: 1 2; }
    AccountsView #accounts-body { height: 1fr; }
    AccountsView .section-title { height: auto; margin-top: 1; }
    AccountsView #aisquare-status { height: auto; margin-top: 1; }
    AccountsView #aisquare-code { height: auto; margin-top: 1; }
    AccountsView .actions { height: auto; margin-top: 1; }
    AccountsView .actions Button { margin: 0 1 0 0; }
    AccountsView #claude-rows { height: auto; margin-top: 1; }
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
        self.usage: dict[int, ClaudeUsage] = {}
        self.login: _ClaudeLogin | None = None
        self._login_timer: Timer | None = None
        self._usage_timer: Timer | None = None
        self._cancel_sign_in: threading.Event | None = None
        self._on_screen = False

    # --- layout ------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="accounts-body"):
            yield Static(Text("AISquare", style="bold"), classes="section-title")
            yield Static(aisquare_status_text(None), id="aisquare-status")
            yield Static("", id="aisquare-code")
            with Horizontal(classes="actions", id="aisquare-actions"):
                yield Button("Sign in", id="aisquare-sign-in", variant="primary")
                yield Button("Sign out", id="aisquare-sign-out")
                yield Button("Cancel", id="aisquare-cancel", variant="warning")
            yield Static(
                Text("Claude Code", style="bold"), id="claude-title", classes="section-title"
            )
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
        self._usage_timer = self.set_interval(USAGE_SECONDS, self.refresh_usage)

    def on_show(self) -> None:
        self._on_screen = True
        self.refresh_usage()

    def on_hide(self) -> None:
        self._on_screen = False

    def on_unmount(self) -> None:
        """The page is leaving (``q``, usually): nothing transient may outlive it.

        Textual cancelling a thread worker does not stop its callable, so the
        device flow's own cancel flag is set — the wait returns within half a
        second and stores nothing. A Claude sign-in window is closed and its
        slot settled the way a poll would have: recorded if the login landed in
        the meantime, discarded if it was fresh and did not. No widget is
        touched here; they are being torn down under us.
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
            if accounts_service.sign_in_landed(login.account) is not None:
                accounts_service.complete_sign_in(login.account)
            elif login.fresh:
                accounts_service.abandon_sign_in(login.account)

    def server(self) -> TmuxServer:
        if self._server is None:
            self._server = fleet_service.server()
        return self._server

    # --- data in ----------------------------------------------------------------------------

    def show(self, overview: AccountsOverview) -> None:
        """A fresh frame from the shell: paint it, and re-read the AISquare session beside it."""
        self.overview = overview
        self.session = self._read_session()
        if not self.is_mounted:
            return
        self._paint_aisquare()
        self._paint_claude(overview)

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

    def _paint_claude(self, overview: AccountsOverview) -> None:
        self.query_one("#claude-title", Static).update(claude_title_text(overview))
        self.query_one("#claude-add", Button).disabled = not overview.claude.installed
        holder = self.query_one("#claude-rows", Vertical)
        existing = {row.slot: row for row in holder.query(AccountRow)}
        for index, status in enumerate(overview.accounts):
            slot = status.account.slot
            row = existing.pop(slot, None)
            if row is None:
                row = AccountRow(status, id=f"account-row-{slot}")
                row.usage = self.usage.get(slot)
                if index < len(holder.children):
                    holder.mount(row, before=index)
                else:
                    holder.mount(row)
            else:
                row.show(status, self.usage.get(slot))
                if index < len(holder.children) and holder.children[index] is not row:
                    holder.move_child(row, before=index)
        for stale in existing.values():
            self.usage.pop(stale.slot, None)
            stale.remove()

    def rows(self) -> list[AccountRow]:
        return list(self.query(AccountRow))

    def _notice(self, text: str, tone: str = "dim") -> None:
        style = {"ok": "green", "warn": "yellow", "error": "bold red"}.get(tone, "dim")
        self.query_one("#accounts-notice", Static).update(Text(text, style=style))

    # --- usage (the one thing here that costs a request) ---------------------------------------

    def refresh_usage(self) -> None:
        """Ask about every signed-in slot off the UI thread, if the page is on screen.

        Every signed-in slot, not only those with a readable token: the service
        answers a Keychain-backed (macOS) or expired token with its reason and
        no request, and that reason is what the row must show instead of a
        ``usage: …`` that never resolves.
        """
        if not self._on_screen or self.overview is None:
            return
        accounts = [status.account for status in self.overview.accounts if status.signed_in]
        if not accounts:
            return
        self.run_worker(
            lambda: {account.slot: accounts_service.usage(account) for account in accounts},
            name=USAGE_WORKER,
            group=USAGE_WORKER,
            exclusive=True,
            thread=True,
            exit_on_error=False,
        )

    def _show_usage(self, fetched: dict[int, ClaudeUsage]) -> None:
        self.usage.update(fetched)
        for row in self.rows():
            if row.slot in fetched:
                row.show(row.status, fetched[row.slot])

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
        if cancel.is_set():
            # Belt to the wait's braces: nothing is stored past a Cancel.
            raise iam.IamError("cancelled", "Sign-in cancelled. Nothing was stored.")
        return auth_service.complete_sign_in(api_url, endpoints, token)

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
            self._notice(f"✓ Signed in to AISquare as {who}", "ok")
            self.post_message(AccountsChanged())
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
            lambda: auth_service.sign_out(session),
            name=SIGN_OUT_WORKER,
            group=SIGN_OUT_WORKER,
            exclusive=True,
            thread=True,
            exit_on_error=False,
        )

    def _sign_out_finished(self, worker: Worker[Any], state: WorkerState) -> None:
        self.session = self._read_session()
        if state is WorkerState.SUCCESS:
            revoked = bool(worker.result)
            self._notice(
                "✓ Signed out of AISquare"
                + ("" if revoked else " (locally — the server could not be reached to revoke)"),
                "ok",
            )
            self.post_message(AccountsChanged())
        elif state is WorkerState.ERROR:
            self._notice(f"✗ sign-out failed: {worker.error}", "error")
        self._paint_aisquare()

    # --- Claude Code: a sign-in window, watched --------------------------------------------------

    @on(Button.Pressed, "#claude-add")
    def _add_claude_account(self) -> None:
        self.begin_claude_sign_in(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.startswith("account-sign-in-"):
            event.stop()
            self.begin_claude_sign_in(int(button_id.rsplit("-", 1)[1]))
        elif button_id.startswith("account-remove-"):
            event.stop()
            self.remove_claude_account(int(button_id.rsplit("-", 1)[1]))

    def begin_claude_sign_in(self, slot: int | None) -> None:
        """Open Claude Code on ``slot`` (a fresh slot when ``None``) in a pane, watched."""
        if self.login is not None:
            self._notice(
                f"a sign-in for {core.label(self.login.account)} is already open below", "warn"
            )
            return
        try:
            account = accounts_service.begin_sign_in(slot)
        except accounts_service.AccountsError as exc:
            self._notice(f"✗ {exc}", "error")
            return
        fresh = slot is None
        try:
            server = self.server()
            window = accounts_service.open_sign_in_window(account, server, cwd=self.sign_in_cwd)
        except TmuxError as exc:
            if fresh:
                accounts_service.abandon_sign_in(account)
            self._notice(f"✗ could not open a sign-in window: {exc}", "error")
            return
        self.login = _ClaudeLogin(account, window.pane_id, server, fresh)
        header = Text()
        header.append(f"Signing in to {core.label(account)}", style="bold")
        header.append(
            " — finish Claude Code's login in this pane; it closes by itself once the account "
            f"is recorded. {self.escape_key.upper()} hands focus back to the sidebar.",
            style="dim",
        )
        self.query_one("#login-header", Static).update(header)
        box = self.query_one("#login-box", Vertical)
        box.display = True
        pane = self.query_one("#login-pane", TerminalPane)
        pane.server = server
        pane.attach(window.pane_id)
        pane.focus()
        self._notice(f"{core.label(account)}: waiting for the sign-in below…")
        self._login_timer = self.set_interval(LOGIN_POLL_SECONDS, self._poll_login)

    def _poll_login(self) -> None:
        login = self.login
        if login is None:
            return
        landed = accounts_service.sign_in_landed(login.account)
        if landed is not None:
            self._finish_login(landed.email)
            return
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
        if login.fresh:
            self._notice(
                f"✓ Added Claude account {account.slot}: {email} — wiring its hooks…", "ok"
            )
        else:
            self._notice(f"✓ {core.label(account)} signed in as {email} — wiring its hooks…", "ok")
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
            who = status.identity.email if status.identity else status.label
            hooks = "hooks installed" if status.hooks_installed else "hooks NOT installed"
            self._notice(
                f"✓ {status.label}: {who} · {hooks}", "ok" if status.hooks_installed else "warn"
            )
        elif state is WorkerState.ERROR:
            self._notice(
                f"⚠ signed in, but aisquare's hooks did not install: {worker.error} — "
                "run: aisquare agents connect claude-code --config-dir <the slot's directory>",
                "warn",
            )
        self.post_message(AccountsChanged())

    def _abandon_login(self, reason: str) -> None:
        login = self._close_login_window()
        if login is None:
            return
        discarded = accounts_service.abandon_sign_in(login.account) if login.fresh else False
        what = "the new slot was discarded" if discarded else "nothing changed"
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
        self.run_worker(
            lambda: accounts_service.remove(account),
            name=REMOVE_WORKER,
            group=REMOVE_WORKER,
            thread=True,
            exit_on_error=False,
        )

    def _remove_finished(self, worker: Worker[Any], state: WorkerState) -> None:
        if state is WorkerState.SUCCESS and isinstance(worker.result, Path):
            self._notice(
                f"✓ removed — its directory is kept at {worker.result}; delete it when sure",
                "ok",
            )
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
        elif worker.name == SIGN_IN_WORKER:
            self._sign_in_finished(worker, state)
        elif worker.name == SIGN_OUT_WORKER:
            self._sign_out_finished(worker, state)
        elif worker.name == COMPLETE_WORKER:
            self._complete_finished(worker, state)
        elif worker.name == REMOVE_WORKER:
            self._remove_finished(worker, state)
