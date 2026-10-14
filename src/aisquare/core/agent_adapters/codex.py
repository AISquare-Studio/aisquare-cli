"""Codex CLI 0.162.1 integration; native hooks keep the real terminal UI."""

import json
import os
import re
import shlex
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from aisquare.core import harness
from aisquare.core.agent_adapters.types import (
    AgentCapabilities,
    BadEffortError,
    HookSpec,
    PluginInstall,
    option_values,
)
from aisquare.core.codex_accounts import CodexAccounts

#: ``model_reasoning_effort`` levels Codex names (0.162.1). Which of them a model
#: takes is Codex's to say, so another level is passed through with a note.
REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
#: The one shared effort name Codex lacks: ``ultracode`` is Claude Code's ``max``
#: plus workflow orchestration, of which Codex takes the level alone.
EFFORT_ALIASES = {"ultracode": "max"}
#: ``approval_policy`` values the CLI takes: ``untrusted`` is gone, ``on-failure``
#: deprecated, and the granular table is config-only.
APPROVAL_POLICIES = ("on-request", "never")
#: Options that settle the sandbox or approvals, which ``--approve-for-me`` cannot
#: be combined with (Codex refuses the launch).
_APPROVAL_SWITCHES = frozenset(
    {"--approve-for-me", "--dangerously-bypass-approvals-and-sandbox", "--yolo"}
)
#: aisquare's Codex plugin (``plugins/codex``), by the name ``codex plugin add
#: aisquare@<marketplace>`` takes and ``config.toml`` keys it under, ``[plugins."aisquare@
#: aisquare-cli"]`` from this repository's marketplace (``.agents/plugins/marketplace.json``).
PLUGIN = "aisquare"
PLUGIN_MARKETPLACE = "aisquare-cli"
#: Where in the plugin Codex reads its hooks, which keys their trust (``[hooks.state]``).
PLUGIN_HOOKS = "hooks/hooks.json"
#: ``codex plugin`` has add, list and remove: no disable (``enabled = false`` is the
#: ``/plugins`` toggle) and no uninstall, so each verb that stops it is ``remove``.
_PLUGIN_VERBS = {"disable": "remove", "uninstall": "remove", "install": "add"}


class CodexPlugin:
    """aisquare's Codex plugin (``plugins/codex``), as Codex 0.162.1 installs it (measured):
    enabled in a Codex home's ``config.toml``, copied into its ``plugins/cache``, and run
    only once ``/hooks`` trusts its hooks. A plugin belongs to one Codex home, at no other
    scope. Read-only; never raises."""

    def supported(self) -> bool:
        from aisquare.core import agents

        return agents.plugin_route_supported()

    def installed(self, config_dir: Path | None) -> PluginInstall | None:
        """The plugin where the home's ``config.toml`` enables it and ``codex plugin add``
        installed it, else ``None``.

        Enabled is a ``[plugins."aisquare@<marketplace>"]`` table without ``enabled =
        false``; installed is its folder in ``plugins/cache/<marketplace>/aisquare/
        <version>/``, whose manifest names the version. Both, because an enabled table
        with nothing installed runs nothing (Codex keeps one version there and removes the
        one it replaces). Trusted is each of its hooks holding a ``trusted_hash`` in
        ``[hooks.state]``, which ``/hooks`` writes: Codex skips a plugin's hooks until then.
        A hash for an older definition reads as trusted here; Codex asks again only when
        ``hooks.json`` itself changes, and keeps the trust across a version that does not.
        """
        home = _codex_home(config_dir)
        found = _enabled_install(home)
        if found is None:
            return None
        plugin_id, root, config = found
        return PluginInstall(
            home, _manifest_version(root) or root.name, trusted=_plugin_trusted(config, plugin_id)
        )

    def repo_installs(self, config_dir: Path | None) -> list[PluginInstall]:
        return []  # a Codex plugin is installed in a home, never for one repository

    def repo_install_here(self, config_dir: Path | None, cwd: Path | None) -> PluginInstall | None:
        return None

    def command(
        self, verb: str, config_dir: Path, *, scope: str = "user", project: Path | None = None
    ) -> str:
        """``codex plugin <verb> aisquare@<marketplace>``, aimed at ``config_dir`` as
        ``aisquare uninstall`` aims it (``lifecycle._codex_command``): nothing for the home
        this shell's ``codex`` reads, ``env -u CODEX_HOME`` for ``~/.codex`` from a shell
        that names another, the variable otherwise. ``disable`` and ``uninstall`` are
        ``remove`` (:data:`_PLUGIN_VERBS`)."""
        from aisquare.core import agents

        found = _enabled_install(config_dir)
        plugin_id = found[0] if found is not None else f"{PLUGIN}@{PLUGIN_MARKETPLACE}"
        argv = ["codex", "plugin", _PLUGIN_VERBS.get(verb, verb), plugin_id]
        command = shlex.join(argv) if sys.platform != "win32" else " ".join(argv)
        ambient = os.environ.get("CODEX_HOME", "").strip()
        default = agents.dir_identity(agents.home() / ".codex")
        here = agents.dir_identity(Path(ambient)) if ambient else default
        there = agents.dir_identity(config_dir)
        if there == here:
            return command
        if sys.platform == "win32":
            need = "unset" if there == default else f"set to {config_dir}"
            return f"{command} (run it with CODEX_HOME {need})"
        if there == default:
            return f"env -u CODEX_HOME {command}"
        return f"CODEX_HOME={shlex.quote(str(config_dir))} {command}"


