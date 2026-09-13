"""Native coding-agent contracts. No service, UI, or subprocess dependencies."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
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
    value_options: frozenset[str] = frozenset()
    switch_options: frozenset[str] = frozenset()


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
    def validate(self, model: str | None, effort: str | None) -> None: ...
    def effort_alias(self, effort: str) -> str: ...
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


@dataclass(frozen=True)
class OptionValue:
    index: int
    value_index: int
    prefix: str
    value: str


def option_occurrences(args: Sequence[str], *options: str) -> Iterator[OptionValue]:
    """Read native option values up to the native CLI's own ``--`` boundary.

    The outer AISquare separator has already been consumed. Like the native
    CLI, ``-mfoo`` is a model; a literal dash-prefixed prompt needs native ``--``.
    """
    tokens = iter(enumerate(args))
    for index, arg in tokens:
        if arg == "--":
            break
        if arg in options:
            following = next(tokens, None)
            if following is not None and following[1] != "--":
                yield OptionValue(index, following[0], "", following[1])
            elif following is not None:
                break
        else:
            for option in options:
                if arg.startswith(option + "="):
                    yield OptionValue(index, index, option + "=", arg[len(option) + 1 :])
                    break
                if len(option) == 2 and arg.startswith(option) and len(arg) > 2:
                    yield OptionValue(index, index, option, arg[2:])
                    break


def option_values(args: Sequence[str], *options: str) -> list[str]:
    return [item.value for item in option_occurrences(args, *options)]


def rewrite_option_values(
    args: Sequence[str], transform: Callable[[str], str], *options: str
) -> list[str]:
    """Transform values while preserving native spelling and the literal boundary."""
    result = list(args)
    for item in option_occurrences(args, *options):
        result[item.value_index] = item.prefix + transform(item.value)
    return result


def is_config_assignment(value: str) -> bool:
    return re.match(r"^[A-Za-z_][\w.-]*\s*=", value.lstrip()) is not None


def config_assignment(assignment: str) -> tuple[str, str] | None:
    key, sep, value = assignment.partition("=")
    if not sep:
        return None
    try:
        decoded = tomllib.loads("value=" + value)["value"]
    except (ValueError, RecursionError):
        decoded = value  # Native CLI validation owns malformed/future TOML values.
    return key.strip(), decoded if isinstance(decoded, str) else value


def model_overrides(agent: str, args: Sequence[str]) -> tuple[str | None, str | None]:
    """Explicit native model/effort wins over AISquare's configured defaults."""
    model: str | None = None
    effort: str | None = None
    if agent == "codex":
        for assignment in option_values(args, "-c", "--config"):
            parsed = config_assignment(assignment)
            if parsed is None:
                continue
            key, native = parsed
            if key == "model":
                model = native
            elif key == "model_reasoning_effort":
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
