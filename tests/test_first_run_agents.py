"""Welcome's step 2 and 3 for any agent: which one the fleet starts, and what step 2 reads.

``services.first_run`` for Codex: the agent the manager would start on
(``fleet_agent``), its four facts read from a real temporary ``CODEX_HOME``
(installed, signed in, connected, trusted), Connect as the doctor's own fix, the
sign-in slot, and ``start_on_agent`` passing the agent to ``fleet.spawn`` and saving
an agent no one chose. No test reads the real ``PATH``: ``which`` is scripted.
Each behaviour has its control (Claude Code's answer unchanged, or the other state).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from aisquare.cli.ui.views.welcome import claude_text
from aisquare.core import agents as agent_core
from aisquare.core import claude_accounts as accounts_core
from aisquare.core.agent_adapters import codex as codex_adapter
from aisquare.core.config import RoleLaunchProfile, load_config, save_config
from aisquare.core.selfcli import CliResult
from aisquare.models import ProjectInfo
from aisquare.services import agent_launch, first_run
from aisquare.services import agents as agents_service
from aisquare.services import claude_accounts as accounts_service
from tests.test_codex_accounts import _sign_in
from tests.test_codex_plugin_route import install as plugin_install
from tests.test_first_run import Spawns, admitted

REPO = Path(__file__).resolve().parents[1]


def on_path(*names: str) -> Callable[[str], str | None]:
    """A ``which`` that finds exactly ``names``, at made-up paths."""
    return lambda name: f"/opt/bin/{name}" if name in names else None


@pytest.fixture
def codex_home(isolated_agent_home: Path) -> Path:
    """The plain ``codex``'s home (``CODEX_HOME`` is unset in the suite): ``~/.codex``."""
    return isolated_agent_home / ".codex"


def _connect(home: Path) -> None:
    """``agents connect codex`` on a home Codex already made: connect makes a missing one
    only where ``codex`` is on PATH, and these tests must not depend on that PATH."""
    home.mkdir(parents=True, exist_ok=True)
    agents_service.connect("codex", home)


@pytest.fixture(autouse=True)
def codex_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """``codex --version`` as 0.162.1 prints it (the suite answers every probe as aisquare)."""
    monkeypatch.setattr(agent_core, "hook_binary_version", lambda argv, **_k: "0.162.1")


# --------------------------------------------------------------------------- which agent


def test_a_codex_only_machine_is_offered_codex_and_marked_unchosen() -> None:
    choice = first_run.fleet_agent(which=on_path("codex"))
    assert choice.agent == "codex" and choice.inferred and choice.installed == ("codex",)
    # Control: with Claude Code here too, nothing is inferred; the default stands.
    both = first_run.fleet_agent(which=on_path("claude", "codex"))
    assert both.agent == "claude-code" and not both.inferred and both.choosable
    assert both.installed == ("claude-code", "codex")


def test_with_neither_installed_the_default_stands_and_nothing_is_inferred() -> None:
    choice = first_run.fleet_agent(which=on_path())
    assert choice.agent == "claude-code" and not choice.inferred and choice.installed == ()
    state = first_run.probe_fleet(which=on_path())
    assert state.choosable and not state.found and state.installed == ()


def test_a_saved_choice_wins_and_only_a_user_default_is_choosable(tmp_path: Path) -> None:
    agent_launch.use("codex")
    user = first_run.fleet_agent(tmp_path, which=on_path("claude", "codex"))
    assert (user.agent, user.source, user.choosable) == ("codex", "user", True)
    config = load_config()
    config.team.profiles["manager"] = RoleLaunchProfile(agent="claude-code")
    save_config(config)
    bound = first_run.fleet_agent(tmp_path, which=on_path("claude", "codex"))
    assert (bound.agent, bound.source, bound.choosable) == ("claude-code", "role", False)


def test_others_offers_each_installed_agent_only_where_a_pick_is_saved() -> None:
    both = first_run.probe_fleet(which=on_path("claude", "codex"), sign_in=False)
    assert both.others == ("claude-code", "codex")  # nothing saved yet: either pick is saved
    agent_launch.use("codex")
    saved = first_run.probe_fleet(which=on_path("claude", "codex"), sign_in=False)
    assert saved.agent == "codex" and saved.others == ("claude-code",)
    alone = first_run.probe_fleet(which=on_path("codex"), sign_in=False)
    assert alone.others == ()  # one agent here: nothing to choose between


# --------------------------------------------------------------------------- Codex's facts


def test_codex_installed_and_unconnected_offers_connect(codex_home: Path) -> None:
    state = first_run.probe_fleet(which=on_path("codex"))
    assert state.agent == "codex" and state.label == "Codex" and state.found
    assert state.version == "0.162.1" and state.binary == "/opt/bin/codex"
    assert state.home == codex_home and not state.connected and not state.ready
    assert state.signed_in is False and state.account_slot == 0
    assert state.switch is None and state.refused is None and not state.trust_pending


