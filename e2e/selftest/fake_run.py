"""A realistic fake run directory, for the report's self-tests and for smoke-testing CI.

    python -m e2e.selftest.fake_run <dir> [--scenario rc-failed|passed|gate-error|env-failed]

writes what one leg of the real harness writes, in the layout ``e2e/contracts.py``
describes: ``run.json``; per step a PNG under ``shots/`` and its screen text
under ``text/``; a placeholder video per recorded journey; two logs. The default
scenario is three journeys and ten steps with exactly one failed check (J04's
last step), so a report built from it shows a red banner, an open ``<details>``
and a failure in verdict.json, with passed journeys beside them. Two watch items
ride along in every scenario that runs anything: QA-01 still open, QA-13 fixed,
neither allowed to move the verdict.

Why a generator and not committed fixtures: the pictures are drawn here, a few
KB each, so the self-tests carry no binaries, every scenario comes from one
description, and a change to the contract breaks this module under mypy instead
of leaving a stale fixture that still loads.

Every pixel is synthetic and says so. Each picture carries an amber FAKE strip
and run.json carries a note, so nothing rendered from a fake run can be taken
for evidence about aisquare. Ids, paths and status rules copy the recorder's
(``e2e/runner/recorder.py``): ``J04.S03.<slug>``, ``shots/<J>/<id>.png``, a failed
check fails its step, a failed step its journey.

Standard library only (``zlib`` and ``struct`` draw the PNGs), like the report.
"""

from __future__ import annotations

import argparse
import json
import shutil
import struct
import sys
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, get_args

from e2e.contracts import (
    Check,
    Environment,
    Journey,
    JourneyKind,
    Run,
    Status,
    Step,
    load,
    save,
    verdict_for,
)

Scenario = Literal["rc-failed", "passed", "gate-error", "env-failed"]
SCENARIOS: tuple[Scenario, ...] = get_args(Scenario)

FAKE_RUNNER = "fake"
"""``Environment.runner`` of every fake run: how :func:`write` tells its output from a real run."""

FAILING_STEP = "J04.S03.f12-scrollback"
"""The one step the scenarios vary. Tests name it, so it is spelled once."""

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
WIDTH, HEIGHT = 960, 600
_START = datetime(2026, 10, 16, 8, 20, 7, tzinfo=UTC)
_PREFLIGHT = "preflight failed: JetBrainsMono-Regular.woff2 does not match vendor/SHA256SUMS"

RGB = tuple[int, int, int]


class FakeRunError(Exception):
    """A directory this generator must not write into."""


# --------------------------------------------------------------------------------------
# What the fake run contains.


@dataclass(frozen=True)
class _StepSpec:
    sid: str
    slug: str
    title: str
    seconds: float
    layout: Literal["shell", "fleet"]
    screen: tuple[str, ...]
    checks: tuple[tuple[str, bool, str], ...]
    agents: tuple[str, ...] = ("MANAGER", "CODER-1", "CODER-2")
    selected: int = 0
    note: str = ""


@dataclass(frozen=True)
class _JourneySpec:
    id: str
    title: str
    story: str
    video: bool
    steps: tuple[_StepSpec, ...]
    kind: JourneyKind = "journey"
    finding: str | None = None


