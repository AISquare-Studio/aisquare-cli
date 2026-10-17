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
from pathlib import Path

from aisquare.core import brain, orchestrator, selfcli, spawn
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


def drain(cwd: Path | None = None, *, rescan: bool = False, board: str | None = None) -> int | None:
    """Distill everything new on this project's pipe, or ``board``'s, named outright; returns
    pages written.

    ``rescan`` restarts from the beginning of the pipe (a backfill — safe,
    since page slugs are event ids, so re-distilling updates in place).
    Returns ``None`` when another drain already holds the brain lock (the
    work is happening, just not here). Skips silently (returning 0) when the
    brain layer is disabled, gbrain is missing, or the brain cannot
    initialise — the watermark then stays put and the next drain retries.
    """
    if not brain.brain_enabled() or brain.gbrain_version() is None:
        return 0
    project_id = board if board is not None else orchestrator.team_project(cwd).id
    written = 0
    with brain.drain_lock(project_id) as won:
        if not won:
            return None
        with store_session() as store:
            if rescan:
                store.set_meta(_watermark_key(project_id), "0")
            roles = {s.id: s.role for s in store.team_sessions(project_id)}
            while True:
                watermark = int(store.get_meta(_watermark_key(project_id)) or 0)
                events = store.events_since(project_id, watermark, limit=_BATCH)
                if not events:
                    return written
                for event in events:
                    if event.kind in DISTILL_KINDS:
                        page = _compose(event, roles.get(event.session_id or ""))
                        if not brain.distill_page(project_id, _slug(event), page):
                            return written  # watermark holds; retry next drain
                        written += 1
                    store.set_meta(_watermark_key(project_id), str(event.seq))
                if len(events) < _BATCH:
                    return written


def spawn_drain(
    cwd: Path | None = None, *, root: Path | None = None, board: str | None = None
) -> None:
    """Kick off a detached drain; returns immediately, never raises.

    This runs on durable-mutation hot paths, so it must not shell out:
    the gbrain gate is a PATH lookup (the worker re-verifies the version),
    and callers pass the already-resolved project ``root`` to spare a
    ``git rev-parse`` subprocess.

    ``board``, the board the write went to, is handed to the worker
    (``team distill --board``), which drains it whatever its directory and
    environment resolve: ``root`` is only its working directory, the board's
    where it has one. Left to work it out again, the worker under the caller's
    ``AISQUARE_TEAM_HUB`` drained the hub's board, and with none the checkout's
    of ``root``, which is not a hub's rooted in a sub-folder or a worktree: a
    note ``fleet tell`` or the phone filed on another board than the caller's
    was distilled from the wrong pipe (review of #259, round 33, 5/10).
    """
    if not brain.brain_enabled() or shutil.which("gbrain") is None:
        return
    if root is None and board is None:
        root = orchestrator.team_project(cwd).root
    named = [] if board is None else ["--board", board]
    try:
        subprocess.Popen(
            selfcli.argv_for(["--quiet", "team", "distill", *named]),
            cwd=None if root is None else str(root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            # Everything the parent had EXCEPT its tracing identity. A detached
            # worker of ours is not an agent session, and this one outlives the
            # process that started it — inheriting a live pipeline id would
            # attach its gbrain work to a Run that may already have ended.
            env=spawn.untraced_env(),
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