def test_connected_codex_is_ready_with_its_trust_step_pending(codex_home: Path) -> None:
    _connect(codex_home)
    pending = first_run.probe_fleet(which=on_path("codex"))
    assert pending.connected and pending.ready and pending.trust_pending
    # Once a hook ran this definition, the trust step is done.
    agent_core.observe_hooks("codex", codex_home, agent_core.hook_fingerprint("codex", codex_home))
    trusted = first_run.probe_fleet(which=on_path("codex"))
    assert trusted.connected and not trusted.trust_pending


def test_hooks_switched_off_are_named_and_never_called_connected(codex_home: Path) -> None:
    _connect(codex_home)
    (codex_home / "config.toml").write_text("[features]\nhooks = false\n", encoding="utf-8")
    state = first_run.probe_fleet(which=on_path("codex"))
    assert not state.connected and state.switched_off
    assert state.switch is not None and "hooks = false" in state.switch
    assert str(codex_home / "config.toml") in state.switch


def test_a_partial_install_is_not_connected(codex_home: Path) -> None:
    _connect(codex_home)
    hooks = codex_home / "hooks.json"
    text = hooks.read_text(encoding="utf-8")
    import json

    data = json.loads(text)
    data["hooks"].pop("Stop")
    hooks.write_text(json.dumps(data), encoding="utf-8")
    assert not first_run.probe_fleet(which=on_path("codex")).connected


def test_signed_in_is_the_managers_accounts_login(codex_home: Path) -> None:
    _sign_in(codex_home, "dev@example.com")
    state = first_run.probe_fleet(which=on_path("codex"))
    assert state.signed_in is True and state.account_slot == 0
    assert state.account == accounts_core.label(accounts_core.default_account(agent="codex"))
    # The periodic look leaves the login out.
    assert first_run.probe_fleet(which=on_path("codex"), sign_in=False).signed_in is None


def test_the_hooks_are_read_in_the_home_of_the_account_the_manager_runs_on(
    codex_home: Path,
) -> None:
    """t8 smoke: with the machine's Codex default on slot 2, step 2 read the plain ~/.codex
    and said connected while slot 2, where the manager starts, had no hooks at all."""
    _connect(codex_home)
    slot = accounts_core.create_account("codex")
    _sign_in(slot.config_dir, "dev@example.com")
    accounts_service.set_default(str(slot.slot))

    bare = first_run.probe_fleet(which=on_path("codex"))
    assert bare.home == slot.config_dir and not bare.connected and not bare.ready
    assert bare.signed_in is True and bare.account_slot == slot.slot
    step_two = claude_text(bare, platform="linux").plain
    assert f"not connected — Connect installs aisquare's hooks in {slot.config_dir}" in step_two
    assert first_run.connect_fix("codex", bare.home).argv[-2:] == (
        "--config-dir",
        str(slot.config_dir),
    )
    (slot.config_dir / "config.toml").write_text("[features]\nhooks = false\n", encoding="utf-8")
    off = first_run.probe_fleet(which=on_path("codex"), sign_in=False)
    assert off.switch is not None and str(slot.config_dir / "config.toml") in off.switch

    (slot.config_dir / "config.toml").unlink()
    _connect(slot.config_dir)
    hooked = first_run.probe_fleet(which=on_path("codex"), sign_in=False)
    assert hooked.home == slot.config_dir and hooked.connected and hooked.trust_pending
    assert f"aisquare's hooks in {slot.config_dir}" in claude_text(hooked, platform="linux").plain
    # Control: with no Codex default, the manager runs on the plain home, read as before,
    # where a launch with nothing named takes the default (`pick = "default"`). 0.8.1's own
    # default, `headroom`, sends it to the signed-in slot that has room, slot 2 here.
    agents_service.disconnect("codex", slot.config_dir)
    accounts_service.set_default(None, agent="codex")
    room = first_run.probe_fleet(which=on_path("codex"))
    assert room.home == slot.config_dir and room.account_slot == slot.slot
    config = load_config()
    config.accounts.pick = "default"
    save_config(config)
    plain = first_run.probe_fleet(which=on_path("codex"))
    assert plain.home == codex_home and plain.connected and plain.account_slot == 0


def test_claude_code_is_still_probed_its_own_way() -> None:
    state = first_run.probe_fleet(which=on_path("claude"), sign_in=False)
    assert state.agent == "claude-code" and state.label == "Claude Code"
    assert state.home is None and not state.trust_pending and state.switch is None


# --------------------------------------------------------------------------- install routes


def test_codex_install_routes_name_its_own_installer_first() -> None:
    linux = first_run.install_routes("linux", agent="codex")
    assert [route.command for route in linux] == [
        codex_adapter.INSTALL_COMMAND,
        "npm install -g @openai/codex",
    ]
    mac = first_run.install_routes("darwin", agent="codex")
    assert codex_adapter.BREW_COMMAND in [route.command for route in mac]
    windows = first_run.install_routes("win32", agent="codex")
    assert [route.how for route in windows] == ["inside WSL2"]
    # Control: Claude Code's routes are unchanged.
    assert first_run.install_routes("linux") == first_run.install_routes(
        "linux", agent="claude-code"
    )


