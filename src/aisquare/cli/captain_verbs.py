"""``aisquare captain`` verbs — the owner's own hands on the captain's tools, from any terminal.

Card T5. Each verb runs ONE captain tool through :func:`actions.perform`, the same
audited frame the captain's MCP tools use, so an action typed at a terminal is one
``captain_action`` event exactly like a spoken one, with the argv as its
``utterance``; ``aisquare captain log`` reads that audit back. The queue verbs
(``attention``, ``next``, ``resolve``, ``snooze``) read and move the attention queue
(T7); ``since`` is the watermark read; ``uav`` is ``attention`` with the sitrep
header; ``wololo`` and ``bt`` are the two easter eggs the contract names;
``actions`` lists the owner action list ``act`` runs from.

Human output by default; under ``--json`` the tool's own result, as the captain
sees it (``action_seq`` included), so the shapes are pinned once for both. A
refusal is ``✗ refused: … (action seq N)`` and exit 1 — ``{"error": "refused",
"detail": "refused: … (action seq N)"}`` under ``--json`` — never a traceback. None
of this needs the MCP SDK.

**Lazy on purpose.** Every ``aisquare`` command — a Claude Code hook included —
imports the CLI, and ``cli/captain.py`` registers these verbs at import; the
captain's actions, queue and state load only inside a verb that runs.
"""

from __future__ import annotations

import json
import shlex
import sqlite3
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, NoReturn

import typer
from rich.table import Table

from aisquare.cli.common import fail, local_time
from aisquare.core.console import stdout_console
from aisquare.core.state import get_state
from aisquare.core.store import store_session
from aisquare.services.captain.errors import Failed, Refused

if TYPE_CHECKING:
    from aisquare.models import ProjectInfo, TeamEvent

DEFAULT_LIMIT = 10
DEFAULT_SNOOZE_MINUTES = 15
LOG_DEFAULT = 20
LOG_MAX = 500
UAV_LINE = "UAV online"
"""The first line ``uav`` prints — the sitrep alias the contract names."""

Limit = Annotated[int, typer.Option("--limit", "-n", min=1, help="How many items to show.")]


TYPED: ContextVar[tuple[str, ...]] = ContextVar("captain_typed", default=())
"""The words after ``captain`` exactly as typed, set by the group around each call
(``cli/captain.py``) before click turns them into values."""


def _typed() -> str:
    """The command as the owner typed it, for the audit's ``utterance`` (13081).

    The process's own argv when it carries this call (it ends with the words the group
    kept), root flags and all. Otherwise — the CLI run in-process, as tests do — the
    root's ``--json`` when it is on and not typed after ``captain``, then the words as
    typed. ``-m 7`` stays ``-m 7`` and a default never appears. Shell-quoted, so the
    line can be run again.
    """
    import sys

    words = list(TYPED.get())
    argv = sys.argv[1:]
    if words and argv[-len(words) :] == words and "captain" in argv[: -len(words)]:
        return shlex.join(["aisquare", *argv])
    json_root = get_state().json_output and "--json" not in words
    return shlex.join(["aisquare", *(["--json"] if json_root else []), "captain", *words])


def _perform(tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
    """One tool through the audited frame; a refusal or failure ends the command in its words."""
    from aisquare.services.captain import actions

    try:
        return actions.perform(tool, args, _typed())
    except (Refused, Failed) as exc:
        _said_and_exit(exc)
    except (sqlite3.DatabaseError, OSError) as exc:
        fail(f"error: {exc}", error="store", detail=str(exc))


def _said_and_exit(exc: Refused | Failed) -> NoReturn:
    """The frame's own words, on both surfaces: the reason and the audit's seq are kept
    under ``--json`` as ``detail`` (a script needs both, not just the kind)."""
    kind = "failed" if isinstance(exc, Failed) else "refused"
    fail(str(exc), error=kind, detail=str(exc))


def _emit_json(data: Mapping[str, Any]) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False, default=str))


def _ago(stamp: object, now: datetime) -> str:
    """``3m``, ``2h``, ``1d`` since an ISO stamp; ``?`` when it is not one."""
    if not isinstance(stamp, str):
        return "?"
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _items_table(items: list[Any], now: datetime) -> Table:
    table = Table(box=None, pad_edge=False, header_style="bold")
    for column in ("id", "kind", "project", "agent", "waited", "asked", "what"):
        table.add_column(column)
    for item in items:
        row = item if isinstance(item, dict) else {}
        table.add_row(
            str(row.get("id", "")),
            str(row.get("kind", "")),
            str(row.get("project_name") or row.get("project", "")),
            str(row.get("agent") or "-"),
            _ago(row.get("first_seen"), now),
            str(row.get("count", "")),
            str(row.get("text", "")),
        )
    return table


