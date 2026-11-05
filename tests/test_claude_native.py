"""Opt-in tests against an installed Claude Code, with no account, no network and no spend.

Run ``AISQUARE_TEST_CLAUDE=1 pytest tests/test_claude_native*.py`` (CI's ``claude`` job runs
``python -m tests.run_claude_native`` on a pinned version). The agent runs under a config
dir of its own, whose ``settings.json`` points it at a loopback Messages stub
(:class:`Stub`) and gives it an ``apiKeyHelper`` key that is not one: no login, no network
(every non-essential call is off) and no model spend, as SMOKE11 and R3 of #113 ran it.
The stub answers real turns, tool calls included, so what is measured is Claude Code's own
behaviour end to end: its argv, environment and settings, every hook aisquare installs
round-tripped to the board, its dialogs, and the transcript it writes.

This module holds the kit the ``test_claude_native_*`` modules share (:class:`Stub`, the
``native`` fixture and :class:`Native`), the launch tests and the hook round trips.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TypeVar

import pytest

from aisquare.core import agents, personas, selfcli
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.agent_adapters.types import screen_of
from aisquare.core.config import (
    FLEET_DENY_TOOLS,
    AgentModelDefault,
    AgentModelSettings,
    load_config,
    save_config,
)
from aisquare.core.store import store_session
from aisquare.core.tmux import TmuxError, TmuxServer
from aisquare.models import FleetAgent, ProjectInfo, TeamSession
from aisquare.services import fleet, team
from tests.conftest import _PRIVATE_FLEETS

AGENT = "claude-code"
T = TypeVar("T")
#: Not a key: what the config dir's ``apiKeyHelper`` prints, so Claude Code asks nothing
#: about a key, and never sent anywhere but the loopback stub.
_KEY = "sk-ant-api03-aisquare-native-fixture-not-a-key-000000000000000000000"
#: Every call Claude Code makes besides the model's: off, so nothing leaves the machine.
QUIET_ENV = {
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}
#: What the stub answers a turn with when its prompt names no directive.
HEARD = "native fixture heard you"
#: What it answers once a tool's result comes back.
DONE = "native fixture done"
#: What Claude Code's ``/compact`` asks the model for (2.1.318), and the stub's answer.
COMPACTING = "<summary>"
SUMMARY = "<summary>native fixture summary of the conversation</summary>"
#: A directive in a prompt: ``[[bash <command>]]``, ``[[write <path> <text>]]``,
#: ``[[hold]]`` (the turn waits until the test releases it), ``[[stall]]`` (its first words
#: stream, the rest wait the same), ``[[fail]]`` (an API error).
DIRECTIVE = re.compile(r"\[\[(\w+)(?: ([^\]]*))?\]\]")

pytestmark = pytest.mark.skipif(
    os.environ.get("AISQUARE_TEST_CLAUDE") != "1", reason="opt-in native Claude Code test"
)


# --- the loopback Messages stub ----------------------------------------------------------


def _sse(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    return b"".join(
        f"event: {name}\ndata: {json.dumps(data)}\n\n".encode() for name, data in events
    )


def _usage(input_tokens: int, output_tokens: int) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def _stream(model: str, block: dict[str, Any], delta: dict[str, Any] | None, stop: str) -> bytes:
    """One assistant message of one content block, streamed as the Messages API does."""
    events: list[tuple[str, dict[str, Any]]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": f"msg_native_{time.monotonic_ns()}",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": _usage(Stub.INPUT_TOKENS, 1),
                },
            },
        ),
        (
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": block},
        ),
    ]
    if delta is not None:
        events.append(
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta})
        )
    events += [
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": Stub.OUTPUT_TOKENS},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return _sse(events)


def text_reply(model: str, text: str) -> bytes:
    return _stream(
        model, {"type": "text", "text": ""}, {"type": "text_delta", "text": text}, "end_turn"
    )


def tool_reply(model: str, name: str, arguments: dict[str, Any]) -> bytes:
    block = {
        "type": "tool_use",
        "id": f"toolu_native_{time.monotonic_ns()}",
        "name": name,
        "input": {},
    }
    delta = {"type": "input_json_delta", "partial_json": json.dumps(arguments)}
    return _stream(model, block, delta, "tool_use")


def _texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    return [
        block.get("text", "")
        for block in content or []
        if isinstance(block, dict) and block.get("type") == "text"
    ]


@dataclass
class Turn:
    """One request of the agent's main loop, as the stub read it."""

    path: str
    """Where it was posted: ``/v1/messages``, under the prefix of a slot's base URL."""
    model: str
    system: str
    said: str
    """The text of the newest user message: the prompt and what hooks added to it."""
    tool_results: list[str]
    """The results the newest user message carries back, where it answers a tool call."""
    body: dict[str, Any]

    @property
    def everything(self) -> str:
        """Every string the request carries, system prompt and messages, one per line."""
        return "\n".join(_strings(self.body))


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _turn(body: dict[str, Any], path: str = "/v1/messages") -> Turn:
    system = body.get("system")
    system_text = "\n".join(_texts(system)) if isinstance(system, list) else str(system or "")
    said, results = "", []
    for message in reversed(body.get("messages") or []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        said = "\n".join(_texts(content))
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                inner = block.get("content")
                results.append("\n".join(_texts(inner)) if not isinstance(inner, str) else inner)
        break
    return Turn(path, str(body.get("model") or ""), system_text, said, results, body)


class Stub(ThreadingHTTPServer):
    """A Messages endpoint on loopback that answers Claude Code's turns.

    A main-loop request (one that offers tools) is recorded in :attr:`turns` and answered by
    the directive in its prompt (:data:`DIRECTIVE`): a tool call, a held turn, an API
    error, or a line of text. A tool's result is answered with :data:`DONE`. Claude Code's
    side requests (the session's title, a prompt suggestion) are answered as a model with
    nothing to add would, and kept in :attr:`side`.
    """

    INPUT_TOKENS = 1200
    OUTPUT_TOKENS = 30
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Messages)
        self.turns: list[Turn] = []
        self.side: list[dict[str, Any]] = []
        self.gate = threading.Semaphore(0)
        self.holding = threading.Event()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"

    def __enter__(self) -> Stub:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for _ in range(100):
            self.gate.release()
        self.shutdown()
        self.thread.join(timeout=5)
        self.server_close()

    def release(self) -> None:
        """Let a held turn (``[[hold]]``) answer."""
        self.gate.release()

    def said(self) -> list[str]:
        with self.lock:
            return [turn.said for turn in self.turns]


