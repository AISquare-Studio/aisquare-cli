"""The credits line on every surface (#143): status, whoami, the Accounts page, doctor --live."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from http.client import IncompleteRead
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import Static
from typer.testing import CliRunner

from aisquare.cli import auth as auth_cli
from aisquare.cli.app import app
from aisquare.cli.ui.views.accounts import credits_text
from aisquare.core.store import store_session
from aisquare.core.workspace import pin_project, project_id_for
from aisquare.models import ProjectInfo
from aisquare.services import auth as auth_service
from aisquare.services import credits as credits_service
from aisquare.services import iam
from tests.idp_stub import IdentityProviderStub
from tests.test_credits import BALANCE, NOW
from tests.test_ui_accounts import (
    _overview,
    _status,
    drive,
    fleet_app,
    open_accounts,
    settle,
    shown,
)
from tests.test_ui_shell import composited

WORKSPACES = [
    {"id": 42, "uid": "ws-uid-42", "name": "acme", "type": "team", "effective_role": "ADMIN"},
]
STUDIOS = {"42": [{"id": 301, "uid": "st-301", "name": "Frontend", "workspace_id": 42}]}


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_cli, "_sleep", lambda _seconds: None)


@pytest.fixture
def idp() -> Iterator[IdentityProviderStub]:
    stub = IdentityProviderStub()
    stub.workspaces = [dict(w) for w in WORKSPACES]
    stub.studios = {k: [dict(s) for s in v] for k, v in STUDIOS.items()}
    stub.credits = json.loads(json.dumps(BALANCE))
    yield stub
    stub.close()


@pytest.fixture
def pointed(
    runner: CliRunner, idp: IdentityProviderStub, isolated_home: Path, tmp_path: Path
) -> ProjectInfo:
    """Signed in, one project pointed at acme/Frontend (no key: the stub refuses the mint)."""
    idp.key_mint = "token_not_valid"
    assert runner.invoke(app, ["login", "--no-browser", "--api-url", idp.url]).exit_code == 0
    root = tmp_path / "web"
    root.mkdir()
    info = ProjectInfo(id=project_id_for(root.resolve()), root=root.resolve(), linked_repos=[])
    with store_session() as store:
        store.onboard_project(info)
    pin_project(info.id)
    assert runner.invoke(app, ["explainability", "use", "acme/Frontend"]).exit_code == 0
    return info


def _balance_calls(idp: IdentityProviderStub) -> list[dict[str, Any]]:
    return [r for r in idp.requests if r["path"] == "/api/v2/credits/balance/"]


def test_status_and_whoami_carry_the_workspace_credits(
    runner: CliRunner, idp: IdentityProviderStub, pointed: ProjectInfo
) -> None:
    status = runner.invoke(app, ["explainability", "status"])
    assert status.exit_code == 0, status.output
    assert "credits:  acme [low] — run credits: today 120 of 500 left (24%)" in status.output
    calls = _balance_calls(idp)
    assert len(calls) == 1 and calls[0]["headers"]["x-workspace-id"] == "ws-uid-42"
    assert calls[0]["headers"]["authorization"].startswith("Bearer aisq_")

    payload = json.loads(runner.invoke(app, ["--json", "explainability", "status"]).stdout)
    assert payload["credits"]["state"] == "low" and payload["credits"]["period"] == "2026-09"
    assert payload["credits"]["pools"]["run.daily"]["remaining"] == 120
    assert payload["credits"]["pools"]["build.daily"]["limit"] is None, "-1 is unlimited"
    assert len(_balance_calls(idp)) == 1, "the second read within a minute is the cache's"

    who = runner.invoke(app, ["whoami"])
    assert "credits: acme [low] — run credits" in who.output
    assert json.loads(runner.invoke(app, ["--json", "whoami"]).stdout)["credits"]["state"] == "low"


def test_without_a_destination_or_a_session_there_is_no_credits_line(
    runner: CliRunner, idp: IdentityProviderStub, pointed: ProjectInfo
) -> None:
    assert runner.invoke(app, ["explainability", "use", "--clear"]).exit_code == 0
    status = runner.invoke(app, ["explainability", "status"])
    assert "credits:" not in status.output
    payload = json.loads(runner.invoke(app, ["--json", "explainability", "status"]).stdout)
    assert payload["credits"] is None
    # Pointed again but signed out: nothing to ask with — the line is absent, not an error.
    assert runner.invoke(app, ["explainability", "use", "acme/Frontend"]).exit_code == 0
    assert runner.invoke(app, ["logout"]).exit_code == 0
    status = runner.invoke(app, ["explainability", "status"])
    assert status.exit_code == 0 and "credits:" not in status.output
    assert "destination: acme" in status.output


def test_an_api_that_cannot_answer_is_a_reason_on_the_row(
    runner: CliRunner, idp: IdentityProviderStub, pointed: ProjectInfo
) -> None:
    idp.credits = None  # the endpoint answers 404 for this workspace
    status = runner.invoke(app, ["explainability", "status"])
    assert "credits:  acme: credits unavailable — HTTP 404" in status.output
    assert status.exit_code == 0


def test_doctor_live_warns_on_the_servers_band(
    runner: CliRunner, idp: IdentityProviderStub, pointed: ProjectInfo
) -> None:
    from aisquare.services import diagnostics

    rows = {c.name: c for c in diagnostics.doctor(live=True)}
    row = rows["workspace-credits"]
    assert row.status == "warn" and "acme" in row.detail and "low" in row.detail
    assert "Top up the workspace" in (row.fix or "")
    idp.credits = json.loads(json.dumps(BALANCE)) | {"state": "ok"}
    rows = {c.name: c for c in diagnostics.doctor(live=True)}
    assert rows["workspace-credits"].status == "ok"
    assert len(_balance_calls(idp)) == 2, "doctor never trusts the cache"
    assert "workspace-credits" not in {c.name for c in diagnostics.doctor(live=False)}


def test_the_accounts_page_draws_the_destination_workspaces_bars(
    idp: IdentityProviderStub, pointed: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = iam.current_session()
    assert session is not None
    monkeypatch.setattr(iam, "current_session", lambda api_url=None: session)

    async def go(pilot: Pilot[None]) -> tuple[str, str]:
        app_ = fleet_app(pilot)
        view = await open_accounts(pilot)
        await settle(app_)
        await pilot.pause()
        widget = view.query_one("#aisquare-credits", Static)
        return shown(widget), composited(widget)

    line, drawn = drive(go)
    assert line.startswith("acme [low]  run today ▮▮▮▮▯ 76% · resets")
    assert "run month ▮▮▯▯▯ 40%" in line and "build today unlimited" in line
    # Review of #173, round 1: the band came LAST on a no-wrap line and the
    # page's width cut it off (140x40 leaves ~106 cells; the line is longer),
    # while `.plain` still ended with it. It leads now, and the drawn strip —
    # what the eye gets — is what carries it.
    assert drawn.startswith("acme [low]  run today"), drawn
    assert len(drawn.rstrip()) < len(line), "the fixture's line is wider than the page"


def test_the_accounts_page_reads_credits_with_no_claude_slot_signed_in(
    idp: IdentityProviderStub, pointed: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #173, round 1: the credits line rode the tail of the Claude
    usage refresh and inherited its early return — no signed-in Claude slot, no
    credits, for a user signed in to AISquare with a workspace chosen."""
    session = iam.current_session()
    assert session is not None
    monkeypatch.setattr(iam, "current_session", lambda api_url=None: session)

    async def go(pilot: Pilot[None]) -> str:
        app_ = fleet_app(pilot)
        view = await open_accounts(pilot)
        await settle(app_)
        await pilot.pause()
        return shown(view.query_one("#aisquare-credits", Static))

    line = drive(go, overview=_overview(_status(1, None, signed_in=False)))
    assert line.startswith("acme [low]  run today ▮▮▮▮▯ 76%"), line


