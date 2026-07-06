"""``aisquare serve`` — the team bus as an MCP server for remote agents.

Lets Claude clients that are not local Claude Code sessions (the Claude
desktop app on the Windows side of WSL2, a browser-debugging agent, another
machine on the LAN) read the board and file tasks, notes and feedback onto
the same bus.

Remote callers act through a **virtual session** (``mcp:<client>``), so their
traffic is attributed on the board and flows into every local session's
deltas like any teammate's. The tool surface is deliberately small — seven
tools — because giant MCP surfaces burn context before any work happens.

Transports:

- ``stdio`` — for Claude Desktop launching the server itself (on Windows:
  ``wsl -e … aisquare serve --stdio``). No network, no token.
- ``streamable HTTP`` — bound to 127.0.0.1 by default and always guarded by
  the static bearer token stored in ``~/.aisquare/credentials`` (0600).

This module needs the optional ``mcp`` dependency (``pip install
aisquare-cli[serve]``); the CLI imports it lazily and explains if missing.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from typing import TYPE_CHECKING, Any

from aisquare.core import paths
from aisquare.models import TaskStatus, TeamSession
from aisquare.services import team as team_service
from aisquare.services.team import ClaimLostError, TeamDisabledError

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

_TOKEN_KEY = "serve_token"
DEFAULT_PORT = 8747


def client_session_id(project_id: str) -> str:
    """The virtual team-session id remote calls act as, scoped per project.

    A global id would pin the session row to the first project ever served —
    every other project's board would render the remote unattributed while
    the first kept a phantom live session forever.
    """
    client = os.environ.get("AISQUARE_SERVE_CLIENT", "").strip() or "remote"
    return f"mcp:{client}:{project_id.removeprefix('prj_')[:6]}"


def _client_role() -> str:
    return os.environ.get("AISQUARE_SERVE_ROLE", "").strip() or "remote"


def _ensure_virtual_session() -> str:
    """Register (or revive) the virtual session on the bus; returns its id.

    Gated like every other bus entry point: the master switch must be on and
    the project must already be activated (``aisquare serve`` activates its
    project explicitly at startup). Without the gate, one read-only MCP call
    against a never-opted-in directory would permanently activate it.
    """
    from datetime import UTC, datetime

    from aisquare.core.store import store_session
    from aisquare.core.teambus import team_enabled, team_project

    if not team_enabled():
        raise TeamDisabledError()
    with store_session() as store:
        project = team_project(None)
        if not store.team_active(project.id):
            raise ValueError(
                f"the team bus is not active for {project.root} — start `aisquare serve` "
                "from the project (it activates it), or run `aisquare team on` there"
            )
        session_id = client_session_id(project.id)
        store.ensure_project(project)
        now = datetime.now(tz=UTC)
        store.upsert_session(
            TeamSession(
                id=session_id,
                project_id=project.id,
                role=_client_role(),
                started_at=now,
                last_seen_at=now,
                cursor=store.latest_seq(project.id),
            )
        )
    return session_id


def _guard(fn: Any, *args: Any, **kwargs: Any) -> str:
    """Run a service call, folding failures into tool-result strings."""
    try:
        return str(fn(*args, **kwargs))
    except TeamDisabledError:
        return "error: the team bus is disabled on the host (AISQUARE_TEAM=0)"
    except ClaimLostError as exc:
        return f"error: {exc}"
    except KeyError as exc:
        return f"error: nothing matches {exc}"
    except ValueError as exc:
        return f"error: {exc}"


# --- the seven tools (plain functions; registered on FastMCP below) -----------


def team_board() -> str:
    """The live team board: sessions, open tasks, recent updates."""

    def run() -> str:
        _ensure_virtual_session()
        project, sessions, tasks, events = team_service.board_data()
        return team_service.render_board(project, sessions, tasks, events)

    return _guard(run)


def task_add(
    title: str, role: str | None = None, detail: str | None = None, needs: str | None = None
) -> str:
    """Add a shared task (idempotent: re-adding the same title returns the original).

    ``needs``: comma-separated task ids this task depends on — it stays
    unavailable to `task_next` until they are done.
    """

    def run() -> str:
        me = _ensure_virtual_session()
        needed = [ref.strip() for ref in (needs or "").split(",") if ref.strip()]
        task, created = team_service.add_task(
            title, detail=detail, role=role, needs=needed or None, session_ref=me
        )
        state = "created" if created else "already existed (idempotent)"
        return f"{state}: {task.id} [{task.status}] {task.title}"

    return _guard(run)


def task_next(role: str | None = None, status: str = "todo", claim: bool = False) -> str:
    """Fetch (optionally claim) the next available task for a role."""

    def run() -> str:
        me = _ensure_virtual_session()
        narrowed: TaskStatus = status  # type: ignore[assignment]
        task = team_service.next_task(role=role, status=narrowed, claim=claim, session_ref=me)
        return task.model_dump_json() if task is not None else "nothing available"

    return _guard(run)


def task_update(ref: str, action: str, note: str | None = None) -> str:
    """Move a task: action is done, review, block, reopen, release or drop.

    ``block`` and ``reopen`` require a note (the reason / the feedback).
    """

    def run() -> str:
        me = _ensure_virtual_session()
        if action == "done":
            task = team_service.finish_task(ref, note=note, session_ref=me)
        elif action == "review":
            task = team_service.review_task(ref, note=note, session_ref=me)
        elif action == "block":
            if not note:
                return "error: block requires a note (the reason)"
            task = team_service.block_task(ref, reason=note, session_ref=me)
        elif action == "reopen":
            if not note:
                return "error: reopen requires a note (the feedback)"
            task = team_service.reopen_task(ref, reason=note, session_ref=me)
        elif action == "release":
            task = team_service.release_task(ref, session_ref=me)
        elif action == "drop":
            task = team_service.drop_task(ref, session_ref=me)
        else:
            return "error: action must be done, review, block, reopen, release or drop"
        return f"{task.id} is now {task.status}"

    return _guard(run)


def note_add(
    text: str, kind: str = "note", task: str | None = None, to_role: str | None = None
) -> str:
    """Share a note, decision, question or result with every session on the bus."""

    def run() -> str:
        me = _ensure_virtual_session()
        event = team_service.add_note(
            text, session_ref=me, task_ref=task, to_role=to_role, kind=kind
        )
        return f"shared ({event.kind}) as event seq {event.seq}"

    return _guard(run)


def team_log(since_seq: int = 0, limit: int = 20) -> str:
    """Team events after ``since_seq`` (track the returned seq as your cursor)."""

    def run() -> str:
        _ensure_virtual_session()
        from aisquare.core.store import store_session
        from aisquare.core.teambus import team_project

        with store_session() as store:
            project = team_project(None)
            events = store.events_since(project.id, since_seq, limit=limit)
        return json.dumps(
            {
                "events": [event.as_envelope().model_dump(mode="json") for event in events],
                "latest_seq": events[-1].seq if events else since_seq,
            }
        )

    return _guard(run)


def recall(query: str) -> str:
    """Search the team's long-term memory (distilled decisions and outcomes)."""

    def run() -> str:
        output = team_service.recall(query)
        return output if output is not None else "brain unavailable (nothing distilled yet?)"

    return _guard(run)


