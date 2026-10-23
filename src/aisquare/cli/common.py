"""Shared CLI parsing and rendering helpers."""

from __future__ import annotations

import errno
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import tomli_w
import typer
from rich import box
from rich.table import Table
from rich.text import Text

from aisquare.core import paths
from aisquare.core import snapshot as snapshot_core
from aisquare.core.claude_accounts import format_reset as _format_reset
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
    PendingRevocation,
    Pool,
    ProjectForgetReport,
    ProjectInfo,
    ProjectPruneReport,
    PromptRecord,
    SetupReport,
    StatusReport,
    TurnMetric,
)

if TYPE_CHECKING:  # the ledger's model only, never loaded for another command
    from aisquare.services.token_ledger import SessionLedger

_DEFAULT_EMPTY = 'No context entries yet. Add one with: aisquare remember "…"'


USAGE_ERROR: type[Exception] = typer.BadParameter.__bases__[0]
"""Click's ``UsageError``, for a usage error raised by hand: exit 2, and ``{"error": "usage"}``
under ``--json``. Click is vendored (``typer._click``), and ``import click`` is another
package whose errors typer does not catch, so like tests/cli_tree.py this stays on the
public typer surface: ``UsageError`` is the direct base of ``typer.BadParameter``. One
copy: ``cli.global_flags`` and ``cli.fleet`` each reached it, and a change to the bases
would have had to find both (review of #266, round 3)."""


def refuse_conflicting_scope(every: bool, project: str | None) -> None:
    """``--all`` and ``--project`` name different scopes; both at once is refused.

    The mechanism, not one command's guard: ``--all`` used to win silently in
    ``fleet shutdown`` (meant as one project, took every fleet down) and still
    did in ``metrics show``/``list`` after that was patched per command (rounds
    4 and 6 of #203). One helper, one wording, one exit code — 2, a usage
    error, which the root group renders as ``{"error": "usage", …}`` on stdout
    under ``--json`` (round 7: a caller that asked for JSON gets JSON or nothing).
    """
    if every and project is not None:
        raise typer.BadParameter(
            "--all and --project conflict: --all is every project, --project one project — "
            "drop one of them",
            param_hint="--all",
        )


def project_for_ref(ref: str) -> ProjectInfo:
    """The project a ``--project`` names, by codename, name or id prefix; a usage error if none.

    Exit 2, as for every usage error, and not ``fail``'s 1: ``explainability
    status`` exits 1 for a red proxy lane and ``doctor`` for a failed check, so
    a cutover script gating on ``$?`` read a typo'd ``--project`` as either one
    (review of #170's follow-ups, round 1, F6). One lookup for the
    explainability commands and ``doctor``, which carried a copy of it (F9).
    The JSON error stays ``not_found`` or ``ambiguous_project``.

    The project service is imported here, not at module scope: this module is
    imported by every command, and the service brings the store (``sqlite3``,
    ``hashlib``) with it, which the commands that never name a project do not
    pay for (tests/test_import_cost_of_the_integration.py).
    """
    from aisquare.services import project as project_service  # lazy: see above

    try:
        return project_service.resolve(ref)
    except KeyError:
        fail(f"no project matches '{ref}'", error="not_found", ref=ref, exit_code=2)
    except ValueError as exc:
        fail(str(exc), error="ambiguous_project", ref=ref, exit_code=2)


def local_time(value: datetime) -> datetime:
    """A stored (UTC) timestamp in the user's local timezone, for display."""
    return value.astimezone()


# The formatter itself lives in core (``core.claude_accounts.format_reset``) so the
# services — the board's ``limited`` line, the agent detail, doctor — render a reset
# the same way the two account surfaces do; re-exported here, where they import it.
format_reset = _format_reset


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
        if record.retrieved_items:
            console.print("  items: " + ", ".join(record.retrieved_items))


def emit_project_detail(project: ProjectInfo) -> None:
    """Render one project — JSON under ``--json``, a key/value view otherwise."""
    if get_state().json_output:
        typer.echo(project.model_dump_json())
        return
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    grid.add_row("name", Text(_project_name(project)))
    grid.add_row("id", project.id)
    grid.add_row("root", str(project.root))
    if project.linked_repos:
        grid.add_row("repos", ", ".join(project.linked_repos))
    stdout_console().print(grid)


