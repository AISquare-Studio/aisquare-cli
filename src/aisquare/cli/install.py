"""``aisquare upgrade`` and ``uninstall``: the CLI replacing or removing itself, out loud.

Rendering only — the decisions live in ``services.lifecycle`` and
``services.install_route``. Both commands keep ``fleet shutdown``'s contract:
the plan, then a question at a terminal; off a terminal a dry run unless
``--yes``; under ``--json`` without ``--yes`` the plan as one object and nothing
changed.
"""

from __future__ import annotations

import json
import sys
from typing import Annotated, Any

import typer

from aisquare.cli.common import fail
from aisquare.core.console import stderr_console, stdout_console
from aisquare.core.state import get_state
from aisquare.services import install_route
from aisquare.services import lifecycle as lifecycle_service


def _stdin_is_a_terminal() -> bool:
    """Whether there is somebody to ask (indirection so tests can intercept).

    ``CliRunner`` replaces ``sys.stdin`` for an invocation, so the confirmation
    branch would otherwise be unreachable from a test — the reason
    ``cli/fleet.py`` has the same function.
    """
    return sys.stdin.isatty()


def _echo_json(payload: dict[str, Any]) -> None:
    typer.echo(json.dumps(payload, separators=(",", ":")))


def _say(line: str) -> None:
    stdout_console().print(line)


def _site_json(site: lifecycle_service.HookSite) -> dict[str, Any]:
    return {
        "config_dir": str(site.config_dir),
        "programs": list(site.programs),
        "reason": site.reason,
    }


def _plan_json(plan: lifecycle_service.UpgradePlan) -> dict[str, Any]:
    return {
        "current": plan.current,
        "target": plan.target or "latest",
        "latest": plan.latest_version,
        "latest_error": plan.latest.error if plan.latest is not None else None,
        "update_available": plan.update_available,
        "route": plan.route.kind,
        "install": plan.route.describe(),
        "runnable": plan.runnable,
        "reason": plan.reason,
        "command": plan.command,
        "argv": list(plan.argv),
        "refresh_hooks": [str(site.config_dir) for site in plan.refresh],
        "hooks_left": [_site_json(site) for site in plan.left],
    }


def _latest_line(plan: lifecycle_service.UpgradePlan) -> str:
    if plan.latest is None:
        return "latest: not asked"
    if plan.latest.version is None:
        return f"latest: unknown — {plan.latest.error}"
    available = plan.update_available
    if available is None:
        verdict = "cannot compare"
    else:
        verdict = "an update is available" if available else "you have it"
    return f"latest: {plan.latest.version} ({verdict})"


def _emit_check(plan: lifecycle_service.UpgradePlan) -> None:
    if get_state().json_output:
        _echo_json(_plan_json(plan))
        return
    _say(f"aisquare {plan.current} — {plan.route.describe()}")
    _say(_latest_line(plan))
    if plan.runnable:
        pin = f" --version {plan.target}" if plan.target else ""
        _say(f"upgrade with: aisquare upgrade{pin}")
    else:
        _say(f"upgrade with: {plan.command}")
        _say(f"(`aisquare upgrade` does not run it: {plan.reason})")


def _emit_plan(plan: lifecycle_service.UpgradePlan) -> None:
    if get_state().json_output:
        _echo_json({"dry_run": True, **_plan_json(plan)})
        return
    where = (
        f"{plan.destination} (latest on PyPI)"
        if plan.target is None and plan.latest_version is not None
        else plan.destination
    )
    _say(f"aisquare {plan.current} → {where}")
    if plan.target is None and plan.latest is not None and plan.latest.version is None:
        _say(f"  {plan.latest.error}; uv will install the newest release its index serves")
    _say(f"  install: {plan.route.describe()}")
    _say(f"  runs:    {plan.command}")
    for site in plan.refresh:
        _say(f"  then:    re-connects the Claude Code hooks in {site.config_dir}")
    for site in plan.left:
        _say(f"  leaves:  {site.config_dir} — {site.reason}")


