"""One session, ONE Run — the trace id is the key, and the launcher owns it.

Measured against production on 2026-09-09 (workspace 881, studio 748): a single
``aisquare launch coder -p …`` produced TWO Runs. ``5efb96de…`` held the model
traffic (157,756 tokens, $3.16) under ``aisquare-coder``; ``6fb49942…`` held the
client lane's spans for the same session, zero tokens, same agent name. The
gateway materialises a Run per OTel ``trace_id`` (``trace_states.trace_id`` IS
``run_id``), and the two lanes never agreed on one: the proxy minted a random
trace per ``X-Pipeline-Id`` session, and ``ship_once`` opened ``AgentRunTracer``,
which starts a NEW trace unconditionally. The shared ``X-Pipeline-Id`` was an
attribute on each root, not the key. ``docs/explainability-tracing-boundary.md``
had flagged the merge as ``[unverified]``; this is the measurement, and the fix.

The fix makes the pipeline id the SOURCE of the trace id (``trace_identity``):
the launcher posts the Run's root span with that id and hands the proxy a
``traceparent`` naming it (the proxy's tier-2 path), and the shipper attaches a
segment under that same root. Every piece below is pinned in isolation, and
the launch path once through the CLI.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import launch as launch_cli
from aisquare.cli.app import app
from aisquare.core import insights, outbox
from aisquare.core.config import AppConfig, save_config
from aisquare.services import explainability as service
from aisquare.services import explainability_ops as ops
from aisquare.services.explainability import (
    ProxyProbe,
    RootReceipt,
    SessionWiring,
    disown_inherited_trace,
    record_join,
    trace_identity,
    trace_marker,
    wire_session,
)

_W3C = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-01$")


@pytest.fixture
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Intercept the exec so the agent is never really launched (as test_launch does)."""
    import shutil

    captured: dict[str, Any] = {}

    def fake_exec(binary: str, argv: list[str], env: dict[str, str]) -> None:
        captured.update(binary=binary, argv=argv, env=env)

    monkeypatch.setattr(launch_cli, "_exec", fake_exec)
    monkeypatch.setattr(shutil, "which", lambda cmd: f"/usr/local/bin/{cmd}")
    return captured


def _settings(**overrides: object) -> Any:
    from aisquare.core.config import ExplainabilitySettings

    return ExplainabilitySettings(enabled=True, **overrides)


def _healthy(url: str) -> ProxyProbe:
    return ProxyProbe(True, "proxy healthy")


def _posted(*_args: str) -> RootReceipt:
    return RootReceipt(posted=True, detail="HTTP 202")


def _refused(*_args: str) -> RootReceipt:
    return RootReceipt(posted=False, detail="HTTP 409: agent_not_registered")


# ── the derivation ───────────────────────────────────────────────────────────


def test_the_trace_id_is_a_pure_function_of_the_pipeline_id() -> None:
    a = trace_identity("79253ef1-2106-47cf-a668-c5d315155acf")
    b = trace_identity("79253ef1-2106-47cf-a668-c5d315155acf")
    assert a == b, "launcher, shipper and a human must all compute the same key"
    assert re.fullmatch(r"[0-9a-f]{32}", a.trace_id)
    assert re.fullmatch(r"[0-9a-f]{16}", a.span_id)
    assert _W3C.match(a.traceparent)


def test_two_pipeline_ids_never_share_a_run() -> None:
    assert trace_identity("session-a").trace_id != trace_identity("session-b").trace_id