def test_signing_out_on_the_accounts_page_clears_the_credits_line(
    idp: IdentityProviderStub, pointed: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #173, round 1: sign-in and sign-out repainted the card and not
    the credits, so the signed-out page kept the last session's bars."""
    session = iam.current_session()
    assert session is not None
    current: dict[str, iam.Session | None] = {"session": session}
    monkeypatch.setattr(iam, "current_session", lambda api_url=None: current["session"])

    def sign_out(_session: iam.Session) -> bool:
        current["session"] = None
        return True

    monkeypatch.setattr(auth_service, "sign_out", sign_out)

    async def go(pilot: Pilot[None]) -> tuple[str, str]:
        app_ = fleet_app(pilot)
        view = await open_accounts(pilot)
        await settle(app_)
        await pilot.pause()
        before = shown(view.query_one("#aisquare-credits", Static))
        await pilot.click("#aisquare-sign-out")
        await settle(app_)
        await pilot.pause()
        return before, shown(view.query_one("#aisquare-credits", Static))

    before, after = drive(go)
    assert before.startswith("acme [low]"), before
    assert after == "", after


def test_a_zero_allowance_draws_a_full_bar_not_unlimited() -> None:
    """Review of #173, round 1: a ``limit`` of 0 read ``unlimited`` on the page."""
    payload = {
        "state": "exhausted",
        "pools": {"build_credits": {"daily": {"used": 0, "limit": 0, "remaining": 0}}},
    }
    reading = credits_service.parse(payload, workspace_id=42, workspace_name="acme", now=NOW)
    line = credits_text([reading]).plain
    assert line == "acme [exhausted]  build today ▮▮▮▮▮ 100%", line


def test_a_truncated_answer_is_a_reason_on_the_row_not_a_traceback(
    runner: CliRunner,
    idp: IdentityProviderStub,
    pointed: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #173, round 1: ``http.client`` raises what ``urllib`` does not
    wrap — ``IncompleteRead`` for a body shorter than its Content-Length — and it
    left ``status`` and ``whoami`` with exit 1 and a traceback. ``whoami`` made
    no request at all before #143; a balance must never cost it the answer."""
    real = iam._http

    def http(method: str, url: str, **kwargs: Any) -> iam.HttpResult:
        if url.endswith("/api/v2/credits/balance/"):
            raise IncompleteRead(b'{"period": "2026-', 40)
        return real(method, url, **kwargs)

    monkeypatch.setattr(iam, "_http", http)
    status = runner.invoke(app, ["explainability", "status"])
    assert status.exit_code == 0, status.output
    assert "credits:  acme: credits unavailable — could not read the balance" in status.output
    assert "IncompleteRead(" in status.output
    who = runner.invoke(app, ["whoami"])
    assert who.exit_code == 0, who.output
    assert "credits: acme: credits unavailable" in who.output


def test_a_sign_in_or_out_in_another_terminal_follows_on_the_next_frame(
    idp: IdentityProviderStub, pointed: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shell hands the page a frame every few seconds with the session
    re-read; one that differs from the last re-reads the credits then, not on
    the minute tick — a ``logout`` elsewhere empties the line, a ``login``
    fills it again."""
    session = iam.current_session()
    assert session is not None
    current: dict[str, iam.Session | None] = {"session": session}
    monkeypatch.setattr(iam, "current_session", lambda api_url=None: current["session"])

    async def go(pilot: Pilot[None]) -> tuple[str, str, str]:
        app_ = fleet_app(pilot)
        view = await open_accounts(pilot)
        await settle(app_)
        await pilot.pause()
        line = view.query_one("#aisquare-credits", Static)
        before = shown(line)
        current["session"] = None  # `aisquare logout` in another terminal
        app_.refresh_accounts()
        await pilot.pause()
        signed_out = shown(line)
        current["session"] = session  # and `aisquare login` again
        app_.refresh_accounts()
        await settle(app_)
        await pilot.pause()
        return before, signed_out, shown(line)

    before, signed_out, again = drive(go)
    assert before.startswith("acme [low]"), before
    assert signed_out == "", signed_out
    assert again.startswith("acme [low]"), again


def test_a_reading_in_flight_at_sign_out_is_never_painted(
    idp: IdentityProviderStub, pointed: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A balance request still out when the session goes (5 s ceiling) is
    cancelled, and its answer — the previous session's bars — is dropped."""
    session = iam.current_session()
    assert session is not None
    current: dict[str, iam.Session | None] = {"session": session}
    monkeypatch.setattr(iam, "current_session", lambda api_url=None: current["session"])
    asked, release = threading.Event(), threading.Event()
    real = credits_service.for_destination

    def slow(*args: Any, **kwargs: Any) -> credits_service.WorkspaceCredits | None:
        asked.set()
        release.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(credits_service, "for_destination", slow)

    def sign_out(_session: iam.Session) -> bool:
        current["session"] = None
        return True

    monkeypatch.setattr(auth_service, "sign_out", sign_out)

    async def go(pilot: Pilot[None]) -> str:
        app_ = fleet_app(pilot)
        view = await open_accounts(pilot)
        try:
            assert await asyncio.to_thread(asked.wait, 5), "the page asked for the credits"
            await pilot.click("#aisquare-sign-out")
            for _ in range(20):
                await pilot.pause()
                if view.session is None:
                    break
        finally:
            release.set()
        await settle(app_)
        await asyncio.sleep(0.2)  # the discarded thread's answer has had time to land
        await pilot.pause()
        return shown(view.query_one("#aisquare-credits", Static))

    assert drive(go) == ""
