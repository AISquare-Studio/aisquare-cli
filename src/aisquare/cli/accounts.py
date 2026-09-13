"""``aisquare accounts`` — the Claude Code accounts this CLI manages.

Thin over :mod:`aisquare.services.claude_accounts`: parse, call, render. The
commands that report (``list``, ``usage``, ``remove``) honour ``--json``; the
two that hand the terminal to Claude Code (``add``, ``run``) refuse it, because
from that moment stdout is Claude Code's and no single JSON object could be
promised on it.

``run`` is what the c1/c2/c3 shell aliases were: ``aisquare accounts run 2``
replaces this process with ``claude`` on slot 2, extra arguments forwarded.
The fleet UI's sign-in window runs exactly this command, so the environment an
account launches with is decided in one place (``services.claude_accounts.
session_env``).
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Annotated, Any, NoReturn

import typer
from rich.table import Table
from rich.text import Text

from aisquare.cli.common import expected_config_write_errors, fail, format_reset
from aisquare.cli.fleet import not_interactive_reason
from aisquare.core import claude_accounts as core
from aisquare.core.console import stderr_console, stdout_console
from aisquare.core.state import get_state
from aisquare.models import (
    AccountsOverview,
    ClaudeAccount,
    ClaudeAccountStatus,
    ClaudeUsage,
    ProjectInfo,
)
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import fleet as fleet_service
from aisquare.services import settings as settings_service

app = typer.Typer(
    help="Claude Code accounts this CLI manages: list, add, remove, run, usage, "
    "default, alias, order, move, disable, enable.",
    no_args_is_help=False,
    invoke_without_command=True,
)

SlotRef = Annotated[
    str, typer.Argument(help="Slot number, alias, or the email the slot is signed in as.")
]
ProjectOpt = Annotated[
    str | None,
    typer.Option(
        "--project",
        "-P",
        help="Act on a PROJECT's default instead of the machine's: a codename, name or id "
        "prefix, or `.` for the project of the current directory.",
        metavar="PROJECT",
    ),
]
RoleOpt = Annotated[
    str | None,
    typer.Option(
        "--role",
        help="Act on a ROLE's binding instead of the machine default (what "
        "`aisquare team bind <role> --account` writes).",
        metavar="ROLE",
    ),
]

_DASH = "—"
_DEFAULT_MARK = "*"


@app.callback(invoke_without_command=True)
def _group(ctx: typer.Context) -> None:
    """Claude Code accounts this CLI manages: list, add, remove, run, usage.

    Bare ``aisquare accounts`` lists them, offline. Slot 1 is the plain
    ``claude`` of this machine; every other slot is a directory the CLI created
    with ``add`` and launches with ``run`` (or ``aisquare launch --account``).
    """
    if ctx.invoked_subcommand is None:
        list_(usage=False)


def _resolve(ref: str) -> ClaudeAccount:
    try:
        return accounts_service.resolve(ref)
    except accounts_service.NoSuchAccount as exc:
        fail(str(exc), error="unknown_account", ref=ref)


def _fail_not_installed(exc: Exception) -> NoReturn:
    fail(str(exc), error="claude_not_installed", hint=core.INSTALL_COMMAND)


def _percent(value: float | None) -> str:
    return _DASH if value is None else f"{value:.0f}%"


def _usage_cells(usage: ClaudeUsage | None, *, now: datetime | None = None) -> tuple[str, str]:
    """``(session, week)`` as the table shows them; the reason when there is nothing to show.

    The reset is ``format_reset``'s string — a distance and a clock time, the
    date when it is not today — shared with the Accounts page so the two
    cannot drift again (#152). ``now`` is for tests; the table reads the clock.
    """
    if usage is None:
        return _DASH, _DASH
    if not usage.available:
        return usage.reason or "unavailable", ""
    session = _percent(usage.session_percent)
    if usage.session_resets_at is not None:
        session += f" · resets {format_reset(usage.session_resets_at, now=now)}"
    week = _percent(usage.week_percent)
    if usage.week_resets_at is not None:
        week += f" · resets {format_reset(usage.week_resets_at, now=now)}"
    return session, week


def _short(path: object) -> str:
    text = str(path)
    home = str(core._home())
    return "~" + text[len(home) :] if home and text.startswith(home) else text


def _overview_json(overview: AccountsOverview) -> str:
    return json.dumps(overview.model_dump(mode="json"))


def _emit_overview(overview: AccountsOverview, *, with_usage: bool) -> None:
    console = stdout_console()
    claude = overview.claude
    if not claude.installed:
        console.print(
            Text.assemble(
                ("✗ Claude Code is not installed", "bold red"),
                f" — install it: {core.INSTALL_COMMAND}  (or: {core.INSTALL_ALTERNATIVE})",
            )
        )
    else:
        version = f" {claude.version}" if claude.version else ""
        console.print(Text.assemble(("✓ ", "green"), f"Claude Code{version} · {claude.binary}"))
    table = Table(box=None, pad_edge=False, show_edge=False, header_style="bold")
    # Rows arrive in PRIORITY order (services.claude_accounts.list_accounts), so
    # the table reads top-down as "the order the fleet would try them"; the
    # default is starred the way `project list` stars the active project.
    for column in (" ", "slot", "label", "signed in as", "plan", "hooks"):
        table.add_column(column)
    if with_usage:
        table.add_column("session")
        table.add_column("week")
    table.add_column("directory", overflow="fold")
    for status in overview.accounts:
        who = Text(status.identity.email) if status.identity else Text("not signed in", style="dim")
        if status.identity and not status.signed_in:
            who = Text(f"{status.identity.email} (token missing)", style="yellow")
        label = Text(status.label)
        if status.account.disabled:
            label.append(" (disabled)", style="dim")
        cells: list[Text | str] = [
            Text(_DEFAULT_MARK, style="bold green") if status.account.is_default else "",
            str(status.account.slot),
            label,
            who,
            status.subscription or _DASH,
            Text("✓", style="green") if status.hooks_installed else Text("✗", style="red"),
        ]
        if with_usage:
            cells.extend(_usage_cells(status.usage))
        cells.append(Text(_short(status.account.config_dir), style="dim"))
        table.add_row(*cells)
    console.print(table)
    default = next((s for s in overview.accounts if s.account.is_default), None)
    if default is None:
        console.print(
            Text(
                "no default account — launches run on whatever claude the shell has; "
                "pick one: aisquare accounts default <slot|alias|email>",
                style="dim",
            )
        )
    console.print(
        Text(
            "add one: aisquare accounts add · open one: aisquare accounts run <slot> · "
            "the Accounts page in asq does both",
            style="dim",
        )
    )


@app.command("list")
def list_(
    usage: Annotated[
        bool,
        typer.Option(
            "--usage",
            help="Also fetch each signed-in account's session and weekly usage (one request "
            "per account).",
        ),
    ] = False,
) -> None:
    """List the accounts: slot, who is signed in, plan, hooks; --usage adds the limits."""
    overview = accounts_service.overview()
    if usage:
        for status in overview.accounts:
            if status.signed_in:
                # `sample_usage`, not `usage`: every reading feeds the trend (#146).
                status.usage = accounts_service.sample_usage(status.account)
    if get_state().json_output:
        typer.echo(_overview_json(overview))
        return
    _emit_overview(overview, with_usage=usage)


@app.command("usage")
def usage_(
    slot: Annotated[
        str | None,
        typer.Argument(help="Slot number or email (default: every signed-in account)."),
    ] = None,
) -> None:
    """Session (5-hour) and weekly usage per signed-in account, as Claude Code's /usage shows it."""
    accounts = [_resolve(slot)] if slot is not None else core.list_accounts()
    statuses: list[ClaudeAccountStatus] = []
    for account in accounts:
        status = accounts_service.describe(account)
        if status.signed_in or slot is not None:
            status.usage = accounts_service.sample_usage(account)
        statuses.append(status)
    if get_state().json_output:
        typer.echo(json.dumps([status.model_dump(mode="json") for status in statuses]))
        return
    console = stdout_console()
    table = Table(box=None, pad_edge=False, show_edge=False, header_style="bold")
    for column in ("slot", "signed in as", "session", "week"):
        table.add_column(column)
    for status in statuses:
        who = Text(status.identity.email) if status.identity else Text("not signed in", style="dim")
        session, week = _usage_cells(status.usage)
        table.add_row(str(status.account.slot), who, session, week)
    console.print(table)


# --- arranging: default, alias, order, disable (#145) -----------------------------------------
#
# Every command here is thin over one service call and renders the result; the
# ladder a launch walks — flag, role binding, project default, machine default —
# lives in ``services.claude_accounts.choose`` and nowhere in this file.


def _account_json(account: ClaudeAccount | None) -> dict[str, Any] | None:
    if account is None:
        return None
    payload = account.model_dump(mode="json")
    payload["label"] = core.label(account)
    identity = core.identity(account)
    payload["email"] = identity.email if identity else None
    return payload


def _who(account: ClaudeAccount) -> str:
    identity = core.identity(account)
    email = f" ({identity.email})" if identity else ""
    return f"slot {account.slot} · {core.label(account)}{email}"


def _project_for(ref: str) -> ProjectInfo:
    """``.`` is the project of the current directory; anything else is a fleet project reference."""
    try:
        return fleet_service.resolve_project(None if ref == "." else ref)
    except fleet_service.FleetError as exc:
        fail(str(exc), error="no_such_project", ref=ref)


def _arrangement_failed(exc: Exception) -> NoReturn:
    code = "unknown_account" if isinstance(exc, accounts_service.NoSuchAccount) else "accounts"
    if isinstance(exc, accounts_service.AccountsUnreadable):
        code = "store_unreadable"
    fail(str(exc), error=code)


def _show_defaults(project: ProjectInfo | None) -> None:
    """The ladder as it stands: machine default, a project's default, every role binding."""
    machine = accounts_service.machine_default()
    per_project = accounts_service.project_default(project) if project is not None else None
    try:
        bindings = settings_service.role_account_bindings()
    except Exception as exc:  # a broken config.toml: say so, show the rest
        bindings = {}
        stderr_console().print(f"role bindings unreadable ({exc})", style="dim")
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "machine_default": _account_json(machine),
                    "project": project.id if project is not None else None,
                    "project_default": _account_json(per_project),
                    "role_bindings": bindings,
                }
            )
        )
        return
    console = stdout_console()
    if machine is None:
        console.print(
            "machine default: none — launches run on whatever claude the shell has "
            "(set one: aisquare accounts default <slot|alias|email>)",
            markup=False,
        )
    else:
        console.print(f"machine default: {_who(machine)}", markup=False)
    if project is not None:
        name = project.root.name or project.id
        if per_project is None:
            console.print(f"project {name}: no default of its own", markup=False)
        else:
            console.print(f"project {name}: {_who(per_project)}", markup=False)
    if bindings:
        for role in sorted(bindings):
            console.print(f"role {role}: account {bindings[role]}", markup=False)
    console.print(
        Text(
            "a launch picks, in order: --account · the role's binding · the project default · "
            "the machine default · whatever the shell has",
            style="dim",
        )
    )


