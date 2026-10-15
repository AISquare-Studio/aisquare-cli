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
from aisquare.core.store import SqliteStore, store_session
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

            # Nothing the sandbox writes in the store chooses what runs outside it (review
            # r3 of #113, findings 2 and 5; r4, findings 1, 2 and 6): from its read-only seat
            # the reviewer rewrites its session's thread and home, its row's role, binary
            # and account, and the project's directory. A replay takes its launch record.
            def rewrite(table: str, column: str, value: str, row: str) -> str:
                code = (
                    "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute("
                    f"'UPDATE {table} SET {column} = ? WHERE id = ?', (sys.argv[2], sys.argv[3]))"
                    "; c.commit(); print('rewrote', c.total_changes)"
                )
                db = str(store / "context.db")
                return "RUN " + shlex.join([sys.executable, "-c", code, db, value, row])

            def sandboxed(told: str, what: str) -> None:
                until(lambda: state(reviewer) == "waiting", 30, "waiting", reviewer.pane_id)
                seen = len(outputs())
                assert fleet.tell(project, reviewer.label, told).delivered
                until(lambda: answered(seen), 30, what, reviewer.pane_id)
                assert "rewrote 1" in outputs()[-1], outputs()[-1]

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
            # window and swaps the two projects' codenames. `fleet stop` of that row, and a
            # scoped `shutdown`, type into and kill nothing of the other project's.
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
            sandboxed(
                "RUN "
                + shlex.join([sys.executable, "-c", insert, db, checker.id, fake_id, their_pane]),
                "the fake row",
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
    window's reset, the board told once, the hand-over ``on_limit = "switch"`` asks for.

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
        monkeypatch.setattr(agents, "_aisquare_command", lambda: shlex.join(selfcli.argv_for([])))
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
            [argv] = detached
            assert argv[-4:-2] == ["hand-over", session.id] and argv[-1] == "session limit"
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
    agent rewrites rows from its sandbox: two labels swapped (rule C) — `fleet stop` of a
    label stops the agent its record gives that label — and its own `ended_at` (rule B) —
    `restart` still finds it running and hands it over. `shutdown` takes it all down.
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
        monkeypatch.setattr(agents, "_aisquare_command", lambda: shlex.join(selfcli.argv_for([])))
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
            assert fleet.tell(project, agent.label, "RUN " + shlex.join(argv)).delivered
            until(lambda: len(outputs()) > seen, 30, "the sandboxed rewrite ran")
            assert "rewrote" in outputs()[-1], outputs()[-1]

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
            assert fleet.tell(project, third.label, "RUN " + shlex.join(argv)).delivered
            until(lambda: len(outputs()) > seen, 30, "the sandboxed swap ran")
            assert "rewrote" in outputs()[-1], outputs()[-1]
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
