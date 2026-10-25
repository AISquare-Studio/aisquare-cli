"""The Accounts page with Codex slots: their own table, offline usage bars, ``codex login``.

The shell and the tmux recorder are ``test_ui_accounts.py``'s (its ``no_real_tmux``
fixture is imported, so no window here reaches a real fleet). The Codex slots are
REAL directories under the suite's own home, with a synthetic ``auth.json`` (an
unsigned token whose payload carries only identity claims) and a synthetic
rollout: the page reads them through the real service, which never asks the
network for a Codex account — ``_http_get`` raises here if anything does.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from textual.containers import Vertical
from textual.pilot import Pilot
from textual.widgets import Button, Static

from aisquare.cli.ui.views.accounts import (
    account_cells,
    agent_section_shown,
    reading_age,
)
from aisquare.core import claude_accounts as core
from aisquare.core.tmux import TmuxServer, WindowInfo
from aisquare.models import (
    AccountsOverview,
    ClaudeAccount,
    ClaudeAccountStatus,
    ClaudeIdentity,
    ClaudeInstall,
    ClaudeUsage,
)
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import iam
from tests.test_ui_accounts import (  # noqa: F401 — `no_real_tmux` is an autouse fixture
    PRIVATE_SOCKET,
    cells,
    drive,
    fleet_app,
    line,
    no_real_tmux,
    notice,
    open_accounts,
    painted,
    press,
    row,
    settle,
    shown,
)
from tests.ui_workers import settle_until

CODEX = "codex"
NOW = datetime.now(tz=UTC).replace(microsecond=0)


# --- synthetic Codex homes ---------------------------------------------------------------------


def _unsigned_token(claims: Mapping[str, Any]) -> str:
    """``header.payload.signature`` — only the payload means anything, and only as claims."""

    def part(value: Mapping[str, Any]) -> str:
        raw = json.dumps(value).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.not-a-signature"


def _sign_in_codex(home: Path, email: str, plan: str = "plus") -> None:
    """What ``codex login`` leaves: auth.json with tokens whose values mean nothing here."""
    claims = {
        "email": email,
        "https://api.openai.com/auth": {
            "chatgpt_plan_type": plan,
            "chatgpt_account_id": "acct-test",
        },
    }
    auth = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": _unsigned_token(claims),
            "access_token": "test-access",
            "refresh_token": "test-refresh",
            "account_id": "acct-test",
        },
        "last_refresh": NOW.isoformat(),
    }
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps(auth), encoding="utf-8")


def _rollout(home: Path, *, recorded: datetime, session: float, week: float) -> Path:
    """A rollout whose newest ``token_count`` carries a five-hour and a weekly window, both
    still to reset when the test runs (the clock read then, not at import: a suite that
    reached the page an hour after collection read its reading an hour older)."""
    now = datetime.now(tz=UTC)
    day = home / "sessions" / f"{recorded:%Y}" / f"{recorded:%m}" / f"{recorded:%d}"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-{recorded:%Y-%m-%dT%H-%M-%S}-01a0-test.jsonl"
    records = [
        {"timestamp": recorded.isoformat(), "type": "session_meta", "payload": {}},
        {
            "timestamp": recorded.isoformat().replace("+00:00", "Z"),
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {
                    "limit_id": "codex",
                    "primary": {
                        "used_percent": session,
                        "window_minutes": 300,
                        "resets_at": int((now + timedelta(hours=2)).timestamp()),
                    },
                    "secondary": {
                        "used_percent": week,
                        "window_minutes": 10080,
                        "resets_at": int((now + timedelta(days=3)).timestamp()),
                    },
                    "credits": None,
                    "plan_type": "plus",
                    "rate_limit_reached_type": None,
                },
            },
        },
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


INSTALLED = {
    core.AGENT: ClaudeInstall(installed=True, binary="/opt/bin/claude", version="2.1.266"),
    CODEX: ClaudeInstall(installed=True, binary="/opt/bin/codex", version="0.162.1"),
}


def _real_overview() -> AccountsOverview:
    """The service's own overview of the suite's home, with installs that do not depend on
    what this machine has on its PATH."""
    overview = accounts_service.overview()
    return overview.model_copy(update={"claude": INSTALLED[core.AGENT], "installs": INSTALLED})


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """No AISquare session, no browser, and a usage endpoint that fails the test if asked."""
    asked: list[str] = []

    def no_request(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        asked.append(url)
        raise AssertionError(f"a Codex account's usage must never be fetched: {url}")

    monkeypatch.setattr(accounts_service, "_http_get", no_request)
    monkeypatch.setattr(iam, "current_session", lambda api_url=None: None)
    return asked


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every sign-in window the page opens, as the real ``open_sign_in_window`` asks for it."""
    opened: list[dict[str, Any]] = []

    def spawn_window(
        self: TmuxServer,
        session: str,
        *,
        name: str,
        cwd: Path,
        command: Sequence[str],
        env: Mapping[str, str] | None = None,
        **_: Any,
    ) -> WindowInfo:
        opened.append(
            {
                "socket": self.socket,
                "session": session,
                "name": name,
                "command": list(command),
                "env": dict(env or {}),
            }
        )
        return WindowInfo(
            session=session,
            window_id="@9",
            name=name,
            pane_id="%9",
            dead=False,
            dead_status=None,
            current_command="codex",
            activity=False,
        )

    monkeypatch.setattr(TmuxServer, "spawn_window", spawn_window)
    monkeypatch.setattr(
        accounts_service,
        "install",
        lambda agent=core.AGENT: INSTALLED[agent],
    )
    # Every tick taken by hand, as test_ui_accounts' sign-in tests take them.
    monkeypatch.setattr("aisquare.cli.ui.views.accounts.LOGIN_POLL_SECONDS", 3600.0)
    return opened