class _Messages(BaseHTTPRequestHandler):
    server: Stub
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _send(self, status: int, body: bytes, kind: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("content-type", kind)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._send(404, b"{}")

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        path = self.path.split("?", 1)[0]
        if path.endswith("/count_tokens"):
            self._send(200, json.dumps({"input_tokens": Stub.INPUT_TOKENS}).encode())
            return
        if not path.endswith("/v1/messages"):
            self._send(404, b"{}")
            return
        model = str(body.get("model") or "claude-native-fixture")
        turn = _turn(body, path)
        if not body.get("tools") or turn.said.startswith("[SUGGESTION MODE"):
            with self.server.lock:
                self.server.side.append(body)
            self._reply(body, model, text_reply(model, ""), "")
            return
        with self.server.lock:
            self.server.turns.append(turn)
        if COMPACTING in turn.said:  # its own request, the conversation's last prompt in it
            self._reply(body, model, text_reply(model, SUMMARY), SUMMARY)
            return
        # The newest directive: a prompt after a failed turn rides in one user message with
        # the failed one's, and one after an interrupted tool with its refused result.
        found = DIRECTIVE.findall(turn.said)
        verb, rest = found[-1] if found else ("", "")
        if turn.tool_results and not found:
            self._reply(body, model, text_reply(model, DONE), DONE)
            return
        if verb == "bash":
            reply = tool_reply(model, "Bash", {"command": rest, "description": "native fixture"})
        elif verb == "write":
            path_, _, text = rest.partition(" ")
            reply = tool_reply(model, "Write", {"file_path": path_, "content": text + "\n"})
        elif verb == "stall":
            self._stall(model)
            return
        elif verb == "fail":
            error = {
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "native fixture refusal"},
            }
            self._send(400, json.dumps(error).encode())
            return
        else:
            if verb == "hold":
                self.server.holding.set()
                self.server.gate.acquire(timeout=60)
                self.server.holding.clear()
            reply = text_reply(model, f"{HEARD}: {rest or verb or 'ok'}")
        self._reply(body, model, reply, "")

    def _stall(self, model: str) -> None:
        """A reply whose first words stream at once and the rest only once the test releases
        it (:meth:`Stub.release`): a turn the human can interrupt mid-answer."""
        whole = text_reply(model, f"{HEARD}: stalled")
        head, _, rest = whole.partition(b"event: content_block_stop")
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(head)
        self.wfile.flush()
        self.server.holding.set()
        released = self.server.gate.acquire(timeout=60)
        self.server.holding.clear()
        if released:
            with suppress(OSError):  # interrupted: Claude Code has hung up
                self.wfile.write(b"event: content_block_stop" + rest)

    def _reply(self, body: dict[str, Any], model: str, streamed: bytes, text: str) -> None:
        if body.get("stream"):
            self._send(200, streamed, "text/event-stream")
            return
        message = {
            "id": "msg_native",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": _usage(Stub.INPUT_TOKENS, Stub.OUTPUT_TOKENS),
        }
        self._send(200, json.dumps(message).encode())