def _project_name(project: ProjectInfo) -> str:
    """``services.project.display_name``: the nickname, else the directory, else the id.

    Imported here, not at module scope, for :func:`project_for_ref`'s reason. A
    nickname is free text, so a line that prints it prints it as data
    (``markup=False``, a :class:`~rich.text.Text` cell): ``[wip]`` is not a style.
    """
    from aisquare.services.project import display_name  # lazy: see project_for_ref

    return display_name(project)


def projects_json(
    projects: list[ProjectInfo], *, group_names: Mapping[str, str] | None = None
) -> list[dict[str, object]]:
    """The ``project list --json`` payload — shared with the Remote Control server verbatim."""
    names = group_names or {}
    return [
        {
            **project.model_dump(mode="json"),
            "name": _project_name(project),
            "group": names.get(project.group_id or "", project.group_id),
            "pinned": project.pinned_at is not None,
        }
        for project in projects
    ]


def emit_projects(
    projects: list[ProjectInfo],
    *,
    active_id: str | None,
    hidden: int = 0,
    group_names: Mapping[str, str] | None = None,
    filtered: str | None = None,
) -> None:
    """Render the project list — a JSON array under ``--json``, a table otherwise.

    The JSON carries the same ``name`` the table shows (#83): it is derived from
    the root rather than stored on the model, and a script picking a project
    by name had nothing to pick on. It also carries ``group`` (the name),
    ``position`` and ``pinned`` (#140); the table shows a GROUP column and a
    📌 marker only when something is grouped or pinned. ``hidden`` is how many
    captured directories the list leaves out (#139); only an empty table
    mentions them. ``filtered`` is what an empty table says when ``--group`` or
    ``--pinned`` left nothing of a list that has rows: neither "nothing
    registered" nor the captured count is true then.
    """
    names = group_names or {}
    if get_state().json_output:
        typer.echo(json.dumps(projects_json(projects, group_names=names)))
        return
    if not projects and filtered:
        stdout_console().print(filtered)
        return
    if not projects and hidden:
        # "nothing registered, run init" was wrong for a machine whose hooked
        # sessions captured directories that are simply not listed (#139).
        noun = "directory" if hidden == 1 else "directories"
        stdout_console().print(
            f"No projects added yet — {hidden} captured {noun} hidden (a hooked session ran "
            "there): aisquare project list --all; add one: aisquare project onboard <path>"
        )
        return
    if not projects:
        stdout_console().print("No projects registered yet. Run: aisquare init")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("", no_wrap=True)
    table.add_column("NAME")
    table.add_column("ID", no_wrap=True)
    table.add_column("ROOT")
    # The column exists only when a captured row is in the list (`--all`), so
    # the everyday table is unchanged (#139).
    captured = any(project.onboarded_at is None for project in projects)
    if captured:
        table.add_column("LISTED", no_wrap=True)
    arranged = any(project.group_id or project.pinned_at for project in projects)
    if arranged:
        table.add_column("GROUP", no_wrap=True)
    for project in projects:
        marker = "*" if project.id == active_id else ""
        if project.pinned_at is not None:
            marker = (marker + "📌").strip()
        # display_name's order, with a dash for the id the next column shows anyway.
        name = Text(project.nickname or project.root.name or "—")
        cells: list[str | Text] = [marker, name, project.id, str(project.root)]
        if captured:
            cells.append("captured" if project.onboarded_at is None else "yes")
        if arranged:
            cells.append(names.get(project.group_id or "", project.group_id or "") or "")
        table.add_row(*cells)
    stdout_console().print(table)
    if captured:
        stdout_console().print(
            "captured = a hooked session ran there; add it on purpose to list it "
            "(aisquare project onboard <path>), or drop the stale ones: "
            "aisquare project prune --captured-only"
        )


def emit_project_action(message: str, project: ProjectInfo) -> None:
    """Confirm a project action — the project as JSON under ``--json``, a message otherwise."""
    if get_state().json_output:
        typer.echo(project.model_dump_json())
    else:
        stdout_console().print(message, markup=False)  # a nickname is text, not markup