@app.command("default")
def default(
    ref: Annotated[
        str | None,
        typer.Argument(
            help="Slot number, alias or email to make the default. Omit to show the defaults."
        ),
    ] = None,
    project: ProjectOpt = None,
    role: RoleOpt = None,
    clear: Annotated[
        bool, typer.Option("--clear", help="Remove the default at the chosen level.")
    ] = False,
) -> None:
    """Choose the account a launch runs under — for the machine, a project, or a role.

    Without a reference it prints the defaults as they stand. With one it sets
    the MACHINE default, or a PROJECT's (``--project``), or a ROLE's binding
    (``--role``, the same thing ``team bind <role> --account`` writes). A launch
    resolves them top-down: ``--account`` on the command line, then the role's
    binding, then the project's default, then the machine's; with none of those
    it runs on whatever ``claude`` the shell already has, exactly as before.
    """
    if project is not None and role is not None:
        fail("--project and --role are mutually exclusive", error="usage")
    target = _project_for(project) if project is not None else None
    if ref is None and not clear:
        _show_defaults(
            target if target is not None else _project_for(".") if _has_project() else None
        )
        return
    if role is not None:
        _set_role_default(role, ref, clear=clear)
        return
    try:
        chosen = accounts_service.set_default(None if clear else ref, project=target)
    except accounts_service.AccountsError as exc:
        _arrangement_failed(exc)
    level = f"project {target.root.name or target.id}" if target is not None else "machine"
    if get_state().json_output:
        typer.echo(json.dumps({"level": level, "default": _account_json(chosen)}))
        return
    if chosen is None:
        stdout_console().print(f"✓ {level} default cleared", markup=False)
    else:
        stdout_console().print(f"✓ {level} default: {_who(chosen)}", markup=False)


