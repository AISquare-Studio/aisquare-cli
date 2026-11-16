"""The Codex plugin behind the adapter's plugin slot: what Codex installed, whether it is
trusted, the command that removes it, and the route each Codex home takes.

``codex plugin add aisquare@aisquare-cli`` (Codex 0.162.1, measured) copies the plugin to
``$CODEX_HOME/plugins/cache/aisquare-cli/aisquare/<version>/``, writes
``[plugins."aisquare@aisquare-cli"] enabled = true`` into ``config.toml``, and runs none of
its hooks until ``/hooks`` trusts them, which it records per hook in
``[hooks.state."aisquare@aisquare-cli:hooks/hooks.json:<event>:0:0"] trusted_hash``. The
fixtures below write the same, from the real ``plugins/codex``.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
import tomli_w
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import agents as agent_core
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.agent_adapters.codex import PLUGIN, PLUGIN_HOOKS, PLUGIN_MARKETPLACE
from tests.test_claude_plugin import Machine, posix_only
from tests.test_codex_plugin import _hooks_json, _program, _run, machine  # noqa: F401

REPO = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO / "plugins" / "codex"
HOOKS_JSON = PLUGIN_DIR / PLUGIN_HOOKS
_EVENTS = [spec.event for spec in get_adapter("codex").capabilities.hooks]

posix_route = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the plugin route runs sh: native Windows reads only the hooks.json route",
)


def _version() -> str:
    version: str = json.loads((PLUGIN_DIR / ".codex-plugin" / "plugin.json").read_text("utf-8"))[
        "version"
    ]
    return version


def _snake(event: str) -> str:
    """Codex's key for an event in ``[hooks.state]``: SessionStart is ``session_start``
    and UserPromptSubmit ``user_prompt_submit`` (measured)."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower()


def _trust_keys(plugin_id: str) -> list[str]:
    """The ``[hooks.state]`` key of every hook in the plugin's hooks.json, by its group and
    handler indexes, as ``/hooks`` writes them."""
    hooks = json.loads(HOOKS_JSON.read_text("utf-8"))["hooks"]
    return [
        f"{plugin_id}:{PLUGIN_HOOKS}:{_snake(event)}:{g}:{h}"
        for event, groups in hooks.items()
        for g, group in enumerate(groups)
        for h, _ in enumerate(group["hooks"])
    ]


def install(
    home: Path,
    *,
    enabled: bool | None = True,
    installed: bool = True,
    trusted: int | None = 0,
    marketplace: str = PLUGIN_MARKETPLACE,
) -> str:
    """What ``codex plugin add aisquare@<marketplace>`` leaves in ``home``: the plugin copied
    into the cache under its version, and its table in config.toml. ``trusted``: how many
    of its hooks ``/hooks`` trusted, ``None`` for all."""
    plugin_id = f"{PLUGIN}@{marketplace}"
    home.mkdir(parents=True, exist_ok=True)
    table = {} if enabled is None else {"enabled": enabled}
    config: dict[str, Any] = {"plugins": {plugin_id: table}}
    keys = _trust_keys(plugin_id)
    keys = keys if trusted is None else keys[:trusted]
    if keys:
        config["hooks"] = {"state": {key: {"trusted_hash": "sha256:0f"} for key in keys}}
    (home / "config.toml").write_text(tomli_w.dumps(config), encoding="utf-8")
    if installed:
        cache = home / "plugins" / "cache" / marketplace / PLUGIN / _version()
        shutil.copytree(PLUGIN_DIR, cache, dirs_exist_ok=True)
    return plugin_id


@pytest.fixture(autouse=True)
def no_codex_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex reads ~/.codex here, under the suite's agent home, whatever this shell names."""
    monkeypatch.delenv("CODEX_HOME", raising=False)


@pytest.fixture
def codex(isolated_agent_home: Path) -> Path:
    """Codex's home, ~/.codex, with nothing of aisquare's in it yet."""
    home = isolated_agent_home / ".codex"
    home.mkdir(parents=True)
    return home