class CodexAdapter:
    id = "codex"
    label = "Codex"
    binary = "codex"
    home_env = "CODEX_HOME"
    home_name = ".codex"
    settings_name = "hooks.json"
    install_hint = "npm install -g @openai/codex"
    accounts = CodexAccounts()
    plugin = CodexPlugin()
    capabilities = AgentCapabilities(
        hooks=(
            HookSpec("SessionStart", "codex", 120),
            HookSpec("UserPromptSubmit", "codex", 120),
            HookSpec("Stop", "codex", 30),
            HookSpec("SessionEnd", "codex", 3),
            HookSpec("Interrupt", "codex", 3),
            HookSpec("PermissionRequest", "codex", 3),
            HookSpec("PreToolUse", "codex", 3, "request_user_input"),
            HookSpec("PostToolUse", "codex", 3),
        ),
        requires_hook_trust=True,
        positional_prompt=True,
        first_context_file_only=True,
        sandbox_permissions=True,
        value_options=frozenset(
            {
                "--model",
                "-m",
                "--config",
                "--add-dir",
                "--cd",
                "-C",
                "--image",
                "-i",
                "--profile",
                "-p",
                "--sandbox",
                "-s",
                "--ask-for-approval",
                "--enable",
                "--disable",
                "--local-provider",
                "--remote",
                "--remote-auth-token-env",
                "--color",
                "--output-schema",
                "--output-last-message",
                "-o",
            }
        ),
        switch_options=frozenset(
            {
                "--no-alt-screen",
                "--yolo",
                "--search",
                "--oss",
                "--json",
                "--approve-for-me",
                "--dangerously-bypass-approvals-and-sandbox",
                "--dangerously-bypass-hook-trust",
                "--no-daemon",
                "--strict-config",
                "--skip-git-repo-check",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
            }
        ),
    )

    def resolve_model(
        self,
        role: str,
        *,
        binary: str,
        env: dict[str, str],
        probe: bool | None,
        refresh: bool,
        effort: str | None,
    ) -> harness.ModelResolution:
        from aisquare.core.agent_adapters.native_models import resolve_model

        # Resolve preferences first; only the effective AISquare-owned value is
        # validated by model_args after native overrides have been applied.
        result = resolve_model(self.id, role, env=env, effort=effort)
        level = self.effort_alias(result.effort)
        notes = []
        if level != result.effort.strip().lower():
            notes.append(f"Codex maps {result.effort!r} to native reasoning effort {level!r}.")
        elif level and level not in REASONING_EFFORTS:
            notes.append(
                f"Codex reasoning effort {level!r} is not one AISquare knows "
                f"({', '.join(REASONING_EFFORTS)}); it is passed on for Codex to validate."
            )
        return result.model_copy(update={"effort": level, "notes": notes})

    @staticmethod
    def effort_alias(effort: str) -> str:
        normalized = effort.strip().lower()
        return EFFORT_ALIASES.get(normalized, normalized)

    def native_args(self, args: list[str]) -> list[str]:
        from aisquare.core.agent_adapters.types import config_assignment, rewrite_option_values

        def config(value: str) -> str:
            parsed = config_assignment(value)
            if parsed is not None:
                key, effort = parsed
                if (
                    key == "model_reasoning_effort"
                    and self.effort_alias(effort) != effort.strip().lower()
                ):
                    return key + '="' + self.effort_alias(effort) + '"'
                if key in {"model", "model_reasoning_effort"}:
                    return key + "=" + value.partition("=")[2]
            return value

        return rewrite_option_values(args, config, "-c", "--config")

    def mcp_args(
        self,
        executable: str,
        args: list[str],
        env_vars: list[str] | None = None,
    ) -> list[str]:
        import json

        return [
            "-c",
            f"mcp_servers.aisquare.command={json.dumps(executable)}",
            "-c",
            f"mcp_servers.aisquare.args={json.dumps(args)}",
            "-c",
            f"mcp_servers.aisquare.env_vars={json.dumps(env_vars or [])}",
            "-c",
            "mcp_servers.aisquare.required=false",
        ]

    def context_files(self, home: Path) -> tuple[Path, ...]:
        return (home / "AGENTS.override.md", home / "AGENTS.md")

    def validate(self, model: str | None, effort: str | None) -> None:
        self.reasoning_effort(effort)

    def model_args(self, model: str | None, effort: str | None) -> list[str]:
        effort = self.reasoning_effort(effort)
        return (["--model", model] if model else []) + (
            ["-c", f'model_reasoning_effort="{effort}"'] if effort else []
        )

    @staticmethod
    def reasoning_effort(effort: str | None) -> str | None:
        """The level for ``model_reasoning_effort``: a known one, or any level-shaped name.

        Levels depend on the model (Codex's config reference), so a name AISquare does
        not know is Codex's to accept or refuse. One that is not a name at all cannot
        be one and would not survive the TOML value it is written into.
        """
        if effort is None or not effort.strip():
            return None
        normalized = CodexAdapter.effort_alias(effort)
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", normalized):
            raise BadEffortError(
                f"Codex reasoning effort must be a level name such as "
                f"{', '.join(REASONING_EFFORTS)}, not {effort!r}"
            )
        return normalized

    def fleet_args(
        self,
        role: str,
        label: str,
        permission_mode: str | None,
        *,
        sandbox: str | None = None,
        approval: str | None = None,
        approve_for_me: bool | None = None,
        native: Sequence[str] = (),
    ) -> list[str]:
        """The fleet's sandbox and approval flags for ``role``.

        ``--approve-for-me`` (Codex's automatic reviewer, in the workspace-write
        sandbox) is the analogue of Claude Code's ``auto`` mode, which the fleet
        gives every role but a reviewer's seats; Codex refuses it beside
        ``--sandbox`` or ``--ask-for-approval``, so it replaces both. Unset
        (``None``) it is on unless a sandbox or an approval policy is chosen, by the
        role's settings, this spawn or its ``native`` arguments.
        A reviewer is ``--sandbox read-only --ask-for-approval on-request``.
        What the ``native`` arguments already choose is not given a second time.
        """
        # Native modes are deliberately distinct from Claude's approval vocabulary.
        if sandbox is not None and permission_mode is not None:
            raise ValueError("Use --sandbox for Codex, without --permission-mode")
        sandbox = sandbox if sandbox is not None else permission_mode
        if sandbox is not None and sandbox not in {
            "",
            "read-only",
            "workspace-write",
            "danger-full-access",
        }:
            raise ValueError(
                "Codex permission mode must be read-only, workspace-write, or "
                "danger-full-access; Claude permission modes cannot be reused"
            )
        if approval is not None and approval not in APPROVAL_POLICIES:
            raise ValueError(
                f"Codex approval policy must be {' or '.join(APPROVAL_POLICIES)}, not {approval!r}"
            )
        reviewer = harness.base_role(role) == "reviewer"
        native_sandbox, native_approval = _native_approvals(native)
        if approve_for_me is None:
            approve_for_me = (
                not reviewer
                and sandbox is None
                and approval is None
                and native_sandbox is None
                and native_approval is None
            )
        if approve_for_me:
            clash = (
                f"--sandbox {sandbox}"
                if sandbox not in (None, "", "workspace-write")
                else f"--ask-for-approval {approval}"
                if approval is not None
                else native_sandbox or native_approval
            )
            if clash is not None:
                raise ValueError(
                    f"Codex --approve-for-me reviews approvals in the workspace-write sandbox "
                    f"and cannot run with {clash}; set fleet.roles.{role}.approve_for_me = "
                    "false to use it"
                )
            return ["--approve-for-me"]
        if sandbox is None:
            sandbox = "read-only" if reviewer else "workspace-write"
        if approval is None and reviewer:
            approval = "on-request"
        return (["--sandbox", sandbox] if sandbox and native_sandbox is None else []) + (
            ["--ask-for-approval", approval] if approval and native_approval is None else []
        )

    def disable_native_teams(self) -> tuple[list[str], dict[str, str]]:
        return ["-c", "agents.enabled=false"], {}