def _print_items(items: list[Any], *, empty: str) -> None:
    console = stdout_console()
    if not items:
        console.print(empty)
        return
    console.print(_items_table(items, datetime.now(UTC)))


def _print_item(item: Mapping[str, Any] | None, *, verb: str) -> None:
    console = stdout_console()
    if item is None:
        console.print("nothing needs you")
        return
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    grid.add_row("id", str(item.get("id", "")))
    grid.add_row("kind", str(item.get("kind", "")))
    grid.add_row("project", str(item.get("project_name") or item.get("project", "")))
    if item.get("agent"):
        grid.add_row("agent", str(item["agent"]))
    grid.add_row("status", str(item.get("status", "")))
    first = _ago(item.get("first_seen"), datetime.now(UTC))
    grid.add_row("asked", f"{item.get('count', 1)} time(s), first {first} ago")
    if item.get("snoozed_until"):
        grid.add_row("snoozed until", str(item["snoozed_until"]))
    console.print(grid)
    console.print()
    console.print(str(item.get("text", "")))
    if verb != "next":
        console.print(f"{verb} · action seq {item.get('action_seq', '')}", style="dim")


# --- the verbs ------------------------------------------------------------------------------------


def register(app: typer.Typer) -> None:
    """Put the verbs on the ``captain`` group (``cli.captain``), one line there."""
    app.command("attention")(attention)
    app.command("next")(next_item)
    app.command("resolve")(resolve)
    app.command("snooze")(snooze)
    app.command("since")(since)
    app.command("log")(log)
    app.command("uav")(uav)
    app.command("wololo")(wololo)
    app.command("bt")(bt)
    app.command("actions")(action_list)


def attention(limit: Limit = DEFAULT_LIMIT) -> None:
    """What needs you across every project, most urgent first (the attention queue)."""
    result = _perform("attention", {"limit": limit})
    if get_state().json_output:
        _emit_json(result)
        return
    items = result.get("items")
    _print_items(items if isinstance(items, list) else [], empty="nothing needs you")


def next_item() -> None:
    """Item one: the single most urgent thing that needs you, or nothing."""
    result = _perform("next", {})
    if get_state().json_output:
        _emit_json(result)
        return
    item = result.get("item")
    _print_item(item if isinstance(item, dict) else None, verb="next")


def resolve(
    item: Annotated[str, typer.Argument(help="The item's id (a prefix is enough).")],
    how: Annotated[
        str, typer.Argument(help="What you did about it — a tell, a press, a decision.")
    ],
) -> None:
    """Mark an attention item resolved, recording what was done about it."""
    result = _perform("resolve", {"item": item, "how": how})
    if get_state().json_output:
        _emit_json(result)
        return
    row = result.get("item")
    resolved = row.get("id", item) if isinstance(row, dict) else item
    stdout_console().print(f"✓ resolved {resolved}: {how} · action seq {result.get('action_seq')}")


def snooze(
    item: Annotated[str, typer.Argument(help="The item's id (a prefix is enough).")],
    minutes: Annotated[
        int, typer.Option("--for", "-m", min=1, help="Minutes to hide it for.")
    ] = DEFAULT_SNOOZE_MINUTES,
) -> None:
    """Hide an attention item for a while; it comes back on its own."""
    result = _perform("snooze", {"item": item, "minutes": minutes})
    if get_state().json_output:
        _emit_json(result)
        return
    row = result.get("item")
    snoozed = row.get("id", item) if isinstance(row, dict) else item
    until = row.get("snoozed_until") if isinstance(row, dict) else None
    stdout_console().print(
        f"✓ snoozed {snoozed} for {minutes} min (until {until}) · "
        f"action seq {result.get('action_seq')}"
    )


