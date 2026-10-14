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
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import agents, outbox, selfcli
from aisquare.core.config import load_config, save_config
from aisquare.core.entries import new_entry
from aisquare.core.store import store_session
from aisquare.services import agent_launch, fleet, native_telemetry, team


def _user_texts(request: dict[str, Any]) -> list[str]:
    """The user messages a provider request carries, oldest first."""
    return [
        part["text"]
        for item in request.get("input", [])
        if item.get("role") == "user"
        for part in item.get("content", [])
        if part.get("type") == "input_text"
    ]


def _answer(handler: BaseHTTPRequestHandler, item: dict[str, Any]) -> None:
    """Answer a Responses request with ``item`` as its one output, streamed as Codex reads it."""
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
        codex = agent_launch.resolve(agent="codex").adapter
        telemetry_args, note = native_telemetry.start_receiver(env, codex)
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
                # interactive session: the message runs as its next turn.
                pane = receipt.agent.pane_id
                until(lambda: state(receipt.agent) == "waiting", 20, "waiting", pane)
                assert fleet.tell(project, label, "SLOW busy fixture").delivered
                until(lambda: state(receipt.agent) == "working", 10, "working", pane)
                queued = fleet.tell(project, label, "queued fixture line")
                assert queued.delivered and "queued for its next turn" in queued.how, queued
                until(lambda: ran("queued fixture line"), 40, "the queued line ran", pane)
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
                        (_user_texts(request) or [""])[-1] == line
                        and "queued fixture line" in _user_texts(request)
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
        monkeypatch.setattr(agents, "_aisquare_command", lambda: shlex.join(selfcli.argv_for([])))
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