def _has_project() -> bool:
    """Whether the current directory resolves to a registered project (never raises)."""
    try:
        fleet_service.resolve_project(None)
    except Exception:
        return False
    return True


def _set_role_default(role: str, ref: str | None, *, clear: bool) -> None:
    if not clear:
        if ref is None:
            fail("pass an account to bind, or --clear", error="usage")
        try:
            accounts_service.resolve(ref)  # a bad name is a usage error, not a stored binding
        except accounts_service.AccountsError as exc:
            _arrangement_failed(exc)
    with expected_config_write_errors():
        bound = settings_service.bind_role(role, account=ref, clear_account=clear)
    if get_state().json_output:
        typer.echo(json.dumps({"level": f"role {role}", "default": bound.account}))
        return
    if clear:
        stdout_console().print(f"✓ role {role}: account binding cleared", markup=False)
    else:
        stdout_console().print(f"✓ role {role} launches on account {bound.account}", markup=False)


@app.command("alias")
def alias(
    ref: SlotRef,
    name: Annotated[
        str | None, typer.Argument(help="The name: a letter, then up to 31 of a-z 0-9 . _ -")
    ] = None,
    clear: Annotated[bool, typer.Option("--clear", help="Remove the slot's alias.")] = False,
) -> None:
    """Name an account (`work`, `personal`) so --account and the board can say it."""
    if name is None and not clear:
        fail("pass a name, or --clear", error="usage")
    try:
        account = accounts_service.set_alias(ref, None if clear else name)
    except ValueError as exc:
        fail(str(exc), error="bad_alias")
    except accounts_service.AccountsError as exc:
        _arrangement_failed(exc)
    if get_state().json_output:
        typer.echo(json.dumps({"account": _account_json(account)}))
        return
    if clear:
        stdout_console().print(f"✓ slot {account.slot}: alias cleared", markup=False)
    else:
        stdout_console().print(f"✓ slot {account.slot} is now called {account.alias}", markup=False)


