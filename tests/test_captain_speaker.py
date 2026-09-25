"""The captain's voice out (card T3): four Speaker adapters behind one runner, the switch and
the spool they drain."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

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
    spk.PowerShellSpeaker(runner).say("On it.")
    (argv, stdin) = runner.calls[0]
    assert argv[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
    assert "System.Speech" in argv[4] and "[Console]::In.ReadToEnd()" in argv[4]
    assert stdin == "On it." and "On it." not in " ".join(argv)


def test_say_takes_the_text_on_stdin_and_spd_say_as_one_argument_after_a_dash_dash() -> None:
    runner = Recorder()
    spk.SaySpeaker(runner).say("Merge is done.")
    spk.SpdSaySpeaker(runner).say("--rm -rf is not a word")
    assert runner.calls[0] == (["say"], "Merge is done.")
    assert runner.calls[1] == (["spd-say", "--wait", "--", "--rm -rf is not a word"], None)


def test_the_null_speaker_runs_nothing_and_says_so_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="aisquare.services.captain.speaker"):
        spk.NullSpeaker().say("quiet room")
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
    assert voice.say("   ") is False and runner.calls == []
    assert voice.say("hello") is True and runner.calls[-1] == (["say"], "hello")
    spk.set_speaker(False)
    assert voice.say("muted") is False and len(runner.calls) == 1
    spk.set_speaker(True)
    broken = spk.Voice(spk.SaySpeaker(Recorder(fail="say exited 1: no audio device")))
    with caplog.at_level(logging.WARNING, logger="aisquare.services.captain.speaker"):
        assert broken.say("lost line") is False
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


def test_the_real_runner_names_a_missing_command(tmp_path: Path) -> None:
    with pytest.raises(spk.SpeakerError, match="not on PATH"):
        spk.run_subprocess([str(tmp_path / "no-such-synth")], "x")
