"""Draining the spool: one Run per session, buffering when the gateway is down.

The SDK is an optional extra and is not installed in the gate environment (it
shares our top-level package directory — see ``test_sdk_coexistence``), so these
tests stand a fake in its place. That is the right seam anyway: what needs
pinning is the CONTRACT we hold the SDK to — one ``AgentRunTracer`` per session,
``run_id`` = the board session id, spans nested inside it, ``flush`` before we
call anything delivered. Whether the real SDK then reaches stg is the E2E lane's
verdict, not a unit test's.
"""

from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import insights, outbox
from aisquare.core.config import AppConfig, save_config
from aisquare.core.store import store_session
from aisquare.core.workspace import active_project
from aisquare.models import TeamSession
from aisquare.services import explainability as service
from tests import winacl


@dataclass
class FakeRun:
    agent_name: str
    run_id: str
    spans: list[tuple[str, str]] = field(default_factory=list)
    status: str = ""

    def __enter__(self) -> FakeRun:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def set_input(self, value: str) -> None:
        self.spans.append(("input", value))

    def set_status(self, value: str) -> None:
        self.status = value


@dataclass
class FakeSpan:
    kind: str
    detail: str
    sink: list[tuple[str, str]]

    def __enter__(self) -> FakeSpan:
        self.sink.append((self.kind, self.detail))
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def set_selected(self, value: str, reason: str = "") -> None:
        self.sink.append(("selected", f"{value}|{reason}"))


