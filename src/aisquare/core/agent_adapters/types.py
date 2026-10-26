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
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, get_args

from aisquare.core import harness, paths

if TYPE_CHECKING:
    from aisquare.core.agent_adapters.transcripts import PendingTool, TranscriptTail
    from aisquare.core.claude_accounts import FailedTurn, LimitNotice
    from aisquare.core.claude_sharing import TrustScope
    from aisquare.core.config import FleetSettings
    from aisquare.core.ledger import SessionLedger
    from aisquare.core.pricing import Price, RequestTokens
    from aisquare.core.usage import Usage
    from aisquare.models import ClaudeAccount, ClaudeIdentity, ClaudeUsage


@dataclass(frozen=True)
class HookSpec:
    """One native hook event AISquare installs, and the ``aisquare hook`` subcommand it runs.

    ``command`` is the subcommand: Claude Code's are one per event (``session-start``…),
    Codex's is ``codex`` for every event, and a new agent's is :data:`GENERIC_HOOK`
    (``aisquare hook event --agent <id>``), so it needs no subcommand of its own.
    ``timeout`` is seconds, ``None`` for the agent's default, written in the agent's own
    unit by :meth:`AgentAdapter.hook_entry`; ``matcher`` narrows the event where the
    agent's hook format has one (Codex's ``PreToolUse``).
    """

    event: str
    command: str
    timeout: int | None = None
    matcher: str | None = None


@dataclass(frozen=True)
class LaunchPart:
    """What one launch slot adds to a launch (:meth:`AgentAdapter.mcp_args`, ``label_args``,
    ``telemetry_args``, ``fleet_settings``, ``disable_native_teams``): ``args`` for the
    agent's command line, ``env`` for its process, and ``settings`` for its per-launch
    settings layer (:meth:`AgentAdapter.settings_layer`), each where the agent takes it.
    Claude Code's and Codex's slots answer arguments (``--mcp-config``, ``-c``); an agent
    whose only seams are its environment and its settings files (Gemini CLI's
    ``mcpServers``, ``experimental.enableAgents``, ``CLI_TITLE``) answers those. ``notes``
    are what the launch prints of it, as a receipt: a setting it left to the operator's
    administrator (Codex's ``fleet_settings`` under a managed config that sets one)."""

    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    settings: Mapping[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.args or self.env or self.settings)


def merged_settings(*layers: Mapping[str, Any]) -> dict[str, Any]:
    """``layers`` as one settings document, table by table, a later value winning: what one
    launch's :class:`LaunchPart` settings make of its settings layer."""
    result: dict[str, Any] = {}
    for layer in layers:
        for key, value in layer.items():
            mine = result.get(key)
            if isinstance(mine, Mapping) and isinstance(value, Mapping):
                result[key] = merged_settings(mine, value)
            else:
                result[key] = dict(value) if isinstance(value, Mapping) else value
    return result


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


def command_entry(spec: HookSpec, command: str) -> dict[str, Any]:
    """One hook handler as Claude Code's ``settings.json`` and Codex's ``hooks.json`` both
    write it: ``{"type": "command", "command": …, "timeout": <seconds>}``, the timeout only
    where ``spec`` has one. What :meth:`AgentAdapter.hook_entry` answers for an agent whose
    hooks file has that shape."""
    entry: dict[str, Any] = {"type": "command", "command": command}
    if spec.timeout is not None:
        entry["timeout"] = spec.timeout
    return entry


@dataclass(frozen=True)
class InstallRoute:
    """One way to install an agent, best first in :attr:`AgentAdapter.install_routes`: how
    (``installer``, ``Homebrew``, ``npm``), the command a terminal runs, and the platforms
    (``sys.platform``) it is offered on, every one where empty. Welcome's step 2 offers them
    (``services.first_run.install_routes``); on Windows, where the fleet runs inside WSL2,
    it offers the first alone, to run there."""

    how: str
    command: str
    platforms: frozenset[str] = frozenset()


LastingKind = Literal["mcp", "plugin"]


@dataclass(frozen=True)
class LastingInstall:
    """Something of aisquare's that one of an agent's homes keeps in a file the agent owns,
    which ``aisquare uninstall`` lists and never edits (a running session of the agent
    rewrites that file too), naming the agent's own command that removes it
    (:meth:`AgentAdapter.lasting_installs`): an MCP server that runs aisquare (``mcp``), the
    aisquare plugin enabled (``plugin``)."""

    agent: str
    kind: LastingKind
    name: str
    """The server's name; the plugin's id as the agent's command takes it."""
    file: Path
    """The file that records it, itself (``core.agents.dir_identity``): one file reached
    through two homes is one entry, and a link a purge deletes never stands for a file it
    leaves."""
    home: Path
    """The agent's config dir it was found for, which :attr:`remove` acts on."""
    remove: str
    """The agent's own command that removes it, aimed at :attr:`home` and, for a project's
    entry, run inside the project: a line to paste, built when the adapter answered."""
    fetches: bool = False
    """It starts aisquare even once the package is gone (a server run through a runner that
    fetches the package, ``uvx``), and so makes the aisquare home again: a purge it would
    undo is refused until it is removed. A plugin always does; its launcher fetches."""
    project: str | None = None
    """The project it is for, as the agent's file spells it; ``None`` for the home's own."""
    scope: str = "user"
    """Where it is enabled: ``user`` (the home), or ``project``/``local`` (a repository)."""
    version: str | None = None
    """A plugin's version as its install records it; ``None`` when none is recorded."""
    shared: bool = False
    """:attr:`file` is the project's own, committed beside its code (Claude Code's
    ``.mcp.json``), which every session there reads: where it is, it names itself."""
    asked_in: tuple[Path, ...] = ()
    """For a :attr:`shared` entry, the files of the agent's homes (each itself) whose
    sessions start it or ask to: once a purge deletes every one, nothing starts it."""


@dataclass(frozen=True)
class SessionFlags:
    """How a launch names or picks the session it runs, in the agent's own flags, for an agent
    whose session AISquare names at launch (``AgentCapabilities.assigns_session_id``): Claude
    Code's ``--session-id <uuid>``, ``--resume``/``-r [<id>]``, ``--continue``/``-c`` and
    ``--fork-session``, each as ``--flag value`` or ``--flag=value``. The session planner
    reads a choice the caller made from them before it names one
    (``services.explainability.plan_session_identity``), names a fresh start's by
    :attr:`names` (``minted_identity``), and a fleet row records its arguments without them,
    which are per launch (``without_session_choice``)."""

    names: str
    """The flag that takes the new session's id: a fresh start's, and a fork's that names the
    new session itself (``ForkArgs.mints_id``)."""
    resumes: tuple[str, ...] = ()
    """Flags that take a session to resume (its id, or its transcript's path), or, with no
    value, pick one at run time."""
    continues: tuple[str, ...] = ()
    """Flags, taking no value, that resume a session chosen at run time."""
    forks: tuple[str, ...] = ()
    """Flags, taking no value, meaningful only beside a resume: its conversation under a new
    id."""

    def minted(self, session_id: str) -> tuple[str, ...]:
        """The arguments that start the new session ``session_id``: :attr:`names` and it."""
        return (self.names, session_id)


