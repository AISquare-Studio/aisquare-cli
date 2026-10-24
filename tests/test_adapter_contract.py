"""The adapter protocol, held for every registered adapter and two synthetic ones.

Parametrized over ``agent_adapters.adapters()`` plus :class:`tests.fake_agent.FakeAgentAdapter`
and :class:`tests.gemini_shaped_agent.GeminiShapedAdapter`, registered for this module only.
The fake speaks a native vocabulary unlike Claude Code's and Codex's, so a shared service that
special-cases either of them, or reads a payload or writes a flag itself instead of asking the
adapter, fails here for the fake; the Gemini-shaped one has the shapes neither has (a home
variable naming the folder its config dir is in, hook timeouts in milliseconds).

What a new adapter must bring for this suite to pass (docs/coding-agents.md, "Adding a
terminal agent"): a recorded-payload fixture for every hook event it installs
(``tests/fixtures/agent_events/<id>.json``, with its ``--version`` line and what switches
its hooks off, and the files of a home that keeps aisquare's MCP server: ``lasting``), a row in
:data:`EXPECTED_PERMISSIONS`, and its transcript at four moments
(``tests/fixtures/agent_transcripts/<id>.json``: a permission prompt up, a question, an
interruption, a turn ended on a question) for its ``transcript_reader``; and with
``AgentScreen.dialog_on_screen`` or ``screen_prompt``, its screens
(``tests/fixtures/agent_screens/<id>.json``: each dialog of its own with the words it reads
and, where it says, the prompt, and screens that show none).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import replace
from importlib import import_module
from pathlib import Path, PureWindowsPath
from typing import Any, cast, get_args
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import agents as agent_core
from aisquare.core import claude_accounts as accounts_core
from aisquare.core import claude_sharing, harness
from aisquare.core.agent_adapters import (
    adapter_for_binary,
    adapters,
    get_adapter,
    register,
    unregister,
)
from aisquare.core.agent_adapters import codex as codex_adapter
from aisquare.core.agent_adapters.types import (
    EVENT_KINDS,
    AgentAccounts,
    AgentAdapter,
    Approvals,
    PermissionIntent,
    PluginInstall,
    SkillDirs,
    WriteScope,
    executable_name,
    hook_args,
    screen_of,
    store_dir,
    store_read_only,
    trusted_by,
    version_in,
)
from aisquare.core.config import FleetRoleSettings, load_config, save_config
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import ClaudeAccount
from aisquare.services import agent_events, first_run, store_broker
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import fleet as fleet_service
from tests import fakebin
from tests.fake_agent import FakeAgentAdapter
from tests.gemini_shaped_agent import GeminiShapedAdapter
from tests.test_fleet_service import FakeTmux

FIXTURES = Path(__file__).parent / "fixtures" / "agent_events"
TRANSCRIPTS = Path(__file__).parent / "fixtures" / "agent_transcripts"
SCREENS = Path(__file__).parent / "fixtures" / "agent_screens"
REGISTERED = tuple(adapter.id for adapter in adapters())
ALL = (*REGISTERED, FakeAgentAdapter.id, GeminiShapedAdapter.id)

#: Stands, in :data:`EXPECTED_PERMISSIONS`, for the adapter's own ``store_access`` for the
#: intent's write scope, the aisquare home it is given being the test's.
STORE = "<store_access>"
#: Each adapter's flags for a coder's and a reviewer's built-in intent
#: (``PermissionIntent.for_role``): the reviewer's seats are read-only on every agent.
EXPECTED_PERMISSIONS: dict[str, dict[str, list[str]]] = {
    "claude-code": {
        "coder": ["--permission-mode", "auto"],
        "reviewer": ["--permission-mode", "auto", "--disallowedTools=Edit Write NotebookEdit"],
    },
    "codex": {
        "coder": ["--approve-for-me", STORE],
        "reviewer": [
            "--ask-for-approval",
            "on-request",
            "-c",
            'approvals_reviewer="auto_review"',
            STORE,
        ],
    },
    "fake-agent": {
        "coder": ["--write", "workspace", "--approvals", "auto", STORE],
        "reviewer": ["--write", "read_only", "--approvals", "auto", STORE],
    },
    "gemini-shaped": {  # no reviewer of its own: workspace:auto runs as workspace:ask
        "coder": ["--approval-mode", "default"],
        "reviewer": ["--approval-mode", "plan"],
    },
}


#: The words of each adapter's headless call (``headless_call``) that keep it from saving a
#: session, and from running hooks where a launch argument can say so: Codex's trusted hooks
#: fire under ``exec`` unless ``features.hooks=false`` (measured on 0.162.1). Claude Code's
#: ``-p`` is main's persona engine's call, isolated by no tools, settings or MCP servers.
EXPECTED_HEADLESS: dict[str, list[str]] = {
    "claude-code": ["--no-session-persistence"],
    "codex": ["--ephemeral", "features.hooks=false"],
    "fake-agent": ["--no-save", "--no-hooks"],
}
#: The switches that leave each adapter's tools-only session (``tools_only_args``, the
#: captain's) no built-in tool to run, as measured: Claude Code's ``--tools ""`` (main's
#: captain), Codex's tool features off (0.162.1: the request offered the server's tools,
#: ``request_user_input`` and its MCP-resource readers, nothing that runs a command).
EXPECTED_TOOLS_OFF: dict[str, list[list[str]]] = {
    "claude-code": [["--tools", ""], ["--strict-mcp-config"]],
    "codex": [
        *(["--disable", feature] for feature in codex_adapter.TOOL_FEATURES),
        ["-c", 'web_search="disabled"'],
    ],
    "fake-agent": [],
}
#: A persona draft, as each adapter's headless call answered it (``headless_answer``): what it
#: printed, and what it wrote to its ``out`` file (``None``: nothing). Codex's is 0.162.1's,
#: recorded through ``headless_call``'s own argv against a loopback provider; Claude Code's
#: is its ``--output-format json`` envelope, in the shape main's persona engine reads.
DRAFT = {
    "name": "skeptic",
    "description": "Questions every claim.",
    "body": "Ask for the evidence.",
    "notes": [],
}
RECORDED_HEADLESS: dict[str, dict[str, str | None]] = {
    "claude-code": {
        "stdout": json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "num_turns": 1,
                "result": "",
                "session_id": "1f0c5a52-2b1e-4e7a-9d0e-5c1f2a3b4c5d",
                "structured_output": DRAFT,
            }
        ),
        "out": None,
    },
    "codex": {"stdout": json.dumps(DRAFT) + "\n", "out": json.dumps(DRAFT)},
    "fake-agent": {"stdout": json.dumps({"reply": DRAFT}), "out": None},
}


def _expected(adapter: AgentAdapter, intent: PermissionIntent, flags: list[str]) -> list[str]:
    store = adapter.store_access(intent.write, store_dir())
    return [arg for flag in flags for arg in (store if flag == STORE else [flag])]


@pytest.fixture(autouse=True)
def fake_agent(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeAgentAdapter]:
    monkeypatch.delenv(FakeAgentAdapter.home_env, raising=False)
    fake = FakeAgentAdapter()
    register(fake)
    try:
        yield fake
    finally:
        unregister(fake.id)


@pytest.fixture(autouse=True)
def gemini_shaped(monkeypatch: pytest.MonkeyPatch) -> Iterator[GeminiShapedAdapter]:
    monkeypatch.delenv(GeminiShapedAdapter.home_env, raising=False)
    shaped = GeminiShapedAdapter()
    register(shaped)
    try:
        yield shaped
    finally:
        unregister(shaped.id)


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
    # The home the variable names is the config dir where its accounts say so (S1).
    named = adapter.accounts.config_dir(elsewhere) if adapter.accounts else elsewhere
    assert real(adapter.config_dir(tmp_path, {adapter.home_env: str(elsewhere)})) == real(named)
    explicit = tmp_path / "explicit"
    chosen = adapter.config_dir(tmp_path, {adapter.home_env: str(elsewhere)}, explicit)
    assert real(chosen) == real(explicit)


def test_install_routes_offer_one_on_every_platform(adapter: AgentAdapter) -> None:
    """``install_routes``: at least one, each a command a terminal runs, and Welcome's step 2
    offers one on every platform (on Windows the best, inside WSL2)."""
    routes = adapter.install_routes
    assert routes, f"{adapter.id} names no way to install it"
    assert all(route.how and route.command.strip() for route in routes)
    for platform in ("linux", "darwin", "win32"):
        offered = first_run.install_routes(platform, agent=adapter.id)
        assert offered and all(route.command for route in offered), platform
    assert first_run.install_routes("win32", agent=adapter.id)[0].command == routes[0].command


def test_lasting_installs_name_a_removal_the_agents_own_binary_runs(
    adapter: AgentAdapter, tmp_path: Path
) -> None:
    """``lasting_installs``: what a home keeps of aisquare's in the agent's own files (the
    fixture's ``lasting``: an MCP server run through uvx, beside one that is not aisquare's),
    each with a removal its own binary runs and whether it outlives the package; nothing in a
    home that keeps none."""
    home = _home(adapter, tmp_path)
    assert adapter.lasting_installs(home) == ()
    lasting = _fixture(adapter)["lasting"]
    for name, text in lasting["files"].items():
        (home / name).parent.mkdir(parents=True, exist_ok=True)
        (home / name).write_text(text, encoding="utf-8")

    found = adapter.lasting_installs(home)

    assert [[entry.kind, entry.name, entry.fetches] for entry in found] == lasting["expect"]
    for entry in found:
        assert entry.agent == adapter.id and entry.home == home
        assert entry.file.is_file() and agent_core.dir_identity(home) in entry.file.parents
        words = entry.remove.split()
        assert adapter.binary in words, f"{adapter.id} removes {entry.name} with {entry.remove}"
        assert entry.name in entry.remove


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
    # Each handler is the adapter's own (`hook_entry`): its shape, its timeout's unit.
    for spec in adapter.capabilities.hooks:
        [handler] = [
            item
            for group in written["hooks"][spec.event]
            for item in group.get("hooks", [])
            if agent_core._is_aisquare_hook_command(item["command"])
        ]
        assert handler == adapter.hook_entry(spec, handler["command"]), spec.event

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


@pytest.mark.parametrize("trusted", [True, False])
@pytest.mark.parametrize("per_event", [True, False])
def test_a_plugin_only_home_is_configured_once_its_hooks_are_trusted(
    adapter: AgentAdapter,
    trusted: bool,
    per_event: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A home connected through the plugin route alone: ``unverified`` until the agent
    trusts the plugin's hooks, whichever agent it is; beside a partial install of ours,
    covered only by a launcher that stands down per event."""
    home = _home(adapter, tmp_path)
    install = PluginInstall(home, "0.9.0", trusted=trusted)
    route = Mock(
        stands_down_per_event=per_event,
        installed=Mock(return_value=install),
        repo_install_here=Mock(return_value=None),
    )
    monkeypatch.setattr(agent_core, "plugin_of", lambda name: route)
    assert agent_core.connected(adapter.id, home)
    state, detail = agent_core.integration_readiness(adapter.id, home)
    if trusted:
        assert state == "configured"
    else:
        assert state == "unverified"
        assert "aisquare plugin 0.9.0" in detail and f"/hooks in {adapter.label}" in detail
    agent_core.install_hooks(adapter.id, home)
    settings = json.loads((home / adapter.settings_name).read_text(encoding="utf-8"))
    del settings["hooks"][adapter.capabilities.hooks[0].event]  # partial: ours, not the plugin's
    (home / adapter.settings_name).write_text(json.dumps(settings), encoding="utf-8")
    assert agent_core.connected(adapter.id, home) is per_event


