"""Setup, upgrade and removal of aisquare itself."""

from __future__ import annotations

from pathlib import Path

from aisquare.core import paths
from aisquare.core.config import AppConfig, save_config
from aisquare.core.store import store_session
from aisquare.core.stubs import stub
from aisquare.core.workspace import current_project
from aisquare.models import SetupReport
from aisquare.services import agents as agents_service
from aisquare.services import project as project_service


def initialize(
    path: Path | None,
    *,
    api_key: str | None,
    local: bool,
    agents: list[str],
    onboard: bool,
    reinit: bool,
    assume_yes: bool,
) -> SetupReport:
    """Set up ``~/.aisquare``, register & snapshot the project, and connect agents.

    Idempotent and non-interactive: safe to re-run (``assume_yes`` is therefore
    moot for now). ``reinit`` resets ``config.toml`` to defaults. Agents named via
    ``--agent`` are connected (hooks installed + context ingested); cloud auth is
    not wired yet.
    """
    home = paths.aisquare_home()
    already_initialized = paths.config_path().exists() or paths.db_path().exists()
    paths.ensure_home()

    if reinit or not paths.config_path().exists():
        save_config(AppConfig())

    project = current_project(path)
    with store_session() as store:
        store.ensure_project(project)

    notes: list[str] = []
    if api_key:
        credentials = paths.credentials_path()
        credentials.write_text(api_key, encoding="utf-8")
        if paths.restrict_to_owner(credentials):
            notes.append("Stored API key in ~/.aisquare/credentials.")
        else:
            notes.append(
                "Stored API key in ~/.aisquare/credentials — but could NOT restrict it to "
                "your account; other users on this machine may be able to read it."
            )
    elif not local:
        notes.append(
            "No API key given — running local-only; re-run with --api-key to connect later."
        )

    onboarded = 0
    if onboard:
        report = project_service.onboard(path, refresh=False)
        onboarded = len(report.seeded)
        if report.snapshot is not None and report.snapshot.status == "ready":
            notes.append(
                f"Snapshot: {report.snapshot.file_count} files, "
                f"{report.snapshot.token_count} tokens packed for fast agent context."
            )
        elif report.snapshot is None:
            notes.append("Codebase snapshot skipped (repomix/Node not available).")

    for agent in agents:
        try:
            connection = agents_service.connect(agent)
        except (KeyError, ValueError) as exc:
            notes.append(f"Could not connect {agent}: {exc}")
            continue
        hook_note = "hooks installed" if connection.hooks_installed else "no hooks for this agent"
        notes.append(f"Connected {agent}: {hook_note}, imported {connection.imported} entries.")

    return SetupReport(
        home=home,
        already_initialized=already_initialized,
        project=project,
        onboarded=onboarded,
        notes=notes,
    )


def upgrade() -> None:
    """Upgrade aisquare and refresh its agent hooks."""
    stub("upgrade")


def uninstall() -> None:
    """Remove agent hooks and optionally wipe local data."""
    stub("uninstall")