# --- the fixture ----------------------------------------------------------------------------


def claude_settings(stub: Stub, *, allow: tuple[str, ...] = (), prefix: str = "") -> dict[str, Any]:
    """A config dir's ``settings.json`` that runs Claude Code on the stub, quietly, its
    requests posted under ``prefix`` (a slot's, to tell slots apart)."""
    settings: dict[str, Any] = {
        "env": {"ANTHROPIC_BASE_URL": stub.url + prefix, **QUIET_ENV},
        "apiKeyHelper": f"echo {_KEY}",
    }
    if allow:
        settings["permissions"] = {"allow": list(allow)}
    return settings


def claude_json(*trusted: Path) -> dict[str, Any]:
    """A config dir's ``.claude.json``: onboarded, and ``trusted`` folders trusted."""
    return {
        "hasCompletedOnboarding": True,
        "lastOnboardingVersion": "2.1.318",
        "theme": "dark",
        "customApiKeyResponses": {"approved": [_KEY[-20:]], "rejected": []},
        "projects": {
            str(folder): {"hasTrustDialogAccepted": True, "allowedTools": []} for folder in trusted
        },
    }


def write_config_dir(
    config_dir: Path,
    stub: Stub,
    *trusted: Path,
    allow: tuple[str, ...] = (),
    prefix: str = "",
    email: str | None = None,
) -> None:
    """A config dir as a sign-in leaves it, less the login: its ``settings.json`` on the stub
    and its ``.claude.json``, naming ``email`` as who it is signed in as."""
    config_dir.mkdir(parents=True, exist_ok=True)
    settings = claude_settings(stub, allow=allow, prefix=prefix)
    (config_dir / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
    state = claude_json(*trusted)
    if email is not None:
        state["oauthAccount"] = {
            "emailAddress": email,
            "organizationName": "AISquare",
            "accountUuid": "0e5f5d3c-6a39-4f3a-9b7e-native000000",
        }
    (config_dir / ".claude.json").write_text(json.dumps(state), encoding="utf-8")


@dataclass
class Native:
    project: ProjectInfo
    srv: TmuxServer
    stub: Stub
    config_dir: Path
    workspace: Path
    outside: Path
    binary: str
    settings: dict[str, Any]
    """The config dir's ``settings.json`` as the operator wrote it, before aisquare's hooks."""
    spawned: list[Any] = field(default_factory=list)

    # -- what the pane shows

    def screen(self, pane: str) -> str:
        return self.srv.run("capture-pane", "-p", "-t", pane)

    def rows(self, pane: str) -> list[str]:
        return self.screen(pane).splitlines()

    def until(
        self, check: Callable[[], T | None], seconds: float, what: str, pane: str | None = None
    ) -> T:
        deadline = time.monotonic() + seconds
        while not (found := check()):
            if time.monotonic() >= deadline:
                raise AssertionError(f"{what}:\n{self.screen(pane) if pane else ''}")
            time.sleep(0.2)
        return found

    # -- what the board holds

    def listed(self, *, live_only: bool = True) -> list[FleetAgent]:
        with store_session() as handle:
            return handle.fleet_agents(self.project.id, live_only=live_only)

    def row(self, label: str) -> FleetAgent:
        with store_session() as handle:
            found = handle.get_fleet_agent(label)
            if found is None:
                found = next(
                    (a for a in handle.fleet_agents(self.project.id) if a.label == label), None
                )
        assert found is not None, label
        return found

    def sessions(self) -> list[TeamSession]:
        with store_session() as handle:
            return handle.team_sessions(self.project.id)

    def session(self, session_id: str | None) -> TeamSession | None:
        return next((s for s in self.sessions() if s.id == session_id), None)

    def state(self, session_id: str | None) -> str | None:
        found = self.session(session_id)
        return found.state if found is not None else None

    def ended(self, session_id: str | None) -> bool:
        found = self.session(session_id)
        return found is not None and found.ended_at is not None

    def prompts(self) -> list[str]:
        with store_session() as handle:
            return [p.text for p in handle.recent_prompts(self.project.id, limit=50)]

    def events(self) -> list[Any]:
        with store_session() as handle:
            return handle.recent_events(self.project.id, limit=200)

    # -- driving it

    def spawn(self, role: str = "coder", **kwargs: Any) -> Any:
        kwargs.setdefault("agent", AGENT)
        kwargs.setdefault("worktree", False)
        receipt = fleet.spawn(self.project, role, **kwargs)
        self.spawned.append(receipt)
        return receipt

    def spawning(self, prompt: str, spawned: list[Any], failed: list[BaseException]) -> Any:
        def spawn() -> None:
            try:
                spawned.append(
                    fleet.spawn(self.project, "coder", agent=AGENT, worktree=False, prompt=prompt)
                )
            except BaseException as exc:  # handed to the test's own thread
                failed.append(exc)

        thread = threading.Thread(target=spawn, daemon=True)
        thread.start()
        return thread

    def ready(self, pane: str, seconds: float = 40) -> None:
        """Until the pane's Claude Code waits at its input box, read by its own screen."""
        ready_on_screen = screen_of(get_adapter(AGENT)).ready_on_screen
        assert ready_on_screen is not None
        self.until(lambda: ready_on_screen(self.rows(pane)), seconds, "ready", pane)

    def type(self, pane: str, text: str) -> None:
        self.srv.send_literal(pane, text)
        time.sleep(0.3)
        self.srv.send_keys(pane, "Enter")

    def heard(self, words: str, seconds: float = 40) -> Turn:
        """Until the stub has a turn whose prompt holds ``words``."""

        def found() -> Turn | None:
            with self.stub.lock:
                return next((t for t in self.stub.turns if words in t.said), None)

        return self.until(found, seconds, f"a turn that says {words!r}")

    def pane_pids(self, pane: str) -> list[int]:
        """The pane's process and its descendants, oldest first (``ps``, both OSes)."""
        root = int(self.srv.run("display-message", "-p", "-t", pane, "#{pane_pid}").strip())
        table = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, check=True
        ).stdout
        children: dict[int, list[int]] = {}
        for line in table.splitlines():
            pid, ppid = (int(word) for word in line.split())
            children.setdefault(ppid, []).append(pid)
        found, todo = [], [root]
        while todo:
            pid = todo.pop(0)
            found.append(pid)
            todo.extend(children.get(pid, []))
        return found

    def agent_argv(self, pane: str) -> list[str]:
        """The command line of the pane's Claude Code process: its words exactly where
        ``/proc`` has them (Linux), else as ``ps -ww`` prints them, split at spaces (macOS).
        Without ``-ww`` procps cuts the line at 80 columns when no terminal is attached."""
        for pid in self.pane_pids(pane):
            cmdline = Path(f"/proc/{pid}/cmdline")
            if cmdline.exists():
                with suppress(OSError):
                    words = cmdline.read_bytes().decode().split("\0")[:-1]
                    if words and Path(words[0]).name in ("claude", "claude.exe"):
                        return " ".join(words).split()
                continue
            args = subprocess.run(
                ["ps", "-ww", "-o", "args=", "-p", str(pid)], capture_output=True, text=True
            ).stdout.strip()
            words = args.split()
            if words and Path(words[0]).name in ("claude", "claude.exe"):
                return words
        raise AssertionError(f"no claude process in pane {pane}")


