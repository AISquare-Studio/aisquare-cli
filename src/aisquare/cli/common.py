"""Shared CLI parsing and rendering helpers."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import NoReturn

import tomli_w
import typer
from rich.table import Table

from aisquare.core.config import AppConfig
from aisquare.core.console import stderr_console, stdout_console
from aisquare.core.state import get_state
from aisquare.models import (
    AgentConnection,
    AgentInfo,
    CheckStatus,
    ContextEntry,
    DoctorCheck,
    InjectionRecord,
    MetricsSummary,
    OnboardReport,
    Pool,
    ProjectInfo,
    PromptRecord,
    SetupReport,
    StatusReport,
    TurnMetric,
)

_DEFAULT_EMPTY = 'No context entries yet. Add one with: aisquare remember "…"'


def local_time(value: datetime) -> datetime:
    """A stored (UTC) timestamp in the user's local timezone, for display."""
    return value.astimezone()


def resolve_pool(user: bool, project: bool) -> Pool | None:
    """Map the ``--user``/``--project`` flag pair onto a pool name.

    Returns ``None`` when neither flag is given, letting the service apply
    the configured default pool.
    """
    if user and project:
        raise typer.BadParameter("--user and --project are mutually exclusive.")
    if user:
        return "user"
    if project:
        return "project"
    return None


def emit_entry(entry: ContextEntry, *, verb: str) -> None:
    """Render a single entry — JSON under ``--json``, a confirmation otherwise."""
    if get_state().json_output:
        typer.echo(entry.model_dump_json())
    else:
        stdout_console().print(f"✓ {verb} ({entry.pool}): {entry.text}")


def emit_entries(entries: list[ContextEntry], *, empty_message: str = _DEFAULT_EMPTY) -> None:
    """Render a list of entries — a JSON array under ``--json``, a table otherwise."""
    if get_state().json_output:
        typer.echo(json.dumps([entry.model_dump(mode="json") for entry in entries]))
        return
    if not entries:
        stdout_console().print(empty_message)
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", no_wrap=True)
    table.add_column("POOL", no_wrap=True)
    table.add_column("TAGS")
    table.add_column("TEXT")
    for entry in entries:
        table.add_row(entry.id, entry.pool, ", ".join(entry.tags), entry.text)
    stdout_console().print(table)


def emit_removed(ref: str) -> None:
    """Confirm a deletion — JSON under ``--json``, a confirmation otherwise."""
    if get_state().json_output:
        typer.echo(json.dumps({"removed": ref}, separators=(",", ":")))
    else:
        stdout_console().print(f"✓ removed {ref}")


def emit_imported(count: int) -> None:
    """Confirm an import — JSON under ``--json``, a confirmation otherwise."""
    if get_state().json_output:
        typer.echo(json.dumps({"imported": count}, separators=(",", ":")))
    else:
        noun = "entry" if count == 1 else "entries"
        stdout_console().print(f"✓ imported {count} {noun}")


def emit_exported(file: Path) -> None:
    """Confirm an export to a file — JSON under ``--json``, a confirmation otherwise."""
    if get_state().json_output:
        typer.echo(json.dumps({"exported": str(file)}, separators=(",", ":")))
    else:
        stdout_console().print(f"✓ exported to {file}")


def emit_entry_detail(entry: ContextEntry) -> None:
    """Render one entry in full — JSON under ``--json``, a key/value view otherwise."""
    if get_state().json_output:
        typer.echo(entry.model_dump_json())
        return
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    grid.add_row("id", entry.id)
    grid.add_row("pool", entry.pool)
    if entry.project_id:
        grid.add_row("project", entry.project_id)
    if entry.tags:
        grid.add_row("tags", ", ".join(entry.tags))
    grid.add_row("created", entry.created_at.isoformat())
    grid.add_row("updated", entry.updated_at.isoformat())
    console = stdout_console()
    console.print(grid)
    console.print()
    console.print(entry.text)


def emit_block(block: str) -> None:
    """Emit an assembled context block — wrapped under ``--json``, verbatim otherwise."""
    if get_state().json_output:
        typer.echo(json.dumps({"block": block}))
    else:
        typer.echo(block, nl=False)


