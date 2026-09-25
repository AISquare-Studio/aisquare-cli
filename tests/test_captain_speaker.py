"""The captain's voice out (card T3): four Speaker adapters behind one runner, the switch,
and the ONE drainer of the spool (13143 (5)): a thread in the captain's server process."""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from aisquare.services.captain import speaker as spk
from aisquare.services.captain import state as captain_state


class Recorder:
    """A :data:`Runner` that records instead of playing, and can fail on demand."""

    def __init__(self, fail: str | None = None) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.fail = fail

    def __call__(self, argv: Sequence[str], stdin: str | None) -> None:
        self.calls.append((list(argv), stdin))
        if self.fail:
            raise spk.SpeakerError(self.fail)


def test_powershell_speaks_through_system_speech_with_the_text_on_stdin() -> None:
    runner = Recorder()
    spk.PowerShellSpeaker(runner).utter("On it.")
    (argv, stdin) = runner.calls[0]
    assert argv[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
    assert "System.Speech" in argv[4] and "[Console]::In.ReadToEnd()" in argv[4]
    assert stdin == "On it." and "On it." not in " ".join(argv)


def test_say_takes_the_text_on_stdin_and_spd_say_as_one_argument_after_a_dash_dash() -> None:
    runner = Recorder()
    spk.SaySpeaker(runner).utter("Merge is done.")
    spk.SpdSaySpeaker(runner).utter("--rm -rf is not a word")
    assert runner.calls[0] == (["say"], "Merge is done.")
    assert runner.calls[1] == (["spd-say", "--wait", "--", "--rm -rf is not a word"], None)


def test_the_null_speaker_runs_nothing_and_says_so_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="aisquare.services.captain.speaker"):
        spk.NullSpeaker().utter("quiet room")
    assert any("quiet room" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("platform", "wsl", "available", "expected"),
    [
        ("win32", False, {"powershell.exe"}, "powershell"),
        ("linux", True, {"powershell.exe", "spd-say"}, "powershell"),
        ("linux", True, {"spd-say"}, "spd-say"),
        ("linux", False, {"spd-say", "powershell.exe"}, "spd-say"),
        ("linux", False, set(), "null"),
        ("darwin", False, {"say"}, "say"),
        ("darwin", False, set(), "null"),
    ],
)
def test_the_platform_picks_the_adapter(
    platform: str, wsl: bool, available: set[str], expected: str
) -> None:
    picked = spk.pick_speaker(
        runner=Recorder(),
        platform=platform,
        wsl=wsl,
        which=lambda name: f"/bin/{name}" if name in available else None,
    )
    assert picked.name == expected


def test_a_configured_name_wins_and_an_unknown_one_is_refused_by_name() -> None:
    picked = spk.pick_speaker(
        runner=Recorder(), platform="darwin", which=lambda n: None, configured="null"
    )
    assert picked.name == "null"
    forced = spk.pick_speaker(
        runner=Recorder(), platform="linux", wsl=False, which=lambda n: None, configured="say"
    )
    assert forced.name == "say"
    with pytest.raises(ValueError, match="powershell, say, spd-say, null"):
        spk.pick_speaker(runner=Recorder(), configured="festival")