def _active_note(
    active: ProjectInfo | None, *, changed: bool, pin_error: str | None = None
) -> str | None:
    """One line saying where the active project went, or None when it did not move.

    A pin the state file refused is said here rather than as a failure: the
    forget (or the sweep) is complete, and only where the pin landed is in doubt.
    """
    if not changed:
        return None
    if pin_error is not None:
        return (
            f"⚠ the pin could not be moved — {pin_error}; the active project follows your "
            "working directory until `project switch` succeeds"
        )
    if active is None:
        return "no projects remain — the active project follows your working directory again"
    return (
        f"active project is now {_project_name(active)} ({active.id}), "
        "the most recently touched one left"
    )


def emit_project_forget(report: ProjectForgetReport) -> None:
    """Render ``project forget`` — the report as JSON under ``--json``, lines otherwise."""
    if get_state().json_output:
        typer.echo(report.model_dump_json())
        return
    console = stdout_console()
    project = report.project
    console.print(
        f"✓ forgot {_project_name(project)} ({project.id}) at {project.root}", markup=False
    )
    if report.purged:
        counts = ", ".join(f"{count} {table}" for table, count in report.removed.items() if count)
        console.print(f"  deleted: {counts or 'no rows'}")
        if report.data_dir_removed:
            console.print(f"  deleted: {paths.project_data_dir(project.id)}")
    else:
        console.print(
            "  its context entries, prompt history and board rows stay in the store, hidden "
            "— --purge deletes them; registering the root again brings them back"
        )
    _say_keys_still_live(report.keys_still_live)
    note = _active_note(report.active, changed=report.active_changed, pin_error=report.pin_error)
    if note is not None:
        console.print(f"  {note}", markup=False)


def _say_keys_still_live(owed: list[PendingRevocation]) -> None:
    """A purge whose minted keys (#142) could not be revoked yet: which, why, and who retries."""
    if not owed:
        return
    from aisquare.services import destinations  # lazy: the explainability modules

    stdout_console().print(
        f"  ⚠ {destinations.describe_owed(owed)} — {destinations.REVOKE_RETRY}", markup=False
    )


