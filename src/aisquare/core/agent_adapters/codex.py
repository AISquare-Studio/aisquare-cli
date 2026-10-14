"""Codex CLI 0.162.1 integration; native hooks keep the real terminal UI."""

import re
from collections.abc import Sequence
from pathlib import Path

from aisquare.core import harness
from aisquare.core.agent_adapters.types import (
    AgentCapabilities,
    BadEffortError,
    HookSpec,
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


class CodexAdapter:
    id = "codex"
    label = "Codex"
    binary = "codex"
    home_env = "CODEX_HOME"
    home_name = ".codex"
    settings_name = "hooks.json"
    install_hint = "npm install -g @openai/codex"
    accounts = CodexAccounts()
    plugin = None
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
