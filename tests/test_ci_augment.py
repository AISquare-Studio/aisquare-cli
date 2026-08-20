"""Prompt augmentation: the experiment, and the promise that off changes nothing.

The load-bearing test in this file is the boring one — with the experiment off,
what the hook returns is byte-identical to what it returned before any of this
existed. Everything else is downstream of that holding.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aisquare.core.injection import build_retrieved_block, load_last
from aisquare.services import ci_augment, ci_client
from aisquare.services import hooks as hooks_service
from aisquare.services import metrics as metrics_service
from tests.stub_ci_server import StubCI, serve

_SESSION = "ses_aug"


@pytest.fixture
def stub() -> Iterator[StubCI]:
    yield from serve()


@pytest.fixture
def wired(stub: StubCI, monkeypatch: pytest.MonkeyPatch) -> StubCI:
    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "1")
    monkeypatch.setenv(ci_client.URL_ENV_VAR, stub.url)
    return stub


def _inject(context: str, sources: list[str] | None = None) -> dict[str, object]:
    return {
        "contract": 1,
        "action": "inject",
        "context": context,
        "provenance": [{"node_id": f"n{i}", "source": s} for i, s in enumerate(sources or [])],
        "server_ms": 30,
    }


# --- off changes nothing ------------------------------------------------------


def test_off_returns_exactly_what_the_hook_returned_before(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The property that makes this safe to ship to everyone."""
    monkeypatch.delenv(ci_client.ENABLED_ENV_VAR, raising=False)
    assert hooks_service.prompt_submitted("hello", tmp_path, session_id=None) == ""


def test_off_makes_no_request(
    stub: StubCI, isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ci_client.ENABLED_ENV_VAR, raising=False)
    monkeypatch.setenv(ci_client.URL_ENV_VAR, stub.url)
    hooks_service.prompt_submitted("hello", tmp_path, session_id=_SESSION)
    hooks_service.session_start_context(tmp_path, session_id=_SESSION)
    assert stub.call_count == 0