class _Alive:
    """A sign-in pane that is still running (``TmuxServer.pane_facts``'s answer)."""

    dead = False


# --- pure helpers ------------------------------------------------------------------------------


def _codex_status(
    slot: int, email: str | None, *, signed_in: bool | None = None
) -> ClaudeAccountStatus:
    account = ClaudeAccount(
        slot=slot,
        config_dir=Path("/h/.aisquare/codex-accounts") / str(slot) if slot else Path("/h/.codex"),
        managed=slot != 0,
        agent=CODEX,
    )
    identity = ClaudeIdentity(email=email) if email else None
    return ClaudeAccountStatus(
        account=account,
        label=core.label(account),
        identity=identity,
        signed_in=(identity is not None) if signed_in is None else signed_in,
        token_state="ok" if identity else "missing",
        subscription="plus" if identity else None,
        hooks_installed=True,
    )


def test_a_codex_row_says_how_old_its_reading_is_and_never_asks_to_open_a_session() -> None:
    reading = ClaudeUsage(
        available=True,
        session_percent=42,
        session_resets_at=NOW + timedelta(hours=2),
        week_percent=7,
        fetched_at=NOW - timedelta(hours=3),
    )
    shown_row = {
        key: text.plain
        for key, text in account_cells(_codex_status(4, "me@example.com"), reading, now=NOW).items()
    }
    assert (shown_row["slot"], shown_row["name"]) == ("4", "account 4")
    assert (shown_row["email"], shown_row["plan"]) == ("me@example.com", "plus")
    assert (shown_row["session"], shown_row["week"]) == ("▮▮▯▯▯ 42%", "▯▯▯▯▯ 7%")
    assert shown_row["status"] == "as of 3h ago"
    unfinished = account_cells(_codex_status(0, "me@example.com", signed_in=False), None, now=NOW)
    assert unfinished["name"].plain == "plain codex"
    assert unfinished["status"].plain == "⚠ login incomplete"  # never "token missing": Claude's
    # A Claude row never carries an age: its reading is taken now, over the network.
    claude = ClaudeAccountStatus(
        account=ClaudeAccount(slot=1, config_dir=Path("/h/.claude")),
        label="plain claude",
        identity=ClaudeIdentity(email="me@example.com"),
        signed_in=True,
    )
    assert "as of" not in account_cells(claude, reading, now=NOW)["status"].plain
    assert reading_age(reading.model_copy(update={"fetched_at": NOW}), now=NOW) == ""
    assert reading_age(reading.model_copy(update={"fetched_at": None}), now=NOW) == ""
    old = reading.model_copy(update={"fetched_at": NOW - timedelta(days=3)})
    assert reading_age(old, now=NOW) == " · as of 3d ago"