def _inside(entry: str, folder: Path) -> bool:
    return bool(entry) and Path(entry).is_relative_to(folder)


def installed_claude() -> str:
    """The installed Claude Code, not the suite's stand-in that ``no_real_fleet`` puts first
    on PATH (conftest)."""
    path = os.environ.get("PATH", "").split(os.pathsep)
    real = [entry for entry in path if not any(_inside(entry, own) for own in _PRIVATE_FLEETS)]
    binary = shutil.which("claude", path=os.pathsep.join(real))
    assert binary, "AISQUARE_TEST_CLAUDE requires an installed Claude Code"
    return binary


def scrub_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in tuple(os.environ):
        if key.startswith(("ANTHROPIC_", "CLAUDE", "OPENAI_", "CODEX_", "AISQUARE_PLAIN_")):
            monkeypatch.delenv(key)
    monkeypatch.delenv("TMPDIR", raising=False)


@pytest.fixture
def native_options() -> dict[str, Any]:
    """What a test asks of the ``native`` fixture: ``trusted`` (the workspace's folder
    trusted on the account), ``allow`` (the tools its settings allow), ``permission_mode``
    (every role's), ``first_prompt``."""
    return {}


@pytest.fixture
def native(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_options: dict[str, Any]
) -> Iterator[Native]:
    """A project with the team on, under a home and a Claude Code config dir of their own,
    both removed after, Claude Code on the loopback stub, and a tmux server killed after
    however far the test got (review r4 of #113, finding 15)."""
    binary = installed_claude()
    path = os.environ.get("PATH", "")
    # Ahead of the stand-in, so the fleet's window starts the real one.
    monkeypatch.setenv("PATH", os.pathsep.join([str(Path(binary).parent), path]))
    outside = Path(tempfile.mkdtemp(prefix="asq-", dir="/var/tmp")).resolve()
    srv: TmuxServer | None = None
    with Stub() as stub:
        try:
            store, config_dir = outside / "h", outside / "claude"
            workspace = outside / "w"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            trusted = (workspace,) if native_options.get("trusted", True) else ()
            allow = native_options.get("allow", ())
            write_config_dir(config_dir, stub, *trusted, allow=allow)
            scrub_agent_env(monkeypatch)
            monkeypatch.chdir(workspace)  # the user's terminal, where the CLI finds the project
            monkeypatch.setenv("AISQUARE_HOME", str(store))
            monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
            monkeypatch.setattr(
                agents,
                "_aisquare_command",
                lambda *args, **kwargs: shlex.join(selfcli.argv_for([])),
            )
            agents.install_hooks(AGENT, config_dir)
            config = load_config()
            config.fleet.tmux_socket = f"asq-claude-native-{os.getpid()}"
            if "permission_mode" in native_options:
                for role in config.fleet.roles.values():
                    role.permission_mode = native_options["permission_mode"]
            if "first_prompt" in native_options:
                config.fleet.first_prompt = native_options["first_prompt"]
            save_config(config)
            project = team.activate(workspace)
            srv = fleet.server(config.fleet)
            settings = claude_settings(stub, allow=allow)
            yield Native(project, srv, stub, config_dir, workspace, outside, binary, settings)
        finally:
            if srv is not None:
                end_server(srv)
            shutil.rmtree(outside, ignore_errors=True)