# --- server assembly ------------------------------------------------------------


def serve_token() -> str:
    """The static bearer token for HTTP serving (created on first use, 0600)."""
    path = paths.credentials_path()
    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except json.JSONDecodeError:
            data = {}
    token = data.get(_TOKEN_KEY)
    if not isinstance(token, str) or not token:
        paths.ensure_home()
        token = secrets.token_urlsafe(32)
        data[_TOKEN_KEY] = token
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return token


def build_server() -> FastMCP:
    """A FastMCP server exposing the team-bus tools."""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP(
        "aisquare-team",
        instructions=(
            "The shared task board and working memory of a team of coding-agent "
            "sessions. Check team_board first; add work with task_add (idempotent); "
            "claim before working (task_next with claim=true); report outcomes with "
            "task_update and note_add."
        ),
    )
    for tool in (team_board, task_add, task_next, task_update, note_add, team_log, recall):
        server.add_tool(tool)
    return server


def run_stdio() -> None:
    """Serve over stdio (Claude Desktop launches and owns the process)."""
    build_server().run(transport="stdio")


class _BearerGuard:
    """Pure ASGI wrapper: every HTTP request must carry the bearer token.

    A plain ASGI class (not ``BaseHTTPMiddleware``) so the MCP SSE stream is
    passed through untouched; lifespan events also flow to the inner app.
    """

    def __init__(self, app: Any, token: str) -> None:
        self._app = app
        self._token = f"Bearer {token}"

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            headers = {key: value for key, value in scope.get("headers") or []}
            supplied = headers.get(b"authorization", b"").decode("latin-1")
            if supplied != self._token:
                from starlette.responses import JSONResponse

                response = JSONResponse({"error": "unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


def run_http(*, bind: str, port: int) -> None:
    """Serve streamable HTTP with mandatory bearer-token auth."""
    import uvicorn

    token = serve_token()
    server = build_server()
    server.settings.host = bind
    server.settings.port = port
    app = server.streamable_http_app()
    uvicorn.run(_BearerGuard(app, token), host=bind, port=port, log_level="warning")