_WELCOME = ("WELCOME",)
_JOURNEYS: tuple[_JourneySpec, ...] = (
    _JourneySpec(
        id="J01",
        title="Install from the README one-liner",
        story=(
            "Fresh machine. Type the install one-liner with this tree's wheel, press Enter at "
            '"Open the fleet UI now?", and land on Welcome.'
        ),
        video=False,
        steps=(
            _StepSpec(
                "S01",
                "one-liner",
                "Type the install one-liner",
                41.8,
                "shell",
                (
                    "$ curl -fsSL http://runner:8765/install.sh | sh",
                    "  AISQUARE_INSTALL_PACKAGE=http://runner:8765/dist/aisquare.whl",
                    "==> installing uv 0.9.2 ........................ ok",
                    "==> installing aisquare 0.8.1.dev3 ............. ok",
                    "==> claude code hooks -> ~/.claude/settings.json  ok",
                    "==> doctor: 11 ok, 1 amber (brain)",
                    "aisquare 0.8.1.dev3 is ready.",
                    "Open the fleet UI now? [Y/n]",
                ),
                (
                    ("installer exits without a traceback", True, "exit 0 after 41.8 s"),
                    ('asks "Open the fleet UI now?"', True, "prompt on row 8"),
                ),
            ),
            _StepSpec(
                "S02",
                "open-fleet-ui",
                'Press Enter at "Open the fleet UI now?"',
                3.2,
                "shell",
                (
                    "Open the fleet UI now? [Y/n]",
                    "> y",
                    "exec asq </dev/tty",
                    "switching to the alternate screen ...",
                ),
                (
                    ("alternate screen on", True, "DECSET 1049"),
                    ("mouse reporting on", True, "modes 1003 + 1006"),
                ),
            ),
            _StepSpec(
                "S03",
                "welcome",
                "Welcome shows this folder",
                2.4,
                "fleet",
                (
                    "Welcome to aisquare",
                    "acme-api · this folder",
                    "[1] Use this folder",
                    "[2] Connect Claude Code",
                    "[3] Start manager",
                ),
                (
                    ('Welcome shows "· this folder"', True, ""),
                    ("aisquare --version in a new window matches", True, "0.8.1.dev3"),
                    ("no traceback on screen", True, ""),
                ),
                agents=_WELCOME,
            ),
        ),
    ),
    _JourneySpec(
        id="J02",
        title="Welcome to a running fleet",
        story=(
            "Use this folder, Connect, Start manager, Open the manager, answer the trust "
            "question, F12 and w, then Start the coders."
        ),
        video=True,
        steps=(
            _StepSpec(
                "S01",
                "use-this-folder",
                "Use this folder",
                1.9,
                "fleet",
                (
                    "Welcome to aisquare",
                    "[1] Use this folder ............ done",
                    "[2] Connect Claude Code",
                    "[3] Start manager",
                ),
                (("card 1 completes", True, ""), ("no traceback on screen", True, "")),
                agents=_WELCOME,
            ),
            _StepSpec(
                "S02",
                "connect",
                "Connect Claude Code",
                6.3,
                "fleet",
                (
                    "[1] Use this folder ............ done",
                    "[2] Connect Claude Code ........ done",
                    "    hooks: SessionStart UserPromptSubmit Stop",
                    "[3] Start manager",
                ),
                (
                    (
                        "hooks written to ~/.claude/settings.json",
                        True,
                        "SessionStart, UserPromptSubmit, Stop, Notification",
                    ),
                ),
                agents=_WELCOME,
            ),
            _StepSpec(
                "S03",
                "start-manager",
                "Start manager and answer the trust question",
                9.7,
                "fleet",
                (
                    "SCRIPTED AGENT · no LLM",
                    "Do you trust the files in this folder?",
                    "> Yes, proceed",
                    "manager ready · waiting for a goal",
                ),
                (
                    ("manager pane shows the SCRIPTED AGENT banner", True, ""),
                    ("trust saved in ~/.claude.json", True, "hasTrustDialogAccepted=true"),
                ),
                agents=("MANAGER",),
                note="The stand-in agent asked Claude Code's trust question; the user said yes.",
            ),
            _StepSpec(
                "S04",
                "fleet-up",
                "Start the coders",
                14.2,
                "fleet",
                (
                    "fleet up: manager, coder-1, coder-2",
                    "coder-1  fleet/amber-otter/coder-1  running",
                    "coder-2  fleet/amber-otter/coder-2  running",
                ),
                (
                    (
                        "manager, coder-1, coder-2 RUNNING in fleet ls --json",
                        True,
                        "3/3 running in two reads 2.5 s apart",
                    ),
                    (
                        "worktrees on fleet/<codename>/coder-N",
                        True,
                        ".aisquare-worktrees/coder-1, -2",
                    ),
                    ("no traceback on screen", True, ""),
                ),
            ),
        ),
    ),
    _JourneySpec(
        id="J04",
        title="Drive an agent pane",
        story=(
            "Click coder-1, click into its pane and type; q must not quit asq; F12 leaves the "
            "pane; the mouse wheel scrolls back."
        ),
        video=True,
        steps=(
            _StepSpec(
                "S01",
                "click-coder",
                "Click coder-1 in the sidebar",
                1.2,
                "fleet",
                (
                    "SCRIPTED AGENT · no LLM",
                    "coder-1 · fleet/amber-otter/coder-1",
                    "waiting for a task",
                ),
                (("coder-1 selected", True, ""), ("pane title reads coder-1", True, "")),
                selected=1,
            ),
            _StepSpec(
                "S02",
                "type-in-pane",
                "Type into the pane; q does not quit asq",
                3.8,
                "fleet",
                (
                    "SCRIPTED AGENT · no LLM",
                    "> hello from the e2e user",
                    "echo: hello from the e2e user",
                    "> q",
                ),
                (
                    ("typed text reaches the agent", True, "echo: hello from the e2e user"),
                    ("asq still running after q", True, ""),
                ),
                selected=1,
            ),
            _StepSpec(
                "S03",
                "f12-scrollback",
                "F12 leaves the pane; the wheel scrolls back",
                5.6,
                "fleet",
                (
                    "$ git log --oneline -3",
                    "$ pytest -q",
                    ".....                                [100%]",
                    "5 passed in 0.41s",
                    "(scrolled back 12 lines)",
                ),
                (
                    ("focus leaves the pane on F12", True, ""),
                    (
                        "pane rows match tmux capture-pane (cropped)",
                        False,
                        "rows 3-5 differ: expected '$ pytest -q', saw '$ pytest -q▌'",
                    ),
                ),
                selected=1,
            ),
        ),
    ),
    # Watch items: known bugs from the QA report, re-checked nightly, never gating.
    _JourneySpec(
        id="W01",
        title="project forget leaves no machine-wide pin",
        story=(
            "Forget a scratch project, then read ~/.aisquare/state.json: nothing may stay "
            "pinned for every session on the machine."
        ),
        video=False,
        kind="watch",
        finding="QA-01",
        steps=(
            _StepSpec(
                "S01",
                "forget-pin",
                "Forget a scratch project; read state.json",
                2.7,
                "shell",
                (
                    "$ aisquare project forget scratch --yes",
                    "forgot scratch (12 entries)",
                    "$ cat ~/.aisquare/state.json",
                    '{"pinned_project": "/home/qa/scratch"}',
                ),
                (
                    ("forget exits 0", True, ""),
                    (
                        "no machine-wide pin left in state.json",
                        False,
                        'state.json still pins "/home/qa/scratch"',
                    ),
                ),
            ),
        ),
    ),
    _JourneySpec(
        id="W02",
        title="similar task titles stay two tasks with --key",
        story="Add two tasks with near-identical titles and distinct --key values; both must stay.",
        video=False,
        kind="watch",
        finding="QA-13",
        steps=(
            _StepSpec(
                "S01",
                "two-keys",
                "Add two similar tasks with distinct keys",
                1.8,
                "shell",
                (
                    "$ aisquare task add 'Fix the login form' --key login-a",
                    "$ aisquare task add 'Fix the login form.' --key login-b",
                    "$ aisquare task list",
                    "login-a  Fix the login form    open",
                    "login-b  Fix the login form.   open",
                ),
                (("two tasks listed", True, "task list shows login-a and login-b"),),
            ),
        ),
    ),
)