def test_the_codex_section_shows_once_codex_is_installed_or_has_a_slot() -> None:
    claude_only = AccountsOverview(claude=INSTALLED[core.AGENT], accounts=[])
    assert not agent_section_shown(CODEX, claude_only)  # an overview from before installs
    missing = claude_only.model_copy(
        update={"installs": {**INSTALLED, CODEX: ClaudeInstall(installed=False)}}
    )
    assert not agent_section_shown(CODEX, missing)
    assert agent_section_shown(CODEX, missing.model_copy(update={"installs": INSTALLED}))
    with_slot = missing.model_copy(update={"accounts": [_codex_status(3, None)]})
    assert agent_section_shown(CODEX, with_slot)


# --- the page ----------------------------------------------------------------------------------


def test_a_codex_slot_renders_in_the_one_table_with_offline_usage_bars(
    offline: list[str],
) -> None:
    account = core.create_account(CODEX)
    _sign_in_codex(account.config_dir, "codex@example.com")
    # Three hours before the page reads it, by the clock now: NOW is the module's import
    # time, and the Windows suite reached this test over an hour after collecting it.
    recorded = datetime.now(tz=UTC).replace(microsecond=0) - timedelta(hours=3)
    _rollout(account.config_dir, recorded=recorded, session=42, week=7)
    overview = _real_overview()
    slot = account.slot

    async def go(pilot: Pilot[None]) -> dict[str, Any]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await settle_until(app, lambda: painted(view, slot))
        return {
            "section": view.query_one("#codex-section", Vertical).display,
            "title": shown(view.query_one("#codex-title", Static)),
            "header": bool(view.query("#accounts-header")),
            "in_table": bool(
                view.query_one("#account-rows", Vertical).query(f"#account-row-{slot}")
            ),
            "line": line(view, slot),
            "cells": cells(view, slot),
            "add": view.query_one("#codex-add", Button).disabled,
            "remove": row(view, slot).query_one(f"#account-remove-{slot}", Button).visible,
            "login": row(view, slot).query_one(f"#account-login-{slot}", Button).visible,
        }

    seen = drive(go, overview=overview)
    assert seen["section"] and seen["title"].startswith("Codex 0.162.1") and seen["header"]
    assert seen["in_table"], "every agent's rows are in the one table"
    assert f"{slot}  account {slot}" in seen["line"]
    assert seen["cells"]["agent"] == "Codex"
    assert "codex@example.com  plus" in seen["line"]
    shown_cells = seen["cells"]
    assert (shown_cells["session"], shown_cells["week"]) == ("▮▮▯▯▯ 42%", "▯▯▯▯▯ 7%")
    assert shown_cells["session-resets"].startswith("in ") and shown_cells["status"] == (
        "as of 3h ago"
    )
    assert not seen["add"] and seen["remove"] and not seen["login"]
    assert offline == []  # read from the rollout: no request was made for it


def test_a_claude_only_machine_shows_no_codex_section() -> None:
    claude = ClaudeAccountStatus(
        account=ClaudeAccount(slot=1, config_dir=Path("/h/.claude")),
        label="plain claude",
        identity=None,
    )
    overview = AccountsOverview(
        claude=INSTALLED[core.AGENT],
        accounts=[claude],
        installs={core.AGENT: INSTALLED[core.AGENT], CODEX: ClaudeInstall(installed=False)},
    )

    async def go(pilot: Pilot[None]) -> bool:
        view = await open_accounts(pilot)
        return view.query_one("#codex-section", Vertical).display

    assert drive(go, overview=overview) is False


