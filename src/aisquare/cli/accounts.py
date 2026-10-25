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
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from rich.table import Table
from rich.text import Text

from aisquare.cli.common import expected_config_write_errors, fail, format_reset
from aisquare.cli.fleet import not_interactive_reason
from aisquare.core import claude_accounts as core
from aisquare.core import claude_sharing
from aisquare.core.console import stderr_console, stdout_console
from aisquare.core.state import get_state
from aisquare.models import (
    AccountsOverview,
    ClaudeAccount,
    ClaudeAccountStatus,
    ClaudeUsage,
    ProjectInfo,
)
from aisquare.services import agents as agents_service
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import fleet as fleet_service
from aisquare.services import settings as settings_service

app = typer.Typer(
    help="Claude Code accounts this CLI manages: list, add, remove, run, usage, "
    "default, alias, order, move, disable, enable, trust, settings.",
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
    """``(session used, week used)`` as the table shows them; the reason when there is nothing.

    The columns say USED in their headers: a bare ``40%`` read as the room left
    to an agent on a fresh install, which then rationed a nearly empty account.

    The reset is ``format_reset``'s string — a distance and a clock time, the
    date when it is not today — shared with the Accounts page so the two
    cannot drift again (#152). ``now`` is for tests; the table reads the clock.
    """
    if usage is None:
        return _DASH, _DASH
    if not usage.available:
        return usage.reason or "unavailable", ""
    session = _window_cell(usage.session_percent, usage.session_resets_at, now=now)
    week = _window_cell(usage.week_percent, usage.week_resets_at, now=now)
    # The per-family weeks (#277), each with its own reset: they bind that family alone. A
    # kept reading has none: the record holds no family week, a spent one is a limit mark.
    for family, percent in sorted(usage.family_week_percent.items()):
        week += f" · {family} {_percent(percent)}"
        if (resets := usage.family_week_resets_at.get(family)) is not None:
            week += f" resets {format_reset(resets, now=now)}"
    return session, week


def _window_cell(percent: float | None, resets_at: datetime | None, *, now: datetime | None) -> str:
    """``40% · resets in 3h 10m (18:00)`` — or ``reset 1h ago`` for a kept reading's window
    that has reset since (its figure emptied: the window it described is over; review of
    #408, MAJ-1), never the old figure with ``resets now``."""
    moment = now or accounts_service._now()
    if percent is None and resets_at is not None and resets_at <= moment:
        return f"reset {accounts_service.describe_age(resets_at, now=moment)}"
    cell = _percent(percent)
    if resets_at is not None:
        cell += f" · resets {format_reset(resets_at, now=moment)}"
    return cell


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
    # the table reads top-down as "the order the fleet would try them": the first
    # enabled, signed-in row is the machine default, and needs no mark of its own.
    for column in ("slot", "label", "signed in as", "plan", "hooks"):
        table.add_column(column)
    if with_usage:
        table.add_column("session used")
        table.add_column("week used")
    table.add_column("directory", overflow="fold")
    for status in overview.accounts:
        who = Text(status.identity.email) if status.identity else Text("not signed in", style="dim")
        if status.identity and not status.signed_in:
            who = Text(f"{status.identity.email} (token missing)", style="yellow")
        label = Text(status.label)
        if status.account.disabled:
            label.append(" (disabled)", style="dim")
        cells: list[Text | str] = [
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
    console.print(
        Text(
            "add one: aisquare accounts add · open one: aisquare accounts run <slot> · "
            "the Accounts page in asq does both",
            style="dim",
        )
    )


_REFRESH_HELP = (
    "Fetch now instead of serving a reading up to five minutes old (the shared usage cache). "
    "An account the usage endpoint rate-limited is not asked again before its wait ends."
)


def _refreshed_line(statuses: list[ClaudeAccountStatus]) -> Text | None:
    """``usage as of 3m ago … --refresh fetches now``, by the Accounts page's rule: the OLDEST
    reading actually read among the rows whose usage can be read
    (``usage_is_readable``), and ``· last try failed just now``, the newest such try, when
    one of them read nothing (an unreachable endpoint, an HTTP error) or a try after its
    reading failed (``last_failed_at``: the cache keeps the figure, and says so).

    ``cached_usage`` stamps a failure with the moment it was found out, and aged with the
    readings it said "usage as of just now" with the endpoint down (review of #261, m5). A
    row that needs a login, or whose token cannot be read, counts in neither half: its own
    cells say why. ``None`` when no row was read or tried.
    """
    tried = [
        status.usage
        for status in statuses
        if status.usage is not None and accounts_service.usage_is_readable(status, status.usage)
    ]
    read = [usage.fetched_at for usage in tried if usage.available and usage.fetched_at]
    failed = [
        when
        for usage in tried
        if (when := usage.last_failed_at if usage.available else usage.fetched_at) is not None
    ]
    if not read and not failed:
        return None
    said = f"usage as of {accounts_service.describe_age(min(read))}" if read else "no usage read"
    if failed:
        said += f" · last try failed {accounts_service.describe_age(max(failed))}"
    # During a wait `--refresh` sends nothing for that account: the hint says so rather than
    # promise a fetch (review of #408, N1).
    moment = accounts_service._now()
    waiting = [
        (status.label, until)
        for status in statuses
        if status.usage is not None
        and status.usage.rate_limited
        and (until := status.usage.rate_limited_until) is not None
        and until > moment
    ]
    if not waiting:
        hint = "--refresh fetches now"
    elif len(waiting) == len(tried):
        first = min(until for _, until in waiting)
        hint = f"{accounts_service.rate_limited_until_text(first)}; --refresh waits too"
    else:
        held = ", ".join(
            f"{label} {accounts_service.rate_limited_until_text(until)}" for label, until in waiting
        )
        hint = f"--refresh fetches now, except {held}"
    return Text(
        f"{said} (percent USED; refreshed at most every 5 min) — {hint}",
        style="dim",
    )


def _kept_lines(statuses: list[ClaudeAccountStatus]) -> list[Text]:
    """One line per row showing an older reading because the newest try read nothing:
    ``account 2: rate-limited until 14:32 — …; showing the reading from 12m ago`` (F1 of
    #400). The numbers in the table are that reading's; this says how old, and why."""
    lines: list[Text] = []
    for status in statuses:
        usage = status.usage
        if usage is None or not usage.available or usage.last_failed_reason is None:
            continue
        age = (
            f"; showing the reading from {accounts_service.describe_age(usage.fetched_at)}"
            if usage.fetched_at is not None
            else ""
        )
        lines.append(Text(f"{status.label}: {usage.last_failed_reason}{age}", style="yellow"))
    return lines


@app.command("list")
def list_(
    usage: Annotated[
        bool,
        typer.Option(
            "--usage",
            help="Also show each signed-in account's session and weekly usage (percent USED), "
            "from the shared five-minute cache.",
        ),
    ] = False,
    refresh: Annotated[bool, typer.Option("--refresh", help=_REFRESH_HELP)] = False,
) -> None:
    """List the accounts: slot, who is signed in, plan, hooks; --usage adds the limits."""
    overview = accounts_service.overview()
    if usage:
        # One concurrent, RECORDING read for every signed-in slot that has no
        # reading younger than five minutes (#146; review of #205, third round):
        # a slow endpoint costs one timeout, not one each.
        signed = [status for status in overview.accounts if status.signed_in]
        readings = accounts_service.cached_usage(
            [status.account for status in signed], force=refresh
        )
        for status in signed:
            status.usage = readings.get(status.account.slot)
    if get_state().json_output:
        typer.echo(_overview_json(overview))
        return
    _emit_overview(overview, with_usage=usage)
    if usage:
        for kept in _kept_lines(overview.accounts):
            stdout_console().print(kept)
    if usage and (line := _refreshed_line(overview.accounts)) is not None:
        stdout_console().print(line)


@app.command("usage")
def usage_(
    slot: Annotated[
        str | None,
        typer.Argument(
            help="Slot number, alias or email (default: every signed-in account, in priority "
            "order)."
        ),
    ] = None,
    refresh: Annotated[bool, typer.Option("--refresh", help=_REFRESH_HELP)] = False,
) -> None:
    """Session (5-hour) and weekly usage per signed-in account — percent USED, as /usage shows it.

    40% used is 60% left. A reading is shared for five minutes between this
    command, `list --usage` and asq's Accounts page; `--refresh` fetches now.
    """
    # The arranged list, like every other account surface: priority order, the
    # alias as the label, disabled marked — and one concurrent read for all of
    # them (review of #205, third round), skipping what the cache still holds.
    accounts = [_resolve(slot)] if slot is not None else accounts_service.list_accounts()
    statuses = [accounts_service.describe(account) for account in accounts]
    asked = [status for status in statuses if status.signed_in or slot is not None]
    readings = accounts_service.cached_usage([status.account for status in asked], force=refresh)
    for status in asked:
        status.usage = readings.get(status.account.slot)
    if get_state().json_output:
        typer.echo(json.dumps([status.model_dump(mode="json") for status in statuses]))
        return
    console = stdout_console()
    labels: list[Text] = []
    for status in statuses:
        label = Text(status.label)  # `describe` put it there, as `_emit_overview` reads it
        if status.account.disabled:
            label.append(" (disabled)", style="dim")
        labels.append(label)
    table = Table(box=None, pad_edge=False, show_edge=False, header_style="bold")
    table.add_column("slot")
    # The label is never squeezed: a narrow terminal shortens the email instead.
    widest = max((len(text.plain) for text in labels), default=5)
    table.add_column("label", no_wrap=True, min_width=widest)
    for column in ("signed in as", "session used", "week used"):
        table.add_column(column)
    for status, label in zip(statuses, labels, strict=True):
        who = Text(status.identity.email) if status.identity else Text("not signed in", style="dim")
        session, week = _usage_cells(status.usage)
        table.add_row(str(status.account.slot), label, who, session, week)
    console.print(table)
    for kept in _kept_lines(asked):
        console.print(kept)
    if (line := _refreshed_line(asked)) is not None:
        console.print(line)


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
            "machine default: none — no enabled account is signed in, so launches run on "
            "whatever claude the shell has",
            markup=False,
        )
    else:
        console.print(f"machine default: {_who(machine)} · rank {machine.position}", markup=False)
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
            "the machine default (the first enabled, signed-in account in priority order) · "
            "whatever the shell has",
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
        bool,
        typer.Option(
            "--clear", help="Remove a project's default or a role's binding (not the machine's)."
        ),
    ] = False,
) -> None:
    """Choose the account a launch runs under — for the machine, a project, or a role.

    Without a reference it prints the defaults as they stand. With one it sets a
    PROJECT's default (``--project``) or a ROLE's binding (``--role``, the same
    thing ``team bind <role> --account`` writes). The MACHINE default is rank 1
    of the priority order — the first enabled, signed-in account — so at machine
    level this is an alias of ``accounts move <ref> top``. A launch resolves them
    top-down: ``--account`` on the command line, then the role's binding, then
    the project's default, then the machine's; with no account signed in it runs
    on whatever ``claude`` the shell already has.
    """
    if project is not None and role is not None:
        fail("--project and --role are mutually exclusive", error="usage")
    if ref is not None and clear:
        fail("pass an account or --clear, not both", error="usage")
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
    elif target is None:
        stdout_console().print(
            f"✓ machine default: {_who(chosen)} — moved to rank 1 of the priority order",
            markup=False,
        )
        if not core.signed_in(chosen):
            # Rank 1 is the default only for an account that can run a launch (review
            # of #406, m1): say so, or the line above promises what the next launch skips.
            stdout_console().print(
                f"  · it is not signed in, so launches skip it until it is: "
                f"aisquare accounts run {chosen.slot}",
                markup=False,
            )
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
    name: Annotated[str | None, typer.Argument(help=f"The name: {core.ALIAS_HINT}")] = None,
    clear: Annotated[bool, typer.Option("--clear", help="Remove the slot's alias.")] = False,
) -> None:
    """Name an account (`work`, `"Work laptop"`) so --account and the board can say it.

    Any text up to 64 characters, its case kept and matched without it; quote a
    name with spaces, here and in --account.
    """
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
        off = "  (disabled)" if account.disabled else ""
        console.print(f"{account.position}. {_who(account)}{off}", markup=False)


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
            "  ⚠ aisquare's hooks did not install into it — run: "
            + agents_service.config_dir_command("connect", "claude-code", account.config_dir),
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
    notes: list[str] = []
    try:
        moved = accounts_service.remove(account, notes=notes)
    except accounts_service.AccountsError as exc:
        fail(str(exc), error="not_removable", ref=slot)
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "removed": account.slot,
                    "email": identity.email if identity else None,
                    "moved_to": str(moved),
                    "notes": notes,
                }
            )
        )
        return
    who = f" ({identity.email})" if identity else ""
    console = stdout_console()
    console.print(
        Text.assemble(
            ("✓ ", "green"),
            f"removed account {account.slot}{who} — its directory is kept at {moved}; "
            "delete it when you are sure",
        )
    )
    for note in notes:
        console.print(f"  · {note}", markup=False)


