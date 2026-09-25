"""The Accounts page and its sidebar section, driven headless through Textual's Pilot.

docs/plans/claude-accounts.md. The shell is the real ``FleetApp`` with the
Accounts reader scripted (``accounts=``), the AISquare session scripted through
``iam.current_session``, and every network-shaped call replaced: the usage
fetch, the device flow's wait, the sign-out's revoke. tmux is a recorder that
answers "no server" and refuses any socket but the test's own, as
``test_ui_shell.py`` does — a sign-in window here must never reach the
developer's real fleet.

Every assertion reads what the widget SHOWS, and every behaviour has its
negative: the button that must be hidden, the row that must not carry a bar,
the slot that must not be discarded.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

import pytest
from textual.containers import Vertical
from textual.pilot import Pilot
from textual.widgets import Button, Static
from textual.worker import Worker, WorkerError, WorkerState

from aisquare.cli.ui.app import ACCOUNTS_WORKER, FleetApp
from aisquare.cli.ui.sidebar import AccountsSection, AccountsTitle
from aisquare.cli.ui.terminal import TerminalPane
from aisquare.cli.ui.views.accounts import (
    SIGN_IN_WORKER,
    AccountRow,
    AccountsView,
    account_line_text,
    aisquare_status_text,
    grant_text,
    summarise,
    usage_bar,
)
from aisquare.core import browser, credentials, paths
from aisquare.core import claude_accounts as core
from aisquare.core import tmux as tmux_core
from aisquare.core.store import store_session
from aisquare.core.tmux import Completed, TmuxServer, WindowInfo
from aisquare.models import (
    AccountsOverview,
    ClaudeAccount,
    ClaudeAccountStatus,
    ClaudeIdentity,
    ClaudeInstall,
    ClaudeUsage,
)
from aisquare.services import auth as auth_service
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import device_flow, iam
from aisquare.services import fleet as fleet_service
from tests.pane_harness import asks_a_server, socket_of
from tests.ui_workers import settle_page

T = TypeVar("T")
SIZE = (140, 40)
PRIVATE_SOCKET = f"asq-test-{os.getpid()}-ui-accounts"
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


# --- fixtures and helpers --------------------------------------------------------------------


def _status(
    slot: int,
    email: str | None,
    *,
    managed: bool | None = None,
    signed_in: bool | None = None,
    token_state: str = "ok",
    subscription: str | None = "max 5x",
    root: Path = Path("/h/.aisquare/claude-accounts"),
) -> ClaudeAccountStatus:
    is_managed = (slot != 1) if managed is None else managed
    account = ClaudeAccount(
        slot=slot,
        config_dir=root / str(slot) if is_managed else Path("/h/.claude"),
        tmp_dir=Path("/h/.aisquare/cache/claude-accounts") / str(slot) if is_managed else None,
        managed=is_managed,
    )
    identity = ClaudeIdentity(email=email, organization="AISquare") if email else None
    return ClaudeAccountStatus(
        account=account,
        label=core.label(account),
        identity=identity,
        signed_in=(identity is not None) if signed_in is None else signed_in,
        token_state=token_state if identity else "missing",
        subscription=subscription if identity else None,
        hooks_installed=True,
    )


def _overview(*statuses: ClaudeAccountStatus, installed: bool = True) -> AccountsOverview:
    claude = ClaudeInstall(installed=installed, binary="/opt/bin/claude" if installed else None)
    if installed:
        claude = claude.model_copy(update={"version": "2.1.266"})
    return AccountsOverview(claude=claude, accounts=list(statuses))


def _session(email: str = "me@aisquare.studio", source: str = "file") -> iam.Session:
    return iam.Session(
        api_url="https://api.aisquare.studio",
        token="aisq_secret",
        source=source,
        expires_at=NOW + timedelta(days=80),
        email=email,
        sub="usr_1",
    )


@pytest.fixture(autouse=True)
def no_real_tmux(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, ...]]]:
    """Every tmux command here addresses the test's socket; the server never answers."""
    ran: list[tuple[str, ...]] = []

    def record(argv: Sequence[str], stdin: bytes | None) -> Completed:
        ran.append(tuple(argv))
        return Completed(1, "", "no server running (a UI test addresses no real fleet)\n")

    monkeypatch.setattr(tmux_core, "_tmux", record)
    # `binary=sys.executable`: the runner above is what answers, but the calls
    # still go through `TmuxServer.binary()`, which is `shutil.which("tmux")` —
    # so on a machine with no tmux these tests raised TmuxUnavailable before
    # reaching the UI they are about. A binary that exists and is never run.
    monkeypatch.setattr(
        fleet_service,
        "server",
        lambda config=None: TmuxServer(PRIVATE_SOCKET, binary=sys.executable),
    )
    yield ran
    wrong = [argv for argv in ran if asks_a_server(argv) and socket_of(argv) != PRIVATE_SOCKET]
    assert not wrong, f"a UI test addressed a tmux socket that is not the test's: {wrong[:2]}"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The usage fetch, the AISquare session and the browser are scripted, never real."""
    script: dict[str, Any] = {
        "usage": ClaudeUsage(available=False, reason="scripted: no fetch"),
        "session": None,
        "usage_calls": [],
    }

    def usage(account: ClaudeAccount, **kwargs: Any) -> ClaudeUsage:
        script["usage_calls"].append(account.slot)
        result = script["usage"]
        answer = result(account) if callable(result) else result
        assert isinstance(answer, ClaudeUsage)
        return answer

    script["opened"] = []

    def open_url(url: str, *args: Any, **kwargs: Any) -> bool:
        script["opened"].append(url)
        return False

    monkeypatch.setattr(accounts_service, "usage", usage)
    monkeypatch.setattr(iam, "current_session", lambda api_url=None: script["session"])
    monkeypatch.setattr(browser, "is_headless", lambda *a, **k: True)
    monkeypatch.setattr(browser, "open_url", open_url)
    return script


def drive(
    fn: Callable[[Pilot[None]], Awaitable[T]],
    *,
    overview: AccountsOverview | None = None,
    notifications: bool = False,
) -> T:
    """Run ``fn`` against a mounted ``FleetApp`` whose Accounts reader answers ``overview``."""
    frame = overview if overview is not None else _overview(_status(1, "me@example.com"))

    async def run() -> T:
        app = FleetApp(refresh_seconds=3600, doctor=lambda: [], accounts=lambda: frame)
        async with app.run_test(size=SIZE, notifications=notifications) as pilot:
            await accounts_read(app)  # the first frame is painted before the test looks
            return await fn(pilot)

    return asyncio.run(run())


def shown(widget: Static) -> str:
    visual = widget.visual
    plain = getattr(visual, "plain", None)
    assert isinstance(plain, str), f"{widget!r} renders a {type(visual).__name__}, not text"
    return plain


async def settle(app: FleetApp) -> None:
    """Let the page go quiet: every message queued on it handled, every worker of ours done.

    Not only the workers that exist when it is called: the page starts its usage
    reading from ``on_show``, and a ``Show`` still queued when a test settled left
    no worker to wait for. On windows-latest the reading then landed after the
    test had read the page, which had no usage at all. See ``settle_page``.
    """
    await settle_page(app)


async def accounts_read(app: FleetApp) -> None:
    """Wait until the shell's accounts read has answered and its frame is painted.

    ``refresh_accounts`` reads in a thread worker (final review of #203, accounts
    F2), so a test that asks for a frame and then reads the page waits for that
    worker, and only that one: :func:`settle` would also wait for a worker a
    test is holding on purpose. Rounds, as ``settle_page`` goes round, because
    the answer is a message the app handles after the worker has finished.
    """
    pilot = Pilot(app)
    for _ in range(20):
        await pilot.pause()
        reads = [w for w in app.workers if w.group == ACCOUNTS_WORKER and not w.is_finished]
        if not reads and not app.message_queue_size:
            return
        for worker in reads:
            with contextlib.suppress(WorkerError):
                await worker.wait()


def fleet_app(pilot: Pilot[None]) -> FleetApp:
    app = pilot.app
    assert isinstance(app, FleetApp)
    return app


async def open_accounts(pilot: Pilot[None]) -> AccountsView:
    app = fleet_app(pilot)
    await pilot.click(app.query_one(AccountsSection))
    await pilot.pause()
    view = app.query_one("#accounts", AccountsView)
    assert app.current_view() is view
    return view


def row(view: AccountsView, slot: int) -> AccountRow:
    return view.query_one(f"#account-row-{slot}", AccountRow)


def line(view: AccountsView, slot: int) -> str:
    return shown(row(view, slot).query_one(".account-line", Static))


def notice(view: AccountsView) -> str:
    return shown(view.query_one("#accounts-notice", Static))


# --- pure helpers --------------------------------------------------------------------------------


def test_usage_bar_fills_five_cells_and_colours_by_pressure() -> None:
    assert usage_bar(0).plain == "▯▯▯▯▯ 0%"
    assert usage_bar(12).plain == "▮▯▯▯▯ 12%"
    assert usage_bar(50).plain == "▮▮▮▯▯ 50%"  # half rounds up, not to even
    assert usage_bar(89).plain == "▮▮▮▮▯ 89%"
    assert usage_bar(90).plain == "▮▮▮▮▮ 90%"
    assert usage_bar(100).plain == "▮▮▮▮▮ 100%"
    assert usage_bar(140).plain == "▮▮▮▮▮ 100%"  # clamped, never six cells
    styles = {percent: str(usage_bar(percent).spans[0].style) for percent in (12, 60, 95)}
    assert styles[12] == "green" and styles[60] == "yellow" and styles[95] == "bold red"


def test_account_line_says_who_what_plan_and_how_much() -> None:
    usage = ClaudeUsage(
        available=True,
        session_percent=12,
        session_resets_at=NOW,
        week_percent=40,
        week_resets_at=NOW,
    )
    signed = account_line_text(_status(2, "two@example.com"), usage).plain
    assert signed.startswith("  2  account 2")  # two blanks: no default badge
    assert "two@example.com" in signed and "max 5x" in signed
    assert "session ▮▯▯▯▯ 12%" in signed and "week ▮▮▯▯▯ 40%" in signed
    assert "resets" in signed

    pending = account_line_text(_status(2, "two@example.com"), None).plain
    assert "usage: …" in pending and "▮" not in pending

    unavailable = ClaudeUsage(available=False, reason="HTTP 503 from the usage endpoint")
    assert "usage: HTTP 503" in account_line_text(_status(2, "two@example.com"), unavailable).plain

    absent = account_line_text(_status(3, None), usage).plain
    assert absent.startswith("  3  account 3") and "not signed in" in absent
    assert "▮" not in absent and "max" not in absent  # nothing about a login that is not there

    expired = _status(1, "me@example.com", token_state="expired")
    assert "token expired" in account_line_text(expired, None).plain
    logged_out = _status(2, "two@example.com", signed_in=False)
    assert "token missing" in account_line_text(logged_out, usage).plain
    assert "▮" not in account_line_text(logged_out, usage).plain


def test_aisquare_status_text_covers_every_source() -> None:
    assert aisquare_status_text(None).plain.startswith("Not signed in")
    filed = aisquare_status_text(_session()).plain
    assert "Signed in as me@aisquare.studio" in filed
    assert "api.aisquare.studio" in filed and "expires" in filed
    env = aisquare_status_text(_session(source="env")).plain
    assert f"token from {iam.TOKEN_ENV_VAR}" in env and "Signed in as" not in env


def test_grant_text_puts_the_code_first_and_says_what_the_browser_did() -> None:
    grant = iam.DeviceAuthorization(
        device_code="d",
        user_code="WDJB-MJHT",
        verification_uri="https://home.aisquare.studio/cli",
        verification_uri_complete="https://home.aisquare.studio/cli?code=WDJB-MJHT",
        expires_in=900,
        interval=5,
    )
    opened = grant_text(grant, opened=True).plain
    assert opened.index("WDJB-MJHT") < opened.index("https://")
    assert "Opening your browser" in opened
    assert "No browser opened here" in grant_text(grant, opened=False).plain
    assert "Waiting for the approval" in grant_text(grant, opened=None).plain


def test_summarise_names_the_worst_thing_first() -> None:
    both = summarise(_overview(_status(1, "me@example.com"), _status(2, "two@x")), _session())
    assert both.aisquare is True
    assert both.line is not None and both.line.plain == "2 Claude · me@example.com"

    gap = summarise(_overview(_status(1, "me@example.com"), _status(3, None)), _session())
    assert gap.line is not None and gap.line.plain == "1 Claude · ⚠ #3 no login"

    no_aisquare = summarise(_overview(_status(1, "me@example.com")), None)
    assert no_aisquare.aisquare is False
    assert no_aisquare.line is not None
    assert no_aisquare.line.plain == "1 Claude · AISquare: sign in"

    missing = summarise(_overview(installed=False), None)
    assert missing.line is not None and missing.line.plain == "⚠ Claude Code not installed"

    unreadable = summarise(None, None, session_known=False)
    assert unreadable.aisquare is None
    assert unreadable.line is not None and "unreadable" in unreadable.line.plain
    # Every line fits the section's 28 columns without an ellipsis.
    for summary in (both, gap, no_aisquare, missing):
        assert summary.line is not None and len(summary.line.plain) <= 28


# --- the sidebar section and the page -----------------------------------------------------------


def test_the_section_summarises_and_opens_the_page(no_network: dict[str, Any]) -> None:
    no_network["session"] = _session()
    overview = _overview(_status(1, "me@example.com"), _status(2, "two@example.com"))

    async def go(pilot: Pilot[None]) -> tuple[str, str, str | None, list[str], str, str]:
        app = fleet_app(pilot)
        title = shown(app.query_one(AccountsTitle))
        detail = shown(app.query_one(AccountsSection).query_one(".accounts-line", Static))
        before = app.content.current
        view = await open_accounts(pilot)
        rows = [line(view, 1), line(view, 2)]
        status = shown(view.query_one("#aisquare-status", Static))
        claude = shown(view.query_one("#claude-title", Static))
        return title, detail, before, rows, status, claude

    title, detail, before, rows, status, claude = drive(go, overview=overview)
    assert title == "Accounts  ✓ AISquare"
    assert detail == "2 Claude · me@example.com"
    assert before == "welcome"
    assert rows[0].startswith("  1  plain claude") and "me@example.com" in rows[0]
    assert rows[1].startswith("  2  account 2") and "two@example.com" in rows[1]
    assert "Signed in as me@aisquare.studio" in status
    assert claude.startswith("Claude Code 2.1.266")


def test_the_shells_tick_reads_the_accounts_off_the_ui_thread_and_paints_the_answer(
    no_network: dict[str, Any],
) -> None:
    """Final review of #203, accounts F2: ``refresh_accounts`` called the reader on the
    event loop every two seconds. The real one reads ``context.db`` and may write the
    registry's reconcile, which waits out the busy timeout behind another writer, so
    the UI froze for as long as a hook held the lock. Held here as that writer would
    hold it: the tick returns at once, the section keeps its frame, and the answer is
    painted when it comes."""
    no_network["session"] = _session()
    frames = [
        _overview(_status(1, "me@example.com")),
        _overview(_status(1, "me@example.com"), _status(2, "two@example.com")),
    ]
    threads: list[str] = []
    hold, asked, release = threading.Event(), threading.Event(), threading.Event()

    def reader() -> AccountsOverview:
        threads.append(threading.current_thread().name)
        if hold.is_set():
            asked.set()
            release.wait(10)
        return frames[0]

    async def run() -> tuple[str, bool, str, str]:
        app = FleetApp(refresh_seconds=3600, doctor=lambda: [], accounts=reader)
        async with app.run_test(size=SIZE) as pilot:
            await accounts_read(app)
            detail = app.query_one(AccountsSection).query_one(".accounts-line", Static)
            first = shown(detail)
            hold.set()
            frames.pop(0)
            try:
                app.refresh_data()  # the two-second tick, with the registry held
                held = await asyncio.to_thread(asked.wait, 5)
                await pilot.pause()
                while_held = shown(detail)
            finally:
                release.set()
            await accounts_read(app)
            return first, held, while_held, shown(detail)

    first, held, while_held, after = asyncio.run(run())
    assert threads and threading.main_thread().name not in threads, threads
    assert held, "the tick asked for the accounts"
    assert first == while_held == "1 Claude · me@example.com"  # the last frame, kept
    assert after == "2 Claude · me@example.com"  # the answer, painted when it came


def test_ticks_while_the_accounts_read_waits_let_it_answer_and_read_once_more_after_it(
    no_network: dict[str, Any],
) -> None:
    """Review of the fix above, round 1: the read was an ``exclusive`` worker, so every
    tick cancelled the one still waiting and started another. A thread cannot be
    stopped: the cancelled read kept its thread and its answer was dropped, so while
    ``context.db`` stayed busy a read slower than the tick (a 5 s busy timeout against a
    2 s tick) was never painted, and a thread piled up per tick. A tick that finds a read
    waiting now leaves it be: its answer is painted, and the ticks it outlived come to ONE
    more read, started after it, so a change made meanwhile (a ▲ click's write) is still
    what the page shows next."""
    no_network["session"] = _session()
    hold = threading.Event()
    asked = {n: threading.Event() for n in range(1, 6)}
    gates = {n: threading.Event() for n in range(1, 6)}
    held: list[int] = []  # the reads that ran while the registry was held, numbered
    running, peak = [0], [0]
    counting = threading.Lock()

    def reader() -> AccountsOverview:
        with counting:
            n = 0
            if hold.is_set():
                held.append(len(held) + 1)
                n = held[-1]
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        try:
            if n:
                asked[n].set()
                gates[n].wait(10)  # as a writer holds the lock
            emails = ["me@example.com", *(f"{i}@example.com" for i in range(2, n + 2))]
            return _overview(*(_status(i, email) for i, email in enumerate(emails, 1)))
        finally:
            with counting:
                running[0] -= 1

    async def run() -> tuple[str, str, str]:
        app = FleetApp(refresh_seconds=3600, doctor=lambda: [], accounts=reader)
        async with app.run_test(size=SIZE) as pilot:
            await accounts_read(app)
            detail = app.query_one(AccountsSection).query_one(".accounts-line", Static)
            hold.set()
            try:
                app.refresh_data()  # a tick: the first held read waits on the registry
                assert await asyncio.to_thread(asked[1].wait, 5), "the tick asked"
                app.refresh_data()  # two more ticks while it waits
                app.refresh_data()
                await pilot.pause()
                while_held = shown(detail)
                gates[1].set()  # the writer lets go
                # The read the ticks were owed starts once the first one's answer is painted.
                assert await asyncio.to_thread(asked[2].wait, 5), "a read after the wait"
                await pilot.pause()
                answered = shown(detail)
                gates[2].set()
                await accounts_read(app)
            finally:
                for gate in gates.values():
                    gate.set()
            return while_held, answered, shown(detail)

    while_held, answered, after = asyncio.run(run())
    assert while_held == "1 Claude · me@example.com"  # the last frame, kept
    assert answered == "2 Claude · me@example.com"  # the waiting read's answer, painted
    assert after == "3 Claude · me@example.com"  # and the one read after it
    assert peak[0] == 1, f"{peak[0]} reads at once"
    assert held == [1, 2], held  # three ticks during one wait cost one more read


def test_buttons_follow_each_slots_state() -> None:
    overview = _overview(
        _status(1, "me@example.com"), _status(2, "two@example.com"), _status(3, None)
    )

    async def go(pilot: Pilot[None]) -> dict[int, tuple[bool, bool]]:
        view = await open_accounts(pilot)
        return {
            slot: (
                row(view, slot).query_one(f"#account-sign-in-{slot}", Button).display,
                row(view, slot).query_one(f"#account-remove-{slot}", Button).display,
            )
            for slot in (1, 2, 3)
        }

    buttons = drive(go, overview=overview)
    assert buttons[1] == (False, False)  # signed in, and never ours to remove
    assert buttons[2] == (False, True)  # signed in, ours
    assert buttons[3] == (True, True)  # needs a sign-in, ours


def test_without_claude_code_the_page_says_how_to_install_and_add_is_disabled() -> None:
    overview = _overview(_status(1, None, signed_in=False), installed=False)

    async def go(pilot: Pilot[None]) -> tuple[str, bool, str, str]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        return (
            shown(view.query_one("#claude-title", Static)),
            view.query_one("#claude-add", Button).disabled,
            shown(app.query_one(AccountsTitle)),
            shown(app.query_one(AccountsSection).query_one(".accounts-line", Static)),
        )

    claude, disabled, title, detail = drive(go, overview=overview)
    assert "Claude Code is not installed" in claude and core.INSTALL_COMMAND in claude
    assert disabled
    assert title == "Accounts  ✗ AISquare" and detail == "⚠ Claude Code not installed"


def test_usage_is_fetched_only_for_signed_in_slots_and_painted_into_their_rows(
    no_network: dict[str, Any],
) -> None:
    no_network["usage"] = lambda account: ClaudeUsage(
        available=True,
        session_percent=12 if account.slot == 1 else 90,
        week_percent=40,
        session_resets_at=NOW,
    )
    overview = _overview(
        _status(1, "me@example.com"), _status(2, "two@example.com"), _status(3, None)
    )

    async def go(pilot: Pilot[None]) -> tuple[list[int], str, str, str]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await settle(app)
        await pilot.pause()
        return sorted(no_network["usage_calls"]), line(view, 1), line(view, 2), line(view, 3)

    calls, first, second, third = drive(go, overview=overview)
    assert calls == [1, 2]  # slot 3 has no token to ask with
    assert "session ▮▯▯▯▯ 12%" in first and "week ▮▮▯▯▯ 40%" in first
    assert "session ▮▮▮▮▮ 90%" in second
    assert "▮" not in third and "not signed in" in third


def test_usage_that_cannot_be_read_says_why_on_the_row(no_network: dict[str, Any]) -> None:
    no_network["usage"] = ClaudeUsage(available=False, reason="the stored token has expired")

    async def go(pilot: Pilot[None]) -> str:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await settle(app)
        await pilot.pause()
        return line(view, 1)

    assert "usage: the stored token has expired" in drive(go)


def test_a_keychain_backed_account_is_asked_and_its_row_says_why(
    no_network: dict[str, Any],
) -> None:
    """macOS: signed in, no credentials file — the service's reason must reach the row."""
    no_network["usage"] = ClaudeUsage(
        available=False, reason="credentials are in the macOS Keychain, which the CLI does not read"
    )
    overview = _overview(_status(1, "me@example.com", token_state="missing"))

    async def go(pilot: Pilot[None]) -> tuple[list[int], str]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await settle(app)
        await pilot.pause()
        return no_network["usage_calls"], line(view, 1)

    calls, first = drive(go, overview=overview)
    assert calls == [1]  # asked, although its token state is "missing"
    assert "usage: credentials are in the macOS Keychain" in first
    assert "usage: …" not in first


# --- AISquare: the device flow as a card --------------------------------------------------------


def _script_device_flow(
    monkeypatch: pytest.MonkeyPatch,
    script: dict[str, Any],
    *,
    outcome: dict[str, Any] | Exception,
    unrestricted: bool = False,
) -> list[str]:
    """Discovery, the grant and the wait, scripted; what the card asked for is recorded.

    A completed sign-in STORES the session, and the page re-reads the store on
    every frame — so the scripted completion sets what ``current_session`` will
    answer next, as the real one does. ``unrestricted`` is the store's answer
    that the credentials file could not be restricted to this account.
    """
    seen: list[str] = []
    endpoints = iam.Endpoints(
        issuer="https://api.aisquare.studio/o",
        device_authorization="https://api.aisquare.studio/o/device-authorization/",
        token="https://api.aisquare.studio/o/token/",
        userinfo="https://api.aisquare.studio/o/userinfo/",
        revocation="https://api.aisquare.studio/o/revoke_token/",
    )
    grant = iam.DeviceAuthorization(
        device_code="dev",
        user_code="WDJB-MJHT",
        verification_uri="https://home.aisquare.studio/cli",
        verification_uri_complete="https://home.aisquare.studio/cli?code=WDJB-MJHT",
        expires_in=900,
        interval=5,
    )
    monkeypatch.setattr(iam, "resolve_api_url", lambda explicit=None: "https://api.aisquare.studio")
    monkeypatch.setattr(iam, "discover", lambda api_url, refresh=False: endpoints)
    monkeypatch.setattr(iam, "start_device_authorization", lambda e: grant)

    def wait(e: iam.Endpoints, g: iam.DeviceAuthorization, *, cancelled: Any) -> dict[str, Any]:
        seen.append(f"wait:{g.user_code}")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(device_flow, "wait_for_token", wait)

    def commit(
        api_url: str, e: iam.Endpoints, token: dict[str, Any], *, cancelled: Any
    ) -> iam.Session:
        seen.append(f"complete:{token['access_token']}")
        session = dataclasses.replace(_session("new@aisquare.studio"), unrestricted=unrestricted)
        script["session"] = session
        return session

    monkeypatch.setattr(device_flow, "commit_sign_in", commit)
    return seen


def test_sign_in_shows_the_code_then_the_new_session(
    monkeypatch: pytest.MonkeyPatch, no_network: dict[str, Any]
) -> None:
    seen = _script_device_flow(
        monkeypatch, no_network, outcome={"access_token": "aisq_new", "expires_in": 1}
    )

    async def go(pilot: Pilot[None]) -> tuple[str, str, bool, str]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        assert not view.query_one("#aisquare-sign-out", Button).display  # nobody to sign out
        await pilot.click("#aisquare-sign-in")
        await pilot.pause()
        await settle(app)
        await pilot.pause()
        return (
            shown(view.query_one("#aisquare-status", Static)),
            notice(view),
            view.query_one("#aisquare-code", Static).display,
            shown(app.query_one(AccountsTitle)),
        )

    status, said, code_shown, title = drive(go)
    assert seen == ["wait:WDJB-MJHT", "complete:aisq_new"]
    # The browser helper decides (an explicit BROWSER beats the headless heuristics),
    # so it is asked even where is_headless() says no.
    assert no_network["opened"] == ["https://home.aisquare.studio/cli?code=WDJB-MJHT"]
    assert "Signed in as new@aisquare.studio" in status
    assert said == "✓ Signed in to AISquare as new@aisquare.studio"
    assert not code_shown  # the card folds away once the session is stored
    assert "✓ AISquare" in title  # the section was re-read from the stored session


def test_a_sign_in_whose_token_could_not_be_restricted_says_so_on_the_page(
    monkeypatch: pytest.MonkeyPatch, no_network: dict[str, Any]
) -> None:
    """The service's warning is a line on stderr, and Textual captures stderr while it runs,
    so a Windows sign-in from this page looked clean with the token readable by other
    accounts (review of #65, R7). The page reads the flag the stored session carries."""
    _script_device_flow(
        monkeypatch,
        no_network,
        outcome={"access_token": "aisq_new", "expires_in": 1},
        unrestricted=True,
    )

    async def go(pilot: Pilot[None]) -> str:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await pilot.click("#aisquare-sign-in")
        await pilot.pause()
        await settle(app)
        await pilot.pause()
        return notice(view)

    said = drive(go)
    assert said.startswith("✓ Signed in to AISquare as new@aisquare.studio, but could not"), said
    assert "other users on this machine may be able to read your session token" in said


def test_a_cancelled_or_denied_sign_in_stores_nothing_and_says_so(
    monkeypatch: pytest.MonkeyPatch, no_network: dict[str, Any]
) -> None:
    seen = _script_device_flow(
        monkeypatch, no_network, outcome=iam.IamError("cancelled", "Sign-in cancelled.")
    )

    async def go(pilot: Pilot[None]) -> tuple[str, str, bool]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await pilot.click("#aisquare-sign-in")
        await pilot.pause()
        await settle(app)
        await pilot.pause()
        return (
            notice(view),
            shown(view.query_one("#aisquare-status", Static)),
            view.query_one("#aisquare-sign-in", Button).display,
        )

    said, status, sign_in_back = drive(go)
    assert seen == ["wait:WDJB-MJHT"]  # complete_sign_in was never reached
    assert said == "Sign-in cancelled. Nothing was stored."
    assert status.startswith("Not signed in")
    assert sign_in_back


def test_a_denied_sign_in_shows_the_providers_reason(
    monkeypatch: pytest.MonkeyPatch, no_network: dict[str, Any]
) -> None:
    _script_device_flow(
        monkeypatch,
        no_network,
        outcome=iam.IamError("access_denied", "The request was denied in the browser."),
    )

    async def go(pilot: Pilot[None]) -> str:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await pilot.click("#aisquare-sign-in")
        await settle(app)
        await pilot.pause()
        return notice(view)

    assert drive(go) == "✗ The request was denied in the browser."


def test_sign_out_revokes_and_forgets(
    monkeypatch: pytest.MonkeyPatch, no_network: dict[str, Any]
) -> None:
    no_network["session"] = _session()
    revoked: list[str] = []

    def sign_out(session: iam.Session) -> auth_service.SignedOut:
        revoked.append(session.email)
        no_network["session"] = None
        return auth_service.SignedOut(revoked=True, restricted=True)

    monkeypatch.setattr(auth_service, "sign_out", sign_out)

    async def go(pilot: Pilot[None]) -> tuple[str, str, bool]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await pilot.click("#aisquare-sign-out")
        await settle(app)
        await pilot.pause()
        return (
            notice(view),
            shown(view.query_one("#aisquare-status", Static)),
            view.query_one("#aisquare-sign-out", Button).display,
        )

    said, status, sign_out_shown = drive(go)
    assert revoked == ["me@aisquare.studio"]
    assert said == "✓ Signed out of AISquare"
    assert status.startswith("Not signed in") and not sign_out_shown


def test_sign_out_takes_the_keys_the_cli_minted_with_it_as_logout_does(
    monkeypatch: pytest.MonkeyPatch, no_network: dict[str, Any], tmp_path: Path
) -> None:
    """*Sign out* revoked the session alone, so every ingest key the CLI had minted
    (#142) outlived the sign-in that obtained it — bound, in its file, and live on the
    server — while ``aisquare logout`` forgot them (review of #172). They go first,
    their revoke taking the session's Bearer, and one the server refuses is said and
    stays owed."""
    from aisquare.core.workspace import project_id_for
    from aisquare.models import ProjectInfo
    from aisquare.services import destinations
    from aisquare.services import explainability as explainability_service

    session = _session()
    no_network["session"] = session
    projects = [
        ProjectInfo(id=project_id_for(tmp_path / name), root=tmp_path / name)
        for name in ("web", "api")
    ]
    with store_session() as store:
        for project, uid in zip(projects, ("key-web", "key-api"), strict=True):
            destinations.choose(
                store,
                project,
                destinations.Workspace(id=42, uid="ws-uid-42", name="acme", role="ADMIN"),
                destinations.Studio(id=301, uid="st-301", name="Frontend"),
                session,
            )
            path = explainability_service.store_project_api_key(project.id, f"AIS_{uid}")
            store.set_project_explainability(
                project.id, target="prod", key_path=path, set_by=None, minted=uid
            )
    order: list[str] = []

    def revoke(path: str, **kwargs: Any) -> iam.HttpResult:
        if not path.endswith("/revoke/"):  # the page's credits reading
            return iam.HttpResult(404, {"detail": "Not found."}, {})
        order.append(path.split("/")[-3])
        return iam.HttpResult(403 if "key-api" in path else 204, {"detail": "not yours"}, {})

    def sign_out(session: iam.Session) -> auth_service.SignedOut:
        order.append("session")
        no_network["session"] = None
        return auth_service.SignedOut(revoked=True, restricted=True)

    monkeypatch.setattr(iam, "request", revoke)
    monkeypatch.setattr(auth_service, "sign_out", sign_out)

    async def go(pilot: Pilot[None]) -> str:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await pilot.click("#aisquare-sign-out")
        await settle(app)
        await pilot.pause()
        return notice(view)

    said = drive(go)
    assert sorted(order[:2]) == ["key-api", "key-web"], order
    assert order[2:] == ["session"], "the keys go before the session: their revoke takes it"
    assert said.startswith("✓ Signed out of AISquare; 1 key the CLI minted is still live"), said
    assert "acme for api (the API answered HTTP 403: not yours)" in said
    assert not any(
        explainability_service.project_key_path(project.id).exists() for project in projects
    )
    with store_session() as store:
        assert [record.key_uid for record in store.pending_revocations()] == ["key-api"]
        assert store.project_explainability_all() == []


def test_a_sign_out_whose_rewrite_could_not_be_restricted_says_so_on_the_page(
    monkeypatch: pytest.MonkeyPatch, no_network: dict[str, Any]
) -> None:
    """Signing out rewrites the credentials file that still holds the API key and the serve
    token, as a new file. One that could not be restricted was reported on stderr only, which
    Textual captures, so the page said "✓ Signed out" as ok (review of the #65 fold, round 2,
    F3). The real sign-out runs here, with the server out of reach and the restriction refused,
    and the page reads the stored session back from the file."""
    api_key = "-".join(["not", "a", "real", "key"])
    credentials.store(api_key=api_key)
    iam.store_session(
        api_url="https://api.aisquare.studio",
        token="aisq_secret",
        expires_in=3600,
        scope="",
        claims={"email": "me@aisquare.studio", "sub": "usr_1"},
    )
    monkeypatch.setattr(iam, "current_session", lambda api_url=None: iam.stored_session())

    def unreachable(api_url: str) -> iam.Endpoints:
        raise iam.IamError("unreachable", "The identity provider could not be reached.")

    monkeypatch.setattr(iam, "discover", unreachable)
    monkeypatch.setattr(paths, "restrict_to_owner", lambda path: False)

    async def go(pilot: Pilot[None]) -> tuple[str, str]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await pilot.click("#aisquare-sign-out")
        await settle(app)
        await pilot.pause()
        return notice(view), shown(view.query_one("#aisquare-status", Static))

    said, status = drive(go)
    assert said.startswith(
        "✓ Signed out of AISquare (locally — the server could not be reached to revoke), "
        "but could not restrict"
    ), said
    assert "other users on this machine may be able to read the credentials left in it" in said
    assert status.startswith("Not signed in")
    assert credentials.load_all() == {"api_key": api_key}


def test_an_environment_token_can_neither_sign_out_nor_start_a_browser_sign_in(
    monkeypatch: pytest.MonkeyPatch, no_network: dict[str, Any]
) -> None:
    no_network["session"] = _session(source="env")
    seen = _script_device_flow(monkeypatch, no_network, outcome={"access_token": "aisq_new"})

    async def go(pilot: Pilot[None]) -> tuple[bool, bool, bool, str, str, list[str]]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        sign_out = view.query_one("#aisquare-sign-out", Button)
        sign_in = view.query_one("#aisquare-sign-in", Button)
        view._start_sign_in()  # the handler itself, past a disabled button
        await pilot.pause()
        await settle(app)
        return (
            sign_out.display,
            sign_out.disabled,
            sign_in.disabled,
            shown(view.query_one("#aisquare-status", Static)),
            notice(view),
            [worker.name for worker in app.workers if worker.name == SIGN_IN_WORKER],
        )

    shown_, out_disabled, in_disabled, status, said, workers = drive(go)
    assert shown_ and out_disabled and in_disabled
    assert iam.TOKEN_ENV_VAR in status
    assert said.startswith(f"{iam.TOKEN_ENV_VAR} is set") and "Unset it" in said
    assert workers == [] and seen == []  # no flow ran, nothing was replaced


def test_quitting_mid_sign_in_cancels_the_device_flow(
    monkeypatch: pytest.MonkeyPatch, no_network: dict[str, Any]
) -> None:
    """Textual cancelling a thread worker does not stop its callable; the page's own flag must."""
    seen = _script_device_flow(monkeypatch, no_network, outcome={"access_token": "aisq_new"})
    released = threading.Event()
    observed: dict[str, bool] = {}

    def wait(e: iam.Endpoints, g: iam.DeviceAuthorization, *, cancelled: Any) -> dict[str, Any]:
        deadline = time.monotonic() + 5
        while not cancelled() and time.monotonic() < deadline:
            time.sleep(0.02)
        observed["cancelled"] = cancelled()
        released.set()
        raise iam.IamError("cancelled", "Sign-in cancelled. Nothing was stored.")

    monkeypatch.setattr(device_flow, "wait_for_token", wait)

    async def go(pilot: Pilot[None]) -> None:
        await open_accounts(pilot)
        await pilot.click("#aisquare-sign-in")
        await pilot.pause()

    drive(go)  # the app exits here: the view unmounts while the wait is in flight

    assert released.wait(5), "the wait never noticed the page had gone"
    assert observed["cancelled"] is True
    assert seen == []  # complete_sign_in never ran: nothing was stored


# --- Claude Code: a sign-in window, watched ----------------------------------------------------


def _script_claude_sign_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, lands: bool
) -> dict[str, Any]:
    """A fresh slot whose sign-in either lands on the first poll or never does."""
    seen: dict[str, Any] = {"opened": [], "completed": [], "abandoned": [], "landed_polls": 0}
    account = ClaudeAccount(
        slot=2,
        config_dir=tmp_path / "claude-accounts" / "2",
        tmp_dir=tmp_path / "cache" / "2",
        managed=True,
    )
    account.config_dir.mkdir(parents=True)

    def begin(slot: int | None) -> ClaudeAccount:
        seen["begin"] = slot
        return account

    def open_window(
        acct: ClaudeAccount, server: TmuxServer, *, cwd: Path | None = None
    ) -> WindowInfo:
        seen["opened"].append((acct.slot, server.socket, cwd))
        return WindowInfo(
            session=accounts_service.SIGN_IN_SESSION,
            window_id="@7",
            name="account-2",
            pane_id="%7",
            dead=False,
            dead_status=None,
            current_command="python",
            activity=False,
        )

    def landed(acct: ClaudeAccount) -> ClaudeIdentity | None:
        seen["landed_polls"] += 1
        return ClaudeIdentity(email="two@example.com") if lands else None

    def complete(acct: ClaudeAccount) -> ClaudeAccountStatus:
        seen["completed"].append(acct.slot)
        return _status(2, "two@example.com")

    def abandon(acct: ClaudeAccount) -> bool:
        seen["abandoned"].append(acct.slot)
        return True

    monkeypatch.setattr(accounts_service, "begin_sign_in", begin)
    monkeypatch.setattr(accounts_service, "open_sign_in_window", open_window)
    monkeypatch.setattr(accounts_service, "sign_in_landed", landed)
    monkeypatch.setattr(accounts_service, "complete_sign_in", complete)
    monkeypatch.setattr(accounts_service, "abandon_sign_in", abandon)
    return seen


