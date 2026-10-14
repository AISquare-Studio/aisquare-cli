"""The adapter protocol, held for every registered adapter and a synthetic third one.

Parametrized over ``agent_adapters.adapters()`` plus :class:`tests.fake_agent.FakeAgentAdapter`,
which is registered for this module only. The fake speaks a native vocabulary unlike Claude
Code's and Codex's, so a shared service that special-cases either of them, or reads a payload
or writes a flag itself instead of asking the adapter, fails here for the fake.

What a new adapter must bring for this suite to pass (docs/coding-agents.md, "Adding a
terminal agent"): a recorded-payload fixture for every hook event it installs
(``tests/fixtures/agent_events/<id>.json``, with its ``--version`` line and what switches
its hooks off) and a row in :data:`EXPECTED_PERMISSIONS`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from importlib import import_module
from pathlib import Path
from typing import Any, get_args
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import agents as agent_core
from aisquare.core import harness
from aisquare.core.agent_adapters import (
    adapter_for_binary,
    adapters,
    get_adapter,
    register,
    unregister,
)
from aisquare.core.agent_adapters.types import (
    EVENT_KINDS,
    AgentAdapter,
    Approvals,
    PermissionIntent,
    WriteScope,
    executable_name,
    hook_args,
    version_in,
)
from aisquare.core.config import FleetRoleSettings, load_config, save_config
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.services import agent_events
from aisquare.services import fleet as fleet_service
from tests import fakebin
from tests.fake_agent import FakeAgentAdapter
from tests.test_fleet_service import FakeTmux

FIXTURES = Path(__file__).parent / "fixtures" / "agent_events"
REGISTERED = tuple(adapter.id for adapter in adapters())
ALL = (*REGISTERED, FakeAgentAdapter.id)

#: Each adapter's flags for a coder's and a reviewer's built-in intent
#: (``PermissionIntent.for_role``): the reviewer's seats are read-only on every agent.
EXPECTED_PERMISSIONS: dict[str, dict[str, list[str]]] = {
    "claude-code": {
        "coder": ["--permission-mode", "auto"],
        "reviewer": ["--permission-mode", "auto", "--disallowedTools=Edit Write NotebookEdit"],
    },
    "codex": {
        "coder": ["--approve-for-me"],
        "reviewer": ["--sandbox", "read-only", "--ask-for-approval", "on-request"],
    },
    "fake-agent": {
        "coder": ["--write", "workspace", "--approvals", "auto"],
        "reviewer": ["--write", "read_only", "--approvals", "auto"],
    },
}


@pytest.fixture(autouse=True)
def fake_agent(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeAgentAdapter]:
    monkeypatch.delenv(FakeAgentAdapter.home_env, raising=False)
    fake = FakeAgentAdapter()
    register(fake)
    try:
        yield fake
    finally:
        unregister(fake.id)


@pytest.fixture(params=ALL)
def adapter(request: pytest.FixtureRequest) -> AgentAdapter:
    return get_adapter(request.param)


def _fixture(adapter: AgentAdapter) -> dict[str, Any]:
    path = FIXTURES / f"{adapter.id}.json"
    assert path.is_file(), f"add {path.relative_to(FIXTURES.parents[1])} for {adapter.id}"
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _home(adapter: AgentAdapter, tmp_path: Path) -> Path:
    home = tmp_path / adapter.home_name
    home.mkdir(parents=True, exist_ok=True)
    return home


def _commands(groups: list[Any]) -> list[str]:
    return [item["command"] for group in groups for item in group.get("hooks", [])]


def _installed_args(adapter: AgentAdapter, home: Path, event: str) -> list[str]:
    """What the installed hook for ``event`` gives ``aisquare hook``, as the shell splits it."""
    settings = json.loads((home / adapter.settings_name).read_text(encoding="utf-8"))
    ours = [
        c for c in _commands(settings["hooks"][event]) if agent_core._is_aisquare_hook_command(c)
    ]
    assert len(ours) == 1, ours
    tokens = agent_core._split_command(ours[0])
    return tokens[tokens.index("hook") + 1 :]


# --- identity and installation ------------------------------------------------------------


def test_every_registered_adapter_brings_its_fixtures() -> None:
    for name in REGISTERED:
        assert (FIXTURES / f"{name}.json").is_file(), f"tests/fixtures/agent_events/{name}.json"
        assert name in EXPECTED_PERMISSIONS, f"add {name} to EXPECTED_PERMISSIONS"


def test_identity_and_config_home(adapter: AgentAdapter, tmp_path: Path) -> None:
    assert adapter.label and adapter.install_hint and adapter.settings_name
    assert executable_name(adapter.binary) == adapter.binary
    assert adapter_for_binary(f"/opt/tools/{adapter.binary}") is adapter
    assert adapter.version_args and all(isinstance(arg, str) for arg in adapter.version_args)
    fixture = _fixture(adapter)
    assert version_in(fixture["version_output"]) == fixture["version"]

    def real(path: Path) -> str:
        return os.path.realpath(path)

    assert real(adapter.config_dir(tmp_path, {})) == real(tmp_path / adapter.home_name)
    elsewhere = tmp_path / "elsewhere"
    assert real(adapter.config_dir(tmp_path, {adapter.home_env: str(elsewhere)})) == real(elsewhere)
    explicit = tmp_path / "explicit"
    chosen = adapter.config_dir(tmp_path, {adapter.home_env: str(elsewhere)}, explicit)
    assert real(chosen) == real(explicit)


# --- hooks -------------------------------------------------------------------------------


def test_hook_install_and_remove_keep_every_foreign_entry(
    adapter: AgentAdapter, tmp_path: Path
) -> None:
    home = _home(adapter, tmp_path)
    settings = home / adapter.settings_name
    first = adapter.capabilities.hooks[0].event
    foreign: dict[str, Any] = {
        "theme": "dark",
        "hooks": {
            first: [{"hooks": [{"type": "command", "command": "~/bin/my-hook stop"}]}],
            "SomeoneElsesEvent": [{"hooks": [{"type": "command", "command": "webhook stop"}]}],
        },
    }
    settings.write_text(json.dumps(foreign), encoding="utf-8")

    assert agent_core.install_hooks(adapter.id, home)
    assert agent_core.install_hooks(adapter.id, home)  # again: replaced, never doubled
    written = json.loads(settings.read_text(encoding="utf-8"))
    assert written["theme"] == "dark"
    assert written["hooks"]["SomeoneElsesEvent"] == foreign["hooks"]["SomeoneElsesEvent"]
    assert "~/bin/my-hook stop" in _commands(written["hooks"][first])
    for spec in adapter.capabilities.hooks:
        args = _installed_args(adapter, home, spec.event)
        if "--definition" in args:  # a hook that needs trust names its definition
            at = args.index("--definition")
            args = args[:at] + args[at + 2 :]
        assert (
            agent_core._split_command(
                " ".join(agent_core._quote(arg) for arg in hook_args(adapter, spec, home))
            )
            == args
        )
    assert agent_core.hooks_installed(adapter.id, home)
    assert agent_core.connected(adapter.id, home)

    assert agent_core.remove_hooks(adapter.id, home)
    assert json.loads(settings.read_text(encoding="utf-8")) == foreign
    assert not agent_core.hooks_installed(adapter.id, home)


def test_the_hooks_off_switch_silences_a_complete_install(
    adapter: AgentAdapter, tmp_path: Path
) -> None:
    home = _home(adapter, tmp_path)
    assert adapter.hooks_switch(home, home / adapter.settings_name) is None
    assert agent_core.install_hooks(adapter.id, home)
    switch = _fixture(adapter)["hooks_off"]
    path = home / switch["file"]
    if path == home / adapter.settings_name:  # the switch shares the hooks file
        merged = {**json.loads(path.read_text(encoding="utf-8")), **json.loads(switch["text"])}
        path.write_text(json.dumps(merged), encoding="utf-8")
    else:
        path.write_text(switch["text"], encoding="utf-8")
    found = agent_core.hooks_switch(adapter.id, home)
    assert found is not None and found.setting
    assert agent_core.hooks_installed(adapter.id, home)
    assert not agent_core.connected(adapter.id, home), "switched off is not connected"


def test_an_installed_hook_command_reaches_its_adapter(
    adapter: AgentAdapter, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command the hooks file holds, run as the agent runs it, lands on the board:
    ``aisquare hook <subcommand>`` decodes with the adapter that installed it."""
    monkeypatch.setenv("AISQUARE_TEAM", "1")
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    home = _home(adapter, tmp_path)
    assert agent_core.install_hooks(adapter.id, home)
    events = _fixture(adapter)["events"]
    repo = tmp_path / "repo"
    repo.mkdir()
    by_kind = {case["expect"]["kind"]: (event, case) for event, case in events.items()}
    for kind in ("session_start", "stop"):
        event, case = by_kind[kind]
        payload = {**case["payload"], "cwd": str(repo)}
        payload.pop("hook_event_name", None)  # the command, or the agent, names it
        if adapter.decode_event({**payload, "hook_event_name": event}) is None:
            pytest.fail(f"{adapter.id} does not decode its own {event} fixture")
        if "hook_event_name" in case["payload"]:
            payload["hook_event_name"] = event
        result = runner.invoke(
            app, ["hook", *_installed_args(adapter, home, event)], input=json.dumps(payload)
        )
        assert result.exit_code == 0, result.output
        assert "failed" not in result.stderr, result.stderr
    native = by_kind["session_start"][1]["expect"]["session_id"]
    with store_session() as store:
        session = store.get_session(agent_events.session_key(adapter, home, native))
    assert session is not None and session.agent == adapter.id
    assert session.state == "waiting"


