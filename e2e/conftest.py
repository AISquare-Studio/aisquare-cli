"""Session wiring: one user machine, one HTTP server, one browser — and run.json at the end.

Environment (all optional locally; the workflow sets them):
  E2E_OUT        run directory (default: out/e2e/local-<track>)
  E2E_TRACK      ``main`` (install this tree's wheel through install.sh) or ``release``
                 (the README one-liner against PyPI)
  E2E_WHEEL      the wheel under test (main track); built with ``uv build`` when unset
  E2E_USER       run the machine as this OS user (CI); unset = scratch HOME (local)
  E2E_RUNNER     ``act`` · ``github-hosted`` · ``local`` (shown in the report)
  E2E_HEADED=1   watch Chromium drive the terminal
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, sync_playwright

from e2e.contracts import Environment, Run, save, verdict_for
from e2e.runner.machine import Machine
from e2e.runner.recorder import Recorder
from e2e.runner.server import StaticServer

REPO = Path(__file__).resolve().parents[1]
E2E = REPO / "e2e"
XTERM_VERSION = "6.0.0"

_STARTED = datetime.now(tz=UTC).isoformat(timespec="seconds")
_FACTS: dict[str, str] = {}
_RECORDER: Recorder | None = None


def track() -> str:
    value = os.environ.get("E2E_TRACK", "main")
    if value not in {"main", "release"}:
        raise pytest.UsageError(f"E2E_TRACK must be main or release, not {value!r}")
    return value


def _run_dir() -> Path:
    configured = os.environ.get("E2E_OUT")
    return Path(configured).resolve() if configured else REPO / "out" / "e2e" / f"local-{track()}"


def pytest_configure(config: pytest.Config) -> None:
    """JUnit next to run.json, whatever directory pytest was started from."""
    if not getattr(config.option, "xmlpath", None):
        out = _run_dir()
        out.mkdir(parents=True, exist_ok=True)
        config.option.xmlpath = str(out / "junit.xml")


@pytest.fixture(scope="session")
def run_dir() -> Path:
    root = _run_dir()
    root.mkdir(parents=True, exist_ok=True)
    # Only what a previous run of THIS harness wrote; never a blanket rmtree of a path from env.
    for name in ("shots", "text", "video", "logs", "report", "dist"):
        shutil.rmtree(root / name, ignore_errors=True)
    (root / "run.json").unlink(missing_ok=True)
    return root


@pytest.fixture(scope="session")
def recorder(run_dir: Path) -> Recorder:
    global _RECORDER
    _RECORDER = Recorder(run_dir)
    return _RECORDER


@pytest.fixture(scope="session")
def wheel(run_dir: Path) -> Path | None:
    if track() == "release":
        return None
    configured = os.environ.get("E2E_WHEEL")
    if configured:
        return Path(configured).resolve()
    dist = run_dir / "dist"
    subprocess.run(["uv", "build", "--wheel", "-q", "-o", str(dist)], cwd=REPO, check=True)
    return next(dist.glob("aisquare_cli-*.whl"))


@pytest.fixture(scope="session")
def server(wheel: Path | None) -> Iterator[StaticServer]:
    routes: dict[str, Path] = {
        "/vendor/": E2E / "terminal" / "vendor",
        "/client/": E2E / "terminal" / "client",
        "/install.sh": REPO / "install.sh",
    }
    if wheel is not None:
        routes["/dist/"] = wheel.parent
    srv = StaticServer(routes).start()
    yield srv
    srv.close()


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    with sync_playwright() as pw:
        chromium = pw.chromium.launch(headless=os.environ.get("E2E_HEADED") != "1")
        _FACTS["chromium"] = chromium.version
        yield chromium
        chromium.close()


@pytest.fixture(scope="session")
def machine(run_dir: Path) -> Iterator[Machine]:
    box = Machine.create(agent_src=E2E / "agent" / "claude")
    try:
        yield box
    finally:
        _collect_machine_facts(box, run_dir)
        repo = box.home / "acme-api"
        if repo.is_dir():
            box.probe("aisquare fleet shutdown --yes >/dev/null 2>&1; true", cwd=repo, timeout=90)
        box.close()


@pytest.fixture(scope="session")
def repo(machine: Machine) -> Path:
    """The small git repo the user stands in when they install aisquare (shared by modules)."""
    return machine.seed_repo("acme-api")


def _collect_machine_facts(box: Machine, run_dir: Path) -> None:
    """Versions as the user's machine sees them, plus the product's own state, for the report."""
    for name, cmd in {
        "aisquare": "aisquare --version",
        "tmux": "tmux -V",
        "uv": "uv --version",
        "git": "git --version",
        "node": "node --version",
        "claude (stand-in)": "claude --version",
    }.items():
        code, out = box.probe(cmd, timeout=30)
        if code == 0 and out.strip():
            _FACTS[name] = out.strip().splitlines()[0]
    logs = run_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    repo = box.home / "acme-api"
    for name, cmd in {
        "doctor.json": "aisquare --json doctor",
        "fleet-ls.json": "aisquare --json fleet ls",
    }.items():
        code, out = box.probe(cmd, cwd=repo if repo.is_dir() else None, timeout=120)
        (logs / name).write_text(out, encoding="utf-8")


def _environment() -> Environment:
    commit = subprocess.run(
        ["git", "rev-parse", "--short=12", "HEAD"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    ttyd = subprocess.run(
        ["ttyd", "--version"], capture_output=True, text=True, check=False
    ).stdout.strip()
    runner = os.environ.get("E2E_RUNNER", "local")
    system = f"{platform.system()} {platform.release()} {platform.machine()}"
    if runner == "act":
        system += " (act container)"
    versions = {"xterm.js": XTERM_VERSION, "ttyd": ttyd.replace("ttyd version ", ""), **_FACTS}
    versions["python (harness)"] = platform.python_version()
    return Environment(
        os=system,
        runner=runner,
        track=track(),
        agent="e2e stand-in (no LLM)",
        aisquare=_FACTS.get("aisquare", "not installed"),
        commit=commit or "unknown",
        versions={k: v for k, v in versions.items() if k != "aisquare"},
    )


def _run_id() -> str:
    """GitHub's run id (+ attempt) on GitHub; a UTC timestamp everywhere else.

    act sets GITHUB_RUN_ID to "1" for every run, so trusting it there would give every
    local run the same id and make published runs overwrite each other.
    """
    stamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
    if os.environ.get("E2E_RUNNER") == "github-hosted" and os.environ.get("GITHUB_RUN_ID"):
        # The track too: a nightly runs both tracks in ONE workflow run, and each leg's
        # evidence is published under its own id (e2e/report/ci_publish.py).
        run, attempt = os.environ["GITHUB_RUN_ID"], os.environ.get("GITHUB_RUN_ATTEMPT", "1")
        return f"{run}-{attempt}-{track()}"
    runner = os.environ.get("E2E_RUNNER", "local")
    return f"{stamp}-{runner}-{track()}"


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if _RECORDER is None:
        return
    run = Run(
        run_id=_run_id(),
        started_at=_STARTED,
        finished_at=datetime.now(tz=UTC).isoformat(timespec="seconds"),
        verdict=verdict_for(_RECORDER.journeys),
        env=_environment(),
        journeys=_RECORDER.journeys,
        notes=[
            "Agents are the e2e stand-in: aisquare itself (installer, Welcome, fleet, panes, "
            "worktrees) is real; no model is called.",
        ],
    )
    save(run, _RECORDER.run_dir / "run.json")


def json_or_none(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None
