"""``aisquare watch``: the fleet's one non-AI watcher (P2-W2, #284).

A manager watched the fleet with its own turns: a Monitor watch, a ``sleep`` loop or a
``gh`` poll is a turn, every turn re-sends the manager's whole context, and most checks
find nothing new. A Monitor also expires (30 minutes at most). This is one plain process
that looks every ``[watch] interval_seconds`` and wakes a manager only for something new,
with a count and a file path and no text.

What it looks at, composed and never classified a third time:

- **Needs** (:func:`remote_needs.scan_needs_you`, the phone's scan, the phone's
  dismissals honoured): a permission or question dialog, an agent asking, a limit, a
  crash, a lost or failed agent, a manager or fleet down.
- **Stale agents** (the captain queue's ``stale`` rule, :func:`captain.queue.observe`).
- **Pull requests** (:mod:`aisquare.services.github_prs`), with ``[watch] github = true``:
  approved, changes requested, checks done; every ``github_every_seconds``.
- **The operator's commands** (``[watch.sources.<name>]``), each printing JSON lines
  ``{"id", "project", "kind", "summary"}`` every ``every_seconds``. aisquare ships no
  integration with any outside service: such a source is the operator's own script.

Delivery, per tick: the ids seen are kept in ``~/.aisquare/watch/state.json`` (written whole
under its lock). Per project with new items, the full items go to
``~/.aisquare/watch/<project>/<UTC stamp>.json`` (mode 600); one board event of kind
``watch`` addressed to the manager carries the counts and that path, never an excerpt
(``watch: 3 new (2 permission, 1 pr_approved): aisquare watch show <path>``); then the
manager's guarded nudge (``fleet.nudge_manager``, P0-W2). A working manager gets the
event at its turn's end: an event addressed to it is a wake (P1 WS-2's ``wake_class``).
With ``[watch] auto_stop`` each tick also stops the finished workers (P2-W1b).
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aisquare.core import atomic, locking, selfcli
from aisquare.core.config import WatchSettings, load_config
from aisquare.core.spawn import untraced_env
from aisquare.core.store import store_session
from aisquare.models import ProjectInfo
from aisquare.services import github_prs

STATE_LOCK_WAIT = 10.0
"""How long a tick waits for ``state.json``'s lock."""
SOURCE_TIMEOUT = 60.0
"""Seconds an operator's source command may run in one tick."""
DETACHED_SESSION = "asq-watch"
"""The tmux session ``watch --detach`` runs the watcher in, on the fleet's own server."""

_sleep: Callable[[float], None] = time.sleep


def _now() -> datetime:
    return datetime.now(tz=UTC)


def watch_dir() -> Path:
    return github_prs.watch_dir()


def state_path() -> Path:
    return watch_dir() / "state.json"


def lock_path() -> Path:
    """The one-watcher-per-home lock: held for the life of a running watcher."""
    return watch_dir() / "watch.lock"


def pid_path() -> Path:
    return watch_dir() / "watch.pid"


@dataclass(frozen=True)
class WatchItem:
    """One thing the watcher saw. ``id`` is stable for what it says, so the same news is
    never new twice, and something new (another prompt, another review) is a new id."""

    id: str
    project_id: str
    project_name: str
    kind: str
    summary: str
    detail: Mapping[str, object] = field(default_factory=dict)

    def as_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "project": self.project_name,
            "kind": self.kind,
            "summary": self.summary,
            "detail": dict(self.detail),
        }


@dataclass
class TickReport:
    """What one tick saw and did."""

    at: datetime
    items: list[WatchItem]
    new: list[WatchItem]
    files: dict[str, Path] = field(default_factory=dict)
    """Project id → the detail file its new items were written to."""
    nudged: list[str] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    """Labels of the finished workers auto-stop stopped this tick."""
    notes: list[str] = field(default_factory=list)
    """Lines for the operator: a source failing (once per streak), a GitHub error."""

    def as_json(self) -> dict[str, object]:
        return {
            "at": self.at.isoformat(timespec="seconds"),
            "new": [item.as_json() for item in self.new],
            "items": len(self.items),
            "files": {pid: str(path) for pid, path in self.files.items()},
            "nudged": self.nudged,
            "stopped": self.stopped,
            "notes": self.notes,
        }


def _project_name(project: ProjectInfo) -> str:
    return project.codename or project.root.name or project.id