def test_the_names_are_the_manifests() -> None:
    market = json.loads((REPO / ".agents" / "plugins" / "marketplace.json").read_text("utf-8"))
    assert (market["plugins"][0]["name"], market["name"]) == (PLUGIN, PLUGIN_MARKETPLACE)
    assert PLUGIN == agent_core.CLAUDE_PLUGIN, "the Claude Code plugin's name: one for both"


def test_the_trust_keys_are_the_ones_codex_wrote() -> None:
    """The fixtures' keys, from the real hooks.json, are those Codex 0.162.1 wrote for it."""
    keys = _trust_keys(f"{PLUGIN}@{PLUGIN_MARKETPLACE}")
    assert "aisquare@aisquare-cli:hooks/hooks.json:session_start:0:0" in keys
    assert "aisquare@aisquare-cli:hooks/hooks.json:user_prompt_submit:0:0" in keys
    assert len(keys) == len(_EVENTS)


@pytest.mark.parametrize(
    ("setup", "trusted"),
    [
        ({}, False),
        ({"trusted": None}, True),
        ({"trusted": len(_EVENTS) - 1}, False),
        ({"enabled": None, "trusted": None}, True),
        ({"marketplace": "my-fork"}, False),
        ({"enabled": False}, None),
        ({"installed": False}, None),
    ],
    ids=["untrusted", "trusted", "one-untrusted", "no-enabled-key", "fork", "disabled", "gone"],
)
def test_the_adapter_reads_the_plugin_codex_installed(
    codex: Path, setup: dict[str, Any], trusted: bool | None
) -> None:
    """Enabled in config.toml AND in the cache, as Claude Code's is enabled AND recorded:
    an enabled table with nothing installed runs nothing, and `enabled = false` (the
    `/plugins` toggle) runs nothing either."""
    plugin = get_adapter("codex").plugin
    assert plugin is not None
    install(codex, **setup)

    found = plugin.installed(codex)

    if trusted is None:
        assert found is None
        return
    assert found is not None
    assert (found.config_dir, found.version, found.trusted) == (codex, _version(), trusted)
    assert (found.scope, found.project) == ("user", None), "a Codex plugin has no other scope"


@pytest.mark.parametrize(
    "config",
    [
        "",
        "[plugins]\n",
        'plugins = "aisquare@aisquare-cli"\n',
        '[plugins."other@aisquare-cli"]\nenabled = true\n',
        '[plugins."aisquare@../escape"]\nenabled = true\n',
        "not toml [",
    ],
    ids=["empty", "no-tables", "a-string", "another-plugin", "a-path", "unreadable"],
)
def test_nothing_else_reads_as_the_plugin(codex: Path, config: str) -> None:
    plugin = get_adapter("codex").plugin
    assert plugin is not None
    install(codex)
    (codex / "config.toml").write_text(config, encoding="utf-8")

    assert plugin.installed(codex) is None


