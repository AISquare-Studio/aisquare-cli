"""One session against the v2 stub: session start, a prompt, a stop — through the hooks and the CLI.

This is the smoke the server team will run against the real ``/v1/hook`` once
it answers: both request bodies validate against the vendored schema, the
prompt row is opened and then closed by Stop, the session-start row is closed
at creation, and the join record the ledger pairs on is spooled with the same
trace id the row carries.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import insights, outbox
from aisquare.core.config import AppConfig, save_config
from aisquare.models import ClientReason
from aisquare.services import hooks as hooks_service
from aisquare.services import metrics as metrics_service
from tests.ci_schemas import assert_valid
from tests.ci_support import RUN, SESSION, wire
from tests.stub_ci_server import StubCI, live_descriptor, serve


@pytest.fixture
def stub() -> Iterator[StubCI]:
    yield from serve()


@pytest.fixture
def wired(stub: StubCI, monkeypatch: pytest.MonkeyPatch) -> StubCI:
    wire(monkeypatch, stub)
    stub.descriptor_json(
        live_descriptor(
            delivery=[
                {
                    "kind": "hook_push",
                    "triggers": ["session_start", "prompt_submit"],
                    "endpoint": "/v1/hook",
                },
                {"kind": "mcp_pull", "tool": "collective_intelligence_recall"},
            ]
        )
    )
    config = AppConfig()
    config.explainability.ship = True
    config.explainability.gateway_url = "https://gateway.example"
    save_config(config)
    insights.reset_cache()
    return stub


def test_a_whole_session_through_the_service_layer(
    wired: StubCI, isolated_home: Path, tmp_path: Path
) -> None:
    context = hooks_service.session_start_context(tmp_path, session_id=SESSION)
    block = hooks_service.prompt_submitted(
        "why did the pool guard leak", tmp_path, session_id=SESSION
    )
    hooks_service.turn_stopped(tmp_path, session_id=SESSION)

    assert "collective_intelligence_recall" in context
    assert "pool-reset" in block
    assert [r["trigger"] for r in wired.requests] == ["session_start", "prompt_submit"]
    for body in wired.requests:
        assert_valid("hook-request.experimental-v2", body)
    assert wired.descriptor_fetches == 1

    turns = {t.trigger: t for t in metrics_service.recent(session_id=SESSION)}
    assert set(turns) == {"session_start", "prompt_submit"}
    assert turns["session_start"].ended_at is not None
    prompt = turns["prompt_submit"]
    assert prompt.ended_at is not None and prompt.wall_ms is not None and prompt.wall_ms >= 0
    assert prompt.client_reason is ClientReason.none and prompt.query_id == "qry_kernel0001"

    joins = [json.loads(p.read_text(encoding="utf-8")) for p in outbox.pending()]
    joins = [r for r in joins if r["kind"] == "ci_turn"]
    assert {j["ci"]["trace_id"] for j in joins} == {t.trace_id for t in turns.values()}
    assert all(j["ci"]["run_id"] == RUN for j in joins)


def test_a_whole_session_through_the_installed_hook_commands(
    wired: StubCI, isolated_home: Path, tmp_path: Path, runner: CliRunner
) -> None:
    def payload(**extra: object) -> str:
        return json.dumps({"session_id": SESSION, "cwd": str(tmp_path), **extra})

    start = runner.invoke(app, ["hook", "session-start"], input=payload(source="startup"))
    prompt = runner.invoke(
        app, ["hook", "user-prompt-submit"], input=payload(prompt="why did it leak")
    )
    stop = runner.invoke(app, ["hook", "stop"], input=payload())

    assert start.exit_code == 0 and prompt.exit_code == 0 and stop.exit_code == 0
    assert "collective_intelligence_recall" in start.stdout
    assert "Retrieved by aisquare" in prompt.stdout and "pool-reset" in prompt.stdout
    assert stop.stdout == ""
    assert wired.requests[1]["prompt"] == "why did it leak"
    (turn,) = [
        t for t in metrics_service.recent(session_id=SESSION) if t.trigger == "prompt_submit"
    ]
    assert turn.ended_at is not None


def test_a_mismatched_contract_is_recorded_as_a_mismatch_not_as_baseline(
    wired: StubCI, isolated_home: Path, tmp_path: Path
) -> None:
    """The deliberately skewed half of the joint smoke: both sides must record a
    mismatch, and the CLI's side must never fold it into the CI-off baseline."""
    from tests.ci_schemas import fixture

    raw = fixture("hook-response.experimental-v2.valid")
    raw["contract"] = 1
    wired.respond_json(raw)
    hooks_service.prompt_submitted("q", tmp_path, session_id=SESSION)
    turn = next(
        t for t in metrics_service.recent(session_id=SESSION) if t.trigger == "prompt_submit"
    )
    assert turn.client_reason is ClientReason.contract_mismatch
    summary = metrics_service.summarize(metrics_service.recent(session_id=SESSION))
    assert summary.failed >= 1 and summary.baseline == 0
