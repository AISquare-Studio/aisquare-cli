"""What one E2E leg records, and what the report, the workflow and the publisher read.

One leg (one OS by one track) writes one *run directory*:

    <run>/run.json                      this module's ``Run``, the single source of truth
    <run>/shots/<J>/<step-id>.png       real pixels of the terminal window at the end of a step
    <run>/text/<J>/<step-id>.txt        the same screen as text, read from the emulator's buffer
    <run>/video/<J>.webm                Playwright's recording of the whole journey
    <run>/logs/…                        installer output, aisquare --json state, ttyd stderr
    <run>/report/…                      written by ``e2e/report`` from run.json only

Paths inside run.json are RELATIVE to the run directory, so a run can be moved,
zipped by ``actions/upload-artifact`` and re-rendered anywhere.

Deliberately stdlib-only (dataclasses + json): the report builder and the
publisher run in CI steps that have not installed the harness's requirements.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

SCHEMA = "aisquare-e2e/run/v1"

Status = Literal["passed", "failed", "skipped", "error"]
"""A step or journey outcome. ``error`` is the harness's own fault (never the product's)."""

Verdict = Literal["PASSED", "RC_FAILED", "ENV_FAILED", "GATE_ERROR"]
"""The QA org's release-gate classes. Precedence: ENV_FAILED > GATE_ERROR > RC_FAILED.

PROVIDER (a model provider failing) only exists once real agents run; a run that
tested nothing is GATE_ERROR, never PASSED.
"""


@dataclass
class Check:
    """One assertion a user would make about a screen or about the machine."""

    name: str
    ok: bool
    detail: str = ""


@dataclass
class Step:
    """One user-visible moment of a journey, with the evidence for it."""

    id: str
    """Stable across nights, e.g. ``J02.S03.fleet-up``, so shots pair up for diffs."""
    title: str
    status: Status
    started_at: str
    duration_s: float
    shot: str | None = None
    text: str | None = None
    checks: list[Check] = field(default_factory=list)
    note: str = ""


@dataclass
class Journey:
    """A user story driven end to end through the real terminal."""

    id: str
    title: str
    story: str
    status: Status
    duration_s: float
    steps: list[Step] = field(default_factory=list)
    video: str | None = None
    error: str | None = None
    """The failure's first line, when the journey failed or errored."""


@dataclass
class Environment:
    os: str
    """e.g. ``ubuntu-24.04 (act)`` or ``macOS 15.3 (local)``."""
    runner: str
    """``act 0.2.89`` · ``github-hosted`` · ``local``."""
    track: str
    """``main`` (a wheel built from this tree) or ``release`` (the README one-liner)."""
    agent: str
    """What runs where ``claude`` would: ``e2e stand-in (no LLM)`` for now."""
    aisquare: str
    commit: str
    versions: dict[str, str] = field(default_factory=dict)
    """tmux, ttyd, chromium, xterm.js, uv, python … as measured during the run."""


@dataclass
class Run:
    run_id: str
    started_at: str
    finished_at: str
    verdict: Verdict
    env: Environment
    journeys: list[Journey] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    schema: str = SCHEMA


def save(run: Run, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(run), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def load(path: Path) -> Run:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        raise ValueError(f"{path}: not an {SCHEMA} file (schema={data.get('schema')!r})")
    env = Environment(**data["env"])
    journeys = [
        Journey(
            **{k: v for k, v in j.items() if k != "steps"},
            steps=[
                Step(
                    **{k: v for k, v in s.items() if k != "checks"},
                    checks=[Check(**c) for c in s.get("checks", [])],
                )
                for s in j.get("steps", [])
            ],
        )
        for j in data.get("journeys", [])
    ]
    return Run(
        run_id=data["run_id"],
        started_at=data["started_at"],
        finished_at=data["finished_at"],
        verdict=data["verdict"],
        env=env,
        journeys=journeys,
        notes=list(data.get("notes", [])),
        schema=data["schema"],
    )


def verdict_for(journeys: list[Journey], *, env_failed: bool = False) -> Verdict:
    """The run's verdict from its journeys, with the release gate's precedence."""
    if env_failed:
        return "ENV_FAILED"
    if not journeys or all(j.status == "skipped" for j in journeys):
        return "GATE_ERROR"
    if any(j.status == "error" for j in journeys):
        return "GATE_ERROR"
    if any(j.status == "failed" for j in journeys):
        return "RC_FAILED"
    return "PASSED"
