"""Native coding-agent contracts. No service, UI, or subprocess dependencies.

:class:`AgentAdapter` is everything AISquare knows about one terminal agent, and the
shared services ask it rather than the agent's name: a new agent is a module that
implements it and one :func:`aisquare.core.agent_adapters.register` line
(docs/coding-agents.md, "Adding a terminal agent"; tests/test_adapter_contract.py
holds every registered adapter to it).
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, get_args

from aisquare.core import harness, paths

if TYPE_CHECKING:
    from aisquare.core.claude_accounts import LimitNotice
    from aisquare.models import ClaudeAccount, ClaudeIdentity, ClaudeUsage


@dataclass(frozen=True)
class HookSpec:
    """One native hook event AISquare installs, and the ``aisquare hook`` subcommand it runs.

    ``command`` is the subcommand: Claude Code's are one per event (``session-start``…),
    Codex's is ``codex`` for every event, and a new agent's is :data:`GENERIC_HOOK`
    (``aisquare hook event --agent <id>``), so it needs no subcommand of its own.
    ``timeout`` is seconds, ``None`` for the agent's default; ``matcher`` narrows the
    event where the agent's hook format has one (Codex's ``PreToolUse``).
    """

    event: str
    command: str
    timeout: int | None = None
    matcher: str | None = None


#: The ``aisquare hook`` subcommand any adapter can install: ``event --agent <id> --event
#: <native event>`` (:func:`hook_args`), decoded by that adapter.
GENERIC_HOOK = "event"


@dataclass(frozen=True)
class AgentCapabilities:
    """What one agent does differently, as flags the shared services read instead of its id.

    Every flag defaults to the behaviour a new agent most likely has; each says which
    service reads it.
    """

    hooks: tuple[HookSpec, ...]
    assigns_session_id: bool = False
    """AISquare names the session at launch (Claude Code's ``--session-id``), so the
    native id IS the board's session id; otherwise the board's is derived from the config
    dir and the native id (``services.agent_events.session_key``) and every hook binds
    it to the launch (``core.agent_sessions.bind_launch_session``)."""
    requires_hook_trust: bool = False
    """The agent runs a hook only once the user trusts its definition (Codex's
    ``/hooks``): the installed commands carry ``--definition <fingerprint>`` and the
    integration reads ``unverified`` until AISquare sees that definition run."""
    hook_names_home: bool = False
    """The installed hook command names its config dir (``--config-dir``): the agent
    does not tell its hooks which home they run for. The dir is then the session's
    account; otherwise the account is read off the transcript path."""
    strict_hooks_file: bool = False
    """A hooks file whose ``hooks`` is not an object of arrays is refused, never
    rewritten (Codex's ``hooks.json``); Claude Code's ``settings.json`` reads it as none."""
    stop_reason_as_prompt: bool = False
    """A Stop hook's ``block`` reason comes back as the next ``UserPromptSubmit`` (Codex),
    which is then recorded as a continuation, not as the human's prompt."""
    limits_in_transcript: bool = False
    """No turn-failed event: a usage limit is read off the end of the session's
    transcript (``accounts.limit_from_transcript``) when a turn stops, is interrupted
    or the session ends. Claude Code reports it in ``StopFailure``."""
    ambient_first_run: bool = False
    """``agents connect`` makes a config dir the agent has not made yet only when it is
    the one a session from this shell reads (Claude Code: a typo must not get hooks);
    otherwise the dir as named (Codex: a fleet account is connected before its first run)."""
    reads_pane_readiness: bool = False
    """The fleet can tell from the pane that the agent sits at its prompt, so ``fleet
    tell`` types into a pane the listing reads as waiting; without it, only into one
    whose own hooks freshly say waiting (anything else is a board note)."""
    resumes_across_homes: bool = False
    """A session resumes from another config dir (Claude Code finds a transcript by its
    absolute path), so ``fleet switch`` to another account resumes it; otherwise the
    thread lives in its home and the replacement starts fresh with a hand-off prompt."""
    exports_home_env: bool = True
    """A launch sets :attr:`AgentAdapter.home_env` to the config dir it resolved; Claude
    Code's ``CLAUDE_CONFIG_DIR`` stays as the shell or the binding wrote it."""
    native_telemetry: bool = False
    """The agent exports OpenTelemetry to a local receiver AISquare starts
    (:meth:`AgentAdapter.telemetry_args`)."""
    own_doctor_check: bool = False
    """``services.diagnostics`` has a dedicated check for this agent (Claude Code's hook
    timeouts, plugin route and config-dir siblings), so the per-adapter row skips it."""
    model_proxy: bool = False
    model_ladders: bool = False
    """AISquare's role model ladder and its probe choose the model (Claude Code); an
    agent without keeps its native default unless a model is pinned."""
    positional_prompt: bool = False
    """The first prompt is a positional argument (Codex); otherwise the fleet types it."""
    first_context_file_only: bool = False
    sandbox_permissions: bool = False
    """``fleet.roles.<role>.sandbox``/``approval_policy``/``approve_for_me`` apply."""
    legacy_fleet_args: bool = False
    """``fleet.roles.<role>.extra_args`` and ``permission_mode`` are in this agent's
    vocabulary (they predate adapters and are Claude Code's); another agent takes only
    an explicitly passed mode and its ``agent_args``."""
    value_options: frozenset[str] = frozenset()
    switch_options: frozenset[str] = frozenset()


# --- events: one vocabulary for every agent's lifecycle hooks -----------------------------

EventKind = Literal[
    "session_start",
    "prompt",
    "stop",
    "session_end",
    "interrupt",
    "attention",
    "tool_done",
    "turn_failed",
    "notification",
]
EVENT_KINDS: tuple[EventKind, ...] = get_args(EventKind)


@dataclass(frozen=True)
class AgentEvent:
    """One native hook call, decoded (:meth:`AgentAdapter.decode_event`).

    ``session_id`` is the agent's own id for the session, ``None`` when the payload
    carries none (the shared services then record what needs no session). The rest is
    what the kind uses: ``prompt`` for ``prompt``; ``source``
    (``startup|resume|clear|compact``) for ``session_start``; ``reason`` for
    ``session_end``; ``stop_hook_active``, the loop guard, for ``stop``; ``error``,
    ``message`` and ``details`` for ``turn_failed``; ``message`` and
    ``notification_type`` for ``attention`` and ``notification``. ``turn_id``, where the
    agent has one, makes a redelivered prompt or stop answer as it did the first time.
    """

    kind: EventKind
    session_id: str | None
    cwd: Path | None = None
    prompt: str | None = None
    transcript_path: str | None = None
    model: str | None = None
    effort: str | None = None
    source: str | None = None
    reason: str | None = None
    stop_hook_active: bool = False
    turn_id: str | None = None
    error: str | None = None
    message: str | None = None
    details: str | None = None
    notification_type: str | None = None


def payload_text(payload: Mapping[str, Any], key: str) -> str | None:
    """A non-empty string field of a hook payload, else ``None``."""
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def payload_cwd(payload: Mapping[str, Any]) -> Path | None:
    cwd = payload_text(payload, "cwd")
    return Path(cwd) if cwd else None


def block_reply(reason: str) -> str:
    """A Stop hook's "keep going" as Claude Code and Codex both read it: the top-level
    ``{"decision": "block", "reason": …}`` object (``services.team.StopDecision``)."""
    import json

    return json.dumps({"decision": "block", "reason": reason})


# --- permissions: one intent, each agent's flags ------------------------------------------

WriteScope = Literal["read_only", "workspace", "full"]
Approvals = Literal["ask", "auto", "never"]


@dataclass(frozen=True)
class PermissionIntent:
    """What an agent may do without asking, in no agent's vocabulary.

    ``write``: ``read_only`` (no edits), ``workspace`` (the project and its worktree)
    or ``full`` (anything). ``approvals``: ``ask`` the human, ``auto`` (the agent's own
    reviewer decides: Claude Code's ``auto`` mode, Codex's ``--approve-for-me``) or
    ``never`` ask (what is not allowed fails). :meth:`AgentAdapter.permission_args` maps
    it; an explicit native flag (``--permission-mode``, ``--sandbox``…) still wins.
    Written ``write:approvals`` (``workspace:auto``).
    """

    write: WriteScope = "workspace"
    approvals: Approvals = "auto"

    @classmethod
    def for_role(cls, role: str) -> PermissionIntent:
        """The built-in intent: a reviewer's seats are read-only, every role else
        ``workspace:auto`` (the fleet's ``auto`` mode and Codex's ``--approve-for-me``)."""
        if harness.base_role(role) == "reviewer":
            return cls("read_only", "auto")
        return cls()

    @classmethod
    def parse(cls, value: str) -> PermissionIntent:
        write, _, approvals = value.strip().partition(":")
        writes, asks = get_args(WriteScope), get_args(Approvals)
        if write not in writes or approvals not in asks:
            raise ValueError(
                f"permission intent must be WRITE:APPROVALS with WRITE one of "
                f"{', '.join(writes)} and APPROVALS one of {', '.join(asks)}, not {value!r}"
            )
        return cls(write, approvals)  # type: ignore[arg-type]

    def __str__(self) -> str:
        return f"{self.write}:{self.approvals}"


# --- version probe, hooks switch, plugin route ---------------------------------------------


_VERSION_TOKEN = re.compile(r"\d+(?:\.\d+)+[0-9A-Za-z.+!-]*")


def version_in(output: str) -> str | None:
    """The first dotted version in what a ``--version`` printed (``codex-cli 0.162.1``,
    ``2.1.250 (Claude Code)``, aisquare's own), or ``None`` when it printed none."""
    match = _VERSION_TOKEN.search(output)
    return match.group(0) if match else None


@dataclass(frozen=True)
class HooksSwitch:
    """What switches every one of an agent's hooks off: the setting, as the operator writes
    it, and the file that sets it (``None``: a launch argument)."""

    setting: str
    path: Path | None = None

    def __str__(self) -> str:
        return f"{self.setting} in {self.path or 'the launch arguments'}"


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

    stands_down_per_event: bool
    """The plugin's launcher stands down only for the events the hooks file already runs
    (Claude Code's), so it covers a partial install; otherwise it stands down beside any
    of our hooks there whose program can start (``core.agents.plugin_stands_down``), and
    a partial install is not covered."""

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
    """One terminal agent, in the terms the shared services ask in.

    Identity and installation: ``id`` (the board's and the config's name), ``label``,
    ``binary``, ``home_env``/``home_name`` with :meth:`config_dir`, ``install_hint``,
    ``version_args`` with :func:`version_in`. Hooks: ``settings_name`` (the hooks file in
    the config dir), ``capabilities.hooks`` with :func:`hook_args`, ``hooks_off_setting``
    with :meth:`hooks_switch`, ``plugin``. Events: :meth:`decode_event` and
    :meth:`encode_reply`. Launch: :meth:`model_args`, :meth:`permission_args`,
    :meth:`fleet_args`, :meth:`label_args`, :meth:`resume_args`, ``exit_command``,
    :meth:`mcp_args`, :meth:`context_files`, :meth:`disable_native_teams`,
    :meth:`telemetry_args`. Accounts: ``accounts``. ``capabilities`` says the rest.
    """

    id: str
    label: str
    binary: str
    home_env: str
    home_name: str
    settings_name: str
    capabilities: AgentCapabilities
    install_hint: str
    version_args: tuple[str, ...]
    """Arguments that print the version without starting a session (``--version``)."""
    hooks_off_setting: str
    """The setting that switches every hook off, as a message names it without a file."""
    exit_command: str
    """What the fleet types into the agent's prompt to end the session gracefully, so its
    ``SessionEnd`` hook runs (``/exit``)."""

    @property
    def accounts(self) -> AgentAccounts | None:
        """Numbered login slots (``aisquare accounts``); ``None`` for an agent without them."""
        ...

    @property
    def plugin(self) -> AgentPlugin | None:
        """The aisquare plugin route for this agent; ``None`` when there is none."""
        ...

    def config_dir(self, home: Path, env: Mapping[str, str], explicit: Path | None = None) -> Path:
        """The config dir the agent reads, launched with ``env``: ``explicit``, else
        ``env[home_env]``, else ``home / home_name`` (:func:`config_home`)."""
        ...

    def hooks_switch(
        self, home: Path, settings_path: Path, args: Sequence[str] = ()
    ) -> HooksSwitch | None:
        """What switches every hook off for the agent in ``home``, launched with ``args``;
        ``None`` when nothing does. Read-only; never raises."""
        ...

    def decode_event(self, payload: Mapping[str, Any]) -> AgentEvent | None:
        """The hook payload as an :class:`AgentEvent`; ``None`` for one AISquare ignores.

        The event is the payload's ``hook_event_name`` (``aisquare hook <subcommand>``
        sets it from the :class:`HookSpec` that installed it, where the agent does not).
        """
        ...

    def encode_reply(self, kind: EventKind, reply: str | None) -> str:
        """What the hook prints for ``reply``: context for ``session_start`` and
        ``prompt``, a continuation reason for ``stop``; ``""`` prints nothing."""
        ...

    def mcp_args(
        self,
        executable: str,
        args: list[str],
        env_vars: list[str] | None = None,
    ) -> list[str]:
        """Launch arguments that register AISquare's MCP server for this session only."""
        ...

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
    def native_args(self, args: list[str]) -> list[str]:
        """``args`` with AISquare's spellings of native values made native (effort aliases)."""
        ...

    def native_choice(self, args: Sequence[str]) -> NativeChoice:
        """The model and effort ``args`` choose natively, the last one winning."""
        ...

    def validate(self, model: str | None, effort: str | None) -> None:
        """Raise :class:`BadEffortError` for an effort the agent cannot take."""
        ...

    def effort_alias(self, effort: str) -> str: ...
    def context_files(self, home: Path) -> tuple[Path, ...]:
        """The instruction files the agent reads from its config dir, in its precedence."""
        ...

    def model_args(self, model: str | None, effort: str | None) -> list[str]: ...
    def permission_args(self, intent: PermissionIntent, role: str) -> list[str]:
        """The native flags for ``intent``. Raises ``ValueError`` for one the agent
        cannot express."""
        ...

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
        """A fleet spawn's flags from the role's native permission settings, and its label."""
        ...

    def label_args(self, label: str) -> list[str]:
        """Flags that show the fleet label in the agent's UI; ``[]`` where it has none."""
        ...

    def resume_args(self, native_session_id: str, transcript_path: Path | None) -> list[str]:
        """Arguments that resume a session, put before every other argument."""
        ...

    def disable_native_teams(self) -> tuple[list[str], dict[str, str]]:
        """Arguments and environment that keep the agent's own sub-agent teams off."""
        ...

    def telemetry_args(self, endpoint: str, authorization: str) -> list[str]:
        """Arguments exporting the agent's OpenTelemetry logs to ``endpoint`` (OTLP/HTTP
        JSON, ``Authorization: <authorization>``); ``[]`` without ``native_telemetry``.
        Whether the operator configured an exporter of their own, which is kept, is
        ``services.native_telemetry.operator_configured`` (Codex's config layers)."""
        ...


@dataclass(frozen=True)
class NativeChoice:
    """What a native argv chooses: the model, the effort, and where an effort AISquare
    validates was given (``effort_index``): Claude Code's ``--effort`` takes AISquare's
    scale, so a saved one is AISquare's to refuse; ``None`` where the native CLI
    validates the value itself (Codex's ``-c model_reasoning_effort``)."""

    model: str | None = None
    effort: str | None = None
    effort_index: int | None = None


def hook_args(adapter: AgentAdapter, hook: HookSpec, home: Path) -> list[str]:
    """What ``aisquare hook`` is given for ``hook``: its subcommand, the agent and the
    event for :data:`GENERIC_HOOK`, and the config dir where the hook must be told it."""
    argv = [hook.command]
    if hook.command == GENERIC_HOOK:
        argv += ["--agent", adapter.id, "--event", hook.event]
    if adapter.capabilities.hook_names_home:
        argv += ["--config-dir", str(home)]
    return argv


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
    from aisquare.core.agent_adapters import get_adapter

    choice = get_adapter(agent).native_choice(args)
    return choice.model, choice.effort


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