def _emit_order(accounts: list[ClaudeAccount]) -> None:
    if get_state().json_output:
        typer.echo(json.dumps({"order": [_account_json(account) for account in accounts]}))
        return
    console = stdout_console()
    for account in accounts:
        mark = _DEFAULT_MARK if account.is_default else " "
        off = "  (disabled)" if account.disabled else ""
        console.print(f"{mark} {account.position}. {_who(account)}{off}", markup=False)


@app.command("order")
def order(
    refs: Annotated[
        list[str],
        typer.Argument(help="Accounts first to last (slot, alias or email); the rest follow."),
    ],
) -> None:
    """Set the priority order — what `fleet spawn` tries first when it picks by headroom."""
    try:
        arranged = accounts_service.reorder(refs)
    except accounts_service.AccountsError as exc:
        _arrangement_failed(exc)
    _emit_order(arranged)


@app.command("move")
def move(
    ref: SlotRef,
    direction: Annotated[str, typer.Argument(help="up, down, top or bottom")],
) -> None:
    """Move one account a step in the priority order."""
    if direction not in ("up", "down", "top", "bottom"):
        fail(f"direction must be up, down, top or bottom, not {direction!r}", error="usage")
    try:
        arranged = accounts_service.move(ref, direction)  # type: ignore[arg-type]
    except accounts_service.AccountsError as exc:
        _arrangement_failed(exc)
    _emit_order(arranged)


def _toggle(ref: str, *, disabled: bool) -> None:
    try:
        account = accounts_service.set_disabled(ref, disabled)
    except accounts_service.AccountsError as exc:
        _arrangement_failed(exc)
    if get_state().json_output:
        typer.echo(json.dumps({"account": _account_json(account)}))
        return
    if disabled:
        stdout_console().print(
            f"✓ {_who(account)} is disabled — never picked automatically; "
            "still usable with --account",
            markup=False,
        )
    else:
        stdout_console().print(f"✓ {_who(account)} is enabled", markup=False)


@app.command("disable")
def disable(ref: SlotRef) -> None:
    """Keep an account out of automatic selection; naming it with --account still works."""
    _toggle(ref, disabled=True)


@app.command("enable")
def enable(ref: SlotRef) -> None:
    """Put an account back into automatic selection."""
    _toggle(ref, disabled=False)


