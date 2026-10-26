"""``aisquare metrics stats`` as text: the same model the Usage & stats page draws."""

from __future__ import annotations

from rich.table import Table
from rich.text import Text

from aisquare.cli import usage_format as fmt
from aisquare.core.console import stdout_console
from aisquare.models import SharingStatus, ShipReport, UsageStats


def _head(text: str) -> Text:
    return Text(text, style="bold")


def emit_usage_stats(stats: UsageStats) -> None:
    console = stdout_console()
    label = fmt.RANGE_LABELS[stats.range]
    if not stats.turns and stats.recorded_turns:
        console.print(
            f"Nothing in {label.lower()}: Claude Code was not used in it. "
            f"{stats.recorded_turns:,} turns are recorded in all: try --range lifetime."
        )
        return
    if not stats.turns:
        console.print(
            f"No usage recorded for {label.lower()} yet. Claude Code's transcripts are read "
            "by `aisquare metrics backfill`, and each Stop hook adds its turn."
        )
        return
    span = f"{stats.since} → {stats.until}" if stats.since != stats.until else stats.until
    console.print(_head(f"Usage — {label}  ({span})"))
    kpi = Table(show_header=False, box=None, pad_edge=False)
    kpi.add_column(style="dim")
    kpi.add_column(justify="right")
    tokens = stats.tokens
    kpi.add_row("tokens (total)", f"{tokens.total:,}")
    kpi.add_row("  input + output", f"{tokens.input_output:,}")
    kpi.add_row("  input", f"{tokens.input:,}")
    kpi.add_row("  output", f"{tokens.output:,}")
    kpi.add_row("  cache write", f"{tokens.cache_write:,}")
    kpi.add_row("  cache read", f"{tokens.cache_read:,}")
    kpi.add_row("list-price cost", fmt.money(stats.cost_usd))
    kpi.add_row("sessions", f"{stats.sessions:,}")
    kpi.add_row("turns", f"{stats.turns:,}")
    kpi.add_row("active days", f"{stats.active_days:,}")
    console.print(kpi)
    if len(stats.days) > 1:
        peak = max(day.tokens.total for day in stats.days)
        console.print(_head("\nPer day (tokens)"))
        for day in stats.days[-31:]:
            console.print(
                f"{fmt.short_date(day.date):<11} {fmt.bar(day.tokens.total, peak, 30):<31}"
                f" {fmt.tokens(day.tokens.total):>7}"
            )
    if stats.busiest_days:
        console.print(_head("\nBusiest days"))
        for day in stats.busiest_days:
            console.print(
                f"{fmt.short_date(day.date):<11} {fmt.tokens(day.tokens.total):>7}"
                f"  {day.turns} turns · {day.sessions} sessions"
            )
    weekdays = " ".join(f"{n}:{c}" for n, c in zip(fmt.WEEKDAYS, stats.by_weekday, strict=True))
    console.print(_head("\nWhen (turns)"))
    console.print(f"by weekday   {fmt.spark(stats.by_weekday)}  {weekdays}")
    console.print(f"by hour       {fmt.spark(stats.by_hour)}  (0h → 23h)")
    _repos(stats)
    _accounts(stats)
    if stats.models:
        table = Table(title="Models", title_justify="left", box=None, pad_edge=False)
        for column in ("model", "tokens", "share", "cost"):
            table.add_column(column, justify="left" if column == "model" else "right")
        for model in stats.models:
            cost = fmt.money(model.cost_usd) if model.priced else "unpriced"
            table.add_row(
                model.model, fmt.tokens(model.tokens.total), fmt.percent(model.share), cost
            )
        console.print()
        console.print(table)
    if stats.features:
        console.print(_head("\nTop features"))
        for feature in stats.features:
            console.print(f"{feature.name:<34} {feature.count:>6,}")
    if stats.unpriced_models:
        console.print(
            f"\n⚠ no list price for {', '.join(stats.unpriced_models)}: in the token totals, "
            "not in the cost"
        )
    console.print(Text(f"\ncost at list prices as of {stats.prices_as_of}", style="dim"))


def _repos(stats: UsageStats) -> None:
    repos = stats.repos
    table = Table(
        title=f"Repositories ({repos.active} active)", title_justify="left", box=None,
        pad_edge=False,
    )  # fmt: skip
    for column in ("repository", "tokens", "sessions", "last active"):
        table.add_column(column, justify="left" if column == "repository" else "right")
    for row in repos.rows:
        table.add_row(
            row.name, fmt.tokens(row.tokens.total), str(row.sessions), row.last_active or ""
        )
    stdout_console().print()
    stdout_console().print(table)


def _accounts(stats: UsageStats) -> None:
    accounts = stats.accounts
    kinds = ", ".join(f"{n} {kind}" for kind, n in accounts.by_type.items()) or "none"
    table = Table(
        title=f"Accounts ({accounts.active} of {accounts.registered} active: {kinds})",
        title_justify="left", box=None, pad_edge=False,
    )  # fmt: skip
    for column in ("account", "plan", "tokens", "share"):
        table.add_column(column, justify="left" if column in ("account", "plan") else "right")
    for row in accounts.rows:
        table.add_row(
            row.label, row.subscription, fmt.tokens(row.tokens.total), fmt.percent(row.share)
        )
    stdout_console().print()
    stdout_console().print(table)


def _when(moment: object) -> str:
    from datetime import datetime

    return moment.astimezone().strftime("%Y-%m-%d %H:%M") if isinstance(moment, datetime) else "—"


def sharing_lines(status: SharingStatus) -> list[str]:
    """``aisquare metrics share status`` as lines: one word first, then the facts."""
    if status.forced_off:
        head = f"Sharing is OFF, and forced off: {status.forced_off}."
    elif status.enabled:
        head = "Sharing is ON: daily usage counts are sent to AISquare."
    else:
        head = "Sharing is off. Nothing leaves this machine."
    lines = [head]
    if status.install_id:
        lines.append(f"installation   {status.install_id[:8]}… (random; not derived from you)")
    if status.enabled or status.last_sent_at:
        who = (
            "signed in: batches are linked to your AISquare account"
            if status.signed_in
            else "not signed in: batches are anonymous"
        )
        lines.append(f"account        {who}")
        sent = _when(status.last_sent_at) if status.last_sent_at else "never"
        linked = "" if status.linked is None else f" (linked: {'yes' if status.linked else 'no'})"
        lines.append(f"last sent      {sent}{linked}")
    if status.enabled:
        lines.append(f"next           {_when(status.next_due_at) if status.next_due_at else 'due'}")
        if status.endpoint:
            lines.append(f"endpoint       {status.endpoint}")
    if status.last_error:
        lines.append(f"last error     {status.last_error} (tried {status.failures}x)")
    lines.append("See exactly what is sent: aisquare metrics share preview")
    return lines


def emit_sharing_status(status: SharingStatus) -> None:
    console = stdout_console()
    for line in sharing_lines(status):
        console.print(line)


def emit_ship_report(report: ShipReport) -> None:
    console = stdout_console()
    if report.sent and report.opt_out:
        console.print("Sent the opt-out notice. Nothing is sent from now on.")
    elif report.sent:
        linked = "" if report.linked is None else f", linked: {'yes' if report.linked else 'no'}"
        console.print(f"Sent {report.days} day(s){linked}.")
    elif report.error:
        console.print(f"Not sent: {report.error}. It is tried again after a wait.")
    else:
        console.print(f"Nothing sent: {report.skipped}.")