def test_none_reads_the_home_a_session_from_this_shell_reads(
    codex: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = get_adapter("codex").plugin
    assert plugin is not None
    other = tmp_path / "codex-2"
    install(other, trusted=None)

    assert plugin.installed(None) is None, "~/.codex has none"
    monkeypatch.setenv("CODEX_HOME", str(other))
    found = plugin.installed(None)
    assert found is not None and found.config_dir == other.resolve()


def test_a_codex_plugin_is_installed_in_a_home_alone(codex: Path) -> None:
    plugin = get_adapter("codex").plugin
    assert plugin is not None
    install(codex)

    assert plugin.repo_installs(codex) == []
    assert plugin.repo_install_here(codex, codex) is None


@pytest.mark.parametrize("verb", ["remove", "uninstall", "disable"])
def test_its_command_is_codex_plugin_remove_as_uninstall_names_it(
    codex: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
) -> None:
    """Codex has no disable or uninstall verb; each is `codex plugin remove`, aimed at the
    home as `aisquare uninstall` aims it, with the id that home enabled."""
    plugin = get_adapter("codex").plugin
    assert plugin is not None
    fork = tmp_path / "codex-fork"
    install(codex)
    install(fork, marketplace="my-fork")

    here = plugin.command(verb, codex)
    there = plugin.command(verb, fork)
    monkeypatch.setenv("CODEX_HOME", str(fork))
    default_from_elsewhere = plugin.command(verb, codex)

    assert here == "codex plugin remove aisquare@aisquare-cli"
    if sys.platform != "win32":
        assert there == f"CODEX_HOME={fork} codex plugin remove aisquare@my-fork"
        assert (
            default_from_elsewhere == "env -u CODEX_HOME codex plugin remove aisquare@aisquare-cli"
        )
    monkeypatch.delenv("CODEX_HOME")
    for home, plugin_id in ((codex, "aisquare@aisquare-cli"), (fork, "aisquare@my-fork")):
        # What uninstall names for it (the adapter's `lasting_installs`).
        [left] = [e for e in get_adapter("codex").lasting_installs(home) if e.kind == "plugin"]
        assert left.name == plugin_id and plugin.command(verb, home) == left.remove


def test_native_windows_has_no_codex_plugin_route(
    codex: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launcher runs sh, which Codex on Windows has no reason to find."""
    install(codex, trusted=None)
    monkeypatch.setattr(agent_core, "plugin_route_supported", lambda: False)

    assert agent_core.plugin_of("codex") is None
    assert agent_core.hook_route("codex", codex) is None


@posix_route
def test_the_route_is_named_for_each_way_in(codex: Path) -> None:
    assert agent_core.hook_route("codex", codex) is None
    install(codex)
    assert agent_core.hook_route("codex", codex) == agent_core.ROUTE_PLUGIN
    assert agent_core.install_hooks("codex", codex)
    assert agent_core.hook_route("codex", codex) == agent_core.ROUTE_BOTH
    (codex / "config.toml").unlink()
    assert agent_core.hook_route("codex", codex) == agent_core.ROUTE_HOOKS


@posix_route
def test_agents_status_reports_the_route(runner: CliRunner, codex: Path) -> None:
    install(codex, trusted=None)

    def status() -> dict[str, Any]:
        listed = runner.invoke(app, ["--json", "agents", "status", "codex"]).stdout
        found: dict[str, Any] = json.loads(listed)[0]
        return found

    alone = status()
    assert agent_core.install_hooks("codex", codex)
    both = status()
    table = runner.invoke(app, ["agents", "status", "codex"], env={"COLUMNS": "200"})

    assert alone["route"] == "plugin"
    assert both["route"] == "both"
    assert (
        f"aisquare runs two ways here: the hooks.json hooks and the aisquare plugin "
        f"{_version()}, whose hooks stand down beside them" in both["detail"]
    )
    assert "two ways" in table.stdout


def test_claude_code_reports_its_route_too(isolated_agent_home: Path) -> None:
    """The route is the adapter's, not Codex's: Claude Code's settings.json hooks are one."""
    claude = isolated_agent_home / ".claude"
    claude.mkdir(parents=True)
    assert agent_core.hook_route("claude-code", claude) is None
    assert agent_core.install_hooks("claude-code", claude)
    assert agent_core.hook_route("claude-code", claude) == agent_core.ROUTE_HOOKS


# ------------------------------------------------------- the launcher's rule, read here


@posix_only
@pytest.mark.parametrize("program", ["kept", "gone"])
def test_the_launcher_and_plugin_stands_down_agree(machine: Machine, program: str) -> None:  # noqa: F811
    """`core.agents.plugin_stands_down` is the launcher's rule, as the doctor reads it:
    beside a hooks.json hook whose aisquare can start it stands down, beside one whose
    aisquare is gone it runs."""
    machine.fake("aisquare")
    codex = machine.home / ".codex"
    named = _program(machine) if program == "kept" else machine.home / "gone" / "aisquare"
    _hooks_json(codex, f"{named} hook codex --definition 4f2a")

    _run(machine, "Stop")

    ran = machine.ran("aisquare") is not None
    assert ran == (program == "gone")
    assert agent_core.plugin_stands_down("codex", codex) == (program == "kept")