def test_an_installed_hook_command_reaches_its_adapter(
    adapter: AgentAdapter,
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    project_capture: None,
) -> None:
    """The command the hooks file holds, run as the agent runs it, lands on the board:
    ``aisquare hook <subcommand>`` decodes with the adapter that installed it, in a
    directory its hooks capture (``project_capture``: off is 0.8.1's default)."""
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
            # A path as this platform spells it: "/repo" is `\repo` on Windows.
            assert value == (Path(expected) if isinstance(value, Path) else expected), (
                event,
                field,
            )
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
    labelled = list(adapter.label_args(label).args)
    assert not labelled or args[-len(labelled) :] == labelled
    assert all(
        isinstance(arg, str)
        for arg in adapter.permission_args(PermissionIntent.for_role(role), role).args
    )
    assert adapter.model_args(None, None) == []
    assert "chosen-model" in adapter.model_args("chosen-model", None)


def test_permission_intent_maps_every_combination(adapter: AgentAdapter) -> None:
    for write in get_args(WriteScope):
        for approvals in get_args(Approvals):
            intent = PermissionIntent(write, approvals)
            assert PermissionIntent.parse(str(intent)) == intent
            try:
                args = list(adapter.permission_args(intent, "coder").args)
            except ValueError as exc:
                assert str(exc), "a refused intent says why"
                continue
            assert args and all(isinstance(arg, str) for arg in args)
    expected = EXPECTED_PERMISSIONS[adapter.id]
    for role, flags in expected.items():
        intent = PermissionIntent.for_role(role)
        assert list(adapter.permission_args(intent, role).args) == _expected(adapter, intent, flags)
    assert expected["reviewer"] != expected["coder"], "the reviewer's seats are read-only"
    with pytest.raises(ValueError):
        PermissionIntent.parse("everything:yes")


def _runs_within(inner: list[str], outer: list[str]) -> bool:
    return any(outer[at : at + len(inner)] == inner for at in range(len(outer) - len(inner) + 1))


def _named(args: list[str]) -> set[str]:
    """Every path ``args`` names, as the agent reads it: each argument as written, and every
    key and string of a ``KEY=VALUE`` read as TOML. Codex's ``-c`` takes TOML, where a
    Windows home ``C:\\Users\\me`` is ``"C:\\\\Users\\\\me"`` on the command line: looked for
    as written, the store a Windows runner's profile names was not found (CI on e8f95719).
    """

    def strings(value: object) -> set[str]:
        if isinstance(value, str):
            return {value}
        if isinstance(value, dict):
            return {*value, *(found for item in value.values() for found in strings(item))}
        if isinstance(value, list):
            return {found for item in value for found in strings(item)}
        return set()

    named = set(args)
    for arg in args:
        if arg.startswith(("{", "[")):  # a JSON argument (Claude Code's --mcp-config)
            with suppress(ValueError, RecursionError):
                named |= strings(json.loads(arg))
        _, assigned, raw = arg.partition("=")
        if not assigned:
            continue
        try:
            named |= strings(tomllib.loads(f"v = {raw}")["v"])
        except (tomllib.TOMLDecodeError, RecursionError):
            continue
    return named


def test_a_sandboxed_agent_has_the_store_read_only(adapter: AgentAdapter, tmp_path: Path) -> None:
    """An agent whose sandbox confines its tools' writes declares the launch arguments that
    hold the aisquare store read-only in it, all of it (``store_access``,
    ``store_read_only``), and every permission the fleet maps for it carries them, the
    role's built-in ones included."""
    # A Windows home too, on every platform: its spelling in the launch is the agent's to
    # choose (TOML escapes Codex's backslashes), and only a Windows runner had one.
    windows = cast(Path, PureWindowsPath(r"C:\Users\me\.aisquare"))
    for store in (tmp_path / "aisquare-home", windows):
        for write in get_args(WriteScope):
            access = adapter.store_access(write, store)
            assert all(isinstance(arg, str) for arg in access)
            if not adapter.capabilities.sandboxes_tools:
                assert access == [], "no sandbox stands between the agent and the store"
            elif write != "full":
                named = _named(access)
                assert str(store) in named, f"{adapter.id}'s {write} sandbox leaves out {store}"
                assert store_read_only(store) == (store,), "all of the store"
    for write in get_args(WriteScope):
        for approvals in get_args(Approvals):
            intent = PermissionIntent(write, approvals)
            try:
                args = list(adapter.permission_args(intent, "coder").args)
            except ValueError:
                continue
            assert _runs_within(adapter.store_access(write, store_dir()), args), intent
    for role in harness.ROLE_PROFILES:
        write = PermissionIntent.for_role(role).write
        launched = adapter.fleet_args(role, f"{role}-1", None)
        assert _runs_within(adapter.store_access(write, store_dir()), launched), role


def test_an_agent_that_sandboxes_its_tools_declares_its_broker(
    adapter: AgentAdapter, tmp_path: Path
) -> None:
    """The store is read-only in the sandbox, so a sandboxed agent's board calls go through
    its launch's broker (``services.store_broker``): the adapter declares how a launch makes
    the broker's inbox the one writable path in the store (``broker_access``), over every
    permission the fleet maps for it, and an agent with no sandbox has no broker."""
    store = tmp_path / "aisquare-home"
    inbox = store / "broker" / "launch-1" / "in"
    for write in get_args(WriteScope):
        for approvals in get_args(Approvals):
            try:
                args = list(
                    adapter.permission_args(PermissionIntent(write, approvals), "coder").args
                )
            except ValueError:
                continue
            args = [*args, *adapter.store_access(write, store)]
            launched = adapter.broker_access(args, store, inbox)
            if not adapter.capabilities.sandboxes_tools or write == "full":
                assert launched is None, f"{adapter.id} {write}: no sandbox, no broker"
                continue
            assert launched is not None, f"{adapter.id} declares no broker for {write}"
            assert str(inbox) in _named(launched), f"{adapter.id}'s inbox is not in its sandbox"
            assert str(inbox) not in _named(args)
            assert str(store) in _named(launched), "the store stays named, read-only"
    assert adapter.broker_access(["--unrelated"], store, inbox) is None


def test_an_agent_that_sandboxes_its_tools_says_what_its_sandbox_may_write(
    adapter: AgentAdapter, tmp_path: Path
) -> None:
    """GATE #113, finding 4: ``sandbox_writes`` decides the install refusal
    (``services.store_broker.refuse_inside``), so an agent that sandboxes its tools answers
    what a sandboxed launch of its may write, read off its own arguments: under a
    workspace scope its workspace, under a read-only one none of it, the store never; and
    ``None`` only where it runs unsandboxed, as an agent without a sandbox always does. An
    adapter that answered ``None`` for a sandboxed launch would let through every install
    its sandbox may write."""
    store = tmp_path / "aisquare-home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for write in get_args(WriteScope):
        try:
            args = list(adapter.permission_args(PermissionIntent(write, "never"), "coder").args)
        except ValueError:
            continue
        args = [*args, *adapter.store_access(write, store)]
        writes = adapter.sandbox_writes(args, workspace)
        if not adapter.capabilities.sandboxes_tools or write == "full":
            assert writes is None, f"{adapter.id} {write}: no sandbox, nothing to answer"
            continue
        assert writes is not None, f"{adapter.id} answers nothing for its {write} sandbox"
        assert not writes.may_write(store / "context.db"), f"{adapter.id}: the store is read-only"
        assert writes.may_write(workspace) is (write == "workspace"), (adapter.id, write)


def test_every_agent_declares_the_launch_inputs_its_start_runs(adapter: AgentAdapter) -> None:
    """GATE #113, finding 2: what a project's tree holds that an agent's unsandboxed start
    there reads and runs (its project settings, hooks, MCP servers) is declared by its
    adapter (``capabilities.launch_inputs``), and every sandboxed agent's profile holds all
    of them read-only (``store_broker.launch_inputs``): the next agent's config cannot be
    planted by a sandboxed Codex in the project."""
    from aisquare.services import store_broker

    inputs = adapter.capabilities.launch_inputs
    assert inputs, f"{adapter.id} declares no launch inputs"
    for name in inputs:
        assert name not in ("", ".", "..") and "/" not in name and "\\" not in name, name
    assert set(inputs) <= set(store_broker.launch_inputs()), "held read-only in every sandbox"