def _environment() -> Environment:
    return Environment(
        os="Linux 6.8.0-1015-azure x86_64 (fake)",
        runner=FAKE_RUNNER,
        track="main",
        agent="e2e stand-in (no LLM)",
        aisquare="aisquare 0.8.1.dev3+g0123456 (fake)",
        commit="0123456789ab",
        versions={
            "xterm.js": "6.0.0",
            "ttyd": "1.7.7-40e79c7",
            "chromium": "141.0.7390.37",
            "tmux": "tmux 3.4",
            "uv": "uv 0.9.2",
            "git": "git version 2.43.0",
            "claude (stand-in)": "2.1.281 (scripted, no LLM)",
            "python (harness)": "3.13.5",
        },
    )


# --------------------------------------------------------------------------------------
# Writing it.


def write(run_dir: Path, scenario: Scenario = "rc-failed", *, run_id: str | None = None) -> Run:
    """Write a fake run into ``run_dir`` and return what run.json now says.

    Refuses (``FakeRunError``) a directory that holds anything but an earlier
    fake run, so a mistyped path cannot clear somebody's real evidence.
    """
    if scenario not in SCENARIOS:
        raise FakeRunError(f"unknown scenario {scenario!r}; one of {', '.join(SCENARIOS)}")
    _prepare(run_dir)
    clock = _START
    journeys: list[Journey] = []
    for spec in _JOURNEYS:
        if scenario == "env-failed":
            # Preflight fails before any journey starts: the recorder's ``skipped``.
            journeys.append(
                Journey(
                    spec.id,
                    spec.title,
                    spec.story,
                    "skipped",
                    0.0,
                    error=_PREFLIGHT,
                    kind=spec.kind,
                    finding=spec.finding,
                )
            )
            continue
        journey, clock = _journey(run_dir, spec, scenario, clock)
        journeys.append(journey)
    notes = [
        "FAKE RUN written by e2e.selftest.fake_run: every pixel and number is synthetic. "
        "It exercises the report, not aisquare.",
        "Agents are the e2e stand-in: aisquare itself is real; no model is called.",
    ]
    if scenario == "env-failed":
        notes.append(_PREFLIGHT)
    _logs(run_dir)
    run = Run(
        run_id=run_id or f"fake-{_START:%Y%m%d-%H%M%S}-{scenario}",
        started_at=_START.isoformat(timespec="seconds"),
        finished_at=(clock + timedelta(seconds=4)).isoformat(timespec="seconds"),
        verdict=verdict_for(journeys, env_failed=scenario == "env-failed"),
        env=_environment(),
        journeys=journeys,
        notes=notes,
    )
    save(run, run_dir / "run.json")
    return run


