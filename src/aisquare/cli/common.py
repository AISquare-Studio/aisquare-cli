"""Shared CLI parsing and rendering helpers."""

from __future__ import annotations

import errno
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import NoReturn

import tomli_w
import typer
from rich.table import Table

from aisquare.core import paths
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
    OnboardReport,
    Pool,
    ProjectInfo,
    PromptRecord,
    SetupReport,
    StatusReport,
    StreamInfo,
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
    total = record.user_count + record.project_count + record.stream_count
    console.print(f"Last injection: {record.injected_at.isoformat()}")
    console.print(
        f"  {total} entries — {record.user_count} from your user pool, "
        f"{record.project_count} from this project, "
        f"{record.stream_count} via streams"
    )
    if record.streams:
        console.print(f"  streams in scope: {', '.join(record.streams)}")


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


def emit_streams(streams: list[StreamInfo]) -> None:
    """Render the stream list — a JSON array under ``--json``, a table otherwise."""
    if get_state().json_output:
        typer.echo(json.dumps([stream.model_dump(mode="json") for stream in streams]))
        return
    if not streams:
        stdout_console().print("No streams yet. Create one with: aisquare stream new NAME")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("NAME")
    table.add_column("ID", no_wrap=True)
    table.add_column("PROJECTS", justify="right")
    table.add_column("REQUIRES", justify="right")
    for stream in streams:
        table.add_row(stream.name, stream.id, str(len(stream.members)), str(len(stream.requires)))
    stdout_console().print(table)


def emit_stream_detail(
    stream: StreamInfo,
    members: list[ProjectInfo],
    required_names: list[str],
    entry_count: int,
) -> None:
    """Render one stream in full — JSON under ``--json``, a key/value view otherwise."""
    if get_state().json_output:
        payload = stream.model_dump(mode="json")
        payload["member_roots"] = [str(project.root) for project in members]
        payload["requires_names"] = required_names
        payload["entry_count"] = entry_count
        typer.echo(json.dumps(payload))
        return
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    grid.add_row("name", stream.name)
    grid.add_row("id", stream.id)
    if required_names:
        grid.add_row("requires", ", ".join(required_names))
    grid.add_row("entries", str(entry_count))
    console = stdout_console()
    console.print(grid)
    if members:
        console.print()
        for project in members:
            console.print(f"  {project.root}")


def emit_stream_action(message: str, stream: StreamInfo) -> None:
    """Confirm a stream action — the stream as JSON under ``--json``, a message otherwise."""
    if get_state().json_output:
        typer.echo(stream.model_dump_json())
    else:
        stdout_console().print(message)


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
    """Render the full config — JSON under ``--json``, TOML otherwise.

    ``exclude_none`` on the TOML side for the reason ``save_config`` states:
    TOML has no null, so ``tomli_w`` raises rather than writing anything, and
    one unset optional field would make the whole config unprintable. It was
    passed on the write path and not here, so ``explainability enable`` (a
    target that overrides nothing) or ``team bind`` without ``--bin`` left
    ``config list`` exiting 1 with a traceback. Omitting the key is also what
    is on disk, so the two renderings agree.

    JSON keeps its nulls: it can express them, and a consumer indexing a key
    would rather read ``null`` than a ``KeyError``.
    """
    if get_state().json_output:
        typer.echo(config.model_dump_json())
    else:
        typer.echo(tomli_w.dumps(config.model_dump(mode="json", exclude_none=True)), nl=False)


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
    shipping = report.shipping
    if shipping is not None:
        console.print(
            f"shipping: {shipping.queued} queued, {shipping.sent} sent, "
            f"{shipping.dead} dead-letter — {shipping.reason}"
        )


_CHECK_SYMBOL = {CheckStatus.ok: "✓", CheckStatus.warn: "⚠", CheckStatus.fail: "✗"}


def emit_doctor(checks: list[DoctorCheck]) -> None:
    """Render diagnostic check results, with a fix hint for anything not OK.

    Details and fixes are DATA, not markup: a check may carry text from a
    subprocess or another tool, and Rich reads anything in square brackets as a
    style tag and prints nothing. Observed live — the SDK doctor reports a
    configured key as ``[present]``, which rendered as an empty detail and read
    exactly like a missing key.

    They are no longer run through ``rich.markup.escape``. The output consoles
    stopped parsing markup at all (see :mod:`aisquare.core.console`), and
    escaping for a parser that is switched off prints the backslash it was
    meant to hide. The protection moved down a layer; the reason for it did not
    change.
    """
    if get_state().json_output:
        typer.echo(json.dumps([check.model_dump(mode="json") for check in checks]))
        return
    console = stdout_console()
    for check in checks:
        console.print(f"{_CHECK_SYMBOL[check.status]} {check.name}: {check.detail}")
        if check.fix and check.status is not CheckStatus.ok:
            console.print(f"    → {check.fix}")