def test_resume_exit_mcp_context_teams_and_telemetry(adapter: AgentAdapter, tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    resume = adapter.resume_args("native-1", transcript)
    assert resume and ("native-1" in resume or str(transcript) in resume)
    assert adapter.exit_command.startswith("/") and "\n" not in adapter.exit_command
    # One tmux key, and never one that answers a dialog with its highlighted choice.
    key = adapter.dismiss_key
    assert key and not any(char.isspace() for char in key), key
    assert key not in {"Enter", "C-m", "KPEnter", "Space"}, key
    mcp = adapter.mcp_args("/opt/aisquare", ["serve", "--stdio"], ["AISQUARE_HOME"])
    assert "/opt/aisquare" in json.dumps([mcp.args, dict(mcp.env), mcp.settings])
    files = adapter.context_files(tmp_path)
    assert files and all(path.parent == tmp_path for path in files)
    assert adapter.disable_native_teams(), "the fleet keeps the agent's own sub-agent teams off"
    telemetry = adapter.telemetry_args("http://127.0.0.1:9/v1/logs")
    if adapter.capabilities.native_telemetry:
        assert "127.0.0.1:9" in json.dumps([telemetry.args, dict(telemetry.env)])


def test_the_unattended_environment_is_the_agents_own_variables(adapter: AgentAdapter) -> None:
    """``unattended_env`` (a session nobody may be watching, the captain's): variables in the
    agent's own name, never one that says which home, process or launch a session is, nor
    one that keeping its native teams off already sets."""
    import re

    env = adapter.unattended_env()
    assert all(isinstance(value, str) for value in env.values())
    assert all(re.fullmatch(r"[A-Z][A-Z0-9_]*", name) for name in env), dict(env)
    capabilities = adapter.capabilities
    taken = {adapter.home_env, capabilities.pid_env, capabilities.tool_shell_env}
    assert not taken & set(env), "never a variable that identifies a session"
    assert not set(adapter.disable_native_teams().env) & set(env)
    assert not any(name.startswith("AISQUARE_") for name in env), "AISquare's are the fleet's"


def test_a_tools_only_session_names_its_server_and_leaves_no_built_in_tool(
    adapter: AgentAdapter, tmp_path: Path
) -> None:
    """``tools_only_args`` (the captain's session): the one server, its command, arguments
    and environment named, and the switches that leave no built-in tool
    (:data:`EXPECTED_TOOLS_OFF`), none switched back on; more than the MCP registration
    every session gets."""
    command, args = "/opt/python", ["-m", "aisquare.services.captain", "--stdio"]
    env = {"AISQUARE_HOME": str(tmp_path / "aisquare home")}
    home = _home(adapter, tmp_path)
    only = adapter.tools_only_args(
        "captain", command, args, env, inherit=(), home=home, cwd=tmp_path, launch_args=[]
    )
    if only is None:
        return  # it cannot be confined so: it cannot be the captain
    assert only and all(isinstance(arg, str) for arg in only)
    assert any("captain" in arg for arg in only), "the server is named"
    named = _named(only)
    assert {command, *args, env["AISQUARE_HOME"]} <= named, named
    for switch in EXPECTED_TOOLS_OFF[adapter.id]:
        assert _runs_within(switch, only), f"{adapter.id} leaves a built-in tool: {switch}"
    assert "--enable" not in only
    assert only != list(adapter.mcp_args(command, args, list(env)).args)


def _operator_server(adapter: AgentAdapter, home: Path, cwd: Path) -> None:
    """An MCP server named ``operator`` where the operator keeps one for the agent: Codex's
    ``config.toml`` in its home, Claude Code's ``.mcp.json`` in the project."""
    server = {"command": "/opt/operator-server", "args": []}
    if adapter.id == "codex":
        (home / "config.toml").write_text(
            '[mcp_servers.operator]\ncommand = "/opt/operator-server"\n', encoding="utf-8"
        )
    elif adapter.id == "claude-code":
        (cwd / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"operator": server}}), encoding="utf-8"
        )


#: What keeps the operator's own MCP server (:func:`_operator_server`) out of each adapter's
#: tools-only session: Claude Code's ``--strict-mcp-config`` (main's captain), Codex's
#: ``enabled=false`` for it (measured on 0.162.1: the request then carried the captain's
#: server alone).
EXPECTED_OPERATOR_OFF: dict[str, list[str]] = {
    "claude-code": ["--strict-mcp-config"],
    "codex": ["-c", "mcp_servers.operator.enabled=false"],
}


def _inherits(
    adapter: AgentAdapter, only: list[str], env: dict[str, str], inherit: tuple[str, ...]
) -> set[str]:
    """The variables a tools-only session's server keeps from the session, as its arguments
    say: Codex's ``env_vars`` (its servers start with ``HOME``, ``LC_CTYPE``, ``PATH`` and
    their ``env`` alone, measured on 0.162.1); every name for Claude Code, whose servers
    inherit the session's environment, the ``env`` of its ``--mcp-config`` being exactly the
    one asked for, so it overwrites none of them."""
    if adapter.id == "codex":
        (kept,) = [
            tomllib.loads(f"v = {item.partition('=')[2]}")["v"]
            for item in only
            if item.startswith("mcp_servers.captain.env_vars=")
        ]
        return set(kept)
    (config,) = [json.loads(arg) for arg in only if arg.startswith("{")]
    assert config["mcpServers"]["captain"]["env"] == env, "its server inherits the rest"
    assert not set(env) & set(inherit)
    return set(inherit)