def test_the_all_zero_ids_the_w3c_spec_rejects_are_never_produced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``extract()`` drops an all-zero id SILENTLY and the proxy would key the
    Run itself — the exact failure this module refuses. Forced, since no real
    input reaches it."""
    import hashlib

    class _Zero:
        def digest(self) -> bytes:
            return b"\x00" * 32

    monkeypatch.setattr(hashlib, "sha256", lambda _data: _Zero())
    identity = trace_identity("anything")
    assert identity.trace_id != "0" * 32
    assert identity.span_id != "0" * 16
    assert _W3C.match(identity.traceparent)


# ── the wiring: who keys the Run ─────────────────────────────────────────────


def test_a_posted_root_puts_traceparent_on_the_wire_and_retires_x_pipeline_id() -> None:
    wiring = wire_session(
        _settings(),
        "coder",
        session_id="sess-1",
        api_key="k",
        gateway_url="https://gateway.example",
        prober=_healthy,
        root_opener=_posted,
    )
    headers = wiring.env["ANTHROPIC_CUSTOM_HEADERS"]
    identity = trace_identity("sess-1")
    assert f"traceparent: {identity.traceparent}" in headers
    # Tier 1 beats tier 2 in the proxy: with X-Pipeline-Id present it opens its
    # own session and never reads the traceparent beside it.
    assert "X-Pipeline-Id" not in headers, "sending both changes nothing"
    assert "X-Agent-Name: aisquare-coder" in headers
    assert wiring.owns_trace is True
    assert wiring.trace_id == identity.trace_id
    assert wiring.pipeline_id == "sess-1", "the pipeline id is still the board key"
    assert f"run {identity.trace_id}" in wiring.reason


def test_a_refused_root_falls_back_to_the_proxy_keyed_run() -> None:
    wiring = wire_session(
        _settings(),
        "coder",
        session_id="sess-1",
        api_key="k",
        gateway_url="https://gateway.example",
        prober=_healthy,
        root_opener=_refused,
    )
    headers = wiring.env["ANTHROPIC_CUSTOM_HEADERS"]
    assert wiring.traced is True, "a failed post costs the join, never the trace"
    assert "X-Pipeline-Id: sess-1" in headers
    assert "traceparent" not in headers
    assert wiring.owns_trace is False
    assert wiring.trace_id is None, "an unused derived id must not be reported as the key"
    assert "agent_not_registered" in wiring.reason


@pytest.mark.parametrize(
    ("api_key", "gateway_url", "why"),
    [
        (None, "https://gateway.example", "no workspace key"),
        ("k", None, "no gateway URL"),
        (None, None, "no gateway URL"),
    ],
)
def test_without_a_gateway_and_a_key_the_wiring_is_the_pre_fix_shape(
    api_key: str | None, gateway_url: str | None, why: str
) -> None:
    """A loopback sidecar with no key is the documented local topology; it keeps
    working byte-for-byte, and the reason says which half is missing."""
    calls: list[tuple[str, ...]] = []

    def spy(*args: str) -> RootReceipt:
        calls.append(args)
        return RootReceipt(True, "")

    wiring = wire_session(
        _settings(),
        "coder",
        session_id="sess-1",
        api_key=api_key,
        gateway_url=gateway_url,
        prober=_healthy,
        root_opener=spy,
    )
    assert calls == [], "nothing to post with — the gateway must not be dialled"
    assert "X-Pipeline-Id: sess-1" in wiring.env["ANTHROPIC_CUSTOM_HEADERS"]
    assert wiring.owns_trace is False
    assert why in wiring.reason


def test_the_default_opener_is_looked_up_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same seam as ``probe_proxy``: patching the module reaches every caller,
    so a test through the CLI never dials a real gateway by accident."""
    seen: list[tuple[str, str, str, str]] = []

    def fake(gateway_url: str, api_key: str, agent_name: str, pipeline_id: str) -> RootReceipt:
        seen.append((gateway_url, api_key, agent_name, pipeline_id))
        return RootReceipt(True, "HTTP 202")

    monkeypatch.setattr(service, "_post_run_root", fake)
    wiring = wire_session(
        _settings(),
        "planner",
        session_id="s",
        api_key="k",
        gateway_url="https://g.example",
        prober=_healthy,
    )
    assert seen == [("https://g.example", "k", "aisquare-planner", "s")]
    assert wiring.owns_trace


# ── the root span the launcher posts ─────────────────────────────────────────


