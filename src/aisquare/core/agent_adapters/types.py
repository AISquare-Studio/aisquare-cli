"""Native coding-agent contracts. No service, UI, or subprocess dependencies."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from aisquare.core import harness


@dataclass(frozen=True)
class HookSpec:
    event: str
    command: str
    timeout: int | None = None
    matcher: str | None = None


@dataclass(frozen=True)
class AgentCapabilities:
    hooks: tuple[HookSpec, ...]
    assigns_session_id: bool = False
    requires_hook_trust: bool = False
    model_proxy: bool = False
    model_ladders: bool = False
    positional_prompt: bool = False
    first_context_file_only: bool = False
    sandbox_permissions: bool = False
    legacy_fleet_args: bool = False


class AgentAdapter(Protocol):
    id: str
    label: str
    binary: str
    home_env: str
    home_name: str
    settings_name: str
    capabilities: AgentCapabilities
    install_hint: str

    def mcp_args(
        self,
        executable: str,
        args: list[str],
        env_vars: list[str] | None = None,
    ) -> list[str]: ...
    def resolve_model(
        self,
        role: str,
        *,
        binary: str,
        env: dict[str, str],
        probe: bool | None,
        refresh: bool,
        effort: str | None,
    ) -> harness.ModelResolution | None: ...
    def native_args(self, args: list[str]) -> list[str]: ...
    def context_files(self, home: Path) -> tuple[Path, ...]: ...
    def model_args(self, model: str | None, effort: str | None) -> list[str]: ...
    def fleet_args(
        self,
        role: str,
        label: str,
        permission_mode: str | None,
        *,
        sandbox: str | None = None,
        approval: str | None = None,
    ) -> list[str]: ...
    def disable_native_teams(self) -> tuple[list[str], dict[str, str]]: ...


def config_home(
    adapter: AgentAdapter,
    home: Path,
    env: Mapping[str, str],
    explicit: Path | None = None,
) -> Path:
    """Resolve against the environment the selected executable will actually use."""
    raw = explicit or env.get(adapter.home_env, "").strip() or home / adapter.home_name
    return Path(raw).expanduser().resolve()


def option_values(args: Sequence[str], *options: str) -> list[str]:
    """Read native option values up to the native CLI's own ``--`` boundary.

    The outer AISquare separator has already been consumed. Like the native
    CLI, ``-mfoo`` is a model; a literal dash-prefixed prompt needs native ``--``.
    """
    values: list[str] = []
    tokens = iter(args)
    for arg in tokens:
        if arg == "--":
            break
        if arg in options:
            value = next(tokens, None)
            if value is not None and value != "--":
                values.append(value)
            elif value == "--":
                break
        else:
            for option in options:
                if arg.startswith(option + "="):
                    values.append(arg[len(option) + 1 :])
                    break
                if len(option) == 2 and arg.startswith(option) and len(arg) > 2:
                    values.append(arg[2:])
                    break
    return values


def has_option(args: Sequence[str], *options: str) -> bool:
    return bool(option_values(args, *options))


def rewrite_option_values(
    args: Sequence[str], transform: Callable[[str], str], *options: str
) -> list[str]:
    """Transform values while preserving native spelling and the literal boundary."""
    result: list[str] = []
    tokens = iter(args)
    for arg in tokens:
        if arg == "--":
            result.extend([arg, *tokens])
            break
        if arg in options:
            result.append(arg)
            value = next(tokens, None)
            if value == "--":
                result.extend([value, *tokens])
                break
            if value is not None:
                result.append(transform(value))
            continue
        for option in options:
            if arg.startswith(option + "="):
                arg = option + "=" + transform(arg[len(option) + 1 :])
                break
            if len(option) == 2 and arg.startswith(option) and len(arg) > 2:
                arg = option + transform(arg[2:])
                break
        result.append(arg)
    return result


def model_overrides(agent: str, args: Sequence[str]) -> tuple[str | None, str | None]:
    """Explicit native model/effort wins over AISquare's configured defaults."""
    import tomllib

    model: str | None = None
    effort: str | None = None
    if agent == "codex":
        for assignment in option_values(args, "-c", "--config"):
            key, sep, value = assignment.partition("=")
            if not sep or key.strip() not in {"model", "model_reasoning_effort"}:
                continue
            try:
                decoded = tomllib.loads("value=" + value)["value"]
            except ValueError:
                decoded = value  # Codex also accepts bare string overrides.
            # Even a value of the wrong TOML type belongs to Codex. Suppress
            # our defaults and let its native config validation explain it.
            native = decoded if isinstance(decoded, str) else value
            if key.strip() == "model":
                model = native
            else:
                effort = native
    models = option_values(args, "--model", "-m")
    if models:
        model = models[-1]
    if agent == "claude-code":
        efforts = option_values(args, "--effort")
        if efforts:
            effort = efforts[-1]
    return model, effort


def executable_name(binary: str) -> str:
    """Canonical native executable name, including Windows paths and npm shims."""
    name = binary.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".cmd", ".ps1"):
        if name.endswith(suffix):
            return name.removesuffix(suffix)
    return name


def fleet_extra_args(
    adapter: AgentAdapter, legacy: Sequence[str], native: Mapping[str, list[str]]
) -> list[str]:
    """Old fleet args belong to Claude; native argument lists name their owner."""
    return [
        *(legacy if adapter.capabilities.legacy_fleet_args else []),
        *native.get(adapter.id, []),
    ]


class BadEffortError(ValueError):
    """An explicit reasoning level is invalid for the selected agent."""