# --- events ------------------------------------------------------------------------------


def test_every_installed_event_decodes_from_its_recorded_payload(adapter: AgentAdapter) -> None:
    events = _fixture(adapter)["events"]
    installed = {spec.event for spec in adapter.capabilities.hooks}
    assert installed <= set(events), f"record a payload for {sorted(installed - set(events))}"
    for event, case in events.items():
        decoded = adapter.decode_event({**case["payload"], "hook_event_name": event})
        assert decoded is not None, event
        assert decoded.kind in EVENT_KINDS
        for field, expected in case["expect"].items():
            value = getattr(decoded, field)
            assert (str(value) if isinstance(value, Path) else value) == expected, (event, field)
    sample = next(iter(events.values()))["payload"]
    assert adapter.decode_event({**sample, "hook_event_name": "NoSuchEvent"}) is None


def test_the_adapters_together_decode_every_event_kind() -> None:
    kinds = {
        case["expect"]["kind"]
        for name in ALL
        for case in _fixture(get_adapter(name))["events"].values()
    }
    assert kinds == set(EVENT_KINDS)


def test_replies_are_encoded_by_the_adapter(adapter: AgentAdapter) -> None:
    for kind in EVENT_KINDS:
        assert adapter.encode_reply(kind, None) == "" and adapter.encode_reply(kind, "") == ""
    context = "aisquare: 2 teammate updates"
    for kind in ("session_start", "prompt"):
        assert context in adapter.encode_reply(kind, context)
    reason = "keep going: the reviewer left 2 findings"
    stop = adapter.encode_reply("stop", reason)
    assert reason in stop and stop != reason, "a Stop reply is a decision, not context"


