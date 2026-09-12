"""Opt-in compatibility tests against an installed Codex, using only loopback.

Run AISQUARE_TEST_CODEX=1 pytest tests/test_codex_native.py. No account, login,
provider credential, production hook trust or model spend is used.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import agents, outbox, selfcli
from aisquare.core.config import load_config, save_config
from aisquare.core.entries import new_entry
from aisquare.core.store import store_session
from aisquare.services import agent_launch, fleet, native_telemetry, team


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
            item = {
                "id": "msg_local",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": "local fixture complete", "annotations": []}
                ],
            }
            if len(requests) == 1:
                item = {
                    "id": "call_local",
                    "type": "function_call",
                    "call_id": "call_local",
                    "name": "exec_command",
                    "namespace": "functions",
                    "arguments": json.dumps({"cmd": "printf local-tool-output"}),
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
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

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
        monkeypatch.setattr(agents, "_aisquare_command", lambda: shlex.join(selfcli.argv_for([])))
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
        telemetry_args, note = native_telemetry.start(env)
        assert telemetry_args, note
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
            assert "local-tool-output" in json.dumps(requests[1])
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
                fleet.stop(project, receipt.agent.label, grace=2)
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
        finally:
            server.shutdown()
            thread.join(timeout=2)