class FakeSDK:
    """Stands in for ``aisquare.explainability``."""

    def __init__(self, *, fail_on_run: bool = False) -> None:
        self.runs: list[FakeRun] = []
        self.spans: list[tuple[str, str]] = []
        self.flushes = 0
        self.init_kwargs: dict[str, Any] | None = None
        self.fail_on_run = fail_on_run

    def init_from_env(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs

    def AgentRunTracer(self, *, agent_name: str, run_id: str) -> FakeRun:
        if self.fail_on_run:
            raise ConnectionError("gateway unreachable")
        run = FakeRun(agent_name=agent_name, run_id=run_id)
        self.runs.append(run)
        return run

    def HumanInterventionTracer(self, *, human_id: str, action: str, reason: str) -> FakeSpan:
        return FakeSpan(f"human:{action}", f"{human_id}|{reason}", self.spans)

    def DecisionTracer(self, *, decision_type: str) -> FakeSpan:
        return FakeSpan(f"decision:{decision_type}", "", self.spans)

    def flush(self) -> None:
        self.flushes += 1


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> FakeSDK:
    """Install a fake SDK and make the service believe the extra is present."""
    fake = FakeSDK()
    monkeypatch.setattr(service, "sdk_available", lambda: True)
    monkeypatch.setattr(service, "_init_sdk", lambda settings, api_key: fake)
    return fake


@pytest.fixture(autouse=True)
def _configured(monkeypatch: pytest.MonkeyPatch) -> None:
    insights.reset_cache()
    monkeypatch.delenv(service.KEY_ENV_VAR, raising=False)
    monkeypatch.delenv(service.GATEWAY_ENV_VAR, raising=False)


def _configure(*, ship: bool = True, key: str | None = "wk-test") -> None:
    config = AppConfig()
    config.explainability.ship = ship
    config.explainability.gateway_url = "https://gateway.example"
    save_config(config)
    if key is not None:
        service.store_api_key(key)
    insights.reset_cache()


def _spool(session_id: str | None, kind: str = "prompt", **extra: object) -> None:
    outbox.enqueue(
        {"v": insights.RECORD_VERSION, "kind": kind, "session_id": session_id, "text": "t", **extra}
    )


# --- one session = one Run ---


def test_a_session_ships_as_one_run_keyed_by_its_board_id(sdk: FakeSDK) -> None:
    _configure()
    for _ in range(3):
        _spool("8dd460fb")

    report = service.ship_once()

    assert report.sent == 3
    assert len(sdk.runs) == 1, "three insights from one session are one Run, not three"
    assert sdk.runs[0].run_id == "8dd460fb", (
        "run_id must be the board session id — the same value the proxy sends as X-Pipeline-Id"
    )
    assert sdk.flushes == 1
    assert outbox.pending() == []


def test_separate_sessions_get_separate_runs(sdk: FakeSDK) -> None:
    _configure()
    _spool("session-a")
    _spool("session-b")

    service.ship_once()

    assert sorted(run.run_id for run in sdk.runs) == ["session-a", "session-b"]


def test_a_second_drain_reuses_the_same_run_id(sdk: FakeSDK) -> None:
    """Run fragmentation doctrine: a later drain rejoins the session's Run."""
    _configure()
    _spool("8dd460fb")
    service.ship_once()
    _spool("8dd460fb")
    service.ship_once()

    assert [run.run_id for run in sdk.runs] == ["8dd460fb", "8dd460fb"]


def test_unattributed_records_share_one_run_rather_than_one_each(sdk: FakeSDK) -> None:
    _configure()
    _spool(None)
    _spool(None)

    service.ship_once()

    assert len(sdk.runs) == 1
    assert sdk.runs[0].run_id == service.UNATTRIBUTED_RUN


def test_the_run_is_attributed_to_the_sessions_board_role(sdk: FakeSDK, runner: CliRunner) -> None:
    """A planner's Run must not be filed under a coder's identity."""
    _configure()
    with store_session() as store:
        project = active_project(store)
        now = datetime.now(tz=UTC)
        store.upsert_session(
            TeamSession(
                id="sess-planner",
                project_id=project.id,
                role="planner",
                started_at=now,
                last_seen_at=now,
            )
        )
    _spool("sess-planner")

    service.ship_once()

    assert sdk.runs[0].agent_name == "aisquare-planner"


def test_prompts_and_board_events_land_in_the_same_run(sdk: FakeSDK) -> None:
    _configure()
    _spool("s1", "prompt")
    _spool("s1", "team_event", event_kind="note", seq=21974)

    service.ship_once()

    assert len(sdk.runs) == 1
    kinds = [kind for kind, _ in sdk.spans]
    assert "human:prompt" in kinds
    assert "decision:board.note" in kinds
    assert any("21974" in detail for _, detail in sdk.spans), (
        "the board seq must ride along — it is the join key back to the row"
    )


# --- "Gateway unreachable ⇒ CLI unaffected, events buffered, status says so" ---


def test_an_unreachable_gateway_buffers_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure()
    _spool("s1")
    _spool("s1")
    broken = FakeSDK(fail_on_run=True)
    monkeypatch.setattr(service, "sdk_available", lambda: True)
    monkeypatch.setattr(service, "_init_sdk", lambda settings, api_key: broken)

    report = service.ship_once()

    assert report.sent == 0
    assert report.deferred == 2
    assert "unreachable" in report.reason
    assert len(outbox.pending()) == 2, "a failed drain must leave every record queued"
    assert service.shipping_state().queued == 2


def test_a_failed_drain_leaves_no_record_claimed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise the records are invisible to status and stuck until reclaim."""
    _configure()
    _spool("s1")
    broken = FakeSDK(fail_on_run=True)
    monkeypatch.setattr(service, "sdk_available", lambda: True)
    monkeypatch.setattr(service, "_init_sdk", lambda settings, api_key: broken)

    service.ship_once()

    assert list(outbox.queue_dir().glob("*.claimed")) == []


def test_an_sdk_that_will_not_start_defers_rather_than_loses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure()
    _spool("s1")
    monkeypatch.setattr(service, "sdk_available", lambda: True)

    def _boom(settings: object, api_key: object) -> None:
        raise RuntimeError("no OTEL exporter")

    monkeypatch.setattr(service, "_init_sdk", _boom)

    report = service.ship_once()

    assert report.deferred == 1
    assert len(outbox.pending()) == 1


# --- refusals: never drain into a half-configured state ---


def test_nothing_ships_when_shipping_is_off(sdk: FakeSDK) -> None:
    _configure(ship=False)
    _spool("s1")

    report = service.ship_once()

    assert report.sent == 0
    assert sdk.runs == []
    assert "not configured" in report.reason


def test_nothing_ships_without_a_key(sdk: FakeSDK) -> None:
    _configure(key=None)
    _spool("s1")

    report = service.ship_once()

    assert report.sent == 0
    assert "no workspace key" in report.reason
    assert len(outbox.pending()) == 1


def test_missing_extra_buffers_with_the_safe_install_line(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure()
    _spool("s1")
    monkeypatch.setattr(service, "sdk_available", lambda: False)

    report = service.ship_once()

    assert report.deferred == 1
    # The invariant is what the advice must never be, not one exact string: a
    # bare `pip install aisquare[explainability]` is the order that overwrites
    # our __init__. Which SAFE advice appears depends on the install shape —
    # this suite runs from an editable checkout, where the extra shadows us
    # outright and the correct advice is not to install it here at all (see
    # test_editable_install_hint). Both shapes are asserted through the same
    # seam so neither can regress into recommending the bare form.
    assert "pip install aisquare[explainability]" not in report.reason
    assert "aisquare-cli[explainability]" in service.install_hint(editable=False)
    assert "editable" in service.install_hint(editable=True).lower()
    assert len(outbox.pending()) == 1


def test_unreadable_records_are_dead_lettered_not_retried_forever(sdk: FakeSDK) -> None:
    _configure()
    outbox.enqueue({"v": 999, "kind": "prompt", "session_id": "s1", "text": "from the future"})

    report = service.ship_once()

    assert report.dead == 1
    assert outbox.counts().dead == 1
    assert outbox.pending() == []


# --- the operator surface ---


def test_status_stays_silent_on_an_install_that_never_opted_in(runner: CliRunner) -> None:
    result = runner.invoke(app, ["status"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "shipping:" not in result.output, "declining must leave zero behavioural change"


def test_status_reports_queued_sent_and_dead_once_configured(runner: CliRunner) -> None:
    _configure()
    _spool("s1")

    result = runner.invoke(app, ["status"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "shipping: 1 queued, 0 sent, 0 dead-letter" in result.output


def test_status_surfaces_a_queue_left_behind_by_a_disabled_install(runner: CliRunner) -> None:
    """Turning shipping off must not hide records already buffered."""
    _configure()
    _spool("s1")
    service.disable_shipping()

    result = runner.invoke(app, ["status"], catch_exceptions=False)

    assert "1 queued" in result.output


def test_ship_command_reports_and_exits_zero_on_a_deferral(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure()
    _spool("s1")
    monkeypatch.setattr(service, "sdk_available", lambda: False)

    result = runner.invoke(app, ["explainability", "ship"], catch_exceptions=False)

    assert result.exit_code == 0, "buffering is the design working, not a failure"
    assert "buffered" in result.output


def test_ship_command_exits_nonzero_when_records_were_dead_lettered(
    runner: CliRunner, sdk: FakeSDK
) -> None:
    _configure()
    outbox.enqueue({"v": 999, "kind": "prompt", "session_id": "s1"})

    result = runner.invoke(app, ["explainability", "ship"])

    assert result.exit_code == 1


# --- init offers the step, and declining changes nothing ---


def test_init_declined_writes_no_shipping_config(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["init", str(tmp_path), "--no-explainability", "--no-onboard"], catch_exceptions=False
    )

    assert result.exit_code == 0, result.output
    assert "xplainability" not in result.output
    insights.reset_cache()
    assert insights.shipping_enabled() is False


def test_init_mentions_the_step_only_when_it_could_be_accepted(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        service,
        "shipping_offer",
        lambda: service.ShippingOffer(
            available=True, reason="ready", gateway_url="https://gw.example", sdk_installed=True
        ),
    )

    result = runner.invoke(app, ["init", str(tmp_path), "--no-onboard"], catch_exceptions=False)

    assert "aisquare init --explainability" in result.output
    insights.reset_cache()
    assert insights.shipping_enabled() is False, "mentioning the step must not enable it"


def test_init_opt_in_turns_shipping_on(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(service.GATEWAY_ENV_VAR, "https://gw.example")
    monkeypatch.setenv(service.KEY_ENV_VAR, "wk-test")
    monkeypatch.setattr(service, "sdk_available", lambda: True)

    result = runner.invoke(
        app, ["init", str(tmp_path), "--explainability", "--no-onboard"], catch_exceptions=False
    )

    assert result.exit_code == 0, result.output
    insights.reset_cache()
    assert insights.shipping_enabled() is True
    assert "no model traffic" in result.output, "the user must be told what is captured"


def test_init_opt_in_without_a_key_refuses_rather_than_buffering_forever(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(service.GATEWAY_ENV_VAR, "https://gw.example")
    monkeypatch.setattr(service, "sdk_available", lambda: True)

    result = runner.invoke(
        app, ["init", str(tmp_path), "--explainability", "--no-onboard"], catch_exceptions=False
    )

    assert result.exit_code == 0, result.output
    insights.reset_cache()
    assert insights.shipping_enabled() is False
    assert "not configured" in result.output


def test_init_never_prompts_when_stdin_is_not_a_terminal(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hook or a CI job must not hang on a question nobody can answer."""
    asked = False

    def _confirm(*args: object, **kwargs: object) -> bool:
        nonlocal asked
        asked = True
        return True

    monkeypatch.setattr("typer.confirm", _confirm)
    monkeypatch.setattr(service, "sdk_available", lambda: True)

    runner.invoke(app, ["init", str(tmp_path), "--no-onboard"], catch_exceptions=False)

    assert asked is False


def test_the_key_never_lands_in_config_toml(monkeypatch: pytest.MonkeyPatch) -> None:
    """config.toml is a settings file people paste into issues."""
    from aisquare.core import paths

    service.configure_shipping(gateway_url="https://gw.example", api_key="wk-secret")

    assert "wk-secret" not in paths.config_path().read_text(encoding="utf-8")
    assert service.key_path().read_text(encoding="utf-8") == "wk-secret"
    # Per platform: `st_mode` reports 0o666 on NTFS however the file is really
    # protected, so an unconditional 0o600 would fail on Windows while saying
    # nothing about who can actually read the key.
    if sys.platform == "win32":
        granted = winacl.dacl_trustees(service.key_path())
        assert granted, "no ACEs read back from the workspace key"
        mine = winacl.user_trustees(granted, winacl.current_user_sid())
        assert mine, granted
        assert not (granted - winacl.PRIVILEGED_TRUSTEES - mine), granted
    else:
        assert service.key_path().stat().st_mode & 0o777 == 0o600


def test_the_sdk_probe_does_not_import_the_sdk() -> None:
    """Importing it costs opentelemetry + httpx; status must stay cheap."""
    service.sdk_available()

    assert service.SDK_MODULE not in sys.modules


def test_fake_sdk_module_shape_matches_what_the_service_calls() -> None:
    """Guard against the fake drifting from the names the service uses."""
    module = types.SimpleNamespace(
        **{name: getattr(FakeSDK(), name) for name in dir(FakeSDK()) if not name.startswith("_")}
    )
    for name in (
        "init_from_env",
        "AgentRunTracer",
        "HumanInterventionTracer",
        "DecisionTracer",
        "flush",
    ):
        assert hasattr(module, name)


# --- the two lanes converge on ONE Run, even when the launch could not be joined ---


def test_the_run_key_env_var_matches_the_launcher() -> None:
    """core.insights duplicates the name to stay off the heavy import path."""
    assert insights.RUN_KEY_ENV_VAR == service.PIPELINE_ID_ENV_VAR


def test_insights_captured_inside_a_traced_session_key_on_its_pipeline_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launcher already decided which Run this process lives in; agree.

    On an unjoinable launch (wrapper binary, --resume, --continue) the pipeline
    id is NOT the board session id. Keying on the board id there would file our
    prompts in a second Run while the model traffic went to the first.
    """
    _configure()
    monkeypatch.setenv(insights.RUN_KEY_ENV_VAR, "minted-pipeline-id")

    insights.record_prompt("inside a traced session", session_id="board-session-id")

    record = json.loads(outbox.pending()[0].read_text(encoding="utf-8"))
    assert record["run_key"] == "minted-pipeline-id"
    assert record["session_id"] == "board-session-id", (
        "the board id must still travel — it is how the span joins back to the row"
    )


def test_an_unjoined_session_ships_into_the_proxys_run(
    sdk: FakeSDK, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure()
    monkeypatch.setenv(insights.RUN_KEY_ENV_VAR, "minted-pipeline-id")
    insights.record_prompt("p", session_id="board-session-id")
    monkeypatch.delenv(insights.RUN_KEY_ENV_VAR)

    service.ship_once()

    assert sdk.runs[0].run_id == "minted-pipeline-id"


def test_identity_still_comes_from_the_board_role_when_the_keys_differ(
    sdk: FakeSDK, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A role lookup by pipeline id would miss and mis-file a planner's Run."""
    _configure()
    with store_session() as store:
        project = active_project(store)
        now = datetime.now(tz=UTC)
        store.upsert_session(
            TeamSession(
                id="board-planner",
                project_id=project.id,
                role="planner",
                started_at=now,
                last_seen_at=now,
            )
        )
    monkeypatch.setenv(insights.RUN_KEY_ENV_VAR, "minted-pipeline-id")
    insights.record_prompt("p", session_id="board-planner")
    monkeypatch.delenv(insights.RUN_KEY_ENV_VAR)

    service.ship_once()

    assert sdk.runs[0].run_id == "minted-pipeline-id"
    assert sdk.runs[0].agent_name == "aisquare-planner"


def test_outside_a_traced_session_the_board_id_is_still_the_run_key(sdk: FakeSDK) -> None:
    _configure()
    insights.record_prompt("p", session_id="board-only")

    service.ship_once()

    assert sdk.runs[0].run_id == "board-only"


def test_a_spool_from_an_older_cli_still_ships(sdk: FakeSDK) -> None:
    """An upgrade mid-shift must not orphan whatever was already buffered."""
    _configure()
    outbox.enqueue({"v": 1, "kind": "prompt", "session_id": "old-session", "text": "t"})

    report = service.ship_once()

    assert report.sent == 1
    assert report.dead == 0
    assert sdk.runs[0].run_id == "old-session"