@contextmanager
def expected_config_write_errors() -> Iterator[None]:
    """Route a foreseeable "config is not writable" failure through ``fail``.

    ``PermissionError`` out of ``save_config`` is not a crash: it is the
    operator's filesystem saying no, and this CLI already has a convention for
    that — one ``✗`` line and exit 1. Without this it arrived as 56 lines of
    Rich traceback with the useful sentence at the bottom, which is the shape
    operators skip past.

    Deliberately NOT a catch in ``main()``. A global handler would tidy
    UNEXPECTED OSErrors the same way, and an unexpected OSError is a bug where a
    traceback is the correct output — burying one costs whoever debugs it later
    far more than a buried message costs an operator now. So only the commands
    that KNOW this failure is foreseeable translate it, and only
    ``PermissionError``: every other OSError keeps its traceback.

    ``hint`` and ``detail`` reach the JSON payload; the human surface prints the
    message alone, so the directory that actually needs permission is named
    there rather than left to the hint.
    """
    try:
        yield
    except PermissionError as exc:
        config = paths.config_path()
        resolved = Path(exc.filename) if exc.filename else None
        directory = resolved.parent if resolved is not None else config.parent
        through = (
            f" (a symlink to {resolved})"
            if resolved is not None and resolved != config and resolved.name == config.name
            else ""
        )
        fail(
            f"cannot write the config at {config}{through}: "
            f"write permission is needed on {directory}",
            error="config_not_writable",
            hint=f"write permission is needed on {directory}",
            detail=exc.strerror or str(exc),
        )
    except FileNotFoundError as exc:
        # ``save_config`` raises this deliberately when a followed symlink's
        # directory is missing: following a link is honouring stated intent,
        # materialising a tree the user never created would be inventing it.
        # Its message already names the missing directory and both remedies, so
        # this ROUTES the message rather than rewriting it — a second wording
        # would drift from the one the tests over there pin.
        fail(
            exc.strerror or str(exc),
            error="config_target_missing",
            hint=f"create {exc.filename} or repoint the link" if exc.filename else None,
        )
    except OSError as exc:
        # EROFS belongs with the two above by the same test: it is a consequence
        # of WHERE THE OPERATOR POINTED THE CONFIG, and a one-line message can
        # name the fix. ENOSPC and EIO are the machine breaking underneath a
        # correct choice — tidying those into a ✗ would understate a condition
        # that is probably breaking other things too, so they keep the traceback
        # that is the honest signal. Re-raised explicitly rather than caught by
        # class, because EROFS has no dedicated exception type.
        if exc.errno != errno.EROFS:
            raise
        where = Path(exc.filename).parent if exc.filename else paths.config_path().parent
        fail(
            f"cannot write the config: {where} is on a read-only filesystem",
            error="config_filesystem_read_only",
            hint=f"remount {where} read-write, or point AISQUARE_HOME elsewhere",
        )


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
    ``detail`` the underlying cause (e.g. the real sqlite error text).

    **Both reach the JSON payload ONLY.** The human surface prints
    ``✗ {message}`` and nothing else — so ANYTHING AN OPERATOR MUST ACT ON
    BELONGS IN ``message``. This docstring used to claim the human message
    "weaves both into its own text", which was never true of the code below
    it, and the cost of that sentence is specific: it invites the next author
    to put the one actionable token of a diagnosis into ``hint``, where the
    only people who will ever see it are reading ``--json``. Caught by
    ``coder3`` while implementing a message whose whole purpose was to stop an
    operator looking at the wrong directory.
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
        # markup=False because an error message is DATA, not a template. Rich
        # reads `[...]` as a style tag and deletes it silently, which rendered
        # the serve hint as `pip install 'aisquare-cli'` — the extra name, the
        # one actionable token in the sentence, gone. Every other fail message
        # interpolates user-controlled text (paths, refs, role names, config
        # values), so this was a class of silent mangling, not one bad string.
        stderr_console().print(f"✗ {message}", markup=False)
    raise typer.Exit(exit_code)
