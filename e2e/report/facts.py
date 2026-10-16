"""What the report says about a run, worked out once so the four outputs cannot disagree.

gallery.html, summary.md, step-summary.md and verdict.json each print a verdict,
counts and the failures. Computing those in each renderer is how a banner comes
to say "9 passed" over a table that adds up to 8; here they are computed once,
from run.json alone, and every renderer reads the same :class:`Facts`.

Two rules decide what the report believes when run.json contradicts itself, and
both lean the same way, because a release gate must never under-report:

- A status is the WORST of what the record says about it. A step marked passed
  with a failed check is failed (the recorder's own rule); a journey marked
  passed with a failed step is failed.
- The verdict is the more severe of run.json's ``verdict`` and the one its
  journeys imply (``e2e.contracts.verdict_for``). The harness may know
  something the journeys do not (a preflight failure, a secret-scan hit), so a
  more severe recorded verdict stands; a milder one is raised.

Every such correction becomes a note, so a reader sees that the record and the
report differ instead of discovering it.

Watch items (``Journey.kind == "watch"``) are known bugs from the QA report,
re-checked nightly. They are kept apart from the start: never in the counts,
the failures or the verdict, each reported as "still open" or "FIXED, promote
me" in its own section, and listed in verdict.json's notes so the gate's
readers still see them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from datetime import datetime
from typing import Final, get_args

from e2e.contracts import Journey, JourneyKind, Run, Status, Step, Verdict, verdict_for

STATUSES: Final[tuple[Status, ...]] = get_args(Status)
VERDICTS: Final[tuple[Verdict, ...]] = get_args(Verdict)
KINDS: Final[tuple[JourneyKind, ...]] = get_args(JourneyKind)

# Precedence, ENV_FAILED > GATE_ERROR > RC_FAILED > PASSED, as e2e.contracts states it.
_VERDICT_RANK: Final[dict[Verdict, int]] = {
    "PASSED": 0,
    "RC_FAILED": 1,
    "GATE_ERROR": 2,
    "ENV_FAILED": 3,
}
# Which way a status can only move when the record contradicts itself: towards trouble.
_STATUS_RANK: Final[dict[Status, int]] = {"skipped": 0, "passed": 1, "failed": 2, "error": 3}

VERDICT_EMOJI: Final[dict[Verdict, str]] = {
    "PASSED": "✅",
    "RC_FAILED": "❌",
    "ENV_FAILED": "🟠",
    "GATE_ERROR": "🟣",
}
STATUS_EMOJI: Final[dict[Status, str]] = {
    "passed": "✅",
    "failed": "❌",
    "error": "🟣",
    "skipped": "⚪",
}
VERDICT_MEANING: Final[dict[Verdict, str]] = {
    "PASSED": "Every journey did what a user expects.",
    "RC_FAILED": "aisquare failed a check a user would notice.",
    "ENV_FAILED": "The test machine was not usable, so aisquare was not judged.",
    "GATE_ERROR": "The harness failed or tested nothing; this says nothing about aisquare.",
}

# A watch item's status, read as news about a known bug. "failed" is the expected
# state of an open bug, so it is amber here, never red.
WATCH_LABEL: Final[dict[Status, str]] = {
    "failed": "still open",
    "passed": "FIXED · promote me",
    "error": "check errored",
    "skipped": "not checked",
}
WATCH_EMOJI: Final[dict[Status, str]] = {
    "failed": "🟠",
    "passed": "🟢",
    "error": "🟣",
    "skipped": "⚪",
}
WATCH_LEGEND: Final = (
    "Known bugs from the QA report, re-checked every night; they never change the verdict. "
    "FIXED means the bug no longer reproduces: promote its check to a gate."
)


@dataclass(frozen=True)
class Counts:
    """Outcomes of what ran: steps, plus each journey that recorded no step at all."""

    passed: int = 0
    failed: int = 0
    error: int = 0
    skipped: int = 0

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.error + self.skipped

    def as_dict(self) -> dict[str, int]:
        return {f.name: int(getattr(self, f.name)) for f in fields(self)}


@dataclass(frozen=True)
class Failure:
    """One entry of verdict.json's ``failures``, with what the renderers link it to."""

    test: str
    """``J04.S03.f12-scrollback`` for a step; the journey id when no step carries it."""
    verdict_class: Verdict
    when: str
    message: str
    journey: Journey | None
    step: Step | None