class Watcher:
    """The watcher's memory across ticks: the needs scan's dates, streaks and kept items,
    when each slow source last ran, and which sources are failing."""

    def __init__(
        self,
        settings: WatchSettings | None = None,
        *,
        needs_sources: Callable[[], Any] | None = None,
        stale: bool = True,
    ) -> None:
        self.settings = settings or load_config().watch
        self._needs_sources = needs_sources
        self._stale = stale
        self.first_seen: dict[str, datetime] = {}
        self.failing: set[str] = set()
        self.kept: list[Any] = []
        self.last_run: dict[str, datetime] = {}
        self.slow_items: dict[str, list[WatchItem]] = {}
        """What each slow source (GitHub, a command) said when it last ran: it stands until
        that source runs again."""
        self.failing_sources: set[str] = set()

    # -- what it looks at ---------------------------------------------------------------

    def _projects(self) -> list[ProjectInfo]:
        with store_session() as store:
            return store.list_projects()

    def needs(self, now: datetime) -> list[WatchItem]:
        from aisquare.services import remote_needs

        sources = (
            self._needs_sources() if self._needs_sources else remote_needs.live_needs_sources()
        )
        found = remote_needs.scan_needs_you(
            sources,
            now=now,
            dismissed=remote_needs.load_needs_dismissals(),
            first_seen=self.first_seen,
            failing=self.failing,
            kept=self.kept,
        )
        self.kept = list(found)
        return [
            WatchItem(
                id=f"needs:{item.id}",
                project_id=item.project_id,
                project_name=item.project_name,
                kind=item.kind,
                summary=f"{item.agent or item.project_name}: {item.reason}",
                detail={
                    "agent": item.agent,
                    "reason": item.reason,
                    "excerpt": item.excerpt,
                    "since": item.since.isoformat(timespec="seconds"),
                    "actions": list(item.actions),
                },
            )
            for item in found
        ]

    def stale(self, now: datetime) -> list[WatchItem]:
        if not self._stale:
            return []
        from aisquare.services.captain import queue

        sources = queue.StoreSources()
        projects = sources.projects()
        cursors = {p.id: sources.latest_seq(p.id) for p in projects}  # no events re-read
        seen = queue.observe(sources, cursors=cursors, now=now)
        return [
            WatchItem(
                id=f"stale:{o.key}",
                project_id=o.project,
                project_name=o.project_name,
                kind="stale",
                summary=o.text,
                detail={"agent": o.agent, "seen_at": o.seen_at.isoformat(timespec="seconds")},
            )
            for o in seen.observations
            if o.kind == "stale"
        ]

    def _due(self, name: str, every: float, now: datetime) -> bool:
        last = self.last_run.get(name)
        return last is None or (now - last).total_seconds() >= every

    def github(self, now: datetime, notes: list[str]) -> list[WatchItem]:
        if not self.settings.github:
            return []
        if not self._due("github", self.settings.github_every_seconds, now):
            return self.slow_items.get("github", [])
        self.last_run["github"] = now
        cache = github_prs.Cache.load()
        items: list[WatchItem] = []
        repos: dict[str, list[ProjectInfo]] = {}
        for project in self._projects():
            repo = github_prs.repo_of(project.root)
            if repo is not None:
                repos.setdefault(repo, []).append(project)
        try:
            for repo, projects in repos.items():
                for state in github_prs.open_pull_requests(repo, cache):
                    for pr in github_prs.pr_items(state):
                        items += [
                            WatchItem(
                                id=f"{pr.id}@{project.id}",
                                project_id=project.id,
                                project_name=_project_name(project),
                                kind=pr.kind,
                                summary=pr.summary,
                                detail={"url": pr.url, "repo": repo, "number": state.number},
                            )
                            for project in projects
                        ]
            self.failing_sources.discard("github")
        except github_prs.GhError as exc:
            if "github" not in self.failing_sources:
                notes.append(f"github: {exc}")
            self.failing_sources.add("github")
            return self.slow_items.get("github", [])
        finally:
            cache.save()
        self.slow_items["github"] = items
        return items

    def commands(self, now: datetime, notes: list[str]) -> list[WatchItem]:
        items: list[WatchItem] = []
        projects = {p.id: p for p in self._projects()}
        by_name = {p.codename: p for p in projects.values() if p.codename}
        for name, source in sorted(self.settings.sources.items()):
            key = f"source:{name}"
            if not self._due(key, source.every_seconds, now):
                items += self.slow_items.get(key, [])
                continue
            self.last_run[key] = now
            try:
                said = _run_source(source.command)
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                if key not in self.failing_sources:
                    notes.append(f"source {name} is failing: {exc}")
                self.failing_sources.add(key)
                items += self.slow_items.get(key, [])
                continue
            if key in self.failing_sources:
                notes.append(f"source {name} works again")
                self.failing_sources.discard(key)
            made = []
            for line in said:
                ref = str(line.get("project") or "")
                project = projects.get(ref) or by_name.get(ref)
                if project is None:
                    continue
                made.append(
                    WatchItem(
                        id=f"{key}:{line.get('id')}",
                        project_id=project.id,
                        project_name=_project_name(project),
                        kind=str(line.get("kind") or name),
                        summary=str(line.get("summary") or ""),
                        detail={"source": name},
                    )
                )
            self.slow_items[key] = made
            items += made
        return items

    def scan(self, now: datetime, notes: list[str] | None = None) -> list[WatchItem]:
        """Everything worth a look right now, across every project."""
        notes = notes if notes is not None else []
        return [
            *self.needs(now),
            *self.stale(now),
            *self.github(now, notes),
            *self.commands(now, notes),
        ]

    # -- one tick -------------------------------------------------------------------------

    def tick(self, now: datetime | None = None) -> TickReport:
        """Scan, then for each project with new items: the detail file, one ``watch`` event
        to its manager, one nudge. Then, with ``[watch] auto_stop``, the finished workers."""
        now = now or _now()
        notes: list[str] = []
        items = self.scan(now, notes)
        watch_dir().mkdir(parents=True, exist_ok=True)
        with locking.holding(_state_lock(), wait_s=STATE_LOCK_WAIT):
            state = read_state()
            seen = set(state.get("seen") or [])
            new = [item for item in items if item.id not in seen]
            _write_state(
                {
                    **state,
                    "seen": sorted({item.id for item in items}),
                    "last_scan_at": now.isoformat(timespec="seconds"),
                }
            )
        report = TickReport(at=now, items=items, new=new, notes=notes)
        by_project: dict[str, list[WatchItem]] = {}
        for item in new:
            by_project.setdefault(item.project_id, []).append(item)
        for project_id, found in by_project.items():
            path = _write_detail(project_id, found, now)
            report.files[project_id] = path
            if _deliver(project_id, found, path):
                report.nudged.append(project_id)
        if self.settings.auto_stop:
            with contextlib.suppress(Exception):  # a stop never costs the tick its news
                from aisquare.services import autostop

                report.stopped = [
                    done.verdict.agent.label
                    for done in autostop.reap_finished(autostop.every_project())
                    if done.stopped
                ]
        return report