def emit_injection_record(record: InjectionRecord | None) -> None:
    """Render the last-injection record for ``why``."""
    if get_state().json_output:
        typer.echo("null" if record is None else record.model_dump_json())
        return
    console = stdout_console()
    if record is None:
        console.print("No context has been injected yet. Run: aisquare inject")
        return
    total = record.user_count + record.project_count
    console.print(f"Last injection: {record.injected_at.isoformat()}")
    console.print(
        f"  {total} entries — {record.user_count} from your user pool, "
        f"{record.project_count} from this project"
    )
    if record.retrieved_chars:
        console.print(
            f"  {record.retrieved_chars} chars retrieved by the CI test bed "
            "(candidate reference, not fetched by the agent)"
        )
        if record.retrieved_sources:
            console.print("  sources: " + ", ".join(record.retrieved_sources))


def emit_project_detail(project: ProjectInfo) -> None:
    """Render one project — JSON under ``--json``, a key/value view otherwise."""
    if get_state().json_output:
        typer.echo(project.model_dump_json())
        return
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    grid.add_row("name", project.root.name or project.id)
    grid.add_row("id", project.id)
    grid.add_row("root", str(project.root))
    if project.linked_repos:
        grid.add_row("repos", ", ".join(project.linked_repos))
    stdout_console().print(grid)


def emit_projects(projects: list[ProjectInfo], *, active_id: str | None) -> None:
    """Render the project list — a JSON array under ``--json``, a table otherwise."""
    if get_state().json_output:
        typer.echo(json.dumps([project.model_dump(mode="json") for project in projects]))
        return
    if not projects:
        stdout_console().print("No projects registered yet. Run: aisquare init")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("", no_wrap=True)
    table.add_column("NAME")
    table.add_column("ID", no_wrap=True)
    table.add_column("ROOT")
    for project in projects:
        marker = "*" if project.id == active_id else ""
        table.add_row(marker, project.root.name or "—", project.id, str(project.root))
    stdout_console().print(table)


def emit_project_action(message: str, project: ProjectInfo) -> None:
    """Confirm a project action — the project as JSON under ``--json``, a message otherwise."""
    if get_state().json_output:
        typer.echo(project.model_dump_json())
    else:
        stdout_console().print(message)


def emit_setup(report: SetupReport) -> None:
    """Render the outcome of ``init``."""
    if get_state().json_output:
        typer.echo(report.model_dump_json())
        return
    console = stdout_console()
    verb = "already initialized" if report.already_initialized else "initialized"
    console.print(f"✓ aisquare {verb} at {report.home}")
    console.print(
        f"  project: {report.project.root.name or report.project.id} ({report.project.id})"
    )
    if report.onboarded:
        console.print(f"  onboarded {report.onboarded} context entries")
    for note in report.notes:
        console.print(f"  note: {note}")


def emit_config(config: AppConfig) -> None:
    """Render the full config — JSON under ``--json``, TOML otherwise."""
    if get_state().json_output:
        typer.echo(config.model_dump_json())
    else:
        typer.echo(tomli_w.dumps(config.model_dump(mode="json")), nl=False)


def emit_config_value(key: str, value: str) -> None:
    """Render one config value — ``{key: value}`` under ``--json``, ``key = value`` otherwise."""
    if get_state().json_output:
        typer.echo(json.dumps({key: value}))
    else:
        stdout_console().print(f"{key} = {value}")


def emit_agents(agents: list[AgentInfo]) -> None:
    """Render detected agents — a JSON array under ``--json``, a table otherwise."""
    if get_state().json_output:
        typer.echo(json.dumps([agent.model_dump(mode="json") for agent in agents]))
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("AGENT")
    table.add_column("DETECTED")
    table.add_column("CONNECTED")
    table.add_column("HOOKS IN")
    table.add_column("CONTEXT")
    for agent in agents:
        context = ", ".join(str(path) for path in agent.config_paths) or "—"
        table.add_row(
            agent.name,
            "yes" if agent.detected else "no",
            "yes" if agent.connected else "no",
            _hook_sites(agent),
            context,
        )
    stdout_console().print(table)


