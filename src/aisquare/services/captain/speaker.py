"""The captain's voice OUT: one ``Speaker`` protocol, four adapters, and the spool they drain.

The captain never plays audio itself. Its ``speak`` tool (T1) spools a line
under ``$AISQUARE_HOME/captain/speech``; the voice page's server (T3) drains
that spool through the one adapter this machine has, and speaks replies the
same way. ``bt`` clears the spool, so a brake also silences what was queued.

Four adapters, each a command the platform already has, run through ONE
``Runner`` seam so a test records the argv instead of playing sound:

- ``powershell.exe`` with ``System.Speech`` — Windows, and WSL, where the
  Windows side owns the sound card; the text goes in on stdin, never in the
  command line, so no quoting rule of PowerShell's ever reaches the owner's
  words.
- ``say`` — macOS; text on stdin.
- ``spd-say`` — Linux with speech-dispatcher; the text is one argument after
  ``--``.
- ``NullSpeaker`` — nothing to play through; the page still shows the reply.

Which one runs is picked by platform (:func:`pick_speaker`), and
``[captain] speaker = "powershell" | "say" | "spd-say" | "null"`` in
config.toml overrides it. ``captain_speaker`` in state.json is the on/off
switch the page and the CLI flip; off, a line is logged and not played.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from aisquare.core import paths, state_file
from aisquare.core.spawn import untraced_env
from aisquare.services.captain import state as captain_state

log = logging.getLogger(__name__)

Runner = Callable[[Sequence[str], str | None], None]
"""Runs one command, ``(argv, stdin_text)``, raising on failure: the seam every adapter uses."""

SPEAK_TIMEOUT_S = 60.0
"""A line that is still playing after this long is a stuck synthesiser, not speech."""
STATE_KEY = "captain_speaker"
SPEECH_TTL_S = 30.0
"""A spooled line older than this when it is taken is dropped, not played late: a cue for a
turn that ended half a minute ago is noise (manager, seq 13143)."""
DRAIN_POLL_S = 0.5
ADAPTERS = ("powershell", "say", "spd-say", "null")
"""The names ``[captain] speaker`` may take, and each adapter's ``name``."""

POWERSHELL_SCRIPT = (
    "Add-Type -AssemblyName System.Speech; "
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
    "$s.Speak([Console]::In.ReadToEnd())"
)
"""Reads the text from stdin so the owner's words never sit in a PowerShell command line."""


class SpeakerError(RuntimeError):
    """The adapter's command failed; the message carries its stderr."""


class Speaker(Protocol):
    """Audio out. ``say`` blocks until the line has been spoken, or raises :class:`SpeakerError`."""

    @property
    def name(self) -> str: ...

    def utter(self, text: str) -> None: ...


def run_subprocess(argv: Sequence[str], stdin: str | None) -> None:
    """The real :data:`Runner`: run ``argv``, feed ``stdin``, raise with stderr on failure."""
    try:
        completed = subprocess.run(
            list(argv),
            input=stdin,
            text=True,
            capture_output=True,
            timeout=SPEAK_TIMEOUT_S,
            check=False,
            # core.spawn.SEAMS: EXCLUDED and stripped — a synthesiser has no use for
            # an identity, and powershell.exe is a shell.
            env=untraced_env(),
        )
    except FileNotFoundError as exc:
        raise SpeakerError(f"{argv[0]} is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise SpeakerError(f"{argv[0]} did not finish speaking in {SPEAK_TIMEOUT_S:g}s") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        raise SpeakerError(
            f"{argv[0]} exited {completed.returncode}: {detail[-1] if detail else 'no output'}"
        )


@dataclass(frozen=True)
class PowerShellSpeaker:
    """Windows, and WSL: ``powershell.exe`` speaks through the Windows sound card."""

    runner: Runner = run_subprocess
    name: str = "powershell"

    def utter(self, text: str) -> None:
        self.runner(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", POWERSHELL_SCRIPT], text
        )