def _prepare(run_dir: Path) -> None:
    if run_dir.exists() and not run_dir.is_dir():
        raise FakeRunError(f"{run_dir} exists and is not a directory")
    if run_dir.is_dir() and any(run_dir.iterdir()):
        previous = run_dir / "run.json"
        if not previous.is_file():
            raise FakeRunError(
                f"refusing to write a fake run into {run_dir}: not empty, and no run.json"
            )
        try:
            runner = load(previous).env.runner
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise FakeRunError(f"refusing to overwrite {previous}: unreadable ({exc})") from exc
        if runner != FAKE_RUNNER:
            raise FakeRunError(f"refusing to overwrite {run_dir}: it holds a real run ({runner})")
        # Only what an earlier fake wrote, so a changed scenario leaves no stale shots behind.
        for name in ("shots", "text", "video", "logs", "report"):
            shutil.rmtree(run_dir / name, ignore_errors=True)
    run_dir.mkdir(parents=True, exist_ok=True)


def _journey(
    run_dir: Path, spec: _JourneySpec, scenario: Scenario, clock: datetime
) -> tuple[Journey, datetime]:
    began = clock
    clock += timedelta(seconds=2.0)  # opening the terminal window, as a real journey does
    steps: list[Step] = []
    error: str | None = None
    for number, item in enumerate(spec.steps):
        step_id = f"{spec.id}.{item.sid}.{item.slug}"
        checks = [Check(name, ok, detail) for name, ok, detail in item.checks]
        status: Status = "passed"
        note = item.note
        if step_id == FAILING_STEP and scenario == "passed":
            checks = [Check(c.name, True, "") for c in checks]
        elif step_id == FAILING_STEP and scenario == "gate-error":
            # The harness lost the page before the second check could run.
            checks = checks[:1]
            status = "error"
            note = "wait_for('scrolled back') gave up after 20 s: the ttyd websocket closed"
            error = f"{note} · TimeoutError"
        if status == "passed" and any(not c.ok for c in checks):
            status = "failed"
            bad = "; ".join(f"{c.name} ({c.detail})" for c in checks if not c.ok)
            error = f"{step_id}: {bad}"
        shot = Path("shots") / spec.id / f"{step_id}.png"
        text = Path("text") / spec.id / f"{step_id}.txt"
        _save_bytes(run_dir / shot, _picture(item, step_id, status, number))
        _save_bytes(run_dir / text, _screen_text(item).encode("utf-8"))
        steps.append(
            Step(
                id=step_id,
                title=item.title,
                status=status,
                started_at=clock.isoformat(timespec="seconds"),
                duration_s=item.seconds,
                shot=shot.as_posix(),
                text=text.as_posix(),
                checks=checks,
                note=note,
            )
        )
        clock += timedelta(seconds=item.seconds + 0.6)
    worst: Status = "passed"
    if any(s.status == "error" for s in steps):
        worst = "error"
    elif any(s.status == "failed" for s in steps):
        worst = "failed"
    video: str | None = None
    if spec.video:
        # Zero bytes on purpose: the report links videos and shows their size, and an
        # honest 0 B says "placeholder" louder than a fake file would.
        video = f"video/{spec.id}.webm"
        _save_bytes(run_dir / video, b"")
    duration = round((clock - began).total_seconds(), 2)
    journey = Journey(
        id=spec.id,
        title=spec.title,
        story=spec.story,
        status=worst,
        duration_s=duration,
        steps=steps,
        video=video,
        error=error if worst != "passed" else None,
        kind=spec.kind,
        finding=spec.finding,
    )
    return journey, clock


