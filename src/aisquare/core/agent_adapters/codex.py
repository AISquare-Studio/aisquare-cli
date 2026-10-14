"""Codex CLI 0.162.1 integration; native hooks keep the real terminal UI."""

import json
import os
import re
import shlex
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

from aisquare.core import harness
from aisquare.core.agent_adapters.types import (
    AgentCapabilities,
    AgentEvent,
    BadEffortError,
    EventKind,
    HookSpec,
    HooksSwitch,
    NativeChoice,
    PermissionIntent,
    PluginInstall,
    WriteScope,
    block_reply,
    config_assignment,
    config_home,
    option_occurrences,
    option_values,
    payload_cwd,
    payload_text,
    store_dir,
    store_read_only,
)
from aisquare.core.codex_accounts import CodexAccounts, approvals_reviewer

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

    #: Its launcher stands down beside ANY aisquare hook in hooks.json whose program can
    #: start, every event sharing one command there: a partial hooks.json is not covered.
    stands_down_per_event = False

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


#: Each write scope of a :class:`PermissionIntent` in ``--sandbox``'s words, and back.
_SANDBOXES = {
    "read_only": "read-only",
    "workspace": "workspace-write",
    "full": "danger-full-access",
}
_SCOPES: dict[str, WriteScope] = {"read-only": "read_only", "workspace-write": "workspace"}
#: The permission profile (``[permissions.<name>]``, chosen by ``default_permissions``) a
#: fleet launch defines for its sandbox: Codex's built-in profile for the write scope with
#: the aisquare store added (:meth:`CodexAdapter.store_access`).
PERMISSION_PROFILE = "aisquare"
_BUILT_IN_PROFILES = {"read_only": ":read-only", "workspace": ":workspace"}
#: ``-c`` keys (and their tables) that choose the sandbox: given natively, the sandbox is
#: the operator's, and Codex refuses ``sandbox_mode`` beside ``default_permissions``.
_SANDBOX_SETTINGS = (
    "sandbox_mode",
    "sandbox_workspace_write",
    "default_permissions",
    "permission_profile",
)
#: Who answers an approval request (``approvals_reviewer``): the human, or Codex's
#: automatic reviewer (``--approve-for-me``; its old name ``guardian_subagent`` is
#: recorded as this one).
REVIEWERS = {"ask": "user", "auto": "auto_review"}
#: The hook events AISquare installs (``capabilities.hooks``) and decodes.
_EVENTS = {
    "SessionStart": "session_start",
    "UserPromptSubmit": "prompt",
    "Stop": "stop",
    "SessionEnd": "session_end",
    "Interrupt": "interrupt",
    "PermissionRequest": "attention",
    "PreToolUse": "attention",
    "PostToolUse": "tool_done",
}