def emit_prune(report: ProjectPruneReport) -> None:
    """Render ``project prune`` — the report as JSON under ``--json``, a table otherwise.

    The same renderer serves the plan and the result: both show every candidate
    with its reason, the plan stopping there and the result adding what was
    dropped. A ``--yes`` caller never saw a plan, so the result has to be the
    plan as well.
    """
    if get_state().json_output:
        typer.echo(report.model_dump_json())
        return
    console = stdout_console()
    if not report.candidates:
        console.print("nothing to prune")
        return
    # Each kept row's reason as the run re-read it (``prune``'s ``kept``), not the plan's copy
    # in ``candidates``: a hold that changed while the prompt sat open read "kept: 0 live
    # agent(s)" beside a summary that named a hub (review of #259, round 34, 6/10). A dry
    # run's ``kept`` is the plan's own.
    kept = {candidate.project.id: candidate for candidate in report.kept}
    # A run marks what it dropped, not what it did not keep: a candidate forgotten elsewhere
    # while the prompt sat open is in neither list, and was marked ✓ beside a count that left
    # it out (review of #259, round 35, 4/8).
    dropped = set(report.dropped)
    changed = set(report.changed)
    forgotten = set(report.forgotten)
    root_gone = set(report.root_gone)
    table = Table(show_header=True, header_style="bold")
    table.add_column("", no_wrap=True)
    table.add_column("WHY", no_wrap=True)
    table.add_column("NAME")
    table.add_column("ID", no_wrap=True)
    table.add_column("ROOT")
    for candidate in report.candidates:
        project = candidate.project
        why: str = candidate.reason
        if candidate.principal is not None:
            why = f"worktree of {_project_name(candidate.principal)}"
        held = kept.get(project.id)
        if held is not None and held.read_by:
            # Not "the board api, web's fleet agents read", as if the last one's alone (review
            # of #259, round 36, 6/9); the names early, before a narrow terminal cuts the cell.
            whose = ", ".join(_project_name(reader) for reader in held.read_by)
            why += f" (kept: the fleet agents of {whose} read its board)"
        elif held is not None:
            why += f" (kept: {held.live_agents} live agent(s))"
        elif project.id in root_gone:
            # Not "changed since the plan" alone: its root went, and --missing drops it (round
            # 39, 5/9).
            why += " (kept: its root went since the plan)"
        elif project.id in changed:
            # Added, given an entry or back on disk while the prompt sat open (round 37, 2/7).
            why += " (kept: changed since the plan)"
        elif not report.dry_run and project.id not in dropped:
            # By its fate, not this run's --purge (round 41, 6/6): one the run found forgotten
            # (a purge purges one still stale, round 36, 5/9, and leaves the others, round 39,
            # 4/9; a plain run passes over each), else one purged since the plan. Said
            # forgotten whenever the run was plain, a purged row read as one an onboard
            # brings back.
            fate = "forgotten" if project.id in forgotten else "purged"
            why += f" (gone already: {fate} since the plan)"
        marker = ""
        if not report.dry_run:
            kept_here = held is not None or project.id in changed
            marker = "✓" if project.id in dropped else "·" if kept_here else "-"
        table.add_row(
            marker, Text(why), Text(_project_name(project)), project.id, str(project.root)
        )
    console.print(table)
    if report.dry_run:
        return
    count = len(report.dropped)
    noun = "registration" if count == 1 else "registrations"
    what = "purged" if report.purged else "forgot"
    console.print(f"✓ {what} {count} {noun}")
    hubs = sum(bool(candidate.read_by) for candidate in report.kept)
    if len(report.kept) > hubs:
        console.print(
            f"  kept {len(report.kept) - hubs} with live fleet agents — stop or reap them first"
        )
    if hubs:
        console.print(
            f"  kept {hubs} whose board another project's fleet agents read, as a hub's — "
            "its row names each project"
        )
    if len(report.changed) > len(report.root_gone):
        stale_no_more = len(report.changed) - len(report.root_gone)
        console.print(f"  kept {stale_no_more} that changed since the plan: no longer stale")
    if report.root_gone:
        # Each row's own forget, as doctor's capture-off line names them: `prune --missing`
        # drops every registration whose root is missing, not only these, and under --purge
        # deletes each one's entries, prompts and data directory (round 41, 5/6). With this
        # run's --purge: a plain forget only forgets them, and nothing lists a forgotten row
        # to purge it after (round 40, 2/6).
        from aisquare.services.project import forget_command  # lazy: see project_for_ref

        roots = {candidate.project.id: candidate.project.root for candidate in report.candidates}
        forgets = [forget_command(roots[gone], purge=report.purged) for gone in report.root_gone]
        drop = f"drop it: {forgets[0]}" if len(forgets) == 1 else f"drop them: {'; '.join(forgets)}"
        console.print(
            f"  kept {len(report.root_gone)} whose root went since the plan, which this prune "
            f"did not ask for — {drop}",
            markup=False,
        )
    if not report.purged and count:
        console.print(
            "  their context entries, prompt history and board rows stay in the store, hidden "
            "— --purge deletes them"
        )
    _say_keys_still_live(report.keys_still_live)
    note = _active_note(report.active, changed=report.active_changed, pin_error=report.pin_error)
    if note is not None:
        console.print(f"  {note}", markup=False)


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


def emit_config_overrides(overrides: Mapping[str, Any]) -> None:
    """Render the settings the user changed — a JSON object under ``--json``, TOML otherwise.

    The TOML goes through ``typer.echo`` as :func:`emit_config`'s does, never the Rich
    console, which would take ``[fleet.roles.coder]`` for markup and print nothing of
    it. Nothing changed prints one TOML comment, so the output still parses.
    """
    if get_state().json_output:
        typer.echo(json.dumps(overrides))
    elif overrides:
        typer.echo(tomli_w.dumps(dict(overrides)), nl=False)
    else:
        typer.echo("# no overrides: every setting is at its system default")