def _run_source(command: Sequence[str]) -> list[dict[str, Any]]:
    """An operator source's JSON lines. Raises on a failure, a timeout or bad JSON."""
    if not command:
        raise ValueError("no command")
    done = subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        timeout=SOURCE_TIMEOUT,
        check=False,
        env=untraced_env(),  # an operator's source may run a model (`claude -p`)
    )
    if done.returncode != 0:
        said = (done.stderr or "").strip().splitlines()
        raise ValueError(f"exit {done.returncode}{': ' + said[-1] if said else ''}")
    lines = []
    for raw in done.stdout.splitlines():
        if raw.strip():
            parsed = json.loads(raw)
            if not isinstance(parsed, dict) or "id" not in parsed:
                raise ValueError("a line that is not an object with an id")
            lines.append(parsed)
    return lines


# --- state.json, detail files ----------------------------------------------------------


def _state_lock() -> Path:
    return watch_dir() / "state.json.lock"


def read_state() -> dict[str, Any]:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(data: Mapping[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_replacing(path, json.dumps(data), durable=False)


def _write_detail(project_id: str, items: Sequence[WatchItem], now: datetime) -> Path:
    """The full items of one tick for one project, mode 600: pane excerpts stay local."""
    with store_session() as store:
        project = store.get_project(project_id, include_forgotten=True)
    folder = watch_dir() / (project.codename or project_id if project else project_id)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    path = folder / f"{stamp}.json"
    body = {
        "project_id": project_id,
        "at": now.isoformat(timespec="seconds"),
        "items": [item.as_json() for item in items],
    }
    atomic.write_replacing(path, json.dumps(body, indent=2), owner_only=True)
    return path


def event_text(items: Sequence[WatchItem], path: Path) -> str:
    """``watch: 3 new (2 permission, 1 pr_approved): aisquare watch show <path>``: counts
    and a path, never an excerpt (a manager pays for every character in its context)."""
    counts = Counter(item.kind for item in items)
    said = ", ".join(
        f"{n} {kind}" for kind, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    return f"watch: {len(items)} new ({said}): aisquare watch show {path}"


def _deliver(project_id: str, items: Sequence[WatchItem], path: Path) -> bool:
    """One ``watch`` event addressed to the project's manager, on the board its row reads,
    then its guarded nudge. Whether the nudge went."""
    from aisquare.services import fleet as fleet_service
    from aisquare.services import team as team_service

    with store_session() as store:
        manager = store.fleet_agent_by_label(
            project_id, fleet_service.MANAGER_LABEL, live_only=True
        )
    board = fleet_service.board_of_row(manager) if manager is not None else project_id
    team_service.watch_event(board, event_text(items, path))
    if manager is None:
        return False
    return fleet_service.nudge_manager(project_id, reason="watch", board=board)


# --- the loop, detached ----------------------------------------------------------------


class AlreadyWatching(RuntimeError):
    """Another watcher holds this home's lock; the message names it."""


def running_pid() -> int | None:
    """The pid of the watcher running for this home, or ``None``: one holds the lock. ``-1``
    for a holder that left no pid file. ``flock`` is per open file, so asking from this
    process's own descriptor never drops a lock it holds elsewhere."""
    try:
        fd = locking.open_lock_file(lock_path(), create=False)
    except FileNotFoundError:
        return None
    try:
        try:
            locking.acquire(fd, wait_s=0)
        except TimeoutError:
            try:
                return int(pid_path().read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                return -1
        with contextlib.suppress(OSError):
            locking.unlock(fd)
        return None
    finally:
        os.close(fd)


def run(*, interval: float | None = None, on_tick: Callable[[TickReport], None]) -> None:
    """The watcher's loop, holding this home's lock for its whole life; returns on
    SIGTERM or SIGINT. Raises :class:`AlreadyWatching`."""
    watch_dir().mkdir(parents=True, exist_ok=True)
    fd = locking.open_lock_file(lock_path())
    try:
        locking.acquire(fd, wait_s=0)
    except TimeoutError as exc:
        os.close(fd)
        pid = running_pid()
        raise AlreadyWatching(
            f"a watcher already runs for this home (pid {pid}): `aisquare watch --status`"
        ) from exc
    stop = False

    def stopping(signum: int, frame: object) -> None:
        nonlocal stop
        stop = True

    try:
        pid_path().write_text(str(os.getpid()), encoding="utf-8")
        with contextlib.suppress(ValueError):  # not the main thread (tests)
            signal.signal(signal.SIGTERM, stopping)
        watcher = Watcher()
        every = interval or watcher.settings.interval_seconds
        while not stop:
            on_tick(watcher.tick())
            waited = 0.0
            while not stop and waited < every:
                _sleep(min(1.0, every - waited))
                waited += 1.0
    finally:
        with contextlib.suppress(OSError):
            pid_path().unlink()
        os.close(fd)


def detach() -> int:
    """Start the watcher in a window of the fleet's private tmux server (session
    :data:`DETACHED_SESSION`), so it outlives this shell; its pid. Raises
    :class:`AlreadyWatching`."""
    from aisquare.services import fleet as fleet_service

    pid = running_pid()
    if pid is not None:
        raise AlreadyWatching(
            f"a watcher already runs for this home (pid {pid}): `aisquare watch --status`"
        )
    config = fleet_service.settings()
    fleet_service.open_window(  # the one way a window opens at the fleet's socket
        fleet_service.server(config),
        DETACHED_SESSION,
        name="watch",
        cwd=Path.home(),
        command=selfcli.argv_for(["watch"]),
        config=config,
    )
    for _ in range(50):  # up to 5 s for it to take the lock
        pid = running_pid()
        if pid is not None:
            return pid
        _sleep(0.1)
    return -1


def stop_detached() -> int | None:
    """Stop the running watcher (SIGTERM) and its tmux session; the pid it had, or
    ``None`` when none ran."""
    from aisquare.services import fleet as fleet_service

    pid = running_pid()
    if pid is not None and pid > 0:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
    with contextlib.suppress(Exception):
        fleet_service.server(fleet_service.settings()).kill_session(DETACHED_SESSION)
    return pid


@dataclass(frozen=True)
class Status:
    pid: int | None
    last_scan_at: datetime | None

    def age(self, now: datetime | None = None) -> timedelta | None:
        if self.last_scan_at is None:
            return None
        return (now or _now()) - self.last_scan_at


def status() -> Status:
    said = read_state().get("last_scan_at")
    try:
        last = datetime.fromisoformat(said) if isinstance(said, str) else None
    except ValueError:
        last = None
    return Status(running_pid(), last)


# --- watch show ------------------------------------------------------------------------


def detail_files() -> list[Path]:
    """Every detail file, newest last."""
    root = watch_dir()
    if not root.is_dir():
        return []
    files = [p for p in root.glob("*/*.json") if p.is_file()]
    return sorted(files, key=lambda p: (p.name, str(p)))


def show(path: Path | None) -> dict[str, Any]:
    """A detail file's items: ``path``, or the newest with ``None``. Raises
    ``FileNotFoundError``."""
    if path is None:
        files = detail_files()
        if not files:
            raise FileNotFoundError("no watch detail file yet")
        path = files[-1]
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a watch detail file")
    data["path"] = str(path)
    return data


def kinds(items: Iterable[WatchItem]) -> dict[str, int]:
    return dict(Counter(item.kind for item in items))