_TRUST_WORDS = {
    "trusted": "trusted now",
    "already": "already trusted",
    "skipped": "skipped",
    "failed": "NOT trusted",
}


@app.command("trust")
def trust(
    folder: Annotated[
        Path | None,
        typer.Argument(
            help="The folder to trust on every account (default: this one). In a git "
            "checkout its repository is trusted, as Claude Code's own yes there would.",
            show_default=False,
        ),
    ] = None,
    sync: Annotated[
        bool,
        typer.Option(
            "--sync",
            help="Instead: copy every folder any account trusts into every other account.",
        ),
    ] = False,
) -> None:
    """Trust a folder on every Claude account at once, so no agent stops at the question.

    Claude Code asks "trust this folder?" once per account, and an agent on an account
    that was never asked waits there for a human. A launch copies the trust from any
    account that has it (`\\[accounts] share_trust`); this does it now, for every account.
    Only the folder's trust entry in each account's .claude.json is written.
    """
    if sync and folder is not None:
        fail("pass a folder, or --sync — not both", error="usage")
    if sync:
        changes, gone = accounts_service.sync_trust()
        _trust_report(changes, None, gone=gone)
        return
    target = (folder or Path.cwd()).expanduser()
    if not target.is_dir():
        fail(f"{target} is not a folder", error="no_such_folder", ref=str(target))
    scope, changes = accounts_service.trust_everywhere(target)
    _trust_report(changes, scope)


