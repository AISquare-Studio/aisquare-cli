"""Native coding-agent contracts. No service, UI, or subprocess dependencies."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
    return Path(raw).expanduser().absolute()


def has_option(args: Sequence[str], *options: str) -> bool:
    """Only inspect agent options, never a prompt after the option terminator."""
    for arg in args:
        if arg == "--":
            break
        if arg.split("=", 1)[0] in options:
            return True
        if any(
            len(option) == 2
            and option.startswith("-")
            and arg.startswith(option)
            and len(arg) > len(option)
            for option in options
        ):
            return True
    return False


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
