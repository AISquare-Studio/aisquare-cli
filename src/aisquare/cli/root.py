"""Top-level commands: setup, memory shortcuts, diagnostics and account."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from aisquare.cli.common import (
    emit_block,
    emit_doctor,
    emit_entry,
    emit_injection_record,
    emit_prompts,
    emit_setup,
    emit_status,
    expected_config_write_errors,
    fail,
    resolve_pool,
)
from aisquare.models import CheckStatus
from aisquare.services import auth as auth_service
from aisquare.services import context as context_service
from aisquare.services import diagnostics as diagnostics_service
from aisquare.services import explainability as explainability_service
from aisquare.services import explainability_ops
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
        bool,
        typer.Option(
            "--reinit",
            help="Re-run setup, resetting config.toml to defaults. Discards role "
            "bindings made with team bind.",
        ),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Answer yes to every prompt.")] = False,
    explainability: Annotated[
        bool | None,
        typer.Option(
            "--explainability/--no-explainability",
            help="Ship this CLI's insights (prompts, board events) to the "
            "explainability gateway. Off unless you ask for it.",
        ),
    ] = None,
) -> None:
    """Set up aisquare on this machine and connect your agents."""
    try:
        with expected_config_write_errors():
            report = lifecycle_service.initialize(
                path,
                api_key=api_key,
                local=local,
                agents=agent or [],
                onboard=not no_onboard,
                reinit=reinit,
                assume_yes=yes,
                explainability=_explainability_decision(explainability),
            )
    except lifecycle_service.ExplainabilityResetRefused as refused:
        fail(
            f"--reinit would discard this machine's explainability configuration "
            f"({refused.summary}) and nothing afterwards would report it missing. "
            f"Re-run with --yes if that is what you mean.",
            error="reinit_would_discard_explainability",
            hint="aisquare init --reinit --yes",
        )
    emit_setup(report)


def _explainability_decision(flag: bool | None) -> bool | None:
    """What the user said about shipping insights — asking only where we may.

    Ordinary ``init`` is non-interactive and idempotent, and scripts and CI
    depend on that, so the question is asked ONLY at a terminal, and only when
    the step could actually be accepted. Anywhere else the answer is "not
    asked", which changes nothing.

    ``--yes`` deliberately does NOT imply consent here. It exists to unblock
    prompts, and "ship my prompts to a server" is not a prompt anyone should
    answer by reflex — #50's boundary is that nothing ships before the user has
    configured it.
    """
    if flag is not None:
        return flag
    if not sys.stdin.isatty():
        return None
    try:
        if not explainability_service.shipping_offer().available:
            return None
    except Exception:  # an optional step may not break setup
        return None
    captures = explainability_service.ShippingOffer.CAPTURES
    return typer.confirm(f"Ship {captures} to the explainability gateway?", default=False)


def status() -> None:
    """Show installation health, the active project and connected agents."""
    emit_status(diagnostics_service.status())


def doctor(
    live: Annotated[
        bool,
        typer.Option(
            "--live",
            help="Also run the checks that leave this machine (explainability "
            "gateway reachability, key acceptance, a real span round-trip).",
        ),
    ] = False,
    fix: Annotated[
        bool,
        typer.Option("--fix", help="Repair what can be repaired, then re-check."),
    ] = False,
    target: Annotated[
        str | None,
        typer.Option("--target", help="Explainability deployment to check, e.g. stg or prod."),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Answer yes to every --fix prompt.")
    ] = False,
) -> None:
    """Run diagnostics and suggest fixes for common problems."""
    if fix:
        for action in explainability_ops.apply_fixes(
            target=target, assume_yes=yes, confirm=typer.confirm
        ):
            typer.echo(f"fix: {action}")
    checks = diagnostics_service.doctor(live=live, target=target)
    emit_doctor(checks)
    if any(check.status is CheckStatus.fail for check in checks):
        raise typer.Exit(1)


def inject() -> None:
    """Inject relevant context into the current agent session."""
    emit_block(context_service.inject())


def remember(
    text: Annotated[str, typer.Argument(help="The fact, preference or convention to remember.")],
    user: Annotated[bool, typer.Option("--user", help="Store in the user (global) pool.")] = False,
    project: Annotated[
        bool, typer.Option("--project", help="Store in the current project pool.")
    ] = False,
    stream: Annotated[
        str | None,
        typer.Option("--stream", help="Store in a named stream's pool.", metavar="NAME"),
    ] = None,
    tag: Annotated[
        list[str] | None,
        typer.Option("--tag", help="Tag for the entry; repeat for several."),
    ] = None,
) -> None:
    """Remember something across sessions (shorthand for `context add`)."""
    if stream is not None and (user or project):
        raise typer.BadParameter("--stream cannot be combined with --user/--project.")
    try:
        entry = context_service.remember(
            text, pool=resolve_pool(user, project), tags=tag or [], stream=stream
        )
    except context_service.HomeProjectRefused as exc:
        fail(str(exc), error="home_is_not_a_project", ref=str(exc.root))
    except LookupError as exc:
        fail(str(exc), error="unknown_stream", ref=stream or "")
    emit_entry(entry, verb="remembered")


def sync() -> None:
    """Synchronise local context with the aisquare cloud."""
    sync_service.sync()


def why() -> None:
    """Explain what context was injected last, and why."""
    emit_injection_record(diagnostics_service.last_injection())


def log() -> None:
    """Show recently captured user prompts for the active project."""
    emit_prompts(diagnostics_service.show_log())


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
    """Attach the top-level commands to ``app`` in display order.

    Roadmap commands are registered ``hidden=True``: they still run (and still
    report the not-implemented contract) but stay out of ``--help``, so the
    listed surface is only what actually works. Un-hide one as it graduates.
    """
    app.command("init")(init)
    app.command("status")(status)
    app.command("doctor")(doctor)
    app.command("inject")(inject)
    app.command("remember")(remember)
    app.command("sync", hidden=True)(sync)
    app.command("why")(why)
    app.command("log")(log)
    app.command("open", hidden=True)(open_)
    app.command("login", hidden=True)(login)
    app.command("logout", hidden=True)(logout)
    app.command("whoami", hidden=True)(whoami)
    app.command("upgrade", hidden=True)(upgrade)
    app.command("uninstall", hidden=True)(uninstall)