def _logs(run_dir: Path) -> None:
    _save_bytes(
        run_dir / "logs" / "installer.log",
        b"FAKE installer log (e2e.selftest.fake_run)\n==> installing aisquare ... ok\n",
    )
    doctor = {"fake": True, "ok": 11, "amber": ["brain"], "red": []}
    _save_bytes(run_dir / "logs" / "doctor.json", json.dumps(doctor, indent=2).encode("utf-8"))


def _save_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _screen_text(item: _StepSpec) -> str:
    """What the emulator's buffer would hold for the drawn screen: the same words."""
    if item.layout == "shell":
        return "\n".join(item.screen) + "\n"
    rows = ["aisquare · acme-api · fleet"]
    for index in range(max(len(item.agents), len(item.screen))):
        agent = item.agents[index].lower() if index < len(item.agents) else ""
        line = item.screen[index] if index < len(item.screen) else ""
        rows.append(f"{agent:<14}│ {line}".rstrip())
    rows.append("F12 leave pane · w welcome · ? help · q quit")
    return "\n".join(rows) + "\n"


# --------------------------------------------------------------------------------------
# Drawing it: a terminal-shaped picture, distinct per step, with a FAKE strip.

_BG: RGB = (13, 16, 22)
_PANEL: RGB = (19, 24, 33)
_BAR: RGB = (28, 35, 47)
_TEXT: RGB = (205, 213, 224)
_DIM: RGB = (120, 132, 150)
_GREEN: RGB = (63, 185, 80)
_RED: RGB = (248, 81, 73)
_PURPLE: RGB = (163, 113, 247)
_AMBER: RGB = (240, 180, 41)
_INK: RGB = (24, 20, 10)
_ACCENTS: tuple[RGB, ...] = (
    (88, 166, 255),
    (63, 185, 80),
    (210, 153, 34),
    (219, 97, 162),
    (57, 197, 207),
    (163, 113, 247),
    (240, 136, 62),
)


