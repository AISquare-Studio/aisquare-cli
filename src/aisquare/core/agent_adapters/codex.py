"""Codex CLI 0.153.4 integration; native hooks keep the real terminal UI."""

from pathlib import Path

from aisquare.core import harness
from aisquare.core.agent_adapters.types import AgentCapabilities, HookSpec


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
        structured_exec=True,
        positional_prompt=True,
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

        result = resolve_model(self.id, role, env=env, effort=effort)
        self.model_args(result.model or None, result.effort or None)
        return result

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
        for name in ("AGENTS.override.md", "AGENTS.md"):
            path = home / name
            if path.is_file() and path.read_text(encoding="utf-8").strip():
                return (path,)
        return ()

    def model_args(self, model: str | None, effort: str | None) -> list[str]:
        if effort is not None and effort not in {"minimal", "low", "medium", "high", "xhigh"}:
            raise ValueError(f"Codex does not support reasoning effort {effort!r}")
        return (["--model", model] if model else []) + (
            ["-c", f'model_reasoning_effort="{effort}"'] if effort else []
        )

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
            sandbox = "read-only" if role == "reviewer" else "workspace-write"
        if approval is not None and approval not in {"on-request", "never", "untrusted"}:
            raise ValueError("Codex approval policy must be on-request, untrusted, or never")
        return (["--sandbox", sandbox] if sandbox else []) + (
            ["--ask-for-approval", approval] if approval else []
        )

    def disable_native_teams(self) -> tuple[list[str], dict[str, str]]:
        return ["-c", "agents.enabled=false"], {}

    def resume_args(self, native_id: str) -> list[str]:
        return ["resume", native_id]

    def exec_args(self, prompt: str, native_id: str | None = None) -> list[str]:
        return ["exec", *(["resume", native_id] if native_id else []), "--json", prompt]