def test_add_opens_a_watched_window_and_records_the_account_when_the_login_lands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_real_tmux: list[tuple[str, ...]]
) -> None:
    seen = _script_claude_sign_in(monkeypatch, tmp_path, lands=True)

    async def go(pilot: Pilot[None]) -> tuple[bool, str | None, str, bool, str]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await pilot.click("#claude-add")
        await pilot.pause()
        box = view.query_one("#login-box", Vertical)
        pane = view.query_one("#login-pane", TerminalPane)
        box_shown, attached = box.display, pane.pane_id
        header = shown(view.query_one("#login-header", Static))
        assert view.login is not None and view.login.fresh
        view._poll_login()  # the timer's tick, taken by hand
        await pilot.pause()
        await settle(app)
        await pilot.pause()
        return box_shown, attached, header, box.display, notice(view)

    box_shown, attached, header, box_after, said = drive(go)
    assert seen["begin"] is None  # + means a fresh slot
    assert seen["opened"] == [(2, PRIVATE_SOCKET, None)]
    assert box_shown and attached == "%7"
    assert header.startswith("Signing in to account 2")
    assert seen["landed_polls"] == 1 and seen["completed"] == [2] and seen["abandoned"] == []
    assert not box_after  # the window is closed the moment the login lands…
    killed = [argv for argv in no_real_tmux if "kill-window" in argv]
    assert killed and killed[0][killed[0].index("-t") + 1] == "%7"  # …and killed in tmux
    assert said.startswith("✓ account 2: two@example.com")