def _picture(item: _StepSpec, step_id: str, status: Status, number: int) -> bytes:
    canvas = _Canvas(WIDTH, HEIGHT, _BG)
    accent = _ACCENTS[(sum(map(ord, step_id)) + number) % len(_ACCENTS)]
    strip_top = HEIGHT - 40
    if item.layout == "shell":
        y = 16
        for line in item.screen:
            colour = _GREEN if line.startswith("$") else _TEXT
            canvas.text(14, y, line, colour)
            y += 24
        canvas.rect(14, y, 10, 14, _TEXT)  # the cursor, after the last line
        canvas.rect(WIDTH - 18, 16, 4, min(strip_top - 32, 40 + 40 * number), accent)
    else:
        canvas.rect(0, 0, WIDTH, 30, _BAR)
        canvas.text(12, 8, "AISQUARE · ACME-API · FLEET", _TEXT)
        canvas.rect(0, 30, 232, strip_top - 58, _PANEL)
        canvas.rect(232, 30, 2, strip_top - 58, _BAR)
        for index, agent in enumerate(item.agents):
            top = 44 + 34 * index
            if index == item.selected:
                canvas.rect(6, top - 6, 220, 28, accent)
            canvas.rect(16, top + 2, 10, 10, _GREEN if index != item.selected else _INK)
            canvas.text(36, top, agent, _TEXT if index != item.selected else _INK)
        y = 46
        for line in item.screen:
            colour = _PURPLE if "SCRIPTED AGENT" in line else _TEXT
            canvas.text(250, y, line, colour)
            y += 26
        if status in ("failed", "error"):
            colour = _RED if status == "failed" else _PURPLE
            canvas.frame(244, 46 + 26 - 6, WIDTH - 256, 26 * 3, colour, 3)
        canvas.rect(0, strip_top - 28, WIDTH, 28, _BAR)
        canvas.text(12, strip_top - 21, "F12 LEAVE PANE   W WELCOME   ? HELP   Q QUIT", _DIM)
    canvas.rect(0, strip_top, WIDTH, 40, _AMBER)
    canvas.text(14, strip_top + 13, f"FAKE SCREENSHOT · {step_id}", _INK)
    canvas.text(WIDTH - 14 - 12 * len("E2E.SELFTEST"), strip_top + 13, "E2E.SELFTEST", _INK)
    return canvas.png()


class _Canvas:
    """RGB rows in bytearrays: rectangles are slice assignments, so a picture takes ms."""

    def __init__(self, width: int, height: int, background: RGB) -> None:
        self.width = width
        self.height = height
        self._rows = [bytearray(bytes(background) * width) for _ in range(height)]

    def rect(self, x: int, y: int, w: int, h: int, colour: RGB) -> None:
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.width, x + w), min(self.height, y + h)
        if x1 <= x0 or y1 <= y0:
            return
        span = bytes(colour) * (x1 - x0)
        for row in self._rows[y0:y1]:
            row[x0 * 3 : x1 * 3] = span

    def frame(self, x: int, y: int, w: int, h: int, colour: RGB, thickness: int) -> None:
        self.rect(x, y, w, thickness, colour)
        self.rect(x, y + h - thickness, w, thickness, colour)
        self.rect(x, y, thickness, h, colour)
        self.rect(x + w - thickness, y, thickness, h, colour)

    def text(self, x: int, y: int, text: str, colour: RGB, scale: int = 2) -> None:
        for char in text.upper():
            for gy, bits in enumerate(_FONT.get(char, ())):
                start = -1
                for gx, bit in enumerate(bits + "0"):  # the sentinel closes a run at the edge
                    if bit == "1" and start < 0:
                        start = gx
                    elif bit != "1" and start >= 0:
                        self.rect(
                            x + start * scale, y + gy * scale, (gx - start) * scale, scale, colour
                        )
                        start = -1
            x += 6 * scale

    def png(self) -> bytes:
        raw = b"".join(b"\x00" + bytes(row) for row in self._rows)  # filter 0 on every row
        return encode_png(self.width, self.height, raw)