@dataclass(frozen=True)
class Vocabulary:
    """The agent's own words that none of its other declarations names, which a module outside
    ``core/agent_adapters`` must not spell any more than those: tests/test_no_agent_literals.py
    reads them with its ``id``, ``binary``, its variables (``home_env``, its accounts' and its
    capabilities', and those its launch slots set), its files (``home_name``,
    ``settings_name``, ``launch_inputs``, ``instruction_files``, its accounts' ``dir_name``), so a
    new agent's words in a shared module fail with their file and line and nothing of that
    guard is edited for it."""

    env_prefixes: tuple[str, ...] = ()
    """The prefixes of the variables it reads beyond its home's (whose first word counts
    already): its model provider's, Claude Code's ``ANTHROPIC``, Codex's ``OPENAI``."""
    files: tuple[str, ...] = ()
    """Its own files and folders beyond those: Claude Code's ``.claude.json`` and
    ``.credentials.json``, Codex's ``auth.json``."""
    flags: tuple[str, ...] = ()
    """The flags of its own that a shared module would spell to drive it (Claude Code's
    ``--permission-mode``, Codex's ``--ask-for-approval``)."""


@dataclass(frozen=True)
class CompactCommand:
    """How an agent's session is compacted from its prompt (``capabilities.compact``)."""

    line: str
    """What is typed: Claude Code's and Codex's ``/compact``."""
    instructions: bool
    """Whether text after it is read as what the summary keeps (Claude Code's). Codex runs
    ``/compact <text>`` as a prompt, a turn and no compaction, so it is typed bare (measured
    on 0.164.0): its summary is its own, and the start pack that comes back with its next
    turn restores what the instructions keep."""


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
    it to the launch (``core.agent_sessions.bind_launch_session``). Its flags are
    :attr:`session_flags`."""
    vocabulary: Vocabulary = Vocabulary()
    """Its own words no other declaration of its names (:class:`Vocabulary`), which no shared
    module spells: what the guard on agent-specific code reads beside them."""
    session_flags: SessionFlags | None = None
    """The agent's flags that name or pick a launch's session (:class:`SessionFlags`), which
    an agent with :attr:`assigns_session_id` declares: the session it is handed is named in
    them, never in another agent's."""
    requires_hook_trust: bool = False
    """The agent runs a hook only once the user trusts its definition (Codex's
    ``/hooks``): the installed commands carry ``--definition <fingerprint>`` and the
    integration reads ``unverified`` until AISquare sees that definition run."""
    hook_names_home: bool = False
    """The installed hook command names its config dir (``--config-dir``): the agent
    does not tell its hooks which home they run for. The dir is then the session's
    account; otherwise the account is read off the transcript path
    (``<config dir>/projects/<slug>/<id>.jsonl``), else the agent's :attr:`AgentAdapter.
    home_env` in the hook's environment (``services.team.session_account``)."""
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
    tool_shell_vars: tuple[str, ...] = ()
    """Every other variable the agent sets in its tools' shells for that one session, call,
    sandbox or install (Codex's ``CODEX_SESSION_ID``, ``CODEX_TOOL_CALL_ID``, ``CODEX_CI``,
    ``CODEX_SANDBOX_NETWORK_DISABLED``, …): a tmux server started from such a shell pinned
    them for every window opened on it, and each agent there inherited the spawner's
    (SMOKE8 of #113). Stripped from every tmux client with :attr:`tool_shell_env`
    (``core.spawn.agent_identity_env_vars``), and from the agent ``aisquare launch`` starts.
    A shell holding any of them is that agent's tool shell, whose
    :attr:`AgentAdapter.home_env` is the spawner's own (``core.spawn.tool_shell_homes``)."""
    exports_home_env: bool = True
    """A launch sets :attr:`AgentAdapter.home_env` to the config dir it resolved; Claude
    Code's ``CLAUDE_CONFIG_DIR`` stays as the shell or the binding wrote it."""
    native_telemetry: bool = False
    """The agent exports OpenTelemetry to a local receiver AISquare starts
    (:meth:`AgentAdapter.telemetry_args`). Remote's explainability card judges its sessions
    by that lane, not the proxy's (``services.explainability_ops._check_lane``)."""
    own_doctor_check: bool = False
    """``services.diagnostics`` has a dedicated check for this agent (Claude Code's hook
    timeouts, plugin route and config-dir siblings), so the per-adapter row skips it; Welcome's
    step 2 probes it with the same check (``services.first_run.probe_claude``), and an
    upgrade's report says its hooks switch as that row does."""
    model_proxy: bool = False
    """Its model traffic goes through AISquare's explainability proxy (Claude Code's, by
    ``ANTHROPIC_BASE_URL``): the lane the doctor's proxy check and Remote's explainability
    card judge its sessions by (``services.explainability_ops._check_lane``)."""
    model_ladders: bool = False
    """AISquare's role model ladder and its probe choose the model (Claude Code); an
    agent without keeps its native default unless a model is pinned."""
    compact: CompactCommand | None = None
    """The command that compacts the session when typed at its idle prompt
    (:class:`CompactCommand`): what P1 WS-4's quiet-point compaction of a manager types
    (``services.quiet_compact``), its acknowledgement the compaction's ``SessionStart``
    (source ``compact``) where it comes at rest (:attr:`starts_at_its_prompt`), else the
    transcript (``AgentTranscript.compacted_since``): Codex's ``SessionStart`` comes with the
    next turn. An agent without is never compacted from here."""
    positional_prompt: bool = False
    """The first prompt is a positional argument, after ``--`` (Claude Code, Codex): a spawn's
    first message goes on its command line (``services.delivery.first_prompt_way``, through a
    file ``launch`` reads and deletes); otherwise the fleet types it."""
    first_context_file_only: bool = False
    """Of its config dir's instruction files (:meth:`AgentAdapter.instruction_files` with no
    ``cwd``, in its precedence) the agent reads the first that is not blank alone (Codex's
    ``AGENTS.override.md`` over its ``AGENTS.md``), which ``agents connect`` imports
    (``core.agents.read_context``); otherwise every one that is there."""
    approves_for_me: bool = False
    """The agent has a reviewer of its own that decides what it may do without asking
    (Claude Code's ``auto`` mode, Codex's ``--approve-for-me``), which ``auto`` approvals
    map to; without one, :meth:`AgentAdapter.permission_args` runs ``auto`` as ``ask`` and
    says so (:func:`stepped_down`)."""
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
    ``AgentScreen.working_on_screen`` says its screen shows a turn running
    (``services.fleet._derive``). Claude Code's prompt sits still until it is answered."""
    one_press_exits: frozenset[str] = frozenset()
    """The keys, as tmux names them once :attr:`AgentAdapter.key_spellings` has spelled them,
    that end the agent with ONE press while its screen shows its empty prompt
    (``AgentScreen.at_empty_prompt``): Codex's ``C-c`` and ``C-d``
    (measured on 0.162.1, after a turn and after one Esc as on a fresh start; ``C-d`` with a
    turn running too, where ``C-c`` opens its ``Cancel task`` / ``Exit`` choice instead;
    neither over a draft). Remote's ``send-keys`` refuses such a key, unless the sender
    confirms it, while the screen shows that prompt or cannot say
    (``services.remote_server.remote_one_press_exit``), as it refuses every agent a second
    ``C-c`` or ``C-d`` within 3 s. Empty for an agent that asks for a second press first:
    Claude Code says ``Press Ctrl-C again to exit``, and the same of Ctrl-D (2.1.304)."""
    hookless_through: str | None = None
    """The last aisquare release that writes none of this agent's hooks (Codex's 0.8.2): an
    upgrade or a move back to it, or to one before it, leaves the agent's hooks as they are
    and says so, since its ``agents refresh-hooks <agent>`` fails
    (``services.lifecycle.lacks_agent_hooks``). ``None``: every release ``upgrade`` installs
    writes them."""
    legacy_fleet_args: bool = False
    """``fleet.roles.<role>.extra_args`` and ``permission_mode`` are in this agent's
    vocabulary (they predate adapters and are Claude Code's); another agent takes only
    an explicitly passed mode and its ``agent_args``."""
    value_options: frozenset[str] = frozenset()
    switch_options: frozenset[str] = frozenset()