def _emit_report(report: lifecycle_service.UpgradeReport) -> None:
    plan = report.plan
    if get_state().json_output:
        _echo_json(
            {
                "dry_run": False,
                "upgraded": report.upgraded,
                "previous": plan.current,
                "version": report.version,
                "latest": plan.latest_version,
                "route": plan.route.kind,
                "command": plan.command,
                "hooks": [
                    {"config_dir": str(hook.config_dir), "refreshed": hook.ok, "error": hook.error}
                    for hook in report.hooks
                ],
                "hooks_left": [_site_json(site) for site in plan.left],
                "notes": list(report.notes),
            }
        )
        return
    if report.version is not None and install_route.same_version(report.version, plan.current):
        # Only reachable when PyPI could not be asked: with an answer, an
        # unchanged version is a failure (lifecycle._verify).
        _say(f"✓ aisquare {report.version} is the newest release your package index serves")
    else:
        _say(f"✓ aisquare {report.version} (was {plan.current}) — checked in a new process")
    for hook in report.hooks:
        if hook.ok:
            _say(f"✓ hooks re-connected in {hook.config_dir}")
        else:
            remedy = install_route.command_line(
                ["aisquare", *lifecycle_service.REFRESH_HOOKS, "--config-dir", str(hook.config_dir)]
            )
            _say(
                f"✗ hooks in {hook.config_dir} were not re-connected ({hook.error}) — run: {remedy}"
            )
    for site in plan.left:
        _say(f"· {site.config_dir} left as it is: {site.reason}")
    for note in report.notes:
        _say(f"· {note}")


def _fallback(plan: lifecycle_service.UpgradePlan) -> str:
    return (
        f"Run it again by hand: {plan.command} — or reinstall from nothing: "
        f"{install_route.INSTALLER_ONE_LINER}"
    )


def upgrade(
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Only report this version, the latest one and how this install upgrades.",
        ),
    ] = False,
    version: Annotated[
        str | None,
        typer.Option(
            "--version",
            metavar="V",
            help="Install version V instead of the latest (an older V moves back to it).",
        ),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Upgrade without asking; required off a terminal.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would run, and run nothing.")
    ] = False,
) -> None:
    """Upgrade aisquare in place, then re-connect its Claude Code hooks.

    Runs only for a uv tool install, which is what the one-line installer makes:
    it reinstalls with `uv tool install --force`, keeping the extras, the
    `--with` packages and the Python the install already has, checks the new
    version in a new process, and re-connects the hooks in every Claude Code
    directory this machine connected. Any other install is told the exact
    command that upgrades it, and exits 1. Asks first at a terminal; off a
    terminal it is a dry run unless --yes.
    """
    json_output = get_state().json_output
    try:
        plan = lifecycle_service.upgrade_plan(version, check=check)
    except lifecycle_service.InvalidVersion as exc:
        fail(
            f"--version takes a version such as 0.9.1, not {str(exc)!r}",
            error="invalid_version",
        )
    if check:
        _emit_check(plan)
        return
    if not plan.runnable:
        # The command goes in the MESSAGE: `fail` shows a human nothing else.
        fail(
            f"aisquare does not upgrade this install itself ({plan.reason}). "
            f"Upgrade it with: {plan.command}",
            error="upgrade_unsupported_route",
            hint=plan.command,
            detail=plan.route.describe(),
        )
    if plan.up_to_date:
        if json_output:
            _echo_json(
                {"dry_run": False, "upgraded": False, "up_to_date": True, **_plan_json(plan)}
            )
        elif plan.target is not None:
            _say(f"aisquare {plan.current} is already the version asked for — nothing to do")
        else:
            _say(
                f"aisquare {plan.current} is up to date (PyPI's latest is "
                f"{plan.latest_version}) — nothing to do"
            )
        return
    if dry_run or not yes:
        _emit_plan(plan)
        if dry_run or json_output:
            return
        if not _stdin_is_a_terminal():
            _say("dry run: nothing installed — re-run with --yes to upgrade")
            return
        if not typer.confirm(
            f"Upgrade aisquare {plan.current} → {plan.destination}?", default=False
        ):
            _say("nothing changed")
            return
    elif not json_output:
        _say(f"upgrading aisquare {plan.current} → {plan.destination}: {plan.command}")
    report = lifecycle_service.upgrade(plan, to_stderr=json_output)
    if not report.installed:
        fail(
            f"the upgrade failed: {report.problem} (its output is above). {_fallback(plan)}",
            error="upgrade_failed",
            hint=plan.command,
            detail=report.problem,
        )
    if not report.upgraded:
        fail(
            f"the upgrade could not be confirmed: {report.problem}. {_fallback(plan)}",
            error="upgrade_not_confirmed",
            hint=plan.command,
            detail=report.problem,
        )
    _emit_report(report)
    if any(not hook.ok for hook in report.hooks):
        raise typer.Exit(1)


