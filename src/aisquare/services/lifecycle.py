"""Setup, upgrade and removal of aisquare itself."""

from __future__ import annotations

from pathlib import Path

from aisquare.core import paths
from aisquare.core.config import AppConfig, save_config
from aisquare.core.store import store_session
from aisquare.core.stubs import stub
from aisquare.core.workspace import current_project
from aisquare.models import SetupReport
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
    """Set up ``~/.aisquare`` and register the project at ``path`` (or the cwd).

    Idempotent and non-interactive: safe to re-run (``assume_yes`` is therefore
    moot for now). ``reinit`` resets ``config.toml`` to defaults. Agent-hook
    installation and cloud auth are not wired yet; requests for them are
    reported as notes rather than performed.
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
        credentials.chmod(0o600)
        notes.append("Stored API key in ~/.aisquare/credentials.")
    elif not local:
        notes.append(
            "No API key given — running local-only; re-run with --api-key to connect later."
        )
    if agents:
        requested = ", ".join(agents)
        notes.append(
            f"Agent hooks not installed yet (requested: {requested}); coming with `agents connect`."
        )

    onboarded = len(project_service.onboard(path, refresh=False)) if onboard else 0
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