@dataclass(frozen=True)
class AgentScreen:
    """How the agent's screen is read (its visible rows, as ``capture-pane -p`` gives them):
    the one optional reader of :attr:`AgentAdapter.screen`, each of its parts optional too.
    What the fleet, Remote and the captain ask of a pane goes through it: the agent's empty
    prompt, a turn running, a dialog of its own and the words it asks, a prompt with the
    keys that answer it, a dialog before its start; and, derived from those where the agent
    declares nothing more of its own, whether a line typed there lands as a prompt
    (:meth:`ready`), whether its prompt is drawn at all (:meth:`drawn`) and what a line must
    not go into (:meth:`modal`). An adapter with no screen (``None``) reads as
    :data:`NO_SCREEN`: never at its prompt, never ready, nothing drawn over it.
    """

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
    idle_on_screen: Callable[[Sequence[str]], bool] | None = None
    """Whether the agent's screen (its visible rows) shows it at its EMPTY prompt with
    nothing drawn over it, where a typed line and its Enter land as a prompt: read in
    place of :attr:`idle_prompt_mark` alone, which an agent may draw only in one of its
    layouts (Codex's ``? for shortcuts``, absent under ``--no-alt-screen``, measured on
    0.162.1). Remote's agent actions read the agent's screen by it too: a quiet pane at
    that prompt is at rest, and anything else drawn over the prompt (a dialog, a draft) is
    one they refuse to type into or ``/exit`` through (``services.remote_needs.
    _needs_screen_drawn``)."""
    working_on_screen: Callable[[Sequence[str]], bool] | None = None
    """Whether the agent's screen (its visible rows) shows a turn running unblocked:
    Codex's status line ``• Working (12s • esc to interrupt)`` where Codex draws it,
    measured on 0.162.1, which its approval view replaces until the human answers and
    which is back the moment they do. The answer's first hook is the approved command's
    PostToolUse, which fires only when the command finishes, so without it an approved
    12-minute command read ``attention`` throughout."""
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
    start_dialog_mark: str | None = None
    """What starts the last line of the agent's screen while a dialog it puts up before its
    start holds it for the human, where an Enter answers it: Claude Code's ``Enter to
    confirm``, the row under its folder-trust and MCP-server dialogs alike, the folder's
    defaulting to ``No, exit`` (measured on 2.1.300); the phrase elsewhere on the screen is
    the agent's output. A spawn's prompt for an agent whose start the board sees
    (:attr:`starts_at_its_prompt`) waits past ``PROMPT_TIMEOUT`` while none is on screen;
    one the board cannot see start (``--continue``, the board off) is not typed while one
    is, nor is ``fleet tell``."""
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
    its ``say`` and its ``press`` and ``paste`` (:meth:`ready`). Claude Code's input
    box drawn and idle (``claude_screen.box_idle``). ``None`` takes the agent's empty prompt
    (:attr:`idle_on_screen`, else :attr:`idle_prompt_mark` on its last line) with no turn
    running on it (:attr:`working_on_screen`): Codex's. Remote never reads it, its own
    reads being :attr:`idle_on_screen`'s."""
    prompt_on_screen: Callable[[Sequence[str]], bool] | None = None
    """Whether the agent's screen (its visible rows) shows its prompt drawn, a turn running
    at it or not: the captain's evidence for a line to a captain the fleet reads
    ``waiting`` (:meth:`drawn`), whose own last reply is what sits above its prompt.
    Read there as :attr:`ready_on_screen`, a reply that quoted a busy footer (``esc to
    interrupt``) kept every ``say`` out for its whole wait (review r17 of #113, finding 1).
    Claude Code's input box drawn (``claude_screen.box_drawn``). ``None`` takes the agent's
    empty prompt (:attr:`idle_on_screen`, else :attr:`idle_prompt_mark` on its last line),
    which a turn running keeps: Codex's."""
    modal_on_screen: Callable[[Sequence[str]], str | None] | None = None
    """What the agent's screen (its visible rows) shows that a typed line must not go into,
    in the owner's words, or ``None`` for a screen a line may go into: what the captain's
    ``say`` refuses before it types, naming it (:meth:`modal`). Claude Code's
    ``claude_screen.modal_showing``: its chooser and trust dialog, the session-rating survey
    just above its box, and any other dialog with an Enter or Esc footer and no box. ``None``
    takes :attr:`dialog_on_screen`'s words (Codex's), else :attr:`screen_prompt`'s
    question."""

    def at_empty_prompt(self, rows: Sequence[str]) -> bool:
        """Whether ``rows`` show the agent at its EMPTY prompt: :attr:`idle_on_screen`, else
        :attr:`idle_prompt_mark` starting its last line; ``False`` with neither."""
        if self.idle_on_screen is not None:
            return self.idle_on_screen(rows)
        mark = self.idle_prompt_mark
        last = next((row.strip() for row in reversed(rows) if row.strip()), "")
        return bool(mark) and last.startswith(mark or "")

    @property
    def reads_empty_prompt(self) -> bool:
        """Whether its empty prompt is read off its screen at all (:attr:`idle_on_screen` or
        :attr:`idle_prompt_mark`): Remote's agent actions read a pane by it, and only by it."""
        return self.idle_on_screen is not None or bool(self.idle_prompt_mark)

    def ready(self, rows: Sequence[str]) -> bool:
        """Whether ``rows`` show it ready for a typed line: its :attr:`ready_on_screen`, else
        its empty prompt (:meth:`at_empty_prompt`) with no turn running on it
        (:attr:`working_on_screen`); ``False`` for an agent that reads none of them."""
        if self.ready_on_screen is not None:
            return self.ready_on_screen(rows)
        working = self.working_on_screen
        return self.at_empty_prompt(rows) and not (working is not None and working(rows))

    def drawn(self, rows: Sequence[str]) -> bool:
        """Whether ``rows`` show its prompt drawn, a turn running at it or not: its
        :attr:`prompt_on_screen`, else its empty prompt (:meth:`at_empty_prompt`); ``False``
        for an agent that reads none of them."""
        if self.prompt_on_screen is not None:
            return self.prompt_on_screen(rows)
        return self.at_empty_prompt(rows)

    def modal(self, rows: Sequence[str]) -> str | None:
        """What ``rows`` show that a typed line must not go into, in the owner's words: its
        :attr:`modal_on_screen`, else the words of a dialog of its own
        (:attr:`dialog_on_screen`; ``a dialog of its own`` when none are on screen), else the
        question of its prompt (:attr:`screen_prompt`); ``None`` for a screen a line may go
        into, or for an agent that reads none of them."""
        if self.modal_on_screen is not None:
            return self.modal_on_screen(rows)
        if self.dialog_on_screen is not None:
            words = self.dialog_on_screen(rows)
            return None if words is None else words or "a dialog of its own"
        if self.screen_prompt is not None:
            prompt = self.screen_prompt(rows)
            return None if prompt is None else prompt.question
        return None