def end_server(srv: TmuxServer) -> None:
    """Kill the server and wait for its agents, which, hung up, still write their transcript
    and run their SessionEnd hook into the dir about to be removed."""
    agents_left = _pane_processes(srv)
    with suppress(TmuxError):  # a server the test never got to start
        srv.run("kill-server")
    deadline = time.monotonic() + 15
    while any(map(_alive, agents_left)) and time.monotonic() < deadline:
        time.sleep(0.1)
    with suppress(OSError):  # tmux 3.7c leaves the killed server's socket file
        srv.socket_path().unlink(missing_ok=True)


def _pane_processes(srv: TmuxServer) -> set[int]:
    """Every process in the server's panes, each agent and its hooks."""
    try:
        todo = [int(pid) for pid in srv.run("list-panes", "-a", "-F", "#{pane_pid}").split()]
    except (TmuxError, ValueError):
        return set()
    table = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True)
    children: dict[int, list[int]] = {}
    for line in table.stdout.splitlines():
        with suppress(ValueError):
            pid, ppid = (int(word) for word in line.split())
            children.setdefault(ppid, []).append(pid)
    seen: set[int] = set()
    while todo:
        pid = todo.pop()
        if pid not in seen:
            seen.add(pid)
            todo.extend(children.get(pid, []))
    return seen


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def options(**asked: Any) -> pytest.MarkDecorator:
    """What a test asks of the ``native`` fixture (:func:`native_options`)."""
    return pytest.mark.parametrize("native_options", [asked], ids=["native"])


def printed_env(turn: Turn) -> dict[str, str]:
    """The ``printenv`` a tool call ran, as its result came back to the model."""
    [result] = turn.tool_results
    return dict(line.partition("=")[::2] for line in result.splitlines() if "=" in line)


def until_result(native: Native, words: str, seconds: float = 40) -> Turn:
    """Until the stub has the turn that carries back the result of the call ``words`` asked."""
    asked = native.heard(words, seconds)

    def answered() -> Turn | None:
        with native.stub.lock:
            later = native.stub.turns[native.stub.turns.index(asked) + 1 :]
        return next((turn for turn in later if turn.tool_results), None)

    return native.until(answered, seconds, f"the result of {words!r}")


# --- the folder-trust dialog ----------------------------------------------------------------


def _at_the_trust_dialog(native: Native, pane: str) -> None:
    """The pane shows Claude Code's folder-trust dialog, and its screen reader reads it as
    one: the start dialog's mark, a ``trust`` prompt no key answers, a modal, not ready."""
    reader = screen_of(get_adapter(AGENT))
    native.until(lambda: "trust this folder" in native.screen(pane), 30, "the dialog", pane)
    rows = native.rows(pane)
    assert reader.start_dialog_mark and reader.start_dialog_mark in native.screen(pane)
    assert reader.screen_prompt is not None and reader.modal_on_screen is not None
    shown = reader.screen_prompt(rows)
    assert shown is not None and shown.shape == "trust", native.screen(pane)
    assert (shown.yes_key, shown.no_key) == (None, None), "the owner's to answer"
    assert reader.modal_on_screen(rows) == "the trust dialog"
    assert reader.ready_on_screen is not None and not reader.ready_on_screen(rows)


def _trust(native: Native, pane: str) -> None:
    native.srv.send_keys(pane, "Down")
    time.sleep(0.3)
    native.srv.send_keys(pane, "Enter")  # Yes, I trust this folder