def test_off_still_records_the_turn(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off is the baseline, not a blackout."""
    monkeypatch.delenv(ci_client.ENABLED_ENV_VAR, raising=False)
    hooks_service.prompt_submitted("hello", tmp_path, session_id=_SESSION)
    (turn,) = metrics_service.recent(session_id=_SESSION)
    assert turn.degradation_reason == "disabled"
    assert turn.injected_chars is None


def test_push_off_under_an_enabled_master_injects_nothing(
    wired: StubCI, isolated_home: Path, tmp_path: Path
) -> None:
    from aisquare.core.config import AppConfig, ExperimentSettings, save_config

    save_config(AppConfig(experiment=ExperimentSettings(enabled=True, push=False)))
    wired.respond_json(_inject("secret sauce"))
    assert hooks_service.prompt_submitted("hello", tmp_path, session_id=None) == ""
    assert wired.call_count == 0


# --- the experiment -----------------------------------------------------------


def test_retrieved_documents_reach_the_prompt(
    wired: StubCI, isolated_home: Path, tmp_path: Path
) -> None:
    wired.respond_json(_inject("- brain.py locks via msvcrt", ["src/aisquare/core/brain.py"]))
    out = hooks_service.prompt_submitted("how does the lock work", tmp_path, session_id=None)
    assert "brain.py locks via msvcrt" in out
    assert "src/aisquare/core/brain.py" in out


def test_retrieved_material_is_framed_as_candidate_not_fact(
    wired: StubCI, isolated_home: Path, tmp_path: Path
) -> None:
    """Presented as plain context it reads as established fact and gets acted on
    unchecked; the framing is part of the experiment, not decoration."""
    wired.respond_json(_inject("- something possibly wrong"))
    out = hooks_service.prompt_submitted("q", tmp_path, session_id=None)
    assert "you did not fetch this" in out
    assert "may be incomplete or" in out
    assert "open the cited source" in out


def test_the_turn_records_what_was_injected(
    wired: StubCI, isolated_home: Path, tmp_path: Path
) -> None:
    wired.respond_json(_inject("- a document"))
    hooks_service.prompt_submitted("q", tmp_path, session_id=_SESSION)
    (turn,) = metrics_service.recent(session_id=_SESSION)
    assert turn.ci_action == "inject"
    assert turn.degradation_reason == "none"
    assert turn.injected_chars and turn.injected_chars > 0


def test_why_can_account_for_retrieved_context(
    wired: StubCI, isolated_home: Path, tmp_path: Path
) -> None:
    wired.respond_json(_inject("- a document", ["docs/a.md", "docs/b.md"]))
    hooks_service.prompt_submitted("q", tmp_path, session_id=_SESSION)
    record = load_last()
    assert record is not None
    assert record.retrieved_chars > 0
    assert record.retrieved_sources == ["docs/a.md", "docs/b.md"]


def test_the_prompt_is_what_gets_sent(wired: StubCI, isolated_home: Path, tmp_path: Path) -> None:
    hooks_service.prompt_submitted("why does the lock use msvcrt", tmp_path, session_id=_SESSION)
    sent = wired.requests[0]
    assert sent["trigger"] == "prompt_submit"
    assert sent["prompt"] == "why does the lock use msvcrt"
    assert sent["trace_id"].startswith("trc_")


def test_session_start_warms_and_may_contribute(
    wired: StubCI, isolated_home: Path, tmp_path: Path
) -> None:
    wired.respond_json(_inject("- session bundle"))
    out = hooks_service.session_start_context(tmp_path, session_id=_SESSION)
    assert "session bundle" in out
    assert wired.requests[0]["trigger"] == "session_start"


def test_the_team_delta_still_comes_first(
    wired: StubCI, isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retrieved material sits closest to the prompt and is what the agent
    should weigh least; the existing output keeps its position."""
    from aisquare.services import team as team_service

    monkeypatch.setattr(team_service, "hook_prompt_heartbeat", lambda *a, **k: "TEAM DELTA")
    wired.respond_json(_inject("- retrieved"))
    out = hooks_service.prompt_submitted("q", tmp_path, session_id=_SESSION)
    assert out.index("TEAM DELTA") < out.index("Possibly relevant")


# --- nothing the endpoint does is visible to the session ----------------------


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (500, "boom"),
        (200, "<html>not json</html>"),
        (200, '{"contract": 99, "action": "inject", "context": "skewed"}'),
        (200, '{"contract": 1, "action": "noop"}'),
        (200, '{"contract": 1, "action": "allow"}'),
        (200, '{"contract": 1, "action": "inject"}'),
        (200, '{"contract": 1, "action": "inject", "context": "   "}'),
    ],
)
def test_a_useless_response_injects_nothing(
    wired: StubCI, isolated_home: Path, tmp_path: Path, status: int, body: str
) -> None:
    wired.respond(status=status, body=body)
    assert hooks_service.prompt_submitted("q", tmp_path, session_id=None) == ""


def test_a_dead_endpoint_leaves_the_turn_untouched(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "1")
    monkeypatch.setenv(ci_client.URL_ENV_VAR, "http://127.0.0.1:1")
    assert hooks_service.prompt_submitted("q", tmp_path, session_id=_SESSION) == ""
    (turn,) = metrics_service.recent(session_id=_SESSION)
    assert turn.degradation_reason == "transport_error"


def test_a_degraded_call_records_no_injection(
    wired: StubCI, isolated_home: Path, tmp_path: Path
) -> None:
    wired.respond(status=500, body="boom")
    hooks_service.prompt_submitted("q", tmp_path, session_id=_SESSION)
    (turn,) = metrics_service.recent(session_id=_SESSION)
    assert turn.injected_chars is None
    assert load_last() is None


def test_an_empty_prompt_is_never_sent(wired: StubCI, isolated_home: Path, tmp_path: Path) -> None:
    """Nothing to retrieve against, and it would still cost the round trip."""
    hooks_service.prompt_submitted("   ", tmp_path, session_id=_SESSION)
    assert wired.call_count == 0


# --- the block ----------------------------------------------------------------


def test_the_block_survives_a_source_repeated() -> None:
    block = build_retrieved_block("body", ["a.md", "a.md", "b.md"])
    assert block.count("a.md") == 1


def test_the_block_omits_the_sources_line_when_there_are_none() -> None:
    assert "Sources:" not in build_retrieved_block("body", [])


def test_augmentation_defaults_to_nothing() -> None:
    empty = ci_augment.Augmentation()
    assert empty.block == ""
    assert empty.call is None