def test_a_window_that_closes_without_a_login_discards_the_fresh_slot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _script_claude_sign_in(monkeypatch, tmp_path, lands=False)

    async def go(pilot: Pilot[None]) -> tuple[str, bool]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await pilot.click("#claude-add")
        await pilot.pause()
        # The recorder answers "no server" to display-message, which is a pane that is gone.
        view._poll_login()
        await pilot.pause()
        await settle(app)
        return notice(view), view.query_one("#login-box", Vertical).display

    said, box = drive(go)
    assert seen["completed"] == [] and seen["abandoned"] == [2]
    assert said.startswith("sign-in cancelled") and "closed before a sign-in landed" in said
    assert "the new slot was discarded" in said
    assert not box


def test_cancel_stops_a_sign_in_and_a_sign_in_of_an_existing_slot_is_never_discarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _script_claude_sign_in(monkeypatch, tmp_path, lands=False)
    overview = _overview(_status(1, "me@example.com"), _status(2, None))

    async def go(pilot: Pilot[None]) -> tuple[int | None, str]:
        view = await open_accounts(pilot)
        await pilot.click("#account-sign-in-2")
        await pilot.pause()
        begun = seen.get("begin", "never")
        assert view.login is not None and not view.login.fresh
        await pilot.click("#login-cancel")
        await pilot.pause()
        return begun, notice(view)

    begun, said = drive(go, overview=overview)
    assert begun == 2  # the row's button signs THAT slot in
    assert seen["abandoned"] == []  # an existing slot is kept, login or not
    assert said.startswith("sign-in cancelled — cancelled; nothing changed")