@options(trusted=False)
def test_real_claude_code_keeps_its_folder_trust_dialog_and_runs_the_argv_prompt_after(
    native: Native,
) -> None:
    """A folder the account has not trusted: Claude Code holds its first message, given on
    its command line (0.9.0's ``claude -- "<message>"``), behind its folder-trust dialog,
    which fires no hook; the human trusts the folder and the message runs once, whole."""
    prompt = "hello from the spawn\nsecond line of it"
    receipt = native.spawn(prompt=prompt)
    pane = receipt.agent.pane_id
    _at_the_trust_dialog(native, pane)
    time.sleep(3)  # well past the agent's start
    assert native.sessions() == [], "no hook fires at the dialog"
    assert native.stub.turns == [], "nothing reached the model"
    facts = native.srv.pane_facts(pane)
    assert facts is not None and not facts.dead, "the agent is still there"
    _trust(native, pane)
    native.heard("second line of it")
    native.until(lambda: prompt in native.prompts(), 20, "the prompt on the board", pane)
    assert native.prompts().count(prompt) == 1, "once, whole"
    assert [turn.said.count("hello from the spawn") for turn in native.stub.turns][:1] == [1]


@options(trusted=False, first_prompt="type")
def test_real_claude_code_typed_first_prompt_waits_for_its_session_past_the_trust_dialog(
    native: Native,
) -> None:
    """``[fleet] first_prompt = "type"``: an Enter at the folder-trust dialog picks its
    default, ``No, exit`` (measured on 2.1.300), so the spawn waits for the agent's
    SessionStart: the agent stays at the dialog, the human trusts the folder, and the
    prompt is typed after, whole."""
    prompt = "hello from the spawn\nsecond line of it"
    spawned: list[Any] = []
    failed: list[BaseException] = []
    thread = native.spawning(prompt, spawned, failed)
    try:
        native.until(lambda: bool(native.listed()), 30, "the coder's row", None)
        pane = native.listed()[0].pane_id
        _at_the_trust_dialog(native, pane)
        time.sleep(3)  # well past the agent's start: the old rule typed by now
        assert native.sessions() == [], "no hook fires at the dialog"
        facts = native.srv.pane_facts(pane)
        assert facts is not None and not facts.dead, "the agent is still there"
        assert "hello from the spawn" not in native.screen(pane)
        assert thread.is_alive(), "the spawn waits for the session"
        _trust(native, pane)
        thread.join(timeout=30)
        assert not failed, failed
        [receipt] = spawned
        assert receipt.prompt_typed == "yes", receipt.notes
        native.until(lambda: prompt in native.prompts(), 20, "the prompt", pane)
        assert native.prompts().count(prompt) == 1, "once, whole"
        facts = native.srv.pane_facts(pane)
        assert facts is not None and not facts.dead
    finally:
        thread.join(timeout=5)


