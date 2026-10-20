"""The adapter protocol, held for every registered adapter and a synthetic third one.

Parametrized over ``agent_adapters.adapters()`` plus :class:`tests.fake_agent.FakeAgentAdapter`,
which is registered for this module only. The fake speaks a native vocabulary unlike Claude
Code's and Codex's, so a shared service that special-cases either of them, or reads a payload
or writes a flag itself instead of asking the adapter, fails here for the fake.

What a new adapter must bring for this suite to pass (docs/coding-agents.md, "Adding a
terminal agent"): a recorded-payload fixture for every hook event it installs
(``tests/fixtures/agent_events/<id>.json``, with its ``--version`` line and what switches
its hooks off), a row in :data:`EXPECTED_PERMISSIONS`, and its transcript at four moments
(``tests/fixtures/agent_transcripts/<id>.json``: a permission prompt up, a question, an
interruption, a turn ended on a question) for its ``transcript_reader``; and with
``capabilities.dialog_on_screen``, its screens (``tests/fixtures/agent_screens/<id>.json``:
each dialog of its own with the words it reads, and screens that show none).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from collections.abc import Iterator
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
from aisquare.core.agent_adapters.types import (
    EVENT_KINDS,
    AgentAccounts,
    AgentAdapter,
    Approvals,
    PermissionIntent,
    PluginInstall,
    WriteScope,
    executable_name,
    hook_args,
    store_dir,
    store_read_only,
    trusted_by,
    version_in,
)
from aisquare.core.config import FleetRoleSettings, load_config, save_config
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import ClaudeAccount
from aisquare.services import agent_events, store_broker
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import fleet as fleet_service
from tests import fakebin
from tests.fake_agent import FakeAgentAdapter
from tests.test_fleet_service import FakeTmux

FIXTURES = Path(__file__).parent / "fixtures" / "agent_events"
TRANSCRIPTS = Path(__file__).parent / "fixtures" / "agent_transcripts"
SCREENS = Path(__file__).parent / "fixtures" / "agent_screens"
REGISTERED = tuple(adapter.id for adapter in adapters())
ALL = (*REGISTERED, FakeAgentAdapter.id)

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
        intent = PermissionIntent.for_role(role)
        assert adapter.permission_args(intent, role) == _expected(adapter, intent, flags)
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
                args = adapter.permission_args(intent, "coder")
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
                args = adapter.permission_args(PermissionIntent(write, approvals), "coder")
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
            args = adapter.permission_args(PermissionIntent(write, "never"), "coder")
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
    assert any("/opt/aisquare" in arg for arg in mcp)
    files = adapter.context_files(tmp_path)
    assert files and all(path.parent == tmp_path for path in files)
    args, env = adapter.disable_native_teams()
    assert args or env, "the fleet keeps the agent's own sub-agent teams off"
    telemetry = adapter.telemetry_args("http://127.0.0.1:9/v1/logs")
    if adapter.capabilities.native_telemetry:
        assert any("127.0.0.1:9" in arg for arg in telemetry)
    else:
        assert telemetry == []


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
        fake_agent, "capabilities", replace(fake_agent.capabilities, idle_prompt_mark="fake>")
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
    path = TRANSCRIPTS / f"{adapter.id}.json"
    assert path.is_file(), f"add {path.relative_to(TRANSCRIPTS.parents[1])} for {adapter.id}"
    moments: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))["moments"]
    assert set(moments) == {"permission", "question", "interrupted", "asked"}
    return moments


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
    recorded = _moments(adapter)[moment]
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
    own with the words it reads, and screens with none. ``None`` for an adapter that reads no
    dialog off its screen, which brings none."""
    path = SCREENS / f"{adapter.id}.json"
    if adapter.capabilities.dialog_on_screen is None:
        assert not path.exists(), f"{adapter.id} reads no dialog off its screen: {path.name}"
        return None
    assert path.is_file(), f"add {path.relative_to(SCREENS.parents[1])} for {adapter.id}"
    screens: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    assert screens["dialogs"] and screens["not_dialogs"]
    return screens


def test_an_agent_reads_its_own_dialogs_off_its_screen(adapter: AgentAdapter) -> None:
    """``capabilities.dialog_on_screen`` on the agent's recorded screens: each dialog's words,
    and ``None`` for a screen with no dialog on it (its empty prompt, a draft, a turn)."""
    screens = _screens(adapter)
    if screens is None:
        return
    read = adapter.capabilities.dialog_on_screen
    assert read is not None
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
    if screens is None:
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