@app.command("add")
def add() -> None:
    """Add a Claude Code account: a fresh slot, signed in through Claude Code's own login."""
    if get_state().json_output:
        fail(
            "accounts add hands the terminal to Claude Code and has no --json form — "
            "list accounts with `aisquare --json accounts list`",
            error="no_json_form",
        )
    reason = not_interactive_reason()
    if reason is not None:
        fail(
            f"accounts add needs an interactive terminal ({reason}) — run it in a terminal, "
            "or add the account from asq → Accounts",
            error="not_a_tty",
            detail=reason,
        )
    try:
        account = accounts_service.begin_sign_in(None)
    except accounts_service.ClaudeNotInstalled as exc:
        _fail_not_installed(exc)
    console = stderr_console()
    console.print(
        f"Slot {account.slot} is ready. Claude Code opens now with its own login — sign in "
        "there, then leave it (/exit) and this command records the account."
    )
    try:
        status = accounts_service.run_session(account)
    except accounts_service.ClaudeNotInstalled as exc:
        accounts_service.abandon_sign_in(account)
        _fail_not_installed(exc)
    except KeyboardInterrupt:
        # The session guards itself against Ctrl-C; this is the belt to its
        # braces (Windows, a signal that arrived between the two calls): a slot
        # nothing signed into must not outlive the attempt.
        accounts_service.abandon_sign_in(account)
        fail("Sign-in cancelled. Nothing was added.", error="cancelled", exit_code=130)
    landed = accounts_service.sign_in_landed(account)
    if landed is None:
        accounts_service.abandon_sign_in(account)
        how = f"status {status}" if status >= 0 else f"signal {-status}"
        fail(
            f"no sign-in landed in slot {account.slot} (Claude Code exited with {how}) "
            "— nothing was added",
            error="not_signed_in",
        )
    described = accounts_service.complete_sign_in(account)
    console.print(
        Text.assemble(("✓ ", "green"), f"Added Claude account {account.slot}: {landed.email}")
    )
    if not described.hooks_installed:
        console.print(
            "  ⚠ aisquare's hooks did not install into it — "
            f"run: aisquare agents connect claude-code --config-dir {account.config_dir}",
            style="yellow",
        )
    console.print(
        f"  open it: aisquare accounts run {account.slot} · "
        f"as a role: aisquare launch coder --account {account.slot}",
        style="dim",
    )


@app.command("remove")
def remove(slot: SlotRef) -> None:
    """Forget a managed account: its directory is kept beside itself as <n>.removed-<stamp>."""
    account = _resolve(slot)
    identity = core.identity(account)
    try:
        moved = accounts_service.remove(account)
    except accounts_service.AccountsError as exc:
        fail(str(exc), error="not_removable", ref=slot)
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "removed": account.slot,
                    "email": identity.email if identity else None,
                    "moved_to": str(moved),
                }
            )
        )
        return
    who = f" ({identity.email})" if identity else ""
    stdout_console().print(
        Text.assemble(
            ("✓ ", "green"),
            f"removed account {account.slot}{who} — its directory is kept at {moved}; "
            "delete it when you are sure",
        )
    )


def _exec(binary: str, argv: list[str], env: dict[str, str]) -> None:
    """Replace this process with Claude Code (indirection so tests can intercept)."""
    os.execve(binary, argv, env)


def run(ctx: typer.Context, slot: SlotRef) -> None:
    """Open Claude Code on one account — what a c1/c2 alias did. Extra arguments go to claude."""
    if get_state().json_output:
        fail(
            "accounts run hands the terminal to Claude Code and has no --json form",
            error="no_json_form",
        )
    account = _resolve(slot)
    found = accounts_service.install()
    if not found.installed or found.binary is None:
        _fail_not_installed(
            accounts_service.ClaudeNotInstalled(
                f"Claude Code is not installed — install it first: {core.INSTALL_COMMAND}"
            )
        )
    identity = core.identity(account)
    who = identity.email if identity else "not signed in yet — Claude Code will ask"
    stderr_console().print(
        Text.assemble("Opening claude as ", (core.label(account), "bold"), f" ({who})…"),
    )
    _exec(found.binary, [found.binary, *ctx.args], accounts_service.session_env(account))


app.command("run", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})(run)