# --- launch ------------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(harness.ROLE_PROFILES))
def test_launch_args_for_each_role(adapter: AgentAdapter, role: str) -> None:
    label = f"{role}-1"
    args = adapter.fleet_args(role, label, None)
    assert all(isinstance(arg, str) for arg in args)
    labelled = adapter.label_args(label)
    assert not labelled or args[-len(labelled) :] == labelled
    assert all(
        isinstance(arg, str)
        for arg in adapter.permission_args(PermissionIntent.for_role(role), role)
    )
    assert adapter.model_args(None, None) == []
    assert "chosen-model" in adapter.model_args("chosen-model", None)


def test_permission_intent_maps_every_combination(adapter: AgentAdapter) -> None:
    for write in get_args(WriteScope):
        for approvals in get_args(Approvals):
            intent = PermissionIntent(write, approvals)
            assert PermissionIntent.parse(str(intent)) == intent
            try:
                args = adapter.permission_args(intent, "coder")
            except ValueError as exc:
                assert str(exc), "a refused intent says why"
                continue
            assert args and all(isinstance(arg, str) for arg in args)
    expected = EXPECTED_PERMISSIONS[adapter.id]
    for role, flags in expected.items():
        assert adapter.permission_args(PermissionIntent.for_role(role), role) == flags
    assert expected["reviewer"] != expected["coder"], "the reviewer's seats are read-only"
    with pytest.raises(ValueError):
        PermissionIntent.parse("everything:yes")


