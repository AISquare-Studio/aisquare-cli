"""``GET /api/explainability/<agent>`` (PLAN §4-I): the card, available and RED, never raising."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from aisquare.cli.app import app as cli
from aisquare.core.paths import remote_audit_path, remote_state_path
from aisquare.core.store import store_session
from aisquare.models import CheckStatus, DoctorCheck, FleetAgent, TeamSession, TurnMetric
from aisquare.services import explainability_ops as ops
from aisquare.services import remote_server
from aisquare.services.remote_server import (
    COOKIE,
    DoctorVerdict,
    NoSuchAgent,
    Runtime,
    Sources,
    build_app,
    explainability_payload,
)

PASSWORD = "Test1234"
T0 = datetime(2026, 9, 12, 10, 0, tzinfo=UTC)


def _agent(session_id: str | None = "ses-1") -> FleetAgent:
    return FleetAgent(
        id="agt_1",
        project_id="prj_1",
        label="coder-1",
        role="coder",
        pane_id="%3",
        session_id=session_id,
        cwd=Path("/tmp/x"),
        created_at=T0,
    )


def _session(model: str | None = "claude-fable-5-1") -> TeamSession:
    return TeamSession(
        id="ses-1",
        project_id="prj_1",
        role="coder",
        started_at=T0,
        last_seen_at=T0 + timedelta(minutes=5),
        model=model,
    )


def _turn(n: int, tokens_in: int | None, tokens_out: int | None) -> TurnMetric:
    return TurnMetric(
        trace_id=f"trc-{n}",
        project_id="prj_1",
        session_id="ses-1",
        started_at=T0 + timedelta(minutes=n),
        ended_at=T0 + timedelta(minutes=n, seconds=30),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


GREEN = DoctorVerdict(sdk_present=True, red=[])
NO_SDK = DoctorVerdict(sdk_present=False, red=[], install_hint="pip install x")
RED = DoctorVerdict(
    sdk_present=True, red=["explainability config: target 'stg' has no gateway URL"]
)
POLICY = {"tracing": True, "shipping": False, "target": "stg", "redaction": "standard"}


# --- the payload -----------------------------------------------------------------------


def test_available_card_has_model_tokens_policy_and_updated_at() -> None:
    turns = [_turn(1, 100, 20), _turn(2, 50, None), _turn(3, None, 5)]
    card = explainability_payload(
        agent=_agent(), session=_session(), turns=turns, verdict=GREEN, policy=POLICY
    )
    assert card == {
        "available": True,
        "model": "claude-fable-5-1",
        "tokens_in": 150,
        "tokens_out": 25,
        "policy": POLICY,
        "updated_at": "2026-09-12T10:05:00+00:00",
    }
    assert "reason" not in card and "cost_estimate_usd" not in card


def test_missing_sdk_is_unavailable_with_reason_but_keeps_fleet_facts() -> None:
    card = explainability_payload(
        agent=_agent(), session=_session(), turns=[_turn(1, 7, 3)], verdict=NO_SDK, policy=None
    )
    assert card["available"] is False
    assert card["reason"] == "explainability SDK not installed (pip install x)"
    assert card["model"] == "claude-fable-5-1"
    assert card["tokens_in"] == 7 and card["tokens_out"] == 3
    assert "policy" not in card


def test_red_doctor_is_unavailable_and_names_the_failing_check() -> None:
    card = explainability_payload(
        agent=_agent(), session=_session(), turns=[], verdict=RED, policy=POLICY
    )
    assert card["available"] is False
    assert card["reason"] == "doctor is RED: explainability config: target 'stg' has no gateway URL"
    assert "tokens_in" not in card and "tokens_out" not in card


def test_an_agent_with_no_session_yields_only_availability_and_a_stamp() -> None:
    card = explainability_payload(
        agent=_agent(session_id=None), session=None, turns=[], verdict=GREEN, policy=None
    )
    assert card == {"available": True, "updated_at": "2026-09-12T10:00:00+00:00"}


def test_updated_at_is_the_latest_fact() -> None:
    late = _turn(30, 1, 1)
    card = explainability_payload(
        agent=_agent(), session=_session(), turns=[late], verdict=GREEN, policy=None
    )
    assert card["updated_at"] == "2026-09-12T10:30:30+00:00"


def test_payload_keys_stay_inside_the_contract() -> None:
    allowed = {
        "available",
        "reason",
        "model",
        "tokens_in",
        "tokens_out",
        "cost_estimate_usd",
        "policy",
        "updated_at",
    }
    for verdict in (GREEN, NO_SDK, RED):
        card = explainability_payload(
            agent=_agent(),
            session=_session(),
            turns=[_turn(1, 1, 1)],
            verdict=verdict,
            policy=POLICY,
        )
        assert set(card) <= allowed, card


# --- the endpoint ----------------------------------------------------------------------


@pytest.fixture
def runtime(isolated_home: Path) -> Runtime:
    rt = Runtime(remote_state_path(), remote_audit_path())
    rt._state.password = PASSWORD
    rt._save()
    return rt


def _sources(explainability: Any) -> Sources:
    return Sources(
        projects=lambda: [],
        fleet=lambda: {},
        board=lambda: {},
        tasks=lambda: [],
        memory=lambda: [],
        panes=lambda agent: {"rows": [], "width": 0, "height": 0},
        explainability=explainability,
    )


def _client(runtime: Runtime, explainability: Any, tmp_path: Path) -> TestClient:
    client = TestClient(build_app(runtime, sources=_sources(explainability), dist_dir=tmp_path))
    assert (
        client.post(f"/r/{runtime.token}/api/unlock", json={"password": PASSWORD}).status_code
        == 200
    )
    return client


def test_endpoint_returns_the_card(runtime: Runtime, tmp_path: Path) -> None:
    def card(label: str) -> dict[str, object]:
        if label == "ghost":
            raise NoSuchAgent("no live agent 'ghost'")
        return {"available": False, "reason": "explainability SDK not installed", "model": "m"}

    client = _client(runtime, card, tmp_path)
    ok = client.get(f"/r/{runtime.token}/api/explainability/coder-1")
    assert ok.status_code == 200
    assert ok.json() == {
        "available": False,
        "reason": "explainability SDK not installed",
        "model": "m",
    }
    assert client.get(f"/r/{runtime.token}/api/explainability/ghost").status_code == 404


def test_endpoint_never_raises_when_the_source_blows_up(runtime: Runtime, tmp_path: Path) -> None:
    def boom(label: str) -> dict[str, object]:
        raise RuntimeError("sdk exploded")

    client = _client(runtime, boom, tmp_path)
    response = client.get(f"/r/{runtime.token}/api/explainability/coder-1")
    assert response.status_code == 200
    assert response.json() == {
        "available": False,
        "reason": "explainability lookup failed: sdk exploded",
    }
    # and the neighbours are untouched
    assert client.get(f"/r/{runtime.token}/api/board").status_code == 200


def test_endpoint_obeys_the_token_and_cookie_gates(runtime: Runtime, tmp_path: Path) -> None:
    app = build_app(runtime, sources=_sources(lambda label: {"available": True}), dist_dir=tmp_path)
    anonymous = TestClient(app)
    assert anonymous.get("/r/wrong/api/explainability/coder-1").status_code == 404
    assert anonymous.get(f"/r/{runtime.token}/api/explainability/coder-1").status_code == 401
    anonymous.cookies.set(COOKIE, "forged")
    assert anonymous.get(f"/r/{runtime.token}/api/explainability/coder-1").status_code == 401


# --- the live lookup, in an isolated home ------------------------------------------------


@pytest.fixture
def fleet_home(isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A configured home with one live agent row, its board session and two turns."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    runner = CliRunner()
    assert runner.invoke(cli, ["init", "--local", "--no-onboard", "--yes"]).exit_code == 0
    assert runner.invoke(cli, ["team", "on"]).exit_code == 0
    from aisquare.services import fleet as fleet_service

    project = fleet_service.resolve_project(None)
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id="ses-live",
                project_id=project.id,
                role="coder",
                started_at=T0,
                last_seen_at=T0 + timedelta(minutes=1),
                model="claude-sonnet-5",
            )
        )
        store.upsert_fleet_agent(
            FleetAgent(
                id="agt_live",
                project_id=project.id,
                label="coder-1",
                role="coder",
                pane_id="%9",
                session_id="ses-live",
                cwd=project_dir,
                created_at=T0,
            )
        )
        for n, (i, o) in enumerate(((120, 30), (80, 10)), start=1):
            store.open_turn(
                TurnMetric(
                    trace_id=f"trc-live-{n}",
                    project_id=project.id,
                    session_id="ses-live",
                    started_at=T0 + timedelta(minutes=n),
                    ended_at=T0 + timedelta(minutes=n, seconds=1),
                    tokens_in=i,
                    tokens_out=o,
                )
            )
    monkeypatch.setattr(remote_server, "_doctor_cache", None)
    return project.id


def test_live_lookup_on_this_machine_is_red_and_still_shows_fleet_facts(fleet_home: str) -> None:
    """The SDK is not installed here (the machine the demo runs on): available:false, no raise."""
    card = remote_server.live_sources().explainability("coder-1")
    assert card["available"] is False
    assert isinstance(card["reason"], str) and "SDK not installed" in card["reason"]
    assert card["model"] == "claude-sonnet-5"
    assert card["tokens_in"] == 200 and card["tokens_out"] == 40
    assert isinstance(card["policy"], dict) and set(card["policy"]) == {
        "tracing",
        "shipping",
        "target",
        "gateway",
        "redaction",
    }
    assert isinstance(card["updated_at"], str)
    with pytest.raises(NoSuchAgent):
        remote_server.live_sources().explainability("nobody")


def test_live_lookup_with_the_sdk_present_and_a_red_doctor(
    fleet_home: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ops, "sdk_presence", lambda: ops.SdkPresence(True, None, "1.2.0", False))
    monkeypatch.setattr(
        ops,
        "checks",
        lambda *a, **k: [
            DoctorCheck(name="explainability", status=CheckStatus.ok, detail="on"),
            DoctorCheck(
                name="explainability proxy", status=CheckStatus.fail, detail="proxy unreachable"
            ),
        ],
    )
    card = remote_server.live_sources().explainability("coder-1")
    assert card["available"] is False
    assert card["reason"] == "doctor is RED: explainability proxy: proxy unreachable"
    assert card["model"] == "claude-sonnet-5"


def test_live_lookup_with_the_sdk_present_and_a_green_doctor(
    fleet_home: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ops, "sdk_presence", lambda: ops.SdkPresence(True, None, "1.2.0", False))
    monkeypatch.setattr(
        ops,
        "checks",
        lambda *a, **k: [DoctorCheck(name="explainability", status=CheckStatus.ok, detail="on")],
    )
    card = remote_server.live_sources().explainability("coder-1")
    assert card["available"] is True and "reason" not in card
    assert card["tokens_in"] == 200


def test_doctor_verdict_is_cached_between_calls(
    fleet_home: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def counted(*a: Any, **k: Any) -> list[DoctorCheck]:
        calls.append(1)
        return []

    monkeypatch.setattr(ops, "checks", counted)
    remote_server._doctor_verdict()
    remote_server._doctor_verdict()
    assert len(calls) == 1


def test_a_crashing_doctor_becomes_a_reason_not_an_error(
    fleet_home: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*a: Any, **k: Any) -> list[DoctorCheck]:
        raise RuntimeError("doctor fell over")

    monkeypatch.setattr(ops, "checks", broken)
    monkeypatch.setattr(ops, "sdk_presence", lambda: ops.SdkPresence(True, None, "1.2.0", False))
    card = remote_server.live_sources().explainability("coder-1")
    assert card["available"] is False
    assert card["reason"] == "doctor is RED: doctor: doctor fell over"