class CodexAdapter:
    id = "codex"
    label = "Codex"
    binary = "codex"
    home_env = "CODEX_HOME"
    home_name = ".codex"
    settings_name = "hooks.json"
    install_hint = "npm install -g @openai/codex"
    version_args: tuple[str, ...] = ("--version",)
    hooks_off_setting = "`features.hooks = false`"
    #: Typed, then its Enter 0.2 s later: the paste-burst guard ate an Enter sent with it.
    exit_command = "/exit"
    #: Measured on 0.162.1 under tmux 3.7c: shift+enter submits the prompt (it arrives
    #: as Enter), ctrl+J inserts the newline. Esc interrupts a turn and shift+tab cycles
    #: Default/Plan mode, as their names are sent.
    key_spellings: Mapping[str, str] = MappingProxyType({"S-Enter": "C-j"})
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
        hook_names_home=True,
        strict_hooks_file=True,
        stop_reason_as_prompt=True,
        limits_in_transcript=True,
        native_telemetry=True,
        positional_prompt=True,
        idle_prompt_mark="? for shortcuts",
        first_context_file_only=True,
        sandbox_permissions=True,
        sandboxes_tools=True,
        tool_shell_env="CODEX_THREAD_ID",
        # -c and -a as well: `fleet spawn` owns neither. `launch` owns both, and keeps
        # -c KEY=VALUE for Codex by its shape; its -a is --account (`launch --help`).
        value_options=frozenset(
            {
                "--model",
                "-m",
                "--config",
                "-c",
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
                "-a",
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

    def config_dir(self, home: Path, env: Mapping[str, str], explicit: Path | None = None) -> Path:
        return config_home(self, home, env, explicit)

    def hooks_switch(
        self, home: Path, settings_path: Path, args: Sequence[str] = ()
    ) -> HooksSwitch | None:
        """``hooks = false`` under ``[features]``, or its deprecated alias ``codex_hooks``,
        in ``config.toml``, in the profile ``-p`` layers on it, or in ``args`` (``-c
        features.hooks=false``, ``--disable hooks``), a later layer winning and the
        canonical key winning over the alias wherever either is set, as Codex 0.162.1
        merges them."""
        user = home / "config.toml"
        layers: list[tuple[dict[str, Any], Path | None, str]] = [(_toml_features(user), user, "")]
        profile = next(reversed(option_values(args, "-p", "--profile")), None)
        if profile and Path(profile).name == profile and profile not in {".", ".."}:
            selected = home / f"{profile}.config.toml"
            layers.append((_toml_features(selected), selected, ""))
        for item in option_occurrences(args, "-c", "--config", "--enable", "--disable"):
            option = item.prefix.rstrip("=") or args[item.index]
            spelled = f"{option} {item.value}"
            if option in {"--enable", "--disable"}:
                layers.append(({item.value: option == "--enable"}, None, spelled))
                continue
            key, separator, raw = item.value.partition("=")
            if separator and key.strip().startswith("features."):
                try:
                    value = tomllib.loads("value=" + raw)["value"]
                except (ValueError, RecursionError):
                    continue  # Codex reads it as a string, which no feature switch takes
                layers.append(({key.strip().removeprefix("features."): value}, None, spelled))
        decided: dict[str, tuple[bool, Path | None, str]] = {}
        for features, path, spelled in layers:
            for key in ("hooks", "codex_hooks"):
                if isinstance(features.get(key), bool):
                    decided[key] = (features[key], path, spelled)
        key = "hooks" if "hooks" in decided else "codex_hooks"
        if key not in decided or decided[key][0]:
            return None
        _, path, spelled = decided[key]
        if path is None:
            return HooksSwitch(f"`{spelled}`")
        return HooksSwitch(f"`{key} = false` under [features]", path)

    def decode_event(self, payload: Mapping[str, Any]) -> AgentEvent | None:
        """Codex's command hook protocol: one ``aisquare hook codex`` for every event,
        named by ``hook_event_name``. A payload without a session id, or with one past
        256 characters, is not one of ours.

        A ``PermissionRequest`` is ``attention`` only when the human answers it: Codex
        fires it for its automatic reviewer's requests too (``--approve-for-me``, no
        prompt in the pane) with the same payload, so the session's rollout says which
        (:func:`~aisquare.core.codex_accounts.approvals_reviewer`); one it cannot read
        rings the bell, as a request nobody answers is worse than a false one.
        Measured on 0.162.1: PreToolUse comes before it, and PostToolUse (allowed),
        Interrupt (Esc) or Stop after."""
        native = payload_text(payload, "session_id")
        event = payload_text(payload, "hook_event_name")
        kind = _EVENTS.get(event or "")
        if not native or len(native) > 256 or kind is None:
            return None
        if event == "PreToolUse" and payload.get("tool_name") != "request_user_input":
            return None
        transcript = payload_text(payload, "transcript_path")
        if (
            event == "PermissionRequest"
            and transcript is not None
            and approvals_reviewer(Path(transcript)) == REVIEWERS["auto"]
        ):
            return None
        asks = "your approval" if event == "PermissionRequest" else "user input"
        return AgentEvent(
            kind,  # type: ignore[arg-type]
            native,
            cwd=payload_cwd(payload),
            prompt=payload_text(payload, "prompt") if kind == "prompt" else None,
            transcript_path=payload_text(payload, "transcript_path"),
            model=payload_text(payload, "model"),
            source=payload_text(payload, "source") if kind == "session_start" else None,
            stop_hook_active=bool(payload.get("stop_hook_active")),
            turn_id=payload_text(payload, "turn_id"),
            message=f"{self.label} needs {asks}" if kind == "attention" else None,
        )

    def encode_reply(self, kind: EventKind, reply: str | None) -> str:
        """Context is plain stdout; a Stop that keeps the turn going is the ``block``
        decision, as JSON on exit 0."""
        if not reply:
            return ""
        return block_reply(reply) if kind == "stop" else reply

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

    def native_choice(self, args: Sequence[str]) -> NativeChoice:
        """``-c model=…``/``-c model_reasoning_effort=…``, and ``--model``/``-m`` over both.
        The effort is a native config value, Codex's to validate: no ``effort_index``."""
        model = effort = None
        for item in option_occurrences(args, "-c", "--config"):
            parsed = config_assignment(item.value)
            if parsed is None:
                continue
            key, native = parsed
            if key == "model":
                model = native
            elif key == "model_reasoning_effort":
                effort = native
        models = option_values(args, "--model", "-m")
        return NativeChoice(models[-1] if models else model, effort)

    def native_args(self, args: list[str]) -> list[str]:
        from aisquare.core.agent_adapters.types import rewrite_option_values

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

    def browser_tools(self, home: Path) -> list[str] | None:
        """Codex's own browser, as ``config.toml`` in ``home`` has it: the ``browser_use``
        feature (stable and on unless ``[features] browser_use = false``, 0.162.1) and each
        enabled ``browser@<marketplace>`` plugin (``browser@openai-bundled``)."""
        config = _read_toml(home / "config.toml")
        features = config.get("features")
        found = []
        if not (isinstance(features, dict) and features.get("browser_use") is False):
            found.append("the browser_use feature")
        plugins = config.get("plugins")
        for plugin_id, table in plugins.items() if isinstance(plugins, dict) else ():
            if str(plugin_id).partition("@")[0] != "browser":
                continue
            if isinstance(table, dict) and table.get("enabled", True) is not False:
                found.append(f"the {plugin_id} plugin")
        return found

    def validate(self, model: str | None, effort: str | None) -> None:
        self.reasoning_effort(effort)

    def role_defaults(self, role: str, *, binary: str, args: Sequence[str]) -> harness.RoleDefaults:
        """None: the ui-tester's browser is Codex's ``browser_use`` feature (stable and on
        in 0.162.1) and its bundled browser plugin, read by ``aisquare doctor``
        (:meth:`browser_tools`), not a flag; Claude Code's ``--chrome`` is not Codex's."""
        return harness.RoleDefaults()

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
        """The fleet's sandbox and approval flags for ``role``, from its settings in Codex's
        words.

        With none chosen (``sandbox``, ``approval`` and ``approve_for_me`` unset), the
        role's built-in permission (``PermissionIntent.for_role``, :meth:`permission_args`):
        ``--approve-for-me`` (Codex's automatic reviewer, in the workspace-write sandbox),
        the analogue of Claude Code's ``auto`` mode, and for a reviewer's seats the
        read-only sandbox with that same reviewer. Codex refuses ``--approve-for-me``
        beside ``--sandbox`` or ``--ask-for-approval``, so a chosen sandbox or policy runs
        without it (``approve_for_me = true`` beside one is refused). A chosen sandbox is
        the store's profile for it (:meth:`store_access`; ``danger-full-access`` is
        ``--sandbox``), and ``on-request`` without ``--approve-for-me`` is the human's to
        answer (``approvals_reviewer="user"``). What the ``native`` arguments already
        choose is not given a second time.
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
        if approve_for_me is None and sandbox is None and approval is None:
            return self.permission_args(PermissionIntent.for_role(role), role, native)
        reviewer = harness.base_role(role) == "reviewer"
        native_sandbox, native_approval = _native_approvals(native)
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
            return ["--approve-for-me", *self._sandbox("workspace", native)]
        if sandbox is None:
            sandbox = "read-only" if reviewer else "workspace-write"
        if approval is None and reviewer:
            approval = "on-request"
        return [
            *(["--ask-for-approval", approval] if approval and native_approval is None else []),
            *(self._reviewer("ask", native) if approval != "never" else []),
            *(self._sandbox(_SCOPES.get(sandbox, "full"), native) if sandbox else []),
        ]

    def permission_args(
        self, intent: PermissionIntent, role: str, native: Sequence[str] = ()
    ) -> list[str]:
        """The write scope is the sandbox, the approvals who answers what it does not
        allow (measured on 0.162.1):

        - ``read_only`` and ``workspace`` are the store's profile (:meth:`store_access`),
          never ``--sandbox``, which would replace it; ``full`` is ``--sandbox
          danger-full-access``.
        - ``auto`` is Codex's automatic reviewer: ``--approve-for-me`` in the workspace,
          ``on-request`` with ``approvals_reviewer="auto_review"`` read-only (a reviewer's
          seats); ``full:auto`` is refused, as ``--approve-for-me`` runs only in
          workspace-write.
        - ``ask`` is ``on-request`` with ``approvals_reviewer="user"``: the human answers,
          on a resumed thread too, which keeps the reviewer it ran under otherwise, and
          whatever config.toml names.
        - ``never`` is ``--ask-for-approval never``.

        What ``native`` already chooses wins and is not given twice: ``--approve-for-me``
        cannot sit beside a native sandbox or policy (Codex refuses the launch), so it is
        left to them, and a native sandbox or reviewer is the operator's
        (:meth:`_sandbox`)."""
        if intent.approvals == "auto" and intent.write == "full":
            raise ValueError(
                "Codex reviews approvals automatically only in the workspace-write sandbox; "
                "use full:ask or full:never"
            )
        native_sandbox, native_approval = _native_approvals(native)
        sandbox = self._sandbox(intent.write, native)
        if intent.approvals == "auto" and intent.write == "workspace":
            return sandbox if native_sandbox or native_approval else ["--approve-for-me", *sandbox]
        approval = "never" if intent.approvals == "never" else "on-request"
        return [
            *(["--ask-for-approval", approval] if native_approval is None else []),
            *(self._reviewer(intent.approvals, native) if approval != "never" else []),
            *sandbox,
        ]

    def store_access(self, write: WriteScope, store: Path) -> list[str]:
        """``-c`` settings that run the session under the profile :data:`PERMISSION_PROFILE`:
        Codex's built-in one for ``write`` (``:read-only``, ``:workspace``) with ``store``
        writable and :func:`store_read_only` read-only within it, the more specific path
        winning. ``full`` runs unsandboxed: nothing to add.

        Measured on 0.162.1, with the store outside ``/tmp`` (which every sandbox may
        write): ``aisquare note`` and ``aisquare board`` succeed on their first, sandboxed
        attempt in the TUI, ``exec`` and ``resume``; the profile holds beside
        ``--approve-for-me`` and over a ``sandbox_mode`` in config.toml, and gives way to
        ``--sandbox`` and to a pick in Codex's own ``/permissions``, which drops it until
        the next launch. Codex's guards in the workspace (``.git``, ``.codex``,
        ``.agents``) still hold, and a read-only path that does not exist cannot be made.
        """
        if write == "full":
            return []
        entries = {store: "write", **dict.fromkeys(store_read_only(store), "read")}
        table = ", ".join(
            f"{json.dumps(str(path))} = {json.dumps(at)}" for path, at in entries.items()
        )
        profile = f"permissions.{PERMISSION_PROFILE}"
        return [
            "-c",
            f"default_permissions={json.dumps(PERMISSION_PROFILE)}",
            "-c",
            f"{profile}.extends={json.dumps(_BUILT_IN_PROFILES[write])}",
            "-c",
            f"{profile}.filesystem={{{table}}}",
        ]

    def _sandbox(self, write: WriteScope, native: Sequence[str]) -> list[str]:
        """The sandbox for ``write``, unless ``native`` chose one: a native ``--sandbox``
        replaces any profile (the store is then the operator's to reach) and a sandbox
        setting (``-c sandbox_mode=…``) cannot sit beside one, while ``--approve-for-me``
        runs in the workspace's, the store with it."""
        chosen, _ = _native_approvals(native)
        if _native_setting(native, _SANDBOX_SETTINGS):
            return []
        if chosen == "--approve-for-me":
            return self.store_access("workspace", store_dir())
        if chosen is not None:
            return []
        if write == "full":
            return ["--sandbox", _SANDBOXES["full"]]
        return self.store_access(write, store_dir())

    @staticmethod
    def _reviewer(approvals: str, native: Sequence[str]) -> list[str]:
        """``approvals_reviewer`` for ``ask`` or ``auto``, unless ``native`` chose one, or
        chose a switch that settles it (``--approve-for-me``, ``--yolo``)."""
        chosen, _ = _native_approvals(native)
        if chosen in _APPROVAL_SWITCHES or _native_setting(native, ("approvals_reviewer",)):
            return []
        return ["-c", f"approvals_reviewer={json.dumps(REVIEWERS[approvals])}"]

    def label_args(self, label: str) -> list[str]:
        return []

    def resume_args(self, native_session_id: str, transcript_path: Path | None) -> list[str]:
        """``codex resume <SESSION_ID>``: the thread by its id, any options after it."""
        return ["resume", native_session_id]

    def queue_args(self, native_session_id: str, message: str) -> list[str] | None:
        """``codex queue --thread <id> --message <text>``: measured on 0.162.1 against an
        interactive TUI session busy with a turn, the message ran as its next turn."""
        return ["queue", "--thread", native_session_id, "--message", message]

    def disable_native_teams(self) -> tuple[list[str], dict[str, str]]:
        return ["-c", "agents.enabled=false"], {}

    def telemetry_args(self, endpoint: str, authorization: str) -> list[str]:
        """Codex's own OTLP/HTTP log exporter (asynchronous; no model routing or auth
        change), without the user's prompts."""
        config = "otel.exporter.otlp-http"
        return [
            "-c",
            f"{config}.endpoint={json.dumps(endpoint)}",
            "-c",
            f'{config}.protocol="json"',
            "-c",
            f"{config}.headers.Authorization={json.dumps(authorization)}",
            "-c",
            "otel.log_user_prompt=false",
        ]


def _toml_features(path: Path) -> dict[str, Any]:
    """``path``'s ``[features]`` table; ``{}`` when there is none or it cannot be read."""
    try:
        if not path.is_file():  # a FIFO would block the read
            return {}
        with path.open("rb") as handle:
            features = tomllib.load(handle).get("features")
    except (OSError, ValueError, RecursionError):
        return {}
    return features if isinstance(features, dict) else {}


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


def _native_setting(args: Sequence[str], keys: Sequence[str]) -> str | None:
    """The first ``-c`` in ``args`` that sets one of ``keys`` or a key of its table, as
    written, else ``None``."""
    for item in option_occurrences(args, "-c", "--config"):
        key = item.value.partition("=")[0].strip()
        if any(key == name or key.startswith(f"{name}.") for name in keys):
            return item.value
    return None