def test_the_root_span_carries_the_derived_ids_and_the_routing_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_request(url: str, **kwargs: Any) -> ops.HttpVerdict:
        captured["url"] = url
        captured.update(kwargs)
        return ops.HttpVerdict(ok=True, status=202, detail="HTTP 202")

    monkeypatch.setattr(ops, "_request", fake_request)
    verdict = ops.open_run_root("https://g.example/", "k", "aisquare-coder", "sess-1")
    assert verdict.ok
    assert captured["url"] == "https://g.example/v1/traces/ingest"
    assert captured["api_key"] == "k"
    identity = trace_identity("sess-1")
    body = captured["body"]
    assert body["trace_id"] == identity.trace_id
    (span,) = body["spans"]
    assert span["span_id"] == identity.span_id, "the traceparent names THIS span as parent"
    assert span["parent_span_id"] is None, "a TRUE root, so the trace is routed on arrival"
    assert span["name"] == "AgentRun:aisquare-coder"
    assert span["attributes"]["agent.name"] == "aisquare-coder", "what routes the trace"
    assert span["attributes"]["agent.run_id"] == "sess-1", "by-agent-run-id keeps working"
    assert span["attributes"]["openinference.span.kind"] == "AGENT"
    assert json.dumps(body), "the batch must be JSON-serialisable"


def test_a_200_from_something_in_front_of_the_gateway_is_not_a_posted_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ops,
        "_request",
        lambda url, **kw: ops.HttpVerdict(ok=True, status=200, detail="HTTP 200"),
    )
    verdict = ops.open_run_root("https://g.example", "k", "aisquare-coder", "sess-1")
    assert not verdict.ok
    assert "202" in verdict.detail


def test_an_unreachable_gateway_is_a_verdict_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ops,
        "_request",
        lambda url, **kw: ops.HttpVerdict(ok=False, status=None, detail="unreachable: refused"),
    )
    verdict = ops.open_run_root("https://g.example", "k", "aisquare-coder", "sess-1")
    assert not verdict.ok
    assert "unreachable" in verdict.detail


# ── the marker and the join record name the Run ──────────────────────────────


def test_an_owned_run_key_travels_in_the_marker_and_is_disowned_with_it() -> None:
    owned = SessionWiring(
        traced=True,
        reason="",
        env={"ANTHROPIC_BASE_URL": "x", "ANTHROPIC_CUSTOM_HEADERS": "y"},
        agent_name="aisquare-coder",
        pipeline_id="p-1",
        trace_id="ab" * 16,
        owns_trace=True,
    )
    marker = trace_marker(owned)
    assert marker[service.RUN_TRACE_ID_ENV_VAR] == "ab" * 16
    env = {**owned.env, **marker}
    assert disown_inherited_trace(env) == "p-1"
    assert service.RUN_TRACE_ID_ENV_VAR not in env, "a child must not inherit the parent's Run key"


def test_a_proxy_keyed_run_puts_no_trace_id_in_the_marker() -> None:
    fallback = SessionWiring(
        traced=True, reason="", env={"a": "b"}, agent_name="aisquare-coder", pipeline_id="p-1"
    )
    assert service.RUN_TRACE_ID_ENV_VAR not in trace_marker(fallback)


def test_the_join_record_names_the_run_a_human_can_open(isolated_home: Path) -> None:
    record_join(session_id="board-1", pipeline_id="board-1", trace_id="cd" * 16)
    record_join(session_id="board-2", pipeline_id="board-2")
    owned, proxy_keyed = service.join_records()
    assert owned["trace_id"] == "cd" * 16
    assert proxy_keyed["trace_id"] is None, "unknown is recorded as unknown, never derived"


