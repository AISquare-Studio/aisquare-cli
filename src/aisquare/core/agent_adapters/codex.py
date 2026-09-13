"""Codex CLI 0.153.4 integration; native hooks keep the real terminal UI."""

from pathlib import Path

from aisquare.core import harness
from aisquare.core.agent_adapters.types import AgentCapabilities, BadEffortError, HookSpec


class CodexAdapter:
    id = "codex"
    label = "Codex"
    binary = "codex"
    home_env = "CODEX_HOME"
    home_name = ".codex"
    settings_name = "hooks.json"
    install_hint = "npm install -g @openai/codex"
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
                "--output-schema",
                "--output-last-message",
                "-o",
            }
        ),
        switch_options=frozenset(
            {
                "--no-alt-screen",
                "--full-auto",
                "--yolo",
                "--search",
                "--oss",
                "--json",
                "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check",
                "--ephemeral",
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
        notes = (
            [f"Codex maps {result.effort!r} to native reasoning effort {level!r}."]
            if level != result.effort.strip().lower()
            else []
        )
        return result.model_copy(update={"effort": level, "notes": notes})

    @staticmethod
    def effort_alias(effort: str) -> str:
        normalized = effort.strip().lower()
        return {"max": "xhigh", "ultracode": "xhigh"}.get(normalized, normalized)

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
        if effort is None or not effort.strip():
            return None
        normalized = CodexAdapter.effort_alias(effort)
        if normalized not in {"minimal", "low", "medium", "high", "xhigh"}:
            raise BadEffortError(f"Codex does not support reasoning effort {effort!r}")
        return normalized

    def fleet_args(
        self,
        role: str,
        label: str,
        permission_mode: str | None,
        *,
        sandbox: str | None = None,
        approval: str | None = None,
    ) -> list[str]:
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
        if sandbox is None:
            sandbox = "read-only" if harness.base_role(role) == "reviewer" else "workspace-write"
        if approval is not None and approval not in {"on-request", "never", "untrusted"}:
            raise ValueError("Codex approval policy must be on-request, untrusted, or never")
        return (["--sandbox", sandbox] if sandbox else []) + (
            ["--ask-for-approval", approval] if approval else []
        )

    def disable_native_teams(self) -> tuple[list[str], dict[str, str]]:
        return ["-c", "agents.enabled=false"], {}
