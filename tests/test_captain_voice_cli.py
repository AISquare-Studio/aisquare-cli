"""``aisquare captain voice`` (card T3): the leaf that serves the page — receipts and
refusals."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import captain_voice
from aisquare.cli.app import app
from aisquare.services.captain import speaker as speaker_mod
from tests.test_stubs import IMPLEMENTED


def test_show_token_prints_the_url_the_qr_and_the_adb_line_without_serving(
    runner: CliRunner, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served: list[dict[str, object]] = []
    monkeypatch.setattr("aisquare.services.captain.voice.serve", lambda **kw: served.append(kw))
    result = runner.invoke(app, ["captain", "voice", "--show-token", "--port", "8751"])
    assert result.exit_code == 0, result.output
    assert "http://localhost:8751/#token=" in result.output
    assert "adb reverse tcp:8751 tcp:8751" in result.output
    assert ("mode: focus" in result.output and "▀" in result.output) or "█" in result.output
    assert served == []
    result = runner.invoke(app, ["--json", "captain", "voice", "--show-token", "--mode", "listen"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert set(data) == {"url", "port", "host", "mode", "speaker", "adb_reverse", "serving"}
    assert data["mode"] == "listen" and data["serving"] is False and data["port"] == 8749


def test_serving_hands_the_token_mode_and_model_to_the_server(
    runner: CliRunner, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served: list[dict[str, Any]] = []

    def fake_serve(**kw: Any) -> None:
        served.append(kw)
        kw["hooks"].on_thinking(True)  # what the page server does on each flip
        kw["hooks"].on_thinking(False)

    monkeypatch.setattr("aisquare.services.captain.voice.serve", fake_serve)
    monkeypatch.setattr(captain_voice, "voice_dependency_error", lambda: None)
    result = runner.invoke(app, ["captain", "voice", "--mode", "listen", "--speaker", "off"])
    assert result.exit_code == 0, result.output
    (call,) = served
    assert call["mode"] == "listen" and call["port"] == 8749 and call["host"] == "127.0.0.1"
    assert "token=" + str(call["token"]) in result.output
    assert speaker_mod.speaker_on() is False, "--speaker off flipped the switch before serving"
    assert call["hooks"].on_thinking is captain_voice._print_thinking
    assert "thinking" in result.output and "idle" in result.output, (
        "the CLI side of the thinking signal: the terminal shows it too"
    )


def test_captain_dash_dash_voice_is_the_plans_spelling_of_the_voice_leaf(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """13143 (1): `aisquare captain --voice` keeps working as the alias of `captain voice`."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(captain_voice, "voice_page", lambda **kw: calls.append(kw))
    result = runner.invoke(app, ["captain", "--voice"])
    assert result.exit_code == 0, result.output
    assert calls == [{}], "the leaf, with its own defaults"


def test_a_missing_extra_is_the_install_line_and_show_token_still_answers(
    runner: CliRunner, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        captain_voice, "_find_spec", lambda name: None if name == "websockets" else object()
    )
    result = runner.invoke(app, ["captain", "voice"])
    assert result.exit_code == 1
    assert "missing websockets" in result.output and "aisquare-cli[voice]" in result.output
    result = runner.invoke(app, ["captain", "voice", "--show-token"])
    assert result.exit_code == 0 and "note: the voice extra is not installed" in result.output


def test_a_non_loopback_host_and_a_bad_mode_are_refused(
    runner: CliRunner, isolated_home: Path
) -> None:
    result = runner.invoke(app, ["captain", "voice", "--host", "0.0.0.0", "--show-token"])
    assert result.exit_code == 1 and "loopback" in result.output
    result = runner.invoke(app, ["captain", "voice", "--mode", "shout", "--show-token"])
    assert result.exit_code == 1 and "focus, listen" in result.output
    result = runner.invoke(app, ["captain", "voice", "--speaker", "loud", "--show-token"])
    assert result.exit_code == 1 and "on or off" in result.output


def test_the_leaf_is_implemented_and_left_uninvoked_by_the_sweeps() -> None:
    # The configured-home sweep imports the damaged-store sweep's UNINVOKED, so one entry
    # covers both; it is checked where it is defined.
    from tests.test_no_traceback_on_a_damaged_store import UNINVOKED

    assert ("captain", "voice") in IMPLEMENTED
    assert "captain voice" in UNINVOKED