@dataclass(frozen=True)
class JourneyFacts:
    journey: Journey
    status: Status
    """The effective status: the worst of the journey's own and its steps'."""
    steps: tuple[tuple[Step, Status], ...]
    """Each step with its effective status."""

    @property
    def steps_passed(self) -> int:
        return sum(1 for _, status in self.steps if status == "passed")

    @property
    def finding(self) -> str:
        """A watch item's QA-report id, or its journey id when it names none."""
        return self.journey.finding or self.journey.id

    @property
    def watch_label(self) -> str:
        return WATCH_LABEL[self.status]


@dataclass(frozen=True)
class Facts:
    run: Run
    verdict: Verdict
    counts: Counts
    journeys: tuple[JourneyFacts, ...]
    """The gating journeys, in run order. Watch items are in ``watch``."""
    watch: tuple[JourneyFacts, ...]
    failures: tuple[Failure, ...]
    notes: tuple[str, ...]
    """run.json's notes, then the report's own (each correction it made)."""
    duration_s: float
    preflight_failed: bool

    @property
    def journeys_passed(self) -> int:
        return sum(1 for j in self.journeys if j.status == "passed")

    @property
    def watch_notes(self) -> tuple[str, ...]:
        """One line per watch item, ``QA-01 still open: …``, for verdict.json's notes."""
        lines: list[str] = []
        for item in self.watch:
            line = f"{item.finding} {item.watch_label}: {item.journey.title}"
            bad = [s for s, status in item.steps if status in ("failed", "error")]
            if bad:
                line += f" ({failure_message(bad[0], item.journey)})"
            elif item.journey.error and item.status != "passed":
                line += f" ({item.journey.error})"
            lines.append(line)
        return tuple(lines)


def problems(run: Run) -> list[str]:
    """Why run.json cannot be reported on, or ``[]``.

    ``e2e.contracts.load`` checks the shape; the values of the closed sets are
    checked here, because an unknown status, kind or verdict would otherwise be
    rendered as if it meant something, and the release gate reads the result.
    """
    found: list[str] = []
    if run.verdict not in VERDICTS:
        found.append(f"verdict {run.verdict!r} is not one of {', '.join(VERDICTS)}")
    seen: set[str] = set()
    for journey in run.journeys:
        if journey.status not in STATUSES:
            found.append(f"journey {journey.id}: status {journey.status!r} is unknown")
        if journey.kind not in KINDS:
            found.append(f"journey {journey.id}: kind {journey.kind!r} is unknown")
        if journey.id in seen:
            found.append(f"journey {journey.id} appears twice")
        seen.add(journey.id)
        if not _is_number(journey.duration_s):
            found.append(f"journey {journey.id}: duration_s {journey.duration_s!r} is not a number")
        for step in journey.steps:
            if step.status not in STATUSES:
                found.append(f"step {step.id}: status {step.status!r} is unknown")
            if not _is_number(step.duration_s):
                found.append(f"step {step.id}: duration_s {step.duration_s!r} is not a number")
            found.extend(
                f"step {step.id}: check {check.name!r} has ok={check.ok!r}, not a bool"
                for check in step.checks
                if not isinstance(check.ok, bool)
            )
    return found


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def step_status(step: Step) -> Status:
    if step.status == "passed" and any(not check.ok for check in step.checks):
        return "failed"
    return step.status