NO_SCREEN = AgentScreen()
"""The screen of an agent whose adapter reads none (:attr:`AgentAdapter.screen` ``None``)."""


def screen_of(adapter: AgentAdapter | None) -> AgentScreen:
    """``adapter``'s :attr:`~AgentAdapter.screen`, :data:`NO_SCREEN` where it has none (or
    for no adapter at all)."""
    screen = adapter.screen if adapter is not None else None
    return NO_SCREEN if screen is None else screen


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
    permission_mode: str | None = None
    """The permission mode a ``stop`` ran under, where the agent's hook says (Claude Code's
    ``permission_mode``): plan mode is no quiet point to compact at (P1 WS-4)."""
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
        ``workspace:auto`` (the fleet's ``auto`` mode and Codex's ``--approve-for-me``), which
        an agent with no reviewer of its own runs as ``workspace:ask`` (:func:`stepped_down`)."""
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
class PermissionArgs:
    """What :meth:`AgentAdapter.permission_args` answers for an intent: the native ``args``,
    and ``notes`` a spawn's receipt says of how the intent was met (``auto`` run as ``ask``
    by an agent with no reviewer of its own, :func:`stepped_down`)."""

    args: tuple[str, ...]
    notes: tuple[str, ...] = ()


def stepped_down(
    capabilities: AgentCapabilities, label: str, intent: PermissionIntent
) -> tuple[PermissionIntent, tuple[str, ...]]:
    """``intent`` as an agent without a reviewer of its own (``capabilities.approves_for_me``)
    can run it: ``auto`` approvals become ``ask``, the human deciding what the reviewer would
    have, with the note that says so; every other intent as it is. The built-in intent is
    ``workspace:auto`` for every role but the reviewer's (:meth:`PermissionIntent.for_role`),
    so such an agent is not refused for every role."""
    if intent.approvals != "auto" or capabilities.approves_for_me:
        return intent, ()
    asked = PermissionIntent(intent.write, "ask")
    return asked, (f"{label} has no reviewer of its own: {intent} runs as {asked}",)


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


def package_version(binary: str, *, depth: int = 4) -> str | None:
    """The ``version`` of the first ``package.json`` beside or above ``binary``'s real path,
    up to ``depth`` folders up: an npm install's (``…/node_modules/<package>/bin/<binary>``,
    linked from npm's ``bin``). ``None`` with none there. Never raises."""
    import json

    try:
        resolved = Path(binary).resolve()
    except (OSError, RuntimeError):
        return None
    for parent in list(resolved.parents)[:depth]:
        try:
            data: Any = json.loads((parent / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            continue
        version = data.get("version") if isinstance(data, dict) else None
        if isinstance(version, str) and version:
            return version
    return None


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


@dataclass(frozen=True)
class FirstEdit:
    """A session's "before the first edit" figures, as its usage row reports them
    (:attr:`UsageScan.first_edit`): whether it has made its first edit (Claude Code's
    ``Edit``/``Write``/``NotebookEdit``/``MultiEdit``, Codex's ``apply_patch``), the raw and
    cost-weighted tokens of every request up to and including the one that made it, and the
    tool calls before it."""

    edited: bool = False
    tokens: int = 0
    weighted: float = 0.0
    tool_calls: int = 0

    def as_dict(self) -> dict[str, Any]:
        """As ``usage_session.usage["first_edit"]`` stores them (``claude_usage.ScanState``'s
        ``first_edit()``)."""
        return {
            "edited": self.edited,
            "tokens": self.tokens,
            "weighted": self.weighted,
            "tool_calls": self.tool_calls,
        }


@dataclass(frozen=True)
class UsageScan:
    """What one scan of a session's transcript found (:meth:`AgentTranscript.usage`): the
    tokens, tool calls and traces of what was appended since the last (``delta``, a
    ``core.usage.Usage``), the state to hand the next scan (``state``, the reader's own text,
    stored as it is), and the session's first-edit figures so far (``first_edit``)."""

    delta: Usage
    state: str
    first_edit: FirstEdit


class AgentTranscript(Protocol):
    """One agent's session transcript (the path its hooks report, ``TeamSession.
    transcript_path``), read for Remote: the needs-you scan's facts
    (``services.remote_needs``) and the Transcript tab's pages (``services.transcript``).

    ``services.transcript`` opens the file (a regular one only, never a pipe), bounds what
    is read of it and pages it; the reader decodes the agent's own records, from its lines
    (:meth:`records`: one JSON object a line by default, or one a later line patches), each
    record's ``timestamp`` ISO 8601 (:func:`~aisquare.core.agent_adapters.transcripts.
    record_time`). The shared shapes are in ``core.agent_adapters.transcripts``.
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

    def records(
        self, lines: Iterable[tuple[int, bytes]], ahead: Iterable[tuple[int, bytes]] = ()
    ) -> Iterator[tuple[int, dict[str, Any] | None]]:
        """The transcript's records from its ``lines``, newest first as they are given
        (``transcripts.lines_backwards``: ``(offset, line)``), each at its line's offset, and
        ``None`` for a line that is no record of its own: one that does not parse, or a later
        line of another record's (Gemini CLI's ``{"$patch": …}`` that brings a tool call its
        result), which it folds into that record before handing it on. What the Transcript
        tab's pager reads (``services.transcript.read_page``) and a reader's own tail walk may.
        ``ahead`` is the lines after the first of ``lines``, oldest first
        (``transcripts.lines_forward``), read only as far as the reader asks: the pager's are
        the newer pages', where a later line of a record on this page may sit, and a reader
        that folds one reads on through them until it has it. Nothing of ``ahead`` is
        yielded. ``transcripts.one_per_line`` for one whole record a line (Claude Code's,
        Codex's), which reads nothing ahead."""
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

    def usage(self, file: Path, state: str | None) -> UsageScan | None:
        """What the transcript ``file`` (its hooks' ``transcript_path``) added since ``state``,
        the last scan's (``None``: read it from the start): each model request's tokens once,
        by model, priced off the agent's ``price_table``, the tool calls, the characters
        aisquare's hooks put in front of the model and every sign aisquare reached the session
        (:class:`UsageScan`). Reads only the complete lines appended since; a rewritten file
        again from its start, counting what is newer than the last scan read; a second call
        with the state the first returned adds nothing. What a Stop or a session's end records
        (``services.metrics.record_usage``). ``None`` for an agent whose transcript holds no
        token record, for which nothing is recorded (never a session of 0 tokens). Never
        raises: a file that cannot be read, or a record it cannot, reads as nothing."""
        ...

    def ledger(
        self, file: Path, *, since: datetime | None, until: datetime | None
    ) -> SessionLedger:
        """The token ledger's counts of the transcript ``file`` (``core.ledger``: requests and
        their input split, the baseline, context sizes, wakes, deltas, compactions and tool
        calls), from what was written in ``[since, until]``: what ``metrics ledger`` prints
        for a session of this agent (#296's WS-0). Never raises; a line it cannot parse is
        counted as skipped."""
        ...

    def context_tokens(self, file: Path) -> int | None:
        """How large the session's context is now (the last request's input), or ``None``
        when the transcript holds no request: what the quiet-point compaction weighs."""
        ...

    def compacted_since(self, file: Path, since: datetime) -> bool:
        """Whether the transcript says the session was compacted at or after ``since``: the
        quiet-point compaction's acknowledgement for an agent whose compaction fires no hook."""
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


@dataclass(frozen=True)
class PriceTable:
    """One agent's model prices (:attr:`AgentAdapter.price_table`): each model its sessions
    run, at its list price in USD per million tokens (``core.pricing.Price``), keyed by
    :attr:`canonical`'s spelling of a model id, read off :attr:`source` on :attr:`as_of`.
    A model it does not list is unpriced, never priced like its neighbour: its tokens are
    counted and reported apart (``core.pricing``). An agent's figure is its API's list
    price whatever its login pays with (a plan's credits), as Claude Code's is."""

    as_of: str
    """The day the prices were read (``YYYY-MM-DD``)."""
    source: str
    """Where they were read: a page, a file of the agent's own, and how."""
    prices: Mapping[str, Price]
    canonical: Callable[[str], str]
    """A model id as :attr:`prices` keys it (no date, no provider prefix, no context-window
    suffix): every key of :attr:`prices` is its own canonical spelling."""

    def price_for(self, model: str) -> Price | None:
        """The list price of ``model``, or ``None`` where the table does not have it."""
        return self.prices.get(self.canonical(model))

    def cost_usd(
        self, model: str, tokens: RequestTokens, *, prompt: int | None = None
    ) -> float | None:
        """The list price of one request of ``model`` in USD (``core.pricing.cost_at``), or
        ``None``: an unpriced model, or a cache write at a price that lists none."""
        from aisquare.core import pricing

        return pricing.cost_at(self.price_for(model), tokens, prompt=prompt)

    def weighted_tokens(
        self, model: str, tokens: RequestTokens, *, prompt: int | None = None
    ) -> float | None:
        """Cost-weighted tokens of one request of ``model`` (``core.pricing.weighted_at``),
        or ``None`` for an unpriced model."""
        from aisquare.core import pricing

        return pricing.weighted_at(self.price_for(model), tokens, prompt=prompt)


class TelemetryReader(Protocol):
    """One agent's native OpenTelemetry, as AISquare's per-launch receiver reads it
    (``services.native_telemetry``; :attr:`AgentAdapter.telemetry_reader`, with
    ``capabilities.native_telemetry``): whether the operator's own config exports already,
    what its tools' shells are kept from, and what of each log record is kept."""

    tool_shell_policy: str
    """How a note names the operator's setting of the environment its tools' shells
    inherit (Codex's ``-c shell_environment_policy``)."""

    def operator_configured(self, home: Path, args: Sequence[str], cwd: Path) -> bool:
        """Whether a config layer of a launch from config dir ``home`` with ``args`` in
        ``cwd`` exports the agent's telemetry already, which is kept (AISquare's tracing is
        off). Raises ``ValueError`` for a layer that cannot be read safely."""
        ...

    def tool_shell_excludes(self, home: Path, args: Sequence[str], cwd: Path) -> list[str] | None:
        """The variable patterns the operator's config keeps out of the shells the agent's
        tools run in, which the receiver's header joins (:meth:`AgentAdapter.telemetry_args`'
        ``keep_out``); ``None`` where an override of the operator's own would hand that
        header to every tool, and tracing stays off. Raises ``ValueError`` as above."""
        ...

    def decode(
        self, attributes: Mapping[str, object], event: str | None
    ) -> dict[str, object] | None:
        """What the receiver keeps of one OTLP log record, from its attributes (the
        resource's under the record's own, every scalar) and its ``eventName``: what names
        the model, the provider, the conversation, the tool, the tokens and the timings, and
        ``event.name``; never a prompt, a tool's arguments or a header. ``None`` keeps
        nothing of the record."""
        ...


class AgentAdapter(Protocol):
    """One terminal agent, in the terms the shared services ask in.

    Identity and installation: ``id`` (the board's and the config's name), ``label``,
    ``binary``, ``home_env``/``home_name`` with :meth:`config_dir`, ``install_hint`` with
    ``install_routes``, ``version_args`` with :func:`version_in`, :meth:`lasting_installs`.
    Hooks: ``settings_name`` (the hooks file in the config dir), ``capabilities.hooks`` with
    :func:`hook_args` and :meth:`hook_entry`, ``hooks_off_setting`` with
    :meth:`hooks_switch`, ``plugin``. Events: :meth:`decode_event` and
    :meth:`encode_reply`. Launch: :meth:`model_args`, :meth:`permission_args`,
    :meth:`store_access`, :meth:`sandbox_writes`, :meth:`broker_access`, :meth:`fleet_args`,
    :meth:`label_args`, :meth:`resume_args`, :meth:`fork_args`,
    ``exit_command``, ``dismiss_key``, :meth:`queue_args`,
    :meth:`mcp_args`, :meth:`tools_only_args`, :meth:`instruction_files`,
    :meth:`disable_native_teams`, :meth:`fleet_settings`, :meth:`settings_layer`,
    :meth:`unattended_env`,
    :meth:`telemetry_args` with ``telemetry_reader``. Personas: :meth:`system_prompt_args`,
    :meth:`skill_dirs`, :meth:`headless_call` with :meth:`headless_answer`. Accounts:
    ``accounts``.
    Remote: ``transcript_reader``. Usage: ``price_table``. ``capabilities`` says the rest.
    """

    id: str
    label: str
    binary: str
    home_env: str
    home_name: str
    settings_name: str
    capabilities: AgentCapabilities
    install_hint: str
    install_routes: tuple[InstallRoute, ...]
    """Every way to install the agent, best first (:class:`InstallRoute`): its own installer,
    a package manager's, ``install_hint`` among them. At least one."""
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
    screen: AgentScreen | None
    """How its screen is read (:class:`AgentScreen`); ``None`` for an agent whose screen is
    not read, which the shared code reads as :data:`NO_SCREEN` (:func:`screen_of`)."""

    @property
    def accounts(self) -> AgentAccounts | None:
        """Numbered login slots (``aisquare accounts``); ``None`` for an agent without them."""
        ...

    @property
    def plugin(self) -> AgentPlugin | None:
        """The aisquare plugin route for this agent; ``None`` when there is none."""
        ...

    @property
    def telemetry_reader(self) -> TelemetryReader | None:
        """How the receiver reads the agent's OpenTelemetry (:class:`TelemetryReader`), with
        ``capabilities.native_telemetry``; ``None`` for an agent without."""
        ...

    @property
    def transcript_reader(self) -> AgentTranscript | None:
        """The reader of the agent's session transcript, for Remote's needs-you cards and
        Transcript tab; ``None`` for an agent whose transcript AISquare cannot read (its
        rows then have no card a transcript decides, and an empty page)."""
        ...

    @property
    def price_table(self) -> PriceTable | None:
        """The list prices of the models its sessions run (:class:`PriceTable`), which its
        token usage is priced at: Claude Code's Anthropic table, Codex's OpenAI one.
        ``None`` for an agent whose prices AISquare has not read: every model of it is
        unpriced, and reported apart."""
        ...

    def config_dir(self, home: Path, env: Mapping[str, str], explicit: Path | None = None) -> Path:
        """The config dir the agent reads, launched with ``env``: ``explicit``, else
        ``env[home_env]``, else ``home / home_name`` (:func:`config_home`)."""
        ...

    def layout_version(self, binary: str) -> str | None:
        """The version the install layout of ``binary`` (the agent's executable as ``PATH``
        finds it) names, read off the filesystem with NO process started: what ``accounts``
        and the Accounts page show beside it (:func:`package_version` reads an npm
        install's). ``None`` for a layout that names none. Never raises."""
        ...

    def hooks_switch(
        self, home: Path, settings_path: Path, args: Sequence[str] = ()
    ) -> HooksSwitch | None:
        """What switches every hook off for the agent in ``home``, launched with ``args``;
        ``None`` when nothing does. Read-only; never raises."""
        ...

    def hook_entry(self, spec: HookSpec, command: str) -> dict[str, Any]:
        """The handler ``core.agents.install_hooks`` writes into a group of
        :attr:`settings_name` for ``spec``, running ``command`` (the whole ``aisquare hook …``
        line): the agent's own shape, ``spec.timeout`` (seconds) in its own unit, and a
        ``timeout`` key, where it has one, that an operator's longer value outlives.
        :func:`command_entry` for Claude Code's and Codex's shape."""
        ...

    def lasting_installs(self, home: Path) -> tuple[LastingInstall, ...]:
        """What config dir ``home`` keeps of aisquare's in the agent's own files, each with
        the agent's command that removes it (:class:`LastingInstall`): what ``aisquare
        uninstall`` leaves and names, and refuses a purge over where it would make the
        aisquare home again. Read-only; never raises; ``()`` for a home that keeps none."""
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
    ) -> LaunchPart:
        """What registers AISquare's MCP server (``executable`` with ``args``, inheriting the
        variables ``env_vars`` names) for this session only (:class:`LaunchPart`)."""
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
    def instruction_files(self, home: Path, cwd: Path | None) -> tuple[Path, ...]:
        """The instruction files a session of config dir ``home`` started in ``cwd`` loads, in
        the order it loads them: its config dir's, then its project's along ``cwd``'s chain,
        each chosen as the agent chooses among a folder's files by what is on disk (Codex's
        ``AGENTS.override.md`` over its ``AGENTS.md``, so only a file there is listed), or every
        one of a fixed set, a file not there listed where the agent would read one (Claude
        Code's; it loads nothing from it): what a session's start leaves out of the
        context it injects, a preference such a file holds already. ``cwd`` ``None``: the
        config dir's alone, every one it may read there in its precedence, read or not
        (``capabilities.first_context_file_only``), which ``agents connect`` imports and the
        doctor shows (``core.agents.context_files``). Absolute where ``home`` and ``cwd`` are;
        read-only, never raises."""
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

    def first_prompts_since(self, home: Path, cwd: Path, since: datetime) -> list[str] | None:
        """The prompts the agent's sessions under ``home`` started in ``cwd`` since ``since``
        ran, as typed, from its own record of them (Codex's rollouts), whether or not its hooks
        ran: what settles a first message no session has spoken for (``services.fleet``,
        #113's #17). ``None`` for an agent whose hooks always say it (Claude Code's
        ``SessionStart`` fires at rest, before its first prompt), or that keeps no such
        record."""
        ...

    def tool_policy(
        self,
        role: str,
        *,
        binary: str,
        args: Sequence[str],
        fleet: FleetSettings,
        effort: str | None = None,
    ) -> harness.ToolPolicy:
        """How a launch of ``role`` on this agent carries the role's deny set and skill
        listing (``harness.denied_for``, P1 WS-1, #280): its own flags, every deny already in
        ``args`` folded into them; or none, with a note (``harness.withheld_policy``), for an
        agent whose tools its permission decides."""
        ...

    def permission_args(
        self, intent: PermissionIntent, role: str, native: Sequence[str] = ()
    ) -> PermissionArgs:
        """The native flags for ``intent``, less what the ``native`` arguments the launch
        already carries settle (they win, as an explicit native flag always does), with
        :meth:`store_access` for its write scope wherever its flags choose the sandbox, and
        the notes a receipt says of them (:class:`PermissionArgs`): an agent without
        ``capabilities.approves_for_me`` runs ``auto`` as ``ask`` (:func:`stepped_down`).
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

    def label_args(self, label: str) -> LaunchPart:
        """What shows the fleet label in the agent's UI (Claude Code's ``--name``); empty
        where it has none."""
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
        self,
        server: str,
        command: str,
        args: list[str],
        env: dict[str, str],
        *,
        inherit: Sequence[str],
        home: Path,
        cwd: Path,
        launch_args: Sequence[str],
    ) -> list[str] | None:
        """Launch arguments that leave the session no tool but those of the one MCP server
        ``server`` (run as ``command`` with ``args`` and ``env``, keeping the session's own
        values of the variables ``inherit`` names), each of its tools allowed without asking,
        and no other MCP server loaded, the operator's own included: the captain's session,
        launched from config dir ``home`` in ``cwd`` with ``launch_args``. ``None`` for an
        agent that cannot be confined so, which then cannot be the captain. ``ValueError``
        for a server (``server``, or one of the operator's to keep out) its arguments cannot
        name."""
        ...

    def queue_args(self, native_session_id: str, message: str) -> list[str] | None:
        """Arguments to the agent's binary that queue ``message`` for its RUNNING session,
        delivered as the next turn once the current one ends (``fleet tell`` to a busy
        agent), run with :attr:`home_env` set to the session's config dir; ``None`` for
        an agent without such a queue (the message is then a board note)."""
        ...

    def disable_native_teams(self) -> LaunchPart:
        """What keeps the agent's own sub-agent teams off (``[fleet]
        disable_native_agent_teams``)."""
        ...

    def fleet_settings(self, home: Path, args: Sequence[str]) -> LaunchPart:
        """What the agent of a fleet window launches with to keep a dialog of its own out of a
        pane the fleet types into, from config dir ``home`` with ``args``, less what a config
        layer of the operator's sets itself (``services.agent_launch.fleet_settings``, asked
        in a fleet window alone: elsewhere the human answers the agent's dialogs). Codex's
        ``notice.hide_rate_limit_model_nudge`` and ``check_for_update_on_startup``."""
        ...

    def settings_layer(
        self,
        settings: Mapping[str, Any],
        file: Path,
        *,
        home: Path,
        env: Mapping[str, str],
        cwd: Path,
    ) -> LaunchPart | None:
        """What gives the session ``settings`` as a settings layer of its own, ABOVE the
        operator's: a value their own config sets (a user's, a project's) loses to it. The
        settings are the :class:`LaunchPart` settings a launch's slots answered
        (:func:`merged_settings`); ``file`` is this launch's own, its directory made, written
        where the layer is a file; the session runs from config dir ``home`` in ``cwd`` with
        the launch's environment ``env``, which an agent whose top layer stands in for a
        file of the machine's reads that file by, to keep it: Claude Code's ``--settings
        <file>``, Codex's ``-c`` pairs, Gemini CLI's ``GEMINI_CLI_SYSTEM_SETTINGS_PATH``
        (its system settings, over every other settings file, the machine's own carried in
        it). Its ``notes`` say what of ``settings`` such a file of the machine's kept out.
        ``None`` for an agent with none, whose slots answer no settings."""
        ...

    def unattended_env(self) -> Mapping[str, str]:
        """Environment for a session nobody may be watching (the captain's, whose pane is
        answered from a phone), which keeps out a prompt of the agent's own that would hold
        its pane for the human and is no work's (Claude Code's session-rating survey);
        empty where none is kept out this way."""
        ...

    def telemetry_args(self, endpoint: str, *, keep_out: Sequence[str] = ()) -> LaunchPart:
        """What exports the agent's OpenTelemetry logs to ``endpoint`` (OTLP/HTTP JSON);
        empty without ``native_telemetry``. Never the receiver's ``Authorization``:
        argv is every local user's to read, so the header rides in the launch environment
        (``services.native_telemetry.HEADERS_ENV``), and ``keep_out`` names the variables
        the shells the agent's tools run in must not inherit, that one among them. Whether
        the operator configured an exporter of their own, which is kept, is its
        :attr:`telemetry_reader`'s ``operator_configured``."""
        ...

    def system_prompt_args(
        self, block: Path, text: str, args: Sequence[str], home: Path, *, cwd: Path
    ) -> list[str] | None:
        """Launch arguments that add ``text`` to the agent's system prompt for the session,
        beside its own (``aisquare launch --persona``: the persona's briefing, which the file
        ``block`` holds), for an agent launched with ``args`` from config dir ``home`` in
        ``cwd``.
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

    def headless_call(
        self, schema: Path, out: Path, model: str | None, *, home: Path, cwd: Path
    ) -> list[str] | None:
        """Arguments to the agent's binary for ONE headless turn whose answer is JSON that
        the JSON Schema in the file ``schema`` describes (``persona import --engine
        manager``), with ``model`` pinned where given: the instructions and the source go on
        its stdin, it keeps no session, it runs no tool and no MCP server (the source is
        untrusted), not even one the config layers of a session from config dir ``home`` in
        ``cwd`` name, its hooks are off where a launch argument can say so, and
        :meth:`headless_answer` reads its answer from what it printed or wrote to ``out``.
        ``None`` for an agent without such a call: the engine ladder goes on to the next
        engine; ``ValueError`` for one whose tools cannot be switched off here. Run with the
        environment of the agent's manager account, in ``cwd``."""
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

    ``env[home_env]`` names the agent's home, which is its config dir only where its
    accounts say so (:meth:`AgentAccounts.config_dir`: Gemini CLI's names the folder its
    ``.gemini`` is in). A ``~olduser`` this machine does not have is kept as written
    (``paths.expand_user``), so ``paths.names_no_home`` still refuses to make it; a symlink
    loop is not resolved. Claude Code's directory, as ``core.agents`` names it, is
    ``core.agents._claude_home``.
    """
    named = env.get(adapter.home_env, "").strip()
    if explicit is not None:
        raw = explicit
    elif named and adapter.accounts is not None:
        raw = adapter.accounts.config_dir(paths.expand_user(Path(named)))
    else:
        raw = Path(named) if named else home / adapter.home_name
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

UsageFetch = Callable[
    [str, Mapping[str, str], float],
    tuple[int, bytes] | tuple[int, bytes, Mapping[str, str]],
]
"""``(url, headers, timeout) -> (status, body[, headers])``: how an agent that reads usage
over the network reaches it. Supplied by the service layer; core never opens a socket. The
answer's headers are optional, so a fetch with none to give still works; they carry a
429's ``Retry-After``."""