def _codex_home(config_dir: Path | None) -> Path:
    """The Codex home ``config_dir`` names, as ``agents connect codex`` resolves it; the one
    a session from this shell reads when ``None``."""
    from aisquare.core import agents

    spec = agents.spec(CodexAdapter.id, config_dir)
    return spec.home if spec is not None else Path(config_dir or ".")


def _enabled_install(home: Path) -> tuple[str, Path, dict[str, Any]] | None:
    """``(plugin id, installed copy, config.toml's table)`` for the first ``aisquare@
    <marketplace>`` that ``home`` enables and has a copy of, else ``None``."""
    config = _read_toml(home / "config.toml")
    tables = config.get("plugins")
    if not isinstance(tables, dict):
        return None
    for plugin_id, table in tables.items():
        name, _, marketplace = str(plugin_id).partition("@")
        if name != PLUGIN or not marketplace or Path(marketplace).name != marketplace:
            continue
        if not isinstance(table, dict) or table.get("enabled", True) is False:
            continue
        root = _installed_root(home / "plugins" / "cache" / marketplace / name)
        if root is not None:
            return str(plugin_id), root, config
    return None


def _plugin_trusted(config: Mapping[str, Any], plugin_id: str) -> bool:
    """Whether ``[hooks.state]`` trusts each hook the plugin's ``hooks.json`` holds: one
    group of one handler per event, keyed ``<plugin id>:hooks/hooks.json:<event in snake
    case>:0:0`` (tests/test_codex_plugin.py holds the file to that shape)."""
    hooks = config.get("hooks")
    states = hooks.get("state") if isinstance(hooks, dict) else None
    if not isinstance(states, dict):
        return False
    for spec in CodexAdapter.capabilities.hooks:
        event = re.sub(r"(?<!^)(?=[A-Z])", "_", spec.event).lower()
        state = states.get(f"{plugin_id}:{PLUGIN_HOOKS}:{event}:0:0")
        trusted = state.get("trusted_hash") if isinstance(state, dict) else None
        if not isinstance(trusted, str) or not trusted:
            return False
    return True


