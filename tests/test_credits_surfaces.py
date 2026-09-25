"""The credits line on every surface (#143): status, whoami, the Accounts page, doctor --live."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import threading
from collections.abc import Iterator
from http.client import IncompleteRead
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from textual.pilot import Pilot
from textual.widgets import Static
from typer.testing import CliRunner

from aisquare.cli import auth as auth_cli
from aisquare.cli.app import app
from aisquare.cli.ui.sidebar import DoctorSection
from aisquare.cli.ui.views.accounts import credits_text
from aisquare.core.config import load_config
from aisquare.core.store import store_session
from aisquare.core.workspace import project_id_for
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
    runner: CliRunner,
    idp: IdentityProviderStub,
    isolated_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> ProjectInfo:
    """Signed in, one project pointed at acme/Frontend (no key: the stub refuses the mint).

    Every command runs from the project's checkout: without ``--project``,
    ``use``, ``status`` and ``whoami`` ask about the project a launch here
    joins, not the ``project switch`` pin, which launches ignore (review of
    #170). With the pin alone, the CLI tests pointed the directory the suite
    ran from and passed, while the view, asked about this project, found no
    destination.
    """
    idp.key_mint = "token_not_valid"
    assert runner.invoke(app, ["login", "--no-browser", "--api-url", idp.url]).exit_code == 0
    root = tmp_path / "web"
    root.mkdir()
    info = ProjectInfo(id=project_id_for(root.resolve()), root=root.resolve(), linked_repos=[])
    with store_session() as store:
        store.onboard_project(info)
    monkeypatch.chdir(root)
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


def test_a_forgotten_projects_workspace_is_asked_about_wherever_a_launch_there_traces(
    runner: CliRunner, idp: IdentityProviderStub, pointed: ProjectInfo
) -> None:
    """One rule for a forgotten project's destination, on every surface. ``project
    forget`` keeps the ``project_destination`` row, and a launch in that root still
    traces into its workspace: the resolver reads the destination by project id, and
    the launch's first prompt revives the row. ``whoami`` and ``explainability status``
    said so, while ``doctor --live`` and the Accounts page (review of #173, round 1)
    hid that workspace's credits, low or exhausted, from the operator whose next fleet
    there traces into it (review of #173 after the stack's merge, J1)."""
    from aisquare.cli.ui.views.accounts import _read_credits
    from aisquare.services import diagnostics, explainability_ops

    session = iam.current_session()
    assert session is not None
    with store_session() as store:
        store.forget_project(pointed.id)
        assert store.get_project(pointed.id) is None
    lands_in = explainability_ops.resolve_target(
        load_config().explainability, None, project_id=pointed.id
    ).destination
    assert lands_in is not None and lands_in.workspace_name == "acme", "a launch there"

    assert "credits: acme [low]" in runner.invoke(app, ["whoami"]).output
    row = {c.name: c for c in diagnostics.doctor(live=True)}["workspace-credits"]
    assert row.status == "warn" and "acme" in row.detail
    assert [r.workspace_name for r in _read_credits(session)] == ["acme"]


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

    def sign_out(_session: iam.Session) -> auth_service.SignedOut:
        current["session"] = None
        return auth_service.SignedOut(revoked=True, restricted=True)

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


def test_the_bars_draw_in_their_own_colour_only_the_labels_are_dim() -> None:
    """Review of #173, round 2: each window was built as ``Text(label,
    style="dim")``, which makes dim the style of everything appended after the
    label too — the bar and its percentage drew faded (``dim yellow``) beside
    the Claude rows' bars below. ``.plain`` cannot see that; the drawn segments can."""
    reading = credits_service.parse(BALANCE, workspace_id=42, workspace_name="acme", now=NOW)
    drawn = [
        (segment.text, str(segment.style))
        for segment in credits_text([reading], now=NOW).render(Console(width=400))
    ]
    assert [(text, style) for text, style in drawn if "▮" in text or "%" in text] == [
        ("▮▮▮▮▯", "yellow"),
        (" 76%", "yellow"),
        ("▮▮▯▯▯", "green"),
        (" 40%", "green"),
    ], drawn
    assert ("run today ", "dim") in drawn and ("build today ", "dim") in drawn


def test_a_truncated_answer_is_a_reason_on_the_row_not_a_traceback(
    runner: CliRunner,
    idp: IdentityProviderStub,
    pointed: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #173, round 1: ``http.client`` raises what ``urllib`` does not
    wrap — ``IncompleteRead`` for a body shorter than its Content-Length — and it
    left ``status`` and ``whoami`` with exit 1 and a traceback. ``whoami`` made
    no request at all before #143; a balance must never cost it the answer.

    Cut short where it happens, in the answer ``urlopen`` hands back: a stub of
    ``iam._http`` raised what the real one stopped letting out, and exercised only
    a branch ``fetch`` no longer has (review of the accounts stack's fold, round 2,
    F4). The row says the server answered, not that it could not be reached."""
    import urllib.request

    real = urllib.request.urlopen

    def urlopen(request: urllib.request.Request, timeout: float) -> Any:
        response = real(request, timeout=timeout)
        if request.full_url.endswith("/api/v2/credits/balance/"):

            def cut_short(*args: object) -> bytes:
                raise IncompleteRead(b'{"period": "2026-', 40)

            response.read = cut_short
        return response

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    status = runner.invoke(app, ["explainability", "status"])
    assert status.exit_code == 0, status.output
    assert "credits:  acme: credits unavailable — http://" in status.output, status.output
    assert (
        "/api/v2/credits/balance/ answered HTTP 200, but the answer was cut short "
        "(IncompleteRead(17 bytes read, 40 more expected))."
    ) in status.output, status.output
    who = runner.invoke(app, ["whoami"])
    assert who.exit_code == 0, who.output
    assert "credits: acme: credits unavailable" in who.output


def test_the_explainability_views_row_says_why_it_has_no_reading(
    runner: CliRunner,
    idp: IdentityProviderStub,
    pointed: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #173, round 1: with a destination chosen but nothing to ask
    with, the row read ``(no destination chosen)`` — directly under the
    ``lands in`` row naming that destination. It says what is missing now."""
    from aisquare.cli.ui.views.explainability import status_report

    rows = dict(status_report(pointed).rows)
    assert rows["credits"].startswith("acme [low] — run credits"), rows["credits"]
    assert runner.invoke(app, ["logout"]).exit_code == 0
    rows = dict(status_report(pointed).rows)
    assert rows["lands in"].startswith("acme / Frontend"), rows["lands in"]
    assert rows["credits"] == "(sign in to read them — aisquare login)"
    elsewhere = iam.Session(api_url="https://api.aisquare.studio", token="aisq_x", source="file")
    monkeypatch.setattr(iam, "current_session", lambda api_url=None: elsewhere)
    rows = dict(status_report(pointed).rows)
    assert rows["credits"] == (
        "(signed in to https://api.aisquare.studio, not this workspace's API — "
        f"aisquare login --api-url {idp.url} to read them)"
    ), rows["credits"]
    # Round 2: an AISQUARE_TOKEN session cannot `aisquare login` (env_token_set);
    # its API is the environment's to change. The review of that round: and its
    # token, which the other server issued — the URL alone reads a 401.
    from_env = dataclasses.replace(elsewhere, source="env")
    monkeypatch.setattr(iam, "current_session", lambda api_url=None: from_env)
    rows = dict(status_report(pointed).rows)
    assert rows["credits"] == (
        "(AISQUARE_TOKEN is used with https://api.aisquare.studio, not this workspace's API — "
        f"set AISQUARE_API_URL={idp.url} and a token that API issued to read them)"
    ), rows["credits"]
    assert len(_balance_calls(idp)) == 1, "none of these cases asks anyone"


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

    def sign_out(_session: iam.Session) -> auth_service.SignedOut:
        current["session"] = None
        return auth_service.SignedOut(revoked=True, restricted=True)

    monkeypatch.setattr(auth_service, "sign_out", sign_out)

    async def go(pilot: Pilot[None]) -> str:
        app_ = fleet_app(pilot)
        view = await open_accounts(pilot)
        try:
            assert await asyncio.to_thread(asked.wait, 5), "the page asked for the credits"
            await pilot.click("#aisquare-sign-out")
            # The sign-out worker is a thread: wait on the clock, not a count of
            # pauses a loaded runner might not finish it in.
            deadline = asyncio.get_running_loop().time() + 5
            while view.session is not None and asyncio.get_running_loop().time() < deadline:
                await pilot.pause(0.05)
            assert view.session is None, "the sign-out finished while the reading was held"
        finally:
            release.set()
        await settle(app_)
        await asyncio.sleep(0.2)  # the discarded thread's answer has had time to land
        await pilot.pause()
        return shown(view.query_one("#aisquare-credits", Static))

    assert drive(go) == ""


def test_another_sign_in_while_the_page_is_hidden_never_shows_the_last_ones_bars(
    idp: IdentityProviderStub, pointed: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #173, round 2: a switch from one session to ANOTHER (not to
    none) while the page was hidden left the first one's bars on the line —
    shown again under the second one's card until its own reading landed — and
    a reading for the first still in flight was painted when it came back."""
    session = iam.current_session()
    assert session is not None
    other = dataclasses.replace(session, token="aisq_someone_else", email="b@example.com")
    idp.issued.append(other.token)  # a sign-in the API honours, like the first
    current: dict[str, iam.Session | None] = {"session": session}
    monkeypatch.setattr(iam, "current_session", lambda api_url=None: current["session"])
    hold, asked, release = threading.Event(), threading.Event(), threading.Event()
    real = credits_service.for_destination

    def held(*args: Any, **kwargs: Any) -> credits_service.WorkspaceCredits | None:
        if hold.is_set():
            asked.set()
            release.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(credits_service, "for_destination", held)

    async def go(pilot: Pilot[None]) -> tuple[str, str, str, str]:
        app_ = fleet_app(pilot)
        view = await open_accounts(pilot)
        await settle(app_)
        await pilot.pause()
        line = view.query_one("#aisquare-credits", Static)
        before = shown(line)
        hold.set()
        view.refresh_readings()  # the minute tick: a reading for the first session goes out
        try:
            assert await asyncio.to_thread(asked.wait, 5), "the tick asked for the credits"
            await pilot.click(app_.query_one(DoctorSection))
            await pilot.pause()
            assert app_.current_view() is not view
            current["session"] = other  # `aisquare login` as someone else, in another terminal
            idp.credits = json.loads(json.dumps(BALANCE)) | {"state": "exhausted"}
            app_.refresh_accounts()
            await pilot.pause()
            hidden = shown(line)
        finally:
            hold.clear()
            release.set()
        await settle(app_)
        await asyncio.sleep(0.2)  # the first session's answer has had time to land
        await pilot.pause()
        landed = shown(line)
        await open_accounts(pilot)
        await settle(app_)
        await pilot.pause()
        return before, hidden, landed, shown(line)

    before, hidden, landed, again = drive(go)
    assert before.startswith("acme [low]"), before
    assert hidden == "", "the first session's bars are not the second one's"
    assert landed == "", "the first session's reading, in flight at the switch, is dropped"
    assert again.startswith("acme [exhausted]"), again