def test_the_configured_speaker_is_read_raw_from_config_and_a_bad_file_is_said(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = tmp_path / "config.toml"
    assert spk.configured_speaker(config) is None
    config.write_text('[captain]\nspeaker = "spd-say"\n', encoding="utf-8")
    assert spk.configured_speaker(config) == "spd-say"
    config.write_text("[captain\nspeaker = ", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="aisquare.services.captain.speaker"):
        assert spk.configured_speaker(config) is None
    assert any("does not parse" in r.getMessage() for r in caplog.records)


def test_wsl_is_read_from_proc_version(tmp_path: Path) -> None:
    version = tmp_path / "version"
    version.write_text("Linux version 6.6.114.1-microsoft-standard-WSL2", encoding="utf-8")
    assert spk.is_wsl(version) is True
    version.write_text("Linux version 6.8.0-generic (buildd@lcy02)", encoding="utf-8")
    assert spk.is_wsl(version) is False
    assert spk.is_wsl(tmp_path / "missing") is False


def test_the_switch_lives_in_state_json_and_is_on_until_set(isolated_home: Path) -> None:
    assert spk.speaker_on() is True
    spk.set_speaker(False)
    assert spk.speaker_on() is False
    spk.set_speaker(True)
    assert spk.speaker_on() is True


def test_voice_is_gated_by_the_switch_skips_empty_lines_and_never_raises(
    isolated_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runner = Recorder()
    voice = spk.Voice(spk.SaySpeaker(runner))
    assert voice.utter("   ") is False and runner.calls == []
    assert voice.utter("hello") is True and runner.calls[-1] == (["say"], "hello")
    spk.set_speaker(False)
    assert voice.utter("muted") is False and len(runner.calls) == 1
    spk.set_speaker(True)
    broken = spk.Voice(spk.SaySpeaker(Recorder(fail="say exited 1: no audio device")))
    with caplog.at_level(logging.WARNING, logger="aisquare.services.captain.speaker"):
        assert broken.utter("lost line") is False
    assert any(
        "lost line" in r.getMessage() and "no audio device" in r.getMessage()
        for r in caplog.records
    )


def test_the_spool_is_drained_oldest_first_and_bt_can_clear_it(isolated_home: Path) -> None:
    runner = Recorder()
    voice = spk.Voice(spk.SaySpeaker(runner))
    captain_state.enqueue_speech("first")
    captain_state.enqueue_speech("second")
    assert spk.drain_spool(voice) == 2
    assert [stdin for _, stdin in runner.calls] == ["first", "second"]
    assert captain_state.pending_speech() == []
    captain_state.enqueue_speech("never said")
    assert captain_state.clear_speech() == 1
    assert spk.drain_spool(voice) == 0


def test_a_stale_spooled_line_is_dropped_not_played_late(
    isolated_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """13143 (5): a cue for a turn that ended half a minute ago is noise, so it is dropped
    (and said in the log); the id carries the stamp, so no clock is stored anywhere."""
    runner = Recorder()
    voice = spk.Voice(spk.SaySpeaker(runner))
    captain_state.enqueue_speech("a cue from a turn long over")
    late = time.time_ns() + int((spk.SPEECH_TTL_S + 1) * 1e9)
    with caplog.at_level(logging.INFO, logger="aisquare.services.captain.speaker"):
        assert spk.drain_spool(voice, now_ns=late) == 0
    assert runner.calls == [] and captain_state.pending_speech() == []
    assert any(
        "dropped" in r.getMessage() and "long over" in r.getMessage() for r in caplog.records
    )
    captain_state.enqueue_speech("fresh")
    assert spk.drain_spool(voice) == 1 and runner.calls[-1][1] == "fresh"
    assert spk.age_of("bogus-id") is None, "an id without a stamp is never dropped for its age"
    assert spk.age_of(f"spk_{1_000:020d}_000000_abcdef", now_ns=2_000_000_000 + 1_000) == 2.0


def test_the_one_drainer_is_a_daemon_thread_that_survives_a_bad_tick_and_stops_on_its_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = Recorder()
    voice = spk.Voice(spk.SaySpeaker(runner))
    spool: list[captain_state.Speech] = []
    broken = {"on": False}

    def take() -> captain_state.Speech | None:
        if broken["on"]:
            broken["on"] = False
            raise OSError("the spool dir vanished")
        return spool.pop(0) if spool else None

    stop = threading.Event()
    with caplog.at_level(logging.WARNING, logger="aisquare.services.captain.speaker"):
        thread = spk.start_drainer(voice, take=take, poll_s=0.01, stop=stop)
        assert thread.daemon and thread.name == "captain-speaker"
        broken["on"] = True
        spool.append(captain_state.Speech(f"spk_{time.time_ns():020d}_000000_abcdef", "hello"))
        deadline = time.monotonic() + 5
        while [line for _, line in runner.calls] != ["hello"] and time.monotonic() < deadline:
            time.sleep(0.01)
    assert [line for _, line in runner.calls] == ["hello"]
    assert thread.is_alive(), "a bad tick is said, then the next tick is tried"
    assert any("spool dir vanished" in r.getMessage() for r in caplog.records)
    stop.set()
    thread.join(2)
    assert not thread.is_alive()


def test_the_server_process_starts_exactly_one_drainer_before_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """13143 (5): the drainer lives as long as the captain does, so its server is its home."""
    from aisquare.services import mcp_server
    from aisquare.services.captain import actions

    order: list[str] = []
    voices: list[spk.Voice] = []

    def fake_start(voice: spk.Voice, **_: object) -> threading.Thread:
        order.append("drainer")
        voices.append(voice)
        return threading.Thread()

    monkeypatch.setattr(spk, "start_drainer", fake_start)
    monkeypatch.setattr(mcp_server, "run_stdio", lambda **_: order.append("serve"))
    actions.run_stdio(close_after=0)
    assert order == ["drainer", "serve"]
    (voice,) = voices
    assert isinstance(voice, spk.Voice)


def test_a_bad_speaker_in_config_is_said_and_the_server_still_starts_its_drainer(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """coderp's S4: pick_speaker raises on an unknown name, and run_stdio built the Voice before
    serving — one config typo and the captain had no tools at all. The server must serve
    whatever the speaker config says; the refusal is logged and the platform adapter plays."""
    from aisquare.services import mcp_server
    from aisquare.services.captain import actions

    monkeypatch.setattr(spk, "configured_speaker", lambda: "bogus")
    started: list[spk.Voice] = []
    served: list[bool] = []
    monkeypatch.setattr(spk, "start_drainer", lambda voice, **_: started.append(voice))
    monkeypatch.setattr(mcp_server, "run_stdio", lambda **_: served.append(True))
    with caplog.at_level(logging.WARNING, logger="aisquare.services.captain.speaker"):
        actions.run_stdio(close_after=0)
    assert served == [True] and len(started) == 1, "the server served, with a drainer"
    assert any("bogus" in r.getMessage() for r in caplog.records), "the typo is said"


def test_machine_voice_is_the_configured_adapter_else_the_platforms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(spk, "configured_speaker", lambda: "null")
    assert isinstance(spk.machine_voice().speaker, spk.NullSpeaker)
    monkeypatch.setattr(spk, "configured_speaker", lambda: None)
    assert hasattr(spk.machine_voice().speaker, "utter")


def test_powershell_reads_its_text_as_utf8_and_the_runner_sends_utf8() -> None:
    """coderp's S7, measured by runner2 through the real powershell.exe (board 13253): the
    script read stdin in the console code page, so an em dash arrived as 'ÔÇö' and was spoken
    as such. The script sets the console's input encoding to UTF-8 before it reads, and the
    runner encodes what it sends as UTF-8 on every platform."""
    script = spk.POWERSHELL_SCRIPT
    assert "[Console]::InputEncoding = [System.Text.Encoding]::UTF8" in script
    assert script.index("InputEncoding") < script.index("ReadToEnd()"), "set before the read"
    calls: list[dict[str, Any]] = []

    def fake_run(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(dict(kwargs))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(subprocess, "run", fake_run)
        spk.run_subprocess(["say"], "Nothing needs you — em dash")
    assert calls[0]["encoding"] == "utf-8" and calls[0]["input"] == "Nothing needs you — em dash"


def test_the_real_runner_delivers_non_ascii_text_intact() -> None:
    """A child that decodes its stdin as UTF-8 must read the em dash the runner sent."""
    check = (
        "import sys; sys.exit(0 if sys.stdin.read() == 'Nothing needs you \u2014 em dash' else 3)"
    )
    spk.run_subprocess([sys.executable, "-X", "utf8", "-c", check], "Nothing needs you — em dash")


def test_the_real_runner_says_a_synthesiser_it_cannot_run(tmp_path: Path) -> None:
    """A PermissionError or a bad interpreter is a SpeakerError like a missing command, so
    Voice.utter keeps its never-raises promise (coderp's minor)."""
    not_runnable = tmp_path / "synth"
    not_runnable.write_text("#!/bin/sh\necho\n", encoding="utf-8")
    not_runnable.chmod(0o644)  # present, not executable: a PermissionError, not a missing command
    with pytest.raises(spk.SpeakerError, match="could not be run"):
        spk.run_subprocess([str(not_runnable)], "x")


def test_the_real_runner_names_a_missing_command(tmp_path: Path) -> None:
    with pytest.raises(spk.SpeakerError, match="not on PATH"):
        spk.run_subprocess([str(tmp_path / "no-such-synth")], "x")