@dataclass(frozen=True)
class SharePart:
    """One thing an agent's accounts can have in common (``[accounts] share`` names it,
    ``aisquare accounts share --enable`` turns it on): :attr:`name`, where it sits in the
    agent's config dir, and how the accounts come to share it."""

    name: str
    """What the operator writes: ``skills``, ``claude_md``, ``agents_md``. A name two agents
    both declare is one switch for both, each into its own entry."""
    entry: str = ""
    """Its entry in the agent's config dir (``AgentAccounts.config_dir`` of a home): a folder
    or a file. ``""`` for a merged part, which is no entry of one dir."""
    is_dir: bool = True
    """Whether :attr:`entry` is a folder (made empty in the plain home when it has none, so
    every account follows what is added there later) or a file (refused when the plain home
    has none: an empty one would hide the account's own)."""
    how: Literal["link", "merge"] = "link"
    """``link``: each managed account's entry becomes a link to the plain home's, its own
    kept beside it as ``<entry>.own-<stamp>`` until the part is turned off. ``merge``: the
    accounts' own copies are merged into one place once, and :attr:`AgentAccounts.memory`
    points every launch at it (nothing is linked)."""
    experimental: bool = False
    """The part can carry what is one account's alone (a login, a plugin store): said in the
    help, never on by default."""


