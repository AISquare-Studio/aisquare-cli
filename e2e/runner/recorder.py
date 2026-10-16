"""Journeys and steps as evidence: every step ends with a screenshot, its screen text
and the checks a user would make, whatever happened inside it.

    with recorder.journey("J02", "Welcome", story) as j:
        with j.step("S03", "Start the coders") as s:
            term.press("Enter")
            screen = term.wait_for("Your fleet is up")
            s.check("all three agents in the sidebar", "coder-2" in screen.text)

A failed check fails the step; a failed step fails the journey, and pytest sees an
``AssertionError`` naming the step. The screenshot is taken in a ``finally``, so the
picture of what went wrong is always there for the report.
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from e2e.contracts import Check, Journey, JourneyKind, Status, Step
from e2e.runner.terminal import TRACEBACK, StepTimeout, TerminalSession


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


class StepRecorder:
    def __init__(self, step: Step) -> None:
        self._step = step

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self._step.checks.append(Check(name=name, ok=bool(ok), detail=detail))
        return bool(ok)

    def note(self, text: str) -> None:
        self._step.note = text


class JourneyRecorder:
    def __init__(self, journey: Journey, run_dir: Path, terminal: TerminalSession | None) -> None:
        self.journey = journey
        self._run_dir = run_dir
        self.terminal = terminal

    @contextmanager
    def step(self, sid: str, title: str, *, slug: str | None = None) -> Iterator[StepRecorder]:
        name = slug or title.lower().replace(" ", "-").replace("/", "-")[:40]
        step = Step(
            id=f"{self.journey.id}.{sid}.{name}",
            title=title,
            status="passed",
            started_at=_now(),
            duration_s=0.0,
        )
        recorder = StepRecorder(step)
        start = time.monotonic()
        failure: BaseException | None = None
        try:
            yield recorder
        except BaseException as exc:  # recorded, then re-raised below
            failure = exc
            step.status = "failed" if isinstance(exc, AssertionError) else "error"
            step.note = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        finally:
            self._evidence(step, recorder)
            step.duration_s = round(time.monotonic() - start, 2)
            if step.status == "passed" and any(not c.ok for c in step.checks):
                step.status = "failed"
            self.journey.steps.append(step)
        if failure is not None:
            raise failure
        if step.status == "failed":
            bad = "; ".join(
                f"{c.name} ({c.detail})" if c.detail else c.name for c in step.checks if not c.ok
            )
            raise AssertionError(f"{step.id}: {bad}")

    def _evidence(self, step: Step, recorder: StepRecorder) -> None:
        if self.terminal is None:
            return
        png = self._run_dir / "shots" / self.journey.id / f"{step.id}.png"
        txt = self._run_dir / "text" / self.journey.id / f"{step.id}.txt"
        try:
            screen = self.terminal.shot(png, txt)
        except Exception as exc:  # a dead page must not hide the step's own failure
            step.note = (step.note + " · " if step.note else "") + f"no screenshot: {exc}"
            return
        step.shot = str(png.relative_to(self._run_dir))
        step.text = str(txt.relative_to(self._run_dir))
        # The demo render's crash rule, on every screen of every step.
        crash = TRACEBACK.search(screen.text)
        recorder.check("no traceback on screen", crash is None, crash.group(0) if crash else "")


class Recorder:
    """Collects the journeys of one run; ``conftest.py`` turns them into run.json."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.journeys: list[Journey] = []

    @contextmanager
    def journey(
        self,
        jid: str,
        title: str,
        story: str,
        terminal: TerminalSession | None,
        *,
        kind: JourneyKind = "journey",
        finding: str | None = None,
    ) -> Iterator[JourneyRecorder]:
        journey = Journey(
            id=jid,
            title=title,
            story=story,
            status="passed",
            duration_s=0.0,
            kind=kind,
            finding=finding,
        )
        self.journeys.append(journey)
        start = time.monotonic()
        try:
            yield JourneyRecorder(journey, self.run_dir, terminal)
        except BaseException as exc:
            status: Status = "failed" if isinstance(exc, AssertionError) else "error"
            if type(exc).__name__ in {"Skipped", "SkipException"}:
                status = "skipped"
            journey.status = status
            journey.error = (str(exc).splitlines() or [type(exc).__name__])[0]
            if status == "error":
                journey.error += " · " + "".join(traceback.format_exception_only(exc)).strip()[:300]
            raise
        finally:
            journey.duration_s = round(time.monotonic() - start, 2)

    def skipped(self, jid: str, title: str, story: str, why: str) -> None:
        self.journeys.append(
            Journey(id=jid, title=title, story=story, status="skipped", duration_s=0.0, error=why)
        )


__all__ = ["JourneyRecorder", "Recorder", "StepRecorder", "StepTimeout"]
