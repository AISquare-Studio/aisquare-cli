"""Streams: named bodies of work that own projects and depend on each other.

A stream sits between the user pool and project pools. Membership is
many-to-many (one checkout can carry several efforts), members need not be git
repositories (compliance work has no code), and a stream may *require* other
streams — ``scope_streams`` follows those edges, so a MetricStream session sees
the platform conventions without anyone copying entries.

Scope resolution stays cwd-first and nothing global overrides it: the only way
to force a stream in from elsewhere is per-shell (``AISQUARE_STREAM``), never a
file that silently follows the user into the next terminal.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from aisquare.core.ids import new_stream_id
from aisquare.core.store import ContextStore, store_session
from aisquare.core.workspace import active_project, current_project, refuse_home_as_project
from aisquare.models import ProjectInfo, StreamInfo

FORCE_ENV_VAR = "AISQUARE_STREAM"
"""Per-shell override: comma-separated stream names ADDED to the resolved
scope (never replacing it) — for work that belongs to a stream while standing
in a project outside it, e.g. a compliance change made from an app repo."""


class UnknownStreamError(LookupError):
    """Raised when a named stream does not exist."""

    def __init__(self, ref: str) -> None:
        super().__init__(f"no stream named {ref!r} — create it with `aisquare stream new {ref}`")
        self.ref = ref


class StreamCycleError(ValueError):
    """Raised when a requires-edge would make the dependency graph cyclic."""

    def __init__(self, path: list[str]) -> None:
        super().__init__(f"that requirement would create a cycle: {' -> '.join(path)}")
        self.path = path


def create(name: str, requires: list[str]) -> StreamInfo:
    """Create a stream, optionally depending on existing streams."""
    with store_session() as store:
        required = [_resolve(store, ref) for ref in requires]
        stream = store.create_stream(
            StreamInfo(id=new_stream_id(), name=name, created_at=datetime.now(tz=UTC))
        )
        for req in required:
            _require(store, stream, req)
        refreshed = store.get_stream(stream.id)
        assert refreshed is not None  # just created
        return refreshed


def add_members(name: str, paths_: list[Path]) -> tuple[StreamInfo, list[ProjectInfo]]:
    """Add the projects containing each path (default: the cwd) to a stream.

    Each path resolves through the normal project resolution — a git worktree
    lands on its principal repository; a marker directory on itself — and the
    project is registered on the way, so linking a repo never depends on it
    having been ``init``-ed first.
    """
    with store_session() as store:
        stream = _resolve(store, name)
        added: list[ProjectInfo] = []
        for path in paths_ or [Path.cwd()]:
            project = current_project(path)
            # A member that resolved to $HOME is the marker-walk trap, not a
            # project — refuse it here so the surprise is loud at add time,
            # not silent at injection time.
            refuse_home_as_project(project.root)
            store.ensure_project(project)
            store.add_stream_member(stream.id, project.id)
            added.append(project)
        refreshed = store.get_stream(stream.id)
        assert refreshed is not None  # membership edits never delete the stream
        return refreshed, added


def remove_member(name: str, path: Path) -> tuple[StreamInfo, ProjectInfo]:
    """Remove the project containing ``path`` from a stream."""
    with store_session() as store:
        stream = _resolve(store, name)
        project = current_project(path)
        store.remove_stream_member(stream.id, project.id)
        refreshed = store.get_stream(stream.id)
        assert refreshed is not None  # membership edits never delete the stream
        return refreshed, project


def require(name: str, requirements: list[str]) -> StreamInfo:
    """Make a stream depend on other streams (refusing cycles)."""
    with store_session() as store:
        stream = _resolve(store, name)
        for ref in requirements:
            _require(store, stream, _resolve(store, ref))
        refreshed = store.get_stream(stream.id)
        assert refreshed is not None  # edge edits never delete the stream
        return refreshed


def list_streams() -> list[StreamInfo]:
    with store_session() as store:
        return store.list_streams()


def show(name: str) -> tuple[StreamInfo, list[ProjectInfo], list[str], int]:
    """A stream with its member projects, required stream names and entry count."""
    with store_session() as store:
        stream = _resolve(store, name)
        members = [
            project
            for project in (store.get_project(pid) for pid in stream.members)
            if project is not None
        ]
        required = [_stream_name(store, sid) for sid in stream.requires]
        entries = len(store.entries("stream", stream_ids=[stream.id]))
        return stream, members, required, entries


def resolve(store: ContextStore, ref: str) -> StreamInfo:
    """Resolve a stream by name/id for callers that already hold a store."""
    return _resolve(store, ref)


def scope_streams(store: ContextStore, project_id: str) -> list[StreamInfo]:
    """Every stream in scope for a project: memberships, their dependency
    closure, and any per-shell forced streams (``AISQUARE_STREAM``).

    Order is deterministic — direct memberships first (alphabetical), then
    requirements breadth-first — so the injected block reads the same way
    every session.
    """
    by_id: dict[str, StreamInfo] = {}
    queue = list(store.streams_for_project(project_id))
    for ref in _forced_names():
        forced = store.get_stream(ref)
        if forced is not None and forced.id not in {s.id for s in queue}:
            queue.append(forced)
    while queue:
        stream = queue.pop(0)
        if stream.id in by_id:
            continue
        by_id[stream.id] = stream
        for required_id in stream.requires:
            required = store.get_stream(required_id)
            if required is not None and required.id not in by_id:
                queue.append(required)
    return list(by_id.values())


def scope_stream_ids(store: ContextStore, project_id: str) -> list[str]:
    return [stream.id for stream in scope_streams(store, project_id)]


def active_scope(
    store: ContextStore, cwd: Path | None = None
) -> tuple[ProjectInfo, list[StreamInfo]]:
    """The active project and every stream in scope for it, in one call."""
    project = active_project(store, cwd)
    return project, scope_streams(store, project.id)


def _forced_names() -> list[str]:
    raw = os.environ.get(FORCE_ENV_VAR, "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def _resolve(store: ContextStore, ref: str) -> StreamInfo:
    stream = store.get_stream(ref)
    if stream is None:
        raise UnknownStreamError(ref)
    return stream


def _stream_name(store: ContextStore, stream_id: str) -> str:
    stream = store.get_stream(stream_id)
    return stream.name if stream is not None else stream_id


def _require(store: ContextStore, stream: StreamInfo, required: StreamInfo) -> None:
    """Add one requires-edge, refusing self-edges and cycles.

    The check walks from ``required`` looking for ``stream``: if it is
    reachable, the new edge closes a loop and injection's closure would never
    terminate meaningfully. The refusal names the path so the operator sees
    which existing edge to reconsider.
    """
    if required.id == stream.id:
        raise StreamCycleError([stream.name, stream.name])
    path = _find_path(store, start=required, target=stream.id)
    if path is not None:
        raise StreamCycleError([stream.name, *path])
    store.add_stream_requirement(stream.id, required.id)


def _find_path(store: ContextStore, *, start: StreamInfo, target: str) -> list[str] | None:
    """Names along a requires-path from ``start`` to ``target``, if one exists."""
    stack: list[tuple[StreamInfo, list[str]]] = [(start, [start.name])]
    seen: set[str] = set()
    while stack:
        stream, path = stack.pop()
        if stream.id == target:
            return path
        if stream.id in seen:
            continue
        seen.add(stream.id)
        for required_id in stream.requires:
            required = store.get_stream(required_id)
            if required is not None:
                stack.append((required, [*path, required.name]))
    return None