class SharedMemory(Protocol):
    """How an agent's accounts share its auto-memory, the one ``merge`` part
    (:attr:`SharePart.how`): one directory per project, merged from every account the first
    time the part is turned on and handed to every launch (``AgentAccounts.memory``)."""

    root_name: str
    """The folder under the aisquare home that keeps the shared copies (``claude-shared``)."""
    override: str
    """The operator's own way of setting it that wins over this one, in the words the
    stand-down line uses (``its own --settings``)."""

    def key(self, cwd: Path) -> str:
        """The key the agent files a folder's memory under."""
        ...

    def projects(self, account: ClaudeAccount) -> Mapping[str, Path]:
        """The memory directories ``account`` holds, by project key, those that exist."""
        ...

    def apply(
        self, args: Sequence[str], operator_args: Sequence[str], directory: Path
    ) -> list[str] | None:
        """``args`` (a launch's own, the role's) with the agent's auto-memory pointed at
        ``directory``, merged into the arguments it already sets that way; ``None`` when
        ``operator_args`` (the operator's line: a binding's, a fleet's, what was typed) set it
        already, which wins (:attr:`override`)."""
        ...

    def warnings(self, account: ClaudeAccount) -> list[str]:
        """What is wrong with ``account``'s memory as it stands (the doctor row's words), or
        ``[]``."""
        ...


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
    """The plain home's slot number: Claude Code's 1, Codex's 0, and a negative one for an agent
    registered after them, since the managed ladder runs from 2 (``core.slot_number``)."""
    plain_label: str
    install_command: str
    network_usage: bool
    """Whether :meth:`usage` leaves the machine (Claude's endpoint) or reads local files:
    a local reading is what the account's own sessions recorded, as fresh as their last
    turn, so a listing says how old it is, and it is keyed to no token it would send."""
    legacy_slots: bool
    """Its slots are those aisquare kept before any other agent had slots (Claude Code's),
    and read as they did then: their arrangement is the store's own ``claude_account`` table
    and project key, their plain slot is listed before its home exists, a role's binding to
    one takes a slot of any agent, and every listing names them as it did (the top level of
    ``accounts --json``, the Accounts page's first section, the doctor's ``claude-*`` rows).
    ``False`` for every other agent: its slots are keyed by its id (store v29's
    ``agent_account``) and listed after. One adapter at most."""
    noun: str
    """What prose calls one of its accounts: ``a Claude account``, ``a Codex account``."""
    relogin: str
    """What the human types in the agent's TUI to sign in again where it does not ask by
    itself, for an agent that signs in from its TUI (:meth:`sign_in_args` ``()``): Claude
    Code's ``/login``. ``""`` for an agent whose sign-in command signs in (Codex's)."""
    sign_out: str
    """How the human signs its plain home out, as a refusal to remove that slot says it:
    Claude Code's ``inside Claude Code (/logout)``, Codex's ``with `codex logout```."""
    shares_settings: bool
    """``[accounts] share_settings`` links a managed slot's settings to its plain home's
    (Claude Code's ``settings.json``, ``services.claude_accounts.share_settings``); ``False``
    for an agent whose settings are each slot's own (Codex's)."""
    share_parts: tuple[SharePart, ...]
    """What its accounts can share (``[accounts] share``, ``aisquare accounts share``): each
    part's name, its entry in the agent's config dir and whether it is linked or merged
    (:class:`SharePart`). Claude Code's are memory, skills, agents, commands, ``CLAUDE.md``
    and plugins; Codex's ``AGENTS.md`` and skills. ``()`` for an agent that shares none."""
    memory: SharedMemory | None
    """How its ``merge`` part is shared (:class:`SharedMemory`), or ``None`` for an agent
    with no such part. Read for the launch, never by name."""

    def sign_in_args(self) -> tuple[str, ...]:
        """Arguments to the agent's binary that start its own sign-in in a slot's env.

        ``()`` means the agent has no sign-in command: it signs in from its own TUI, which
        starts its first-run sign-in in a slot that has none (Claude Code's, whose ``/login``
        does it again; Gemini CLI's). A sign-in window then starts the binary in the slot
        with nothing after it, and waits until :meth:`login_fingerprint` changes
        (``services.claude_accounts.sign_in_landed``): the human signs in there and leaves
        it with the adapter's ``exit_command``."""
        ...

    def plain_home(self, home: Path, plain: Mapping[str, str]) -> Path:
        """The plain home, from the shell's own launch variables (``plain``)."""
        ...

    def launched_account(
        self, env: Mapping[str, str], accounts: Sequence[ClaudeAccount]
    ) -> ClaudeAccount | None:
        """The one of ``accounts`` (its own) that the agent's binary started with ``env``
        runs on, if any: the slot whose home it reads (``core.claude_accounts.
        account_in_home``), Claude Code's by the ``.claude.json`` it reads, the plain one's
        being beside its directory. What ``[accounts]`` sharing shares into."""
        ...

    def config_dir(self, slot_dir: Path) -> Path:
        """The config dir the agent reads in a home :attr:`home_env` names (a slot's, the
        plain one): where its hooks file, settings and login are. The home itself for
        Claude Code and Codex; Gemini CLI's names the folder its ``.gemini`` is in."""
        ...

    def identity(
        self, account: ClaudeAccount, *, env: Mapping[str, str] | None = None
    ) -> ClaudeIdentity | None: ...
    def signed_in(self, account: ClaudeAccount, *, fresh: bool = False) -> bool:
        """A login is recorded in the slot and usable; ``fresh`` reads the store it is kept in
        again rather than take a read of the last few seconds (Claude Code's Keychain)."""
        ...

    def subscription(self, account: ClaudeAccount) -> str | None: ...
    def token_state(self, account: ClaudeAccount) -> str:
        """``ok``, ``expired`` or ``missing`` — read, never refreshed; ``unreadable`` when the
        store the login is kept in would not say (a locked Keychain), which is no login to
        do again."""
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