def _hook_sites(agent: AgentInfo) -> str:
    """One cell summarising where an agent's hooks live and whether they're healthy.

    Parallel installs each own a config dir, so a bare yes/no would hide a dir
    whose hooks went missing — name the broken ones explicitly.
    """
    if not agent.sites:
        return "—"
    broken = [site.config_dir for site in agent.sites if not site.hooks_installed]
    if not broken:
        if len(agent.sites) == 1:
            return str(agent.sites[0].config_dir)
        return f"{len(agent.sites)} dirs, all ok"
    listed = ", ".join(str(path) for path in broken)
    return f"{len(agent.sites) - len(broken)}/{len(agent.sites)} ok — missing in {listed}"


def emit_connected(connection: AgentConnection) -> None:
    """Confirm an agent connection: hook install + context ingested."""
    if get_state().json_output:
        typer.echo(connection.model_dump_json())
        return
    hooks = "hooks installed" if connection.hooks_installed else "no hooks for this agent"
    noun = "entry" if connection.imported == 1 else "entries"
    stdout_console().print(
        f"✓ connected {connection.name} — {hooks}; imported {connection.imported} {noun}"
    )


def emit_onboard(report: OnboardReport) -> None:
    """Render the outcome of ``project onboard`` — snapshot summary + seeded facts."""
    if get_state().json_output:
        typer.echo(report.model_dump_json())
        return
    console = stdout_console()
    snapshot = report.snapshot
    if snapshot is not None and snapshot.status == "ready":
        line = f"✓ snapshot: {snapshot.file_count} files, {snapshot.token_count} tokens"
        if snapshot.skeleton_token_count:
            line += f" (skeleton {snapshot.skeleton_token_count} tokens)"
        console.print(line)
    elif snapshot is not None and snapshot.status == "too_large":
        console.print("snapshot: codebase too large to pack within the token budget")
    else:
        console.print("snapshot: skipped (repomix/Node not available)")
    if report.seeded:
        console.print(f"seeded {len(report.seeded)} project fact(s):")
        for entry in report.seeded:
            console.print(f"  - {entry.text}")


def emit_prompts(prompts: list[PromptRecord]) -> None:
    """Render captured prompt history — a JSON array under ``--json``, a table otherwise."""
    if get_state().json_output:
        typer.echo(json.dumps([prompt.model_dump(mode="json") for prompt in prompts]))
        return
    if not prompts:
        stdout_console().print(
            "No prompts captured yet. Connect Claude Code: aisquare agents connect claude-code"
        )
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("WHEN", no_wrap=True)
    table.add_column("PROMPT")
    for prompt in prompts:
        table.add_row(prompt.created_at.strftime("%Y-%m-%d %H:%M"), prompt.text)
    stdout_console().print(table)


def emit_metrics_summary(summary: MetricsSummary) -> None:
    """Render a metrics summary — JSON under ``--json``, a table otherwise."""
    if get_state().json_output:
        typer.echo(json.dumps(summary.model_dump(mode="json")))
        return
    console = stdout_console()
    if not summary.turns:
        console.print(
            "No turns recorded yet. Connect Claude Code: aisquare agents connect claude-code"
        )
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("METRIC", no_wrap=True)
    table.add_column("VALUE", justify="right")
    table.add_row("turns", str(summary.turns))
    table.add_row("median wall", _ms(summary.median_wall_ms))
    table.add_row("CI answered", str(summary.ci_consulted))
    table.add_row("CI degraded", str(summary.degraded))
    table.add_row("cache hits", str(summary.cache_hits))
    table.add_row("budget breaches", str(summary.budget_breaches))
    table.add_row("turns with injection", str(summary.injected_turns))
    table.add_row("median round trip", _ms(summary.median_round_trip_ms))
    table.add_row("p95 round trip", _ms(summary.p95_round_trip_ms))
    console.print(table)

    if summary.by_reason:
        reasons = ", ".join(
            f"{reason} {count}" for reason, count in sorted(summary.by_reason.items())
        )
        console.print(f"reasons: {reasons}")
    if summary.budget_breaches:
        console.print(
            f"⚠ {summary.budget_breaches} turn(s) hit the client backstop — "
            "that is a server-side latency bug, not a result"
        )
    if not summary.turns_with_tokens:
        console.print(
            "note: no token counts recorded — hook payloads do not carry them "
            "(pending the OTel work), so token savings cannot be read from this yet"
        )


