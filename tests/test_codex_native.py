"""Opt-in compatibility tests against an installed Codex, using only loopback.

Run AISQUARE_TEST_CODEX=1 pytest tests/test_codex_native.py. No account, login,
provider credential, production hook trust or model spend is used.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import agents, launch_seal, outbox, selfcli
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.agent_adapters.types import store_dir, store_read_only
from aisquare.core.config import load_config, save_config
from aisquare.core.entries import new_entry
from aisquare.core.store import SqliteStore, store_session
from aisquare.services import agent_launch, fleet, native_telemetry, store_broker, team
from aisquare.services.remote_server import build_app
from tests.remote_kit_helpers import base, make_client, make_runtime, unlock

#: The real install check, before the suite's stand-in (``tests/conftest.py``).
REAL_INSTALL_PATHS = store_broker.install_paths


def _user_texts(request: dict[str, Any]) -> list[str]:
    """The user messages a provider request carries, oldest first."""
    return [
        part["text"]
        for item in request.get("input", [])
        if item.get("role") == "user"
        for part in item.get("content", [])
        if part.get("type") == "input_text"
    ]


def _answer(
    handler: BaseHTTPRequestHandler, item: dict[str, Any], headers: dict[str, str] | None = None
) -> None:
    """Answer a Responses request with ``item`` as its one output, streamed as Codex reads it,
    with ``headers`` beside the stream's own."""
    response = {
        "id": "resp_local",
        "object": "response",
        "status": "completed",
        "output": [item],
        "usage": {
            "input_tokens": 19,
            "output_tokens": 3,
            "total_tokens": 22,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }
    stream: list[dict[str, Any]] = [
        {
            "type": "response.created",
            "response": {**response, "status": "in_progress", "output": []},
        },
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": response},
    ]
    data = "".join(
        f"event: {entry['type']}\ndata: {json.dumps(entry)}\n\n" for entry in stream
    ).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _message(text: str) -> dict[str, Any]:
    return {
        "id": "msg_local",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


#: What bwrap says when it may not bring up the loopback of the network namespace it gave
#: a sandboxed command: a GitHub-hosted runner's unprivileged user namespace refuses it.
_LOOPBACK_REFUSED = "bwrap: loopback:"


def _sandbox_refused(binary: str, where: Path) -> str | None:
    """bwrap's refusal when Codex's sandbox cannot start a command on this host; ``None``
    when ``codex sandbox -- true`` runs.

    The probe is the sandbox the agent's commands run in (measured on 0.162.1: its own
    network namespace, with only ``lo``), and the refusal is the one that failed every
    sandboxed command on the runner, ``bwrap: loopback: Failed RTM_NEWADDR: Operation not
    permitted``. Only that is a reason to skip: any other failure is the sandbox the test
    is about, and fails it here.
    """
    home = where / "sandbox-probe"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENAI_", "CODEX_"))}
    probe = subprocess.run(
        [binary, "sandbox", "--", "true"],
        cwd=where,
        env={**env, "CODEX_HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    said = [line.strip() for line in (probe.stdout + probe.stderr).splitlines()]
    refusal = next((line for line in said if line.startswith(_LOOPBACK_REFUSED)), None)
    if probe.returncode != 0 and refusal is not None:
        return refusal
    assert probe.returncode == 0, "\n".join(said)
    return None


@pytest.mark.skipif(os.environ.get("AISQUARE_TEST_CODEX") != "1", reason="opt-in native Codex test")
def test_real_codex_hooks_resume_and_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = shutil.which("codex")
    assert binary, "AISQUARE_TEST_CODEX requires an installed Codex"
    native_home = tmp_path / "codex"
    native_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    requests: list[dict[str, Any]] = []

    def names(value: Any) -> list[str]:
        if isinstance(value, dict):
            return [str(v) for k, v in value.items() if k == "name"] + [
                name for v in value.values() for name in names(v)
            ]
        if isinstance(value, list):
            return [name for v in value for name in names(v)]
        return []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            if not self.path.endswith("/responses"):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")
                return
            requests.append(json.loads(raw))
            said = _user_texts(requests[-1])
            if said and said[-1] == "SLOW busy fixture":
                time.sleep(8)  # a turn long enough to tell the agent something mid-turn
            item = _message("local fixture complete")
            if len(requests) == 1:
                item = {
                    "id": "call_local",
                    "type": "function_call",
                    "call_id": "call_local",
                    "name": "exec_command",
                    "namespace": "functions",
                    "arguments": json.dumps(
                        {
                            "cmd": 'printf "local-tool-output[%s|%s]" '
                            f'"${native_telemetry.HEADERS_ENV}" "$OPERATOR_SECRET"'
                        }
                    ),
                }
            elif len(requests) == 2:
                item = {
                    "id": "call_board",
                    "type": "function_call",
                    "call_id": "call_board",
                    "name": "team_board",
                    "namespace": "mcp__aisquare",
                    "arguments": "{}",
                }
            _answer(self, item)

        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        (native_home / "config.toml").write_text(
            'model = "local-fixture"\nmodel_provider = "local"\n'
            '[model_providers.local]\nname = "Local fixture"\nwire_api = "responses"\n'
            f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
            "requires_openai_auth = false\n"
            f'[projects.{json.dumps(str(workspace))}]\ntrust_level = "trusted"\n',
            encoding="utf-8",
        )
        for key in tuple(os.environ):
            if key.startswith(("OPENAI_", "ANTHROPIC_", "CODEX_")):
                monkeypatch.delenv(key)
        monkeypatch.setenv("CODEX_HOME", str(native_home))
        monkeypatch.setenv("AISQUARE_ROLE", "coder")
        monkeypatch.setenv("AISQUARE_TEAM", "1")
        monkeypatch.setenv("AISQUARE_LAUNCH_ID", "native-fixture")
        monkeypatch.setattr(
            agents, "_aisquare_command", lambda *args, **kwargs: shlex.join(selfcli.argv_for([]))
        )
        agents.install_hooks("codex", native_home)
        with store_session() as store:
            store.add(new_entry("LOCAL_CONTEXT_MARKER_4851", "user", None, [], "test"))
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("OPENAI_", "ANTHROPIC_", "CODEX_"))
        }
        env["CODEX_HOME"] = str(native_home)
        env["AISQUARE_TEAM_HUB"] = str(workspace)
        config = load_config()
        config.explainability.enabled = True
        config.explainability.ship = True
        config.agents.mcp = True
        save_config(config)
        codex = agent_launch.resolve(agent="codex").adapter
        # A selected profile with an exclude of its own: the receiver's, given with -c,
        # replaces its list, and the profile's pattern joins it (review r3 of #113, 7).
        (native_home / "p.config.toml").write_text(
            '[shell_environment_policy]\nexclude = ["OPERATOR_SECRET"]\n', encoding="utf-8"
        )
        env["OPERATOR_SECRET"] = "kept-out"
        excluded = native_telemetry.tool_shell_excludes(native_home, ["-p", "p"], workspace)
        assert excluded == ["OPERATOR_SECRET"]
        telemetry_args, note = native_telemetry.start_receiver(env, codex, excluded)
        assert telemetry_args, note
        # The receiver's token reaches Codex in its environment, never on its command line.
        token = env[native_telemetry.HEADERS_ENV].rpartition("%20")[2]
        assert token and not any(token in arg for arg in telemetry_args)
        # Only this test's owned hook definitions are trusted by this flag.
        base = [
            binary,
            *telemetry_args,
            *agent_launch.mcp_args(agent_launch.resolve(agent="codex")),
            "-c",
            "mcp_optional_startup_grace_ms=0",
            "-c",
            "mcp_servers.aisquare.required=true",
            "-c",
            'mcp_servers.aisquare.tools.team_board.approval_mode="approve"',
            "--dangerously-bypass-hook-trust",
            "-c",
            "agents.enabled=false",
            "-p",
            "p",
            "exec",
        ]
        try:
            first = subprocess.run(
                [*base, "--skip-git-repo-check", "--json", "check local fixture"],
                cwd=workspace,
                env=env,
                capture_output=True,
                text=True,
                timeout=45,
            )
            assert first.returncode == 0, first.stderr + first.stdout
            stream: list[dict[str, Any]] = [
                json.loads(line) for line in first.stdout.splitlines() if line.startswith("{")
            ]
            native_id = next(
                event["thread_id"] for event in stream if event["type"] == "thread.started"
            )
            assert any(
                event["type"] == "turn.completed" and event["usage"]["input_tokens"] == 57
                for event in stream
            ), first.stdout + first.stderr
            with store_session() as store:
                bound = store.get_meta("launch-session:native-fixture")
                session = store.get_session(bound) if bound else None
                assert session is not None, first.stderr
                assert session.agent == "codex" and session.native_session_id == native_id
                assert len(store.recent_prompts(session.project_id)) == 1
            resumed = subprocess.run(
                [
                    *base,
                    "resume",
                    "--skip-git-repo-check",
                    "--json",
                    native_id,
                    "resume local fixture",
                ],
                cwd=workspace,
                env=env,
                capture_output=True,
                text=True,
                timeout=45,
            )
            assert resumed.returncode == 0, resumed.stderr + resumed.stdout
            with store_session() as store:
                assert store.get_meta("launch-session:native-fixture") == bound
                assert len(store.recent_prompts(session.project_id)) == 2
            assert len(requests) == 4
            # The receiver's token is Codex's exporter's alone: the shells its tools run in
            # never see it, where an app's OpenTelemetry SDK would send it on.
            if _sandbox_refused(binary, tmp_path) is None:  # a runner's sandbox runs none
                assert "local-tool-output[|]" in json.dumps(requests[1])
            assert token not in json.dumps(requests[1])
            assert "mcp:remote" not in json.dumps(requests[2])
            assert "unknown tool" not in json.dumps(requests[2]).lower()
            board_result = next(
                item["output"]
                for item in requests[2]["input"]
                if item.get("call_id") == "call_board"
                and item.get("type") == "function_call_output"
            )
            assert bound and bound[:8] in json.dumps(board_result), (
                board_result,
                names(requests[0]["tools"]),
                first.stderr,
            )
            assert "LOCAL_CONTEXT_MARKER_4851" in json.dumps(requests[0]), (
                "native request must contain hook context"
            )
            recorded = [json.loads(path.read_text()) for path in outbox.pending()]
            native = [record for record in recorded if record.get("kind") == "native_event"]
            assert native, note
            assert any("tool" in str(record["native"]["event.name"]) for record in native)
            assert (
                sum(int(record["native"].get("input_token_count") or 0) for record in native) == 76
            )
            assert any(
                int(record["native"].get("input_token_count") or 0) == 19 for record in native
            ), [record["native"] for record in native]
            assert all(
                record["native"].get("provider_name") == "Local fixture"
                for record in native
                if record["native"].get("input_token_count")
            )
            assert any(
                record["session_id"] == bound and record["run_key"] == "native-fixture"
                for record in native
            )
            # Exercise the production fleet -> asq launch -> interactive Codex
            # path as well as exec. All trust applies to this owned fixture.
            config.fleet.tmux_socket = f"asq-codex-test-{os.getpid()}"
            save_config(config)
            project = team.activate(workspace)
            srv = fleet.server(config.fleet)
            try:
                receipt = fleet.spawn(
                    project,
                    "coder",
                    agent="codex",
                    worktree=False,
                    prompt="interactive local fixture",
                    agent_args=["--dangerously-bypass-hook-trust", "--no-alt-screen"],
                )
                deadline = time.monotonic() + 20
                status = fleet.status_of(receipt.agent)
                while status.state != "waiting" and time.monotonic() < deadline:
                    time.sleep(0.1)
                    status = fleet.status_of(receipt.agent)
                assert status.session is not None and status.state == "waiting", srv.run(
                    "capture-pane", "-p", "-t", receipt.agent.pane_id
                )
                assert status.session.agent == "codex"
                assert fleet.tell(
                    project, receipt.agent.label, "second interactive fixture"
                ).delivered
                srv.resize(receipt.agent.pane_id, 100, 30)
                deadline = time.monotonic() + 10
                while len(requests) < 6 and time.monotonic() < deadline:
                    time.sleep(0.1)
                assert len(requests) >= 6
                label = receipt.agent.label

                def until(check: Any, seconds: float, what: str, pane: str) -> None:
                    deadline = time.monotonic() + seconds
                    while not check():
                        if time.monotonic() >= deadline:
                            screen = srv.run("capture-pane", "-p", "-t", pane)
                            raise AssertionError(f"{what}:\n{screen}")
                        time.sleep(0.1)

                def state(agent: Any) -> str | None:
                    session = fleet.status_of(agent).session
                    return session.state if session is not None else None

                def ran(text: str) -> bool:
                    return any((_user_texts(request) or [""])[-1] == text for request in requests)

                # A busy agent is told through `codex queue`, which reaches the
                # interactive session: the message runs as its next turn. One that
                # starts with a dash is the message, not an option of the queue's.
                pane = receipt.agent.pane_id
                until(lambda: state(receipt.agent) == "waiting", 20, "waiting", pane)
                assert fleet.tell(project, label, "SLOW busy fixture").delivered
                until(lambda: state(receipt.agent) == "working", 10, "working", pane)
                told = "--no-verify queued fixture line"
                queued = fleet.tell(project, label, told)
                assert queued.delivered and "queued for its next turn" in queued.how, queued
                until(lambda: ran(told), 40, "the queued line ran", pane)
                # A restart resumes the bound thread: `codex ... resume <id> -- <line>`,
                # the history before it and the claims with it.
                until(lambda: state(receipt.agent) == "waiting", 20, "waiting again", pane)
                session = fleet.status_of(receipt.agent).session
                assert session is not None
                task, _ = team.add_task("graceful stop fixture", cwd=workspace)
                with store_session() as store:
                    assert store.claim_task(
                        task.id, session.id, datetime.now(UTC) + timedelta(minutes=5)
                    )
                restarted = fleet.restart(project, label)
                assert restarted.resumed and restarted.was_running, restarted.notes
                assert restarted.started.session_id == session.id
                line = fleet._restart_prompt(restarted.started)
                pane = restarted.started.pane_id
                until(
                    lambda: any(
                        (_user_texts(request) or [""])[-1] == line and told in _user_texts(request)
                        for request in requests
                    ),
                    40,
                    "the restart resumed its thread with its history",
                    pane,
                )
                with store_session() as store:
                    held = store.get_task(task.id)
                    assert held is not None and held.claimed_by == session.id
                # A /clear: no SessionEnd for the thread it leaves; the new thread's
                # SessionStart (source clear) comes with its first prompt, from the same
                # process, and the row and the claim move to it there.
                until(lambda: state(restarted.started) == "waiting", 20, "waiting", pane)
                srv.send_literal(pane, "/clear")
                time.sleep(0.3)
                srv.send_keys(pane, "Enter")
                time.sleep(1)
                assert fleet.tell(project, label, "after clear fixture").delivered

                def cleared() -> bool:
                    with store_session() as store:
                        row = store.get_fleet_agent(restarted.started.id)
                    return row is not None and row.session_id not in (None, session.id)

                until(cleared, 20, "the /clear moved the row", pane)
                with store_session() as store:
                    row = store.get_fleet_agent(restarted.started.id)
                    held = store.get_task(task.id)
                assert row is not None and held is not None
                assert held.claimed_by == row.session_id, "the claim moved with the row"
                # A graceful stop: Codex's own SessionEnd hook gives the claim back
                # ("session ended") before the window goes; a window kill fires no
                # hook and leaves the release to stop ("agent stopped").
                stopped = fleet.stop(project, label, grace=10)
                assert not stopped.released, stopped
                with store_session() as store:
                    ended = store.get_session(row.session_id or "")
                    released = [
                        event.text
                        for event in store.recent_events(project.id, limit=50)
                        if event.kind == "task_released" and event.task_id == task.id
                    ]
                assert ended is not None and ended.ended_at is not None
                assert released == ["graceful stop fixture (session ended)"], released
            finally:
                if srv.has_session(
                    fleet.session_name(fleet.ensure_codename(project).codename or "")
                ):
                    srv.run("kill-server")
            # Native argv has no English-word heuristic: -migrate is -m igrate.
            # A literal leading dash requires Codex's own -- separator.
            for args, expected in (
                (["-migrate", "fixture"], "igrate"),
                (["-migrate the schema", "fixture"], "igrate the schema"),
                (["--", "-migrate the schema"], "local-fixture"),
            ):
                result = subprocess.run(
                    [*base, "--skip-git-repo-check", "--json", *args],
                    cwd=workspace,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=45,
                )
                assert result.returncode == 0, result.stdout + result.stderr
                assert requests[-1]["model"] == expected
            # Observe the provider request too: accepting/reporting an alias
            # is insufficient if whitespace sends it to an unused TOML key.
            # Codex's own max passes unchanged; ultracode is max. (A level the model
            # lacks is Codex's to settle: gpt-5.4 given ultra sends its default.)
            selected = agent_launch.resolve(agent="codex")
            for alias, level in (("max", "max"), ("ultracode", "max")):
                arguments = agent_launch.prepare_arguments(
                    selected,
                    [],
                    [
                        "-c",
                        "model = gpt-5.4",
                        "-c",
                        f"model_reasoning_effort = {alias}",
                        "fixture",
                    ],
                )
                result = subprocess.run(
                    [*base, "--skip-git-repo-check", "--json", *arguments.argv],
                    cwd=workspace,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=45,
                )
                assert result.returncode == 0, result.stdout + result.stderr
                assert requests[-1]["model"] == "gpt-5.4"
                assert requests[-1]["reasoning"]["effort"] == level
        finally:
            server.shutdown()
            thread.join(timeout=2)


@pytest.mark.skipif(os.environ.get("AISQUARE_TEST_CODEX") != "1", reason="opt-in native Codex test")
def test_real_codex_panes_take_a_told_line_before_any_hook_and_the_managers_nudge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """How a line gets into Codex's TUI (lane B of #113's live smoke): at its empty prompt,
    the Enter after a gap. A coder started with no prompt has fired no hook, and is told
    through its pane; one at the hook-review dialog, as quiet, is not typed into; a manager
    waiting at its prompt is woken by a coder's ``task review``."""
    binary = shutil.which("codex")
    assert binary, "AISQUARE_TEST_CODEX requires an installed Codex"
    native_home = tmp_path / "codex"
    native_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            if not self.path.endswith("/responses"):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")
                return
            requests.append(json.loads(raw))
            _answer(self, _message("ok"))

        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        (native_home / "config.toml").write_text(
            'model = "local-fixture"\nmodel_provider = "local"\n'
            '[model_providers.local]\nname = "Local fixture"\nwire_api = "responses"\n'
            f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
            "requires_openai_auth = false\n"
            f'[projects.{json.dumps(str(workspace))}]\ntrust_level = "trusted"\n',
            encoding="utf-8",
        )
        for key in tuple(os.environ):
            if key.startswith(("OPENAI_", "ANTHROPIC_", "CODEX_")):
                monkeypatch.delenv(key)
        monkeypatch.setenv("CODEX_HOME", str(native_home))
        monkeypatch.setattr(
            agents, "_aisquare_command", lambda *args, **kwargs: shlex.join(selfcli.argv_for([]))
        )
        agents.install_hooks("codex", native_home)
        config = load_config()
        config.fleet.tmux_socket = f"asq-codex-panes-{os.getpid()}"
        save_config(config)
        project = team.activate(workspace)
        srv = fleet.server(config.fleet)
        # Only this test's owned hook definitions are trusted by this flag.
        trusted = ["--dangerously-bypass-hook-trust"]

        def screen(pane: str) -> str:
            return srv.run("capture-pane", "-p", "-t", pane)

        def until(check: Any, seconds: float, what: str, pane: str) -> None:
            deadline = time.monotonic() + seconds
            while not check():
                if time.monotonic() >= deadline:
                    raise AssertionError(f"{what}:\n{screen(pane)}")
                time.sleep(0.2)

        def ran(text: str) -> bool:
            return any(text in _user_texts(request) for request in requests)

        def quiet_at_its_prompt(agent: Any) -> bool:
            lines = [line.strip() for line in screen(agent.pane_id).splitlines() if line.strip()]
            return (
                fleet.status_of(agent).state == "waiting"
                and bool(lines)
                and lines[-1].startswith("? for shortcuts")
            )

        try:
            # Welcome's coder: started with no prompt, so Codex has fired no hook.
            coder = fleet.spawn(
                project, "coder", agent="codex", worktree=False, agent_args=trusted
            ).agent
            until(
                lambda: quiet_at_its_prompt(coder),
                30,
                "a quiet empty prompt",
                coder.pane_id,
            )
            assert fleet.status_of(coder).session is None, "no hook before the first prompt"
            told = fleet.tell(project, coder.label, "reply ok")
            assert told.delivered and "typed" in told.how, told
            until(lambda: ran("reply ok"), 20, "the told line ran", coder.pane_id)

            def bound() -> str | None:
                with store_session() as store:
                    row = store.get_fleet_agent(coder.id)
                return row.session_id if row is not None else None

            until(lambda: bound() is not None, 20, "its first prompt bound the row", coder.pane_id)
            # A Codex whose hooks are not trusted opens at the hook review, as quiet as its
            # prompt: an Enter there would answer it, so the line is a board note.
            untrusted = fleet.spawn(project, "coder", agent="codex", worktree=False).agent
            until(
                lambda: (
                    fleet.status_of(untrusted).state == "waiting"
                    and "Hooks need review" in screen(untrusted.pane_id)
                ),
                30,
                "a quiet hook review",
                untrusted.pane_id,
            )
            refused = fleet.tell(project, untrusted.label, "not into a dialog")
            assert not refused.delivered and "board note" in refused.how, refused
            time.sleep(1)
            assert "Hooks need review" in screen(untrusted.pane_id)
            assert "not into a dialog" not in screen(untrusted.pane_id)
            # The manager waits at its prompt; a coder's task review wakes it.
            manager = fleet.spawn(
                project,
                "manager",
                agent="codex",
                worktree=False,
                prompt="manager fixture",
                agent_args=trusted,
            ).agent

            def waiting(agent: Any) -> bool:
                session = fleet.status_of(agent).session
                return session is not None and session.state == "waiting"

            until(lambda: waiting(manager), 40, "the manager waiting", manager.pane_id)
            coder_session = bound()
            task, _ = team.add_task("lane B wake fixture", cwd=workspace)
            team.claim_task(task.id, session_ref=coder_session)
            team.review_task(task.id, note="ready", session_ref=coder_session)
            until(lambda: ran(fleet.NUDGE_TEXT), 20, "the nudge woke the manager", manager.pane_id)
            with store_session() as store:
                prompts = store.recent_prompts(project.id, limit=50)
            assert any(
                prompt.text == fleet.NUDGE_TEXT and prompt.source == "codex" for prompt in prompts
            ), [(prompt.text, prompt.source) for prompt in prompts]
            assert fleet.NUDGE_TEXT not in screen(manager.pane_id).splitlines()[-4:], (
                "nothing left unsent in its prompt"
            )
        finally:
            if srv.has_session(fleet.session_name(fleet.ensure_codename(project).codename or "")):
                srv.run("kill-server")
            server.shutdown()
            thread.join(timeout=2)


#: What a write to the store from a sandboxed command says: the store is read-only in a
#: Codex agent's sandbox, all of it (review r9 of #113: the broker). SQLite says the
#: first while another connection keeps the WAL's files, the second when it would have
#: to make them in the read-only directory.
_READ_ONLY = ("attempt to write a readonly database", "unable to open database file")


def _refused_then_made(told: str, output: str, *, first: Any = None) -> None:
    """The write ``told`` (``RUN <argv>``) was run in an agent's sandbox, and ``output``
    is what it printed: refused there, the store being read-only. ``first`` checks what
    the refusal left (nothing). The same write is then made here, outside, as a sandbox
    could make it before the broker, so the launch records are still held to it."""
    assert any(said in output for said in _READ_ONLY) and "rewrote 1" not in output, output
    if first is not None:
        first()
    made = subprocess.run(
        shlex.split(told.removeprefix("RUN ")), capture_output=True, text=True, check=False
    )
    assert made.returncode == 0 and "rewrote" in made.stdout, made.stdout + made.stderr


class _Scripted(BaseHTTPRequestHandler):
    """A loopback Responses provider driven by the last user line: ``RUN <cmd>`` calls
    ``exec_command`` in the sandbox, ``ESC <cmd>`` asks for it outside (``sandbox_permissions
    = require_escalated``), anything else is answered. Codex's automatic reviewer (model
    ``codex-auto-review``) is told to allow. Each request lands in ``server.requests``."""

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        if not self.path.endswith("/responses"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")
            return
        request = json.loads(raw)
        requests: list[dict[str, Any]] = self.server.requests  # type: ignore[attr-defined]
        requests.append(request)
        if request.get("model") == "codex-auto-review":
            verdict = {"outcome": "allow", "risk_level": "low", "rationale": "fixture"}
            _answer(self, _message(json.dumps(verdict)))
            return
        items, said = request.get("input", []), _user_texts(request)
        verb, _, command = (said[-1] if said else "").partition(" ")
        if verb not in {"RUN", "ESC"} or items[-1].get("type") == "function_call_output":
            _answer(self, _message("local fixture complete"))
            return
        arguments = {"cmd": command}
        if verb == "ESC":
            arguments["sandbox_permissions"] = "require_escalated"
            arguments["justification"] = "reach the aisquare board"
        call = f"call_{len(requests)}"
        _answer(
            self,
            {
                "id": call,
                "type": "function_call",
                "call_id": call,
                "name": "exec_command",
                "namespace": "functions",
                "arguments": json.dumps(arguments),
            },
        )

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.mark.skipif(os.environ.get("AISQUARE_TEST_CODEX") != "1", reason="opt-in native Codex test")
def test_real_codex_reaches_the_store_from_its_sandbox_and_rings_only_for_a_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lane A of the t8 smoke, on the real Codex with only loopback.

    The aisquare home is OUTSIDE /tmp, which every Codex sandbox may write (the suite's
    own home is under it, and so hid the defect), and short: Codex binds a socket in its
    home. Without the store profile a sandboxed ``aisquare note`` cannot open the store;
    a fleet coder (``--approve-for-me``) posts a note and claims a task on the first,
    sandboxed attempt, through its launch's broker (review r9 of #113), runs every board
    call of the role protocol there the same way, is refused one as another session,
    writes nothing in the store (its database included), and an escalation its
    automatic reviewer allows rings no bell. Restarted onto
    ``workspace:ask`` the human answers: Codex's own approval view in the pane, read as
    attention for as long as it redraws unanswered, working again after ``y``. A
    reviewer's read-only seat posts its verdict and cannot write the worktree.
    """
    binary = shutil.which("codex")
    assert binary, "AISQUARE_TEST_CODEX requires an installed Codex"
    refused = _sandbox_refused(binary, tmp_path)
    if refused is not None:
        pytest.skip(f"Codex's sandbox cannot start a command on this host: {refused}")
    outside = Path(tempfile.mkdtemp(prefix="asq-", dir="/var/tmp"))
    store, native_home = outside / "h", outside / "c"
    native_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cli = shlex.join(selfcli.argv_for([]))
    with ThreadingHTTPServer(("127.0.0.1", 0), _Scripted) as server:
        server.requests = []  # type: ignore[attr-defined]
        requests: list[dict[str, Any]] = server.requests  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        (native_home / "config.toml").write_text(
            'model = "local-fixture"\nmodel_provider = "local"\n'
            '[model_providers.local]\nname = "Local fixture"\nwire_api = "responses"\n'
            f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
            "requires_openai_auth = false\n"
            f'[projects.{json.dumps(str(workspace))}]\ntrust_level = "trusted"\n',
            encoding="utf-8",
        )
        for key in tuple(os.environ):
            if key.startswith(("OPENAI_", "ANTHROPIC_", "CODEX_")):
                monkeypatch.delenv(key)
        monkeypatch.delenv("TMPDIR", raising=False)
        monkeypatch.setenv("AISQUARE_HOME", str(store))
        monkeypatch.setenv("CODEX_HOME", str(native_home))
        monkeypatch.setattr(
            agents, "_aisquare_command", lambda *args, **kwargs: shlex.join(selfcli.argv_for([]))
        )
        agents.install_hooks("codex", native_home)
        config = load_config()
        config.fleet.tmux_socket = f"asq-codex-sandbox-{os.getpid()}"
        save_config(config)
        project = team.activate(workspace)
        for kept in store_read_only(store_dir()):
            kept.parent.mkdir(parents=True, exist_ok=True)
        (store / "codex-accounts").mkdir(exist_ok=True)

        def outputs() -> list[str]:
            """Each command's output, oldest first: every request replays the history."""
            found: dict[str, str] = {}
            for request in list(requests):
                for item in request.get("input", []):
                    if item.get("type") == "function_call_output":
                        found.setdefault(str(item.get("call_id")), str(item.get("output")))
            return list(found.values())

        def reviews() -> int:
            return sum(1 for request in requests if request.get("model") == "codex-auto-review")

        # The defect, measured first: Codex's own workspace sandbox cannot open the store.
        env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENAI_", "CODEX_"))}
        env["CODEX_HOME"] = str(native_home)
        plain = subprocess.run(
            [
                binary,
                "--dangerously-bypass-hook-trust",
                "exec",
                "--skip-git-repo-check",
                "--approve-for-me",
                f"RUN {cli} board",
            ],
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
        assert plain.returncode == 0, plain.stderr
        assert "cannot be opened" in outputs()[-1], outputs()[-1:]
        srv = fleet.server(config.fleet)
        try:

            def until(check: Any, seconds: float, what: str, pane: str) -> None:
                deadline = time.monotonic() + seconds
                while not check():
                    if time.monotonic() >= deadline:
                        screen = srv.run("capture-pane", "-p", "-t", pane)
                        raise AssertionError(f"{what}:\n{screen}\n{outputs()[-2:]}")
                    time.sleep(0.2)

            def answered(before: int) -> bool:
                return len(outputs()) > before

            def state(agent: Any) -> str:
                return fleet.status_of(agent).state

            seen = len(outputs())
            coder = fleet.spawn(
                project,
                "coder",
                agent="codex",
                worktree=False,
                prompt=f"RUN {cli} note 'lane-a sandboxed note'",
                agent_args=["--dangerously-bypass-hook-trust", "--no-alt-screen"],
            ).agent
            pane, label = coder.pane_id, coder.label
            until(lambda: answered(seen), 40, "the sandboxed note ran", pane)
            assert "shared (note)" in outputs()[-1], outputs()[-1]
            until(lambda: state(coder) == "waiting", 20, "waiting", pane)
            row = fleet.status_of(coder).session
            assert row is not None
            task, _ = team.add_task("lane-a sandboxed claim", cwd=workspace)
            seen = len(outputs())
            claim = f"RUN {cli} task claim {task.id} --as {row.id[:8]}"
            assert fleet.tell(project, label, claim).delivered
            until(lambda: answered(seen), 30, "the sandboxed claim ran", pane)
            assert "claimed" in outputs()[-1], outputs()[-1]
            # Through the coder's own broker, which its launch runs outside the sandbox.
            assert [d.name for d in (store / store_broker.DIR_NAME).iterdir()], "a broker"

            def sandboxed_calls(commands: list[str], what: str) -> list[str]:
                """Each of ``commands`` (aisquare's arguments) run in the coder's sandbox,
                in one turn: each one's exit status."""
                until(lambda: state(coder) == "waiting", 20, "waiting", pane)
                seen = len(outputs())
                chain = "; ".join(f'{cli} {c} >/dev/null 2>&1; echo "rc=$?"' for c in commands)
                assert fleet.tell(project, label, f"RUN {chain}").delivered
                until(lambda: answered(seen), 60, what, pane)
                return re.findall(r"rc=(\d+)", outputs()[-1])

            # Every board call the role protocols teach, enumerated, from the sandbox.
            mine = row.id[:8]
            task_a, task_b = (team.add_task(f"enumerated-{n}", cwd=workspace)[0].id for n in "ab")
            enumerated = [
                [
                    f"task add enumerated-c --role coder --detail d --as {mine}",
                    "task list",
                    f"task show {task_a}",
                    f"task next --role coder --as {mine}",
                    f"task claim {task_a} --as {mine}",
                ],
                [
                    f"task review {task_a} --note verify --as {mine}",
                    f"task next --status review --as {mine}",
                    f"task reopen {task_a} --reason again --as {mine}",
                    f"task claim {task_a} --as {mine}",
                    f"task done {task_a} --note done --as {mine}",
                ],
                [
                    f"task claim {task_b} --as {mine}",
                    f"task release {task_b} --as {mine}",
                    f"task block {task_b} --reason stuck --as {mine}",
                    f"task drop {task_b} --as {mine}",
                    f"note 'a decision' --kind decision --as {mine}",
                ],
                [
                    f"team focus enumerating --as {mine}",
                    f"team signal fold-ready on --as {mine}",
                    "team signals",
                    f"team log -n 3 --as {mine}",
                    "team verify 1",
                ],
                [
                    "board",
                    "team status",
                    "remember 'a fact'",
                    "context add 'another fact'",
                    "context list",
                ],
                ["context search fact", "context preview", "inject", "log"],
            ]
            for number, commands in enumerate(enumerated):
                codes = sandboxed_calls(commands, f"enumerated board calls {number}")
                assert codes == ["0"] * len(commands), list(zip(commands, codes, strict=False))
            with store_session() as handle:
                done = {task.title: task.status for task in handle.team_tasks(project.id)}
                said = [event.text for event in handle.recent_events(project.id, limit=80)]
            assert done["enumerated-a"] == "done" and done["enumerated-b"] == "dropped"
            assert done["enumerated-c"] == "todo" and "a decision" in said

            # Review r10 of #113, findings 1, 3 and 4: this coder runs in the project's own
            # directory, as a tester does. What a launch outside any sandbox reads there,
            # another agent's worktree and the tmux sockets' directory are read-only to it,
            # absent ones included and none left behind; and its broker acts only on its own
            # board's tasks and in its own project's pool.
            other_worktree = workspace / ".aisquare-worktrees" / "coder-9"
            other_worktree.mkdir(parents=True)
            socket_decoy = srv.socket_path().parent / f"socket_decoy-{os.getpid()}"
            socket_decoy.write_text("x", encoding="utf-8")
            elsewhere = tmp_path / "elsewhere"
            elsewhere.mkdir()
            team.activate(elsewhere)
            elsewhere_task, _ = team.add_task("their task", cwd=elsewhere)
            until(lambda: state(coder) == "waiting", 20, "waiting", pane)
            seen = len(outputs())
            launch_inputs = "; ".join(
                [
                    "mkdir -p .claude; echo x > .claude/settings.json && echo WROTE-settings",
                    "echo x > .mcp.json && echo WROTE-mcp",
                    f"touch {other_worktree / 'x'} && echo WROTE-worktree",
                    f"rm -f {socket_decoy}; test -e {socket_decoy} || echo REMOVED-socket-dir",
                    f"touch {socket_decoy.parent / 'planted'} && echo WROTE-socket-dir",
                    f"{cli} task done {elsewhere_task.id} --as {mine}",
                    f"{cli} remember everyone --user",
                ]
            )
            assert fleet.tell(project, label, f"RUN {launch_inputs}").delivered
            until(lambda: answered(seen), 40, "the r10 probe ran", pane)
            probed = outputs()[-1]
            assert "WROTE" not in probed and "REMOVED" not in probed, probed
            assert "another board" in probed and "user pool" in probed, probed
            assert not (workspace / ".mcp.json").exists(), "nothing left on the host"
            assert not (workspace / ".claude" / "settings.json").exists() and socket_decoy.exists()
            with store_session() as handle:
                their_task = handle.get_task(elsewhere_task.id)
            assert their_task is not None and their_task.status == "todo"
            socket_decoy.unlink()
            # A waiting manager is woken by a result the coder posts from its sandbox,
            # which tmux refuses: by the coder's own hook, which Codex runs outside it.
            manager = fleet.spawn(
                project,
                "manager",
                agent="codex",
                worktree=False,
                prompt="manager fixture",
                agent_args=["--dangerously-bypass-hook-trust", "--no-alt-screen"],
            ).agent

            def waiting(agent: Any) -> bool:
                session = fleet.status_of(agent).session
                return session is not None and session.state == "waiting"

            def ran(text: str) -> bool:
                return any(text in _user_texts(request) for request in list(requests))

            until(lambda: waiting(manager), 40, "the manager waiting", manager.pane_id)
            # A board call as another session (the manager's) is refused by the broker.
            manager_seat = fleet.status_of(manager).session
            assert manager_seat is not None
            until(lambda: state(coder) == "waiting", 20, "waiting", pane)
            seen = len(outputs())
            forged = f"RUN {cli} note 'as the manager' --as {manager_seat.id[:8]}"
            assert fleet.tell(project, label, forged).delivered
            until(lambda: answered(seen), 30, "the forged note ran", pane)
            assert "is not this agent's own session" in outputs()[-1], outputs()[-1]
            with store_session() as handle:
                texts = [event.text for event in handle.recent_events(project.id, limit=80)]
            assert "as the manager" not in texts
            until(lambda: state(coder) == "waiting", 20, "waiting", pane)
            seen, reviewed = len(outputs()), reviews()
            socket = config.fleet.tmux_socket
            tmux_probe = f"RUN tmux -L {socket} list-sessions; {cli} note 'lane-d' --kind result"
            assert fleet.tell(project, label, tmux_probe).delivered
            until(lambda: answered(seen), 30, "the sandboxed result ran", pane)
            assert "Operation not permitted" in outputs()[-1], outputs()[-1]
            assert "shared (result)" in outputs()[-1], outputs()[-1]
            assert reviews() == reviewed, "on the first, sandboxed attempt"
            until(lambda: ran(fleet.NUDGE_TEXT), 10, "the result woke the manager", manager.pane_id)
            # What runs outside the sandbox stays out of its reach.
            until(lambda: state(coder) == "waiting", 20, "waiting", pane)
            seen = len(outputs())
            probe = (
                f"RUN touch {store / 'credentials'}; touch {store / 'codex-accounts' / 'x'};"
                f" touch {store / 'config.toml'}; touch {store / 'fleet-tmux.conf'};"
                f" touch {store / 'agents.json'};"
                f" touch {launch_seal._path(coder.id)}; echo probed"
            )
            assert fleet.tell(project, label, probe).delivered
            until(lambda: answered(seen), 30, "the probe ran", pane)
            # Refused, each: "Read-only file system", or "Permission denied" where the
            # path did not exist and the sandbox left its read-only empty file there.
            assert outputs()[-1].count("cannot touch") == 6, outputs()[-1]
            # The record a replay takes the coder's launch from is still its spawn's.
            recorded = launch_seal.read(coder.id)
            assert isinstance(recorded, launch_seal.LaunchRecord) and recorded.binary == "codex"
            # An escalation Codex's reviewer answers: reviewed, and no bell.
            until(lambda: state(coder) == "waiting", 20, "waiting", pane)
            seen, reviewed = len(outputs()), reviews()
            assert fleet.tell(project, label, f"ESC {cli} board").delivered
            until(lambda: answered(seen), 40, "the reviewed escalation ran", pane)
            assert reviews() > reviewed and "<aisquare-team>" in outputs()[-1]
            until(lambda: state(coder) == "waiting", 20, "waiting", pane)
            with store_session() as handle:
                bells = [
                    event.text
                    for event in handle.recent_events(project.id, limit=80)
                    if event.kind == "attention"
                ]
            assert bells == []

            # Onto workspace:ask: the resumed thread is the human's to approve.
            restarted = fleet.restart(project, label, permission_mode="workspace:ask")
            assert restarted.resumed, restarted.notes
            coder, pane = restarted.started, restarted.started.pane_id
            until(lambda: state(coder) == "waiting", 40, "waiting after the restart", pane)
            seen, reviewed = len(outputs()), reviews()
            asked = f"ESC sleep 8; {cli} note 'asked first'"
            assert fleet.tell(project, label, asked).delivered

            def asking() -> bool:
                return "Would you like to run" in srv.run("capture-pane", "-p", "-t", pane)

            until(asking, 30, "Codex's approval view", pane)
            first = fleet._activity_times(srv).get(pane)
            for _ in range(3):
                time.sleep(1.2)
                assert state(coder) == "attention"
            assert fleet._activity_times(srv).get(pane) != first, "the view redraws"
            assert reviews() == reviewed and not answered(seen)
            srv.send_keys(pane, "y")
            # Working the moment the human allows, not when the command's PostToolUse
            # fires at its end (review r2 of #113, finding 7).
            until(lambda: state(coder) == "working", 5, "working once allowed", pane)
            assert not answered(seen), "the approved command still runs"
            until(lambda: answered(seen), 30, "the approved note ran", pane)
            assert "shared (note)" in outputs()[-1]
            until(lambda: state(coder) in {"working", "waiting"}, 20, "the bell cleared", pane)
            with store_session() as handle:
                bells = [
                    event.text
                    for event in handle.recent_events(project.id, limit=80)
                    if event.kind == "attention"
                ]
            assert bells == ["Codex needs your approval"]

            # A reviewer's read-only seat posts its verdict and cannot edit the worktree.
            seen = len(outputs())
            reviewer = fleet.spawn(
                project,
                "reviewer",
                agent="codex",
                worktree=False,
                prompt=f"RUN {cli} note 'reviewer verdict'; touch {workspace / 'edited'}",
                agent_args=["--dangerously-bypass-hook-trust", "--no-alt-screen"],
            ).agent
            until(lambda: answered(seen), 40, "the reviewer's note ran", reviewer.pane_id)
            assert "shared (note)" in outputs()[-1], outputs()[-1]
            assert "Read-only file system" in outputs()[-1]
            assert not (workspace / "edited").exists()

            # The sandbox writes nothing in the store (review r9 of #113: the broker): from
            # its read-only seat the reviewer's rewrites of its session's thread and home,
            # its row's role, binary and account, and the project's directory are refused.
            # Made outside as it could once make them, they still choose nothing a replay
            # runs (r3, findings 2 and 5; r4, 1, 2 and 6): it takes its launch record.
            def rewrite(table: str, column: str, value: str, row: str) -> str:
                code = (
                    "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute("
                    f"'UPDATE {table} SET {column} = ? WHERE id = ?', (sys.argv[2], sys.argv[3]))"
                    "; c.commit(); print('rewrote', c.total_changes)"
                )
                db = str(store / "context.db")
                return "RUN " + shlex.join([sys.executable, "-c", code, db, value, row])

            def sandboxed(told: str, what: str, first: Any = None) -> None:
                until(lambda: state(reviewer) == "waiting", 30, "waiting", reviewer.pane_id)
                seen = len(outputs())
                assert fleet.tell(project, reviewer.label, told).delivered
                until(lambda: answered(seen), 30, what, reviewer.pane_id)
                _refused_then_made(told, outputs()[-1], first=first)

            seat = fleet.status_of(reviewer).session
            assert seat is not None and seat.native_session_id
            recorded_thread = seat.native_session_id
            flag = "--dangerously-bypass-approvals-and-sandbox"
            sandboxed(rewrite("team_session", "native_session_id", flag, seat.id), "thread")
            sandboxed(rewrite("team_session", "account", str(workspace), seat.id), "home")
            sandboxed(rewrite("fleet_agent", "binary", "/tmp/x", reviewer.id), "binary")
            sandboxed(rewrite("fleet_agent", "account_slot", "7", reviewer.id), "account")
            # Last: a row whose role is rewritten is one nothing types into any more (review
            # r8 of #113, rule C), and it is still replayed as recorded.
            sandboxed(rewrite("fleet_agent", "role", "coder", reviewer.id), "role")
            until(lambda: state(reviewer) == "waiting", 20, "waiting", reviewer.pane_id)
            replaced = fleet.restart(project, reviewer.label)
            command = replaced.started.launch_spec.command if replaced.started.launch_spec else []
            assert replaced.resumed, replaced.notes
            assert flag not in command and "/tmp/x" not in command and "--account" not in command
            at = command.index("resume")
            assert command[at : at + 2] == ["resume", recorded_thread], "the recorded thread"
            assert command[command.index("launch") + 1] == "reviewer", "the recorded role"
            reviewer = replaced.started
            until(lambda: state(reviewer) == "waiting", 40, "the replacement", reviewer.pane_id)
            sandboxed(rewrite("project", "root", str(outside), project.id), "the project's root")
            with store_session() as handle:
                stored = handle.get_project(project.id)
            assert stored is not None and stored.root == outside
            moved = "is not the one it was spawned in"
            with pytest.raises(fleet.FleetError, match=moved):
                fleet.restart(stored, reviewer.label)
            assert fleet.status_of(reviewer).state in {"waiting", "working"}, "left running"
            with store_session() as handle:
                said = [
                    event.text
                    for event in handle.recent_events(project.id, limit=80)
                    if event.kind == "note" and moved in event.text
                ]
            assert said and said[0].startswith(f"{reviewer.label}: cannot restart it"), said

            # Review r5 of #113, finding 2: the transcript a failed turn is read from is
            # the record's. The reviewer points its session at a rollout that ends in a
            # usage limit; with the row working (a write its Stop would undo, made here),
            # the fleet's read leaves it working: no limited, no hand-over.
            until(lambda: state(reviewer) == "waiting", 30, "waiting", reviewer.pane_id)
            seat = fleet.status_of(reviewer).session
            assert seat is not None
            planted = store / "planted-rollout.jsonl"
            failed = {
                "timestamp": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "planted",
                    "error": {"message": "limit", "codex_error_info": "usage_limit_exceeded"},
                },
            }
            planted.write_text(json.dumps(failed) + "\n", encoding="utf-8")
            sandboxed(
                rewrite("team_session", "transcript_path", str(planted), seat.id), "transcript"
            )
            until(lambda: state(reviewer) == "waiting", 20, "waiting", reviewer.pane_id)
            with store_session() as handle:
                handle.touch_session(seat.id, state="working")
            listed = {status.agent.id: status.state for status in fleet.list_agents(project)}
            assert listed[reviewer.id] == "working", listed
            with store_session() as handle:
                kinds = [event.kind for event in handle.recent_events(project.id, limit=80)]
                handle.touch_session(seat.id, state="waiting")
            assert "limited" not in kinds and "turn_failed" not in kinds

            # Finding 1: what is typed into or killed is the pane the record places. The
            # reviewer points its row at a pane of another tmux server, the user's own:
            # `tell` files the note, `stop` is refused, and that pane gets nothing.
            users = f"asq-users-{os.getpid()}"
            subprocess.run(
                ["tmux", "-L", users, "new-session", "-d", "-s", "main", "cat"], check=True
            )
            display = ["tmux", "-L", users, "display-message", "-p", "-t", "main"]
            users_pane = subprocess.run(
                [*display, "#{pane_id}"], capture_output=True, text=True, check=True
            ).stdout.strip()
            users_socket = subprocess.run(
                [*display, "#{socket_path}"], capture_output=True, text=True, check=True
            ).stdout.strip()
            code = (
                "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute('UPDATE "
                "fleet_agent SET socket_path = ?, pane_id = ? WHERE id = ?', sys.argv[2:5])"
                "; c.commit(); print('rewrote', c.total_changes)"
            )
            db = str(store / "context.db")
            pointed = [sys.executable, "-c", code, db, users_socket, users_pane, reviewer.id]
            sandboxed("RUN " + shlex.join(pointed), "the pane")
            # Its turn ends on its own; the row no longer names its pane to watch it by.
            time.sleep(3)
            seen = len(outputs())
            not_its = "its row was rewritten in the store"
            told = fleet.tell(project, reviewer.label, "LEAKED LINE")
            assert not told.delivered and not_its in told.how, told.how
            assert len(outputs()) == seen, "nothing reached the reviewer either"
            # Review r6 of #113, finding 4: refused, that one UPDATE made the agent one no
            # stop could end. It is stopped where its launch record places it.
            stopped = fleet.stop(project, reviewer.label)
            assert stopped.agent.ended_at is not None
            assert srv.pane_facts(reviewer.pane_id) is None, "its own window is gone"
            screen = subprocess.run(
                ["tmux", "-L", users, "capture-pane", "-p", "-t", users_pane],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            assert "LEAKED" not in screen and "/exit" not in screen, screen
            assert (
                subprocess.run(
                    ["tmux", "-L", users, "has-session", "-t", "main"], check=False
                ).returncode
                == 0
            ), "the user's pane runs on"
            with store_session() as handle:
                said = [
                    event.text
                    for event in handle.recent_events(project.id, limit=80)
                    if event.kind == "note" and not_its in event.text
                ]
            assert said, "the reason is on the board"

            # Review r7 of #113: every tmux act goes through the one placement resolver.
            # The project's directory is put back first, as its operator would, so its next
            # agents are spawned and replayed in it.
            with store_session() as handle:
                assert isinstance(handle, SqliteStore)
                handle._conn.execute(
                    "UPDATE project SET root = ? WHERE id = ?", (str(workspace), project.id)
                )
                handle._conn.commit()
            # Finding 3: from its sandbox an agent points its row at a dead window of the
            # fleet's session; `restart` still finds it running where its launch record
            # places it, and hands it over there, the dead window untouched.
            seen = len(outputs())
            checker = fleet.spawn(
                project,
                "reviewer",
                label="checker",
                agent="codex",
                worktree=False,
                prompt=f"RUN {cli} note 'checker up'",
                agent_args=["--dangerously-bypass-hook-trust", "--no-alt-screen"],
            ).agent
            until(lambda: answered(seen), 40, "the checker's note ran", checker.pane_id)
            [checker_place] = [
                placement.session
                for placement in [fleet.resolve_place(checker)]
                if isinstance(placement, fleet.Placed)
            ]
            assert checker_place is not None
            decoy = srv.run(
                "new-window", "-d", "-P", "-F", "#{pane_id}", "-t", f"{checker_place.target}:",
                "-n", "decoy", "--", "true",
            ).strip()  # fmt: skip

            def decoy_dead() -> bool:
                facts = srv.pane_facts(decoy)
                return facts is not None and facts.dead

            until(decoy_dead, 10, "the decoy window dead", decoy)
            reviewer = checker  # `sandboxed` tells the reviewer's label
            sandboxed(rewrite("fleet_agent", "pane_id", decoy, checker.id), "the dead pane")
            until(lambda: state(checker) == "waiting", 30, "waiting", checker.pane_id)
            again = fleet.restart(project, checker.label)
            assert again.was_running, "it runs where its record places it"
            assert srv.pane_facts(checker.pane_id) is None, "handed over: its own window is gone"
            dead = srv.pane_facts(decoy)
            assert dead is not None and dead.dead, "the dead window it named is untouched"
            checker = again.started
            until(lambda: state(checker) == "waiting", 40, "the replacement", checker.pane_id)

            # Finding 1: from its sandbox it INSERTs a live row naming another project's
            # window and swaps the two projects' codenames: refused, no row is made there.
            # Made outside, `fleet stop` of that row, and a scoped `shutdown`, type into and
            # kill nothing of the other project's.
            other_root = tmp_path / "other"
            other_root.mkdir()
            other = fleet.ensure_codename(team.activate(other_root))
            theirs = fleet.session_name(other.codename or "")
            srv.run("new-session", "-d", "-s", theirs, "-n", "theirs", "--", "cat")
            their_pane = srv.run("display-message", "-p", "-t", f"={theirs}:", "#{pane_id}").strip()
            insert = (
                "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); "
                "cols = [r[1] for r in c.execute('PRAGMA table_info(fleet_agent)')]; "
                "row = dict(zip(cols, c.execute('SELECT * FROM fleet_agent WHERE id = ?', "
                "(sys.argv[2],)).fetchone())); "
                "row.update(id=sys.argv[3], label='coder-9', pane_id=sys.argv[4], "
                "session_id=None, ended_at=None); "
                "c.execute('INSERT INTO fleet_agent (%s) VALUES (%s)' % (','.join(row), "
                "','.join('?' * len(row))), list(row.values())); "
                "c.commit(); print('rewrote', c.total_changes)"
            )
            fake_id = f"agt_fake{os.getpid()}"
            db = str(store / "context.db")
            reviewer = checker

            def no_fake_row() -> None:
                with store_session() as handle:
                    assert handle.get_fleet_agent(fake_id) is None, "the sandbox made no row"

            sandboxed(
                "RUN "
                + shlex.join([sys.executable, "-c", insert, db, checker.id, fake_id, their_pane]),
                "the fake row",
                first=no_fake_row,
            )
            ours = fleet.ensure_codename(project).codename or ""
            sandboxed(rewrite("project", "codename", "spare-heron", other.id), "their codename")
            sandboxed(rewrite("project", "codename", other.codename or "", project.id), "ours")
            sandboxed(rewrite("project", "codename", ours, other.id), "the swap")
            with pytest.raises(fleet.FleetError, match="a live window is never killed"):
                fleet.stop(project, "coder-9")
            report = fleet.shutdown(project, force=True)
            assert fake_id in [row.agent.id for row in report.failed], report
            assert srv.has_session(theirs), "the other project's session stands"
            their_screen = srv.run("capture-pane", "-p", "-t", their_pane)
            facts = srv.pane_facts(their_pane)
            assert facts is not None and not facts.dead and "/quit" not in their_screen
        finally:
            if srv.has_session(fleet.session_name(fleet.ensure_codename(project).codename or "")):
                srv.run("kill-server")
            subprocess.run(
                ["tmux", "-L", f"asq-users-{os.getpid()}", "kill-server"],
                capture_output=True,
                check=False,
            )
            server.shutdown()
            thread.join(timeout=2)
            shutil.rmtree(outside, ignore_errors=True)


class _AtItsLimit(BaseHTTPRequestHandler):
    """A loopback Responses provider for an account at its usage limit. A turn whose last
    user line starts ``LIMIT`` is answered as the ChatGPT backend answered a team plan at its
    spend cap: ``429`` ``usage_limit_reached`` with the server's own text, the active limit
    ``premium``. Codex 0.162.1 records it as it recorded SMOKE3's live limit (a ``premium``
    reading with both windows ``null``, then the turn's ``task_complete`` ``error``
    ``usage_limit_exceeded``) and fires no ``Stop`` for it. Every other turn is answered with
    the five-hour window at 100 % in its headers, resetting at ``server.resets_at``."""

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        if not self.path.endswith("/responses"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")
            return
        said = _user_texts(json.loads(raw))
        resets: int = self.server.resets_at  # type: ignore[attr-defined]
        windows = {
            "x-codex-primary-used-percent": "100.0",
            "x-codex-primary-window-minutes": "300",
            "x-codex-primary-reset-at": str(resets),
            "x-codex-secondary-used-percent": "24.0",
            "x-codex-secondary-window-minutes": "10080",
            "x-codex-secondary-reset-at": str(resets + 4 * 24 * 3600),
        }
        if not (said and said[-1].startswith("LIMIT")):
            _answer(self, _message("ok"), windows)
            return
        body = json.dumps(
            {
                "error": {
                    "type": "usage_limit_reached",
                    "message": "You hit your spend cap set in your workspace. "
                    "Increase your spend cap to continue.",
                    "plan_type": "team",
                    "resets_at": resets,
                }
            }
        ).encode()
        self.send_response(429)
        self.send_header("Content-Type", "application/json")
        self.send_header("x-codex-active-limit", "premium")
        self.send_header("x-codex-credits-has-credits", "true")
        self.send_header("x-codex-credits-unlimited", "false")
        self.send_header("x-codex-rate-limit-reached-type", "workspace_owner_usage_limit_reached")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.mark.skipif(os.environ.get("AISQUARE_TEST_CODEX") != "1", reason="opt-in native Codex test")
@pytest.mark.parametrize("dialog", [False, True], ids=["aisquare-hides-it", "operator-shows-it"])
def test_real_codex_at_its_usage_limit_reaches_the_board_with_no_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dialog: bool
) -> None:
    """SMOKE3 (tsk_01m4yj1n37f9): a fleet Codex told a line its account cannot pay for fires
    its prompt's hook and no other, and its row read working for good. The fleet's read of
    the row finds the failed turn at the end of its rollout: ``limited`` with the full
    window's reset, the board told once, and no hand-over with no other account to go to.

    Past the first turn, at 100 %, Codex offers a cheaper model in a dialog (``Switch to
    gpt-6-luna for lower credit usage? 1/2/3``) while its hooks say waiting, where the Enter
    after a typed line picked option 1 (tsk_01m4ysys67). The fleet's launch hides it
    (``-c notice.hide_rate_limit_model_nudge=true``); an operator who keeps it on
    (``[notice] … = false``) gets a board note from ``tell``, the dialog unanswered, and a
    stop that kills the window without an ``/exit``. No command runs in Codex's sandbox
    here, so a runner whose bwrap cannot start one (``_sandbox_refused``) runs it as well."""
    binary = shutil.which("codex")
    assert binary, "AISQUARE_TEST_CODEX requires an installed Codex"
    native_home = tmp_path / "codex"
    native_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with ThreadingHTTPServer(("127.0.0.1", 0), _AtItsLimit) as server:
        resets = int(time.time()) + 3 * 3600
        server.resets_at = resets  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        (native_home / "config.toml").write_text(
            'model = "local-fixture"\nmodel_provider = "local"\n'
            '[model_providers.local]\nname = "Local fixture"\nwire_api = "responses"\n'
            f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
            "requires_openai_auth = false\n"
            f'[projects.{json.dumps(str(workspace))}]\ntrust_level = "trusted"\n'
            + ("[notice]\nhide_rate_limit_model_nudge = false\n" if dialog else ""),
            encoding="utf-8",
        )
        for key in tuple(os.environ):
            if key.startswith(("OPENAI_", "ANTHROPIC_", "CODEX_")):
                monkeypatch.delenv(key)
        monkeypatch.setenv("CODEX_HOME", str(native_home))
        monkeypatch.setattr(
            agents, "_aisquare_command", lambda *args, **kwargs: shlex.join(selfcli.argv_for([]))
        )
        agents.install_hooks("codex", native_home)
        config = load_config()
        config.fleet.tmux_socket = f"asq-codex-limit-{os.getpid()}"
        config.accounts.on_limit = "switch"
        save_config(config)
        # The hand-over is decided by the read below, in this process: its worker is not run.
        from aisquare.services import hooks

        detached: list[list[str]] = []
        monkeypatch.setattr(hooks, "_detach", detached.append)
        project = team.activate(workspace)
        srv = fleet.server(config.fleet)

        def screen(pane: str) -> str:
            return srv.run("capture-pane", "-p", "-t", pane)

        def until(check: Any, seconds: float, what: str, pane: str) -> None:
            deadline = time.monotonic() + seconds
            while not check():
                if time.monotonic() >= deadline:
                    raise AssertionError(f"{what}:\n{screen(pane)}")
                time.sleep(0.2)

        def board(agent: Any) -> Any:
            with store_session() as store:
                row = store.get_fleet_agent(agent.id)
                return store.get_session(row.session_id) if row and row.session_id else None

        def quiet_at_its_prompt(agent: Any) -> bool:
            lines = [line.strip() for line in screen(agent.pane_id).splitlines() if line.strip()]
            return (
                fleet.status_of(agent).state == "waiting"
                and bool(lines)
                and lines[-1].startswith("? for shortcuts")
            )

        try:
            coder = fleet.spawn(
                project,
                "coder",
                agent="codex",
                worktree=False,
                agent_args=["--dangerously-bypass-hook-trust"],
            ).agent
            until(lambda: quiet_at_its_prompt(coder), 30, "a quiet empty prompt", coder.pane_id)
            assert fleet.tell(project, coder.label, "reply ok").delivered
            until(
                lambda: (session := board(coder)) is not None and session.state == "waiting",
                30,
                "the first turn's Stop",
                coder.pane_id,
            )
            offer = "Switch to gpt-6-luna"
            if dialog:
                until(lambda: offer in screen(coder.pane_id), 20, "its dialog", coder.pane_id)
                refused = fleet.tell(project, coder.label, "LIMIT probe")
                assert not refused.delivered and "hooks say waiting" in refused.how, refused
                # Review r6 of #113, finding 1: the row's agent column, rewritten as a
                # sandbox can (NULL reads as Claude Code's, whose check reads no dialog),
                # chooses nothing: the record's agent decides tell's and stop's typing.
                with store_session() as handle:
                    assert isinstance(handle, SqliteStore)
                    handle._conn.execute(
                        "UPDATE fleet_agent SET agent = NULL WHERE id = ?", (coder.id,)
                    )
                    handle._conn.commit()
                rewritten = fleet.tell(project, coder.label, "LIMIT probe")
                assert not rewritten.delivered and "rewritten" in rewritten.how, rewritten
                time.sleep(1)
                assert offer in screen(coder.pane_id), "nothing answered the dialog"
                assert "LIMIT probe" not in screen(coder.pane_id)
                stopped = fleet.stop(project, coder.label)
                assert stopped.killed is not None and "dialog" in stopped.killed, stopped
                assert detached == []
                return
            until(lambda: quiet_at_its_prompt(coder), 30, "quiet after its turn", coder.pane_id)
            assert offer not in screen(coder.pane_id), "the fleet's launch hides the dialog"
            told = fleet.tell(project, coder.label, "LIMIT probe")
            assert told.delivered and "typed" in told.how, told
            until(
                lambda: "Usage limit reached" in screen(coder.pane_id),
                20,
                "the limit on its screen",
                coder.pane_id,
            )
            session = board(coder)
            assert session is not None and session.state == "working", "no Stop came"
            until(
                lambda: fleet.status_of(coder).state == "limited",
                10,
                "the fleet's read parks it",
                coder.pane_id,
            )
            session = board(coder)
            assert session is not None
            assert session.limit_resets_at == datetime.fromtimestamp(resets, tz=UTC)
            assert [status.state for status in fleet.list_agents(project)] == ["limited"]
            with store_session() as store:
                events = store.recent_events(project.id, limit=20)
            assert [event.kind for event in events].count("limited") == 1, events
            # One account, the shell's: since 0.8.1 a limit with nowhere to go starts no
            # hand-over worker (`claude_accounts.somewhere_to_hand_over`), which could only
            # say "not switched". The hand-over to a second Codex slot is
            # tests/test_codex_accounts.py's.
            assert detached == []
        finally:
            if srv.has_session(fleet.session_name(fleet.ensure_codename(project).codename or "")):
                srv.run("kill-server")
            server.shutdown()
            thread.join(timeout=2)


@pytest.mark.skipif(os.environ.get("AISQUARE_TEST_CODEX") != "1", reason="opt-in native Codex test")
def test_real_codex_fleet_from_before_launch_records_and_rows_its_sandbox_rewrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r8 of #113, on the real Codex with only loopback and a real tmux server.

    The upgrade (rule A): three agents run, and their launch records are gone, as the
    aisquare before this one left them (it kept none). This aisquare's first fleet read
    takes the census and places them: they list running, a new spawn joins their session,
    `stop` ends one, `restart` starts one fresh, a dead one is reaped. Then a sandboxed
    agent's rewrites of rows are refused in its sandbox (review r9: the store is read-only
    there) and made outside as it could once make them: two labels swapped (rule C) —
    `fleet stop` of a label stops the agent its record gives that label — and its own
    `ended_at` (rule B) — `restart` still finds it running and hands it over. `shutdown`
    takes it all down.
    """
    binary = shutil.which("codex")
    assert binary, "AISQUARE_TEST_CODEX requires an installed Codex"
    refused = _sandbox_refused(binary, tmp_path)
    if refused is not None:
        pytest.skip(f"Codex's sandbox cannot start a command on this host: {refused}")
    outside = Path(tempfile.mkdtemp(prefix="asq-", dir="/var/tmp"))
    store, native_home = outside / "h", outside / "c"
    native_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cli = shlex.join(selfcli.argv_for([]))
    socket = f"asq-codex-upgrade-{os.getpid()}"
    with ThreadingHTTPServer(("127.0.0.1", 0), _Scripted) as server:
        server.requests = []  # type: ignore[attr-defined]
        requests: list[dict[str, Any]] = server.requests  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        (native_home / "config.toml").write_text(
            'model = "local-fixture"\nmodel_provider = "local"\n'
            '[model_providers.local]\nname = "Local fixture"\nwire_api = "responses"\n'
            f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
            "requires_openai_auth = false\n"
            f'[projects.{json.dumps(str(workspace))}]\ntrust_level = "trusted"\n',
            encoding="utf-8",
        )
        for key in tuple(os.environ):
            if key.startswith(("OPENAI_", "ANTHROPIC_", "CODEX_")):
                monkeypatch.delenv(key)
        monkeypatch.delenv("TMPDIR", raising=False)
        monkeypatch.setenv("AISQUARE_HOME", str(store))
        monkeypatch.setenv("CODEX_HOME", str(native_home))
        monkeypatch.setattr(
            agents, "_aisquare_command", lambda *args, **kwargs: shlex.join(selfcli.argv_for([]))
        )
        agents.install_hooks("codex", native_home)
        config = load_config()
        config.fleet.tmux_socket = socket
        save_config(config)
        project = team.activate(workspace)
        for kept in store_read_only(store_dir()):
            kept.parent.mkdir(parents=True, exist_ok=True)
        (store / "codex-accounts").mkdir(exist_ok=True)
        srv = fleet.server(config.fleet)
        args = ["--dangerously-bypass-hook-trust", "--no-alt-screen"]

        def outputs() -> list[str]:
            found: dict[str, str] = {}
            for request in list(requests):
                for item in request.get("input", []):
                    if item.get("type") == "function_call_output":
                        found.setdefault(str(item.get("call_id")), str(item.get("output")))
            return list(found.values())

        def until(check: Any, seconds: float, what: str) -> None:
            deadline = time.monotonic() + seconds
            while not check():
                if time.monotonic() >= deadline:
                    raise AssertionError(f"{what}: {outputs()[-2:]}")
                time.sleep(0.2)

        def state(agent: Any) -> str:
            return fleet.status_of(agent).state

        def up(role: str, label: str) -> Any:
            seen = len(outputs())
            agent = fleet.spawn(
                project, role, label=label, agent="codex", worktree=False,
                prompt=f"RUN {cli} note '{label} up'", agent_args=args,
            ).agent  # fmt: skip
            until(lambda: len(outputs()) > seen, 40, f"{label}'s note ran")
            until(lambda: state(agent) == "waiting", 30, f"{label} waiting")
            return agent

        def sandboxed(agent: Any, sql: str, *values: str) -> None:
            code = (
                "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); "
                f"c.execute({sql!r}, sys.argv[2:]); c.commit(); print('rewrote', c.total_changes)"
            )
            argv = [sys.executable, "-c", code, str(store / "context.db"), *values]
            until(lambda: state(agent) == "waiting", 30, "waiting to be told")
            seen = len(outputs())
            told = "RUN " + shlex.join(argv)
            assert fleet.tell(project, agent.label, told).delivered
            until(lambda: len(outputs()) > seen, 30, "the sandboxed rewrite ran")
            _refused_then_made(told, outputs()[-1])

        try:
            manager, first, second = (
                up("manager", "manager"),
                up("coder", "coder-a"),
                up("coder", "coder-b"),
            )
            [session] = srv.list_sessions()
            # As the aisquare before this one left them: no launch record, no census.
            shutil.rmtree(launch_seal.seals_dir())
            launch_seal._CENSUS_CACHE.clear()

            listed = {s.agent.id: s.state for s in fleet.list_agents(project)}
            assert listed == {a.id: "waiting" for a in (manager, first, second)}, listed
            assert launch_seal.census_taken()
            third = up("coder", "coder-c")
            assert srv.list_sessions() == [session], "joined their session, by its id"
            stopped = fleet.stop(project, first.label)
            assert stopped.agent.ended_at is not None and srv.pane_facts(first.pane_id) is None
            restarted = fleet.restart(project, second.label)
            assert restarted.was_running and not restarted.resumed, restarted.notes
            assert srv.pane_facts(second.pane_id) is None, "handed over at its own pane"
            second = restarted.started
            until(lambda: state(second) == "waiting", 40, "the fresh replacement")
            manager_pid = srv.pane_pid(manager.pane_id)
            assert manager_pid is not None
            os.kill(manager_pid, 15)
            until(lambda: bool((f := srv.pane_facts(manager.pane_id)) and f.dead), 20, "dead")
            reaped = fleet.reap(project)
            status = fleet.status_of(manager)
            assert [row.id for row in reaped.ended] == [manager.id], (status.state, status.detail)

            # Rule B: from its sandbox the replacement ends its own row; restart still
            # finds it running at its record's pane and hands it over there.
            sandboxed(
                second,
                "UPDATE fleet_agent SET ended_at = ? WHERE id = ?",
                datetime.now(UTC).isoformat(),
                second.id,
            )
            handed = fleet.restart(project, second.label)
            assert handed.was_running and srv.pane_facts(second.pane_id) is None, handed.notes
            fourth = handed.started
            until(lambda: state(fourth) == "waiting", 40, "the second replacement")
            # Rule C: from its sandbox coder-c swaps its row's label with that replacement's,
            # in one command; `fleet stop coder-c` stops the agent its record calls coder-c.
            swap = (
                "UPDATE fleet_agent SET label = 'spare-label' WHERE id = :a;"
                "UPDATE fleet_agent SET label = :b_label WHERE id = :b;"
                "UPDATE fleet_agent SET label = :a_label WHERE id = :x"
            )
            code = (
                "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); a, b, al, bl = "
                "sys.argv[2:6]; [c.execute(q, {'a': a, 'b': b, 'x': a, 'a_label': bl, "
                f"'b_label': al}}) for q in {swap!r}.split(';')]; c.commit(); "
                "print('rewrote', c.total_changes)"
            )
            argv = [
                sys.executable, "-c", code, str(store / "context.db"),
                third.id, fourth.id, third.label, fourth.label,
            ]  # fmt: skip
            until(lambda: state(third) == "waiting", 30, "coder-c waiting")
            seen = len(outputs())
            told = "RUN " + shlex.join(argv)
            assert fleet.tell(project, third.label, told).delivered
            until(lambda: len(outputs()) > seen, 30, "the sandboxed swap ran")
            _refused_then_made(told, outputs()[-1])
            gone = fleet.stop(project, third.label, force=True)
            assert gone.agent.id == third.id and srv.pane_facts(third.pane_id) is None
            assert srv.pane_facts(fourth.pane_id) is not None, "the swapped-in agent runs on"

            report = fleet.shutdown(project, force=True)
            assert report.failed == [] and report.incomplete_projects == [], report
            assert srv.list_sessions() == []
        finally:
            subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True, check=False)
            server.shutdown()
            thread.join(timeout=2)
            shutil.rmtree(outside, ignore_errors=True)


@pytest.mark.skipif(os.environ.get("AISQUARE_TEST_CODEX") != "1", reason="opt-in native Codex test")
def test_real_codex_under_a_hub_in_a_worktree_reaches_its_board_and_leaves_no_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r11 of #113, findings 1, 2 and 3, and r12, findings 1, 9, 15 and R1, on the
    real Codex with only loopback.

    A sandboxed coder in its worktree posts a note through its broker, and the principal
    checkout's absent launch inputs, which the profile names outside every writable root,
    leave nothing on the host after a real session. Under ``AISQUARE_TEAM_HUB`` another
    coder in ITS worktree (roots from its launch record, board the hub's) posts one, and
    it lands on the hub's board (the broker took its board from spawn's project, which is
    not the hub's, and refused every call). With the agent's hooks connected from an
    aisquare in the project's tree, a workspace seat is refused, by the fleet before any
    window and by a hand launch, naming the remedy; a ``:read-only`` seat, whose sandbox
    may write none of the tree, runs and posts its note. A sandboxed launch from inside
    the aisquare it would run outside its sandbox is refused.
    """
    import aisquare

    binary = shutil.which("codex")
    assert binary, "AISQUARE_TEST_CODEX requires an installed Codex"
    refused = _sandbox_refused(binary, tmp_path)
    if refused is not None:
        pytest.skip(f"Codex's sandbox cannot start a command on this host: {refused}")
    outside = Path(tempfile.mkdtemp(prefix="asq-", dir="/var/tmp"))
    store, native_home, hub = outside / "h", outside / "c", outside / "hub"
    native_home.mkdir()
    hub.mkdir()
    workspace = outside / "w"
    workspace.mkdir()
    git = ["git", "-C", str(workspace), "-c", "user.email=a@b", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    label = "coder-hub"
    worktree = workspace / ".aisquare-worktrees" / label
    cli = shlex.join(selfcli.argv_for([]))
    socket = f"asq-codex-hub-{os.getpid()}"
    with ThreadingHTTPServer(("127.0.0.1", 0), _Scripted) as server:
        server.requests = []  # type: ignore[attr-defined]
        requests: list[dict[str, Any]] = server.requests  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        trusted = "".join(
            f'[projects.{json.dumps(str(place))}]\ntrust_level = "trusted"\n'
            for place in (
                workspace,
                worktree,
                workspace / ".aisquare-worktrees" / "coder-hub2",
                workspace / ".aisquare-worktrees" / "tester-ro",
            )
        )
        (native_home / "config.toml").write_text(
            'model = "local-fixture"\nmodel_provider = "local"\n'
            '[model_providers.local]\nname = "Local fixture"\nwire_api = "responses"\n'
            f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
            "requires_openai_auth = false\n" + trusted,
            encoding="utf-8",
        )
        for key in tuple(os.environ):
            if key.startswith(("OPENAI_", "ANTHROPIC_", "CODEX_")):
                monkeypatch.delenv(key)
        monkeypatch.delenv("TMPDIR", raising=False)
        monkeypatch.setenv("AISQUARE_HOME", str(store))
        monkeypatch.setenv("CODEX_HOME", str(native_home))
        monkeypatch.setattr(
            agents, "_aisquare_command", lambda *args, **kwargs: shlex.join(selfcli.argv_for([]))
        )
        agents.install_hooks("codex", native_home)
        config = load_config()
        config.fleet.tmux_socket = socket
        save_config(config)
        project = team.activate(workspace)
        before = {path.name for path in workspace.iterdir()}

        def outputs() -> list[str]:
            found: dict[str, str] = {}
            for request in list(requests):
                for item in request.get("input", []):
                    if item.get("type") == "function_call_output":
                        found.setdefault(str(item.get("call_id")), str(item.get("output")))
            return list(found.values())

        def until(check: Any, seconds: float, what: str) -> None:
            deadline = time.monotonic() + seconds
            while not check():
                if time.monotonic() >= deadline:
                    raise AssertionError(f"{what}: {outputs()[-2:]}")
                time.sleep(0.2)

        def noted(board: str, text: str) -> bool:
            with store_session() as handle:
                return text in [event.text for event in handle.recent_events(board, limit=80)]

        try:
            # In its worktree: the note lands, and the principal checkout, outside every
            # writable root, keeps nothing the profile named and it lacked (finding 3).
            seen = len(outputs())
            coder = fleet.spawn(
                project, "coder", label=label, agent="codex", worktree=True,
                prompt=f"RUN {cli} note 'note from the worktree'",
                agent_args=["--dangerously-bypass-hook-trust", "--no-alt-screen"],
            ).agent  # fmt: skip
            assert Path(coder.cwd).resolve() == worktree.resolve()
            until(lambda: len(outputs()) > seen, 60, "the worktree's note ran")
            assert "shared (note): note from the worktree" in outputs()[-1], outputs()[-1]
            assert noted(project.id, "note from the worktree")
            after = {path.name for path in workspace.iterdir()}
            assert after - before <= {".aisquare-worktrees"}, after - before
            for name in (".mcp.json", ".claude", ".codex", ".agents", ".aisquare"):
                assert not (workspace / name).exists(), name
                assert not (worktree / name).exists(), name
            # Under a hub, as `launch` gives every window one: spawn's project is the
            # repository's, every board call the hub's, and the note lands there (finding 1).
            from aisquare.core.orchestrator import team_project

            monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub))
            hub_board = team.activate(hub).id
            assert hub_board == team_project(workspace).id != project.id
            seen = len(outputs())
            hubbed = fleet.spawn(
                project, "coder", label="coder-hub2", agent="codex", worktree=True,
                prompt=f"RUN {cli} note 'note under the hub'",
                agent_args=["--dangerously-bypass-hook-trust", "--no-alt-screen"],
            ).agent  # fmt: skip
            assert hubbed.cwd is not None
            assert Path(hubbed.cwd).resolve() == (worktree.parent / "coder-hub2").resolve()
            until(lambda: len(outputs()) > seen, 60, "the hub's note ran")
            assert "shared (note): note under the hub" in outputs()[-1], outputs()[-1]
            assert noted(hub_board, "note under the hub")
            monkeypatch.delenv("AISQUARE_TEAM_HUB")
            # The agent's hooks connected from an aisquare in the project's tree, the
            # launcher's own install elsewhere (r12, finding 1): the hooks run that one.
            in_tree = workspace / ".venv" / "bin" / "aisquare"
            in_tree.parent.mkdir(parents=True)
            in_tree.write_text(f'#!/bin/sh\nexec {cli} "$@"\n', encoding="utf-8")
            in_tree.chmod(0o755)
            monkeypatch.setattr(
                agents, "_aisquare_command", lambda *args, **kwargs: shlex.quote(str(in_tree))
            )
            agents.install_hooks("codex", native_home)
            # The suite answers that aisquare lives outside every tree (conftest); this is
            # the real answer.
            monkeypatch.setattr(store_broker, "install_paths", REAL_INSTALL_PATHS)
            with pytest.raises(fleet.FleetError, match="aisquare-cli") as refusal:
                fleet.spawn(project, "tester", label="tester-ws", agent="codex", worktree=False)
            assert str(in_tree.resolve()) in str(refusal.value)
            assert not [a for a in fleet.list_agents(project) if a.agent.label == "tester-ws"]
            profile = get_adapter("codex").store_access("workspace", store_dir())
            by_hand = subprocess.run(
                [*selfcli.argv_for(["launch", "tester", "--agent", "codex", "--", *profile])],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=120,
                stdin=subprocess.DEVNULL,
                check=False,
            )
            assert by_hand.returncode != 0, by_hand.stdout + by_hand.stderr
            assert "uv tool install aisquare-cli" in by_hand.stdout + by_hand.stderr
            # A `:read-only` seat in its own worktree, as a fleet reviewer's, writes none of
            # the tree, its worktree included (R1): it runs, and its hooks and broker with it.
            seen = len(outputs())
            seat = fleet.spawn(
                project, "tester", label="tester-ro", agent="codex", worktree=True,
                sandbox="read-only", approval_policy="never",
                prompt=f"RUN touch ro-probe {workspace}/ro-probe; {cli} note 'from read-only'",
                agent_args=["--dangerously-bypass-hook-trust", "--no-alt-screen"],
            ).agent  # fmt: skip
            assert seat.cwd is not None
            own = Path(seat.cwd)
            assert own.resolve() == (worktree.parent / "tester-ro").resolve()
            until(lambda: len(outputs()) > seen, 60, "the read-only seat's note ran")
            assert "shared (note): from read-only" in outputs()[-1], outputs()[-1]
            assert "cannot touch 'ro-probe'" in outputs()[-1], outputs()[-1]
            assert noted(project.id, "from read-only")
            assert not (own / "ro-probe").exists() and not (workspace / "ro-probe").exists()
            # A sandboxed launch from inside the aisquare it would run unsandboxed (finding 2).
            inside = Path(aisquare.__file__).parent
            launched = subprocess.run(
                [*selfcli.argv_for(["launch", "tester", "--agent", "codex", "--", *profile])],
                cwd=inside,
                capture_output=True,
                text=True,
                timeout=120,
                stdin=subprocess.DEVNULL,
                check=False,
            )
            assert launched.returncode != 0, launched.stdout + launched.stderr
            assert "uv tool install aisquare-cli" in launched.stdout + launched.stderr
        finally:
            subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True, check=False)
            server.shutdown()
            thread.join(timeout=2)
            shutil.rmtree(outside, ignore_errors=True)


@pytest.mark.skipif(os.environ.get("AISQUARE_TEST_CODEX") != "1", reason="opt-in native Codex test")
def test_real_codex_in_a_direnv_project_keeps_its_pythonpath_and_aisquare_imports_none_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r14 of #113, findings 1, 3 and 4, on the real Codex with only loopback: a
    project on ``PYTHONPATH=$PWD/src`` (direnv's ``layout``), set where the fleet's tmux
    server starts, as an operator's shell sets it. The agent's own tool imports the
    project's package from it; its note goes through its broker; and the ``sitecustomize``
    in ``src``, which can leave a mark only from outside the sandbox, is imported by no
    process aisquare runs there: the window's ``aisquare launch``, the agent's hooks, its
    broker and the command the broker runs all ignore ``PYTHONPATH`` (``-E``)."""
    binary = shutil.which("codex")
    assert binary, "AISQUARE_TEST_CODEX requires an installed Codex"
    refused = _sandbox_refused(binary, tmp_path)
    if refused is not None:
        pytest.skip(f"Codex's sandbox cannot start a command on this host: {refused}")
    outside = Path(tempfile.mkdtemp(prefix="asq-", dir="/var/tmp"))
    store, native_home, marks = outside / "h", outside / "c", outside / "marks"
    native_home.mkdir()
    marks.mkdir()
    workspace = outside / "w"
    (workspace / "src" / "probe_pkg").mkdir(parents=True)
    (workspace / "src" / "probe_pkg" / "__init__.py").write_text("OK = 'probe-ok'\n", "utf-8")
    (workspace / "src" / "sitecustomize.py").write_text(
        "import os, sys\n"
        "try:\n"
        f"    with open(os.path.join({str(marks)!r}, str(os.getpid())), 'w') as mark:\n"
        "        mark.write(' '.join(sys.argv))\n"
        "except OSError:\n"
        "    pass\n",
        encoding="utf-8",
    )
    git = ["git", "-C", str(workspace), "-c", "user.email=a@b", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    cli = shlex.join(selfcli.argv_for([]))
    socket = f"asq-codex-direnv-{os.getpid()}"
    with ThreadingHTTPServer(("127.0.0.1", 0), _Scripted) as server:
        server.requests = []  # type: ignore[attr-defined]
        requests: list[dict[str, Any]] = server.requests  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        (native_home / "config.toml").write_text(
            'model = "local-fixture"\nmodel_provider = "local"\n'
            '[model_providers.local]\nname = "Local fixture"\nwire_api = "responses"\n'
            f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
            "requires_openai_auth = false\n"
            f'[projects.{json.dumps(str(workspace))}]\ntrust_level = "trusted"\n',
            encoding="utf-8",
        )
        for key in tuple(os.environ):
            if key.startswith(("OPENAI_", "ANTHROPIC_", "CODEX_")):
                monkeypatch.delenv(key)
        monkeypatch.delenv("TMPDIR", raising=False)
        monkeypatch.setenv("AISQUARE_HOME", str(store))
        monkeypatch.setenv("CODEX_HOME", str(native_home))
        # The hooks as this version connects them for Codex: `<python> -E -P -m aisquare`.
        agents.install_hooks("codex", native_home)
        assert all(agents.hook_runs_isolated(c) for c in agents.hook_commands("codex", native_home))
        config = load_config()
        config.fleet.tmux_socket = socket
        save_config(config)
        project = team.activate(workspace)

        def outputs() -> list[str]:
            found: dict[str, str] = {}
            for request in list(requests):
                for item in request.get("input", []):
                    if item.get("type") == "function_call_output":
                        found.setdefault(str(item.get("call_id")), str(item.get("output")))
            return list(found.values())

        try:
            # Set before the fleet's server starts, which every window inherits.
            monkeypatch.setenv("PYTHONPATH", str(workspace / "src"))
            seen = len(outputs())
            fleet.spawn(
                project, "coder", label="coder-direnv", agent="codex", worktree=False,
                prompt=(
                    "RUN python3 -c 'import probe_pkg; print(probe_pkg.OK)'; "
                    f"{cli} note 'note from a direnv project'"
                ),
                agent_args=["--dangerously-bypass-hook-trust", "--no-alt-screen"],
            )  # fmt: skip
            deadline = time.monotonic() + 60
            while len(outputs()) <= seen:
                assert time.monotonic() < deadline, f"the command ran: {outputs()[-2:]}"
                time.sleep(0.2)
            said = outputs()[-1]
            assert "probe-ok" in said, said
            assert "shared (note): note from a direnv project" in said, said
            with store_session() as handle:
                texts = [event.text for event in handle.recent_events(project.id, limit=50)]
            assert "note from a direnv project" in texts
            imported = {mark.name: mark.read_text(encoding="utf-8") for mark in marks.iterdir()}
            assert imported == {}, f"aisquare imported the tree's sitecustomize: {imported}"
        finally:
            subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True, check=False)
            server.shutdown()
            thread.join(timeout=2)
            shutil.rmtree(outside, ignore_errors=True)


@pytest.mark.skipif(os.environ.get("AISQUARE_TEST_CODEX") != "1", reason="opt-in native Codex test")
def test_real_fleet_refuses_a_codex_agent_given_a_directory_inside_the_aisquare_its_hooks_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SMOKE6, live: ``fleet spawn coder --agent codex -- -c
    'sandbox_workspace_write.writable_roots=["<the install>/lib"]'`` was spawned, and its
    sandboxed command wrote into the venv the hooks and broker run. The install is this
    tree's own, as the hooks this version connects run it (``<python> -E -P -m
    aisquare``): the spawn is refused before any window, naming the directory."""
    binary = shutil.which("codex")
    assert binary, "AISQUARE_TEST_CODEX requires an installed Codex"
    outside = Path(tempfile.mkdtemp(prefix="asq-", dir="/var/tmp"))
    store, native_home, workspace = outside / "h", outside / "c", outside / "w"
    native_home.mkdir()
    workspace.mkdir()
    git = ["git", "-C", str(workspace), "-c", "user.email=a@b", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    socket = f"asq-codex-ad2-{os.getpid()}"
    for key in tuple(os.environ):
        if key.startswith(("OPENAI_", "ANTHROPIC_", "CODEX_")):
            monkeypatch.delenv(key)
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.setenv("AISQUARE_HOME", str(store))
    monkeypatch.setenv("CODEX_HOME", str(native_home))
    monkeypatch.setattr(store_broker, "install_paths", REAL_INSTALL_PATHS)
    agents.install_hooks("codex", native_home)
    config = load_config()
    config.fleet.tmux_socket = socket
    save_config(config)
    project = team.activate(workspace)
    lib = Path(sys.prefix) / "lib"
    assert lib.is_dir(), lib
    roots = f"sandbox_workspace_write.writable_roots=[{json.dumps(str(lib))}]"
    try:
        with pytest.raises(fleet.FleetError) as refusal:
            fleet.spawn(
                project, "coder", label="ad2", agent="codex", worktree=False,
                agent_args=["-c", roots],
            )  # fmt: skip
        assert str(lib.resolve()) in str(refusal.value), str(refusal.value)
        assert not [a for a in fleet.list_agents(project) if a.agent.label == "ad2"]
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True, check=False)
        shutil.rmtree(outside, ignore_errors=True)


class _Asking(BaseHTTPRequestHandler):
    """A loopback Responses provider for Remote's Codex cards, driven by the last user line:
    ``ESC <cmd>`` asks to run ``cmd`` outside the sandbox (Codex's approval view), ``ASK``
    asks one question with ``request_user_input`` (Plan mode only), and a call's result is
    answered with words that end on a question. Each request lands in ``server.requests``."""

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        if not self.path.endswith("/responses"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")
            return
        request = json.loads(raw)
        requests: list[dict[str, Any]] = self.server.requests  # type: ignore[attr-defined]
        requests.append(request)
        items, said = request.get("input", []), _user_texts(request)
        verb, _, command = (said[-1] if said else "").partition(" ")
        if items and items[-1].get("type") == "function_call_output":
            _answer(self, _message(f"done: {str(items[-1].get('output'))[:40]}. Shall I go on?"))
            return
        call = f"call_{len(requests)}"
        arguments: dict[str, Any]
        if verb == "ESC":
            arguments = {
                "cmd": command,
                "sandbox_permissions": "require_escalated",
                "justification": "Remote answers it",
            }
            name = "exec_command"
        elif verb == "ASK":
            arguments = {
                "questions": [
                    {
                        "id": "colour",
                        "header": "Colour",
                        "question": "Which colour should the button be?",
                        "options": [
                            {"label": "Red (Recommended)", "description": "Warm and loud"},
                            {"label": "Blue", "description": "Calm"},
                        ],
                    }
                ]
            }
            name = "request_user_input"
        else:
            _answer(self, _message("local fixture complete"))
            return
        _answer(
            self,
            {
                "id": call,
                "type": "function_call",
                "call_id": call,
                "name": name,
                "arguments": json.dumps(arguments),
            },
        )

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.mark.skipif(os.environ.get("AISQUARE_TEST_CODEX") != "1", reason="opt-in native Codex test")
def test_real_codex_approval_and_question_are_answered_from_the_remote_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCOPE #113 0.8.1, items 1 to 3, live: a Codex coder on ``workspace:ask`` in the fleet,
    read by Remote's own app (its live sources, the store, the fleet's tmux), with no fake
    between. Its approval view is a permission card naming the call, with Codex's keys, and
    the card's ``1`` approves it; the turn that ends on a question is an ``asked`` card; an
    approval answered "No" from the phone (Esc) stops the turn, an ``interrupted`` card; in
    Plan mode a ``request_user_input`` is a question card whose ``2`` picks Blue; and the
    Transcript tab is the conversation under ``* codex``. Before the transcript slot every
    one of these read the rollout as Claude Code's JSONL: no card but "shows a dialog that
    needs you", no answers, an empty page."""
    binary = shutil.which("codex")
    assert binary, "AISQUARE_TEST_CODEX requires an installed Codex"
    refused = _sandbox_refused(binary, tmp_path)
    if refused is not None:
        pytest.skip(f"Codex's sandbox cannot start a command on this host: {refused}")
    outside = Path(tempfile.mkdtemp(prefix="asq-", dir="/var/tmp"))
    store, native_home, workspace = outside / "h", outside / "c", outside / "w"
    native_home.mkdir()
    workspace.mkdir()
    socket = f"asq-codex-remote-{os.getpid()}"
    with ThreadingHTTPServer(("127.0.0.1", 0), _Asking) as server:
        server.requests = []  # type: ignore[attr-defined]
        requests: list[dict[str, Any]] = server.requests  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        (native_home / "config.toml").write_text(
            'model = "local-fixture"\nmodel_provider = "local"\n'
            '[model_providers.local]\nname = "Local fixture"\nwire_api = "responses"\n'
            f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
            "requires_openai_auth = false\n"
            f'[projects.{json.dumps(str(workspace))}]\ntrust_level = "trusted"\n',
            encoding="utf-8",
        )
        for key in tuple(os.environ):
            if key.startswith(("OPENAI_", "ANTHROPIC_", "CODEX_")):
                monkeypatch.delenv(key)
        monkeypatch.delenv("TMPDIR", raising=False)
        monkeypatch.setenv("AISQUARE_HOME", str(store))
        monkeypatch.setenv("CODEX_HOME", str(native_home))
        monkeypatch.setattr(
            agents, "_aisquare_command", lambda *args, **kwargs: shlex.join(selfcli.argv_for([]))
        )
        agents.install_hooks("codex", native_home)
        config = load_config()
        config.fleet.tmux_socket = socket
        save_config(config)
        project = team.activate(workspace)
        for kept in store_read_only(store_dir()):
            kept.parent.mkdir(parents=True, exist_ok=True)
        (store / "codex-accounts").mkdir(exist_ok=True)
        srv = fleet.server(config.fleet)
        runtime = make_runtime()
        runtime.set_allow_write(True)
        client = make_client(build_app(runtime, dist_dir=tmp_path))
        assert unlock(client, runtime).status_code == 200
        api = f"{base(runtime)}/api"
        try:

            def until(check: Any, seconds: float, what: str, pane: str) -> Any:
                deadline = time.monotonic() + seconds
                while not (found := check()):
                    if time.monotonic() >= deadline:
                        screen = srv.run("capture-pane", "-p", "-t", pane)
                        raise AssertionError(f"{what}:\n{screen}\n{feed()}")
                    time.sleep(0.3)
                return found

            def feed() -> list[dict[str, Any]]:
                response = client.get(f"{api}/needs")
                assert response.status_code == 200, response.text
                return [item for item in response.json()["items"] if item["agent"] == label]

            def card(kind: str, *, besides: str = "") -> dict[str, Any] | None:
                found = (item for item in feed() if item["kind"] == kind and item["id"] != besides)
                return next(found, None)

            held: list[str] = []

            def answer(item: dict[str, Any], key: str) -> None:
                """Tap ``key`` on the card the moment it shows, as again and again as it is
                refused for a view not drawn yet (Codex records the call before its hooks
                run and its view is drawn), until it goes in."""
                deadline, kind = time.monotonic() + 20, item["kind"]
                while True:
                    body = {"id": item["id"], "keys": [key]}
                    said = client.post(f"{api}/needs/answer", json=body)
                    if said.status_code == 200:
                        assert said.json()["sent"] is True, said.text
                        return
                    assert said.status_code == 409, said.text
                    assert "shows no prompt now" in said.json()["message"], said.text
                    held.append(kind)
                    assert time.monotonic() < deadline, "the view was never drawn"
                    time.sleep(0.3)
                    item = until(lambda: card(kind), 10, "the card back", pane)

            def state() -> str:
                return fleet.status_of(coder).state

            def results() -> list[str]:
                return [
                    str(item.get("output"))
                    for request in list(requests)
                    for item in request.get("input", [])
                    if item.get("type") == "function_call_output"
                ]

            first = outside / "approved-from-the-phone"
            coder = fleet.spawn(
                project,
                "coder",
                agent="codex",
                worktree=False,
                permission_mode="workspace:ask",
                prompt=f"ESC touch {first}",
                agent_args=["--dangerously-bypass-hook-trust", "--no-alt-screen"],
            ).agent
            pane, label = coder.pane_id, coder.label

            # The approval view: a permission card naming the call, on Codex's own keys.
            asked = until(lambda: card("permission"), 60, "the approval's card", pane)
            assert asked["reason"] == f"{label} waits for a permission answer to use exec_command"
            assert asked["detail"]["tool"] == "exec_command"
            assert asked["detail"]["input"]["cmd"] == f"touch {first}"
            assert asked["answers"] == [
                {"label": "1", "keys": ["1"]},
                {"label": "2", "keys": ["2"]},
                {"label": "No", "keys": ["Escape"]},
            ]
            assert "Would you like to run" in srv.run("capture-pane", "-p", "-t", pane)
            answer(asked, "1")
            until(first.exists, 30, "the command the phone approved ran", pane)
            # Its turn ends on a question: an `asked` card once its Stop says waiting.
            ended = until(lambda: card("asked"), 40, "the turn's question card", pane)
            assert ended["excerpt"].endswith("Shall I go on?")

            # An approval answered "No" from the phone is Codex's Esc: the turn stops.
            second = outside / "refused-from-the-phone"
            assert fleet.tell(project, label, f"ESC touch {second}").delivered
            refusing = until(
                lambda: card("permission", besides=asked["id"]), 40, "the next card", pane
            )
            answer(refusing, "Escape")
            stopped = until(lambda: card("interrupted"), 40, "the interrupted card", pane)
            assert stopped["reason"] == f"{label} was interrupted and waits for you"
            assert not second.exists()

            # Plan mode's question: a question card, its options on their digits.
            until(lambda: state() == "waiting", 20, "waiting", pane)
            srv.run("send-keys", "-t", pane, "BTab")
            until(
                lambda: "Plan mode" in srv.run("capture-pane", "-p", "-t", pane), 10, "Plan", pane
            )
            assert fleet.tell(project, label, "ASK").delivered
            question = until(lambda: card("question"), 40, "the question's card", pane)
            assert question["excerpt"].startswith("Which colour should the button be?")
            assert question["answers"] == [
                {"label": "Red (Recommended)", "keys": ["1"]},
                {"label": "Blue", "keys": ["2"]},
                {"label": "Cancel", "keys": ["Escape"]},
            ]
            answer(question, "2")
            until(lambda: any('"Blue"' in said for said in results()), 30, "Blue answered", pane)
            print(f"taps held for a view not drawn yet: {held}")

            # The Transcript tab: the conversation, under the agent's own speaker.
            response = client.get(
                f"{api}/transcript/{label}", params={"project": project.id, "width": 80}
            )
            assert response.status_code == 200, response.text
            lines = [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in response.json()["lines"]]
            assert "* codex" in lines and "* claude" not in lines
            assert "> you" in lines and f"  ESC touch {first}" in lines
            assert "  ? Colour: Which colour should the button be?" in lines
            assert "  ⎿ interrupted" in lines
        finally:
            subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True, check=False)
            server.shutdown()
            thread.join(timeout=2)
            shutil.rmtree(outside, ignore_errors=True)
