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
from typing import Annotated, NoReturn

import typer
from rich.table import Table
from rich.text import Text

from aisquare.cli.common import fail, local_time
from aisquare.cli.fleet import not_interactive_reason
from aisquare.core import claude_accounts as core
from aisquare.core.console import stderr_console, stdout_console
from aisquare.core.state import get_state
from aisquare.models import AccountsOverview, ClaudeAccount, ClaudeAccountStatus, ClaudeUsage
from aisquare.services import claude_accounts as accounts_service

app = typer.Typer(
    help="Claude Code accounts this CLI manages: list, add, remove, run, usage.",
    no_args_is_help=False,
    invoke_without_command=True,
)

SlotRef = Annotated[str, typer.Argument(help="Slot number, or the email the slot is signed in as.")]

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


def _resets(when: datetime | None) -> str:
    return _DASH if when is None else local_time(when).strftime("%H:%M")


def _usage_cells(usage: ClaudeUsage | None) -> tuple[str, str]:
    """``(session, week)`` as the table shows them; the reason when there is nothing to show."""
    if usage is None:
        return _DASH, _DASH
    if not usage.available:
        return usage.reason or "unavailable", ""
    session = _percent(usage.session_percent)
    if usage.session_resets_at is not None:
        session += f" (resets {_resets(usage.session_resets_at)})"
    week = _percent(usage.week_percent)
    if usage.week_resets_at is not None:
        week += f" (resets {_resets(usage.week_resets_at)})"
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
    for column in ("slot", "label", "signed in as", "plan", "hooks"):
        table.add_column(column)
    if with_usage:
        table.add_column("session")
        table.add_column("week")
    table.add_column("directory", overflow="fold")
    for status in overview.accounts:
        who = Text(status.identity.email) if status.identity else Text("not signed in", style="dim")
        if status.identity and not status.signed_in:
            who = Text(f"{status.identity.email} (token missing)", style="yellow")
        cells: list[Text | str] = [
            str(status.account.slot),
            status.label,
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
                status.usage = accounts_service.usage(status.account)
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
            status.usage = accounts_service.usage(account)
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
