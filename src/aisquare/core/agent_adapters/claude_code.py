"""Claude Code's native integration vocabulary."""

from pathlib import Path

from aisquare.core import harness
from aisquare.core.agent_adapters.types import AgentCapabilities, BadEffortError, HookSpec


class ClaudeCodeAdapter:
    id = "claude-code"
    label = "Claude Code"
    binary = "claude"
    home_env = "CLAUDE_CONFIG_DIR"
    home_name = ".claude"
    settings_name = "settings.json"
    install_hint = "https://claude.ai/install.sh"
    capabilities = AgentCapabilities(
        hooks=(
            HookSpec("SessionStart", "session-start", 120),
            HookSpec("UserPromptSubmit", "user-prompt-submit", 120),
            HookSpec("SessionEnd", "session-end"),
            HookSpec("Stop", "stop"),
            HookSpec("Notification", "notification"),
        ),
        assigns_session_id=True,
        model_proxy=True,
        model_ladders=True,
        legacy_fleet_args=True,
        value_options=frozenset(
            {
                "--model",
                "-m",
                "--add-dir",
                "--settings",
                "--effort",
                "--resume",
                "--session-id",
                "--permission-mode",
                "--mcp-config",
                "--allowedTools",
                "--disallowedTools",
                "--system-prompt",
                "--append-system-prompt",
                "--max-turns",
                "--output-format",
                "--input-format",
                "--name",
            }
        ),
        switch_options=frozenset(
            {
                "--dangerously-skip-permissions",
                "--continue",
                "--verbose",
                "--chrome",
                "--no-chrome",
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
    ) -> harness.ModelResolution | None:
        return harness.resolve_model(
            role,
            probe=probe,
            refresh=refresh,
            effort=effort,
            context=harness.ProbeContext(binary=binary, env=env),
        )

    def mcp_args(
        self,
        executable: str,
        args: list[str],
        env_vars: list[str] | None = None,
    ) -> list[str]:
        import json

        return [
            "--mcp-config",
            json.dumps({"mcpServers": {"aisquare": {"command": executable, "args": args}}}),
        ]

    def context_files(self, home: Path) -> tuple[Path, ...]:
        return (home / "CLAUDE.md",)

    def native_args(self, args: list[str]) -> list[str]:
        return list(args)

    @staticmethod
    def effort_alias(effort: str) -> str:
        return effort.strip().lower()

    def validate(self, model: str | None, effort: str | None) -> None:
        if effort is not None and harness.normalize_effort(effort) is None:
            raise BadEffortError(
                f"Claude Code does not support effort {effort!r}; use one of: "
                + ", ".join((*harness.EFFORT_SCALE, harness.ULTRACODE))
            )

    def model_args(self, model: str | None, effort: str | None) -> list[str]:
        self.validate(model, effort)
        return (["--model", model] if model else []) + (["--effort", effort] if effort else [])

    def fleet_args(
        self,
        role: str,
        label: str,
        permission_mode: str | None,
        *,
        sandbox: str | None = None,
        approval: str | None = None,
    ) -> list[str]:
        if sandbox or approval:
            raise ValueError(
                "Claude Code uses --permission-mode; sandbox/approval are Codex settings"
            )
        return (["--permission-mode", permission_mode] if permission_mode else []) + [
            "--name",
            label,
        ]

    def disable_native_teams(self) -> tuple[list[str], dict[str, str]]:
        return [], {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "0"}