def since(
    project: Annotated[str, typer.Argument(help="Project: id prefix, directory name or codename.")],
    agent: Annotated[
        str | None, typer.Option("--agent", "-a", help="One agent's events and its pane tail.")
    ] = None,
    advance: Annotated[
        bool, typer.Option("--advance", help="Move the watermark past what this shows.")
    ] = False,
) -> None:
    """Board events since the captain's watermark — what happened since you last looked."""
    args: dict[str, Any] = {"project": project, "agent": agent, "advance": advance}
    result = _perform("since", args)
    if get_state().json_output:
        _emit_json(result)
        return
    console = stdout_console()
    who = f"{project}/{agent}" if agent else project
    events = result.get("events")
    rows = events if isinstance(events, list) else []
    frm, to = result.get("from_seq"), result.get("to_seq")
    span = f", seq {'the start' if frm is None else frm} → {to}" if to is not None else ""
    console.print(  # the tool's own line: it says an agent that never joined, and a page more
        f"{who}: {result.get('said')}{span}"
        + (" · watermark advanced" if result.get("advanced") else "")
    )
    for event in rows:
        if not isinstance(event, dict):
            continue
        console.print(
            f"  {event.get('seq')}  {event.get('kind')}  {event.get('by') or '-'}: "
            f"{event.get('text')}"
        )
    pane = result.get("pane")
    if isinstance(pane, list) and pane:
        console.print("pane (untrusted):", style="bold")
        for line in pane:
            console.print(f"  {line}")
    if result.get("pane_error"):
        console.print(f"pane not read: {result['pane_error']}", style="yellow")
    if result.get("after_error"):
        console.print(str(result["after_error"]), style="yellow")


def log(
    project: Annotated[
        str | None,
        typer.Argument(help="One project's board; default: the home board and every project."),
    ] = None,
    limit: Annotated[
        int, typer.Option("--limit", "-n", min=1, max=LOG_MAX, help="How many.")
    ] = LOG_DEFAULT,
) -> None:
    """The captain's audit: every action it — or you, from a terminal — took, newest last."""
    from aisquare.services import fleet as fleet_service

    def read() -> tuple[dict[str, Any], str]:
        try:
            found = audit_log(project, limit=limit)
        except fleet_service.NoSuchProject as exc:
            raise Refused(str(exc)) from exc
        return {"events": found}, f"read {len(found)} captain action(s)"

    rows = _read_audited("log", {"project": project, "limit": limit}, read, project=project)[
        "events"
    ]
    if get_state().json_output:
        _emit_json({"events": rows})
        return
    console = stdout_console()
    if not rows:
        console.print("no captain actions yet")
        return
    table = Table(box=None, pad_edge=False, header_style="bold")
    for column in ("seq", "when", "board", "tool", "ok", "said", "utterance"):
        table.add_column(column)
    for row in rows:
        at = row.get("at")
        when = f"{local_time(datetime.fromisoformat(at)):%H:%M:%S}" if isinstance(at, str) else "?"
        table.add_row(
            str(row.get("seq")),
            when,
            str(row.get("board")),
            str(row.get("tool")),
            "✓" if row.get("ok") else "✗",
            str(row.get("said")),
            str(row.get("utterance") or ""),
        )
    console.print(table)


def _read_audited(
    name: str,
    args: Mapping[str, Any],
    read: Callable[[], tuple[dict[str, Any], str]],
    *,
    project: str | None = None,
) -> dict[str, Any]:
    """A read that is no captain tool, audited like one (13081). The verb prints only what it
    read, so ``log`` and ``actions`` keep their shapes."""
    from aisquare.services.captain import actions

    try:
        data = actions.perform_read(name, args, _typed(), read, project=project)
    except (Refused, Failed) as exc:
        _said_and_exit(exc)
    except (sqlite3.DatabaseError, OSError) as exc:
        fail(f"error: {exc}", error="store", detail=str(exc))
    return data


def audit_log(project: str | None, *, limit: int = LOG_DEFAULT) -> list[dict[str, Any]]:
    """The last ``limit`` ``captain_action`` events, decoded, oldest first.

    One board when ``project`` names one; otherwise the home board (where a call
    that names no project is audited) and every onboarded project, merged by seq.
    A record that does not decode is kept with its raw text, never dropped: the
    audit is the one thing this command must not edit.
    """
    from aisquare.services import fleet as fleet_service
    from aisquare.services.captain import actions
    from aisquare.services.captain import state as captain_state

    boards: list[ProjectInfo]
    if project is not None:
        boards = [fleet_service.resolve_project(project)]
    else:
        with store_session() as store:
            listed = store.list_projects()
        home = captain_state.home_project()
        boards = [home, *[p for p in listed if p.id != home.id]]
    events: list[tuple[TeamEvent, ProjectInfo]] = []
    with store_session() as store:
        for board in boards:
            for event in store.filtered_events(board.id, kind=actions.AUDIT_KIND, limit=limit):
                events.append((event, board))
    events.sort(key=lambda pair: pair[0].seq)
    return [_decode(event, board) for event, board in events[-limit:]]