@options(trusted=False, first_prompt="type")
def test_real_claude_code_whose_start_the_board_cannot_see_gets_the_prompt_past_its_dialog(
    native: Native, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typed first prompt for a launch that names no session (the pin turned off;
    ``--continue`` too) gives the board no start to see, and the prompt is typed once the
    agent is in the foreground (review r4 of #113, finding 4), but not while its
    folder-trust dialog is on its screen, where the Enter would pick ``No, exit``: the
    dialog's ``Enter to confirm`` row keeps it out until the human has answered."""
    monkeypatch.setenv("AISQUARE_PIN_SESSION_ID", "0")
    spawned: list[Any] = []
    failed: list[BaseException] = []
    thread = native.spawning("hello from the spawn", spawned, failed)
    try:
        native.until(lambda: bool(native.listed()), 30, "the coder's row", None)
        assert native.listed()[0].session_id is None, "a start the board cannot see"
        pane = native.listed()[0].pane_id
        _at_the_trust_dialog(native, pane)
        time.sleep(4)  # past the settle the foreground rule types after
        facts = native.srv.pane_facts(pane)
        assert facts is not None and not facts.dead, "no Enter reached the dialog"
        assert "hello from the spawn" not in native.screen(pane)
        assert thread.is_alive(), "the spawn waits for the dialog to be answered"
        _trust(native, pane)
        thread.join(timeout=30)
        assert not failed, failed
        [receipt] = spawned
        assert receipt.prompt_typed == "yes", receipt.notes
        native.heard("hello from the spawn")
        facts = native.srv.pane_facts(pane)
        assert facts is not None and not facts.dead
    finally:
        thread.join(timeout=5)


# --- launch -----------------------------------------------------------------------------


@options(allow=("Bash(printenv)",))
def test_real_claude_code_launch_argv_env_and_settings_for_the_default_role(native: Native) -> None:
    """A coder spawned on the role's built-in settings: Claude Code runs with the argv the
    adapter and the role give it, the environment aisquare's launch exports, and the
    hooks aisquare installed in its config dir beside the operator's own settings, which
    it still applies (its ``env`` reaches the tool's shell)."""
    receipt = native.spawn(prompt="launch [[bash printenv]]")
    row = receipt.agent
    env = printed_env(until_result(native, "launch"))
    args = " ".join(native.agent_argv(row.pane_id))
    role = load_config().fleet.roles["coder"]
    assert f"--permission-mode {role.permission_mode} " in args, args
    assert f"--session-id {row.session_id} " in args, args
    assert f"--name {row.label} " in args, args
    assert f"--disallowedTools={' '.join(sorted(FLEET_DENY_TOOLS))} " in args, args
    assert '--settings {"skillListingMaxDescChars": 1}' in args, args
    assert args.endswith(" -- launch [[bash printenv]]"), "the first message, on its argv"
    assert env["AISQUARE_ROLE"] == "coder"
    assert env["AISQUARE_FLEET_AGENT"] == row.id
    assert env["AISQUARE_LAUNCH_AGENT"] == AGENT
    assert env["AISQUARE_LAUNCH_ID"]
    assert env["AISQUARE_HOME"] == os.environ["AISQUARE_HOME"]
    assert env["CLAUDE_CONFIG_DIR"] == str(native.config_dir)
    assert env["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] == "0", "its own agent teams off"
    assert env["ANTHROPIC_BASE_URL"] == native.stub.url, "the operator's settings applied"
    assert "ANTHROPIC_API_KEY" not in env
    # The config dir's settings: the operator's keys as they were, and one aisquare hook
    # for every event the adapter installs, with its timeout.
    after = json.loads((native.config_dir / "settings.json").read_text())
    assert {k: v for k, v in after.items() if k != "hooks"} == native.settings
    for spec in get_adapter(AGENT).capabilities.hooks:
        [group] = after["hooks"][spec.event]
        [entry] = group["hooks"]
        assert entry["command"].endswith(f"aisquare hook {spec.command}"), entry
        assert entry.get("timeout") == spec.timeout, entry
    # The board's session is the one the launch named, on this config dir's account.
    session = native.until(lambda: native.session(row.session_id), 20, "the session")
    assert session.native_session_id == row.session_id
    assert session.account == str(native.config_dir)
    assert session.transcript_path == str(
        native.config_dir / "projects" / encoded(native.workspace) / f"{row.session_id}.jsonl"
    )
    assert session.model == native.stub.turns[0].model
    assert native.prompts() == ["launch [[bash printenv]]"]


def encoded(folder: Path) -> str:
    """The folder a config dir keeps a project's transcripts in."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(folder))


@options()
def test_real_claude_code_launch_with_a_model_pin_extra_args_and_a_persona(native: Native) -> None:
    """The role's model pin (``[agents.models]``), its own arguments for Claude Code
    (``[fleet.roles.coder] agent_args``) and a persona reach Claude Code: the model it asks
    for, the folder it adds to its workspace, and the persona's briefing in what the model
    reads."""
    extra = native.outside / "extra-dir"
    extra.mkdir()
    config = load_config()
    config.agents.models[AGENT] = AgentModelSettings(
        roles={"coder": AgentModelDefault(model="claude-sonnet-4-5")}
    )
    config.fleet.roles["coder"].agent_args = {AGENT: ["--add-dir", str(extra)]}
    save_config(config)
    receipt = native.spawn(prompt="pinned and briefed", persona="skeptic")
    turn = native.heard("pinned and briefed")
    args = " ".join(native.agent_argv(receipt.agent.pane_id))
    assert "--model claude-sonnet-4-5" in args, args
    assert f"--add-dir {extra}" in args, args
    assert turn.model == "claude-sonnet-4-5", "the pin is the model Claude Code asks for"
    assert str(extra) in turn.everything, "Claude Code took the added folder"
    block = "\n".join(personas.briefing(personas.resolve("skeptic", native.workspace)))
    assert block.splitlines()[0] in turn.everything, "the persona's briefing reaches the model"
    session = native.until(lambda: native.session(receipt.agent.session_id), 20, "the session")
    assert session.persona == "skeptic"
    assert session.model == "claude-sonnet-4-5"


@options(allow=("Bash(printenv)",))
def test_real_claude_code_started_by_hand_with_aisquare_launch_joins_the_board(
    native: Native,
) -> None:
    """``aisquare launch coder -- "<prompt>"`` typed in a terminal, no fleet: Claude Code
    runs with the role exported, its hooks join the board as a coder, and its prompt is
    on the board."""
    command = shlex.join(
        [*selfcli.argv_for(["launch", "coder"]), "--", "by hand [[bash printenv]]"]
    )
    pane = native.srv.run(
        "new-session", "-d", "-s", "hand", "-x", "200", "-y", "50",
        "-P", "-F", "#{pane_id}", "-c", str(native.workspace), command,
    ).strip()  # fmt: skip
    env = printed_env(until_result(native, "by hand"))
    assert env["AISQUARE_ROLE"] == "coder"
    assert env["AISQUARE_LAUNCH_AGENT"] == AGENT
    assert "AISQUARE_FLEET_AGENT" not in env, "no fleet row"
    args = " ".join(native.agent_argv(pane))
    assert args.endswith(" by hand [[bash printenv]]"), args
    sessions = native.until(native.sessions, 20, "the session")
    [session] = sessions
    assert session.role == "coder" and session.agent == AGENT
    assert native.prompts() == ["by hand [[bash printenv]]"]


# --- the hooks ------------------------------------------------------------------------------


@options(permission_mode="manual")
def test_real_claude_code_fires_every_hook_aisquare_installs_and_each_reaches_the_board(
    native: Native,
) -> None:
    """Every event in the adapter's ``capabilities.hooks``, fired by real Claude Code and
    seen on the board: SessionStart (startup and compact, each handing its context back to
    the model, and a resume by hand of the ended session, which takes it back),
    UserPromptSubmit, Notification (its permission prompt, a bell), Stop, StopFailure (an
    API error) and SessionEnd. A hook added to the adapter without a round trip here fails
    the last assertion."""
    seen: dict[str, str] = {}
    receipt = native.spawn(prompt="first [[bash touch asked.txt]]")
    row = receipt.agent
    pane = row.pane_id
    first = native.heard("first")
    assert "SessionStart:startup hook success: <aisquare-team>" in first.everything
    session = native.until(lambda: native.session(row.session_id), 20, "the session")
    assert session.transcript_path and session.model
    seen["SessionStart"] = "startup"
    native.until(lambda: native.prompts() == ["first [[bash touch asked.txt]]"], 20, "prompt", pane)
    seen["UserPromptSubmit"] = "the prompt"
    # Its permission prompt: a Notification the board reads as a bell.
    native.until(lambda: native.state(row.session_id) == "attention", 30, "bell", pane)
    assert "Do you want to proceed?" in native.screen(pane)
    seen["Notification"] = "permission_prompt"
    native.srv.send_keys(pane, "Escape")  # No: the turn ends interrupted, with no Stop
    native.ready(pane)
    assert not (native.workspace / "asked.txt").exists()
    native.type(pane, "an ordinary turn")
    native.heard("an ordinary turn")
    native.until(lambda: native.state(row.session_id) == "waiting", 30, "Stop", pane)
    seen["Stop"] = "waiting"
    native.ready(pane)
    native.type(pane, "[[fail]] this one")
    failed = native.until(
        lambda: [e for e in native.events() if e.kind == "turn_failed"], 30, "failure", pane
    )
    assert "native fixture refusal" in failed[0].text, failed[0].text
    seen["StopFailure"] = failed[0].text
    native.ready(pane)
    native.type(pane, "/compact")
    native.until(lambda: "Compacted" in native.screen(pane), 40, "compacted", pane)
    native.ready(pane)
    native.type(pane, "after the compaction")
    after = native.heard("after the compaction")
    assert "SessionStart:compact hook success: <aisquare-team>" in after.everything
    seen["SessionStart"] += ", compact"
    native.ready(pane)
    native.type(pane, "/exit")
    native.until(lambda: native.ended(row.session_id), 30, "SessionEnd", pane)
    seen["SessionEnd"] = "ended"
    # Resumed by hand: its SessionStart (source resume) takes the ended session back on the
    # board before anything is typed. Whether 2.1.318 hands that hook's context to the
    # model is a race it runs against its first request (F3, pinned deterministically in
    # test_claude_native_fleet.py), so the board is what this reads.
    command = shlex.join(
        [*selfcli.argv_for(["launch", "coder"]), "--", "--resume", str(row.session_id)]
    )
    native.srv.run(
        "new-session", "-d", "-s", "resumed", "-x", "120", "-y", "40",
        "-c", str(native.workspace), command,
    )  # fmt: skip
    native.until(lambda: not native.ended(row.session_id), 30, "SessionStart resume")
    assert native.prompts().count("after the compaction") == 1, "nothing typed yet"
    native.ready("resumed")
    native.type("resumed", "after the resume")
    resumed = native.heard("after the resume")
    assert "after the compaction" in resumed.everything, "the conversation, resumed"
    seen["SessionStart"] += ", resume"
    assert set(seen) == {spec.event for spec in get_adapter(AGENT).capabilities.hooks}, seen