def test_add_codex_account_opens_codex_login_in_a_new_slot_and_records_it(
    windows: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    completed: list[tuple[int, str]] = []

    def complete(account: ClaudeAccount) -> ClaudeAccountStatus:
        completed.append((account.slot, account.agent))
        # What a connect that installed the hooks answers; the connect itself is the service's.
        return accounts_service.describe(account).model_copy(
            update={"hooks_installed": True, "detail": ""}
        )

    monkeypatch.setattr(accounts_service, "complete_sign_in", complete)
    overview = _real_overview()

    async def go(pilot: Pilot[None]) -> dict[str, Any]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await pilot.click("#codex-add")
        await settle(app)
        login = view.login
        assert login is not None and login.fresh
        header = shown(view.query_one("#login-header", Static))
        view._poll_login()  # nothing landed yet, and the window is still there…
        await settle(app)
        still_open = view.login is not None
        _sign_in_codex(login.account.config_dir, "new@example.com")  # …then codex login writes
        view._poll_login()
        await pilot.pause()
        await settle(app)
        return {
            "account": login.account,
            "header": header,
            "still_open": still_open,
            "box": view.query_one("#login-box", Vertical).display,
            "notice": notice(view),
        }

    monkeypatch.setattr(TmuxServer, "pane_facts", lambda self, pane: _Alive())
    seen = drive(go, overview=overview)
    account = seen["account"]
    assert account.agent == CODEX and account.managed
    assert account.config_dir.parent == core.accounts_root(CODEX)
    marker = json.loads((account.config_dir / core.MARKER).read_text(encoding="utf-8"))
    assert marker["agent"] == CODEX
    [window] = windows
    assert window["socket"] == PRIVATE_SOCKET and window["session"] == "asq-accounts"
    assert window["command"][-4:] == ["accounts", "run", str(account.slot), "login"]
    assert "CLAUDE_CONFIG_DIR" not in window["env"]  # a Codex window carries Codex's own
    assert "`codex login`" in seen["header"] and "Claude Code's login" not in seen["header"]
    assert seen["still_open"] and not seen["box"]
    assert completed == [(account.slot, CODEX)]
    assert seen["notice"].startswith(f"✓ account {account.slot}: new@example.com")


def test_sign_in_on_an_existing_codex_slot_runs_codex_login_there_and_keeps_the_slot(
    windows: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    abandoned: list[int] = []

    def abandon(account: ClaudeAccount) -> bool:
        abandoned.append(account.slot)
        return False

    monkeypatch.setattr(accounts_service, "abandon_sign_in", abandon)
    account = core.create_account(CODEX)  # a slot whose login never landed
    overview = _real_overview()
    slot = account.slot

    async def go(pilot: Pilot[None]) -> tuple[bool, str]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        shown_button = row(view, slot).query_one(f"#account-login-{slot}", Button).visible
        await press(pilot, f"#account-login-{slot}")
        await settle(app)
        assert view.login is not None and not view.login.fresh
        # The recorder answers "no server" for the pane: the window closed with no login.
        view._poll_login()
        await pilot.pause()
        await settle(app)
        return shown_button, notice(view)

    shown_button, said = drive(go, overview=overview)
    assert shown_button
    [window] = windows
    assert window["command"][-4:] == ["accounts", "run", str(slot), "login"]
    assert "Codex closed before a sign-in landed" in said and "nothing changed" in said
    assert abandoned == [] and account.config_dir.is_dir()  # an existing slot is never discarded


def test_log_in_over_a_codex_slots_stale_login_waits_for_codex_login_to_replace_it(
    windows: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCOPE #113, 0.8.1 item 11: *Log in* on a Codex row runs ``codex login`` over the
    auth.json still there, whose tokens already read signed in, so the window closed as
    landed at its first poll, before anyone had signed in. It is opened over the login's
    fingerprint and waits for new tokens."""
    completed: list[int] = []

    def complete(account: ClaudeAccount) -> ClaudeAccountStatus:
        completed.append(account.slot)
        return accounts_service.describe(account)

    monkeypatch.setattr(accounts_service, "complete_sign_in", complete)
    account = core.create_account(CODEX)
    _sign_in_codex(account.config_dir, "old@example.com")
    stale = accounts_service.login_fingerprint(account)
    overview = _real_overview()

    async def go(pilot: Pilot[None]) -> tuple[str | None, bool, bool]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        view.begin_claude_sign_in(account.slot)  # what *Log in* on its row does
        await settle_until(app, lambda: view.login is not None)
        assert view.login is not None
        since = view.login.since
        view._poll_login()  # the stale tokens: not a landing
        await pilot.pause()
        await settle(app)
        waited = view.login is not None
        auth = account.config_dir / "auth.json"
        data = json.loads(auth.read_text(encoding="utf-8"))
        data["tokens"]["access_token"] = "fresh-access"  # what `codex login` writes
        auth.write_text(json.dumps(data), encoding="utf-8")
        view._poll_login()
        await pilot.pause()
        await settle(app)
        return since, waited, view.login is None

    monkeypatch.setattr(TmuxServer, "pane_facts", lambda self, pane: _Alive())
    since, waited, closed = drive(go, overview=overview)
    assert stale is not None and since == stale
    assert waited, "the login it was opened over did not land"
    assert closed and completed == [account.slot]
    [window] = windows
    assert window["command"][-4:] == ["accounts", "run", str(account.slot), "login"]


def test_default_on_a_codex_row_sets_the_codex_default_and_leaves_claudes_alone() -> None:
    account = core.create_account(CODEX)
    _sign_in_codex(account.config_dir, "codex@example.com")
    overview = _real_overview()
    slot = account.slot

    async def go(pilot: Pilot[None]) -> str:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await press(pilot, f"#account-default-{slot}")
        await settle_until(app, lambda: "default" in notice(view))
        return notice(view)

    said = drive(go, overview=overview)
    assert said == f"✓ slot {slot} is the Codex machine default"
    codex_default = accounts_service.machine_default(CODEX)
    assert codex_default is not None and codex_default.slot == slot
    assert accounts_service.machine_default() is None  # Claude Code's own is untouched


def test_a_codex_account_moves_above_a_claude_one_in_the_one_order_at_80x24(
    fake_home: Path,
) -> None:
    """Owner, PR #113: one priority order over every agent's accounts. *↑* on a Codex row
    moves it past the Claude row above it, in the store and on the page, at 80x24 (the
    table scrolls sideways to the button), and the order says what it means above it."""
    import asyncio

    from aisquare.cli.ui.app import FleetApp
    from tests.test_ui_accounts import _registry_frame, accounts_read

    claude = core.create_account()
    codex = core.create_account(CODEX)

    async def run() -> tuple[list[int], list[int], str, str]:
        app = FleetApp(refresh_seconds=3600, doctor=lambda: [], accounts=_registry_frame)
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(app)
            view = await open_accounts(pilot)
            await settle_until(app, lambda: painted(view, claude.slot, codex.slot))
            before = [r.slot for r in view.rows()]
            await press(pilot, f"#account-up-{codex.slot}")
            await settle(app)
            await accounts_read(app)
            await settle_until(app, lambda: [r.slot for r in view.rows()] != before)
            return (
                before,
                [r.slot for r in view.rows()],
                cells(view, codex.slot)["agent"],
                shown(view.query_one("#accounts-order", Static)),
            )

    before, after, agent, order_note = asyncio.run(run())
    plain = core.kind(core.AGENT).plain_slot
    assert before == [plain, claude.slot, codex.slot]
    assert after == [plain, codex.slot, claude.slot]
    assert [a.slot for a in accounts_service.list_accounts()] == after
    assert agent == "Codex"
    assert order_note.startswith("In priority order, every agent's")