def test_a_tools_only_session_loads_no_server_of_the_operators_and_keeps_what_it_inherits(
    adapter: AgentAdapter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The captain's session loads no MCP server but its own: one the operator keeps where the
    agent reads servers (:func:`_operator_server`) is kept out (:data:`EXPECTED_OPERATOR_OFF`)
    and the captain's is never switched off; and the variables ``inherit`` names reach the
    server (:func:`_inherits`). An adapter that cannot be confined so answers ``None``."""
    monkeypatch.setattr(codex_adapter, "SYSTEM_CONFIG", tmp_path / "system.toml")
    monkeypatch.setattr(codex_adapter, "MANAGED_CONFIG", tmp_path / "managed.toml")
    home, cwd = _home(adapter, tmp_path), tmp_path / "brain"
    cwd.mkdir()
    _operator_server(adapter, home, cwd)
    env, inherit = {"AISQUARE_HOME": "/srv/home"}, ("TMUX_TMPDIR", "XDG_RUNTIME_DIR")
    only = adapter.tools_only_args(
        "captain", "/opt/python", [], env, inherit=inherit, home=home, cwd=cwd, launch_args=[]
    )
    if only is None:
        return
    assert _runs_within(EXPECTED_OPERATOR_OFF[adapter.id], only), only
    assert not any("captain" in arg and "enabled=false" in arg for arg in only)
    assert set(inherit) <= _inherits(adapter, only, env, inherit)


def test_a_fork_names_the_session_it_forks_and_is_no_resume(
    adapter: AgentAdapter, tmp_path: Path
) -> None:
    """``fork_args`` (a hand-off's Fork) starts a new session from this one's conversation:
    not the resume, which goes on in the same one, and named by the same session; a launch
    names the new session (``mints_id``) only for an agent it names at launch."""
    transcript = tmp_path / "session.jsonl"
    fork = adapter.fork_args("native-1", transcript)
    if fork is None:
        return  # cannot fork: the fork starts fresh with a hand-off prompt
    assert fork.args and all(isinstance(arg, str) for arg in fork.args)
    assert list(fork.args) != adapter.resume_args("native-1", transcript)
    assert "native-1" in fork.args or str(transcript) in fork.args
    assert adapter.capabilities.assigns_session_id or not fork.mints_id


# --- personas: the system prompt ------------------------------------------------------------


def test_the_system_prompt_seam_names_the_block_and_stands_down_before_the_operators(
    adapter: AgentAdapter, tmp_path: Path
) -> None:
    """``system_prompt_args`` puts the persona's block in the agent's system prompt, by its
    file or its text, and answers nothing where the launch arguments already set that seam:
    the adapter's own answer, given back as the operator's, makes it stand down, wherever
    in the arguments it sits."""
    home = _home(adapter, tmp_path)
    text = '## Persona: skeptic\nQuestion every claim, "quoted" or not. ✓'
    block = tmp_path / "skeptic-0123456789ab.md"
    block.write_text(text + "\n", encoding="utf-8")
    seam = adapter.system_prompt_args(block, text, [], home, cwd=tmp_path)
    if seam is None:
        return  # no seam: the session-start hook alone briefs the persona
    assert seam and all(isinstance(arg, str) for arg in seam)
    named = _named(seam)
    assert str(block) in named or text in named, f"{adapter.id}'s seam names neither"
    assert adapter.system_prompt_args(block, text, seam, home, cwd=tmp_path) is None, (
        "the operator's wins"
    )
    assert adapter.system_prompt_args(block, text, ["--verbose", *seam], home, cwd=tmp_path) is None


def test_only_an_agent_with_a_system_seam_keeps_it_across_a_resume(
    adapter: AgentAdapter, tmp_path: Path
) -> None:
    """``capabilities.resume_keeps_system_prompt`` tells a restart's receipt that the persona
    in the system prompt is still its first launch's: only an agent whose
    ``system_prompt_args`` has a seam has one to keep."""
    keeps = adapter.capabilities.resume_keeps_system_prompt
    assert isinstance(keeps, bool)
    seam = adapter.system_prompt_args(
        tmp_path / "b.md", "persona", [], _home(adapter, tmp_path), cwd=tmp_path
    )
    assert seam is not None or not keeps, f"{adapter.id} keeps a system prompt it cannot set"


# --- personas: skills ------------------------------------------------------------------------


def test_skill_dirs_are_absolute_the_users_first_and_the_projects_in_the_repository(
    adapter: AgentAdapter, tmp_path: Path
) -> None:
    """``skill_dirs``: where ``persona export --skill`` writes (the first of each) and what
    ``import --list`` reads, the user's dirs outside the repository and the project's
    inside it, every one absolute; outside a repository, the user's alone."""
    home = _home(adapter, tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    dirs = adapter.skill_dirs(home, repo)
    assert isinstance(dirs, SkillDirs) and dirs.user and dirs.project
    every = (*dirs.user, *dirs.project)
    assert all(path.is_absolute() for path in every), every
    assert len(set(every)) == len(every), "each dir once"
    assert all(path.is_relative_to(repo) and path != repo for path in dirs.project)
    assert not any(path.is_relative_to(repo) for path in dirs.user)
    assert adapter.skill_dirs(home, None) == SkillDirs(dirs.user, ())


# --- personas: the headless call of the import engine --------------------------------------


def test_the_headless_call_holds_the_schema_keeps_no_session_and_reads_its_answer(
    adapter: AgentAdapter, tmp_path: Path
) -> None:
    """``headless_call`` (``persona import --engine manager``) holds the schema, by its file
    or its text, keeps no session and runs no hook where a launch argument can say so
    (:data:`EXPECTED_HEADLESS`), and pins a model it is given; ``headless_answer`` reads the
    answer the agent gave (:data:`RECORDED_HEADLESS`), and nothing from a call that gave
    none."""
    schema = tmp_path / "persona-draft.schema.json"
    shape = {"type": "object", "properties": {"name": {"type": "string"}}}
    schema.write_text(json.dumps(shape), encoding="utf-8")
    out = tmp_path / "answer.json"
    home = _home(adapter, tmp_path)
    call = adapter.headless_call(schema, out, None, home=home, cwd=tmp_path)
    if call is None:
        return  # no headless call: the engine ladder goes on to the next engine
    assert call and all(isinstance(arg, str) for arg in call)

    def schema_in(arg: str) -> bool:
        with suppress(ValueError):
            return bool(arg == str(schema) or json.loads(arg) == shape)
        return False

    assert any(map(schema_in, call)), f"{adapter.id}'s call holds no schema"
    named = _named(call)
    for word in EXPECTED_HEADLESS[adapter.id]:
        assert word in named, f"{adapter.id}'s headless call lacks {word}"
    pinned = adapter.headless_call(schema, out, "chosen-model", home=home, cwd=tmp_path)
    assert pinned is not None and "chosen-model" in pinned and "chosen-model" not in call
    recorded = RECORDED_HEADLESS[adapter.id]
    if recorded["out"] is not None:
        out.write_text(recorded["out"], encoding="utf-8")
    assert adapter.headless_answer(str(recorded["stdout"]), out) == DRAFT
    out.unlink(missing_ok=True)
    assert adapter.headless_answer("", out) is None
    assert adapter.headless_answer("not json", out) is None


def test_the_headless_call_runs_no_tool_and_no_mcp_server_of_the_operators(
    adapter: AgentAdapter, tmp_path: Path
) -> None:
    """Its stdin frames an untrusted source (``persona import https://…``), so the turn runs
    with no built-in tool (:data:`EXPECTED_TOOLS_OFF`, the captain's switches: main's engine
    ran Claude Code's ``--tools ""``) and none of the operator's MCP servers
    (:data:`EXPECTED_OPERATOR_OFF`), none switched back on (review r17 of #113, finding 4)."""
    schema = tmp_path / "persona-draft.schema.json"
    schema.write_text('{"type": "object"}', encoding="utf-8")
    home = _home(adapter, tmp_path)
    _operator_server(adapter, home, tmp_path)
    call = adapter.headless_call(schema, tmp_path / "answer.json", None, home=home, cwd=tmp_path)
    if call is None:
        return  # no headless call: the engine ladder goes on to the next engine
    for switch in EXPECTED_TOOLS_OFF[adapter.id]:
        assert _runs_within(switch, call), f"{adapter.id}'s headless call leaves a tool: {switch}"
    off = EXPECTED_OPERATOR_OFF.get(adapter.id)
    assert off is None or _runs_within(off, call), f"{adapter.id} loads the operator's server"
    assert "--enable" not in call


# --- account slots: a login's fingerprint, folder trust shared between accounts ---------------


def _accounts_or_skip(adapter: AgentAdapter, monkeypatch: pytest.MonkeyPatch) -> AgentAccounts:
    if adapter.accounts is None:
        pytest.skip(f"{adapter.label} has no account slots")
    # The login a fixture writes is a file: never a Keychain the test machine has.
    monkeypatch.setattr(accounts_core, "keychain_platform", lambda: False)
    return adapter.accounts


def _sign_in(adapter: AgentAdapter, account: ClaudeAccount, which: str) -> None:
    """Write the fixture's ``login`` (or ``relogin``: the same login after a new sign-in)."""
    for name, text in _fixture(adapter)["accounts"][which].items():
        path = account.config_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_an_accounts_slot_fingerprints_its_login_and_a_new_sign_in_changes_it(
    adapter: AgentAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``login_fingerprint`` is what a *Log in* over a login still on disk waits to see
    change (``claude_accounts.sign_in_landed``): none before a login, the same digest while
    it stands, another once a sign-in replaced it, and never a token."""
    slot = _accounts_or_skip(adapter, monkeypatch)
    account = accounts_core.create_account(adapter.id)
    assert slot.login_fingerprint(account) is None
    assert accounts_service.sign_in_landed(account) is None
    _sign_in(adapter, account, "login")

    first = slot.login_fingerprint(account)
    assert first is not None and len(first) >= 32
    assert slot.login_fingerprint(account) == first, "read afresh, the same login"
    written = "".join(_fixture(adapter)["accounts"]["login"].values())
    assert first not in written, "a digest, never what the login file holds"
    identity = accounts_service.sign_in_landed(account)
    assert identity is not None
    assert accounts_service.sign_in_landed(account, since=first) is None, "the login it opened over"

    _sign_in(adapter, account, "relogin")
    assert slot.login_fingerprint(account) not in (None, first)
    assert accounts_service.sign_in_landed(account, since=first) == identity


def test_a_slots_config_dir_holds_its_hooks_once_signed_in(
    adapter: AgentAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``AgentAccounts.config_dir``: a slot's sign-in connects aisquare in the config dir the
    agent reads there (Gemini-shaped's ``<slot>/.gemini``, not the slot), which a hook naming
    it (``--config-dir``) maps back to the slot."""
    slot = _accounts_or_skip(adapter, monkeypatch)
    account = accounts_core.create_account(adapter.id)
    config = slot.config_dir(account.config_dir)
    assert accounts_core.agent_config_dir(account) == config
    _sign_in(adapter, account, "login")  # the agent's own sign-in, which makes its config dir

    described = accounts_service.complete_sign_in(account)

    assert described.hooks_installed, described.detail
    assert (config / adapter.settings_name).is_file()
    assert agent_core.hooks_installed(adapter.id, config)
    assert accounts_core.managed_slot(config, adapter.id) == account.slot
    assert accounts_core.managed_slot(account.config_dir, adapter.id) == account.slot


def test_an_accounts_slot_shares_a_folders_trust_between_its_accounts(
    adapter: AgentAdapter, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``trust`` (``types.FolderTrust``): a yes on one account reaches another of the same
    agent at a launch, in that agent's own file and rules, and touches nothing else there."""
    slot = _accounts_or_skip(adapter, monkeypatch)
    trust = slot.trust
    if trust is None:
        return  # an agent that asks nothing per folder has no trust to share
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
            cwd=repo,
            check=True,
            capture_output=True,
        )
    one, two = (accounts_core.create_account(adapter.id) for _ in range(2))
    for account in (one, two):
        _sign_in(adapter, account, "login")
    scope = trust.scope(claude_sharing.trust_scope(repo / "src"))
    assert scope.folder == Path(os.path.realpath(repo / "src")) and scope.key in scope.covering
    assert trusted_by(trust.read(one), scope) is None

    assert trust.grant(one, scope.key) == [scope.key]
    assert trust.grant(one, scope.key) == [], "a key already trusted is left out"
    assert trusted_by(trust.read(one), scope) == scope.key
    assert (one.config_dir / trust.file_name).is_file(), "file_name names the file in the home"
    login = (accounts_core.identity(two), slot.login_fingerprint(two))

    notes = accounts_service.share_for_launch(repo / "src", account=two)

    name, other = accounts_core.label(two), accounts_core.label(one)
    assert notes == [f"{scope.key} is trusted on {other} — now on {name} too"]
    assert isinstance(notes[0], accounts_service.SharedNote)
    assert trusted_by(trust.read(two), scope) == scope.key
    assert (accounts_core.identity(two), slot.login_fingerprint(two)) == login, "its login kept"
    assert accounts_service.share_for_launch(repo / "src", account=two) == [], "once"


# --- the shared fleet path, with no branch for any agent ---------------------------------


def test_the_fake_agent_runs_through_the_fleet_like_any_other(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    fake_agent: FakeAgentAdapter,
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
    store = fake_agent.store_access("workspace", store_dir())
    assert native[: 6 + len(store)] == [
        "--write",
        "workspace",
        "--approvals",
        "auto",
        *store,
        "--title",
        label,
    ]
    env = spawned["env"]
    assert isinstance(env, dict) and env["FAKE_AGENT_TEAMS"] == "off"
    assert "--no-subagents" in native
    assert "start here" not in command, "no positional prompt: the fleet types it"

    # The window's own `aisquare launch` execs the fake with those arguments.
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    result = runner.invoke(app, command[command.index("launch") :])
    assert result.exit_code == 0, result.output
    argv, launched_env = execute.call_args.args[1], execute.call_args.args[2]
    assert Path(argv[0]).name.startswith("fakeagent")
    # With its broker's inbox writable in its sandbox, the rest of the store read-only;
    # the broker named in its environment where one runs (POSIX: its bell is a FIFO).
    broker = store_broker.launch_dir(store_dir(), launched_env["AISQUARE_LAUNCH_ID"])
    protect, writable = store_broker.launch_paths(launched_env, Path.cwd())
    expected = fake_agent.broker_access(
        native, store_dir(), broker / store_broker.INBOX, protect=protect, writable=writable
    )
    assert expected is not None and argv[-len(expected) :] == expected
    if sys.platform != "win32":
        assert launched_env[store_broker.DIR_ENV] == str(broker)

    # The adapter's own exit command, typed by `fleet stop`.
    assert fleet_service._exit_command(receipt.agent) == "/quit"
    tmux.honours_exit = False
    fleet_service.stop(project, label, grace=0)
    assert (receipt.agent.pane_id, "literal", "/quit") in tmux.typed


def test_the_hand_over_dismisses_a_dialog_with_the_fake_agents_own_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_agent: FakeAgentAdapter
) -> None:
    """The automatic hand-over's stop (``dismiss_dialog``) presses the row's adapter's
    ``dismiss_key`` at a screen that does not show its empty prompt, then that adapter's
    exit command once the prompt is back: no key of Codex's or Claude Code's."""
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
    monkeypatch.setattr(fleet_service, "_sleep", lambda seconds: None)
    agent = fleet_service.spawn(team_project(tmp_path), "coder", agent="fake-agent").agent
    # Its screen read as Codex's is (an empty-prompt mark), at a dialog its own key closes.
    monkeypatch.setattr(
        fake_agent, "screen", replace(screen_of(fake_agent), idle_prompt_mark="fake>")
    )
    tmux.screens[agent.pane_id] = ["Really quit? [y/N]"]
    send_keys = tmux.send_keys

    def keys(pane_id: str, *names: str) -> None:
        send_keys(pane_id, *names)
        if names == (fake_agent.dismiss_key,):
            tmux.screens[pane_id] = ["fake> "]

    monkeypatch.setattr(tmux, "send_keys", keys)

    stopped = fleet_service.stop(team_project(tmp_path), agent.label, grace=0, dismiss_dialog=True)

    typed = [(kind, text) for pane, kind, text in tmux.typed if pane == agent.pane_id]
    assert typed[:2] == [("key", "C-g"), ("literal", "/quit")], typed
    assert stopped.killed is None and stopped.dismissed is not None


# --- the transcript, for Remote's needs-you cards and Transcript tab --------------------


def _moments(adapter: AgentAdapter) -> dict[str, Any]:
    return _moment_sets(adapter)[0]


def _moment_sets(adapter: AgentAdapter) -> list[dict[str, Any]]:
    """The adapter's recorded moments, then those of each other release of the agent recorded
    beside them (``beside``: Codex 0.164.0's rollout beside 0.162.1's)."""
    path = TRANSCRIPTS / f"{adapter.id}.json"
    assert path.is_file(), f"add {path.relative_to(TRANSCRIPTS.parents[1])} for {adapter.id}"
    recorded = json.loads(path.read_text(encoding="utf-8"))
    sets: list[dict[str, Any]] = [recorded["moments"]]
    sets += [release["moments"] for release in recorded.get("beside", [])]
    for moments in sets:
        assert set(moments) == {"permission", "question", "interrupted", "asked"}
    return sets


def _written(moment: dict[str, Any], path: Path) -> Path:
    """The transcript as it stood at ``moment``: its records, or the recorded session's first
    lines."""
    if "rollout" in moment:
        lines = (TRANSCRIPTS / moment["rollout"]).read_text(encoding="utf-8").splitlines()
        text = "".join(line + "\n" for line in lines[: moment["lines"]])
    else:
        text = "".join(json.dumps(record) + "\n" for record in moment["records"])
    path.write_text(text, encoding="utf-8")
    return path


def _reader(adapter: AgentAdapter) -> Any:
    reader = adapter.transcript_reader
    assert reader is not None, f"{adapter.id} has no transcript_reader: Remote reads nothing of it"
    return reader


@pytest.mark.parametrize("moment", ["permission", "question", "interrupted", "asked"])
def test_the_transcript_reader_reads_each_recorded_moment(
    adapter: AgentAdapter, moment: str, tmp_path: Path
) -> None:
    """What the needs-you scan classifies on, from the agent's own transcript: the calls
    still waiting and what each waits for, the newest record, the agent's last words, and no
    walk cut short on a file this small."""
    from aisquare.services import transcript

    reader = _reader(adapter)
    for moments in _moment_sets(adapter):
        recorded = moments[moment]
        expect = recorded["expect"]
        tail = transcript.read_transcript_tail(
            _written(recorded, tmp_path / "session.jsonl"), agent=adapter.id
        )
        assert tail is not None and tail.cut_at is None
        assert tail.newest == expect["newest"]
        pending = [[tool.tool_use_id, tool.name, reader.call_asks(tool)] for tool in tail.pending]
        assert pending == expect["pending"]
        if "mark" in expect:
            assert reader.call_mark(tail.pending[0]) == expect["mark"]
        if "last_text_end" in expect:
            assert tail.last_text is not None and tail.last_text.endswith(expect["last_text_end"])


def test_a_card_is_made_from_the_readers_facts_and_answered_with_its_keys(
    adapter: AgentAdapter, tmp_path: Path
) -> None:
    """The shared needs scan asks the reader, not the agent's name: a pending call with the
    board's attention is a permission card on the adapter's own keys, a question call is a
    question card whose options are the adapter's keys for them, and each key is one a
    phone may send (``remote_needs.NEEDS_ANSWER_KEYS``). The fake answers with ``y``/``n``
    and arrows, so digits written into a service fail here."""
    from datetime import UTC, timedelta

    from aisquare.models import FleetAgent, FleetAgentStatus, ProjectInfo, TeamEvent, TeamSession
    from aisquare.services import remote_needs, transcript

    reader = _reader(adapter)
    project = ProjectInfo(id="prj_contract", root=tmp_path)
    moments = _moments(adapter)
    for moment, kind in (("permission", "permission"), ("question", "question")):
        path = _written(moments[moment], tmp_path / f"{moment}.jsonl")
        tail = transcript.read_transcript_tail(path, agent=adapter.id)
        assert tail is not None and tail.newest_at is not None
        now = tail.newest_at + timedelta(minutes=1)
        row = FleetAgent(
            id="agt_1", project_id=project.id, label="coder-1", role="coder", agent=adapter.id,
            binary=adapter.binary, pane_id="%1", session_id="ses_1", cwd=tmp_path,
            created_at=tail.newest_at - timedelta(hours=1),
        )  # fmt: skip
        seen = tail.newest_at + timedelta(seconds=1)
        session = TeamSession(
            id="ses_1", project_id=project.id, role="coder", started_at=row.created_at,
            last_seen_at=seen, state="attention", transcript_path=str(path),
        )  # fmt: skip
        notice = TeamEvent(
            seq=1, id="evt_1", project_id=project.id, session_id="ses_1", kind="attention",
            text=f"{adapter.label} needs you", created_at=seen,
        )  # fmt: skip
        status = FleetAgentStatus(agent=row, state="attention", session=session)
        (card,) = remote_needs.needs_from_agent(
            status, tail, project=project, events=[notice], now=now.astimezone(UTC)
        )
        assert card.kind == kind, (moment, card.reason)
        options = moments[moment]["expect"].get("options", 0)
        keys = reader.answer_keys(kind, options)
        assert keys, f"{adapter.id} offers no quick answer for a {kind}"
        assert [answer.keys for answer in card.answers] == [key.keys for key in keys]
        assert all(set(key.keys) <= remote_needs.NEEDS_ANSWER_KEYS for key in keys)


def test_the_transcript_page_names_the_human_and_the_agent(
    adapter: AgentAdapter, tmp_path: Path
) -> None:
    """A page of the Transcript tab, from the agent's own records: the human's words under
    ``> you``, the agent's under its own speaker (``* claude`` named every agent's)."""
    import re

    from aisquare.core.agent_adapters.transcripts import YOU, speaker_line
    from aisquare.services import transcript

    reader = _reader(adapter)
    assert reader.speaker and re.fullmatch(r"[a-z][a-z0-9-]*", reader.speaker)
    path = _written(_moments(adapter)["asked"], tmp_path / "session.jsonl")
    page = transcript.read_page(path, agent=adapter.id, width=60)
    assert YOU in page.lines and speaker_line(reader.speaker) in page.lines
    assert page.more is False and page.lines[-1] == ""


def test_the_reply_since_the_newest_prompt_is_read_off_a_recorded_moment(
    adapter: AgentAdapter, tmp_path: Path
) -> None:
    """``reply_since`` (the captain's reply to what the owner just said), off the agent's own
    transcript: its words after the newest prompt of a turn that ended on them; nothing yet
    when that prompt is older than ``since``; no words for a turn interrupted before any;
    nothing for a transcript that is not there."""
    from datetime import timedelta

    from aisquare.services import transcript

    reader = _reader(adapter)
    moments = _moments(adapter)
    asked = moments["asked"]
    path = _written(asked, tmp_path / "asked.jsonl")
    reply = reader.reply_since(path, None)
    assert reply is not None and reply.endswith(asked["expect"]["last_text_end"]), reply
    tail = transcript.read_transcript_tail(path, agent=adapter.id)
    assert tail is not None and tail.newest_at is not None
    assert reader.reply_since(path, tail.newest_at + timedelta(minutes=1)) is None
    assert reader.reply_since(path, tail.newest_at - timedelta(days=1)) == reply
    interrupted = _written(moments["interrupted"], tmp_path / "interrupted.jsonl")
    assert not reader.reply_since(interrupted, None), "an interrupted turn said nothing"
    assert reader.reply_since(tmp_path / "gone.jsonl", None) is None


# --- Remote's key pad: the keys that end the agent on one press --------------------------


def test_the_one_press_exits_are_keys_the_pad_reaches_the_agent_with(
    adapter: AgentAdapter,
) -> None:
    """``capabilities.one_press_exits`` names keys as the agent is sent them: each is what a
    key the pad sends becomes past the adapter's ``key_spellings``. A key no pad key becomes
    (``^C``, ``C-C``) guards nothing, and the press it meant goes unasked. An adapter that
    declares one reads its empty prompt off its screen, or every press of it is asked."""
    from aisquare.services import remote_server

    exits = adapter.capabilities.one_press_exits
    assert isinstance(exits, frozenset)
    spellings = adapter.key_spellings
    for key in exits:
        pads = [key, *(pad for pad, sent in spellings.items() if sent == key)]
        assert any(
            remote_server.REMOTE_KEY_NAME.fullmatch(pad) and spellings.get(pad, pad) == key
            for pad in pads
        ), f"{adapter.id}: no pad key reaches it as {key!r}"


def test_send_keys_asks_before_the_keys_that_end_the_agent_on_one_press(
    adapter: AgentAdapter, tmp_path: Path
) -> None:
    """Remote's ``send-keys`` asks the row's adapter which keys end its agent with one press at
    its empty prompt, never its name: with nothing to say the agent is not there (a screen
    tmux would not show), each of its own is refused unconfirmed (409 ``one_press_exit``,
    naming the agent), and any other exit key goes as a first press. Claude Code declares
    none and Codex Ctrl-C and Ctrl-D; the fake ends on Ctrl-D alone, so a service that
    knows Codex's pair fails here for it."""
    from datetime import UTC, datetime

    from aisquare.core.tmux import TmuxError
    from aisquare.models import FleetAgent
    from aisquare.services import remote_server

    class Unread:
        def screen_text(self, pane_id: str) -> list[str]:
            raise TmuxError("capture-pane: server exited unexpectedly")

    row = FleetAgent(
        id="agt_1", project_id="prj_contract", label="coder-1", role="coder", agent=adapter.id,
        binary=adapter.binary, pane_id="%1", cwd=tmp_path, created_at=datetime.now(UTC),
    )  # fmt: skip
    exits = adapter.capabilities.one_press_exits
    for key in sorted(remote_server.EXIT_KEYS | exits):
        refused = remote_server.remote_one_press_exit(
            Unread(),  # type: ignore[arg-type]
            row,
            [key],
            label=row.label,
        )
        if key not in exits:
            assert refused is None, f"{adapter.id}: {key} asked"
            continue
        assert refused is not None, f"{adapter.id}: {key} went unasked"
        assert (refused.status, refused.error) == (409, "one_press_exit")
        assert adapter.label in str(refused.extra["reason"]), "the page asks in its name"


# --- a dialog of the agent's own that only its screen shows, for Remote's needs-you cards ---


def _screens(adapter: AgentAdapter) -> dict[str, Any] | None:
    """The adapter's screens (``tests/fixtures/agent_screens/<id>.json``): each dialog of its
    own with the words it reads (with ``dialog_on_screen``) and the prompt it reads (with
    ``screen_prompt``, where a ``prompt`` says), and screens with none. ``None`` for an
    adapter that reads neither off its screen, which brings none."""
    path = SCREENS / f"{adapter.id}.json"
    screen = screen_of(adapter)
    if screen.dialog_on_screen is None and screen.screen_prompt is None:
        assert not path.exists(), f"{adapter.id} reads nothing off its screen: {path.name}"
        return None
    assert path.is_file(), f"add {path.relative_to(SCREENS.parents[1])} for {adapter.id}"
    screens: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    assert screens["dialogs"] and screens["not_dialogs"]
    return screens


def test_an_agent_reads_its_own_dialogs_off_its_screen(adapter: AgentAdapter) -> None:
    """``AgentScreen.dialog_on_screen`` on the agent's recorded screens: each dialog's words,
    and ``None`` for a screen with no dialog on it (its empty prompt, a draft, a turn)."""
    screens = _screens(adapter)
    read = screen_of(adapter).dialog_on_screen
    if screens is None or read is None:
        return
    for shown in screens["dialogs"]:
        assert read(shown["rows"]) == shown["words"], shown["what"]
    for shown in screens["not_dialogs"]:
        assert read(shown["rows"]) is None, shown["what"]


def test_a_dialog_no_hook_reports_is_a_card_naming_no_agent_with_no_quick_answers(
    adapter: AgentAdapter, tmp_path: Path
) -> None:
    """The shared needs scan asks the adapter what a live row's screen shows, with no call
    pending and no bell, as a fresh agent has neither: a dialog of its own is a permission
    card whose reason names who and no agent, its words the excerpt, with no quick answers
    (a key's meaning differs from one dialog to the next) and its push held 30 s; any other
    screen is no card. The screen of an agent that reads no dialog off it is never read, so
    its cards are as they were."""
    from datetime import UTC, datetime, timedelta

    from aisquare.models import FleetAgent, FleetAgentStatus, ProjectInfo
    from aisquare.services import remote_needs

    project = ProjectInfo(id="prj_contract", root=tmp_path)
    now = datetime(2026, 10, 19, 23, 0, tzinfo=UTC)
    row = FleetAgent(
        id="agt_1", project_id=project.id, label="coder-1", role="coder", agent=adapter.id,
        binary=adapter.binary, pane_id="%1", cwd=tmp_path, created_at=now - timedelta(seconds=20),
    )  # fmt: skip
    status = FleetAgentStatus(agent=row, state="waiting", detail="no hooks")

    def cards(rows: list[str] | None) -> list[Any]:
        def screen() -> list[str] | None:
            assert rows is not None, f"{adapter.id} reads no dialog off its screen: never read"
            return rows

        return remote_needs.needs_from_agent(
            status, None, project=project, events=[], now=now, pane_screen=screen
        )

    screens = _screens(adapter)
    if screens is None or screen_of(adapter).dialog_on_screen is None:
        assert cards(None) == []
        return
    for shown in screens["dialogs"]:
        (card,) = cards(shown["rows"])
        assert (card.kind, card.reason) == ("permission", "coder-1 shows a dialog that needs you")
        assert card.excerpt == remote_needs._needs_excerpt(shown["words"])
        assert card.answers == ()
        assert card.push_after == now + timedelta(seconds=30)
    for shown in screens["not_dialogs"]:
        assert cards(shown["rows"]) == [], shown["what"]


def test_a_prompt_on_the_agents_screen_is_read_with_keys_a_phone_may_send(
    adapter: AgentAdapter,
) -> None:
    """``AgentScreen.screen_prompt`` (the captain's press and paste ask it of the pane's own
    agent) on the agent's recorded screens: every dialog of its own is a prompt, a ``trust``
    or ``dialog`` one with no keys (the owner's to answer) and a ``chooser`` or ``yn`` one with
    its No, each key one a quick answer may send; ``None`` on every screen with none; and the
    shape and keys a dialog's ``prompt`` names, where it names them."""
    from aisquare.core.agent_adapters.types import ScreenShape
    from aisquare.services import remote_needs

    read = screen_of(adapter).screen_prompt
    screens = _screens(adapter)
    if read is None or screens is None:
        return
    for shown in screens["dialogs"]:
        prompt = read(shown["rows"])
        assert prompt is not None, f"{adapter.id} reads no prompt on {shown['what']}"
        assert prompt.shape in get_args(ScreenShape) and prompt.question and prompt.whole
        keys = {key for key in (prompt.yes_key, prompt.no_key) if key is not None}
        assert keys <= remote_needs.NEEDS_ANSWER_KEYS, (shown["what"], keys)
        if prompt.shape in ("trust", "dialog"):
            assert not keys, f"{shown['what']}: the owner's to answer"
        else:
            assert prompt.no_key is not None, shown["what"]
        if "prompt" in shown:
            assert [prompt.shape, prompt.yes_key, prompt.no_key] == shown["prompt"], shown["what"]
    for shown in screens["not_dialogs"]:
        assert read(shown["rows"]) is None, shown["what"]


def test_a_line_is_typed_only_where_the_agents_screen_reads_ready(adapter: AgentAdapter) -> None:
    """``screen_ready`` (``AgentScreen.ready_on_screen``, else its default; the captain's
    word over a ``working`` read) on the agent's recorded screens: no dialog of its own is
    ready, and of the screens with none, exactly those marked ``ready`` are (Claude Code's
    idle box with a finished turn above it, not one mid-turn; Codex's empty composer, not
    a turn running nor a draft)."""

    screens = _screens(adapter)
    if screens is None:
        assert not screen_of(adapter).ready(["$ "])
        return
    for shown in screens["dialogs"]:
        assert not screen_of(adapter).ready(shown["rows"]), shown["what"]
    for shown in screens["not_dialogs"]:
        ready = screen_of(adapter).ready(shown["rows"])
        assert ready is shown.get("ready", False), shown["what"]


def test_a_screen_ready_for_a_line_shows_its_prompt_drawn(adapter: AgentAdapter) -> None:
    """``screen_drawn`` (``AgentScreen.prompt_on_screen``, else its empty prompt; what a
    waiting captain's line needs, review r17 of #113, finding 1) on the agent's recorded
    screens: every screen ready for a line shows its prompt drawn, and no dialog of its own
    does."""
    screens = _screens(adapter)
    if screens is None:
        assert not screen_of(adapter).drawn(["$ "])
        return
    for shown in screens["dialogs"]:
        assert not screen_of(adapter).drawn(shown["rows"]), shown["what"]
    for shown in screens["not_dialogs"]:
        if screen_of(adapter).ready(shown["rows"]):
            assert screen_of(adapter).drawn(shown["rows"]), shown["what"]


def test_an_agent_that_declares_no_ready_screen_is_ready_at_its_empty_prompt_alone() -> None:
    """The default an adapter gets with no ``ready_on_screen`` (the fake's): its empty prompt
    (``idle_on_screen``, else ``idle_prompt_mark`` starting its last line) with no turn
    running on it (``working_on_screen``), and never ready with none of them declared."""

    plain = screen_of(FakeAgentAdapter())
    assert plain.ready_on_screen is None
    idle, busy, draft = ["done", "fake> "], ["running it", "fake> "], ["fake> ?? deploy it"]
    assert not any(plain.ready(rows) for rows in (idle, busy, draft))
    marked = replace(plain, idle_prompt_mark="fake>")
    assert marked.ready(idle) and marked.ready(draft), "the mark alone"
    read = replace(
        marked,
        idle_on_screen=lambda rows: rows[-1].strip() == "fake>",
        working_on_screen=lambda rows: "running it" in rows,
    )
    assert read.ready(idle) and not read.ready(draft), "its own reading"
    assert not read.ready(busy), "a turn running on it"


def test_what_a_line_must_not_go_into_is_named_on_the_agents_screen(adapter: AgentAdapter) -> None:
    """``screen_modal`` (``AgentScreen.modal_on_screen``, else its default; what the
    captain's ``say`` refuses by name) on the agent's recorded screens: each dialog of its own
    is named, as its ``modal`` says where it says (Claude Code's ``[y/N]`` line is a prompt to
    answer, not a dialog to refuse), and a screen with none names one only where its
    ``modal`` does (Claude Code's rating survey over its box, its model picker)."""

    screens = _screens(adapter)
    if screens is None:
        assert screen_of(adapter).modal(["$ "]) is None
        return
    for shown in screens["dialogs"]:
        said = screen_of(adapter).modal(shown["rows"])
        if "modal" in shown:
            assert said == shown["modal"], shown["what"]
        else:
            assert said, f"{adapter.id} names nothing on {shown['what']}"
    for shown in screens["not_dialogs"]:
        assert screen_of(adapter).modal(shown["rows"]) == shown.get("modal")


def test_an_agent_that_declares_no_modal_screen_names_its_dialogs_by_their_words() -> None:
    """The default an adapter gets with no ``modal_on_screen`` (the fake's): the words of a
    dialog of its own (``dialog_on_screen``), a dialog whose words are off screen named as
    one, else its prompt's question (``screen_prompt``), and nothing with neither."""

    plain = screen_of(FakeAgentAdapter())
    assert plain.modal_on_screen is None
    asking, scrolled = ["?? Allow shell: make deploy", "(y/n)"], ["  make deploy", "(y/n)"]
    assert plain.modal(asking) == "Allow shell: make deploy"
    assert plain.modal(scrolled) == "a dialog of its own"
    assert plain.modal(["fake> "]) is None
    prompts = replace(plain, dialog_on_screen=None)
    assert prompts.modal(asking) == "Allow shell: make deploy", "its prompt's question"
    assert prompts.modal(["fake> "]) is None
    assert replace(prompts, screen_prompt=None).modal(asking) is None


def test_the_default_binary_is_the_default_agents_own(
    monkeypatch: pytest.MonkeyPatch, fake_agent: FakeAgentAdapter
) -> None:
    """Nothing shared spells the binary a launch runs when none is named: it is the default
    agent's (``agent_adapters.default_binary``), so the fake made the default gives its own
    to a role's resolution, to a fleet row recorded without one and to the status line."""
    from datetime import UTC, datetime

    from aisquare.cli.ui.sidebar import status_line_text
    from aisquare.core import agent_adapters
    from aisquare.models import FleetAgent

    monkeypatch.setattr(agent_adapters, "DEFAULT_AGENT", fake_agent.id)
    for name in (harness._BIN_ENV_GLOBAL, harness._bin_env_var("coder")):
        monkeypatch.delenv(name, raising=False)

    assert agent_adapters.default_binary() == harness.default_binary() == fake_agent.binary
    assert harness.resolve_binary("coder") == harness.BinaryResolution(
        binary=fake_agent.binary, source="default"
    )
    row = FleetAgent(
        id="a1",
        project_id="p",
        label="coder-1",
        role="coder",
        pane_id="%1",
        cwd=Path("/w"),
        created_at=datetime.now(UTC),
    )
    assert row.binary == fake_agent.binary
    assert status_line_text(True, None).plain.endswith(fake_agent.binary)


def test_a_binarys_flags_session_id_and_account_are_its_adapters(
    adapter: AgentAdapter, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nothing shared decides by a binary's name (B1, B2, B10): a role's own flags are those
    of the agent the binary's name runs (``adapter_for_binary``), a session id is minted for
    it at launch exactly when it says it takes one (``capabilities.assigns_session_id``),
    and a hook that did not name its account reads it off the transcript only for an agent
    whose hooks are not told their home (``capabilities.hook_names_home``), else from its
    own home variable."""
    from aisquare.services import explainability
    from aisquare.services import team as team_service

    binary = f"/opt/bin/{adapter.binary}"
    assert adapter_for_binary(binary) is adapter
    wrapper = adapter.role_defaults("ui-tester", binary=f"{binary}-next", args=[])
    assert wrapper.args == [], "a wrapper by another name may be another program"

    monkeypatch.delenv(explainability._PIN_ENV_VAR, raising=False)
    takes = adapter.capabilities.assigns_session_id
    assert explainability.accepts_session_id(binary) is takes
    planned = explainability.plan_session_identity(binary, [])
    assert (planned.session_id is not None) is takes, planned.note
    assert takes or adapter.binary in str(planned.note)

    home = tmp_path / "home"
    monkeypatch.setenv(adapter.home_env, str(home))
    transcript = tmp_path / "dir" / "projects" / "slug" / "session.jsonl"
    read = home if adapter.capabilities.hook_names_home else tmp_path / "dir"
    assert team_service.session_account(str(transcript), adapter.id) == str(read)
    assert team_service.session_account(None, adapter.id) == str(home)


def test_a_sign_in_runs_the_agents_own_command_or_its_tui_bare(
    adapter: AgentAdapter, tmp_path: Path
) -> None:
    """S7: ``sign_in_args`` is the agent's own sign-in command, run in the slot (Codex's
    ``login``), or ``()`` for one that signs in from its TUI (Claude Code's, the
    Gemini-shaped one's): its window starts the binary in the slot with nothing after the
    slot, and the human leaves it with its ``exit_command``; either way the login has
    landed once ``login_fingerprint`` reads one."""
    accounts = adapter.accounts
    if accounts is None:
        return
    account = ClaudeAccount(slot=7, config_dir=tmp_path / "7", managed=True, agent=adapter.id)
    args = accounts.sign_in_args()
    command = accounts_service.sign_in_command(account)
    assert command[-1 - len(args) :] == ["7", *args], command
    if not args:
        assert adapter.exit_command.strip(), f"{adapter.id} signs in from its TUI: how to leave it"
    assert accounts.login_fingerprint(account) is None, "an empty slot holds no login"


def test_at_most_one_agent_keeps_the_slots_from_before_agents() -> None:
    """``AgentAccounts.legacy_slots`` is Claude Code's alone: the store's own table and
    project key, the listings' first place. A new agent's slots are keyed by its id."""
    legacy = [a.id for a in adapters() if a.accounts is not None and a.accounts.legacy_slots]
    assert legacy == ["claude-code"]


def test_an_agents_slots_are_arranged_launched_and_refused_by_its_own_answers(
    adapter: AgentAdapter, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nothing shared branches on Claude Code's id (B8): a slot of every agent is listed,
    named, arranged (alias, disabled, default, order) in its own registry, launched with its
    own variables, read through its own slot (identity, sign-in, token, subscription,
    version) and its plain home refused a removal with its own way to sign out."""
    slot = _accounts_or_skip(adapter, monkeypatch)
    account = accounts_core.create_account(adapter.id)
    _sign_in(adapter, account, "login")

    listed = accounts_service.list_accounts()
    assert account.slot in {a.slot for a in listed if a.agent == adapter.id}
    assert accounts_core.label(account) == f"account {account.slot}"
    plain = accounts_core.default_account(agent=adapter.id)
    assert (plain.slot, accounts_core.label(plain)) == (slot.plain_slot, slot.plain_label)
    assert (plain in accounts_core.list_accounts()) is (
        slot.legacy_slots or plain.config_dir.is_dir()
    )

    named = accounts_service.set_alias(str(account.slot), f"{adapter.id}-work")
    assert named.alias == f"{adapter.id}-work"
    assert accounts_service.set_disabled(str(account.slot), True).disabled
    accounts_service.set_disabled(str(account.slot), False)
    chosen = accounts_service.set_default(str(account.slot), agent=adapter.id)
    assert chosen is not None and chosen.slot == account.slot
    default = accounts_service.machine_default(adapter.id)
    assert default is not None and default.slot == account.slot
    keys = {accounts_service.project_account_key(a.id) for a in adapters() if a.accounts}
    assert len(keys) == len([a for a in adapters() if a.accounts]), "one project key each"

    env = accounts_core.launch_env(account)
    assert env[slot.home_env] == str(account.config_dir)
    assert all(env[name] == str(account.tmp_dir) for name in slot.extra_env)
    assert accounts_service.carried_vars(adapter.id)[1:] == (slot.home_env, *slot.extra_env)
    assert slot.launched_account(env, listed) == next(a for a in listed if a.slot == account.slot)

    described = accounts_service.describe(account)
    assert described.identity == slot.identity(account) and described.signed_in
    assert described.token_state == slot.token_state(account) == "ok"
    assert described.subscription == slot.subscription(account)
    assert adapter.layout_version(str(tmp_path / "nowhere" / adapter.binary)) is None

    with pytest.raises(accounts_service.AccountsError) as refused:
        accounts_service.remove(plain)
    assert f"sign out of it {slot.sign_out} instead" in str(refused.value)
    with pytest.raises(accounts_service.NoSuchAccount, match=f"no {slot.noun} account in slot 99"):
        accounts_service.resolve("99", agent=adapter.id)


def test_the_accounts_commands_name_an_agents_slots_in_its_own_words(
    adapter: AgentAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing in ``aisquare accounts`` names an agent by comparing it with Claude Code's id
    (B9): its slots are said in its accounts slot's words (``noun``, its label, its binary),
    and Claude Code's alone (``legacy_slots``) read bare, as they always did."""
    slot = _accounts_or_skip(adapter, monkeypatch)
    account = accounts_core.create_account(adapter.id)
    _sign_in(adapter, account, "login")
    runner = CliRunner()

    chosen = runner.invoke(app, ["accounts", "default", str(account.slot)])
    cleared = runner.invoke(app, ["accounts", "default", "--clear", "--agent", adapter.id])
    missing = runner.invoke(app, ["accounts", "default", "99", "--agent", adapter.id])

    assert chosen.exit_code == 0, chosen.output
    named = f" · {adapter.label}" in chosen.output
    assert named is not slot.legacy_slots, chosen.output
    assert cleared.exit_code == 0, cleared.output
    assert (f"cleared for {adapter.label}" in cleared.output) is not slot.legacy_slots
    assert f"no {slot.noun} account in slot 99" in missing.output


def test_a_reading_is_its_windows_and_the_one_that_limits_ranks_it() -> None:
    """S6: an account's usage is its windows (``ClaudeUsage.windows``, shortest first), each
    named and of its own length, with Claude Code's ``session`` and ``week`` as views of
    them; the pick, the agent rows and the info line go by the one that limits (the fullest
    read), and a reading whose shortest window went unread ranks nowhere."""
    from aisquare.models import ClaudeUsage, UsageWindow

    claude = ClaudeUsage(available=True, session_percent=12.0, week_percent=95.0)
    assert [(w.name, w.minutes) for w in claude.windows] == [("session", 300), ("week", 10080)]
    assert (claude.session_percent, claude.week_percent) == (12.0, 95.0)
    assert claude.limiting == claude.windows[1] and claude.ranked_percent == 95.0
    unread = ClaudeUsage(
        available=True,
        windows=(
            UsageWindow(name="session", minutes=300),
            UsageWindow(name="week", minutes=10080, used_percent=30),
        ),
    )
    assert unread.ranked_percent is None and unread.week_percent == 30
    daily = ClaudeUsage(
        available=True,
        windows=(
            UsageWindow(name="pro daily", minutes=1440, used_percent=70.0),
            UsageWindow(name="flash daily", minutes=1440, used_percent=10.0),
        ),
    )
    assert (daily.session_percent, daily.week_percent) == (None, None)
    assert daily.ranked_percent == 70.0
    account = ClaudeAccount(slot=5, config_dir=Path("/accounts/5"), agent="gemini-shaped")
    assert accounts_service.describe_headroom(account, daily) == (
        "account 5 pro daily 70% used (30% left)"
    )
    summary = accounts_service.fleet_headroom([account], {5: daily})
    assert summary.limit_by_slot == {5: 70.0} and summary.left_by_slot == {5: 30.0}
    dumped = ClaudeUsage.model_validate(claude.model_dump(mode="json"))
    assert dumped == claude and claude.model_dump()["session_used_percent"] == 12.0


def test_an_agent_without_a_reviewer_runs_auto_as_ask_and_the_receipt_says_so(
    adapter: AgentAdapter,
) -> None:
    """S4: ``permission_args`` answers ``PermissionArgs(args, notes)``. An agent with no
    reviewer of its own (``capabilities.approves_for_me`` False: the Gemini-shaped one, whose
    ``auto_edit`` approves edits unreviewed) runs the built-in ``workspace:auto`` as
    ``workspace:ask`` and its spawn's receipt says so, where it was refused for every role;
    one with a reviewer maps ``auto`` to it and says nothing."""
    for write in get_args(WriteScope):
        auto, ask = PermissionIntent(write, "auto"), PermissionIntent(write, "ask")
        try:
            granted = adapter.permission_args(auto, "coder")
        except ValueError:
            continue
        if adapter.capabilities.approves_for_me:
            assert granted.notes == (), (adapter.id, write)
        else:
            assert granted.args == adapter.permission_args(ask, "coder").args, write
            assert granted.notes == (
                f"{adapter.label} has no reviewer of its own: {auto} runs as {ask}",
            )
    receipt: list[str] = []
    fleet_service.permission_flags(adapter, "coder", "coder-1", FleetRoleSettings(), notes=receipt)
    assert bool(receipt) is not adapter.capabilities.approves_for_me, receipt


def test_the_receiver_keeps_what_the_agents_own_telemetry_reader_keeps(
    adapter: AgentAdapter, tmp_path: Path
) -> None:
    """B7: nothing in ``services.native_telemetry`` names Codex. An agent with native
    telemetry has a ``telemetry_reader``; the receiver parses OTLP itself and keeps of each
    log record what the reader decodes, an event name always, never a prompt or a header,
    and a record says the agent's own id. Gemini-shaped's reader reads its own attributes."""
    from aisquare.services import native_telemetry

    reader = adapter.telemetry_reader
    assert reader is not None or not adapter.capabilities.native_telemetry
    if reader is None:
        return
    secret = {"prompt": "DO_NOT_SPOOL", "authorization": "DO_NOT_SPOOL"}
    kept = reader.decode({**secret, "event.name": ""}, "an.event")
    assert kept is not None and kept["event.name"] == "an.event"
    assert not set(secret) & set(kept)
    assert reader.operator_configured(tmp_path, [], tmp_path) is False, "an empty home"
    assert reader.tool_shell_excludes(tmp_path, [], tmp_path) == []
    payload = {
        "resourceLogs": [
            {
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "eventName": "an.event",
                                "attributes": [
                                    {"key": key, "value": {"stringValue": value}}
                                    for key, value in secret.items()
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }
    [record] = native_telemetry.events(payload, reader)
    assert "DO_NOT_SPOOL" not in json.dumps(record) and record["event.name"] == "an.event"


def test_every_launch_slot_answers_a_launch_part_and_its_settings_reach_the_agents_layer(
    adapter: AgentAdapter, tmp_path: Path
) -> None:
    """S3: ``mcp_args``, ``label_args``, ``disable_native_teams``, ``fleet_settings`` and
    ``telemetry_args`` answer ``LaunchPart(args, env, settings)``, so an agent whose only
    seams are its variables and its settings files (the Gemini-shaped one) is no edit of the
    launch's; an adapter whose slots answer settings has a ``settings_layer`` that gives them
    to its session, and one that names a file names the one it wrote."""
    from aisquare.core.agent_adapters.types import LaunchPart, merged_settings

    parts = [
        adapter.mcp_args("/opt/aisquare", ["serve", "--stdio"], ["AISQUARE_HOME"]),
        adapter.label_args("coder-1"),
        adapter.disable_native_teams(),
        adapter.fleet_settings(tmp_path, []),
        adapter.telemetry_args("http://127.0.0.1:9/v1/logs"),
    ]
    for part in parts:
        assert isinstance(part, LaunchPart)
        assert all(isinstance(arg, str) for arg in part.args)
        assert all(isinstance(v, str) for v in part.env.values())
        json.dumps(part.settings)  # a settings document
    answered = merged_settings(*(part.settings for part in parts))
    file = tmp_path / "launch-settings" / "launch-1.json"
    file.parent.mkdir()
    layer = adapter.settings_layer(answered or {"aisquare": {"conformance": True}}, file)
    assert layer is not None or not answered, f"{adapter.id} answers settings with no layer"
    if layer is None:
        return
    assert layer, "a layer gives the session its settings"
    named = [*layer.args, *layer.env.values()]
    if str(file) in named:
        assert json.loads(file.read_text("utf-8")) == (
            answered or {"aisquare": {"conformance": True}}
        )
    else:
        assert not file.exists(), "a layer that names no file writes none"


def test_a_transcripts_records_are_its_lines_newest_first_each_at_its_offset(
    adapter: AgentAdapter,
) -> None:
    """S5: ``AgentTranscript.records`` turns the lines a walk read (newest first, each at its
    offset) into the agent's records, at those offsets: one a line by default
    (``transcripts.one_per_line``), ``None`` for a line that is no record of its own."""
    reader = adapter.transcript_reader
    if reader is None:
        return
    lines = [(30, b'{"type": "x", "n": 2}'), (12, b"not json"), (0, b'{"type": "x", "n": 1}')]
    read = list(reader.records(iter(lines)))
    assert [offset for offset, _ in read] == [30, 12, 0]
    assert read[1][1] is None
    assert [record for _, record in read if record is not None] == [
        {"type": "x", "n": 2},
        {"type": "x", "n": 1},
    ]


def test_the_screen_is_one_optional_reader_of_nine_parts() -> None:
    """``AgentAdapter.screen``: the screen-reading capabilities are one ``AgentScreen``, each
    of its nine parts optional, and none of them on ``AgentCapabilities`` any more."""
    from dataclasses import fields

    from aisquare.core.agent_adapters.types import AgentCapabilities, AgentScreen

    parts = {
        "idle_prompt_mark",
        "idle_on_screen",
        "working_on_screen",
        "dialog_on_screen",
        "start_dialog_mark",
        "screen_prompt",
        "ready_on_screen",
        "modal_on_screen",
        "prompt_on_screen",
    }
    assert {f.name for f in fields(AgentScreen)} == parts
    assert all(f.default is None for f in fields(AgentScreen))
    assert not parts & {f.name for f in fields(AgentCapabilities)}


def test_an_agent_that_declares_no_drawn_prompt_reads_it_off_its_empty_prompt() -> None:
    """The default an adapter gets with no ``prompt_on_screen`` (the fake's): its empty prompt
    (``idle_on_screen``, else ``idle_prompt_mark``), a turn running at it or not."""
    plain = screen_of(FakeAgentAdapter())
    assert plain.prompt_on_screen is None and not plain.drawn(["done", "fake> "])
    marked = replace(plain, idle_prompt_mark="fake>")
    assert marked.drawn(["running it", "fake> "]), "busy, its prompt still drawn"
    assert not marked.drawn(["?? Allow shell: make deploy", "(y/n)"])


def test_an_agent_with_no_screen_still_runs_and_is_never_typed_into_on_its_screens_word(
    gemini_shaped: GeminiShapedAdapter,
) -> None:
    """An adapter that declares no screen (the Gemini-shaped one: its screens are unmeasured)
    reads as ``NO_SCREEN``: never at its prompt, never ready, nothing drawn, nothing modal,
    no empty prompt the fleet or Remote could read."""
    from aisquare.core.agent_adapters.types import NO_SCREEN

    assert gemini_shaped.screen is None and screen_of(gemini_shaped) is NO_SCREEN
    rows = ["> ", "? for shortcuts"]
    screen = screen_of(gemini_shaped)
    assert not (screen.at_empty_prompt(rows) or screen.ready(rows) or screen.drawn(rows))
    assert screen.modal(rows) is None and not screen.reads_empty_prompt


def test_the_guide_names_every_member_of_the_protocol() -> None:
    """docs/coding-agents.md, "Adding a terminal agent", names every member of the adapter
    protocol's classes (an inline code span holds it), so a slot added is a slot documented:
    the guide-vs-code check the lanes ran by hand, kept."""
    import ast
    import re

    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "src/aisquare/core/agent_adapters/types.py").read_text("utf-8"))
    guide = (root / "docs/coding-agents.md").read_text("utf-8")
    spans = re.findall(r"`([^`\n]+)`", guide)
    named = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", " ".join(spans)))
    classes = {
        "AgentAdapter",
        "AgentCapabilities",
        "AgentScreen",
        "AgentAccounts",
        "AgentTranscript",
        "TelemetryReader",
        "AgentPlugin",
        "FolderTrust",
        "LaunchPart",
        "PermissionArgs",
    }
    missing = []
    for node in tree.body:
        if not (isinstance(node, ast.ClassDef) and node.name in classes):
            continue
        members = [node.name]
        for item in node.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                members.append(item.target.id)
            elif isinstance(item, ast.FunctionDef) and not item.name.startswith("_"):
                members.append(item.name)
        missing += [member for member in members if member not in named]
    assert not missing, f"name these in docs/coding-agents.md: {missing}"
