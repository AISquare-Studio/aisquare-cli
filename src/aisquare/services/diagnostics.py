"""Health checks and introspection."""

from __future__ import annotations

from aisquare.core import agents as agent_core
from aisquare.core import paths
from aisquare.core.config import load_config
from aisquare.core.injection import load_last
from aisquare.core.store import store_session
from aisquare.core.stubs import stub
from aisquare.core.workspace import active_project
from aisquare.models import DoctorCheck, InjectionRecord, PromptRecord, StatusReport


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
    """Run health checks over the install and report each one."""
    checks: list[DoctorCheck] = []

    home = paths.aisquare_home()
    checks.append(
        DoctorCheck(
            name="home",
            ok=home.exists(),
            detail=f"{home} exists"
            if home.exists()
            else f"{home} is missing — run `aisquare init`",
        )
    )

    try:
        load_config()
        checks.append(DoctorCheck(name="config", ok=True, detail="config.toml is valid"))
    except Exception as exc:  # diagnostics must never crash
        checks.append(DoctorCheck(name="config", ok=False, detail=f"config error: {exc}"))

    try:
        with store_session() as store:
            user_entries = len(store.entries("user"))
        checks.append(
            DoctorCheck(
                name="database",
                ok=True,
                detail=f"context.db is readable ({user_entries} user entries)",
            )
        )
    except Exception as exc:  # diagnostics must never crash
        checks.append(DoctorCheck(name="database", ok=False, detail=f"database error: {exc}"))

    detected = [agent.name for agent in agent_core.detect_all() if agent.detected]
    checks.append(
        DoctorCheck(
            name="agents",
            ok=bool(detected),
            detail="detected: " + ", ".join(detected) if detected else "no coding agents detected",
        )
    )
    return checks


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