def _trust_report(
    changes: list[accounts_service.TrustChange],
    scope: claude_sharing.TrustScope | None,
    *,
    gone: int = 0,
) -> None:
    failed = any(change.outcome == "failed" for change in changes)
    if get_state().json_output:
        payload: dict[str, Any] = {
            "accounts": [
                {
                    "slot": change.account.slot,
                    "label": core.label(change.account),
                    "outcome": change.outcome,
                    "folders": list(change.folders),
                    "reason": change.reason,
                }
                for change in changes
            ]
        }
        if scope is not None:
            payload["folder"] = str(scope.folder)
            payload["trusted_as"] = scope.key
        else:
            payload["folders_gone"] = gone
        typer.echo(json.dumps(payload))
    else:
        console = stdout_console()
        if scope is not None:
            head = f"{scope.key} on every account that has started"
            if scope.key != claude_sharing.project_key(scope.folder):
                head += f" (it covers {scope.folder})"
        else:
            head = "every trusted folder on every account that has started"
        mark, style = ("✗ ", "red") if failed else ("✓ ", "green")
        console.print(Text.assemble((mark, style), head))
        for change in changes:
            words = _TRUST_WORDS[change.outcome]
            if scope is None and change.outcome == "trusted":
                words = f"{len(change.folders)} folder(s) trusted now: {', '.join(change.folders)}"
            line = f"  · {core.label(change.account)}: {words}"
            if change.reason:
                line += f" — {change.reason}"
            console.print(line, markup=False, style="red" if change.outcome == "failed" else None)
        if gone:
            console.print(
                f"  · {gone} trusted folder(s) no longer exist — not copied", markup=False
            )
    if failed:
        raise typer.Exit(1)