def _decode(event: TeamEvent, board: ProjectInfo) -> dict[str, Any]:
    record: dict[str, Any] = {
        "seq": event.seq,
        "at": event.created_at.isoformat(),
        "board": board.root.name or board.id,
        "board_id": board.id,
    }
    try:
        body = json.loads(event.text)
    except ValueError:
        body = None
    if isinstance(body, dict):
        for key in ("v", "tool", "project", "args", "utterance", "ok", "said", "receipt"):
            record[key] = body.get(key)
    else:
        record.update({"tool": None, "ok": None, "said": event.text, "utterance": None})
    return record


def uav(limit: Limit = DEFAULT_LIMIT) -> None:
    """The sitrep: 'UAV online', whether the captain is thinking, then what needs you."""
    from aisquare.services.captain import state as captain_state

    result = _perform("attention", {"limit": limit})
    busy_error: str | None = None
    try:
        busy = captain_state.busy_since()
    except OSError as exc:  # the queue answered; the flag's file did not — say which
        busy, busy_error = None, str(exc)
    report: dict[str, Any] = {
        "uav": "online",
        "busy_since": busy.isoformat() if busy is not None else None,
        **({"busy_error": busy_error} if busy_error is not None else {}),
        **result,
    }
    if get_state().json_output:
        _emit_json(report)
        return
    console = stdout_console()
    console.print(UAV_LINE, style="bold")
    if busy_error is not None:
        console.print(f"captain state unreadable: {busy_error}", style="yellow")
    else:
        console.print(
            f"captain thinking since {local_time(busy):%H:%M:%S}"
            if busy is not None
            else "captain idle"
        )
    items = result.get("items")
    rows = items if isinstance(items, list) else []
    console.print(f"{len(rows)} item(s) need you" if rows else "nothing needs you")
    if rows:
        console.print(_items_table(rows, datetime.now(UTC)))


def wololo(
    project: Annotated[str, typer.Argument(help="Project: id prefix, directory name or codename.")],
    label: Annotated[str, typer.Argument(help="The idle agent to convert (its fleet label).")],
    task: Annotated[str, typer.Argument(help="The todo card to claim for it (id prefix).")],
) -> None:
    """Wololo! Convert an idle agent to a task: release its claims, claim the card, tell it."""
    result = _perform("wololo", {"project": project, "label": label, "task": task})
    if get_state().json_output:
        _emit_json(result)
        return
    console = stdout_console()
    console.print(f"{result.get('said')} · action seq {result.get('action_seq')}")
    released = result.get("released")
    if isinstance(released, list) and released:
        console.print(f"released: {', '.join(str(r) for r in released)}")
    console.print(f"told {label}: {result.get('told')}")


def bt() -> None:
    """The brake: cancel a wait, clear the speech queue, undo the last reversible action."""
    result = _perform("bt", {})
    if get_state().json_output:
        _emit_json(result)
        return
    stdout_console().print(f"{result.get('said')} · action seq {result.get('action_seq')}")


def action_list() -> None:
    """The owner action list: what `act <name>` runs, bundled and from config.toml."""
    from aisquare.services.captain import actions

    holder: dict[str, Any] = {}

    def read() -> tuple[dict[str, Any], str]:
        holder["listed"] = actions.action_list()
        return {}, f"listed {len(holder['listed'])} owner action(s)"

    _read_audited("actions", {}, read)
    listed = holder["listed"]
    if get_state().json_output:
        _emit_json(
            {
                "actions": {
                    name: {
                        "steps": list(action.steps),
                        "description": action.description,
                        "problem": action.problem,
                    }
                    for name, action in sorted(listed.items())
                }
            }
        )
        return
    table = Table(box=None, pad_edge=False, header_style="bold")
    for column in ("name", "steps", "description"):
        table.add_column(column)
    for name, action in sorted(listed.items()):
        steps = " → ".join(action.steps) if action.steps else f"INVALID: {action.problem}"
        table.add_row(name, steps, action.description)
    stdout_console().print(table)


__all__: list[str] = ["UAV_LINE", "audit_log", "register"]


def _unused(_: Callable[..., NoReturn]) -> None:  # pragma: no cover
    """Keep the two typing names imported for the annotations above."""
