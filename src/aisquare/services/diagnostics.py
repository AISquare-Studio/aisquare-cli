"""Health checks and introspection."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

from aisquare.core import agents as agent_core
from aisquare.core import brain as brain_core
from aisquare.core import harness, orchestrator, paths
from aisquare.core import snapshot as snapshot_core
from aisquare.core.config import load_config
from aisquare.core.injection import load_last
from aisquare.core.store import store_session
from aisquare.core.stubs import stub
from aisquare.core.workspace import active_project
from aisquare.models import CheckStatus, DoctorCheck, InjectionRecord, PromptRecord, StatusReport
from aisquare.services import distill as distill_service


def status() -> StatusReport:
    """Summarise installation health, pools, the active project and agents."""
    # Compute "initialized" before opening the store, which would create the db.
    initialized = paths.config_path().exists() or paths.db_path().exists()
    agents = agent_core.detect_all()
    with store_session() as store:
        project = active_project(store)
        report = StatusReport(
            home=paths.aisquare_home(),
            initialized=initialized,
            user_entries=len(store.entries("user")),
            project_entries=len(store.entries("project", project_id=project.id)),
            active_project=project,
            project_count=len(store.list_projects()),
            agents_detected=[agent.name for agent in agents if agent.detected],
            agents_connected=[agent.name for agent in agents if agent.connected],
        )
    return report


def doctor() -> list[DoctorCheck]:
    """Run health checks over the install, dependencies and integration."""
    return [
        _check_python(),
        _check_install(),
        _check_home(),
        _check_config(),
        _check_database(),
        _check_repomix(),
        _check_tiktoken(),
        _check_claude_code(),
        _check_snapshot(),
        _check_brain(),
        _check_harness(),
    ]


def _ok(name: str, detail: str) -> DoctorCheck:
    return DoctorCheck(name=name, status=CheckStatus.ok, detail=detail)


def _warn(name: str, detail: str, fix: str) -> DoctorCheck:
    return DoctorCheck(name=name, status=CheckStatus.warn, detail=detail, fix=fix)


def _fail(name: str, detail: str, fix: str) -> DoctorCheck:
    return DoctorCheck(name=name, status=CheckStatus.fail, detail=detail, fix=fix)


def _check_python() -> DoctorCheck:
    info = sys.version_info
    return _ok("python", f"Python {info.major}.{info.minor}.{info.micro}")


def _check_install() -> DoctorCheck:
    """Where aisquare runs from — the Claude Code hook needs a stable path."""
    binary = shutil.which("aisquare")
    if binary is None:
        return _warn(
            "install",
            "aisquare is not on your PATH",
            "Install as a global tool: pipx install aisquare",
        )
    if {".venv", "venv"} & set(Path(binary).parts):
        return _warn(
            "install",
            f"aisquare runs from a virtualenv ({binary})",
            "For stable Claude Code hooks, install globally: pipx install aisquare",
        )
    return _ok("install", f"aisquare at {binary}")


def _check_home() -> DoctorCheck:
    home = paths.aisquare_home()
    if home.exists():
        return _ok("home", f"{home} exists")
    return _fail("home", f"{home} is missing", "Set it up: aisquare init")


def _check_config() -> DoctorCheck:
    try:
        load_config()
    except Exception as exc:  # diagnostics must never crash
        return _fail(
            "config", f"config.toml is invalid: {exc}", "Fix or reset: aisquare init --reinit"
        )
    return _ok("config", "config.toml is valid")


def _check_database() -> DoctorCheck:
    try:
        with store_session() as store:
            count = len(store.entries("user"))
    except Exception as exc:  # diagnostics must never crash
        return _fail("database", f"context.db is unreadable: {exc}", "Re-initialise: aisquare init")
    return _ok("database", f"context.db is readable ({count} user entries)")


def _check_repomix() -> DoctorCheck:
    if shutil.which("repomix"):
        return _ok("repomix", "repomix found — codebase snapshots enabled")
    if shutil.which("npx"):
        return _ok("repomix", "repomix available on demand via npx")
    return _warn(
        "repomix",
        "repomix not found — codebase snapshots are disabled",
        "Install Node.js, then: npm install -g repomix",
    )


def _check_tiktoken() -> DoctorCheck:
    if _has_module("tiktoken"):
        return _ok("tiktoken", "exact snapshot token counts enabled")
    return _warn(
        "tiktoken",
        "tiktoken not installed — snapshot token counts are estimated",
        "Install it: pip install tiktoken (or: pipx inject aisquare tiktoken)",
    )


def _check_claude_code() -> DoctorCheck:
    info = agent_core.detect("claude-code")
    if info is None or not info.detected:
        return _ok("claude-code", "Claude Code not detected on this machine")
    if agent_core.hooks_installed("claude-code"):
        return _ok("claude-code", "Claude Code connected (all lifecycle hooks installed)")
    return _warn(
        "claude-code",
        "Claude Code hooks are missing or outdated (older installs lack the "
        "Stop/Notification/SessionEnd events)",
        "(Re)connect it: aisquare agents connect claude-code",
    )


def _check_snapshot() -> DoctorCheck:
    try:
        with store_session() as store:
            project = active_project(store)
        snap = snapshot_core.load(project.id)
    except Exception as exc:  # diagnostics must never crash
        return _warn(
            "snapshot", f"could not check the snapshot: {exc}", "Try: aisquare project onboard"
        )
    if snap is not None and snap.status == "ready":
        return _ok(
            "snapshot", f"snapshot ready ({snap.file_count} files, {snap.token_count} tokens)"
        )
    return _warn(
        "snapshot",
        "no codebase snapshot for the active project",
        "Pack one: aisquare project onboard",
    )


def _check_brain() -> DoctorCheck:
    """The team's long-term memory: gbrain presence, brain state, distill lag."""
    if not brain_core.brain_enabled():
        return _ok("brain", "brain layer disabled (AISQUARE_BRAIN=0)")
    version = brain_core.gbrain_version()
    if version is None:
        return _warn(
            "brain",
            "gbrain not found — team decisions/results are not distilled",
            "Optional: install the AISquare gbrain CLI to enable long-term memory "
            "(not the unrelated 'gbrain' on public npm)",
        )
    try:
        project = orchestrator.team_project()
        with store_session() as store:
            if not store.team_active(project.id):
                return _ok("brain", f"gbrain {version} ready (orchestrator not active here)")
            lag = distill_service.pending(store, project.id)
    except Exception as exc:  # diagnostics must never crash
        return _warn("brain", f"could not check the brain: {exc}", "Try: aisquare team distill")
    if not brain_core.brain_ready(project.id):
        return _warn(
            "brain",
            f"gbrain {version} found but this project's brain is not initialised",
            "It initialises on the first distill: aisquare team distill",
        )
    # The embedding schema is fixed at create time, and the knob lives in
    # per-shell env (never persisted), so BOTH mismatch directions are real and
    # invisible without a signal. Surface either rather than reporting healthy.
    knob = brain_core.embeddings_enabled()
    embeds = brain_core.brain_embeds(project.id)
    if knob and not embeds:
        return _warn(
            "brain",
            f"gbrain {version}, brain ready — but it was created WITHOUT embeddings, "
            "so AISQUARE_BRAIN_EMBED has no effect here (recall stays keyword-only)",
            "Rebuild embedding-capable: remove ~/.aisquare/projects/<id>/brain, then "
            "AISQUARE_BRAIN_EMBED=1 aisquare team distill --all",
        )
    if embeds and not knob:
        return _warn(
            "brain",
            f"gbrain {version}, brain has embeddings but AISQUARE_BRAIN_EMBED is off — "
            "recall stays keyword-only and new pages are distilled without vectors",
            "Export AISQUARE_BRAIN_EMBED=1 (and OPENAI_API_KEY) in the shells that run "
            "aisquare, or add them to your shell profile",
        )
    embed = " (embeddings on)" if embeds else ""
    if lag > 0:
        return _ok(
            "brain", f"gbrain {version}, brain ready{embed} ({lag} pipe events awaiting distill)"
        )
    return _ok("brain", f"gbrain {version}, brain ready and fully distilled{embed}")


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