def _read_toml(path: Path) -> dict[str, Any]:
    """``path``'s TOML table, or ``{}``: absent, not a file, unreadable, or not TOML."""
    try:
        if not path.is_file():  # a FIFO would block the read
            return {}
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, ValueError, RecursionError):
        return {}


def _manifest(root: Path) -> Path:
    return root / ".codex-plugin" / "plugin.json"


def _installed_root(plugin_dir: Path) -> Path | None:
    """The installed copy under ``plugin_dir`` (one folder per version): the one whose
    manifest was written last, should an older one be left beside it."""
    try:
        roots = [root for root in plugin_dir.iterdir() if _manifest(root).is_file()]
    except OSError:
        return None
    try:
        return max(roots, key=lambda root: _manifest(root).stat().st_mtime, default=None)
    except OSError:
        return roots[0]


def _manifest_version(root: Path) -> str | None:
    try:
        data = json.loads(_manifest(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) and version else None


def _native_approvals(args: Sequence[str]) -> tuple[str | None, str | None]:
    """The options in ``args`` (up to Codex's own ``--``) that choose the sandbox and
    the approval policy, as written; a switch such as ``--approve-for-me`` chooses both."""
    for arg in args:
        if arg == "--":
            break
        if arg in _APPROVAL_SWITCHES:
            return arg, arg
    sandbox = approval = None
    for option in ("--sandbox", "-s"):
        for value in option_values(args, option):
            sandbox = sandbox or f"{option} {value}"
    for option in ("--ask-for-approval", "-a"):
        for value in option_values(args, option):
            approval = approval or f"{option} {value}"
    return sandbox, approval
