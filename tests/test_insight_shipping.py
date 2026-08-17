"""Insights the CLI captures reach the gateway — without ever being in the way.

Issue #50's four acceptance clauses, plus the one property the whole design
exists to protect: the primary path does no network I/O, so an unreachable or
slow gateway cannot cost a prompt, a board write, or an exit code.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import insights, outbox
from aisquare.core.config import AppConfig, load_config, save_config
from aisquare.services import hooks as hooks_service


@pytest.fixture(autouse=True)
def _fresh_settings_cache() -> None:
    """Each test states its own config; nothing may leak through the cache."""
    insights.reset_cache()


def _configure_shipping(*, ship: bool = True) -> None:
    config = AppConfig()
    config.explainability.ship = ship
    config.explainability.gateway_url = "https://gateway.invalid"
    save_config(config)
    insights.reset_cache()


def _records() -> list[dict[str, object]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in outbox.pending()]


# --- "No key/config ⇒ nothing captured for shipping, nothing logged as error" ---


def test_unconfigured_captures_nothing_and_says_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hooks_service.capture_prompt("what does this repo do?", tmp_path, session_id="s1")

    assert outbox.pending() == []
    assert not outbox.root().exists(), "an unconfigured install must not even create a spool"
    captured = capsys.readouterr()
    assert captured.err == ""


def test_unconfigured_board_write_captures_nothing(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["note", "hello"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert outbox.pending() == []


# --- "A session produces prompts and team events, keyed to the session" ---


def test_configured_prompt_is_spooled_with_its_session(tmp_path: Path) -> None:
    _configure_shipping()

    hooks_service.capture_prompt("ship the RC", tmp_path, session_id="8dd460fb")

    (record,) = _records()
    assert record["kind"] == "prompt"
    assert record["text"] == "ship the RC"
    assert record["session_id"] == "8dd460fb", "the session id is the Run key — it must survive"
    assert record["v"] == insights.RECORD_VERSION


def test_configured_board_event_is_spooled_with_its_receipt(runner: CliRunner) -> None:
    _configure_shipping()

    result = runner.invoke(app, ["note", "gate is green"], catch_exceptions=False)
    assert result.exit_code == 0, result.output

    events = [r for r in _records() if r["kind"] == "team_event"]
    note = next(r for r in events if r["event_kind"] == "note")
    assert note["text"] == "gate is green"
    assert isinstance(note["seq"], int) and note["seq"] > 0, (
        "seq is the join key between a board row and the span it becomes"
    )
    assert note["event_id"]


def test_long_prompts_are_clipped_not_dropped(tmp_path: Path) -> None:
    _configure_shipping()

    hooks_service.capture_prompt("x" * 50_000, tmp_path, session_id="s1")

    (record,) = _records()
    text = record["text"]
    assert isinstance(text, str)
    assert len(text) < 50_000
    assert text.endswith("[truncated by aisquare-cli]"), "a clipped span must admit it is clipped"


# --- "Failure to ship must never block or slow the CLI's primary function" ---


def test_primary_path_opens_no_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The strongest form of "never slowed": it cannot reach the network at all.

    A timing bound would pass on a fast machine with a blocking send hidden in
    it. Making the socket constructor itself fail the test is exact: if any
    capture seam ever grows a POST, this goes red on every machine.
    """
    _configure_shipping()

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("the primary path opened a socket — shipping must be out-of-process")

    monkeypatch.setattr(socket, "socket", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)

    hooks_service.capture_prompt("still fast", tmp_path, session_id="s1")

    assert len(_records()) == 1


def test_a_broken_spool_costs_a_trace_not_a_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Read-only home, full disk, permissions — the prompt still lands locally."""
    _configure_shipping()
    # A file where the spool wants a directory: mkdir raises NotADirectoryError,
    # exactly as a read-only or full home would, and only for the spool.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(outbox, "root", lambda: blocker / "explainability")

    hooks_service.capture_prompt("record me locally", tmp_path, session_id="s1")

    from aisquare.core.store import store_session
    from aisquare.core.workspace import active_project

    with store_session() as store:
        project = active_project(store, tmp_path)
        prompts = store.recent_prompts(project.id, limit=5)
    assert any(p.text == "record me locally" for p in prompts)
    assert outbox.pending() == []
    assert capsys.readouterr().err == ""


def test_board_write_survives_a_broken_spool(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_shipping()
    monkeypatch.setattr(
        outbox, "enqueue", lambda record: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    result = runner.invoke(app, ["note", "the board still works"])

    assert result.exit_code == 0, result.output


# --- The spool itself: durable, concurrent-safe, honest about its counts ---


def test_counts_report_queued_sent_and_dead() -> None:
    _configure_shipping()
    for index in range(3):
        outbox.enqueue({"kind": "prompt", "text": f"p{index}"})

    assert outbox.counts().queued == 3

    first, second, _ = outbox.pending()
    claimed = outbox.claim(first)
    assert claimed is not None
    outbox.mark_sent(claimed)
    dead = outbox.claim(second)
    assert dead is not None
    outbox.mark_dead(dead, "409 no_agent_identity")

    counts = outbox.counts()
    assert (counts.queued, counts.sent, counts.dead) == (1, 1, 1)


def test_a_claimed_record_cannot_be_claimed_twice() -> None:
    _configure_shipping()
    outbox.enqueue({"kind": "prompt", "text": "once"})
    (path,) = outbox.pending()

    assert outbox.claim(path) is not None
    assert outbox.claim(path) is None, "two sweepers must not ship the same record"
    assert outbox.pending() == [], "an in-flight record is not queued"


def test_dead_letters_keep_the_payload_and_the_reason() -> None:
    _configure_shipping()
    outbox.enqueue({"kind": "prompt", "text": "unshippable"})
    claimed = outbox.claim(outbox.pending()[0])
    assert claimed is not None

    grave = outbox.mark_dead(claimed, "gateway rejected: no_agent_identity")

    assert grave is not None
    body = json.loads(grave.read_text(encoding="utf-8"))
    assert body["text"] == "unshippable"
    assert body["dead_letter_reason"] == "gateway rejected: no_agent_identity"


def test_a_sweeper_killed_mid_flight_does_not_strand_the_record() -> None:
    _configure_shipping()
    outbox.enqueue({"kind": "prompt", "text": "orphan"})
    claimed = outbox.claim(outbox.pending()[0])
    assert claimed is not None and outbox.pending() == []

    # Far enough in the future that the claim is unambiguously abandoned.
    recovered = outbox.reclaim_stale(now=claimed.stat().st_mtime + 100_000)

    assert recovered == 1
    assert len(outbox.pending()) == 1


def test_shipping_defaults_to_off() -> None:
    assert load_config().explainability.ship is False
    assert insights.shipping_enabled() is False
