"""The credits line on every surface (#143): status, whoami, the Accounts page, doctor --live."""

from __future__ import annotations

import json
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
from aisquare.core.store import store_session
from aisquare.core.workspace import pin_project, project_id_for
from aisquare.models import ProjectInfo
from aisquare.services import iam
from tests.idp_stub import IdentityProviderStub
from tests.test_credits import BALANCE
from tests.test_ui_accounts import drive, fleet_app, open_accounts, settle, shown

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

    async def go(pilot: Pilot[None]) -> str:
        app_ = fleet_app(pilot)
        view = await open_accounts(pilot)
        await settle(app_)
        await pilot.pause()
        return shown(view.query_one("#aisquare-credits", Static))

    line = drive(go)
    assert line.startswith("acme  run today ▮▮▮▮▯ 76% · resets")
    assert "run month ▮▮▯▯▯ 40%" in line and "build today unlimited" in line
    assert line.rstrip().endswith("[low]")


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
