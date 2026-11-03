"""``aisquare instances``: the asq UIs live on this home (#20).

The owner runs asq at the Mac and over SSH into the same Mac at once; the chip under asq's
status line says so at a glance, and this lists them, from the registry every asq keeps
(``core.instances``). A look: nothing is pruned, nothing is made.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import typer
from rich.table import Table

from aisquare.core import instances as instances_core
from aisquare.core.console import stderr_console, stdout_console
from aisquare.core.instances import Instance
from aisquare.core.state import get_state


def instances() -> None:
    """List the asq UIs running on this home: kind, host, pid, version, since and heartbeat."""
    now = datetime.now(UTC)
    live = instances_core.live_instances(prune=False)
    mine = instances_core.this_instance()
    this_id = None if mine is None else mine.id
    if get_state().json_output:
        typer.echo(json.dumps({"instances": [as_json(i, now, this_id) for i in live]}))
        return
    if not live:
        stdout_console().print("No asq is running on this home.")
        return
    table = Table(box=None, pad_edge=False, show_edge=False, header_style="bold")
    for column in ("KIND", "HOST", "PID", "VERSION", "SINCE", "HEARTBEAT"):
        table.add_column(column, no_wrap=True)
    for instance in live:
        kind = f"{instance.kind} (this)" if instance.id == this_id else instance.kind
        table.add_row(
            kind,
            instance.host,
            str(instance.pid),
            instance.version,
            instance.started_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            f"{heartbeat_age(instance, now)} s ago",
        )
    stdout_console().print(table)
    if any(instance.differs_from(live[0]) for instance in live[1:]):
        stderr_console().print(
            "⚠ these asqs run different releases or store schemas on one home: quit the older "
            "one (q in it) and start it again with this release",
            markup=False,
            highlight=False,
        )


def heartbeat_age(instance: Instance, now: datetime) -> int:
    """Whole seconds since ``instance`` last rewrote its record (never below zero: a clock
    of another host sharing the home may run ahead)."""
    return max(0, int((now - instance.heartbeat).total_seconds()))


def as_json(instance: Instance, now: datetime, this_id: str | None) -> dict[str, Any]:
    """One live asq under ``--json``: its record, its heartbeat's age, and whether it is the
    asking process (only ever true inside asq)."""
    return {
        **instance.to_record(),
        "heartbeat_age_s": heartbeat_age(instance, now),
        "this": instance.id == this_id,
    }