@dataclass(frozen=True)
class SaySpeaker:
    """macOS ``say``; the text on stdin."""

    runner: Runner = run_subprocess
    name: str = "say"

    def utter(self, text: str) -> None:
        self.runner(["say"], text)


@dataclass(frozen=True)
class SpdSaySpeaker:
    """Linux speech-dispatcher; ``--wait`` so the call returns when the line is done."""

    runner: Runner = run_subprocess
    name: str = "spd-say"

    def utter(self, text: str) -> None:
        self.runner(["spd-say", "--wait", "--", text], None)


@dataclass(frozen=True)
class NullSpeaker:
    """No synthesiser here: the line is logged, the page still shows it."""

    name: str = "null"

    def utter(self, text: str) -> None:
        log.info("captain speaker (null): %s", text)


def is_wsl(proc_version: Path = Path("/proc/version")) -> bool:
    """Whether this Linux is a WSL guest — the Windows side owns the speakers."""
    try:
        return "microsoft" in proc_version.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False


def configured_speaker(config_path: Path | None = None) -> str | None:
    """``[captain] speaker = "..."`` from config.toml, or ``None``; a bad file is said, not fatal.

    Read raw, like ``actions.action_list``: a broken config.toml costs the adapter choice,
    never the page.
    """
    path = config_path if config_path is not None else paths.config_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning(
            "captain speaker: %s could not be read, using the platform's adapter: %s", path, exc
        )
        return None
    try:
        loaded = tomllib.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        log.warning(
            "captain speaker: %s does not parse, using the platform's adapter: %s", path, exc
        )
        return None
    captain = loaded.get("captain")
    value = captain.get("speaker") if isinstance(captain, dict) else None
    return value if isinstance(value, str) and value else None


def pick_speaker(
    *,
    runner: Runner = run_subprocess,
    platform: str = sys.platform,
    which: Callable[[str], str | None] = shutil.which,
    wsl: bool | None = None,
    configured: str | None = None,
) -> Speaker:
    """The adapter for this machine: the configured name wins, else the platform decides.

    Windows, and a WSL guest with ``powershell.exe`` on PATH, speak through
    PowerShell; macOS through ``say``; Linux through ``spd-say`` when it is
    installed; anything else is :class:`NullSpeaker`. An unknown configured
    name is a ``ValueError`` naming the four, never a silent fallback.
    """
    if configured is not None:
        if configured not in ADAPTERS:
            raise ValueError(
                f"[captain] speaker = {configured!r} is not one of {', '.join(ADAPTERS)}"
            )
        return _by_name(configured, runner)
    on_wsl = is_wsl() if wsl is None else wsl
    windows_owns_the_sound = platform == "win32" or (platform.startswith("linux") and on_wsl)
    if windows_owns_the_sound and which("powershell.exe") is not None:
        return PowerShellSpeaker(runner)
    if platform == "darwin" and which("say") is not None:
        return SaySpeaker(runner)
    if platform.startswith("linux") and which("spd-say") is not None:
        return SpdSaySpeaker(runner)
    return NullSpeaker()


def _by_name(name: str, runner: Runner) -> Speaker:
    if name == "powershell":
        return PowerShellSpeaker(runner)
    if name == "say":
        return SaySpeaker(runner)
    if name == "spd-say":
        return SpdSaySpeaker(runner)
    return NullSpeaker()


def speaker_on() -> bool:
    """The on/off switch in state.json (``captain_speaker``); on when it was never set."""
    flag = state_file.read_state().get(STATE_KEY)
    if isinstance(flag, dict):
        on = flag.get("on")
        return on if isinstance(on, bool) else True
    return True


def set_speaker(on: bool) -> None:
    """Flip the switch — the page's speaker toggle and ``aisquare captain voice --speaker``."""
    state_file.update_state(STATE_KEY, {"on": on})