def last_injection() -> InjectionRecord | None:
    """Return the most recent injection record (backs ``why``), or None."""
    return load_last()


def show_log(limit: int = 20) -> list[PromptRecord]:
    """Recent captured user prompts for the active project (newest first)."""
    with store_session() as store:
        project = active_project(store)
        return store.recent_prompts(project.id, limit=limit)


def open_home() -> None:
    """Open the aisquare home directory or web dashboard."""
    stub("open")


def _check_harness() -> DoctorCheck:
    """Role→model harness health: env interference, mismatched live sessions, cache.

    Read-only and offline by design (doctor makes no network calls): ladder
    availability comes from the probe cache; live verification is
    ``aisquare team spawn <role>``. Warns, never fails — a stale cache or an
    off-ladder session is advice, not breakage.
    """
    name = "agent harness"
    try:
        # Not-applicable = ok: a repo that never opted into the orchestrator must
        # see nothing from the harness, not even an env-hygiene warning.
        if not orchestrator.team_enabled():
            return _ok(name, "orchestrator disabled")
        with store_session() as store:
            project = orchestrator.team_project(None)
            if not store.team_active(project.id):
                return _ok(name, "not activated for this project")
            live = [s for s in store.team_sessions(project.id) if s.ended_at is None]
        interference = harness.interfering_env()
        mismatches: list[str] = []
        for session in live:
            complaint = harness.model_mismatch(session.role, session.model)
            if complaint:
                mismatches.append(f"{session.id[:8]} ({session.role}): {complaint}")
        problems: list[str] = []
        if interference:
            problems.append(f"env overrides model selection: {', '.join(interference)}")
        if mismatches:
            problems.append("off-ladder sessions: " + "; ".join(mismatches))
        if problems:
            return _warn(
                name,
                " — ".join(problems),
                "unset the interfering env vars and relaunch off-ladder roles with "
                "`aisquare team spawn <role>`",
            )
        fable = harness.cached_probe("fable")
        if fable is not None and not fable.available:
            return _warn(
                name,
                f"fable probed unavailable ({fable.reason}) — top-tier roles fall back to opus",
                "expected on non-enterprise accounts; re-check with "
                "`aisquare team spawn planner --refresh`",
            )
        detail = "role ladders clean"
        if fable is not None and fable.resolved_id:
            detail = f"role ladders clean; fable available ({fable.resolved_id})"
        return _ok(name, detail)
    except Exception:  # diagnostics must never crash
        return _ok(name, "not evaluated")