def emit_config_reset(config: Path, backup: Path | None) -> None:
    """Report ``config reset``: where the user's file went, or that there was nothing to reset."""
    if get_state().json_output:
        payload = {"config": str(config), "reset": backup is not None, "backup": None}
        if backup is not None:
            payload["backup"] = str(backup)
        typer.echo(json.dumps(payload))
    elif backup is None:
        stdout_console().print(
            f"nothing to reset: {config} holds no setting, so every setting is at its "
            "system default"
        )
    else:
        stdout_console().print(
            f"✓ every setting is back at its system default; your file is kept as {backup}"
        )


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
    whose hooks went missing — name the broken ones explicitly. A dir whose
    settings switch every hook off is named as that, and so is one whose files
    `agents connect` refuses, with connect's reason as the doctor gives it (the file
    and why, or that the directory does not exist): "missing" pointed at Connect,
    which cannot change either, and the directory alone named no file (review of
    #257).
    """
    if not agent.sites:
        return "—"
    off = [site.config_dir for site in agent.sites if site.hooks_off is not None]
    down = [site for site in agent.sites if not site.hooks_installed and site.hooks_off is None]
    missing = [site.config_dir for site in down if site.refused is None]
    if not off and not down:
        if len(agent.sites) == 1:
            return str(agent.sites[0].config_dir)
        return f"{len(agent.sites)} dirs, all ok"
    clauses = [
        f"{what} in {', '.join(str(path) for path in dirs)}"
        for what, dirs in (("missing", missing), ("switched off", off))
        if dirs
    ]
    clauses.extend(
        f"cannot be written in {site.config_dir}: {site.refused}"
        + (f" — {'; or '.join(site.remedies)}" if site.remedies else "")
        for site in down
        if site.refused is not None
    )
    ok = len(agent.sites) - len(off) - len(down)
    return f"{ok}/{len(agent.sites)} ok — {'; '.join(clauses)}"


def emit_connected(connection: AgentConnection) -> None:
    """Confirm an agent connection: hook install + context ingested.

    Hooks installed where the settings switch every hook off are not a connection:
    Claude Code runs none of them until that key goes, so the line says so and
    names the file (review of #257).
    """
    if get_state().json_output:
        typer.echo(connection.model_dump_json())
        return
    # Always installed: `connect` refuses rather than return a connection without hooks.
    noun = "entry" if connection.imported == 1 else "entries"
    imported = f"imported {connection.imported} {noun}"
    if connection.hooks_off is not None:
        stdout_console().print(
            f"hooks installed for {connection.name}, but switched off — {imported}"
        )
        stderr_console().print(
            f'note: {connection.hooks_off} sets "disableAllHooks": true, so Claude Code runs '
            "none of its hooks, aisquare's included — remove that key to turn them on"
        )
        return
    stdout_console().print(f"✓ connected {connection.name} — hooks installed; {imported}")


def emit_onboard(report: OnboardReport) -> None:
    """Render the outcome of ``project onboard`` — snapshot summary + seeded facts."""
    if get_state().json_output:
        typer.echo(report.model_dump_json())
        return
    console = stdout_console()
    snapshot = report.snapshot
    if report.snapshot_note is not None:
        # Nothing packed: off, a pack that failed and why, or a --refresh that kept
        # the last pack -- the service's sentence, which `init` prints too.
        console.print(f"snapshot: {report.snapshot_note}")
    elif snapshot is not None and snapshot.status == "ready":
        line = f"✓ snapshot: {snapshot.file_count} files, {snapshot.token_count} tokens"
        if snapshot.skeleton_token_count:
            line += f" (skeleton {snapshot.skeleton_token_count} tokens)"
        console.print(line)
    elif snapshot is not None and snapshot.status == "skeleton_only":
        console.print(f"snapshot: {snapshot_core.skeleton_only_detail(snapshot)}")
    elif snapshot is not None and snapshot.status == "too_large" and snapshot_core.can_pack():
        # The same sentence the doctor prints (one definition, so the two never
        # disagree on the numbers), plus what to run once a remedy is in place.
        # Where nothing can pack, no remedy can run: the verdict reads off below,
        # as the doctor's row does.
        console.print(
            f"snapshot: {snapshot_core.too_large_detail(snapshot)} {snapshot_core.REPACK_HINT}"
        )
    else:
        console.print(f"snapshot: {snapshot_core.skipped_detail()}")
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
    """Render a metrics summary — JSON under ``--json``, a table otherwise.

    The three reason groups are three rows, never one: a baseline run with the
    experiment off must not read as one where every call failed.
    """
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
    if summary.by_trigger:
        table.add_row("by trigger", _counts(summary.by_trigger))
    table.add_row("CI consulted", str(summary.consulted))
    table.add_row("baseline (never asked)", str(summary.baseline))
    table.add_row("skipped by design", str(summary.skipped))
    table.add_row("failed", str(summary.failed))
    table.add_row("turns with injection", str(summary.injected_turns))
    table.add_row("deadline breaches", str(summary.deadline_breaches))
    table.add_row("median wall", _ms(summary.median_wall_ms))
    table.add_row("median round trip", _ms(summary.median_round_trip_ms))
    table.add_row("p95 round trip", _ms(summary.p95_round_trip_ms))
    console.print(table)

    if summary.by_reason:
        console.print(f"reasons: {_counts(summary.by_reason)}")
    if summary.by_status:
        console.print(f"server statuses: {_counts(summary.by_status)}")
    if summary.deadline_breaches:
        console.print(
            f"⚠ {summary.deadline_breaches} turn(s) hit the client ceiling — "
            "that is a server-side latency problem, not a result"
        )
    override_turns = summary.by_delivery_source.get("override", 0)
    if override_turns:
        console.print(
            f"⚠ {override_turns} turn(s) ran under the staging delivery override — "
            "kept out of the round-trip figures; they measure nothing"
        )
    if not summary.turns_with_tokens:
        console.print(
            "note: no token counts recorded — hook payloads do not carry them (they will "
            "come from Explainability spans), so token savings cannot be read from this yet"
        )


def _counts(bucket: dict[str, int]) -> str:
    return ", ".join(f"{key} {count}" for key, count in sorted(bucket.items()))


def emit_token_ledger(rows: list[SessionLedger], summed: SessionLedger) -> None:
    """Render the token ledger — ``{"sessions": [...], "total": {...}}`` under ``--json``, a
    table with a total row otherwise. Counts only: no path, no prompt, no text."""
    if get_state().json_output:
        typer.echo(
            json.dumps({"sessions": [row.as_counts() for row in rows], "total": summed.as_counts()})
        )
        return
    if not rows:
        stdout_console().print("No agent sessions recorded for this scope.")
        return
    table = Table(show_header=True, header_style="bold", box=box.SIMPLE_HEAD)
    for column in ("SESSION", "ROLE"):
        table.add_column(column, no_wrap=True)
    for column in ("REQ", "INPUT", "BASELINE", "PEAK", "WAKES", "DELTA CH", "COMPACT"):
        table.add_column(column, justify="right", no_wrap=True)
    for row in [*rows, summed]:
        mine = row is not summed
        table.add_row(
            row.session_id[:8] if mine else "total",
            (row.role or "") + (" (no transcript)" if mine and not row.transcript_found else ""),
            f"{row.requests:,}",
            f"{row.input_total:,}",
            f"{row.baseline:,}" if row.baseline is not None else "—",
            f"{row.peak_context:,}",
            f"{row.wakes}/{row.no_action_wakes}",
            f"{row.delta_chars:,}",
            str(row.compactions),
        )
    console = stdout_console()
    console.print(table)
    console.print(
        "INPUT is every request's input summed (fresh + cache write + cache read); "
        "WAKES is wakes/no-action wakes. Every field: --json",
        style="dim",
    )


def emit_turn_metrics(turns: list[TurnMetric]) -> None:
    """Render recent turns — a JSON array under ``--json``, a table otherwise."""
    if get_state().json_output:
        typer.echo(json.dumps([turn.model_dump(mode="json") for turn in turns]))
        return
    if not turns:
        stdout_console().print("No turns recorded yet.")
        return
    # The trace id is printed WHOLE and never wraps: it is the join key to the
    # server's ledger and to `refs/aisquare/wip/`, and a truncated id cannot be
    # looked up anywhere. The vocabulary columns wrap instead when the terminal
    # is narrow.
    table = Table(show_header=True, header_style="bold")
    table.add_column("WHEN", no_wrap=True)
    table.add_column("TRACE", no_wrap=True, min_width=30)
    table.add_column("TRIGGER", overflow="fold")
    table.add_column("REASON", overflow="fold")
    table.add_column("STATUS", overflow="fold")
    table.add_column("ACTION", overflow="fold")
    # Which document ruled delivery. A consulted row under the staging override
    # must never read as one the descriptor allowed (services/ci_override.py).
    table.add_column("SOURCE", overflow="fold")
    table.add_column("WALL", justify="right")
    table.add_column("TRIP", justify="right")
    for turn in turns:
        table.add_row(
            local_time(turn.started_at).strftime("%m-%d %H:%M:%S"),
            turn.trace_id,
            turn.trigger or "—",
            turn.client_reason.value,
            turn.status or "—",
            turn.action or "—",
            turn.delivery_source or "—",
            _ms(turn.wall_ms),
            _ms(turn.round_trip_ms),
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
    hidden = (
        f" (+{report.captured_count} captured, hidden: aisquare project list --all)"
        if report.captured_count
        else ""
    )
    console.print(
        f"context:  {report.user_entries} user, {report.project_entries} in this project; "
        f"{report.project_count} project(s) registered{hidden}"
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


def home_blocker() -> Path | None:
    """The non-directory standing at or above the aisquare home, if there is one.

    Walks up from ``AISQUARE_HOME`` to the first path that exists: if that is a
    directory, ``ensure_home`` can create the rest, and this returns ``None``.
    If it is a file, that file is why the home cannot come into being — the case
    where ``AISQUARE_HOME`` points AT a file and the case where it points THROUGH
    one both land here, and both name the file the operator has to move.

    A DANGLING SYMLINK counts as a blocker: ``exists()`` follows links and says
    no, but ``mkdir`` still refuses with ``FileExistsError``, so treating it as
    "nothing there" would send exactly that case back to a traceback.

    Cheap by construction (a handful of stats, no mkdir) and never raises, so it
    is safe to consult from an exception handler.
    """
    home = paths.aisquare_home()
    for candidate in (home, *home.parents):
        try:
            if candidate.is_dir():
                return None
            if candidate.exists() or candidate.is_symlink():
                return candidate
        except OSError:  # an unstattable ancestor is not a diagnosis we can make
            return None
    return None


def _fail_home_not_creatable(exc: OSError, blocker: Path | None = None) -> NoReturn:
    """The one-line refusal for a home that cannot be created.

    Shared by both guards below so the message and the ``home_not_creatable``
    code cannot drift between the config-write path and the every-command path.
    """
    where = blocker or exc.filename or paths.aisquare_home()
    fail(
        f"cannot create the aisquare home: {where} is in the way "
        f"({exc.strerror or type(exc).__name__})",
        error="home_not_creatable",
        hint="AISQUARE_HOME must point at a directory (it may not exist yet) — "
        "move the file, or point AISQUARE_HOME elsewhere",
        detail=exc.strerror or str(exc),
    )


@contextmanager
def expected_home_creation_errors() -> Iterator[None]:
    """Route ``ensure_home`` failing on a home that cannot exist through ``fail``.

    Wider reach than :func:`expected_config_write_errors`, and it needs to be.
    ``core.store.open_store`` calls ``paths.ensure_home()`` unconditionally, so
    EVERY command that touches the store hits this failure — not just the four
    that write config. Measured with ``AISQUARE_HOME=<a regular file>``:
    ``aisquare --json init <dir>`` printed the one refusal line, while
    ``aisquare --json fleet ls`` printed 97 lines of Rich traceback with an EMPTY
    stdout (``project list``: 83, ``status``: 86), which breaks the ``--json``
    contract the branch was added to protect and leaves the fleet UI with nothing
    to report.

    Still not a general tidier, which is what ``CONTRIBUTING`` forbids: an
    unexpected ``OSError`` is a bug and a traceback is the correct output for one.
    Two things keep this narrow. The exception classes are the two
    ``mkdir``/``open`` raises for a non-directory in the path, and the handler
    only translates when :func:`home_blocker` confirms the operator's home really
    is blocked RIGHT NOW. A ``FileExistsError`` from anywhere else in a command,
    on a machine whose home is a healthy directory, is re-raised untouched.
    """
    try:
        yield
    except (FileExistsError, NotADirectoryError) as exc:
        blocker = home_blocker()
        if blocker is None:
            raise
        _fail_home_not_creatable(exc, blocker)


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
    except (FileExistsError, NotADirectoryError) as exc:
        # AISQUARE_HOME pointing at (or through) a FILE is a foreseeable operator
        # mistake, not a crash: ``ensure_home`` cannot mkdir over it. Measured
        # before this existed: ``aisquare --json init <dir>`` printed a Rich
        # traceback to stderr and NOTHING on stdout — a traceback under --json
        # breaks the machine contract, and the fleet UI's onboarding reads that
        # stdout to say why an onboard failed.
        #
        # Unconditional here, unlike in ``expected_home_creation_errors``: a
        # config write is one statement, this pair of exceptions has one cause on
        # that path, and narrowing an already-narrow site would only add a way to
        # miss it.
        _fail_home_not_creatable(exc)
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