_SETTINGS_WORDS = {
    "shared": "shares it now",
    "already": "shares it",
    "kept": "keeps its own",
    "unshared": "has its own copy now",
    "own": "has its own",
    "none": "has none",
    "failed": "FAILED",
}


@app.command("settings")
def settings(
    sync: Annotated[
        bool,
        typer.Option(
            "--sync",
            help="Link every managed account's settings.json to the plain claude's now "
            "(each one's own is kept beside it as settings.json.own-<stamp>).",
        ),
    ] = False,
    unshare: Annotated[
        bool,
        typer.Option(
            "--unshare",
            help="Give every linked account its own copy again, as it is now.",
        ),
    ] = False,
) -> None:
    """One settings.json for every account: the plain claude's, which the others link to.

    Bare, it shows what each managed account's settings.json is. Permissions, hooks,
    the model and the status line then change once for all; the logins stay apart —
    .claude.json and .credentials.json are never shared, and a settings.json that sets
    what decides a login (an API key, a token, a cloud provider) is never shared or
    replaced. `aisquare config set accounts.share_settings true` relinks at each launch.
    """
    if sync and unshare:
        fail("pass --sync or --unshare — not both", error="usage")
    refusal: str | None = None
    try:
        if sync:
            source, changes = accounts_service.share_settings()
        elif unshare:
            source, changes = accounts_service.unshare_settings()
        else:
            source, refusal, changes = accounts_service.settings_overview()
    except accounts_service.AccountsError as exc:
        fail(str(exc), error="not_shareable")
    sharing_on = accounts_service.accounts_settings().share_settings
    failed = any(change.outcome == "failed" for change in changes)
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "source": str(source),
                    "share_settings": sharing_on,
                    "refusal": refusal,
                    "accounts": [
                        {
                            "slot": change.account.slot,
                            "label": core.label(change.account),
                            "outcome": change.outcome,
                            "kept": None if change.backup is None else str(change.backup),
                            "reason": change.reason,
                        }
                        for change in changes
                    ],
                }
            )
        )
    else:
        console = stdout_console()
        console.print(
            f"the plain claude's settings.json: {source} · relinked at each launch: "
            f"{'on' if sharing_on else 'off'}",
            markup=False,
        )
        if refusal is not None:
            console.print(f"  ⚠ cannot be shared: {refusal}", markup=False, style="yellow")
        if not changes:
            console.print(
                "  · no managed accounts — `aisquare accounts add` makes one", markup=False
            )
        for change in changes:
            line = f"  · {core.label(change.account)} {_SETTINGS_WORDS[change.outcome]}"
            if change.backup is not None:
                line += f" (its own kept as {change.backup})"
            if change.reason:
                line += f" — {change.reason}"
            console.print(line, markup=False, style="red" if change.outcome == "failed" else None)
    if failed:
        raise typer.Exit(1)


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
    for note in accounts_service.share_for_launch(account=account):
        stderr_console().print(f"accounts: {note}", style="dim")
    _exec(found.binary, [found.binary, *ctx.args], accounts_service.session_env(account))


app.command("run", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})(run)
