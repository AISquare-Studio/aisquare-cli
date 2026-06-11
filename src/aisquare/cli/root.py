"""Top-level commands: setup, memory shortcuts, diagnostics and account."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from aisquare.cli.common import resolve_pool
from aisquare.services import auth as auth_service
from aisquare.services import context as context_service
from aisquare.services import diagnostics as diagnostics_service
from aisquare.services import lifecycle as lifecycle_service
from aisquare.services import sync as sync_service


def init(
    path: Annotated[
        Path | None,
        typer.Argument(help="Directory to initialise (defaults to the current directory)."),
    ] = None,
    api_key: Annotated[
        str | None, typer.Option("--api-key", help="API key to store during setup.")
    ] = None,
    local: Annotated[
        bool, typer.Option("--local", help="Local-only mode (no cloud account).")
    ] = False,
    agent: Annotated[
        list[str] | None,
        typer.Option("--agent", help="Agent to connect; repeat for several."),
    ] = None,
    no_onboard: Annotated[
        bool, typer.Option("--no-onboard", help="Skip project onboarding after setup.")
    ] = False,
    reinit: Annotated[
        bool, typer.Option("--reinit", help="Re-run setup even if already initialised.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Answer yes to every prompt.")] = False,
) -> None:
    """Set up aisquare on this machine and connect your agents."""
    lifecycle_service.initialize(
        path,
        api_key=api_key,
        local=local,
        agents=agent or [],
        onboard=not no_onboard,
        reinit=reinit,
        assume_yes=yes,
    )


def status() -> None:
    """Show installation health, the active project and connected agents."""
    diagnostics_service.status()


def doctor() -> None:
    """Run diagnostics and suggest fixes for common problems."""
    diagnostics_service.doctor()


def inject() -> None:
    """Inject relevant context into the current agent session."""
    context_service.inject()


def remember(
    text: Annotated[str, typer.Argument(help="The fact, preference or convention to remember.")],
    user: Annotated[bool, typer.Option("--user", help="Store in the user (global) pool.")] = False,
    project: Annotated[
        bool, typer.Option("--project", help="Store in the current project pool.")
    ] = False,
    tag: Annotated[
        list[str] | None,
        typer.Option("--tag", help="Tag for the entry; repeat for several."),
    ] = None,
) -> None:
    """Remember something across sessions (shorthand for `context add`)."""
    context_service.remember(text, pool=resolve_pool(user, project), tags=tag or [])


def sync() -> None:
    """Synchronise local context with the aisquare cloud."""
    sync_service.sync()


def why() -> None:
    """Explain what context was injected last, and why."""
    diagnostics_service.why()


def log() -> None:
    """Show recent capture and injection activity."""
    diagnostics_service.show_log()


def open_() -> None:
    """Open the aisquare home directory (or web dashboard)."""
    diagnostics_service.open_home()


def login() -> None:
    """Authenticate this machine with the aisquare cloud."""
    auth_service.login()


def logout() -> None:
    """Discard stored credentials."""
    auth_service.logout()


def whoami() -> None:
    """Show which account this machine is authenticated as."""
    auth_service.whoami()


def upgrade() -> None:
    """Upgrade aisquare and refresh agent hooks."""
    lifecycle_service.upgrade()


def uninstall() -> None:
    """Remove agent hooks and optionally local data."""
    lifecycle_service.uninstall()


def register(app: typer.Typer) -> None:
    """Attach the top-level commands to ``app`` in display order."""
    app.command("init")(init)
    app.command("status")(status)
    app.command("doctor")(doctor)
    app.command("inject")(inject)
    app.command("remember")(remember)
    app.command("sync")(sync)
    app.command("why")(why)
    app.command("log")(log)
    app.command("open")(open_)
    app.command("login")(login)
    app.command("logout")(logout)
    app.command("whoami")(whoami)
    app.command("upgrade")(upgrade)
    app.command("uninstall")(uninstall)