def emit_turn_metrics(turns: list[TurnMetric]) -> None:
    """Render recent turns — a JSON array under ``--json``, a table otherwise."""
    if get_state().json_output:
        typer.echo(json.dumps([turn.model_dump(mode="json") for turn in turns]))
        return
    if not turns:
        stdout_console().print("No turns recorded yet.")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("WHEN", no_wrap=True)
    table.add_column("TRACE", no_wrap=True)
    table.add_column("ACTION", no_wrap=True)
    table.add_column("REASON", no_wrap=True)
    table.add_column("WALL", justify="right")
    table.add_column("TRIP", justify="right")
    for turn in turns:
        table.add_row(
            local_time(turn.started_at).strftime("%m-%d %H:%M:%S"),
            turn.trace_id[:12],
            turn.ci_action,
            turn.degradation_reason,
            _ms(turn.wall_ms),
            "cached" if turn.cache_hit else _ms(turn.round_trip_ms),
        )
    stdout_console().print(table)


def _ms(value: int | None) -> str:
    """A millisecond count, or an em dash when the turn never recorded one."""
    return "—" if value is None else f"{value} ms"


def emit_disconnected(name: str) -> None:
    """Confirm an agent disconnection."""
    if get_state().json_output:
        typer.echo(json.dumps({"disconnected": name}, separators=(",", ":")))
    else:
        stdout_console().print(f"✓ disconnected {name}")


def emit_status(report: StatusReport) -> None:
    """Render the status summary."""
    if get_state().json_output:
        typer.echo(report.model_dump_json())
        return
    console = stdout_console()
    project = report.active_project
    console.print(f"aisquare: {'initialized' if report.initialized else 'not initialized'}")
    console.print(f"home:     {report.home}")
    console.print(f"project:  {project.root.name or project.id} ({project.id})")
    console.print(
        f"context:  {report.user_entries} user, {report.project_entries} in this project; "
        f"{report.project_count} project(s) registered"
    )
    console.print(f"detected: {', '.join(report.agents_detected) or 'none'}")
    console.print(f"connected: {', '.join(report.agents_connected) or 'none'}")


_CHECK_SYMBOL = {CheckStatus.ok: "✓", CheckStatus.warn: "⚠", CheckStatus.fail: "✗"}


def emit_doctor(checks: list[DoctorCheck]) -> None:
    """Render diagnostic check results, with a fix hint for anything not OK."""
    if get_state().json_output:
        typer.echo(json.dumps([check.model_dump(mode="json") for check in checks]))
        return
    console = stdout_console()
    for check in checks:
        console.print(f"{_CHECK_SYMBOL[check.status]} {check.name}: {check.detail}")
        if check.fix and check.status is not CheckStatus.ok:
            console.print(f"    → {check.fix}")


def fail(
    message: str,
    *,
    error: str,
    ref: str | None = None,
    hint: str | None = None,
    detail: str | None = None,
    exit_code: int = 1,
) -> NoReturn:
    """Report a runtime error and exit.

    Mirrors the stub contract: a machine-readable object on stdout under
    ``--json``, a human message on stderr otherwise. ``hint`` carries
    actionable context (e.g. which board actually holds a receipt) and
    ``detail`` the underlying cause (e.g. the real sqlite error text) into
    the JSON payload; the human message weaves both into its own text.
    """
    if get_state().json_output:
        payload = {"error": error}
        if ref is not None:
            payload["ref"] = ref
        if hint is not None:
            payload["hint"] = hint
        if detail is not None:
            payload["detail"] = detail
        typer.echo(json.dumps(payload, separators=(",", ":")))
    else:
        stderr_console().print(f"✗ {message}")
    raise typer.Exit(exit_code)