def encode_png(width: int, height: int, raw: bytes) -> bytes:
    """An 8-bit RGB PNG from filtered scanlines: the four chunks a decoder needs."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


# A 5x7 bitmap font: enough of ASCII (plus · and the box bar) to write what a terminal shows.
_FONT: dict[str, tuple[str, ...]] = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("01110", "00100", "00100", "00100", "00100", "00100", "01110"),
    "J": ("00111", "00010", "00010", "00010", "00010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "10001", "11001", "10101", "10011", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
    ",": ("00000", "00000", "00000", "00000", "01100", "00100", "01000"),
    ":": ("00000", "01100", "01100", "00000", "01100", "01100", "00000"),
    ";": ("00000", "01100", "01100", "00000", "01100", "00100", "01000"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    "_": ("00000", "00000", "00000", "00000", "00000", "00000", "11111"),
    "=": ("00000", "00000", "11111", "00000", "11111", "00000", "00000"),
    "+": ("00000", "00100", "00100", "11111", "00100", "00100", "00000"),
    "/": ("00001", "00001", "00010", "00100", "01000", "10000", "10000"),
    "|": ("00100", "00100", "00100", "00100", "00100", "00100", "00100"),
    "│": ("00100", "00100", "00100", "00100", "00100", "00100", "00100"),
    "(": ("00010", "00100", "01000", "01000", "01000", "00100", "00010"),
    ")": ("01000", "00100", "00010", "00010", "00010", "00100", "01000"),
    "[": ("01110", "01000", "01000", "01000", "01000", "01000", "01110"),
    "]": ("01110", "00010", "00010", "00010", "00010", "00010", "01110"),
    "<": ("00010", "00100", "01000", "10000", "01000", "00100", "00010"),
    ">": ("01000", "00100", "00010", "00001", "00010", "00100", "01000"),
    "?": ("01110", "10001", "00001", "00010", "00100", "00000", "00100"),
    "!": ("00100", "00100", "00100", "00100", "00100", "00000", "00100"),
    "'": ("00100", "00100", "01000", "00000", "00000", "00000", "00000"),
    '"': ("01010", "01010", "00000", "00000", "00000", "00000", "00000"),
    "$": ("00100", "01111", "10100", "01110", "00101", "11110", "00100"),
    "%": ("11001", "11010", "00010", "00100", "01000", "01011", "10011"),
    "#": ("01010", "01010", "11111", "01010", "11111", "01010", "01010"),
    "~": ("00000", "00000", "01000", "10101", "00010", "00000", "00000"),
    "*": ("00000", "10101", "01110", "11111", "01110", "10101", "00000"),
    "·": ("00000", "00000", "00000", "01100", "01100", "00000", "00000"),
}


# --------------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m e2e.selftest.fake_run",
        description="Write a fake E2E run directory: synthetic pixels, for report tests "
        "and CI smoke.",
    )
    parser.add_argument(
        "run_dir", type=Path, help="where to write it (created; must be empty or a fake)"
    )
    parser.add_argument("--scenario", choices=SCENARIOS, default="rc-failed")
    parser.add_argument("--run-id", help="override the run id (default: fake-<time>-<scenario>)")
    args = parser.parse_args(argv)
    try:
        run = write(args.run_dir, args.scenario, run_id=args.run_id)
    except FakeRunError as exc:
        print(f"fake_run: {exc}", file=sys.stderr)
        return 2
    journeys = [j for j in run.journeys if j.kind == "journey"]
    steps = sum(len(j.steps) for j in journeys)
    print(
        f"fake run ({args.scenario}) written to {args.run_dir}: {len(journeys)} journeys, "
        f"{steps} steps, {len(run.journeys) - len(journeys)} watch items, verdict {run.verdict}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