class Voice:
    """One adapter behind the on/off switch, that never raises: the owner hears what it can.

    A failed adapter is a warning with the line that was lost, never an error
    out of the page or the tool that spoke: the text is still on the page.
    """

    def __init__(self, speaker: Speaker, *, enabled: Callable[[], bool] = speaker_on) -> None:
        self.speaker = speaker
        self._enabled = enabled

    def utter(self, text: str) -> bool:
        """Speak ``text``; returns whether it was played (off, empty or failed → ``False``)."""
        line = text.strip()
        if not line:
            return False
        if not self._enabled():
            log.info("captain speaker is off; not spoken: %s", line[:80])
            return False
        try:
            self.speaker.utter(line)
        except SpeakerError as exc:
            log.warning(
                "captain speaker (%s) failed; not spoken, the page still shows it: %r (%s)",
                self.speaker.name,
                line[:80],
                exc,
            )
            return False
        return True


def age_of(speech_id: str, *, now_ns: int | None = None) -> float | None:
    """Seconds since a spooled line was written, read from its id (``spk_<time_ns>_…``)."""
    parts = speech_id.split("_")
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    stamp = int(parts[1])
    current = time.time_ns() if now_ns is None else now_ns
    return max(0.0, (current - stamp) / 1e9)


def drain_spool(
    voice: Voice,
    *,
    take: Callable[[], captain_state.Speech | None] = captain_state.take_speech,
    limit: int = 20,
    ttl_s: float = SPEECH_TTL_S,
    now_ns: int | None = None,
) -> int:
    """Speak what the captain spooled (T1's ``speak`` tool), oldest first; returns how many.

    A line older than ``ttl_s`` is dropped and said in the log, never played late.
    """
    spoken = 0
    for _ in range(limit):
        line = take()
        if line is None:
            break
        age = age_of(line.id, now_ns=now_ns)
        if age is not None and age > ttl_s:
            log.info(
                "captain speaker: dropped a %.0fs-old line, not played late: %r",
                age,
                line.text[:80],
            )
            continue
        voice.utter(line.text)
        spoken += 1
    return spoken


def _drain_until_stopped(
    voice: Voice,
    take: Callable[[], captain_state.Speech | None],
    poll_s: float,
    ttl_s: float,
    halt: threading.Event,
) -> None:
    """The drainer thread's body: a tick every ``poll_s`` until ``halt`` is set.

    Module-level, not nested in :func:`start_drainer`, and reached only as a
    ``Thread`` target: a bad tick is said here and never raised to the server's
    client, and tests/test_config_writes_stay_in_the_cli.py's by-name graph,
    which fuses ``Voice.say`` with the CLI's ``say`` command, must not read the
    server as a daemon that reaches a config write through it.
    """
    while not halt.is_set():
        try:
            drain_spool(voice, take=take, ttl_s=ttl_s)
        except Exception as exc:  # said, then tried again next tick: the spool is a courtesy
            log.warning("captain speaker: the spool could not be drained this tick: %s", exc)
        halt.wait(poll_s)


def start_drainer(
    voice: Voice,
    *,
    take: Callable[[], captain_state.Speech | None] = captain_state.take_speech,
    poll_s: float = DRAIN_POLL_S,
    ttl_s: float = SPEECH_TTL_S,
    stop: threading.Event | None = None,
) -> threading.Thread:
    """THE drainer: one daemon thread that lives as long as the captain's server does.

    Exactly one per captain (manager, seq 13143): it runs in the Actions server
    process, so speech plays whenever the captain speaks and the switch is on,
    whether or not the voice page or the TUI is open. ``take_speech`` claims a
    line by rename, so even a second drainer could not say a line twice; this
    keeps the design to one anyway. ``stop`` ends it; a daemon thread ends with
    the process regardless.
    """
    halt = stop if stop is not None else threading.Event()
    thread = threading.Thread(
        target=_drain_until_stopped,
        args=(voice, take, poll_s, ttl_s, halt),
        name="captain-speaker",
        daemon=True,
    )
    thread.stop = halt  # type: ignore[attr-defined]
    thread.start()
    return thread


def machine_voice() -> Voice:
    """The Voice this machine speaks with: the configured adapter, else the platform's."""
    return Voice(pick_speaker(configured=configured_speaker()))