def _settle(journey: Journey, notes: list[str]) -> JourneyFacts:
    """A journey with its effective statuses, noting every correction made to them."""
    steps = tuple((step, step_status(step)) for step in journey.steps)
    for step, status in steps:
        if status != step.status:
            notes.append(
                f"{step.id} is recorded as {step.status} but has a failed check; "
                f"reported as {status}."
            )
    status = journey.status
    worst = max((s for _, s in steps), key=_STATUS_RANK.__getitem__, default=None)
    if worst in ("failed", "error") and _STATUS_RANK[worst] > _STATUS_RANK[status]:
        notes.append(
            f"{journey.id} is recorded as {status} but a step {worst}; reported as {worst}."
        )
        status = worst
    return JourneyFacts(journey, status, steps)


def of(run: Run) -> Facts:
    """Everything the renderers print, from run.json alone."""
    notes = list(run.notes)
    settled = [_settle(journey, notes) for journey in run.journeys]
    journeys = [j for j in settled if j.journey.kind != "watch"]
    watch = [j for j in settled if j.journey.kind == "watch"]

    counts = {status: 0 for status in STATUSES}
    failures: list[Failure] = []
    for facts in journeys:
        journey = facts.journey
        for step, status in facts.steps:
            counts[status] += 1
            if status in ("failed", "error"):
                failures.append(
                    Failure(
                        test=qualified_id(journey, step),
                        verdict_class="RC_FAILED" if status == "failed" else "GATE_ERROR",
                        when="call",
                        message=failure_message(step, journey),
                        journey=journey,
                        step=step,
                    )
                )
        if not facts.steps:
            counts[facts.status] += 1  # a journey that recorded nothing still happened, once
        carried = any(s in ("failed", "error") for _, s in facts.steps)
        if facts.status in ("failed", "error") and not carried:
            # It failed between steps (or before the first): count it, or the counts
            # would say "0 failed" under a red banner.
            if facts.steps:
                counts[facts.status] += 1
            failures.append(
                Failure(
                    test=journey.id,
                    verdict_class="RC_FAILED" if facts.status == "failed" else "GATE_ERROR",
                    when="call",
                    message=journey.error or f"{journey.id} {facts.status} outside any step",
                    journey=journey,
                    step=None,
                )
            )

    implied = verdict_for(
        [replace(j.journey, status=j.status) for j in settled],
        env_failed=run.verdict == "ENV_FAILED",
    )
    verdict: Verdict = run.verdict
    if implied != run.verdict:
        if _VERDICT_RANK[implied] > _VERDICT_RANK[run.verdict]:
            notes.append(
                f"run.json says {run.verdict}, but its journeys say {implied}; "
                f"reported as {implied}."
            )
            verdict = implied
        else:
            notes.append(
                f"run.json says {run.verdict}, more severe than its journeys ({implied}); kept."
            )

    # Preflight is what fails before anything runs, watch items included.
    preflight_failed = verdict == "ENV_FAILED" and not any(j.steps for j in settled)
    tested = any(j.steps for j in journeys)
    if verdict == "ENV_FAILED":
        failures.insert(
            0,
            Failure(
                test="preflight" if preflight_failed else "environment",
                verdict_class="ENV_FAILED",
                when="setup" if preflight_failed else "call",
                message=_environment_message(run),
                journey=None,
                step=None,
            ),
        )
    if verdict == "GATE_ERROR" and not any(f.verdict_class == "GATE_ERROR" for f in failures):
        # A run that tested nothing is GATE_ERROR with no failing step to point at.
        failures.append(
            Failure(
                test="run",
                verdict_class="GATE_ERROR",
                when="call",
                message="the harness reported an error" if tested else "nothing was tested",
                journey=None,
                step=None,
            )
        )
    return Facts(
        run=run,
        verdict=verdict,
        counts=Counts(**counts),
        journeys=tuple(journeys),
        watch=tuple(watch),
        failures=tuple(failures),
        notes=tuple(notes),
        duration_s=run_duration(run),
        preflight_failed=preflight_failed,
    )


def _environment_message(run: Run) -> str:
    """The reason the environment failed: a skipped journey's reason, else a note."""
    for journey in run.journeys:
        if journey.status == "skipped" and journey.error:
            return journey.error
    for note in run.notes:
        if "preflight" in note.lower():
            return note
    return "the environment failed before aisquare could be judged"