# --- uninstall ---------------------------------------------------------------------------


def _uninstall_plan_json(plan: lifecycle_service.UninstallPlan) -> dict[str, Any]:
    refusal = plan.refusal
    return {
        "hooks": [
            {"config_dir": str(site.config_dir), "programs": list(site.programs)}
            for site in plan.hooks
        ],
        "unreadable": [
            {"config_dir": str(site.config_dir), "reason": site.reason} for site in plan.unreadable
        ],
        "mcp": [
            {"name": entry.name, "file": str(entry.file), "project": entry.project}
            for entry in plan.mcp
        ],
        "package": {
            "route": plan.route.kind,
            "command": plan.package_command,
            "argv": list(plan.package_argv),
            "runs": plan.package_reason is None,
            "reason": plan.package_reason,
        },
        "home": {
            "path": str(plan.home),
            "exists": plan.home_exists,
            "entries": len(plan.home_entries),
            "accounts": list(plan.accounts),
            "action": "delete" if plan.purge else "keep",
        },
        "purge_refusal": plan.purge_refusal,
        "live_agents": list(plan.live_agents),
        "fleet_error": plan.fleet_error,
        "refusal": None if refusal is None else {"error": refusal.error, "message": str(refusal)},
    }


def _home_line(plan: lifecycle_service.UninstallPlan) -> str:
    count = len(plan.home_entries)
    what = f"{count} entr{'ies' if count != 1 else 'y'}: your memory, boards and settings"
    if plan.accounts:
        what += f", and the Claude Code logins in {'; '.join(plan.accounts)}"
    return f"{plan.home} — {what}"


def _emit_uninstall_plan(plan: lifecycle_service.UninstallPlan) -> None:
    if get_state().json_output:
        _echo_json({"dry_run": True, **_uninstall_plan_json(plan)})
        return
    _say("aisquare uninstall would:")
    if plan.hooks:
        _say("  remove aisquare's hooks from:")
        for site in plan.hooks:
            runs = f" (they run {', '.join(site.programs)})" if site.programs else ""
            _say(f"    {site.config_dir}{runs}")
    else:
        _say("  find no aisquare hooks in any Claude Code directory")
    for site in plan.unreadable:
        _say(f"  ⚠ could not check {site.config_dir}: {site.reason}")
    if plan.purge and plan.home_exists:
        _say(f"  DELETE {_home_line(plan)}")
    if plan.package_reason is None:
        _say(f"  then remove the package: {plan.package_command}")
    else:
        _say(f"  then tell you to remove the package yourself: {plan.package_command}")
        _say(f"    (aisquare does not run it: {plan.package_reason})")
    _say("and keep:")
    if plan.home_exists and not plan.purge:
        _say(f"  {_home_line(plan)} (delete it too with --purge)")
    if plan.mcp:
        _say("  MCP servers that run aisquare (Claude Code owns .claude.json; remove each with")
        _say("  `claude mcp remove <name>`):")
        for entry in plan.mcp:
            where = f"{entry.file}" + (f", project {entry.project}" if entry.project else "")
            _say(f"    {entry.name} in {where}")
    _say("  uv, tmux, Node, gh and Claude Code")
    if plan.fleet_error is not None:
        _say(f"⚠ the fleet's agents could not be counted ({plan.fleet_error}); make sure none run")
    elif plan.live_agents and not plan.tmux_found:
        _say(
            "· the board lists live fleet agents, but tmux is not installed, so none can be running"
        )
    refusal = plan.refusal
    if refusal is not None:
        _say(f"✗ it will not start: {refusal}")


