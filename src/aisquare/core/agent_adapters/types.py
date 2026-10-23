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
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, get_args

from aisquare.core import harness, paths

if TYPE_CHECKING:
    from aisquare.core.agent_adapters.transcripts import PendingTool, TranscriptTail
    from aisquare.core.claude_accounts import FailedTurn, LimitNotice
    from aisquare.core.claude_sharing import TrustScope
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


ScreenShape = Literal["trust", "chooser", "yn", "dialog"]


@dataclass(frozen=True)
class ScreenPrompt:
    """A prompt at the bottom of the agent's screen, and the keys that answer it
    (``AgentCapabilities.screen_prompt``): what the captain's press and paste ask of a pane
    before a key or a line goes into it."""

    shape: ScreenShape
    """``chooser`` (a numbered menu: its yes is the digit of its first option that says Yes,
    never a blind ``1``), ``yn`` (a ``[y/N]`` line), ``trust`` (the agent's folder-trust
    question: trusting a folder is the owner's, so no keys) or ``dialog`` (any other of the
    agent's own, whose keys mean something new each time: the owner's too, no keys)."""
    question: str
    yes_key: str | None
    """The tmux key that answers yes; ``None`` where none may (``trust``, ``dialog``) or the
    options hold no Yes."""
    no_key: str | None
    whole: str
    """Everything the prompt shows (what its question is about, the question, the options,
    the key hint) as one line, whitespace collapsed and the highlight dropped: the same
    prompt redrawn (the highlight moved, the pane resized) reads the same, and the next one,
    asking the same about another command, does not."""


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
    or the session ends, and a turn that failed with no hook at all (Codex 0.162.1 fires
    no ``Stop`` for one) when the fleet reads a row its board says is working
    (``accounts.failed_turn``). Claude Code reports both in ``StopFailure``."""
    ambient_first_run: bool = False
    """``agents connect`` makes a config dir the agent has not made yet only when it is
    the one a session from this shell reads (Claude Code: a typo must not get hooks);
    otherwise the dir as named (Codex: a fleet account is connected before its first run)."""
    reads_pane_readiness: bool = False
    """The fleet can tell from the pane that the agent sits at its prompt, so ``fleet
    tell`` types into a pane the listing reads as waiting; without it, only into one
    whose own hooks freshly say waiting (anything else is a board note)."""
    starts_at_its_prompt: bool = False
    """The agent's ``SessionStart`` fires as it comes to rest at its prompt, ready, and
    nothing fires before it: Claude Code's, once its folder-trust dialog is answered
    (measured on 2.1.300, where an Enter at that dialog picks its default, ``No, exit``).
    A session that starts (not a compaction) is then ``waiting`` until a prompt says
    otherwise, and one that has not started is not typed into. Codex's fires with its
    first prompt."""
    idle_prompt_mark: str | None = None
    """What starts the last line of the agent's screen while it sits at an EMPTY prompt
    (Codex's footer ``? for shortcuts``, measured on 0.162.1; text in the prompt replaces
    it, and a turn redraws the pane every second, the footer kept below an empty
    composer). For an agent without :attr:`reads_pane_readiness` whose hooks say nothing
    fresh — none fire before its first prompt — a pane quiet for ``ACTIVITY_WINDOW`` that
    shows it is at its prompt, so ``fleet tell`` and the manager's nudge type there; the
    agent's folder-trust and hook-review dialogs are as quiet and show no such line. And
    the line is asked for even when its hooks freshly say waiting, by them and by ``fleet
    stop``'s ``/exit``: Codex opens dialogs of its own after a turn's ``Stop`` (``Switch
    to gpt-6-luna for lower credit usage?``, past a turn near its limit), where an Enter
    picks for the human."""
    fleet_settings: tuple[str, ...] = ()
    """``-c`` settings (``key=value``, Codex's config overrides) the agent of a fleet window
    launches with, each unless a config layer of the operator's sets its key
    (``services.agent_launch.fleet_settings_args``): Codex's
    ``notice.hide_rate_limit_model_nudge=true``, which keeps its ``Switch to gpt-6-luna
    for lower credit usage?`` dialog, opened past a turn near the five-hour limit while
    its hooks say waiting, out of a pane the fleet types into (measured on 0.162.1), and
    ``check_for_update_on_startup=false``, which keeps its ``Update available`` dialog
    out of a starting pane: an npm-installed 0.162.1 opened it once 0.163.0 was out
    (CI on 72a88fc0), its first choice, which an Enter picks, running ``npm install
    -g @openai/codex``."""
    start_dialog_mark: str | None = None
    """What starts the last line of the agent's screen while a dialog it puts up before its
    start holds it for the human, where an Enter answers it: Claude Code's ``Enter to
    confirm``, the row under its folder-trust and MCP-server dialogs alike, the folder's
    defaulting to ``No, exit`` (measured on 2.1.300); the phrase elsewhere on the screen is
    the agent's output. A spawn's prompt for an agent whose start the board sees
    (:attr:`starts_at_its_prompt`) waits past ``PROMPT_TIMEOUT`` while none is on screen;
    one the board cannot see start (``--continue``, the board off) is not typed while one
    is, nor is ``fleet tell``."""
    resumes_across_homes: bool = False
    """A session resumes from another config dir (Claude Code finds a transcript by its
    absolute path), so ``fleet switch`` to another account resumes it; otherwise the
    thread lives in its home and the replacement starts fresh with a hand-off prompt."""
    resume_keeps_system_prompt: bool = False
    """A resumed session keeps what :meth:`AgentAdapter.system_prompt_args` gave its first
    launch and ignores what a resume passes (Codex's thread holds its first turn's
    ``developer_instructions``, measured on 0.162.1): a ``fleet restart`` or ``switch`` of a
    row whose persona changed since leaves its system prompt as it was, and the new persona
    reaches it through the session-start briefing alone. Claude Code takes the file again
    at every launch, a resume included."""
    pid_env: str | None = None
    """The variable in which the agent hands each hook its own pid (Claude Code's
    ``CLAUDE_PID``): the process identity ``services.team`` binds a fleet row to. Any such
    variable a hook of another agent finds is some other process's, inherited."""
    tool_shell_env: str | None = None
    """The variable the agent sets in the shells its tools run in, and never in its own
    hooks (Codex's ``CODEX_THREAD_ID``, measured on 0.162.1): a hook that finds it set
    belongs to a process one of those tools started (a nested ``codex exec``), never to
    the agent ``aisquare launch`` started, which clears it (``core.agent_sessions.
    own_process``; rule 1 of ``services.team``'s fleet-row section)."""
    exports_home_env: bool = True
    """A launch sets :attr:`AgentAdapter.home_env` to the config dir it resolved; Claude
    Code's ``CLAUDE_CONFIG_DIR`` stays as the shell or the binding wrote it."""
    native_telemetry: bool = False
    """The agent exports OpenTelemetry to a local receiver AISquare starts
    (:meth:`AgentAdapter.telemetry_args`). Remote's explainability card judges its sessions
    by that lane, not the proxy's (``services.explainability_ops._check_lane``)."""
    own_doctor_check: bool = False
    """``services.diagnostics`` has a dedicated check for this agent (Claude Code's hook
    timeouts, plugin route and config-dir siblings), so the per-adapter row skips it."""
    model_proxy: bool = False
    """Its model traffic goes through AISquare's explainability proxy (Claude Code's, by
    ``ANTHROPIC_BASE_URL``): the lane the doctor's proxy check and Remote's explainability
    card judge its sessions by (``services.explainability_ops._check_lane``)."""
    model_ladders: bool = False
    """AISquare's role model ladder and its probe choose the model (Claude Code); an
    agent without keeps its native default unless a model is pinned."""
    positional_prompt: bool = False
    """The first prompt is a positional argument (Codex); otherwise the fleet types it."""
    first_context_file_only: bool = False
    sandbox_permissions: bool = False
    """``fleet.roles.<role>.sandbox``/``approval_policy``/``approve_for_me`` apply."""
    launch_inputs: tuple[str, ...] = ()
    """The paths in a project's tree, relative to it, that THIS agent's launch reads and
    runs code from: its project settings, hooks, MCP servers, project config, agents
    (Claude Code's ``.claude``, ``.mcp.json``; Codex's ``.codex``, ``.agents``). A sandboxed
    agent's profile holds every registered agent's read-only at the tops of its trees
    (``services.store_broker.launch_inputs``), so a sandboxed Codex in a project cannot
    plant what another agent's next unsandboxed start there runs (review r10 of #113,
    finding 1; GATE #113). Every adapter declares its own; the conformance suite fails one
    that declares none."""
    sandboxes_tools: bool = False
    """The agent runs its tools' commands in an OS sandbox that confines their writes
    (Codex's). Its permission flags make the aisquare store read-only there
    (:meth:`AgentAdapter.store_access`), and every board call it makes there goes through
    its launch's broker, outside the sandbox (``services.store_broker``), whose inbox is
    the one path in the store its sandbox may write (:meth:`AgentAdapter.broker_access`).
    Claude Code runs its tools unsandboxed."""
    redraws_while_asking: bool = False
    """The agent's UI keeps redrawing while it waits on the human (Codex's approval view,
    every second, the screen unchanged: 0.162.1), so output in its pane answers nothing:
    an ``attention`` row holds until one of its hooks moves the row, or
    :attr:`working_on_screen` says its screen shows a turn running
    (``services.fleet._derive``). Claude Code's prompt sits still until it is answered."""
    working_on_screen: Callable[[Sequence[str]], bool] | None = None
    """Whether the agent's screen (its visible rows) shows a turn running unblocked:
    Codex's status line ``• Working (12s • esc to interrupt)`` where Codex draws it,
    measured on 0.162.1, which its approval view replaces until the human answers and
    which is back the moment they do. The answer's first hook is the approved command's
    PostToolUse, which fires only when the command finishes, so without it an approved
    12-minute command read ``attention`` throughout."""
    idle_on_screen: Callable[[Sequence[str]], bool] | None = None
    """Whether the agent's screen (its visible rows) shows it at its EMPTY prompt with
    nothing drawn over it, where a typed line and its Enter land as a prompt: read in
    place of :attr:`idle_prompt_mark` alone, which an agent may draw only in one of its
    layouts (Codex's ``? for shortcuts``, absent under ``--no-alt-screen``, measured on
    0.162.1). Remote's agent actions read the agent's screen by it too: a quiet pane at
    that prompt is at rest, and anything else drawn over the prompt (a dialog, a draft) is
    one they refuse to type into or ``/exit`` through (``services.remote_needs.
    _needs_screen_drawn``)."""
    one_press_exits: frozenset[str] = frozenset()
    """The keys, as tmux names them once :attr:`AgentAdapter.key_spellings` has spelled them,
    that end the agent with ONE press while its screen shows its empty prompt
    (:attr:`idle_on_screen`, else :attr:`idle_prompt_mark`): Codex's ``C-c`` and ``C-d``
    (measured on 0.162.1, after a turn and after one Esc as on a fresh start; ``C-d`` with a
    turn running too, where ``C-c`` opens its ``Cancel task`` / ``Exit`` choice instead;
    neither over a draft). Remote's ``send-keys`` refuses such a key, unless the sender
    confirms it, while the screen shows that prompt or cannot say
    (``services.remote_server.remote_one_press_exit``), as it refuses every agent a second
    ``C-c`` or ``C-d`` within 3 s. Empty for an agent that asks for a second press first:
    Claude Code says ``Press Ctrl-C again to exit``, and the same of Ctrl-D (2.1.304)."""
    dialog_on_screen: Callable[[Sequence[str]], str | None] | None = None
    """What a dialog of the agent's own on its screen (its visible rows, as plain text) asks:
    the words right above its options, ``""`` when none are on screen, and ``None`` for a
    screen with no such dialog on it: its empty prompt, a turn running, a draft in its
    composer, its transcript browser. Codex's folder-trust and hook-review dialogs before its
    first prompt, its ``Switch to gpt-6-luna for lower credit usage?`` after a turn's
    ``Stop`` and ``Implement this plan?`` after a Plan-mode turn's, fire no hook (measured
    on 0.162.1), so the board never hears of them: Remote's needs-you scan reads the screen
    of such an agent with no call pending and no bell, and makes a card of the dialog it
    shows (``services.remote_needs._needs_screen_dialog_item``). ``None`` here for an agent
    whose dialogs each reach the board through a hook (Claude Code's ``Notification``): its
    screen is not read for one."""
    screen_prompt: Callable[[Sequence[str]], ScreenPrompt | None] | None = None
    """The prompt at the bottom of the agent's screen (its visible rows, as ``capture-pane``
    gives them), read by its structure, or ``None`` for a screen with none
    (:class:`ScreenPrompt`): what the captain asks of the pane's own agent before it presses
    a key there (``press yes`` sends ``yes_key``) or pastes a line (refused at a ``trust``
    prompt, whose answer is the owner's). Claude Code's input box, chooser and trust dialog
    (``claude_screen.prompt_showing``); Codex's dialogs as ``dialog_on_screen`` reads them,
    its approval a chooser. ``None`` here for an agent whose screen is not read for one."""
    ready_on_screen: Callable[[Sequence[str]], bool] | None = None
    """Whether the agent's screen (its visible rows) shows its prompt drawn, with no turn
    running at it and nothing drawn over it, so a line typed there and its Enter land as a
    prompt (a draft may sit in it): the captain's evidence that a pane the fleet still reads
    ``working`` is ready (a fresh Claude Code reads working until its first ``Stop``), for
    its ``say`` and its ``press`` and ``paste`` (:func:`screen_ready`). Claude Code's input
    box drawn and idle (``claude_screen.box_idle``). ``None`` takes the agent's empty prompt
    (:attr:`idle_on_screen`, else :attr:`idle_prompt_mark` on its last line) with no turn
    running on it (:attr:`working_on_screen`): Codex's. Remote never reads it, its own
    reads being :attr:`idle_on_screen`'s."""
    legacy_fleet_args: bool = False
    """``fleet.roles.<role>.extra_args`` and ``permission_mode`` are in this agent's
    vocabulary (they predate adapters and are Claude Code's); another agent takes only
    an explicitly passed mode and its ``agent_args``."""
    value_options: frozenset[str] = frozenset()
    switch_options: frozenset[str] = frozenset()


def screen_ready(capabilities: AgentCapabilities, rows: Sequence[str]) -> bool:
    """Whether ``rows``, an agent's screen, show it ready for a typed line: its
    :attr:`~AgentCapabilities.ready_on_screen`, else its empty prompt (``idle_on_screen``,
    else ``idle_prompt_mark`` starting its last line) with no turn running on it
    (``working_on_screen``); ``False`` for an agent that reads none of them off its screen."""
    if capabilities.ready_on_screen is not None:
        return capabilities.ready_on_screen(rows)
    if capabilities.idle_on_screen is not None:
        idle = capabilities.idle_on_screen(rows)
    else:
        mark = capabilities.idle_prompt_mark
        last = next((row.strip() for row in reversed(rows) if row.strip()), "")
        idle = bool(mark) and last.startswith(mark or "")
    working = capabilities.working_on_screen
    return idle and not (working is not None and working(rows))


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

    @staticmethod
    def written(value: str) -> bool:
        """Whether ``value`` is written as an intent (``write:approvals``) rather than in an
        agent's own words (Claude Code's ``auto``, Codex's ``read-only``): no native
        permission value has a colon."""
        return ":" in value

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


@dataclass(frozen=True)
class SandboxWrites:
    """What a sandboxed session may write: ``writable`` roots, with ``read_only`` paths
    within them held read-only (the more specific path wins, as Codex's profile rules it).
    Of the roots, ``workspace`` is the session's directory and ``temporary`` the
    temporary directories every such sandbox writes; the rest were added (a directory
    given it, a writable root, a profile's ``write`` path)."""

    writable: tuple[Path, ...]
    read_only: tuple[Path, ...] = ()
    workspace: Path | None = None
    temporary: tuple[Path, ...] = ()

    def root_of(self, path: Path) -> Path | None:
        """The most specific of :attr:`writable` that holds ``path``, or ``None``."""
        holding = [root for root in self.writable if path == root or path.is_relative_to(root)]
        return max(holding, key=lambda root: len(root.parts), default=None)

    def may_write(self, path: Path) -> bool:
        """Whether a write at ``path`` is allowed: under a writable root, and under no
        read-only path more specific than that root."""

        def depth(root: Path) -> int | None:
            return len(root.parts) if path == root or path.is_relative_to(root) else None

        best = max((d for root in self.writable if (d := depth(root)) is not None), default=None)
        if best is None:
            return False
        return not any((d := depth(root)) is not None and d > best for root in self.read_only)


def store_dir() -> Path:
    """The aisquare home a launch from this process gives its agent, and so the store the
    agent's ``aisquare`` calls open: what :meth:`AgentAdapter.store_access` makes reachable."""
    home = paths.expand_user(paths.aisquare_home())
    try:
        return home.resolve()
    except (OSError, RuntimeError):
        return home.absolute()


def store_read_only(store: Path) -> tuple[Path, ...]:
    """What under the aisquare home ``store`` a sandboxed agent may read and not write: all
    of it, the store's database first. So nothing a process in its sandbox writes reaches
    the board, the launch records, the census, the config, the account homes, the model
    probe's cache or anything else there, and nothing there that chooses what runs outside
    the sandbox (a replay, a kill, a typed line, a launch, a model or an account, a
    worktree removal, a paste) can have been written from inside one. The one path there
    the sandbox may write is its own launch's broker inbox
    (:meth:`AgentAdapter.broker_access`), whose requests the broker runs only from its
    whitelist, as the launch's own sessions (``services.store_broker``)."""
    return (store,)


def prepare_store(store: Path) -> None:
    """Make the store its profile names before the agent starts, and the launch records'
    directory (``launch_seal.ensure_dir``): a fleet agent's own record was written before
    its window started, and an agent that sandboxes its tools is not started without one.
    The broker's inbox, the profile's one writable path there, is made by
    ``services.store_broker.start_for_launch``. A path the profile names that does not exist
    is no harm: measured on 0.162.1 in a real turn (the worktree layout, a path inside the
    workspace and outside every writable root, an absent parent too), the sandbox shows it
    as an empty read-only mount point and leaves nothing on the host (review r11 of #113,
    finding 3). Never raises."""
    from aisquare.core import launch_seal

    with suppress(OSError):
        store.mkdir(parents=True, exist_ok=True)
    launch_seal.ensure_dir()


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


CallAsk = Literal["tool", "question", "plan", "subagent"]
"""What a pending call waits on the human for (:meth:`AgentTranscript.call_asks`)."""

AnswerKind = Literal["permission", "question", "plan"]
"""The cards a phone answers with keys (:meth:`AgentTranscript.answer_keys`)."""


@dataclass(frozen=True)
class AnswerKey:
    """One quick answer a card offers: its label, and the tmux key names it sends."""

    label: str
    keys: tuple[str, ...]


class AgentTranscript(Protocol):
    """One agent's session transcript (the path its hooks report, ``TeamSession.
    transcript_path``), read for Remote: the needs-you scan's facts
    (``services.remote_needs``) and the Transcript tab's pages (``services.transcript``).

    ``services.transcript`` opens the file (a regular one only, never a pipe), bounds what
    is read of it and pages it; the reader decodes the agent's own records. A record is one
    JSON object a line, its ``timestamp`` ISO 8601 (:func:`~aisquare.core.agent_adapters.
    transcripts.record_time`). The shared shapes are in ``core.agent_adapters.transcripts``.
    """

    speaker: str
    """How a page names the agent's own turns: ``* <speaker>`` (``claude``, ``codex``)."""

    def tail_facts(self, file: Path, size: int, budget: int, written: datetime) -> TranscriptTail:
        """What the end of the transcript says (SPEC §4.3): the calls still waiting for their
        result, the newest record's kind (``TranscriptTail.newest``), the agent's newest
        words, and ``cut_at`` = ``written`` (the file's mtime) for a walk that ran out of
        ``budget`` bytes back from ``size``, or of ``transcripts.TAIL_RECORDS``, before it saw
        the newest model response whole, while that response calls tools and the turn has
        not ended (a call of it may still wait unseen). A turn seen to end (its last
        words, an interruption) waits on nothing however much is unread. Reads only the
        tail (``transcripts.lines_backwards``), a line past ``transcripts.MAX_LINE`` off its
        bytes alone; may raise ``OSError``."""
        ...

    def page_lines(self, record: dict[str, Any], width: int) -> list[str]:
        """One record as the page's terminal lines, wrapped to ``width`` columns: the human's
        words under ``transcripts.YOU``, the agent's under ``transcripts.speaker_line``, a
        call as one dim line (a question or a plan in full); ``[]`` for a record that is
        not conversation (results, thinking, the agent's own injected context)."""
        ...

    def call_asks(self, tool: PendingTool) -> CallAsk:
        """What a pending call waits on the human for: ``question`` (its dialog is the call
        itself; ``input["questions"]`` holds them as ``AskUserQuestion`` does: ``header``,
        ``question``, ``multiSelect``, ``options`` of ``label`` and ``description``),
        ``plan`` (``input["plan"]``, a plan to approve), ``subagent`` (a sub-agent runs
        inside it, and a prompt while it is pending is the sub-agent's) or ``tool`` (a
        permission prompt, when the board's attention says one is up)."""
        ...

    def call_mark(self, tool: PendingTool) -> str | None:
        """What the agent's prompt for the call shows of it (a command, a file's base name, a
        question's words), by which the needs scan tells which of several pending calls
        a dialog on screen is for; ``None`` for a call whose prompt is not known to."""
        ...

    def answer_keys(self, kind: AnswerKind, options: int = 0) -> tuple[AnswerKey, ...]:
        """The quick answers of a card of ``kind`` on this agent's dialog: for ``question``,
        first the key that picks each of its ``options`` (in their order; the card labels
        them), then the rest. ``()`` where the agent has no such dialog."""
        ...

    def reply_since(self, file: Path, since: datetime | None) -> str | None:
        """The agent's words since the transcript's newest prompt of the human's: every
        message of the turn that answers it, in order, joined by newlines (a call's own
        records are no words), for the captain's reply to what the owner just said. ``""``
        is an answer without words. ``None`` with no prompt, while nothing of the agent's
        follows it, or when it is older than ``since`` (when it was said: the words have not
        reached the transcript yet; a prompt with no time counts). Reads back from the end,
        far enough to find that prompt behind one turn's tool results. Never raises."""
        ...


class AgentAdapter(Protocol):
    """One terminal agent, in the terms the shared services ask in.

    Identity and installation: ``id`` (the board's and the config's name), ``label``,
    ``binary``, ``home_env``/``home_name`` with :meth:`config_dir`, ``install_hint``,
    ``version_args`` with :func:`version_in`. Hooks: ``settings_name`` (the hooks file in
    the config dir), ``capabilities.hooks`` with :func:`hook_args`, ``hooks_off_setting``
    with :meth:`hooks_switch`, ``plugin``. Events: :meth:`decode_event` and
    :meth:`encode_reply`. Launch: :meth:`model_args`, :meth:`permission_args`,
    :meth:`store_access`, :meth:`sandbox_writes`, :meth:`broker_access`, :meth:`fleet_args`,
    :meth:`label_args`, :meth:`resume_args`, :meth:`fork_args`,
    ``exit_command``, ``dismiss_key``, :meth:`queue_args`,
    :meth:`mcp_args`, :meth:`tools_only_args`, :meth:`context_files`,
    :meth:`disable_native_teams`, :meth:`unattended_env`,
    :meth:`telemetry_args`. Personas: :meth:`system_prompt_args`, :meth:`skill_dirs`,
    :meth:`headless_call` with :meth:`headless_answer`. Accounts: ``accounts``.
    Remote: ``transcript_reader``. ``capabilities`` says the rest.
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
    dismiss_key: str
    """The tmux key that closes a dialog of the agent's own with its least answer (No to a
    permission prompt, back out of Codex's ``Switch to gpt-6-luna?`` offer) and leaves it
    at its prompt (``Escape``). The automatic usage-limit hand-over presses it once where
    the agent's screen shows anything but its empty prompt, then types
    :attr:`exit_command` once that prompt is back, as Remote's ``dismiss_dialog`` does
    (``services.fleet._dismissed``): a window killed instead fires no ``SessionEnd``, and
    the session it leaves stays on the board, parked on its limit. Never ``Enter``, which
    answers a dialog with its highlighted choice."""
    key_spellings: Mapping[str, str]
    """tmux key names this agent reads under another spelling, which the fleet UI's pane and
    Remote's key pad send instead (``core.keys.translate``, ``services.remote_server.
    remote_keys_spelled``): Codex submits on ``S-Enter`` and takes ``C-j`` as its newline.
    Empty where every key means what tmux names it."""

    @property
    def accounts(self) -> AgentAccounts | None:
        """Numbered login slots (``aisquare accounts``); ``None`` for an agent without them."""
        ...

    @property
    def plugin(self) -> AgentPlugin | None:
        """The aisquare plugin route for this agent; ``None`` when there is none."""
        ...

    @property
    def transcript_reader(self) -> AgentTranscript | None:
        """The reader of the agent's session transcript, for Remote's needs-you cards and
        Transcript tab; ``None`` for an agent whose transcript AISquare cannot read (its
        rows then have no card a transcript decides, and an empty page)."""
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

    def browser_tools(self, home: Path) -> list[str] | None:
        """The browser tooling the agent brings itself in config dir ``home``, as the
        doctor names it (empty: none, or switched off); ``None`` for an agent whose
        browser the doctor finds another way (Claude Code's MCP servers and plugins,
        ``services.diagnostics``). Read-only; never raises."""
        ...

    def model_args(self, model: str | None, effort: str | None) -> list[str]: ...
    def role_defaults(self, role: str, *, binary: str, args: Sequence[str]) -> harness.RoleDefaults:
        """The flags ``role`` needs on this agent wherever it starts (the ui-tester's
        browser), less what ``args`` already says, with a note for each it withholds."""
        ...

    def permission_args(
        self, intent: PermissionIntent, role: str, native: Sequence[str] = ()
    ) -> list[str]:
        """The native flags for ``intent``, less what the ``native`` arguments the launch
        already carries settle (they win, as an explicit native flag always does), with
        :meth:`store_access` for its write scope wherever its flags choose the sandbox.
        Raises ``ValueError`` for an intent the agent cannot express."""
        ...

    def store_access(self, write: WriteScope, store: Path) -> list[str]:
        """Launch arguments that run a session sandboxed at ``write`` with the aisquare
        store in ``store`` (:func:`store_dir`) read-only in its sandbox, all of it
        (:func:`store_read_only`); its board calls go through its broker
        (:meth:`broker_access`). ``[]`` without ``capabilities.sandboxes_tools`` (nothing
        stands between them) and for a scope the agent runs unsandboxed (``full``)."""
        ...

    def sandbox_writes(
        self, args: Sequence[str], cwd: Path, home: Path | None = None
    ) -> SandboxWrites | None:
        """What the sandbox of a session launched with ``args`` in ``cwd`` may write, read
        as the agent reads its settings: its config in ``home`` (its config dir; ``None``
        is this shell's), the profile ``args`` select, then ``args`` themselves, whatever
        chose the sandbox (the store's profile, a native flag or setting, the directories
        added); ``None`` where the session runs unsandboxed.
        ``services.store_broker.refuse_inside`` refuses a launch whose sandbox may write
        the aisquare its hooks and broker run."""
        ...

    def broker_access(
        self,
        args: Sequence[str],
        store: Path,
        inbox: Path,
        *,
        protect: Sequence[Path] = (),
        writable: Sequence[Path] = (),
    ) -> list[str] | None:
        """``args`` with ``inbox``, the launch's broker inbox (``services.store_broker``),
        the one path under ``store`` the sandbox may write, and the rest of the store
        read-only whatever ``args`` recorded (a launch spec from before this rule); every
        launch of an agent with ``capabilities.sandboxes_tools`` asks this before it
        starts. ``protect`` is read-only there too, outside the store, whether or not it
        exists yet (the launch inputs of the project's trees, the other agents' worktrees,
        the tmux sockets' directory: ``services.store_broker.launch_paths``), and
        ``writable`` stays writable within it (the agent's own worktree), the more specific
        path winning. ``None`` where ``args`` run no sandbox of :meth:`store_access`'s (the
        agent unsandboxed, or a sandbox the operator chose), which no broker serves."""
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

    def fork_args(self, native_session_id: str, transcript_path: Path | None) -> ForkArgs | None:
        """Arguments that start a NEW session carrying this one's conversation, which goes on
        unchanged (a hand-off's Fork; :class:`ForkArgs`); ``None`` for an agent that cannot
        fork one, whose fork starts fresh with a hand-off prompt instead. Run in the session's
        own config dir unless ``capabilities.resumes_across_homes``."""
        ...

    def tools_only_args(
        self, server: str, command: str, args: list[str], env: dict[str, str]
    ) -> list[str] | None:
        """Launch arguments that leave the session no tool but those of the one MCP server
        ``server`` (run as ``command`` with ``args`` and ``env``), each of its tools allowed
        without asking: the captain's session. ``None`` for an agent that cannot be confined
        so, which then cannot be the captain."""
        ...

    def queue_args(self, native_session_id: str, message: str) -> list[str] | None:
        """Arguments to the agent's binary that queue ``message`` for its RUNNING session,
        delivered as the next turn once the current one ends (``fleet tell`` to a busy
        agent), run with :attr:`home_env` set to the session's config dir; ``None`` for
        an agent without such a queue (the message is then a board note)."""
        ...

    def disable_native_teams(self) -> tuple[list[str], dict[str, str]]:
        """Arguments and environment that keep the agent's own sub-agent teams off."""
        ...

    def unattended_env(self) -> Mapping[str, str]:
        """Environment for a session nobody may be watching (the captain's, whose pane is
        answered from a phone), which keeps out a prompt of the agent's own that would hold
        its pane for the human and is no work's (Claude Code's session-rating survey);
        empty where none is kept out this way."""
        ...

    def telemetry_args(self, endpoint: str, *, keep_out: Sequence[str] = ()) -> list[str]:
        """Arguments exporting the agent's OpenTelemetry logs to ``endpoint`` (OTLP/HTTP
        JSON); ``[]`` without ``native_telemetry``. Never the receiver's ``Authorization``:
        argv is every local user's to read, so the header rides in the launch environment
        (``services.native_telemetry.HEADERS_ENV``), and ``keep_out`` names the variables
        the shells the agent's tools run in must not inherit, that one among them. Whether
        the operator configured an exporter of their own, which is kept, is
        ``services.native_telemetry.operator_configured`` (Codex's config layers)."""
        ...

    def system_prompt_args(
        self, block: Path, text: str, args: Sequence[str], home: Path
    ) -> list[str] | None:
        """Launch arguments that add ``text`` to the agent's system prompt for the session,
        beside its own (``aisquare launch --persona``: the persona's briefing, which the file
        ``block`` holds), for an agent launched with ``args`` from config dir ``home``.
        ``None`` where ``args`` already set that seam, the operator's, or where the agent has
        none: the session-start hook alone briefs the persona then. An agent that takes the
        text itself rather than the file puts it in argv, which every local user can read:
        a persona is no secret, and its 12,000-character cap sits far under ``ARG_MAX``."""
        ...

    def skill_dirs(self, home: Path, repo: Path | None) -> SkillDirs:
        """Where a session of config dir ``home`` reads skills, the user's and, in
        ``repo`` (``None``: outside one), the project's (:class:`SkillDirs`): absolute, in the
        agent's own order."""
        ...

    def headless_call(self, schema: Path, out: Path, model: str | None) -> list[str] | None:
        """Arguments to the agent's binary for ONE headless turn whose answer is JSON that
        the JSON Schema in the file ``schema`` describes (``persona import --engine
        manager``), with ``model`` pinned where given: the instructions and the source go on
        its stdin, it keeps no session, its hooks are off where a launch argument can say so,
        and :meth:`headless_answer` reads its answer from what it printed or wrote to
        ``out``. ``None`` for an agent without such a call: the engine ladder goes on to the
        next engine. Run with the environment of the agent's manager account."""
        ...

    def headless_answer(self, stdout: str, out: Path) -> object | None:
        """The JSON answer of a :meth:`headless_call` that exited 0, from its ``stdout`` or
        the file ``out``; ``None`` when there is none (no JSON, an error the agent reported).
        Never raises."""
        ...


@dataclass(frozen=True)
class ForkArgs:
    """How one session forks into a new one that carries its conversation
    (:meth:`AgentAdapter.fork_args`): ``args``, put first as :meth:`AgentAdapter.resume_args`
    are, and ``mints_id``, whether the launch names the new session itself (Claude Code's
    ``--session-id <new>`` beside ``--fork-session``, with ``capabilities.
    assigns_session_id``) or the agent's first hook tells it (Codex's)."""

    args: tuple[str, ...]
    mints_id: bool


@dataclass(frozen=True)
class SkillDirs:
    """Where an agent reads skills, one ``<name>/SKILL.md`` folder each
    (:meth:`AgentAdapter.skill_dirs`): ``user``, the dirs every session of a config dir
    reads, and ``project``, those a session in the repository reads, each in the agent's
    own order. The first of each is where ``persona export --skill`` writes; ``persona
    import --list`` reads them all, the user's first."""

    user: tuple[Path, ...]
    project: tuple[Path, ...] = ()


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

    def failed_turn(self, path: Path) -> FailedTurn | None:
        """The transcript's newest turn when it ended on an error that fired no hook, or
        ``None`` (an agent whose hooks report every failure: ``None`` always). Read on
        every fleet listing of a working row, so it reads only the transcript's end."""
        ...

    def login_fingerprint(self, account: ClaudeAccount) -> str | None:
        """A digest of the login stored for ``account`` (its tokens), never a token: what a
        sign-in window opened over a login still on disk waits to see change, so that login
        does not count as the new one at the first poll
        (``services.claude_accounts.sign_in_landed``). ``None`` when no usable login is
        stored. Read afresh. Raises ``core.claude_accounts.LoginUnreadable`` with the reason
        when the store would not say (a locked Keychain): ``None`` there would let the
        login already stored land."""
        ...

    @property
    def trust(self) -> FolderTrust | None:
        """Where the agent records that the human trusts a folder, per account; ``None``
        for an agent that asks nothing per folder (no trust to share between accounts)."""
        ...


class FolderTrust(Protocol):
    """One agent's folder-trust records, one set per account home: what ``[accounts]
    share_trust``, ``accounts trust`` and ``--sync`` copy between that agent's accounts
    (``services.claude_accounts.share_for_launch``), so an agent on an account that was
    never asked does not stop at the question a yes on another account answered.

    ``file_name`` names the file that holds them in a note. A scope's ``folder`` is the
    folder resolved and its ``key`` what a yes there records, the canonical repository
    root in a git checkout, else the folder (``core.claude_sharing.trust_scope``, which
    both Claude Code and Codex 0.162.1 record by): :meth:`scope` reads it in the agent's
    own rules. The records read as ``{key: yes}``, ``False`` for a decision against it
    that a share never overrides."""

    file_name: str

    def scope(self, base: TrustScope) -> TrustScope:
        """``base`` in this agent's rules: its spelling of ``key``, and in ``covering`` every
        entry that decides whether it asks in ``folder``, in the order the agent reads them,
        the first one there deciding (:func:`trusted_by`)."""
        ...

    def read(self, account: ClaudeAccount) -> dict[str, bool]:
        """The account's records, ``{key: trusted}`` in the file's order. Raises
        ``core.claude_sharing.SharingError`` saying why they cannot be read, its
        ``NotStartedYet`` when there is nothing to add to yet."""
        ...

    def grant(self, account: ClaudeAccount, *keys: str) -> list[str]:
        """Record a yes for each of ``keys`` in the account's file; those written. A key
        already trusted is left out, and nothing else in the file changes. Raises
        ``core.claude_sharing.SharingError``, the file left as it was."""
        ...


def trusted_by(records: Mapping[str, bool], scope: TrustScope) -> str | None:
    """The entry of ``records`` (:meth:`FolderTrust.read`) that trusts ``scope``'s folder,
    or ``None``: the first of ``scope.covering`` the records hold decides."""
    for key in scope.covering:
        said = records.get(key)
        if said is not None:
            return key if said else None
    return None