def qualified_id(journey: Journey, step: Step) -> str:
    """``<J>.<step-id>``, without doubling a prefix the recorder already wrote."""
    if step.id.startswith(journey.id + "."):
        return step.id
    return f"{journey.id}.{step.id}"


def short_step(journey: Journey, step: Step) -> str:
    """``S03`` out of ``J04.S03.f12-scrollback``: what a timeline numbers a step by."""
    rest = step.id[len(journey.id) + 1 :] if step.id.startswith(journey.id + ".") else step.id
    return rest.split(".", 1)[0] or step.id


def failure_message(step: Step, journey: Journey) -> str:
    bad = [c for c in step.checks if not c.ok]
    if bad:
        return "; ".join(f"{c.name}: {c.detail}" if c.detail else c.name for c in bad)
    return step.note or journey.error or f"{step.id} {step_status(step)}"


def run_duration(run: Run) -> float:
    """Wall time from the run's own timestamps; the journeys' sum when those do not parse."""
    try:
        began = datetime.fromisoformat(run.started_at)
        ended = datetime.fromisoformat(run.finished_at)
        seconds = (ended - began).total_seconds()
    except (TypeError, ValueError):
        seconds = -1.0
    if seconds >= 0:
        return seconds
    return float(sum(j.duration_s for j in run.journeys if _is_number(j.duration_s)))


def duration(seconds: float) -> str:
    """``0.4s`` · ``41.2s`` · ``3m 05s`` · ``1h 02m``: short enough for a table cell."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    whole = round(seconds)
    if whole < 3600:
        return f"{whole // 60}m {whole % 60:02d}s"
    return f"{whole // 3600}h {whole % 3600 // 60:02d}m"


def headline(facts: Facts) -> str:
    """The verdict in words, for a heading: ``1 of 3 journeys failed``."""
    total = len(facts.journeys)
    noun = "journey" if total == 1 else "journeys"
    if facts.verdict == "PASSED":
        return f"all {total} {noun} passed" if total != 1 else "the 1 journey passed"
    if facts.verdict == "RC_FAILED":
        failed = sum(1 for j in facts.journeys if j.status == "failed")
        return f"{failed} of {total} {noun} failed"
    if facts.verdict == "ENV_FAILED":
        return "the test machine was not usable; aisquare was not judged"
    if not any(j.steps for j in facts.journeys):
        return "nothing was tested"
    return "the harness failed; aisquare was not judged"


@dataclass(frozen=True)
class Links:
    """Where the outputs can send a reader beyond themselves."""

    artifact_name: str = "e2e-report"
    """The Actions artifact that carries report/gallery.html."""
    repo_url: str | None = None
    """``https://github.com/OWNER/NAME``: a commit becomes a link."""
    run_url: str | None = None
    """The workflow run that produced this report."""

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Links:
        """What a GitHub Actions step knows; nothing at all on a laptop."""
        artifact = environ.get("E2E_ARTIFACT_NAME") or cls.artifact_name
        server = (environ.get("GITHUB_SERVER_URL") or "https://github.com").rstrip("/")
        repository = environ.get("GITHUB_REPOSITORY")
        repo_url = f"{server}/{repository}" if repository else None
        run_id = environ.get("GITHUB_RUN_ID")
        run_url = f"{repo_url}/actions/runs/{run_id}" if repo_url and run_id else None
        return cls(artifact_name=artifact, repo_url=repo_url, run_url=run_url)

    def commit_url(self, commit: str) -> str | None:
        hexdigits = set("0123456789abcdef")
        if self.repo_url is None or len(commit) < 7 or not set(commit.lower()) <= hexdigits:
            return None
        return f"{self.repo_url}/commit/{commit}"


def env_line(run: Run) -> str:
    """``os/runner/track``: the release gate's ``env`` and every summary's first fact."""
    return f"{run.env.os}/{run.env.runner}/{run.env.track}"
