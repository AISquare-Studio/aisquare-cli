"""``aisquare serve`` — the orchestrator as an MCP server for remote agents.

Lets Claude clients that are not local Claude Code sessions (the Claude
desktop app on the Windows side of WSL2, a browser-debugging agent, another
machine on the LAN) read the board and file tasks, notes and feedback onto
the same board.

Remote callers act through a **virtual session** (``mcp:<client>``), so their
traffic is attributed on the board and flows into every local session's
deltas like any teammate's. The tool surface is deliberately small — nine
tools — because giant MCP surfaces burn context before any work happens.

Failing tools surface as real MCP error results (``isError: true``), never as
error-prefixed success strings, and the error wording is a contract: remote
agents self-correct off it, so it must reach them preserved end-to-end.

Transports:

- ``stdio`` — for Claude Desktop launching the server itself (on Windows:
  ``wsl -e … aisquare serve --stdio``). No network, no token.
- ``streamable HTTP`` — bound to 127.0.0.1 by default and always guarded by
  the static bearer token stored in ``~/.aisquare/credentials``, restricted to
  the current user (0600 on POSIX, an equivalent ACL on Windows).

This module needs the optional ``mcp`` dependency (``pip install
aisquare-cli[serve]``); the CLI imports it lazily and explains if missing.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import sys
import time
from typing import TYPE_CHECKING, Any, cast

from aisquare.core import credentials as credentials_store
from aisquare.core import paths
from aisquare.core.store import is_locked_error
from aisquare.models import TaskStatus, TeamSession
from aisquare.services import team as team_service
from aisquare.services.team import ClaimLostError, DeliveryUnconfirmedError, TeamDisabledError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mcp.server.fastmcp import FastMCP
    from mcp.types import CallToolResult, ContentBlock

_TOKEN_KEY = "serve_token"
DEFAULT_PORT = 8747
DEFAULT_CLOSE_AFTER = 300
"""Idle seconds before a stdio server closes itself (0 = run forever)."""


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
    """Register (or revive) the virtual session with the orchestrator; returns its id.

    Gated like every other orchestrator entry point: the master switch must be on and
    the project must already be activated (``aisquare serve`` activates its
    project explicitly at startup). Without the gate, one read-only MCP call
    against a never-opted-in directory would permanently activate it.
    """
    from datetime import UTC, datetime

    from aisquare.core.orchestrator import team_enabled, team_project
    from aisquare.core.store import store_session

    if not team_enabled():
        raise TeamDisabledError()
    with store_session() as store:
        project = team_project(None)
        if not store.team_active(project.id):
            raise ValueError(
                f"the agent orchestrator is not active for {project.root} — start `aisquare serve` "
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


def _tool_error(message: str) -> Exception:
    """A ``ToolError`` carrying ``message`` verbatim (``mcp`` imports lazily)."""
    from mcp.server.fastmcp.exceptions import ToolError

    return ToolError(message)


def _guard(fn: Any, *args: Any, **kwargs: Any) -> str:
    """Run a service call; failures become MCP tool errors (``isError: true``).

    The texts are the same stable ``error: …`` wording the tools have always
    used — remote agents self-correct off it — moved off the success channel
    onto the protocol's error channel.
    """
    try:
        return str(fn(*args, **kwargs))
    except TeamDisabledError as exc:
        raise _tool_error(
            "error: the agent orchestrator is disabled on the host (AISQUARE_TEAM=0)"
        ) from exc
    except ClaimLostError as exc:
        raise _tool_error(f"error: {exc}") from exc
    except DeliveryUnconfirmedError as exc:
        # The #20 contract crosses the remote surface too: an unconfirmed
        # write must reach the agent as the error wording, never a raw
        # tool exception that reads like an infrastructure hiccup.
        raise _tool_error(f"error: {exc}") from exc
    except sqlite3.DatabaseError as exc:
        # Same narrowing as the CLI: lock/busy is a retryable condition,
        # everything else is a real store error with its cause preserved.
        if is_locked_error(exc):
            raise _tool_error(f"error: context store busy ({exc}) — retry shortly") from exc
        raise _tool_error(f"error: context store error: {exc}") from exc
    except LookupError as exc:
        # KeyError (unknown ref) and AmbiguousIdError (short prefix) both
        # subclass LookupError; remote callers get the error contract, not
        # a raw tool exception. Keep the "nothing matches …" wording so a
        # remote agent can self-correct rather than getting a naked ref.
        if isinstance(exc, KeyError):
            raise _tool_error(
                f"error: nothing matches {(exc.args[0] if exc.args else exc)!r}"
            ) from exc
        raise _tool_error(f"error: {exc}") from exc
    except ValueError as exc:
        raise _tool_error(f"error: {exc}") from exc


# --- the nine tools (plain functions; registered on FastMCP below) -----------


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
        from typing import get_args

        statuses = get_args(TaskStatus)
        if status not in statuses:
            raise _tool_error(f"error: status must be one of: {', '.join(statuses)}")
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
                raise _tool_error("error: block requires a note (the reason)")
            task = team_service.block_task(ref, reason=note, session_ref=me)
        elif action == "reopen":
            if not note:
                raise _tool_error("error: reopen requires a note (the feedback)")
            task = team_service.reopen_task(ref, reason=note, session_ref=me)
        elif action == "release":
            task = team_service.release_task(ref, session_ref=me)
        elif action == "drop":
            task = team_service.drop_task(ref, session_ref=me)
        else:
            raise _tool_error("error: action must be done, review, block, reopen, release or drop")
        return f"{task.id} is now {task.status}"

    return _guard(run)


def note_add(
    text: str, kind: str = "note", task: str | None = None, to_role: str | None = None
) -> str:
    """Share a note, decision, question or result with every session on the board."""

    def run() -> str:
        me = _ensure_virtual_session()
        event = team_service.add_note(
            text, session_ref=me, task_ref=task, to_role=to_role, kind=kind
        )
        # The receipt names the board (project) that recorded the event, so a
        # remote agent can detect a write that landed somewhere unexpected.
        return f"shared ({event.kind}) as event seq {event.seq} on board {event.project_id}"

    return _guard(run)


def team_log(since_seq: int = 0, limit: int = 20, by_session: str | None = None) -> str:
    """Team events after ``since_seq`` (track the returned seq as your cursor).

    ``by_session`` filters to one author: a session id/prefix, or the literal
    ``"me"`` for your own virtual session — read back your own recent writes.
    """

    def run() -> str:
        me = _ensure_virtual_session()
        author = me if by_session == "me" else by_session
        events = team_service.log_events(
            limit=limit,
            by=author,
            since_seq=since_seq,
            session_ref=me,
        )
        return json.dumps(
            {
                "events": [event.as_envelope().model_dump(mode="json") for event in events],
                "latest_seq": events[-1].seq if events else since_seq,
            }
        )

    return _guard(run)


def verify(receipt: str) -> str:
    """Re-check a write receipt (a seq number or event id): is it on this board?

    The pull side of delivery trust: every write tool's success names ``seq``
    and board; this re-proves the write landed, any time.
    """

    def run() -> str:
        me = _ensure_virtual_session()
        result = team_service.verify_receipt(receipt, session_ref=me)
        if result.event is None:
            hint = f" — it exists on board {result.elsewhere}" if result.elsewhere else ""
            raise _tool_error(
                f"error: no event matches receipt {receipt!r} on board {result.board_name}{hint}"
            )
        return (
            f"delivered · seq {result.event.seq} on board {result.event.project_id}: {result.line}"
        )

    return _guard(run)


def signal(name: str, value: str | None = None) -> str:
    """Set (value given) or read (no value) a named board state — never free text.

    Sets emit a ``signal`` event whose payload carries structured
    ``name``/``value``/``prev``/``set_by`` fields; watchers filter
    ``team_log`` by kind and key on payload fields, so prose like
    "NOT READY" can never trip a ``ready`` watcher (#23).
    """

    def run() -> str:
        me = _ensure_virtual_session()
        if value is None:
            state = team_service.read_signal(name, session_ref=me)
            if state is None:
                raise _tool_error(f"error: no signal named {name!r} on this board")
            return (
                f"{state.name} = {state.value} · set by {state.set_by or 'cli'} · seq {state.seq}"
            )
        state, prev = team_service.set_signal(name, value, session_ref=me)
        was = f" (was {prev})" if prev is not None else ""
        return f"signal {state.name}: {state.value}{was} · seq {state.seq}"

    return _guard(run)


def recall(query: str) -> str:
    """Search the team's long-term memory (distilled decisions and outcomes)."""

    def run() -> str:
        output = team_service.recall(query)
        return output if output is not None else "brain unavailable (nothing distilled yet?)"

    return _guard(run)


# --- server assembly ------------------------------------------------------------


def serve_token() -> str:
    """The static bearer token for HTTP serving, created on first use, owner-only."""
    # Read-merge-write through the shared helper: this file also holds the API
    # key that `init --api-key` stores, and reading a non-JSON file as "no data"
    # is what silently discarded it.
    token = credentials_store.load_all().get(_TOKEN_KEY)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        _, restricted = credentials_store.store(**{_TOKEN_KEY: token})
        if not restricted:
            # The token is the only thing standing in front of the HTTP
            # server, so an unrestricted file is worth a word on stderr.
            print(
                f"warning: could not restrict {paths.credentials_path()} to your account — "
                "other users on this machine may be able to read the serve token.",
                file=sys.stderr,
            )
    return token


def build_server() -> FastMCP:
    """A FastMCP server exposing the orchestrator tools."""
    from mcp import types
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError

    server = FastMCP(
        "aisquare-team",
        instructions=(
            "The shared task board and working memory of a team of coding-agent "
            "sessions. Check team_board first; add work with task_add (idempotent); "
            "claim before working (task_next with claim=true); report outcomes with "
            "task_update and note_add. Every write's success names a seq receipt — "
            "verify(receipt) re-proves it landed; team_log(by_session='me') reads "
            "back your own recent writes; signal(name, value?) sets or reads named "
            "board states with structured events — key on payload fields, not text."
        ),
    )
    for tool in (
        team_board,
        task_add,
        task_next,
        task_update,
        note_add,
        team_log,
        verify,
        signal,
        recall,
    ):
        server.add_tool(tool)

    async def call_tool_with_exact_errors(
        name: str, arguments: dict[str, Any]
    ) -> Sequence[ContentBlock] | dict[str, Any] | CallToolResult:
        """FastMCP's tool dispatch, minus its message-mangling exception wrap.

        The SDK's ``Tool.run`` folds every exception — ``ToolError`` included —
        into ``ToolError("Error executing tool <name>: <msg>")``, so the wording
        a failing tool chose would reach remote agents prefixed and broken.
        Unwrap our own ``ToolError`` (carried as ``__cause__``) back to its
        exact message; any other cause is a genuine bug and keeps the SDK's
        text. Either way the client gets a real error result, not a success.
        """
        try:
            return await server.call_tool(name, arguments)
        except ToolError as exc:
            message = str(exc.__cause__) if isinstance(exc.__cause__, ToolError) else str(exc)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=message)], isError=True
            )

    # Replace the handler FastMCP registered for itself (last registration
    # wins; validate_input=False matches its own).
    server._mcp_server.call_tool(validate_input=False)(call_tool_with_exact_errors)
    return server