def _emit_uninstall_report(report: lifecycle_service.UninstallReport) -> None:
    plan = report.plan
    if get_state().json_output:
        _echo_json(
            {
                "dry_run": False,
                "hooks": [
                    {"config_dir": str(hook.config_dir), "removed": hook.ok, "error": hook.error}
                    for hook in report.hooks
                ],
                "home": {
                    "path": str(plan.home),
                    "deleted": report.purged,
                    "error": report.purge_error,
                },
                "package": {
                    "command": plan.package_command,
                    "runs": report.package_runs,
                    "reason": plan.package_reason,
                },
                "notes": list(report.notes),
            }
        )
        return
    for hook in report.hooks:
        if hook.ok:
            _say(f"✓ hooks removed from {hook.config_dir}")
        else:
            _say(f"✗ hooks in {hook.config_dir} were not removed: {hook.error}")
    if report.purged:
        _say(f"✓ deleted {plan.home}")
    elif report.purge_error is not None:
        _say(f"✗ {plan.home} was not deleted: {report.purge_error}")
    elif plan.home_exists:
        _say(f"· kept {plan.home} — delete it by hand if you do not want it")
    for note in report.notes:
        _say(f"· {note}")
    if report.failed:
        _say(
            "✗ the package was NOT removed, so `aisquare uninstall` can be run again once "
            "the problems above are fixed"
        )
    elif plan.package_reason is not None:
        _say(f"to finish, remove the package: {plan.package_command}")
    else:
        _say(f"removing the package: {plan.package_command}")


def _uninstall_question(plan: lifecycle_service.UninstallPlan) -> str | None:
    """The y/N question, naming every step that will happen — ``None`` when none will."""
    steps: list[str] = []
    if plan.hooks:
        count = len(plan.hooks)
        steps.append(f"remove aisquare's hooks from {count} director{'ies' if count != 1 else 'y'}")
    if plan.purge and plan.home_exists:
        steps.append(f"DELETE {plan.home}")
    if plan.package_reason is None:
        steps.append("remove the package")
    if not steps:
        return None
    text = steps[0] if len(steps) == 1 else ", ".join(steps[:-1]) + " and " + steps[-1]
    return text[0].upper() + text[1:] + "?"


def uninstall(
    purge: Annotated[
        bool,
        typer.Option(
            "--purge",
            help="Also delete the aisquare home (~/.aisquare): memory, boards, settings and "
            "the Claude Code logins in its account slots.",
        ),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Uninstall without asking; required off a terminal.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would be removed, and remove nothing.")
    ] = False,
) -> None:
    """Remove aisquare: its Claude Code hooks, then the package. Keeps ~/.aisquare.

    Takes aisquare's hooks out of every Claude Code directory it finds (the ones
    this machine connected, $CLAUDE_CONFIG_DIR, ~/.claude* and the account
    slots), leaving every other hook as it was. Refuses while fleet agents are
    running. ~/.aisquare is kept unless --purge. The package goes last: a uv
    tool install is removed with `uv tool uninstall`; any other install is told
    its command. Asks first at a terminal; off a terminal it is a dry run unless
    --yes.
    """
    json_output = get_state().json_output
    plan = lifecycle_service.uninstall_plan(purge=purge)
    refusal = plan.refusal
    if dry_run or not yes:
        _emit_uninstall_plan(plan)
        if dry_run or json_output:
            return
        if not _stdin_is_a_terminal():
            _say("dry run: nothing removed — re-run with --yes to uninstall")
            return
        if refusal is not None:
            raise typer.Exit(1)  # the plan just said why; asking would offer a refused yes
        question = _uninstall_question(plan)
        if question is None:
            _say(
                f"nothing for aisquare to remove here — remove the package: {plan.package_command}"
            )
            return
        if not typer.confirm(question, default=False):
            _say("nothing removed")
            return
    if refusal is not None:
        fail(str(refusal), error=refusal.error)
    report = lifecycle_service.uninstall(plan)
    _emit_uninstall_report(report)
    if report.failed:
        raise typer.Exit(1)
    if report.package_runs:
        try:
            lifecycle_service.remove_package(plan, stdout_to_stderr=json_output)
        except OSError as exc:
            # Only reached when the package manager could not be started at all;
            # under --json the report is already out, so this goes to stderr.
            stderr_console().print(
                f"✗ could not start {plan.package_argv[0]} ({exc}) — remove the package "
                f"yourself: {plan.package_command}"
            )
            raise typer.Exit(1) from exc
