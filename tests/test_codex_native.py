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
from aisquare.core.agent_adapters.types import store_dir, store_read_only
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
                        {"cmd": f'printf "local-tool-output[%s]" "${native_telemetry.HEADERS_ENV}"'}
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
                assert "local-tool-output[]" in json.dumps(requests[1])
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
    sandboxed attempt, cannot write the account homes or the credentials beside the
    store, and an escalation its automatic reviewer allows rings no bell. Restarted onto
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
        monkeypatch.setattr(agents, "_aisquare_command", lambda: shlex.join(selfcli.argv_for([])))
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
                f" touch {launch_seal._seal_path(coder.id)}; echo probed"
            )
            assert fleet.tell(project, label, probe).delivered
            until(lambda: answered(seen), 30, "the probe ran", pane)
            # Refused, each: "Read-only file system", or "Permission denied" where the
            # path did not exist and the sandbox left its read-only empty file there.
            assert outputs()[-1].count("cannot touch") == 6, outputs()[-1]
            # The seal a replay checks the coder's row against is still its own.
            assert launch_seal.holds(fleet.status_of(coder).agent)
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

            # Nothing the sandbox writes in the store chooses what runs outside it (review
            # r3 of #113, findings 2 and 5): from its read-only seat the reviewer rewrites
            # its session's thread id, then its replacement's row, and neither is replayed.
            def rewrite(table: str, column: str, value: str, row: str) -> str:
                code = (
                    "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute("
                    f"'UPDATE {table} SET {column} = ? WHERE id = ?', (sys.argv[2], sys.argv[3]))"
                    "; c.commit(); print('rewrote', c.total_changes)"
                )
                db = str(store / "context.db")
                return "RUN " + shlex.join([sys.executable, "-c", code, db, value, row])

            until(
                lambda: state(reviewer) == "waiting", 30, "the reviewer waiting", reviewer.pane_id
            )
            seat = fleet.status_of(reviewer).session
            assert seat is not None and seat.native_session_id
            flag = "--dangerously-bypass-approvals-and-sandbox"
            seen = len(outputs())
            told = rewrite("team_session", "native_session_id", flag, seat.id)
            assert fleet.tell(project, reviewer.label, told).delivered
            until(lambda: answered(seen), 30, "the thread id rewritten", reviewer.pane_id)
            assert "rewrote 1" in outputs()[-1], outputs()[-1]
            until(lambda: state(reviewer) == "waiting", 20, "waiting", reviewer.pane_id)
            replaced = fleet.restart(project, reviewer.label)
            assert not replaced.resumed
            assert any("is not one Codex makes" in note for note in replaced.notes)
            assert replaced.started.launch_spec is not None
            assert flag not in replaced.started.launch_spec.command
            reviewer = replaced.started
            until(lambda: state(reviewer) == "waiting", 40, "the replacement", reviewer.pane_id)
            seen = len(outputs())
            told = rewrite("fleet_agent", "role", "coder", reviewer.id)
            assert fleet.tell(project, reviewer.label, told).delivered
            until(lambda: answered(seen), 30, "the role rewritten", reviewer.pane_id)
            assert "rewrote 1" in outputs()[-1], outputs()[-1]
            sealed = "is not the launch `fleet spawn` sealed"
            with pytest.raises(fleet.FleetError, match=sealed):
                fleet.restart(project, reviewer.label)
            assert fleet.status_of(reviewer).state in {"waiting", "working"}, "left running"
        finally:
            if srv.has_session(fleet.session_name(fleet.ensure_codename(project).codename or "")):
                srv.run("kill-server")
            server.shutdown()
            thread.join(timeout=2)
            shutil.rmtree(outside, ignore_errors=True)
