"""Native coding-agent contracts. No service, UI, or subprocess dependencies."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from aisquare.core import harness, paths

if TYPE_CHECKING:
    from aisquare.core.claude_accounts import LimitNotice
    from aisquare.models import ClaudeAccount, ClaudeIdentity, ClaudeUsage


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


@dataclass(frozen=True)
class PluginInstall:
    """The aisquare plugin, installed in one config directory and enabled there (user
    scope) or in one repository (project and local scope)."""

    config_dir: Path
    version: str | None
    """What the agent's install record says; ``None`` when it records none."""
    scope: str = "user"
    """``user``, or ``project``/``local``: the scope the plugin was installed at."""
    project: Path | None = None
    """The repository a project- or local-scope install is enabled in; ``None`` at user scope."""
    trusted: bool = True
    """Whether the agent runs the plugin's hooks: Codex runs none until ``/hooks`` trusts
    them; an agent that never asks always does."""


class AgentPlugin(Protocol):
    """The no-installer route: an aisquare plugin whose hooks run ``aisquare hook`` and
    stand down where the agent's hooks file already runs aisquare's. Read-only; never
    raises. ``config_dir`` ``None`` is the dir a session from this shell reads."""

    def supported(self) -> bool:
        """Whether the plugin's hooks can run on this platform."""
        ...

    def installed(self, config_dir: Path | None) -> PluginInstall | None:
        """The user-scope install that ``config_dir`` enables, else ``None``."""
        ...

    def repo_installs(self, config_dir: Path | None) -> list[PluginInstall]:
        """Project- and local-scope installs recorded in ``config_dir`` whose repository
        still enables them."""
        ...

    def repo_install_here(self, config_dir: Path | None, cwd: Path | None) -> PluginInstall | None:
        """The repository install a session of ``config_dir`` started in ``cwd`` loads."""
        ...

    def command(
        self, verb: str, config_dir: Path, *, scope: str = "user", project: Path | None = None
    ) -> str:
        """The agent's own ``plugin <verb>`` command line for that install, to paste."""
        ...


class AgentAdapter(Protocol):
    id: str
    label: str
    binary: str
    home_env: str
    home_name: str
    settings_name: str
    capabilities: AgentCapabilities
    install_hint: str

    @property
    def accounts(self) -> AgentAccounts | None:
        """Numbered login slots (``aisquare accounts``); ``None`` for an agent without them."""
        ...

    @property
    def plugin(self) -> AgentPlugin | None:
        """The aisquare plugin route for this agent; ``None`` when there is none."""
        ...

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
        approve_for_me: bool | None = None,
        native: Sequence[str] = (),
    ) -> list[str]: ...
    def disable_native_teams(self) -> tuple[list[str], dict[str, str]]: ...


def config_home(
    adapter: AgentAdapter,
    home: Path,
    env: Mapping[str, str],
    explicit: Path | None = None,
) -> Path:
    """Resolve against the environment the selected executable will actually use.

    A ``~olduser`` this machine does not have is kept as written (``paths.expand_user``),
    so ``paths.names_no_home`` still refuses to make it; a symlink loop is not resolved.
    Claude Code's directory, as ``core.agents`` names it, is ``core.agents._claude_home``.
    """
    raw = explicit or env.get(adapter.home_env, "").strip() or home / adapter.home_name
    path = paths.expand_user(Path(raw))
    if paths.names_no_home(path):
        return path
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path.absolute()


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


# --- accounts: one login per home directory, in numbered slots --------------------------
#
# ``core.claude_accounts`` owns the slot directories for every agent that has this
# slot; what differs per agent is here. One ladder numbers the slots of every agent
# (a managed slot's number is unique across them), and each agent's PLAIN home —
# whatever its binary uses in this shell — has a reserved number of its own.

UsageFetch = Callable[[str, Mapping[str, str], float], tuple[int, bytes]]
"""``(url, headers, timeout) -> (status, body)``: how an agent that reads usage over the
network reaches it. Supplied by the service layer; core never opens a socket."""


class AgentAccounts(Protocol):
    agent: str
    home_env: str
    """The variable naming the home a login lives in (``CLAUDE_CONFIG_DIR``, ``CODEX_HOME``)."""
    extra_env: tuple[str, ...]
    """Further per-slot variables, each set to the slot's own directory under ``cache``."""
    plain_vars: Mapping[str, str]
    """Each launch variable → where a launch onto a managed slot keeps the shell's own."""
    dir_name: str
    """The slots' parent under the aisquare home (``claude-accounts``, ``codex-accounts``)."""
    plain_slot: int
    plain_label: str
    install_command: str
    network_usage: bool
    """Whether :meth:`usage` leaves the machine (Claude's endpoint) or reads local files."""

    def sign_in_args(self) -> tuple[str, ...]:
        """Arguments to the agent's binary that start its own sign-in in a slot's env."""
        ...

    def plain_home(self, home: Path, plain: Mapping[str, str]) -> Path:
        """The plain home, from the shell's own launch variables (``plain``)."""
        ...

    def identity(
        self, account: ClaudeAccount, *, env: Mapping[str, str] | None = None
    ) -> ClaudeIdentity | None: ...
    def signed_in(self, account: ClaudeAccount) -> bool: ...
    def subscription(self, account: ClaudeAccount) -> str | None: ...
    def token_state(self, account: ClaudeAccount) -> str:
        """``ok``, ``expired`` or ``missing`` — read, never refreshed."""
        ...

    def usage(
        self, account: ClaudeAccount, *, now: datetime | None, fetch: UsageFetch
    ) -> ClaudeUsage: ...
    def limit_from_transcript(
        self, path: Path, *, now: datetime | None = None
    ) -> LimitNotice | None:
        """A usage limit the session's own transcript records at its end, or ``None``."""
        ...