def test_the_hook_copies_the_owned_key_from_the_environment(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.services import hooks

    monkeypatch.setenv(service.PIPELINE_ID_ENV_VAR, "p-1")
    monkeypatch.setenv(service.TRACE_AGENT_NAME_ENV_VAR, "aisquare-coder")
    monkeypatch.setenv(service.RUN_TRACE_ID_ENV_VAR, "ef" * 16)
    assert hooks.record_trace_join("board-9") is None
    (record,) = service.join_records()
    assert record["trace_id"] == "ef" * 16


# ── the shipper attaches to the launched Run ────────────────────────────────


class _FakeSpan:
    def __init__(self, name: str, context: Any, attributes: dict[str, Any]) -> None:
        self.name = name
        self.parent_context = context
        self.attributes = dict(attributes)
        self.ended = False
        self.status: Any = None

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_status(self, code: Any, description: str | None = None) -> None:
        self.status = (code, description)

    def end(self) -> None:
        self.ended = True


class _FakeTracer:
    def __init__(self, sink: list[_FakeSpan]) -> None:
        self.sink = sink

    def start_span(self, name: str, *, context: Any, attributes: dict[str, Any]) -> _FakeSpan:
        span = _FakeSpan(name, context, attributes)
        self.sink.append(span)
        return span


class _FakeOtelTrace:
    class TraceFlags:
        SAMPLED = 1

        def __init__(self, flags: int) -> None:
            self.flags = flags

    class SpanContext:
        def __init__(
            self, *, trace_id: int, span_id: int, is_remote: bool, trace_flags: Any
        ) -> None:
            self.trace_id = trace_id
            self.span_id = span_id
            self.is_remote = is_remote

    class NonRecordingSpan:
        def __init__(self, ctx: Any) -> None:
            self.ctx = ctx

    class StatusCode:
        OK = "OK"
        ERROR = "ERROR"

    @staticmethod
    def set_span_in_context(span: Any) -> dict[str, Any]:
        return {"span": span}


class _FakeOtelContext:
    def __init__(self) -> None:
        self.attached: list[Any] = []
        self.detached: list[Any] = []

    def attach(self, ctx: Any) -> object:
        token = object()
        self.attached.append(ctx)
        return token

    def detach(self, token: Any) -> None:
        self.detached.append(token)


class _ShipSdk:
    """The fake ``aisquare.explainability`` a launched drain sees."""

    def __init__(self) -> None:
        self.roots: list[tuple[str, str]] = []
        self.segments: list[_FakeSpan] = []
        self.leaves: list[str] = []
        self.flushes = 0

    def init_from_env(self, **kwargs: Any) -> None:
        return None

    def AgentRunTracer(self, *, agent_name: str, run_id: str) -> Any:
        self.roots.append((agent_name, run_id))
        sdk = self

        class _Run:
            def __enter__(self) -> _Run:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def set_input(self, value: str) -> None:
                sdk.leaves.append(f"input:{value}")

            def set_status(self, value: str) -> None:
                sdk.leaves.append(f"status:{value}")

        return _Run()

    def get_tracer(self, name: str) -> _FakeTracer:
        return _FakeTracer(self.segments)

    def HumanInterventionTracer(self, *, human_id: str, action: str, reason: str) -> Any:
        return self._leaf(f"human:{action}")

    def DecisionTracer(self, *, decision_type: str) -> Any:
        return self._leaf(f"decision:{decision_type}")

    def _leaf(self, label: str) -> Any:
        sdk = self

        class _Leaf:
            def __enter__(self) -> _Leaf:
                sdk.leaves.append(label)
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def set_selected(self, value: str, reason: str = "") -> None:
                return None

        return _Leaf()

    def flush(self) -> None:
        self.flushes += 1


@pytest.fixture
def ship_sdk(monkeypatch: pytest.MonkeyPatch) -> tuple[_ShipSdk, _FakeOtelContext]:
    fake = _ShipSdk()
    otel_context = _FakeOtelContext()
    monkeypatch.setattr(service, "sdk_available", lambda: True)
    monkeypatch.setattr(service, "_init_sdk", lambda settings, api_key: fake)
    monkeypatch.setattr(service, "_otel", lambda: (_FakeOtelTrace, otel_context))
    insights.reset_cache()
    monkeypatch.delenv(service.KEY_ENV_VAR, raising=False)
    monkeypatch.delenv(service.GATEWAY_ENV_VAR, raising=False)
    config = AppConfig()
    config.explainability.ship = True
    config.explainability.gateway_url = "https://gateway.example"
    save_config(config)
    service.store_api_key("wk-test")
    insights.reset_cache()
    return fake, otel_context


def _capture(
    monkeypatch: pytest.MonkeyPatch,
    wiring: SessionWiring | None,
    session_id: str,
    text: str = "t",
) -> None:
    """Spool one prompt the way a process inside ``wiring``'s session would.

    Through the REAL ``insights.record_prompt``, under exactly the env
    ``trace_marker`` exports — ``None`` for a plain session, which carries no
    marker at all. The record's SHAPE is the whole question the sweeper
    branches on, and a hand-built dict is free to have a shape production
    never produces: this helper replaced one that enqueued a record with NO
    run key, which ``_spool`` cannot emit for a session that has an id (see
    ``insights.run_key``'s fallback). That test passed while the code under
    test was wrong.
    """
    for name in (
        service.PIPELINE_ID_ENV_VAR,
        service.TRACE_AGENT_NAME_ENV_VAR,
        service.RUN_TRACE_ID_ENV_VAR,
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in (trace_marker(wiring) if wiring else {}).items():
        monkeypatch.setenv(name, value)
    insights.record_prompt(text, session_id=session_id)


def _launch(session_id: str, **overrides: Any) -> SessionWiring:
    """A wiring as ``aisquare launch`` builds it — owning its Run by default."""
    return wire_session(
        _settings(),
        "cli",
        session_id=session_id,
        api_key=overrides.pop("api_key", "k"),
        gateway_url=overrides.pop("gateway_url", "https://gateway.example"),
        prober=_healthy,
        root_opener=overrides.pop("root_opener", _posted),
        **overrides,
    )


def test_a_launched_sessions_insights_join_the_run_the_launcher_keyed(
    isolated_home: Path,
    ship_sdk: tuple[_ShipSdk, _FakeOtelContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk, otel_context = ship_sdk
    owned = _launch("pipe-1")
    assert owned.owns_trace, "this launch posted the root — that is what earns the segment"
    _capture(monkeypatch, owned, "board-1")
    _capture(monkeypatch, owned, "board-1")
    record = json.loads(outbox.pending()[0].read_text(encoding="utf-8"))
    assert record["run_trace_id"] == trace_identity("pipe-1").trace_id, (
        "the owned key travels in the record — it is the sweeper's only evidence of a root"
    )
    report = service.ship_once()
    assert report.sent == 2, report.reason
    assert sdk.roots == [], "AgentRunTracer opens a NEW trace — that is the bug"
    (segment,) = sdk.segments
    identity = trace_identity("pipe-1")
    parent = segment.parent_context["span"].ctx
    assert parent.trace_id == int(identity.trace_id, 16), "same trace as the model traffic"
    assert parent.span_id == int(identity.span_id, 16), "child of the root the launcher posted"
    assert parent.is_remote is True
    assert segment.name == "AgentRun:aisquare-cli"
    assert segment.attributes["agent.name"] == "aisquare-cli", "routes even before the root lands"
    assert segment.attributes["agent.run_id"] == "pipe-1"
    assert segment.attributes["input.value"] == "aisquare-cli session pipe-1"
    assert segment.attributes["agent.run.status"] == "completed"
    assert segment.ended and segment.status == ("OK", None)
    assert sdk.leaves.count("human:prompt") == 2, "the records nest under the segment"
    assert len(otel_context.attached) == len(otel_context.detached) == 1
    assert sdk.flushes == 1
    assert outbox.pending() == []


def test_a_plain_sessions_insights_still_open_their_own_run(
    isolated_home: Path,
    ship_sdk: tuple[_ShipSdk, _FakeOtelContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No launcher, no proxy lane, nothing to join: the pre-fix path, untouched.

    The record here DOES carry a run key — ``insights.run_key`` falls back to
    the board session id, so every plain session's does. That is exactly the
    trap: read as "launched", it hung this drain on a root nobody posted, and
    the gateway made a rootless Run of a session that had a well-formed one.
    """
    sdk, _ = ship_sdk
    _capture(monkeypatch, None, "board-2")
    record = json.loads(outbox.pending()[0].read_text(encoding="utf-8"))
    assert record["run_key"] == "board-2", "the fallback fires — a run key proves nothing"
    assert record["run_trace_id"] is None, "and nobody posted a root for it"

    service.ship_once()

    assert sdk.roots == [("aisquare-cli", "board-2")]
    assert sdk.segments == [], "a segment here would attach to a root that does not exist"


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"root_opener": _refused}, "the root post was refused"),
        ({"gateway_url": None}, "no gateway URL to post a root to"),
        ({"api_key": None}, "no workspace key to post a root with"),
    ],
)
def test_a_fail_open_launchs_insights_still_open_their_own_run(
    overrides: dict[str, Any],
    why: str,
    isolated_home: Path,
    ship_sdk: tuple[_ShipSdk, _FakeOtelContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Traced, but nobody owns the Run: the proxy keyed one we cannot name.

    ``trace_marker`` still exports the pipeline id here — that is what makes
    fail-open fail OPEN — so the record carries a run key and looks launched.
    It is not joinable: two Runs for one session is the old, known cost of
    this path, and it stays that rather than becoming one headless Run.
    """
    sdk, _ = ship_sdk
    wiring = _launch("pipe-open", **overrides)
    assert wiring.traced and not wiring.owns_trace, why
    _capture(monkeypatch, wiring, "board-open")
    record = json.loads(outbox.pending()[0].read_text(encoding="utf-8"))
    assert record["run_key"] == "pipe-open", "the marker is exported — this LOOKS launched"
    assert record["run_trace_id"] is None, "but no root was posted, so nothing owns the Run"

    service.ship_once()

    assert sdk.roots == [("aisquare-cli", "pipe-open")]
    assert sdk.segments == []


def test_a_segment_that_fails_is_closed_and_the_records_stay_queued(
    isolated_home: Path,
    ship_sdk: tuple[_ShipSdk, _FakeOtelContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk, otel_context = ship_sdk

    def boom(*args: Any, **kwargs: Any) -> None:
        raise ConnectionError("gateway unreachable")

    monkeypatch.setattr(sdk, "HumanInterventionTracer", boom)
    _capture(monkeypatch, _launch("pipe-3"), "board-3")
    report = service.ship_once()
    assert report.sent == 0 and report.deferred == 1
    (segment,) = sdk.segments
    assert segment.ended, "the span is closed on the way out"
    assert segment.status[0] == "ERROR"
    assert len(otel_context.detached) == 1, "the context is detached on the way out"
    assert len(outbox.pending()) == 1


def test_the_sdk_inbox_lives_in_our_home_not_the_cwd(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SDK's default inbox path is RELATIVE, so every drain left
    ``explainability_inbox.db`` (+ -shm/-wal) in whatever directory it ran from."""
    import sys
    import types

    monkeypatch.delenv(service.SDK_INBOX_ENV_VAR, raising=False)
    fake = types.ModuleType(service.SDK_MODULE)
    fake.init_from_env = lambda **kw: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, service.SDK_MODULE, fake)
    service._init_sdk("https://g.example", "k")
    from aisquare.core import paths

    assert Path(service_env := __import__("os").environ[service.SDK_INBOX_ENV_VAR]) == (
        paths.explainability_dir() / "inbox.db"
    ), service_env


def test_an_operators_own_inbox_path_is_left_alone(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    import types

    monkeypatch.setenv(service.SDK_INBOX_ENV_VAR, "/somewhere/theirs.db")
    fake = types.ModuleType(service.SDK_MODULE)
    fake.init_from_env = lambda **kw: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, service.SDK_MODULE, fake)
    service._init_sdk("https://g.example", "k")
    assert __import__("os").environ[service.SDK_INBOX_ENV_VAR] == "/somewhere/theirs.db"


# ── once through the CLI ─────────────────────────────────────────────────────


def test_launch_hands_the_proxy_the_run_the_launcher_owns(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through ``aisquare launch``: the target's gateway and key
    resolve, the root is posted (faked), and the agent's env carries a
    ``traceparent`` — not an ``X-Pipeline-Id`` — plus the marker the hook
    turns into a join record naming that Run."""
    from aisquare.core.config import ExplainabilitySettings, ExplainabilityTarget

    save_config(
        AppConfig(
            explainability=ExplainabilitySettings(
                enabled=True,
                proxy_url="https://proxy.example:9443",
                target="prod",
                targets={
                    "prod": ExplainabilityTarget(
                        gateway_url="https://gateway.example",
                        proxy_url="https://proxy.example:9443",
                    )
                },
            )
        )
    )
    service.store_api_key("wk-test")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    monkeypatch.setattr(service, "probe_proxy", _healthy)
    posted: list[tuple[str, str, str, str]] = []

    def fake_post(gateway_url: str, api_key: str, agent_name: str, pipeline_id: str) -> RootReceipt:
        posted.append((gateway_url, api_key, agent_name, pipeline_id))
        return RootReceipt(True, "HTTP 202")

    monkeypatch.setattr(service, "_post_run_root", fake_post)
    result = runner.invoke(app, ["launch", "coder"])
    assert result.exit_code == 0, result.output
    ((gateway_url, api_key, agent_name, pipeline_id),) = posted
    assert (gateway_url, api_key, agent_name) == (
        "https://gateway.example",
        "wk-test",
        "aisquare-coder",
    )
    headers = spy["env"]["ANTHROPIC_CUSTOM_HEADERS"]
    identity = trace_identity(pipeline_id)
    assert f"traceparent: {identity.traceparent}" in headers
    assert "X-Pipeline-Id" not in headers
    assert spy["env"][service.RUN_TRACE_ID_ENV_VAR] == identity.trace_id
    assert spy["env"][service.PIPELINE_ID_ENV_VAR] == pipeline_id
    assert f"run {identity.trace_id}" in result.output


# ── print-only: `explainability env` claims no Run ───────────────────────────
#
# PR #107 review, blocker 2. With a gateway configured, every invocation of a
# command whose entire job is to PRINT exports posted a root — a parentless,
# already-ended span the worker reads as a terminal batch and MERGE_RUN files
# as a `completed` Run of one 0 ms span and zero tokens, named after the role,
# for a session that may never start. Without `--session-id` the pipeline id is
# a fresh uuid4, so each invocation minted a NEW empty Run rather than
# re-touching one: a second terminal, a shell rc, a `--json` reader, an operator
# inspecting the delta. Plus up to three seconds of WAN I/O behind a print.


class _RootPosted(AssertionError):
    """Raised by the print-only fakes, distinct from any assertion below."""


def _must_not_post(
    gateway_url: str, api_key: str, agent_name: str, pipeline_id: str
) -> RootReceipt:
    """The fake that fails LOUDLY if the print-only path ever reaches the seam."""
    raise _RootPosted(
        f"a print minted a dashboard Run: root for {agent_name} (pipeline {pipeline_id}) "
        f"posted to {gateway_url}"
    )


def test_the_print_only_mode_never_dials_the_gateway() -> None:
    """Gateway AND key in hand — the one state where the default would post."""
    wiring = wire_session(
        _settings(),
        "coder",
        session_id="sess-env",
        api_key="k",
        gateway_url="https://gateway.example",
        prober=_healthy,
        root_opener=_must_not_post,
        post_root=False,
    )
    headers = wiring.env["ANTHROPIC_CUSTOM_HEADERS"]
    assert wiring.traced is True, "print-only costs the OWNERSHIP, never the trace"
    assert "X-Pipeline-Id: sess-env" in headers, "the proxy keys the Run, as before ownership"
    assert "traceparent" not in headers, "a traceparent names a root nobody posted"
    assert wiring.owns_trace is False
    assert wiring.trace_id is None, "an unposted derived id must not be reported as the key"
    assert "root not posted" in wiring.reason and "print-only" in wiring.reason, wiring.reason
    assert service.RUN_TRACE_ID_ENV_VAR not in trace_marker(wiring), (
        "the hook would record a join against a Run that does not exist"
    )


@pytest.fixture
def gateway_target(work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A machine where a launch WOULD own its Run: target gateway, key, https proxy.

    Exactly the launch CLI test's configuration, so the two commands are judged
    on the same footing. The markers are cleared too: this suite runs inside
    traced sessions, and an inherited ``AISQUARE_PIPELINE_ID`` would make the
    spawn path disown a parent that is not part of the test.
    """
    from aisquare.core.config import ExplainabilitySettings, ExplainabilityTarget

    save_config(
        AppConfig(
            explainability=ExplainabilitySettings(
                enabled=True,
                proxy_url="https://proxy.example:9443",
                target="prod",
                targets={
                    "prod": ExplainabilityTarget(
                        gateway_url="https://gateway.example",
                        proxy_url="https://proxy.example:9443",
                    )
                },
            )
        )
    )
    service.store_api_key("wk-test")
    for name in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        service.PIPELINE_ID_ENV_VAR,
        service.TRACE_AGENT_NAME_ENV_VAR,
        service.RUN_TRACE_ID_ENV_VAR,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(service, "probe_proxy", _healthy)


@pytest.mark.parametrize("form", ["shell", "json"])
def test_explainability_env_prints_the_delta_without_minting_a_run(
    form: str, runner: CliRunner, gateway_target: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ZERO network calls to the gateway from a print — asserted two ways.

    The seam fake raises if ``wire_session`` reaches the root post at all, and
    a socket tripwire catches any other route to the network once the probe is
    faked: the exports must come out with the machine's network unplugged.
    ``catch_exceptions=False`` so a trip surfaces as ITS message, not as a
    bare exit 1 that reads like a refused proxy.
    """
    import socket

    monkeypatch.setattr(service, "_post_run_root", _must_not_post)
    attempts: list[str] = []

    class Tripwire(socket.socket):
        def __init__(self, *args: object, **kwargs: object) -> None:
            attempts.append("socket")
            raise _RootPosted("explainability env opened a socket — the print must stay local")

    monkeypatch.setattr(socket, "socket", Tripwire)

    args = ["explainability", "env", "coder", "--session-id", "sess-env"]
    result = runner.invoke(
        app, (["--json", *args] if form == "json" else args), catch_exceptions=False
    )

    assert result.exit_code == 0, result.output
    assert attempts == [], "the print-only path reached the network"
    if form == "json":
        exports = json.loads(result.stdout)["env"]
    else:
        exports = {
            line.split("=", 1)[0].removeprefix("export "): line
            for line in result.output.split("\nexport ")
            if line
        }
    headers = exports["ANTHROPIC_CUSTOM_HEADERS"]
    # The delta a session started from this output carries: the proxy-keyed
    # header pair plus the key, and the two markers that do not depend on
    # ownership. Nothing else moved — a launch's extra marker names a root, and
    # this command posted none.
    assert "X-Pipeline-Id: sess-env" in headers
    assert "X-Agent-Name: aisquare-coder" in headers
    assert "X-AISquare-Key: wk-test" in headers, "the hosted proxy still authenticates"
    assert "traceparent" not in headers
    assert exports.keys() == {
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        service.PIPELINE_ID_ENV_VAR,
        service.TRACE_AGENT_NAME_ENV_VAR,
    }, sorted(exports)
    assert service.RUN_TRACE_ID_ENV_VAR not in result.output


def test_spawn_exec_still_posts_the_root_it_is_about_to_fill(
    runner: CliRunner, gateway_target: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other launch path keeps the default: ``team spawn --exec`` is
    committed to starting the agent, so it owns the Run exactly as ``launch``
    does (pinned above) — traceparent on the wire, the run key in the env."""
    posted: list[tuple[str, str, str, str]] = []

    def fake_post(gateway_url: str, api_key: str, agent_name: str, pipeline_id: str) -> RootReceipt:
        posted.append((gateway_url, api_key, agent_name, pipeline_id))
        return RootReceipt(True, "HTTP 202")

    monkeypatch.setattr(service, "_post_run_root", fake_post)
    seen: dict[str, Any] = {}
    monkeypatch.setattr("aisquare.cli.team.shutil.which", lambda _name: "/usr/bin/claude")
    monkeypatch.setattr(
        "aisquare.cli.team.os.execvpe", lambda file, argv, env: seen.update(argv=argv, env=env)
    )

    result = runner.invoke(app, ["team", "spawn", "coder", "--exec", "--no-probe"])

    assert result.exit_code == 0, result.output
    ((gateway_url, api_key, agent_name, pipeline_id),) = posted
    assert (gateway_url, api_key, agent_name) == (
        "https://gateway.example",
        "wk-test",
        "aisquare-coder",
    )
    identity = trace_identity(pipeline_id)
    headers = seen["env"]["ANTHROPIC_CUSTOM_HEADERS"]
    assert f"traceparent: {identity.traceparent}" in headers
    assert "X-Pipeline-Id" not in headers
    assert seen["env"][service.RUN_TRACE_ID_ENV_VAR] == identity.trace_id
    assert seen["env"][service.PIPELINE_ID_ENV_VAR] == pipeline_id