class _StampedStdin:
    """Async line source over stdin that stamps a clock on every received line.

    Duck-types the one thing ``mcp.server.stdio.stdio_server`` does with its
    injectable ``stdin`` — ``async for line in stdin`` — so the idle watchdog
    can measure time since the LAST message the client actually sent.
    """

    def __init__(self, inner: Any, last_activity: list[float]) -> None:
        self._inner = inner
        self._last = last_activity

    def __aiter__(self) -> _StampedStdin:
        return self

    async def __anext__(self) -> str:
        line = cast(str, await self._inner.readline())
        if not line:  # EOF — the immediate-exit path, untouched by the deadline
            raise StopAsyncIteration
        self._last[0] = time.monotonic()
        return line


async def _serve_stdio_until_idle(server: FastMCP, close_after: int) -> None:
    """Run the stdio transport with an idle deadline (#19).

    The deadline counts seconds since the last inbound client message; any
    protocol traffic (handshake included) resets it. A busy server never
    exits; an abandoned one — including a client killed mid-handshake with
    the pipe write end still open, so EOF never comes — always does.
    """
    from io import TextIOWrapper

    import anyio
    from mcp.server.stdio import stdio_server

    last_activity = [time.monotonic()]
    wrapped = anyio.wrap_file(TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace"))
    stdin = _StampedStdin(wrapped, last_activity)

    async def watchdog() -> None:
        interval = max(0.2, min(2.0, close_after / 4))
        while True:
            await anyio.sleep(interval)
            if time.monotonic() - last_activity[0] < close_after:
                continue
            # stdout is the protocol channel: announce on stderr only. Then
            # os._exit, deliberately: the pending readline sits in a blocked
            # (non-daemon) worker thread that a never-EOF pipe can never
            # unblock — a normal return would hang the interpreter on that
            # thread's join and reintroduce the orphan this deadline retires.
            sys.stderr.write(
                f"aisquare serve --stdio: no client messages for {close_after}s — "
                "closing (idle deadline; --close-after 0 disables)\n"
            )
            sys.stderr.flush()
            os._exit(0)

    async with anyio.create_task_group() as tg:
        tg.start_soon(watchdog)
        # Mirrors FastMCP.run_stdio_async, which offers no seam to inject the
        # activity-stamping stdin — the public stdio_server(stdin=...) does.
        async with stdio_server(stdin=cast(Any, stdin)) as (read_stream, write_stream):
            await server._mcp_server.run(
                read_stream,
                write_stream,
                server._mcp_server.create_initialization_options(),
            )
        tg.cancel_scope.cancel()  # EOF: stop the watchdog, exit normally


def run_stdio(*, close_after: int = DEFAULT_CLOSE_AFTER) -> None:
    """Serve over stdio (Claude Desktop launches and owns the process).

    ``close_after`` is the idle deadline in seconds — time since the last
    client message — after which the server exits 0 on its own (#19).
    ``0`` disables it for deliberately persistent clients. No process
    management anywhere: the daemon minds only its own clock.
    """
    server = build_server()
    if close_after <= 0:
        server.run(transport="stdio")
        return
    import anyio

    anyio.run(_serve_stdio_until_idle, server, close_after)


class _BearerGuard:
    """Pure ASGI wrapper: every HTTP request must carry the bearer token.

    A plain ASGI class (not ``BaseHTTPMiddleware``) so the MCP SSE stream is
    passed through untouched; lifespan events also flow to the inner app.
    """

    def __init__(self, app: Any, token: str) -> None:
        self._app = app
        # Compare raw bytes: ASGI header values are bytes, and str-mode
        # secrets.compare_digest rejects non-ASCII with a TypeError, which
        # would turn a garbage Authorization header into a 500 instead of 401.
        self._token = f"Bearer {token}".encode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            headers = {key: value for key, value in scope.get("headers") or []}
            supplied = headers.get(b"authorization", b"")
            if not secrets.compare_digest(supplied, self._token):
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