def test_quitting_mid_claude_sign_in_closes_the_window_and_discards_the_fresh_slot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_real_tmux: list[tuple[str, ...]]
) -> None:
    seen = _script_claude_sign_in(monkeypatch, tmp_path, lands=False)

    async def go(pilot: Pilot[None]) -> None:
        view = await open_accounts(pilot)
        await pilot.click("#claude-add")
        await pilot.pause()
        assert view.login is not None

    drive(go)  # the app exits with the sign-in window still open

    killed = [argv for argv in no_real_tmux if "kill-window" in argv]
    assert killed and killed[0][killed[0].index("-t") + 1] == "%7"
    assert seen["abandoned"] == [2] and seen["completed"] == []


def test_quitting_after_the_login_landed_records_it_instead(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _script_claude_sign_in(monkeypatch, tmp_path, lands=False)
    landed = {"now": False}
    monkeypatch.setattr(
        accounts_service,
        "sign_in_landed",
        lambda acct: ClaudeIdentity(email="two@example.com") if landed["now"] else None,
    )

    async def go(pilot: Pilot[None]) -> None:
        view = await open_accounts(pilot)
        await pilot.click("#claude-add")
        await pilot.pause()
        assert view.login is not None
        landed["now"] = True  # the login lands, and the user quits before the next poll

    drive(go)

    assert seen["completed"] == [2] and seen["abandoned"] == []


def test_remove_runs_the_service_and_reports_where_the_directory_went(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    removed: list[int] = []

    def remove(account: ClaudeAccount, *, notes: list[str] | None = None) -> Path:
        removed.append(account.slot)
        if notes is not None:  # what the service says about a binding that named the slot
            notes.append("role coder was bound to slot 2; it now names two@example.com — …")
        return tmp_path / "2.removed-20260909T120000Z"

    monkeypatch.setattr(accounts_service, "remove", remove)
    monkeypatch.setattr(core, "find_account", lambda slot: _status(slot, "x@y").account)
    overview = _overview(_status(1, "me@example.com"), _status(2, "two@example.com"))

    async def go(pilot: Pilot[None]) -> str:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await pilot.click("#account-remove-2")
        await settle(app)
        await pilot.pause()
        return notice(view)

    said = drive(go, overview=overview)
    assert removed == [2]
    assert said.startswith("✓ removed") and "2.removed-20260909T120000Z" in said
    assert "role coder was bound to slot 2; it now names two@example.com" in said  # third round


# --- arranging (#145): the default badge, the order arrows, disable -----------------------------


def _arranged(
    status: ClaudeAccountStatus,
    *,
    is_default: bool = False,
    disabled: bool = False,
    alias: str | None = None,
    position: int | None = None,
) -> ClaudeAccountStatus:
    """``status`` with its registry arrangement set, as ``services.claude_accounts`` folds it."""
    account = status.account.model_copy(
        update={
            "is_default": is_default,
            "disabled": disabled,
            "alias": alias,
            "position": position,
        }
    )
    return status.model_copy(update={"account": account, "label": core.label(account)})


def test_the_default_is_starred_named_by_its_alias_and_its_row_hides_the_default_button() -> None:
    overview = _overview(
        _arranged(_status(2, "two@example.com"), is_default=True, alias="work", position=1),
        _arranged(_status(1, "me@example.com"), position=2),
        _arranged(_status(3, "three@example.com"), disabled=True, position=3),
    )

    async def go(pilot: Pilot[None]) -> tuple[list[str], dict[int, tuple[bool, bool, bool, str]]]:
        view = await open_accounts(pilot)
        order = [r.slot for r in view.rows()]
        buttons = {
            slot: (
                row(view, slot).query_one(f"#account-default-{slot}", Button).display,
                row(view, slot).query_one(f"#account-up-{slot}", Button).disabled,
                row(view, slot).query_one(f"#account-down-{slot}", Button).disabled,
                str(row(view, slot).query_one(f"#account-toggle-{slot}", Button).label),
            )
            for slot in (2, 1, 3)
        }
        return [line(view, slot) for slot in order], buttons

    lines, buttons = drive(go, overview=overview)
    assert lines[0].startswith("★ 2  work") and "two@example.com" in lines[0]  # the default, first
    assert lines[1].startswith("  1  plain claude")  # no star: not the default
    assert lines[2].startswith("  3  account 3") and "disabled" in lines[2]
    assert buttons[2] == (False, True, False, "Disable")  # default hidden; first: ↑ disabled
    assert buttons[1] == (True, False, False, "Disable")  # middle: both arrows live
    assert buttons[3] == (True, False, True, "Enable")  # last: ↓ disabled; disabled → Enable


def test_default_move_and_disable_buttons_write_through_the_service_and_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row never paints optimistically: the service is called, the shell re-reads."""
    calls: list[tuple[str, ...]] = []
    frames: list[AccountsOverview] = [
        _overview(_status(1, "me@example.com"), _status(2, "two@example.com")),
        _overview(
            _arranged(_status(2, "two@example.com"), is_default=True, position=1),
            _arranged(_status(1, "me@example.com"), position=2),
        ),
    ]

    def set_default(ref: str | None, *, project: Any = None) -> Any:
        calls.append(("default", str(ref)))
        return None

    def move(ref: str, direction: str) -> list[Any]:
        calls.append(("move", ref, direction))
        return []

    def set_disabled(ref: str, disabled: bool) -> Any:
        calls.append(("disable", ref, str(disabled)))
        raise accounts_service.AccountsUnreadable("the accounts registry cannot be written (x)")

    monkeypatch.setattr(accounts_service, "set_default", set_default)
    monkeypatch.setattr(accounts_service, "move", move)
    monkeypatch.setattr(accounts_service, "set_disabled", set_disabled)

    async def go(pilot: Pilot[None]) -> tuple[str, list[int], str]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await pilot.click("#account-default-2")
        await settle(app)
        await pilot.pause()
        after_default = notice(view)
        # The shell's next frame is the arranged one; the page follows it.
        frames.pop(0)
        app.refresh_accounts()
        await accounts_read(app)
        order = [r.slot for r in view.rows()]
        await pilot.click("#account-down-2")
        await settle(app)
        await pilot.click("#account-toggle-1")
        await settle(app)
        await pilot.pause()
        return after_default, order, notice(view)

    async def run() -> tuple[str, list[int], str]:
        app = FleetApp(refresh_seconds=3600, doctor=lambda: [], accounts=lambda: frames[0])
        async with app.run_test(size=SIZE) as pilot:
            await accounts_read(app)
            return await go(pilot)

    after_default, order, last = asyncio.run(run())
    assert after_default == "✓ slot 2 is the machine default"
    assert order == [2, 1]  # the page re-read the shell's frame: the default moved up
    assert ("default", "2") in calls
    assert ("move", "2", "down") in calls
    assert ("disable", "1", "True") in calls
    assert last.startswith("✗ the accounts registry cannot be written")  # the error, not a crash


def test_an_arrange_notice_names_the_action_its_own_worker_finished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The notice was a field on the page, so a second click overwrote it before the first
    worker's report was handled — a thread worker ``exclusive`` cancels still finishes —
    and the first one's success was announced as the second action (review of #205,
    fourth round). The text now travels with the work, as the worker's result."""
    release = threading.Event()

    async def go(pilot: Pilot[None]) -> tuple[str, str]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        workers: list[Worker[Any]] = []
        real_run = view.run_worker

        def spy(*args: Any, **kwargs: Any) -> Worker[Any]:
            worker = real_run(*args, **kwargs)
            workers.append(worker)
            return worker

        monkeypatch.setattr(view, "run_worker", spy)
        view.arrange_accounts(lambda: None, done="✓ slot 2 moved down in the priority order")
        await settle(app)
        await pilot.pause()
        first = notice(view)
        # The second click, still running when the first worker's report is handled.
        view.arrange_accounts(release.wait, done="✓ slot 1 disabled — never picked automatically")
        view._arrange_finished(workers[0], WorkerState.SUCCESS)
        late = notice(view)
        release.set()
        await settle(app)
        return first, late

    first, late = drive(go)
    assert first == "✓ slot 2 moved down in the priority order"
    assert late == first  # the first worker's own action, not the click that came after


def test_two_quick_arrange_clicks_write_in_turn_and_both_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``exclusive`` cancelled the earlier thread worker on paper only — a thread runs to its
    end — so two quick ▲ clicks ran two read-then-write ``move`` calls at once, one step was
    lost, and the cancelled worker's notice never showed (review of the #205 fold, round 1).
    The writes now take their turn, and each worker reports its own action."""
    first_in = threading.Event()
    release = threading.Event()
    inside: list[str] = []
    overlapped: list[bool] = []

    def write(name: str, *, hold: bool) -> Callable[[], None]:
        def run() -> None:
            overlapped.append(bool(inside))
            inside.append(name)
            if hold:
                first_in.set()
                release.wait(timeout=5)
            inside.remove(name)

        return run

    async def go(pilot: Pilot[None]) -> list[str]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        said: list[str] = []
        real_notice = view._notice

        def spy(text: str, tone: str = "dim") -> None:
            said.append(text)
            real_notice(text, tone)

        monkeypatch.setattr(view, "_notice", spy)
        view.arrange_accounts(write("first", hold=True), done="✓ slot 3 moved up once")
        assert await asyncio.to_thread(first_in.wait, 5)  # the first write is under way…
        view.arrange_accounts(write("second", hold=False), done="✓ slot 3 moved up twice")
        await asyncio.sleep(0.2)  # …and the second click's thread has had its chance to start
        release.set()
        await settle(app)
        await pilot.pause()
        return said

    said = drive(go)
    assert overlapped == [False, False]  # never two writes at once
    assert [text for text in said if text.startswith("✓")] == [
        "✓ slot 3 moved up once",
        "✓ slot 3 moved up twice",
    ]


# --- the pace of the five-hour window (#146) ------------------------------------------------------


def test_the_row_says_how_long_the_window_has_at_the_current_pace(
    no_network: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page's minute tick RECORDS each reading; the trend it computes is painted dim."""
    from aisquare.models import UsageTrend

    usage = ClaudeUsage(
        available=True,
        session_percent=60,
        session_resets_at=NOW + timedelta(hours=3),
        week_percent=20,
        week_resets_at=NOW + timedelta(days=3),
        fetched_at=NOW,
    )
    no_network["usage"] = usage
    recorded: list[int] = []
    trends: dict[int, UsageTrend | None] = {
        1: UsageTrend(percent=60, per_hour=30.0, minutes_to_limit=80.0, span_minutes=20.0),
        2: None,
    }

    real_usage = accounts_service.usage

    def usage_spy(account: ClaudeAccount, **kwargs: Any) -> ClaudeUsage:
        recorded.append(account.slot)
        return real_usage(account, **kwargs)

    # The tick fetches through `read_usage(record=False)` and records in one store
    # session of its own (third round): the reading is spied, the sample is checked below.
    monkeypatch.setattr(accounts_service, "usage", usage_spy)
    monkeypatch.setattr(
        accounts_service, "usage_trend", lambda slot, latest, **kwargs: trends.get(slot)
    )
    monkeypatch.setattr(accounts_service, "_now", lambda: NOW)
    overview = _overview(_status(1, "me@example.com"), _status(2, "two@example.com"))

    async def go(pilot: Pilot[None]) -> tuple[str, str]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await settle(app)
        await pilot.pause()
        return line(view, 1), line(view, 2)

    one, two = drive(go, overview=overview)
    assert sorted(recorded) == [1, 2]  # every signed-in slot was read…
    with store_session() as store:  # …and SAMPLED, in the page's one store open
        assert len(store.usage_samples(1, since=NOW - timedelta(days=1))) == 1
        assert len(store.usage_samples(2, since=NOW - timedelta(days=1))) == 1
    assert "session ▮▮▮▯▯ 60%" in one and "≈ 1.3 h to the limit" in one
    assert "session ▮▮▮▯▯ 60%" in two and "to the limit" not in two  # no trend yet: no claim


def test_a_row_that_leaves_takes_its_projection_with_it(
    no_network: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``self.trends`` was not pruned beside ``self.usage`` (review of #205, second round):
    after a remove-then-add the newcomer in the re-used slot painted the old projection."""
    from aisquare.models import UsageTrend

    no_network["usage"] = ClaudeUsage(
        available=True, session_percent=60, session_resets_at=NOW + timedelta(hours=3)
    )
    monkeypatch.setattr(
        accounts_service,
        "usage_trend",
        lambda slot, latest, **kwargs: UsageTrend(
            percent=60, per_hour=30.0, minutes_to_limit=80.0, span_minutes=20.0
        ),
    )
    monkeypatch.setattr(accounts_service, "_now", lambda: NOW)
    both = _overview(_status(1, "me@example.com"), _status(2, "two@example.com"))

    async def go(pilot: Pilot[None]) -> tuple[set[int], set[int], set[int], set[int]]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await settle(app)
        await pilot.pause()
        before = (set(view.usage), set(view.trends))
        view.show(_overview(_status(1, "me@example.com")))  # slot 2 removed
        await pilot.pause()
        return before[0], before[1], set(view.usage), set(view.trends)

    usage_before, trends_before, usage_after, trends_after = drive(go, overview=both)
    assert usage_before == trends_before == {1, 2}
    assert usage_after == trends_after == {1}
