"""The distiller: durable team events → the project brain (gbrain).

An outbox pattern over the team pipe. Distill-worthy events (decisions,
results, task outcomes and reopen feedback) already sit in ``team_event``;
a per-project watermark in ``team_meta`` tracks what has been distilled.
``drain`` moves the watermark forward through cold ``gbrain put`` calls —
off the hot path, under aisquare's own brain lock, never fatal.

Mutating commands call :func:`spawn_drain` (a detached ``aisquare team
distill``) so knowledge lands in the brain seconds after it hits the pipe
without any command or hook ever waiting on gbrain.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from aisquare.core import brain, teambus
from aisquare.core.store import ContextStore, store_session
from aisquare.models import TeamEvent

# Deliberate team communication and task outcomes distill; presence churn,
# focus flickers and permission notices do not.
DISTILL_KINDS = frozenset(
    {
        "note",
        "decision",
        "question",
        "result",
        "task_review",
        "task_done",
        "task_blocked",
        "task_reopened",
    }
)
_BATCH = 100


def _watermark_key(project_id: str) -> str:
    return f"distill_seq:{project_id}"


def pending(store: ContextStore, project_id: str) -> int:
    """How many pipe events the distiller has not yet scanned (doctor signal)."""
    watermark = int(store.get_meta(_watermark_key(project_id)) or 0)
    return max(0, store.latest_seq(project_id) - watermark)


def drain(cwd: Path | None = None, *, rescan: bool = False) -> int | None:
    """Distill everything new on this project's pipe; returns pages written.

    ``rescan`` restarts from the beginning of the pipe (a backfill — safe,
    since page slugs are event ids, so re-distilling updates in place).
    Returns ``None`` when another drain already holds the brain lock (the
    work is happening, just not here). Skips silently (returning 0) when the
    brain layer is disabled, gbrain is missing, or the brain cannot
    initialise — the watermark then stays put and the next drain retries.
    """
    if not brain.brain_enabled() or brain.gbrain_version() is None:
        return 0
    project = teambus.team_project(cwd)
    written = 0
    with brain.drain_lock(project.id) as won:
        if not won:
            return None
        with store_session() as store:
            if rescan:
                store.set_meta(_watermark_key(project.id), "0")
            roles = {s.id: s.role for s in store.team_sessions(project.id)}
            while True:
                watermark = int(store.get_meta(_watermark_key(project.id)) or 0)
                events = store.events_since(project.id, watermark, limit=_BATCH)
                if not events:
                    return written
                for event in events:
                    if event.kind in DISTILL_KINDS:
                        page = _compose(event, roles.get(event.session_id or ""))
                        if not brain.distill_page(project.id, _slug(event), page):
                            return written  # watermark holds; retry next drain
                        written += 1
                    store.set_meta(_watermark_key(project.id), str(event.seq))
                if len(events) < _BATCH:
                    return written


def spawn_drain(cwd: Path | None = None, *, root: Path | None = None) -> None:
    """Kick off a detached drain; returns immediately, never raises.

    This runs on durable-mutation hot paths, so it must not shell out:
    the gbrain gate is a PATH lookup (the worker re-verifies the version),
    and callers pass the already-resolved project ``root`` to spare a
    ``git rev-parse`` subprocess.
    """
    if not brain.brain_enabled() or shutil.which("gbrain") is None:
        return
    if root is None:
        root = teambus.team_project(cwd).root
    try:
        subprocess.Popen(
            [sys.executable, "-m", "aisquare", "--quiet", "team", "distill"],
            cwd=str(root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return


def _slug(event: TeamEvent) -> str:
    kind = event.kind.replace("_", "-")
    return f"team/{kind}/{event.id}"


def _compose(event: TeamEvent, role: str | None) -> str:
    """Render one pipe event as a brain page (frontmatter + searchable body)."""
    title = event.text.splitlines()[0][:80] if event.text else event.kind
    who = event.session_id or "cli"
    lines = [
        "---",
        "type: note",
        f"tags: [aisquare-team, {event.kind.replace('_', '-')}]",
        "---",
        "",
        f"# {event.kind.replace('_', ' ')}: {title}",
        "",
        event.text,
        "",
        f"- session: {who}" + (f" ({role})" if role else ""),
        f"- at: {event.created_at.isoformat()}",
    ]
    if event.task_id:
        lines.append(f"- task: {event.task_id}")
    if event.to_role:
        lines.append(f"- for: {event.to_role}")
    return "\n".join(lines) + "\n"