def test_resume_exit_mcp_context_teams_and_telemetry(adapter: AgentAdapter, tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    resume = adapter.resume_args("native-1", transcript)
    assert resume and ("native-1" in resume or str(transcript) in resume)
    assert adapter.exit_command.startswith("/") and "\n" not in adapter.exit_command
    mcp = adapter.mcp_args("/opt/aisquare", ["serve", "--stdio"], ["AISQUARE_HOME"])
    assert any("/opt/aisquare" in arg for arg in mcp)
    files = adapter.context_files(tmp_path)
    assert files and all(path.parent == tmp_path for path in files)
    args, env = adapter.disable_native_teams()
    assert args or env, "the fleet keeps the agent's own sub-agent teams off"
    telemetry = adapter.telemetry_args("http://127.0.0.1:9/v1/logs", "Bearer t")
    if adapter.capabilities.native_telemetry:
        assert any("127.0.0.1:9" in arg for arg in telemetry)
    else:
        assert telemetry == []


# --- the shared fleet path, with no branch for any agent ---------------------------------


def test_the_fake_agent_runs_through_the_fleet_like_any_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    config = load_config()
    config.fleet.roles["coder"] = FleetRoleSettings(worktree=False)
    save_config(config)
    bin_dir = tmp_path / "bin"
    fakebin.executable_fake(
        bin_dir, "fakeagent", windows="echo fake agent\nset /p line=", posix="read line"
    )
    fakebin.prepend_to_path(bin_dir, monkeypatch)
    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server", lambda config=None: tmux)
    project = team_project(tmp_path)

    receipt = fleet_service.spawn(project, "coder", agent="fake-agent", prompt="start here")
    assert receipt.agent.agent == "fake-agent"
    spawned = tmux.spawned[-1]
    command = spawned["command"]
    assert isinstance(command, list)
    native = command[command.index("--") + 1 :]
    label = receipt.agent.label
    assert native[:6] == ["--write", "workspace", "--approvals", "auto", "--title", label]
    env = spawned["env"]
    assert isinstance(env, dict) and env["FAKE_AGENT_TEAMS"] == "off"
    assert "--no-subagents" in native
    assert "start here" not in command, "no positional prompt: the fleet types it"

    # The window's own `aisquare launch` execs the fake with those arguments.
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    result = runner.invoke(app, command[command.index("launch") :])
    assert result.exit_code == 0, result.output
    argv = execute.call_args.args[1]
    assert Path(argv[0]).name.startswith("fakeagent")
    assert argv[-len(native) :] == native

    # The adapter's own exit command, typed by `fleet stop`.
    assert fleet_service._exit_command(receipt.agent) == "/quit"
    tmux.honours_exit = False
    fleet_service.stop(project, label, grace=0)
    assert (receipt.agent.pane_id, "literal", "/quit") in tmux.typed