def test_the_codex_installer_is_the_one_install_sh_runs() -> None:
    script = (REPO / "install.sh").read_text(encoding="utf-8")
    url = re.search(r'^CODEX_INSTALLER_URL="([^"]+)"', script, re.MULTILINE)
    assert url is not None and url.group(1) in codex_adapter.INSTALL_COMMAND


# --------------------------------------------------------------------------- Connect, sign-in


class Runs:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: Any, *, cwd: Path | None = None) -> CliResult:
        self.calls.append(list(args))
        return CliResult(argv=list(args), returncode=0, stdout="{}", stderr="")


def test_connect_is_the_doctors_fix_for_the_agent_step_2_shows(
    codex_home: Path, tmp_path: Path
) -> None:
    state = first_run.probe_fleet(which=on_path("codex"))
    run = Runs()
    assert first_run.connect(run=run, state=state).ok
    assert run.calls == [["--json", "agents", "connect", "codex"]]
    # Another home than the shell's is named, and the doctor's table admits it.
    elsewhere = first_run.connect_fix("codex", tmp_path / "elsewhere")
    assert elsewhere.argv[-2:] == ("--config-dir", str(tmp_path / "elsewhere"))
    assert admitted(elsewhere)
    # Control: Claude Code's Connect is what it was.
    assert first_run.connect_fix().argv == first_run.CONNECT_ARGV


def test_sign_in_opens_the_plain_home_making_it_if_codex_never_ran(codex_home: Path) -> None:
    state = first_run.probe_fleet(which=on_path("codex"))
    assert not codex_home.exists()
    slot = first_run.sign_in_slot(state)
    assert slot == 0 and codex_home.is_dir()
    assert accounts_core.find_account(0) is not None  # the Accounts page lists it now


# --------------------------------------------------------------------------- step 3


def _project(root: Path) -> ProjectInfo:
    return ProjectInfo(id="prj_demo", root=root)


def _start(
    root: Path, which: Callable[[str], str | None], monkeypatch: pytest.MonkeyPatch
) -> Spawns:
    spawns = Spawns()
    real = first_run.start_fleet

    def scripted(project: ProjectInfo, **kwargs: Any) -> first_run.FleetStart:
        return real(project, spawn=spawns, live=lambda p: [], **kwargs)

    monkeypatch.setattr(first_run, "start_fleet", scripted)
    first_run.start_on_agent(_project(root), which=which)
    return spawns


def test_the_fleet_starts_on_codex_and_saves_it_when_it_was_the_only_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawns = _start(tmp_path, on_path("codex"), monkeypatch)
    assert [(role, kwargs.get("agent")) for role, kwargs in spawns.calls] == [
        ("manager", "codex"),
        ("coder", "codex"),
        ("coder", "codex"),
    ]
    assert all("prompt" not in kwargs for _role, kwargs in spawns.calls)
    assert load_config().agents.default == "codex"  # the manager's own spawns land on it


def test_a_role_bound_to_its_own_agent_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_launch.use("codex")
    config = load_config()
    config.team.profiles["coder"] = RoleLaunchProfile(agent="claude-code")
    save_config(config)
    spawns = _start(tmp_path, on_path("claude", "codex"), monkeypatch)
    agents = [(role, kwargs.get("agent")) for role, kwargs in spawns.calls]
    assert agents[0] == ("manager", "codex")
    assert all(agent is None for role, agent in agents if role == "coder"), agents


@pytest.mark.parametrize("binding", ["config", "role-env"])
def test_a_role_bound_to_its_own_executable_keeps_its_family(
    binding: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_launch.use("codex")
    if binding == "config":
        config = load_config()
        config.team.profiles["coder"] = RoleLaunchProfile(bin="claude")
        save_config(config)
    else:
        monkeypatch.setenv("AISQUARE_BIN_CODER", "claude")
    spawns = _start(tmp_path, on_path("claude", "codex"), monkeypatch)
    agents = [(role, kwargs.get("agent")) for role, kwargs in spawns.calls]
    assert agents[0] == ("manager", "codex")
    assert all(agent is None for role, agent in agents if role == "coder"), agents
    # So fleet.spawn resolves the coder from its binding, as Claude Code's.
    assert agent_launch.resolve("coder", cwd=tmp_path).adapter.id == "claude-code"


def test_claude_codes_fleet_saves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spawns = _start(tmp_path, on_path("claude", "codex"), monkeypatch)
    assert {kwargs.get("agent") for _role, kwargs in spawns.calls} == {"claude-code"}
    assert load_config().agents.default is None


@pytest.mark.skipif(sys.platform == "win32", reason="the plugin route runs sh")
def test_a_plugin_only_home_is_connected_through_it_and_never_offered_connect(
    codex_home: Path,
) -> None:
    """The aisquare Codex plugin is a route of its own: Connect beside it would install
    hooks.json's as a second one. Its hooks wait for the same /hooks trust."""
    plugin_install(codex_home)
    untrusted = first_run.probe_fleet(which=on_path("codex"))
    assert untrusted.connected and untrusted.trust_pending and untrusted.ready
    plugin_install(codex_home, trusted=None)
    trusted = first_run.probe_fleet(which=on_path("codex"))
    assert trusted.connected and not trusted.trust_pending
