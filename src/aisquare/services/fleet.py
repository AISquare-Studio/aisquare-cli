"""The fleet: one private tmux server, one session per project, one window per agent.

The TUI is a view; THIS is the model. Everything here works with the UI closed
— ``aisquare fleet …`` is the same code the sidebar calls — and nothing here
knows about Textual. State lives in two places only: tmux (the processes) and
``context.db`` (``fleet_agent`` rows, the project's codename, the board). There
is no daemon.

Layout of this module (docs/plans/fleet-tui.md §5.3, §5.7, §7):

- names: codenames, labels, tmux session names, branch names — pure rules;
- ``spawn`` / ``list_agents`` / ``tell`` / ``stop`` / ``reap`` — the lifecycle;
- ``bulk`` — restart or stop every running agent of every project at once;
- ``nudge_manager`` — the wake-up a sub-agent's board write sends the manager;
- ``pause`` / ``resume`` — a named board signal the manager's cycle respects.

Every launch goes through ``aisquare launch <role>`` INSIDE the tmux window, so
model ladders, effort offsets, ``team bind`` profiles and the explainability
wiring apply unchanged (§3.4). The session id is minted here, before launch, so
a row knows its board session before the agent's first hook fires.

Two process seams live here, both registered in ``core.spawn.SEAMS``: every tmux
command goes through :class:`~aisquare.core.tmux.TmuxServer` (its own seam), and
:func:`_git` is the ONE place git runs for worktrees (§3.5). ``services.team``
and ``cli.launch`` are imported lazily — the CLI imports this module, and the
team service may one day call back into it for nudges.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import re
import shutil
import sqlite3
import subprocess
import time
import unicodedata
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

from aisquare.core import agents as agent_core
from aisquare.core import claude_accounts as claude_accounts_core
from aisquare.core import (
    claude_sharing,
    codenames,
    experimental,
    harness,
    launch_seal,
    orchestrator,
    paths,
    personas,
    selfcli,
)
from aisquare.core import tmux as tmux_core
from aisquare.core.agent_adapters import (
    DEFAULT_AGENT,
    adapter_for_binary,
    claude_screen,
    get_adapter,
    recorded_adapter,
)
from aisquare.core.agent_adapters.types import (
    AgentAdapter,
    AgentScreen,
    ForkArgs,
    PermissionIntent,
    option_values,
    screen_of,
    stepped_down,
)
from aisquare.core.config import (
    CLAUDE_PERMISSION_MODES,
    FleetRoleSettings,
    FleetSettings,
    built_in_extra_args,
    load_config,
    replacing_the_former_reviewer_default_first,
)
from aisquare.core.ids import new_agent_id
from aisquare.core.locking import holding
from aisquare.core.spawn import git_env, untraced_env
from aisquare.core.store import (
    AmbiguousIdError,
    ContextStore,
    StoreUnopenable,
    is_corrupt_error,
    is_locked_error,
    store_session,
)
from aisquare.core.tmux import (
    DEFAULT_WINDOW_HEIGHT,
    DEFAULT_WINDOW_WIDTH,
    PaneFacts,
    SocketAnswer,
    TmuxError,
    TmuxPartlyTyped,
    TmuxServer,
    TmuxTimedOut,
    TmuxUnavailable,
    WindowInfo,
)
from aisquare.core.version import __version__
from aisquare.core.workspace import (
    _gone,
    _resolved,
    _resolved_or_spelled,
    active_project,
    git_argv,
    project_id_for,
)
from aisquare.models import (
    CLOSED_STATUSES,
    ClaudeAccount,
    EndedBy,
    FleetAgent,
    FleetAgentState,
    FleetAgentStatus,
    LaunchSpec,
    ProjectInfo,
    TeamEvent,
    TeamSession,
    TeamTask,
)
from aisquare.services import (
    agent_events,
    agent_launch,
    auto_mode,
    explainability_ops,
    install_route,
)
from aisquare.services import claude_accounts as claude_accounts_service
from aisquare.services import explainability as explainability_service
from aisquare.services import personas as personas_service
from aisquare.services import project as project_service

FLEET_ROLES: tuple[str, ...] = ("manager", "coder", "tester", "reviewer", "validator", "ui-tester")
"""The fleet's own roles (§3.3). Any harness or ``team bind`` role is accepted too."""

PLAIN_ROLE = harness.PLAIN_ROLE
"""No role: a plain Claude Code session, on the board with no standing cycle or mission.
Not one of :data:`FLEET_ROLES`, whose every member the manager's loop and the docs brief."""

MANAGER_LABEL = "manager"
"""A label the fleet reserves: exactly one manager per project."""

CAPTAIN_ROLE = "captain"
CAPTAIN_LABEL = "captain"
"""The home-level captain (services.captain): one per HOME, on the home board — its role
and its label are one word, as the manager's are, and the label is reserved (T2, 13121)."""

LABEL = re.compile(r"^[a-z][a-z0-9-]{1,23}$")
"""An agent label: ≤ 24 chars, no ``.``, ``:`` or spaces (tmux target separators)."""

SESSION_PREFIX = "asq-"
PAUSE_SIGNAL = harness.PAUSE_SIGNAL
TASK_SHORT = 8
"""Characters of a task id (after ``tsk_``) that name it in a branch — the same
width ``services.team.short_id`` uses for sessions."""

NUDGE_TEXT = "aisquare: board update — read the delta above."
"""The one line a nudge types (§7.3). It carries nothing — the existing delta
injection on ``UserPromptSubmit`` delivers the details — and never starts with
``/`` or ``!``, Claude Code's command prefixes."""

NUDGE_DEBOUNCE = timedelta(seconds=5)
"""Two board writes inside this window wake the manager once."""

NUDGE_OWED = "nudge-owed:"
"""The ``team_meta`` key prefix, before a project id, of the nudge a board write owed
that project's manager and could not send: tmux refused the writer (a command in Codex's
sandbox, :func:`nudge_manager`). Its value is when; :func:`deliver_owed_nudge` takes it."""

PROMPT_TIMEOUT = 20.0
"""How long ``spawn --prompt`` waits for the agent to come up before typing anyway."""

START_TIMEOUT = 120.0
"""How long ``spawn --prompt`` waits for the start of an agent whose start the board sees
(``capabilities.starts_at_its_prompt``) while no dialog of its is on screen: past
:data:`PROMPT_TIMEOUT` only a dialog stops the wait, which needs the human. Claude Code
2.1.300 started within a second here even resuming a 67 MB transcript, or with an MCP
server that never answered (measured), so this is a slow machine's margin, the receipt's
delay only when the agent is neither started nor at a dialog."""

ACTIVITY_WINDOW = timedelta(seconds=5)
"""Output within this window reads as ``working`` for an agent the board cannot
vouch for (no hooks, or a stale row). Measured on tmux 3.7c: the plan's
``window_activity_flag`` is set the instant a detached window is created and never
clears without a client, so it cannot tell working from idle; ``#{window_activity}``
— the epoch second of the last output — can."""

_STATUS_WINDOW = 1.0
"""Seconds to keep polling a DEAD pane for its exit status (see ``stop``)."""

_POLL_INTERVAL = 0.25
_PROMPT_SETTLE = 2.0
"""After the agent process appears, its UI needs a moment before it reads input."""
_KEY_GAP = 0.2
"""Between a typed line and its Enter (:meth:`PlacedPane.type_line`). Codex reads keys that arrive
in one burst as a paste, Enter included: ``/exit`` sent with its Enter stayed in the
composer and the stop's grace ran out into a kill, with no ``SessionEnd``; the manager's
nudge stayed there unsent, three of them by the end of a run (0.162.1 under tmux 3.7c:
an Enter 20 ms behind the text is kept, 50 ms behind it submits)."""
_SUBMIT_GRACE = 1.0
"""How long a prompt may sit in Claude Code's input box after its Enter before the Enter is
read as lost and pressed once more (:func:`_submitted`)."""
_SUBMIT_TIMEOUT = 5.0
"""How long :func:`_submitted` reads the pane back for the prompt to leave the input box."""
_SECURITY_PRESSES = 3
"""How many Enters :func:`_at_input_box` gives Claude Code's first-run Security notes."""
_SECURITY_REPRESS = 6.0
"""How long the notes must stay drawn, unchanged, after an Enter before it is read as
swallowed (the notes drawn before Claude Code read its input) and pressed again: far past
any redraw, so a second Enter never lands on the screen the first one brought."""
_SHELLS = frozenset({"sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh", "ash"})
_NOT_AN_AGENT = _SHELLS | {"tmux"}
"""Foreground commands that are never the agent: a shell (left behind after an
exit), and ``tmux`` itself — a new pane reads ``tmux`` for the first few hundred
milliseconds, between the fork and the exec (measured on 3.7c)."""
_LABEL_RETRIES = 5
_CODENAME_RETRIES = 5
"""How many times a codename assignment re-walks past a name another process took."""
_GIT_TIMEOUT = 120.0
_SEP = "|~|"
_ACTIVITY_FORMAT = f"#{{pane_id}}{_SEP}#{{window_activity}}"

#: Injection points for tests: a fake clock keeps the prompt wait and the stop
#: grace period instant without touching the logic they time.
_sleep: Callable[[float], None] = time.sleep
_monotonic: Callable[[], float] = time.monotonic

#: The board states a fresh ``team_session`` row can hand a fleet row (§5.1).
_BOARD_STATES: dict[str, FleetAgentState] = {
    "working": "working",
    "waiting": "waiting",
    "attention": "attention",
    "limited": "limited",
}


class FleetError(RuntimeError):
    """Anything the fleet cannot do, with the reason in the message."""


class FleetUnavailable(FleetError):
    """tmux is missing or too old for the fleet (§8.2), or would not answer what a command
    has to know before it acts; the command is to be tried again."""


class NoSuchProject(FleetError):
    """The project reference matched nothing (or several things — said in the message)."""


class NoSuchAgent(FleetError):
    """No live agent with that label in this project."""


class EndedMeanwhile(NoSuchAgent):
    """A stop's ``/exit`` reached the running agent, and another writer ended its row before
    the stop did (:func:`_stop_row`): this stop took the agent down, and is nobody's.

    Still a :class:`NoSuchAgent` to ``shutdown``, which reads it as a row that went away;
    :func:`bulk` must not read it as an agent it left alone (review of #266, round 3).
    """


class FleetInterrupted(FleetError):
    """The operator interrupted ``shutdown`` mid-loop; ``report`` says how far it got.

    A ``FleetError`` so a caller that only knows the family still stops cleanly;
    the CLI catches it first, prints the report, and exits 130.
    """

    def __init__(self, report: ShutdownReport) -> None:
        super().__init__(report.interrupted or "shutdown interrupted")
        self.report = report


class BulkInterrupted(FleetError):
    """The operator interrupted :func:`bulk` part-way; ``report`` says how far it got.

    A ``FleetError`` for the same reason as :class:`FleetInterrupted`, whose way it goes:
    the CLI catches it first, prints the report and exits 130.
    """

    def __init__(self, report: BulkReport) -> None:
        super().__init__(report.interrupted or "interrupted")
        self.report = report


@dataclass(frozen=True)
class ResumeSpec:
    """Start the window on an EXISTING session rather than a fresh one (#146).

    The agent's own resume (``AgentAdapter.resume_args``: ``claude --resume <absolute
    transcript path>``, which Claude Code ≥ 2.1.223 finds wherever it lives) keeps the
    original session id, so the fleet row is joined to the same board session the
    agent already had: its claims, its cursor and its notes carry on. ``session_id``
    is that id, recorded on the row up front rather than learned from the first hook;
    ``native_session_id`` the agent's own, where it is another.
    """

    session_id: str
    transcript_path: Path
    native_session_id: str | None = None
    fork: bool = False
    """Resume it as a FORK (the Spawn dialog's hand-off, card tsk_01m3ns5a736s): the source's
    conversation under a NEW session id, minted by :func:`spawn` and passed as
    ``--session-id`` beside ``--resume <transcript> --fork-session``, so the new row joins
    its own session and the source's id is never shared. ``session_id`` is then the
    source's, the one forked from."""


Typed = Literal["yes", "no", "unsent", "maybe"]
"""How a write into an agent's pane ended (:class:`PaneWrite`, :class:`TellResult`), and how a
spawn's prompt or a replacement's first line reached it (:func:`_type_prompt`): the word an
audit line says. ``yes``, all of it went through; ``no``, tmux refused it and typed
nothing; ``unsent``, it is at the agent's prompt but its Enter did not go through, so an
Enter sends it; ``maybe``, tmux did not answer in time, and it may still arrive. ``unsent``
was ``no``, and a caller retrying on ``no`` pasted a second copy beside the first, both
submitted by the next Enter (review of #259, round 12, 2/9). The receipts said it in a
vocabulary of their own, ``True``/``False``/``unsent``/``maybe``, translated from this one
by hand, which is how a tell had no word for ``unsent`` (9/9). Their JSON still says
``true`` and ``false`` (:func:`prompt_typed_json`); a ``False``, "NOT typed … Tell it what
to do", for the other two had the Tell go in beside the first copy (round 11, 2/13)."""


def prompt_typed_json(typed: Typed | None) -> bool | str | None:
    """A receipt's ``prompt_typed`` as the CLI's JSON and the remote's answer say it: ``true``,
    typed and sent; ``false``, not typed; ``unsent`` and ``maybe`` as their words."""
    return None if typed is None else {"yes": True, "no": False}.get(typed, typed)


@dataclass(frozen=True)
class SwitchReceipt:
    """What ``fleet switch`` did: the agent it stopped, the one it started, and how."""

    stopped: FleetAgent
    started: FleetAgent
    from_slot: int | None
    to_slot: int
    resumed: bool
    """True when the replacement resumed the old session's transcript; False when it
    started fresh with a hand-off prompt built from the board."""
    tmux_session: str
    notes: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    """The notes that say what the switch could NOT do with its replacement up: a first
    line not typed, claims not moved, an old session not marked ended, a hand-over mark
    not taken back. Each is in ``notes`` too, beside the ones every switch has (the
    headroom read, the launch replayed, the worktree kept). The automatic hand-over has
    no terminal to print ``notes`` to and posts these alone (``services.hooks.hand_over``)."""
    prompt_typed: Typed = "yes"
    """Whether the replacement's first line reached its pane: the line telling a resumed
    session to continue, or a fresh one's hand-off prompt. ``no`` leaves it idle at an
    empty prompt, ``unsent`` and ``maybe`` with the line at its prompt or on its way
    (:data:`Typed`), and ``notes`` say why."""

    @property
    def how(self) -> str:
        """How the replacement began, in the words of the board's ``switched`` line."""
        return _how_started(self.resumed, typed=self.prompt_typed)


@dataclass(frozen=True)
class SpawnReceipt:
    """What ``fleet spawn`` did — including the label it ACTUALLY used."""

    agent: FleetAgent
    asked_label: str | None
    tmux_session: str
    notes: list[str] = field(default_factory=list)
    """Anything the caller should see: a suffixed label, a reused worktree (and the
    branch it was put on), a prompt that could not be typed, an agent that will not
    join the board. NOT a permission-mode fallback — there is none: the mode is the
    flag, then the role's config, then ``auto``, and nothing here rewrites it."""
    prompt_typed: Typed | None = None
    """Whether the ``prompt`` asked for reached the pane (:data:`Typed`): ``None`` when none
    was asked for; otherwise the notes say why it is not ``yes``. One an agent takes as its
    argument (``capabilities.positional_prompt``) is on its command line: ``yes``."""
    failures: list[str] = field(default_factory=list)
    """The notes about what did NOT happen once the agent was up: the prompt not typed,
    the claims of a hand-over not moved, the previous session not marked ended. Each
    is in ``notes`` too; a prompt typed past the wait is typed, and is not here."""
    shared: list[str] = field(default_factory=list)
    """The notes that say something was done for the agent, not a warning: a share made into
    its account (a folder's trust copied, its settings.json linked), or a key the launcher
    pressed so its prompt went in (#32: past the Security notes, a lost Enter pressed
    again). Each is in ``notes`` too."""


@dataclass(frozen=True)
class TellResult:
    """How a message reached (or did not reach) an agent: ``typed`` as :class:`PaneWrite` says
    it, ``yes`` once its text and its Enter went through; ``unsent``, its text is at the
    agent's prompt and its Enter was refused; ``maybe``, tmux did not answer in time with one
    of them, which may still arrive. One field for them all, as on
    :class:`PaneWrite`: ``delivered`` and a ``maybe`` beside it could say "delivered, maybe"
    (review of #259, round 11, 13/13)."""

    typed: Typed
    how: str

    @property
    def delivered(self) -> bool:
        """Whether the message went in and was sent: ``typed`` is ``yes``."""
        return self.typed == "yes"


@dataclass(frozen=True)
class PaneWrite:
    """How one write into an agent's pane ended (:func:`pane_write`): ``yes``, tmux typed it;
    ``no``, tmux refused it and typed nothing; ``unsent``, refused past the writes of its chain
    that went in (``went``), or past a part of it (``TmuxPartlyTyped``); ``maybe``, tmux did
    not answer in time (``TmuxTimedOut``), and a wedged server may still type it once it
    recovers, or the rest of a part it typed.

    Every write that types into an agent on someone's word asks it here: the remote's tell
    in each of its modes, its Escapes, ``send-keys`` and quick answers, and ``fleet tell``.
    A ``maybe`` is said by :meth:`late` and audited ``…=maybe``. It is never "nothing was
    typed", nor advice to type it again: a second paste is submitted beside the first, and
    a second Enter answers whatever prompt the first one's message brought up. Rounds 7 to
    9 of #259's review taught that to one call site at a time, each in words of its own,
    and the sites not reached yet took a timeout for a refusal (round 10, 8/9).
    """

    typed: Typed
    what: str
    error: TmuxError | None = None
    went: tuple[str, ...] = ()
    """The writes of a chain (:func:`pane_writes`) that went through before ``what``, which are
    in the pane whatever came of it: refused past them, it is ``unsent``."""

    def said(self, label: str, *, press: str, again: str = "send it again") -> str:
        """The sentence for an ``unsent`` or ``maybe`` write into ``label``'s pane: past a write
        that went in (``went``), what waits at its prompt, or its Enter on its way
        (:func:`pasted_unsent`, ``press``); otherwise what tmux may still type (:meth:`late`,
        ``again``). A ``no`` typed nothing, and its caller words it."""
        if self.went:
            return f"{label}: {pasted_unsent(self, label, press=press, put='typed').how}"
        return self.late(label, again=again)

    @property
    def audited(self) -> str:
        """The word its audit line ends on: ``failed`` for a ``no``, else ``delivered=…``."""
        return "failed" if self.typed == "no" else f"delivered={self.typed}"

    def late(self, label: str, *, then: str = "", again: str = "send it again") -> str:
        """``maybe``'s sentence: ``what`` may still reach ``label``'s pane (and ``then``), or a
        part of it did, and an ``unsent`` part's, so its live screen is read before anyone does
        ``again``."""
        error = self.error
        read = f"read its live screen before you {again}"
        if isinstance(error, TmuxPartlyTyped):
            typed = f"tmux typed {error.typed} of the {error.size} bytes of {self.what} into"
            if isinstance(error.cause, TmuxTimedOut):
                return (
                    f"{typed} {label}'s pane, then did not answer in time ({error.cause}): "
                    f"more of it may still arrive{then} — {read}"
                )
            return f"{typed} {label}'s pane, then refused the rest ({error.cause}){then} — {read}"
        return (
            f"tmux did not answer in time with {self.what} for {label}'s pane ({error}): "
            f"it may still arrive{then} — {read}"
        )


def pane_write(what: str, write: Callable[[], object]) -> PaneWrite:
    """``write()``, which types ``what`` into an agent's pane, and how it ended
    (:class:`PaneWrite`). ``TmuxError`` is every way a tmux call fails (``core.tmux._tmux``),
    ``TmuxTimedOut`` the one way that may still happen, and ``TmuxPartlyTyped`` a write of
    several calls that failed past its first: ``core.tmux`` says which of its calls timed out
    (review of #259, round 11, 7/13). Past a refusal no more of it can arrive: ``unsent``, not
    ``maybe``, which audited it ``delivered=maybe`` (round 12, 7/9)."""
    try:
        write()
    except TmuxPartlyTyped as exc:
        return PaneWrite("maybe" if isinstance(exc.cause, TmuxTimedOut) else "unsent", what, exc)
    except TmuxTimedOut as exc:
        return PaneWrite("maybe", what, exc)
    except TmuxError as exc:
        return PaneWrite("no", what, exc)
    return PaneWrite("yes", what)


def pane_writes(*writes: tuple[str, Callable[[], object]] | None) -> PaneWrite:
    """``writes`` in turn, each ``(what, write)`` as :func:`pane_write` takes it (``None``: none
    here), up to the first that did not go through: how that one ended, its ``what`` saying
    which it was and ``went`` the ones before it, in the pane; the last one's when all did.
    What comes after a write tmux refused or did not answer would land beside it, or ahead of
    what may still arrive. ``send-keys`` and a quick answer chained it by hand, the first
    beside a stand-in "nothing" write (review of #259, round 11, 12/13). And each worked out
    by hand what was in the pane before the write that failed, a quick answer not at all
    (round 12, 1/9 and 8/9): :meth:`PaneWrite.said` says it, :attr:`PaneWrite.audited`
    audits it."""
    wrote = PaneWrite("yes", "nothing")
    went: tuple[str, ...] = ()
    for what, write in filter(None, writes):
        wrote = pane_write(what, write)
        if wrote.typed != "yes":
            typed: Typed = "unsent" if went and wrote.typed == "no" else wrote.typed
            return replace(wrote, typed=typed, went=went)
        went = (*went, what)
    return wrote


def type_then_enter(
    server: TmuxServer,
    pane_id: str,
    text: str,
    label: str,
    *,
    press: str,
    done: str = "typed into its pane at its prompt",
    what: str = "the text",
) -> TellResult | PaneWrite:
    """Paste ``text`` (``what``) at the prompt in ``label``'s pane, then press Enter: how every
    message is typed into an agent, by :func:`tell`, a spawn's prompt (:func:`_type_prompt`)
    and the remote's paste (``remote_actions.action_paste``).

    A paste that did not go through is answered as its :class:`PaneWrite`, its caller's to
    say: a tell files the text as a note, the remote refuses with a 409, a spawn notes it.
    Past the paste the text is at the prompt, and the answer is how the Enter ended:
    ``done``, or :func:`pasted_unsent` (``press``). The three wrote paste, Enter and
    :func:`pasted_unsent` out each in words of its own (review of #259, round 11, 12/13).
    """

    def enter() -> None:
        # A key of its own, :data:`_KEY_GAP` after the paste: Codex takes keys that arrive
        # in one burst for a paste and keeps the Enter in it as text (``PlacedPane.type_line``).
        _sleep(_KEY_GAP)
        server.send_keys(pane_id, "Enter")

    wrote = pane_writes((what, lambda: server.paste(pane_id, text)), ("Enter", enter))
    if wrote.typed == "yes":
        return TellResult("yes", done)
    return pasted_unsent(wrote, label, press=press) if wrote.went else wrote


def pasted_unsent(entered: PaneWrite, label: str, *, press: str, put: str = "pasted") -> TellResult:
    """A message ``put`` at ``label``'s prompt (pasted, or typed as ``send-keys`` types it)
    whose Enter did not go through (``entered``): the text is there either way. Refused, it
    waits there unsent, and ``press`` says where an Enter sends it. Not answered in time, the
    Enter may still send it, so nothing invites a second one: that would answer whatever
    prompt the message brings up, "1. Yes" to a permission nobody saw (review of #259, round
    10, 3/9)."""
    if entered.typed == "maybe":
        how = entered.late(label, then=", and send it", again="press Enter")
        return TellResult("maybe", f"{put} it at its prompt, but {how}")
    return TellResult(
        "unsent",
        f"{put} it at its prompt, but tmux could not press Enter ({entered.error}) — {press} "
        "to send it",
    )


@dataclass(frozen=True)
class AttachReceipt:
    """What :func:`attach_persona` did (docs/plans/spawn-personas.md §4.7)."""

    agent: FleetAgent
    persona: str
    replaced: str | None
    """The persona the agent ran as before, when it was a different one."""
    delivered: Literal["typed", "noted", "unsent", "maybe"]
    """``typed`` into a waiting agent's pane, or ``noted``: an agent that could not be
    typed into gets no note on the board in its place. The board's ``persona_attached``
    line and its rows are the record, and the agent's own next prompt hands it the
    briefing, for it alone. ``unsent`` and ``maybe`` as :class:`TellResult` says them —
    at its prompt with its Enter refused, or tmux did not answer in time — neither to be
    told again."""
    how: str
    """What happened, in words to show the caller: ``tell``'s own for a briefing that was
    typed; otherwise why it was not, and how the persona reaches the agent."""


@dataclass(frozen=True)
class ReapReport:
    """What a reap pass found."""

    ended: list[FleetAgent] = field(default_factory=list)
    lost: list[FleetAgent] = field(default_factory=list)
    worktrees_removed: list[Path] = field(default_factory=list)
    worktrees_kept: list[tuple[Path, str]] = field(default_factory=list)
    """An ended agent's worktree left in place, with why: uncommitted changes, or commits
    found nowhere but there (:func:`_remove_finished_worktrees`)."""
    branches_deleted: list[tuple[Path, str]] = field(default_factory=list)
    """``(repo, branch)`` for each ``fleet/…`` branch ended agents' rows recorded and no
    worktree had out, deleted once the default branch had its work
    (:func:`_sweep_fleet_branches`). With its repo, as a reap of every project walks
    several."""
    branches_kept: list[tuple[Path, str]] = field(default_factory=list)
    """``(repo, branch)`` for each ``fleet/…`` branch ended agents' rows recorded and no
    worktree has out that holds commits the default branch lacks, named on every reap: kept,
    so a spawn for its task or a restart of its agent carries on from it
    (:func:`_remake_worktree`). With its repo, which a ``git branch -D`` needs."""
    claims_released: list[str] = field(default_factory=list)
    """Task ids the ended and lost rows' board sessions still held, returned to
    the pool — the same count ``shutdown`` reports, where ``reap`` used to
    discard :func:`services.team.release_agent_claims`' answer in both branches
    (review of #203, round 4)."""
    release_failures: list[str] = field(default_factory=list)
    """``<label>: <what happened>`` for each ended or lost row whose release did
    not fully happen — refused by the store, or released and not announced to
    the board. The row is ended and committed before the release runs, so a
    raise here used to take the rest of the sweep down with it: the remaining
    agents, every later project, the worktree pass and the manager nudges, and
    a traceback instead of a report (round 5)."""
    held: list[ShutdownRow] = field(default_factory=list)
    """Rows the sweep left live whose agent may still be running where no tmux client
    reaches it, each with why and the way out. On every ``reap``: a row whose socket another
    tmux server answers on (:func:`_hold_on_another_server`). Under ``--server-down`` also:
    a row on a socket tmux reports no server for, which the one rule says may still run, a
    socket file taken from under its server or a socket that could not be asked
    (:func:`_hold_on_absent_server`). The reason says which. Said as ``shutdown`` says a row
    it left live (follow-up of #257; review of #259, round 5)."""


@dataclass(frozen=True)
class StopReceipt:
    """What :func:`stop` did: the row it ended, and the claims that ended with it.

    ``released`` is the answer ``shutdown`` needs to COUNT what a stop returned
    to the pool. It used to reach that through a ``release_claims=False`` flag
    on ``stop`` and a second release of its own — two paths that had to agree
    the pane was dead, and a caller that forgot the flag double-released
    (review of #203, round 4). One release, in ``stop``, reported here — and
    none under ``handover`` (``switch``, and ``restart`` of a running agent),
    whose claims wait for the replacement, so its ``released`` is empty
    whatever the session held.
    """

    agent: FleetAgent
    released: list[TeamTask]
    release_failed: str | None = None
    """Why the release did not happen, when it did not: the row IS ended and the
    pane IS dead by then, so a store that refuses the courtesy (a lock, a
    vanished session row) costs the release and is SAID here — never the stop,
    which used to raise out of ``shutdown`` and report a dead, ended row as
    LEFT LIVE, keep the project's pause and exit 1 (review of the fold)."""
    killed: str | None = None
    """Why no exit command was typed and the window was killed at once, when it was not
    (:func:`_stop_row`): the agent's screen showed a dialog of its own, not its empty
    prompt, where the exit command's Enter would have answered it; or the row has no
    launch record to place its pane (spawned before this version), and nothing is typed
    into a pane the fleet has no record of."""
    dismissed: str | None = None
    """What was pressed before the exit command, when something was: the agent's
    ``dismiss_key`` at a dialog of its own, under ``dismiss_dialog`` (:func:`_dismissed`)."""
    withheld: bool = False
    """A hand-over's stop ended the row itself, releasing and announcing nothing:
    the claims wait for the replacement, and if it never starts
    :func:`_abandon_handover` gives them back and announces the exit. False for
    a plain stop, and for a hand-over whose row a listing ended first — that
    listing released and announced it, and nothing is left to give back
    (:func:`_stop_row`)."""
    marked_only: bool = False
    """The agent had ended already and no window of its was left to remove: the stop
    recorded it as stopped on purpose and nothing else, so Restart all leaves it (#38)."""


@dataclass(frozen=True)
class ShutdownRow:
    """One row ``shutdown`` did not simply stop, and WHY it reads that way.

    The reason is set where the service KNOWS it, because the CLI cannot know
    and must not guess. The first draft of this command printed "its server was
    not running" over every recorded row — including rows that got there because
    ``stop`` raised on a server that had answered and was reported killed on the
    line above, and rows whose agent had exited cleanly on its own.
    """

    agent: FleetAgent
    reason: str
    held: bool = False
    """Left live as one whose agent may run where no tmux client reaches it (``_held_rows``),
    not as a pane seen alive: no server this run asks holds its pane, so it spares no session
    there (``_spared_panes``). Never a row held as ``"answers"``: a server came up on its socket
    after the probe, and its pane may be on it."""


@dataclass
class ShutdownReport:
    """What ``shutdown`` did, row by row and session by session."""

    stopped: list[FleetAgent] = field(default_factory=list)
    """Agents ``stop`` ended on an answering server: graceful ``/exit`` unless
    ``force``, exit status where tmux exposed one, ``agent_exited`` emitted. An
    agent that had already exited on its OWN between the snapshot and its turn
    joins them: same end state, and it keeps the exit status it recorded."""
    recorded: list[ShutdownRow] = field(default_factory=list)
    """Rows ended with no exit status on the operator's word, each with its reason:
    their socket did not answer, or they appeared after their session was killed."""
    failed: list[ShutdownRow] = field(default_factory=list)
    """Rows LEFT LIVE, each with its reason: ``stop`` refused because the pane was
    seen alive, the row could not be written, or the agent was no longer there to
    stop. Nothing in this list was ended — that is the point of the list."""
    sessions_killed: list[str] = field(default_factory=list)
    """``<socket>:<session>`` for each of the fleet's OWN sessions this killed."""
    sessions_absent: list[str] = field(default_factory=list)
    """Sessions already gone when the kill phase looked — the ordinary shape after a
    clean stop pass: killing a session's last window takes the session with it."""
    sessions_failed: list[str] = field(default_factory=list)
    """Sessions tmux refused to kill. Tried and did NOT happen — never reported as
    killed, which is what the first draft did with ``servers_killed``. A session
    still standing because the dead pane of a row that exited LATE could not be
    removed from it is here too, with that as its reason (review of #121,
    round 9): the outcome is the same one the operator has to act on."""
    sessions_left_up: list[str] = field(default_factory=list)
    """Sessions deliberately spared: they hold a row this left live, and killing the
    session would kill an agent whose pane tmux had just confirmed alive. The same
    rule spares a session that outlived the late dead pane removed from it."""
    servers_absent: list[str] = field(default_factory=list)
    """Sockets with no server to ask — the rows on them went to ``recorded``, but for a
    row whose agent may still run (:func:`_unreached`), which went to ``failed`` unless
    the operator said ``forget``."""
    claims_released: list[str] = field(default_factory=list)
    """Task ids released from the ended rows' board sessions (``release_claims``)."""
    release_failures: list[str] = field(default_factory=list)
    """``<label>: <error>`` for each ended row whose claims could NOT be released —
    a store that refused after the row was ended. The row is down and counted
    where it belongs; its work stays claimed by a session that no longer exists
    until the lease lapses, which the operator has to be told rather than have
    swallowed (the release used to be a silent ``suppress(Exception)``)."""
    paused_cleared: list[str] = field(default_factory=list)
    """Projects whose ``fleet-paused`` signal this cleared, by display name."""
    paused_kept: list[str] = field(default_factory=list)
    """Projects whose ``fleet-paused`` signal was deliberately LEFT ON because the
    shutdown did not confirm them down (a row left live, a session left up or
    unkillable, a listing that failed). Clearing it there would remove the
    manager's instruction not to spawn precisely while the operator is trying to
    stop it (review of #121, round 2)."""
    incomplete_projects: list[str] = field(default_factory=list)
    """Project ids the shutdown could not confirm down — the set the pause rule
    and any caller that wants "is it really down?" should read, rather than
    re-deriving it from the other lists."""
    interrupted: str | None = None
    """Set when the operator interrupted the stop loop: which row it was on, and
    that the rest of the fleet was left as it was. Rows before it are ended and
    their claims released, committed row by row, so the report is owed even
    then — it travels on :class:`FleetInterrupted` (round 5)."""
    late_scan_failed: str | None = None
    """Why the final re-read of live rows did not happen (a store that refused).
    A scan that did not run cannot vouch for anything, so every project in the
    run's snapshot is marked not confirmed down and keeps its pause; the CLI
    reports PARTLY and exits 1 (review of #121, round 7)."""
    pause_scan_failed: str | None = None
    """Why the pause signals could not be reconciled (a store that refused the
    visible-projects lookup AFTER agents were already stopped). Every pause is
    then KEPT — a forgotten project's pause is not ours to touch, and this could
    not tell one from a live project — and the CLI reports PARTLY and exits 1,
    rather than discarding the whole report (review of #121, round 8). It also
    carries the per-project failures — ``<project>: could not be read/cleared
    (…)``, joined by ``; `` — for a lookup that SUCCEEDED and left one project's
    own signal unread or unwritten (review of #121, round 9)."""
    pause_unsettled: list[ProjectInfo] = field(default_factory=list)
    """The projects named in ``pause_scan_failed`` as one whose signal could not be read or
    could not be cleared: their ``fleet-paused`` may still be set, and the report gives a
    ``fleet resume`` for each. Under ``--all`` the warning said only "re-run", with no way
    to clear a signal and no project to clear it in (review of #259, round 22). Empty when
    the lookup itself failed: then no project's signal was asked."""


@dataclass(frozen=True)
class ShutdownPlan:
    """What ``shutdown`` WOULD end — read-only, so the CLI can ask before doing it.

    ``fleet shutdown`` ends running work, so it prints this and asks first, the
    way ``project prune`` does for stale registrations. ``absent_sockets`` is
    what separates the two dispositions: a row on one of them cannot be stopped,
    only recorded lost.
    """

    projects: list[ProjectInfo] = field(default_factory=list)
    agents: list[FleetAgent] = field(default_factory=list)
    sessions: list[str] = field(default_factory=list)
    """``<socket>:<session>`` for every fleet session the kill phase would target."""
    absent_sockets: list[str] = field(default_factory=list)
    held: dict[str, str] = field(default_factory=dict)
    """Rows the run would LEAVE LIVE, by id, with why: their agent may still run where no
    tmux client reaches it (:func:`_unreached`), on a socket that did not answer or one
    another server answers. Under ``forget``, only those whose socket answers, which it
    does not end."""
    lost: dict[str, str] = field(default_factory=dict)
    """Rows the run would record lost, by id, with their socket: its name, or the path its
    server said it listened on (:func:`_address`). Those no server answered on, and under
    ``forget`` those held, whether or not their socket answers; under
    :attr:`tmux_unusable`, every row not :attr:`held`, unchecked."""
    lost_why: dict[str, str] = field(default_factory=dict)
    """Why the run would record each of :attr:`lost` lost, by id: the reason its report
    gives the row (``ShutdownReport.recorded``)."""
    tmux_unusable: str | None = None
    """Why tmux is not usable here, for a ``forget`` plan: every row is then ended
    unchecked, on the operator's word, but for those in :attr:`held`, whose socket
    answers, and nothing is stopped or killed."""


# --- settings ---------------------------------------------------------------------


def settings() -> FleetSettings:
    """The ``[fleet]`` section; defaults when the config is unreadable (fail-open)."""
    try:
        return load_config().fleet
    except Exception:  # a broken config costs the customisation, never the fleet
        return FleetSettings()


def role_settings(role: str, config: FleetSettings | None = None) -> FleetRoleSettings:
    """The role's launch shape, or the built-in default for a role the config omits."""
    config = config or settings()
    selected = config.roles.get(role, FleetRoleSettings())
    base = config.roles.get(harness.base_role(role), FleetRoleSettings())
    # Numbered seats historically use their own worktree/Claude permission/argv
    # settings. Only native sandbox and approval defaults inherit per field.
    return selected.model_copy(
        update={
            "sandbox": selected.sandbox if selected.sandbox is not None else base.sandbox,
            "approval_policy": (
                selected.approval_policy
                if selected.approval_policy is not None
                else base.approval_policy
            ),
            "approve_for_me": (
                selected.approve_for_me
                if selected.approve_for_me is not None
                else base.approve_for_me
            ),
        }
    )


def server(config: FleetSettings | None = None) -> TmuxServer:
    """The private tmux server the fleet runs on."""
    return TmuxServer((config or settings()).tmux_socket)


def server_for(socket: str, config: FleetSettings | None = None) -> TmuxServer:
    """The server a RECORDED agent lives on — ITS socket, not today's config.

    Every ``fleet_agent`` row stores the socket it was started on, because an
    operator who edits ``[fleet] tmux_socket`` must not thereby make every
    running agent unreachable: the new socket answers "no such window" for
    them, which is indistinguishable from "the pane is gone" and would end
    rows whose processes are still running. ``services.diagnostics`` and the
    UI's agent pane already address panes by the stored socket; the lifecycle
    here does too. Routed through :func:`server` so a caller that fakes that
    one factory sees every socket. This asks by the name alone (``tmux -L``); a
    row's pane is asked of its :class:`PlacedPane`, at the path its launch record holds.

    The socket is all :func:`server` reads of the config, so ``config.toml`` is read only
    for a row that recorded none: every quick answer's screen check and every snapshot of
    Remote's re-read it for a name they held (review of #259, round 9, 7/8).
    """
    if config is None:
        config = (
            FleetSettings().model_copy(update={"tmux_socket": socket}) if socket else settings()
        )
    elif socket and socket != config.tmux_socket:
        config = config.model_copy(update={"tmux_socket": socket})
    return server(config)


@dataclass(frozen=True)
class _Address:
    """Where a recorded row's server is asked: by its socket's name (``tmux -L``), where this
    shell's tmux looks, or at ``path`` (``tmux -S``), where the server said at spawn that it
    listened (``FleetAgent.socket_path``) when that is elsewhere. Named as reports name it:
    the socket's name, else that path."""

    name: str
    path: Path | None = None

    def __str__(self) -> str:
        return self.name if self.path is None else str(self.path)


def _address(
    socket: str, socket_path: Path | None, config: FleetSettings | None = None
) -> _Address:
    """Where the server a launch record names is asked (:class:`_Address`): ``socket`` by
    name, ``socket_path`` where the server said at spawn that it listened.

    At the path its spawn recorded, unless that is where ``-L`` finds the socket's name from
    this shell anyway. ``-L`` from a shell without the spawning shell's ``TMUX_TMPDIR`` asks
    another directory, where another server on the same name may run, and its panes take
    the row's pane id: it was read as gone, its row ended under its running agent, or
    ``/exit`` typed into a stranger's pane (round 28 of #257). A row from before v25
    recorded no path, and is asked by name.

    Every question about one row's pane is put here — the listings and the UI's tick, a
    spawn's and a restart's reconcile, ``reap``, ``tell``, ``stop``, ``shutdown``, the
    doctor — so none of them reads another server's ``%N`` as the row's.
    """
    if socket_path is not None:
        try:
            here = server_for(socket, config).socket_path()
        except TmuxUnavailable:
            here = None  # no POSIX uid: nothing to compare, so the recorded path is asked
        if here is None or not _same_socket(socket_path, here):
            return _Address(socket, socket_path)
    return _Address(socket)


def server_of(agent: FleetAgent, config: FleetSettings | None = None) -> TmuxServer:
    """The server ``agent`` is asked on, where it said it listened (:func:`_address`): for a
    caller outside this module that holds an agent a listing placed
    (``FleetAgentStatus.agent``, whose socket, path and pane are its launch record's,
    :func:`resolve_place`), as Remote's actions do. Never a row read from the store: every
    question this module puts about a row's pane is put to its :class:`PlacedPane`.

    The address of each listed row is worked out once, by its placement
    (:attr:`PlacedPane.address`); the listing asked it about five times per row before — the
    observe, the reconcile, the lingering windows, the status and its ``socket_at`` — on asq's
    tick every two seconds (follow-up of #257)."""
    config = config or settings()
    return _server_at(_address(agent.tmux_socket, agent.socket_path, config), config)


def placed_row(row: FleetAgent) -> FleetAgent:
    """``row`` as its launch record places it (:attr:`Placed.agent`: its socket, path, pane,
    boot, times and agent the record's), for a caller outside this module that puts its pane
    to tmux, as Remote's keys and screens do (:func:`server_of`). :class:`FleetError` when no
    record places it or the row was rewritten in the store, as :func:`tell` types nothing
    there (review r5 of #113, finding 1; r6, finding 4)."""
    placement = resolve_place(row)
    if isinstance(placement, Unplaced):
        raise FleetError(f"{row.label} is not placed: {placement.why}")
    if placement.rewritten is not None:
        raise FleetError(f"{row.label}: {placement.rewritten}")
    return placement.agent


def socket_at(agent: FleetAgent) -> Path | None:
    """The path ``agent``'s server is asked at (``tmux -S``), or ``None`` when it is asked by
    its socket's name (:func:`_address`), for an agent a listing placed, as
    :func:`server_of`. It reads the config, so a caller holding a listed
    :class:`FleetAgentStatus` reads its ``socket_at``, worked out with the listing's: asked
    on the UI thread, each frame reloaded config.toml for every open project (round 29 of
    #257)."""
    return _address(agent.tmux_socket, agent.socket_path).path


def _server_at(address: _Address, config: FleetSettings | None = None) -> TmuxServer:
    """The server at ``address``, through :func:`server_for` so a faked factory sees it."""
    return server_for(address.name, config).at(address.path)


# --- where the fleet acts: the ONE resolver ------------------------------------------
#
# The store is read-only in an agent's sandbox (`types.store_read_only`; its board calls
# go through its launch's broker, `services.store_broker`), and still, as defence in
# depth (`core.launch_seal`), no value read from it — a row's pane, socket, socket path
# or agent, a project's codename — may say where the fleet types, reads or kills. Every
# tmux question about a row's window, and every act on it — typing (`tell`, the nudge, a
# spawn's prompt), `/exit` and the kill (`stop`, and restart, switch, the hand-over and
# `shutdown` through it), liveness (the listings, `reap`, a spawn's and a restart's
# reconcile), lingering and late panes,
# `shutdown`'s sessions, `rename` and `attach` — is put to a `PlacedPane` or a
# `PlacedSession`, and only `resolve_place` mints those from a launch record (the
# recordless kill's `_recordless_pane` and `--all`'s `_listed_session` from what tmux
# itself listed). A row no record places is a typed refusal, `Unplaced`. The places are
# the only objects whose methods put a pane or a session to tmux:
# tests/test_fleet_placement.py enumerates every tmux call in this module and in
# `services.team`, and every place a place is minted, so a next path cannot take the
# store's word without failing it. Rounds r5 to r7 of #113 found that word taken one path
# at a time (r5, finding 1; r6, findings 1, 4, 6 and 7; r7, findings 1, 2, 3, 6 and 7).


@dataclass(frozen=True)
class PlacedPane:
    """A row's pane where its launch record places it: what every keystroke, read and kill
    of a row's window goes through (:func:`resolve_place`)."""

    address: _Address
    """The server the record places it on (:func:`_address` of the record's socket)."""
    pane_id: str
    created_at: datetime
    """The record's: a server that started later holds none of its panes (:func:`_outlived`)."""
    config: FleetSettings = field(repr=False, compare=False)
    server_started_at: datetime | None = None
    """The record's start of that server (``WindowPlace.server_started_at``): a server that
    started at any other time is another (:func:`_started_after`)."""

    def server(self) -> TmuxServer:
        return _server_at(self.address, self.config)

    def outlived(self, started: datetime | None) -> bool:
        """Whether a server that started at ``started`` is not the one this pane's record
        was written on, so the id names another agent's pane there (:func:`_outlived`)."""
        return _started_after(self.created_at, self.server_started_at, started)

    def facts(self) -> PaneFacts | None:
        return self.server().pane_facts(self.pane_id)

    def facts_or_raise(self) -> PaneFacts | None:
        return self.server().pane_facts_or_raise(self.pane_id)

    def pid(self) -> int | None:
        return self.server().pane_pid(self.pane_id)

    def screen(self) -> list[str]:
        return self.server().screen_text(self.pane_id)

    def captured(self) -> list[str]:
        """The rows on its screen with their escapes (``TmuxServer.capture``): what Claude
        Code's folder-trust question is read off (``claude_sharing.shows_trust_prompt``)."""
        return list(self.server().capture(self.pane_id).lines)

    def kill(self) -> None:
        self.server().kill_window(self.pane_id)

    def press(self, key: str) -> None:
        """Send the one tmux ``key`` (``Escape``), no text: a dialog's own key
        (:func:`_dismissed`). Raises ``TmuxError`` as the tmux call does."""
        self.server().send_keys(self.pane_id, key)

    def holding_session(self) -> PlacedSession | None:
        """The session tmux says holds this pane, on this pane's server while that server
        is the record's (:meth:`outlived`); ``None`` when the pane is gone or tmux will not
        say. How a legacy row's session is found, whose census record names none: by its
        pane, never by a name built from the codename (review r8 of #113, finding 1)."""
        srv = self.server()
        try:
            held = srv.pane_session(self.pane_id)
            if held is None or self.outlived(srv.started_at()):
                return None
        except TmuxError:
            return None
        session_id, name = held
        return PlacedSession(
            self.address, session_id, name, self.created_at, self.config, self.server_started_at
        )

    def type_then_enter(
        self,
        text: str,
        label: str,
        *,
        press: str,
        done: str = "typed into its pane at its prompt",
        what: str = "the text",
    ) -> TellResult | PaneWrite:
        """:func:`type_then_enter` into this pane, how every message the fleet types is typed:
        ``text`` without its control characters (:func:`_typeable`), its Enter
        :data:`_KEY_GAP` after the paste (:meth:`type_line`), each saying how it ended."""
        return type_then_enter(
            self.server(), self.pane_id, _typeable(text), label, press=press, done=done, what=what
        )

    def type_line(self, text: str, *, command: bool = False) -> None:
        """Type ``text`` at the agent's prompt and submit it: how the fleet types every line.

        The text goes in as one bracketed paste, a line break in it part of the message — a
        ``command`` (``/exit``) keyed in as typed instead — and its Enter :data:`_KEY_GAP`
        later, a key of its own: Codex takes keys that arrive in one burst for a paste and
        keeps the Enter in it as text. (A bracketed paste with its Enter right behind it did
        submit on 0.162.1, which is why ``tell`` reached a waiting Codex and the nudge, typed
        key by key, did not; one rule for both costs the paste 0.2 s.) Raises ``TmuxError``
        as the tmux calls do.
        """
        srv = self.server()
        text = _typeable(text)
        if command:
            srv.send_literal(self.pane_id, text)
        else:
            srv.paste(self.pane_id, text)
        _sleep(_KEY_GAP)
        srv.send_keys(self.pane_id, "Enter")


def _typeable(text: str) -> str:
    """``text`` without its control characters but the line break and the tab: what the
    fleet types into a pane. The text can be the store's — a task's detail, a board entry,
    quoted in a replacement's hand-off prompt — and a tmux before 3.7 pastes it as it is
    (``paste-buffer`` sanitises only since): an ``ESC [201~`` in it ended the bracketed
    paste, and what followed arrived as keys, a ``\\r!cmd\\r`` running ``cmd`` in Claude
    Code's shell mode (review r9 of #113, finding 3). C0, DEL and C1 all go."""
    return "".join(char for char in text if char in "\n\t" or unicodedata.category(char) != "Cc")


@dataclass(frozen=True)
class PlacedSession:
    """A tmux session the fleet may join, list, rename, attach to or take down: by tmux's
    own id (``$3``) a launch record holds, on the server the record places it, and only
    while that server is the one the record was written on (``created_at``); or, for
    ``shutdown --all``'s sweep, by the name tmux itself just listed (:func:`_listed_session`).
    Never by a name built from the project's codename in the store."""

    address: _Address
    target: str
    """``-t``: tmux's id for it, or the name tmux listed."""
    name: str
    """What reports call it: the record's name for it, or the listed one."""
    created_at: datetime | None
    """The record's; ``None`` for a session tmux listed just now."""
    config: FleetSettings = field(repr=False, compare=False)
    server_started_at: datetime | None = None
    """The record's start of that server (:func:`_started_after`); ``None`` when it kept none."""

    def server(self) -> TmuxServer:
        return _server_at(self.address, self.config)

    def _outlived(self, srv: TmuxServer) -> bool:
        """Whether the server is younger than the record, so the id is another session's."""
        return self.created_at is not None and _started_after(
            self.created_at, self.server_started_at, srv.started_at()
        )

    def present(self) -> bool:
        """Whether it is there; ``False`` too when tmux cannot say."""
        srv = self.server()
        try:
            return srv.has_session(self.target) and not self._outlived(srv)
        except TmuxError:
            return False

    def present_or_raise(self) -> bool:
        """Whether it is there, as tmux CONFIRMS it; a question that could not be put raises."""
        srv = self.server()
        return srv.has_session_or_raise(self.target) and not self._outlived(srv)

    def panes(self) -> list[WindowInfo]:
        """Its windows (one pane each), as ``list-panes -s`` lists them; empty when it is not
        there or tmux will not say."""
        return self.server().list_windows(self.target)

    def panes_or_raise(self) -> list[WindowInfo]:
        """:meth:`panes`, except a server that could not be ASKED raises."""
        return self.server().windows_or_raise(self.target)

    def kill(self) -> None:
        self.server().kill_session(self.target)

    def rename(self, new: str) -> None:
        self.server().rename_session(self.target, new)

    def attach_command(self) -> list[str]:
        return self.server().attach_argv(self.target)

    def new_window(self, *, notes: list[str] | None = None, **window: Any) -> WindowInfo:
        """A new window in this session (``TmuxServer.spawn_window``): only by its id, so it
        goes into this session or, should the session have gone meanwhile, nowhere. Opened
        through :func:`open_window`, as every window at the fleet's socket is."""
        return open_window(self.server(), self.target, config=self.config, notes=notes, **window)


@dataclass(frozen=True)
class Placed:
    """A row its launch record places (:func:`resolve_place`)."""

    row: FleetAgent
    """The row as the store has it: who it is (id, label, project, session, task), never
    where it is."""
    agent: FleetAgent
    """The row with its window and its agent where the record places them (socket, path,
    pane, boot, time, agent): what a listing shows and derives from."""
    record: launch_seal.LaunchRecord
    pane: PlacedPane
    session: PlacedSession | None
    """The session the record names by tmux's id; ``None`` for a record that kept none."""
    rewritten: str | None = None
    """Every field the row was rewritten in, both sides; ``None`` when it is its record's."""


@dataclass(frozen=True)
class Unplaced:
    """The typed refusal: a row no launch record places. Nothing is typed into, read of or
    killed at a place it names; its one way out is :func:`_recordless_pane`."""

    row: FleetAgent
    why: str

    @property
    def agent(self) -> FleetAgent:
        """The row as the store has it — for its id and label, never its place."""
        return self.row


Placement = Placed | Unplaced


def take_census() -> None:
    """Take the census of the rows spawned before launch records were kept
    (``launch_seal.LegacyCensus``), once on this machine, while no launch record has been
    written yet: each such row's record made from the row as it stands now. Run before
    anything in a sandbox could have written the store: by the first fleet read or spawn
    of this aisquare (a spawn takes it before it writes a record or starts any window), and
    before any agent that sandboxes its tools is launched (``agent_launch.prepare_sandbox``).
    Never raises: a census not taken leaves those rows refused, as a row with no record
    is. The doctor, which writes nothing, never takes it."""
    if launch_seal.census_taken() or launch_seal.records_kept():
        # Taken already; or never to be: records were kept here before it, by an aisquare
        # that took none, and an agent sandboxed since under that aisquare's profile,
        # which left the store writable, may have written it.
        return
    with suppress(Exception):
        with store_session() as store:
            projects = {
                project.id: project
                for project in store.list_projects(all=True, include_forgotten=True)
            }
            rows = [
                row
                for project in projects.values()
                for row in store.fleet_agents(project.id, live_only=False)
            ]
        launch_seal.take_census(
            [
                _legacy_record(row, projects[row.project_id])
                for row in rows
                if row.project_id in projects and not launch_seal.recorded(row.id)
            ]
        )


def _legacy_record(row: FleetAgent, project: ProjectInfo) -> launch_seal.LaunchRecord:
    """The record the census makes for ``row``, spawned before records were kept: the row
    as it stands, its window where it is, its session the one its codename named then."""
    spec = row.launch_spec or LaunchSpec(binary=row.binary, worktree=row.worktree)
    return launch_seal.LaunchRecord(
        id=row.id,
        project_id=row.project_id,
        project_root=str(project.root),
        role=row.role,
        agent=row.agent or DEFAULT_AGENT,
        binary=row.binary,
        cwd=str(row.cwd),
        worktree=row.worktree,
        account_slot=row.account_slot,
        spec=spec,
        label=row.label,
        legacy=True,
        window=launch_seal.WindowPlace(
            tmux_socket=row.tmux_socket,
            pane_id=row.pane_id,
            socket_path=str(row.socket_path) if row.socket_path is not None else None,
            boot_id=row.boot_id,
            created_at=row.created_at,
            server_started_at=row.server_started_at,
            session=session_name(project.codename) if project.codename else None,
        ),
    )


def _read_record(agent_id: str) -> launch_seal.LaunchRecord | str:
    """``launch_seal.read``, the census taken first when it has not been (:func:`take_census`):
    the first read of this aisquare finds a row spawned before records were kept placed."""
    if not launch_seal.census_taken() and not launch_seal.recorded(agent_id):
        take_census()
    return launch_seal.read(agent_id)


def resolve_place(
    row: FleetAgent,
    record: launch_seal.LaunchRecord | str | None = None,
    config: FleetSettings | None = None,
) -> Placement:
    """Where the fleet may act on ``row``'s window: the server, the pane, the session, the
    boot, the time and the agent its launch record holds (``launch_seal.WindowPlace``), as
    tmux answered the spawner and as the spawner resolved the agent — or :class:`Unplaced`
    with why. Never the row's ``pane_id``, ``tmux_socket``, ``socket_path`` or ``agent``, nor
    the project's codename: store columns, which a sandbox could write before its
    broker (``services.store_broker``) and no record vouches for.

    Pointed at any pane of the user's, ``tell`` typed there and ``stop`` typed ``/exit`` and
    killed that window (review r5 of #113, finding 1); with its ``agent`` set to another's,
    the readiness check that keeps an Enter out of Codex's dialogs was the other agent's
    (r6, finding 1). A row rewritten to name another place is placed all the same, with
    :attr:`Placed.rewritten` saying what differs: typing refuses it, a stop acts where the
    record places it (r6, finding 4). ``record`` is one already read (r6, finding 11).
    """
    config = config or settings()
    record = _read_record(row.id) if record is None else record
    if isinstance(record, str):
        return Unplaced(row, record)
    place = record.window
    if place is None:
        return Unplaced(
            row,
            "its launch record does not say which pane is its (it was spawned by an aisquare "
            "that recorded none)",
        )
    if record.project_id != row.project_id:
        return Unplaced(row, "its launch record is another project's")
    row_path = str(row.socket_path) if row.socket_path is not None else None
    differ = [
        f"{name} {mine!r} where its launch record has {recorded!r}"
        for name, mine, recorded in (
            ("tmux socket", row.tmux_socket, place.tmux_socket),
            ("socket path", row_path, place.socket_path),
            ("pane", row.pane_id, place.pane_id),
            # A row from before adapters names no agent: Claude Code's, as its record says.
            ("agent", row.agent or DEFAULT_AGENT, record.agent),
            # Who the row is, too: two rows' labels swapped, `fleet stop coder-1` stopped
            # the manager (review r8 of #113, finding 5). A record from before it kept the
            # label has none to compare.
            ("label", row.label, record.label if record.label is not None else row.label),
            ("role", row.role, record.role),
        )
        if mine != recorded
    ]
    # Every field that differs, both sides: a socket name alone rewritten printed the
    # same path twice (SMOKE4).
    rewritten = f"its row was rewritten in the store: {'; '.join(differ)}" if differ else None
    # The start of the record's server, while the row still holds it: the store may only
    # FORGET it (`_forget_an_exited_server`, for the rows of a server that exited), after
    # which the row is judged by its record's time, as one from before it was kept. A start
    # the row says and its record does not is never taken: matched to a newer server's, it
    # would make that server's `%N` read as this row's pane.
    started_on = (
        place.server_started_at if row.server_started_at == place.server_started_at else None
    )
    placed = row.model_copy(
        update={
            "tmux_socket": place.tmux_socket,
            "pane_id": place.pane_id,
            "socket_path": Path(place.socket_path) if place.socket_path is not None else None,
            "boot_id": place.boot_id,
            "created_at": place.created_at,
            "server_started_at": started_on,
            "agent": record.agent,
            "label": record.label if record.label is not None else row.label,
            "role": record.role,
            # The account a replay runs on is the record's, so is the one a listing shows:
            # a row's slot rewritten in the store showed until a restart (SMOKE6).
            "account_slot": record.account_slot,
        }
    )
    socket_path = Path(place.socket_path) if place.socket_path is not None else None
    address = _address(place.tmux_socket, socket_path, config)
    pane = PlacedPane(address, place.pane_id, place.created_at, config, started_on)
    session = (
        PlacedSession(
            address,
            place.session_id,
            place.session or place.session_id,
            place.created_at,
            config,
            started_on,
        )
        if place.session_id
        else None
    )
    return Placed(row, placed, record, pane, session, rewritten)


def _resolve_all(
    rows: Sequence[FleetAgent],
    config: FleetSettings,
    records: Mapping[str, launch_seal.LaunchRecord | str] | None = None,
) -> list[Placement]:
    """:func:`resolve_place` for each row, each record read once (``records``, when the
    caller read them already)."""
    return [
        resolve_place(row, records.get(row.id) if records is not None else None, config)
        for row in rows
    ]


def _placed_only(placements: Sequence[Placement]) -> list[Placed]:
    return [placement for placement in placements if isinstance(placement, Placed)]


def _sessions_of(placements: Sequence[Placement]) -> list[PlacedSession]:
    """The sessions the placed rows' records name, each once (by server and id), with the
    newest record's time: a server that started after every one of them holds none."""
    found: dict[tuple[_Address, str], PlacedSession] = {}
    for placement in _placed_only(placements):
        session = placement.session
        if session is None:
            continue
        key = (session.address, session.target)
        kept = found.get(key)
        if kept is None or (
            session.created_at is not None
            and kept.created_at is not None
            and session.created_at > kept.created_at
        ):
            found[key] = session
    return list(found.values())


def _indexed_sessions(project_id: str, config: FleetSettings) -> list[PlacedSession]:
    """The sessions the project's spawns made (``launch_seal.project_sessions``), by tmux's
    id on the server each was made on: the project's, whatever its rows say."""
    return [
        PlacedSession(
            _address(
                place.tmux_socket,
                Path(place.socket_path) if place.socket_path is not None else None,
                config,
            ),
            place.session_id,
            place.name,
            place.created_at,
            config,
            place.server_started_at,
        )
        for place in launch_seal.project_sessions(project_id)
    ]


def _legacy_panes(project_id: str, config: FleetSettings) -> list[PlacedPane]:
    """The panes the census placed for ``project_id``'s rows spawned before launch records
    were kept (``launch_seal.LegacyCensus``), on the server that is still the one each was
    spawned on: a pane of an earlier server is another's (:func:`_outlived`)."""
    panes: list[PlacedPane] = []
    started: dict[_Address, datetime | None] = {}
    for record in launch_seal.legacy_records(project_id):
        place = record.window
        if place is None:
            continue
        path = Path(place.socket_path) if place.socket_path is not None else None
        pane = PlacedPane(
            _address(place.tmux_socket, path, config),
            place.pane_id,
            place.created_at,
            config,
            place.server_started_at,
        )
        if pane.address not in started:
            try:
                started[pane.address] = pane.server().started_at()
            except TmuxError:
                started[pane.address] = None
        if not pane.outlived(started[pane.address]):
            panes.append(pane)
    return panes


def _project_sessions(
    project_id: str,
    placements: Sequence[Placement],
    config: FleetSettings,
    *,
    every_legacy: bool = False,
) -> list[PlacedSession]:
    """Every session of the project's the fleet may join, take down, rename or attach to,
    each once: the ones its spawns made (:func:`_indexed_sessions`), the ones the
    ``placements``' records name (a record from before the index), and the legacy session,
    the one tmux says holds a pane the census placed (a placement's, or with
    ``every_legacy`` any of the project's census rows', :func:`_legacy_panes`). Never one
    found by a name built from the codename (review r8 of #113, findings 1, 2 and 4)."""
    found: dict[tuple[_Address, str], PlacedSession] = {}

    def keep(session: PlacedSession | None) -> None:
        if session is not None:
            found.setdefault((session.address, session.target), session)

    for session in [*_indexed_sessions(project_id, config), *_sessions_of(placements)]:
        keep(session)
    legacy = [
        placement.pane
        for placement in _placed_only(placements)
        if placement.record.legacy and placement.row.project_id == project_id
    ]
    if every_legacy:
        legacy += _legacy_panes(project_id, config)
    held: set[tuple[_Address, str]] = set()
    for pane in legacy:
        if (pane.address, pane.pane_id) in held:
            continue
        held.add((pane.address, pane.pane_id))
        keep(pane.holding_session())
    return list(found.values())


def _running_ids(project_id: str, candidates: Collection[str], config: FleetSettings) -> set[str]:
    """The ``candidates`` (row ids, ended in the store) whose windows run, read from the
    panes alone: the windows the project's spawns recorded (``launch_seal.WindowEntry``)
    among the live panes of the sessions they recorded, by tmux's id, and the panes the
    census placed for rows spawned before records among the live panes of the legacy
    session, listed by the name the census holds. Never from ``ended_at`` or a row's age:
    a manager whose row read ended in 2000 was hidden from ``fleet ls``, and a second was
    spawned beside it (review r9 of #113, finding 6). Asks tmux nothing when no candidate
    has a window recorded, and each server twice at most (its start, every live pane of it:
    r10, finding 11), however many sessions; a server that does not answer shows none."""
    wanted = set(candidates)
    if not wanted:
        return set()
    running: set[str] = set()
    entries = [entry for entry in launch_seal.project_windows(project_id) if entry.row_id in wanted]
    servers: dict[_Address, list[tuple[PlacedSession, list[launch_seal.WindowEntry]]]] = {}
    for session in _indexed_sessions(project_id, config) if entries else ():
        mine = [
            entry
            for entry in entries
            if (entry.tmux_socket, entry.session_id) == (session.address.name, session.target)
        ]
        if mine:
            servers.setdefault(session.address, []).append((session, mine))
    for sessions in servers.values():
        try:
            srv = sessions[0][0].server()
            started = srv.started_at()
            live = srv.live_panes_or_raise()
        except TmuxError:
            continue
        for session, mine in sessions:
            # A session or window recorded before the server started is another's under
            # the same id (`_outlived`): the next server numbers them afresh.
            if session.created_at is not None and _started_after(
                session.created_at, session.server_started_at, started
            ):
                continue
            alive = live.get(session.target, set())
            running |= {
                entry.row_id
                for entry in mine
                if entry.pane_id in alive
                and not _started_after(entry.created_at, entry.server_started_at, started)
            }
    legacy = [record for record in launch_seal.legacy_records(project_id) if record.id in wanted]
    groups: dict[tuple[_Address, str], list[launch_seal.LaunchRecord]] = {}
    for record in legacy:
        place = record.window
        if place is None or not place.session:
            continue
        path = Path(place.socket_path) if place.socket_path is not None else None
        groups.setdefault((_address(place.tmux_socket, path, config), place.session), []).append(
            record
        )
    for (address, name), records in groups.items():
        session = _listed_session(address, name, config)
        try:
            started = session.server().started_at()
            alive = {window.pane_id for window in session.panes_or_raise() if not window.dead}
        except TmuxError:
            continue
        running |= {
            record.id
            for record in records
            if record.window is not None
            and record.window.pane_id in alive
            and not _started_after(
                record.window.created_at, record.window.server_started_at, started
            )
        }
    return running


def _listed_session(address: _Address, name: str, config: FleetSettings) -> PlacedSession:
    """A session by the name tmux itself listed just now on ``address``: ``shutdown
    --all``'s sweep of every ``asq-`` session, which no record needs to name."""
    return PlacedSession(address, name, name, None, config)


def _recordless_pane(
    row: FleetAgent, placements: Sequence[Placement], config: FleetSettings
) -> tuple[PlacedPane, PlacedSession] | None:
    """The DEAD window of ``row``, which no launch record places, found the one way the
    fleet may: listed by tmux in a session of its project's (:func:`_project_sessions`: by
    tmux's id, on the server a record places it), under the pane id the row names, where
    no record of the project places an agent. With that session; ``None`` when there is
    none such.

    Dead only: a live window is never killed on a row's word. A fake row named a shell the
    human had opened in the project's session, or an agent whose own row was ended, and
    ``fleet stop`` killed it with no ``/exit`` (review r8 of #113, finding 9). The row's
    pane id only picks among the windows tmux listed: it is never put to tmux, and its
    socket and path are never asked; asked of ``asq-<codename>``, a session named from the
    store, a fake row and a swap of two projects' codenames had another project's window
    killed (r7, finding 1). ``placements`` are the project's live rows, placed."""
    mine = [
        placement for placement in _placed_only(placements)
        if placement.row.project_id == row.project_id
    ]  # fmt: skip
    claimed = {(placement.pane.address, placement.pane.pane_id) for placement in mine}
    for session in _project_sessions(row.project_id, mine, config):
        try:
            if not session.present_or_raise():
                continue
            windows = session.panes_or_raise()
        except TmuxError:
            continue
        for window in windows:
            if window.pane_id != row.pane_id or not window.dead:
                continue
            if (session.address, window.pane_id) in claimed:
                continue
            created = session.created_at or _now()
            pane = PlacedPane(
                session.address, window.pane_id, created, session.config, session.server_started_at
            )
            return pane, session
    return None


ServerState = Literal["up", "gone", "silent"]
"""An agent's tmux server: answering, provably gone (no socket file), or not answering."""


def server_state(agent: FleetAgent) -> tuple[ServerState, str]:
    """Whether an agent's tmux server is up, provably gone, or merely not answering.

    One answer for every caller that must not act on a pane nobody hosts: the
    captain's delivery (T2's reboot rule, 13189) and ``bt``'s re-claim for a
    converted agent (T5, 13371) — a row that reads ``waiting`` after a reboot is
    not an agent that can work.

    ``reachable`` is the one question that separates every state: True is a server
    that answered; False is tmux's own word that no server is behind the socket;
    a client that could not run, a denied socket or a wedged server RAISE, and
    that is no evidence either way. "No server" is ``gone`` only when the socket
    file is absent where the fleet resolves it (13189: a swept ``/tmp``); a file
    with nothing behind it is ``silent`` — a kill-server leaves the file, and so
    does a server alive under another ``TMUX_TMPDIR``.
    """
    srv = server_of(agent)
    try:
        if srv.reachable():
            return "up", ""
    except TmuxUnavailable as exc:
        return "silent", f"tmux could not be run ({exc})"
    except TmuxError as exc:
        return "silent", f"tmux could not be asked ({exc})"
    try:
        path = srv.socket_path()
    except TmuxError as exc:
        return "silent", str(exc)
    if not path.exists():
        return "gone", f"no socket file at {path}"
    return "silent", f"nothing answers on {path}"


def _team() -> ModuleType:
    """``services.team``, imported on first use (see the module docstring)."""
    from aisquare.services import team

    return team


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _name(project: ProjectInfo) -> str:
    """How a refusal or a note names ``project``: as every list does
    (``project.display_name``: its nickname, else its directory, else its id)."""
    return project_service.display_name(project)


# --- names (§5.7) -------------------------------------------------------------------


def session_name(codename: str) -> str:
    """``asq-<codename>`` — always targeted exactly (``=asq-…``) by the callers."""
    return SESSION_PREFIX + codename


def is_label(text: str) -> bool:
    return LABEL.match(text) is not None


def slugify(text: str, *, limit: int = 32) -> str:
    """``[^a-z0-9]+`` → ``-``, lowercased, clipped, no leading or trailing ``-``."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-")


def branch_name(codename: str, *, task_id: str | None, title: str | None) -> str:
    """``fleet/<codename>/<task-short-id>-<slug>``; ``fleet/<codename>/<slug>`` without a task.

    Every segment is built from slug-safe parts (a codename, a base32 id, a
    slug), so the result is a legal ref by construction and needs no
    ``git check-ref-format`` round trip.
    """
    parts = [f"fleet/{codename}"]
    leaf = slugify(title or "")
    if task_id:
        short = task_id.removeprefix("tsk_")[:TASK_SHORT]
        leaf = f"{short}-{leaf}" if leaf else short
    parts.append(leaf or "work")
    return "/".join(parts)


def is_git_project(root: Path) -> bool:
    """Whether the project root is a git checkout — the precondition for worktrees.

    A project root is either a repository root (``find_project_root`` resolves
    worktrees to their principal checkout, whose ``.git`` is a directory) or a
    plain directory holding several repos, which has no ``.git`` at all.
    """
    return (root / ".git").exists()


def resolve_project(ref: str | None = None, *, cwd: Path | None = None) -> ProjectInfo:
    """A project by id prefix, directory name or codename — or the active one for ``cwd``."""
    with store_session() as store:
        if ref is None:
            return active_project(store, cwd)
        return only_project(ref, store.find_projects(ref))


def only_project(ref: str, matches: list[ProjectInfo]) -> ProjectInfo:
    """The one project of ``matches``, those ``ref`` names; :class:`NoSuchProject` for none
    or several, the message naming them."""
    if not matches:
        raise NoSuchProject(f"no project matches {ref!r} (id prefix, name or codename)")
    if len(matches) > 1:
        names = ", ".join(
            f"{p.root.name or p.id}" + (f" · {p.codename}" if p.codename else "") for p in matches
        )
        raise NoSuchProject(f"{ref!r} matches several projects: {names} — use the codename")
    return matches[0]


def ensure_codename(project: ProjectInfo, store: ContextStore | None = None) -> ProjectInfo:
    """Give the project its codename if it has none (lazily, on first fleet contact).

    ``codename_for`` walks past the names a SNAPSHOT of the store called taken,
    so two processes naming two projects at the same moment can both pick the
    same candidate; the ``project_codename`` index then refuses the loser.
    That refusal is retried, not raised: the walk is deterministic, and the
    re-read includes the name that was just taken, so the next attempt lands
    one pair further on.

    A codename is not an onboarding (#139). ``attach_argv`` and ``_stop_row``
    call this too, and neither is a deliberate add: ``fleet attach`` in a
    captured directory listed it and was then refused, since it had no session
    to attach to. A codename still needs a live row to sit on, so a directory
    with none, never seen or forgotten, is CAPTURED here while ``[projects]
    capture`` is on, as the next prompt there would capture it. The deliberate
    callers onboard for themselves: :func:`spawn` once its agent is recorded,
    :func:`rename` before it writes (review of #168, round 2).

    With capture off, only a project added on purpose gets one here
    (:func:`~aisquare.services.project.tracked`): a directory nobody added comes back
    without a codename, whether it has no live row or one captured before capture
    went off with none (:func:`attach_argv` then takes the one a forgotten row holds,
    or refuses: no agent ran there). Written onto a captured row, it
    reserved a codename for a directory nobody added, on every ``fleet attach`` there,
    which then refused (review of #259, round 18). A spawn works its codename out
    without writing it (:func:`_codename_to_add`), and adds the project with it once
    the window exists, with the agent's row: added here, before the window, a spawn
    refused afterwards left the directory listed with no agent, or brought a
    forgotten one back (round 17). A codename its live row holds already comes back
    whatever capture says: held back, a stop in a directory captured under 0.8.0
    looked for its agents in the session ``asq-`` (review of #259, round 19).
    """
    if project.codename:
        return project

    def assign(store: ContextStore) -> ProjectInfo:
        stored = store.get_project(project.id)
        if stored is not None and stored.codename:
            return stored
        if not project_service.tracked(store, project):
            return project
        if stored is None:
            store.ensure_project(project)  # capture on: captured, as its next prompt would be
        for _ in range(_CODENAME_RETRIES):
            current = store.get_project(project.id)
            if current is not None and current.codename:
                return current
            name = codenames.codename_for(project.id, taken=store.codenames_in_use())
            try:
                return store.set_codename(project.id, name)
            except sqlite3.IntegrityError:
                continue  # another process took that name between the read and the write
        raise FleetError(
            f"could not give {_name(project)} a codename: every name this walk tried was "
            f"taken by another project while writing it ({_CODENAME_RETRIES} attempts)"
        )

    if store is not None:
        return assign(store)
    with store_session() as opened:
        return assign(opened)


def _kept_codename(store: ContextStore, project: ProjectInfo) -> str | None:
    """The codename ``project``'s row holds, a forgotten one's included, or ``None`` when it
    has no row. A forget keeps the codename (its tombstone), and the windows its agents left
    linger under it. One row read by its key: every row was read and built, tombstones
    included, to find it (review of #259, round 18)."""
    row = store.get_project(project.id, include_forgotten=True)
    return row.codename if row is not None else None


def _codename_to_add(store: ContextStore, project: ProjectInfo) -> str:
    """The codename a spawn names its session and branch by, for a project
    :func:`ensure_codename` gave none (``[projects] capture`` off, and nobody added it),
    read without a write: the one its row holds, captured or forgotten, else the walk past
    every name in use.
    :func:`_write_row` stores it with the project's add and the agent's row."""
    kept = _kept_codename(store, project)
    if kept is not None:
        return kept
    return codenames.codename_for(project.id, taken=store.codenames_in_use())


def _is_home_project(project: ProjectInfo) -> bool:
    """Whether ``project`` is the aisquare home's own row — the captain's home board."""
    home = paths.aisquare_home().resolve()
    return project.id == project_id_for(home)


def _is_the_captains_launch(cwd: Path | None) -> bool:
    """Whether a captain spawn carries what only its caller gives the captain: a folder of
    its own (``services.captain.brain.start``'s brain folder, the record's on a restart).
    Its tools are never the caller's: the fleet confines them at every launch, as the
    captain's agent spells it (:func:`_the_captains_tools`)."""
    return cwd is not None


def _the_captains_tools(
    adapter: AgentAdapter, home: Path, cwd: Path, launch_args: Sequence[str]
) -> list[str]:
    """The arguments that leave the captain's session no tool but its own server and no other
    MCP server, the operator's own included, as its agent spells them
    (``AgentAdapter.tools_only_args``, for ``services.captain.brain.server``), launched from
    config dir ``home`` in ``cwd`` with ``launch_args``. Asked at every launch and never
    recorded, so a restart's are today's: the interpreter the server runs under, and the
    operator's servers as their config names them now (a Codex captain's are switched off
    by name, and a name its config no longer has would stop it starting). A
    :class:`FleetError`, in one line, for an agent that cannot be confined so."""
    from aisquare.services.captain import brain

    server = brain.server()
    try:
        confined = adapter.tools_only_args(
            brain.SERVER,
            server.command,
            server.args,
            server.env,
            inherit=server.inherit,
            home=home,
            cwd=cwd,
            launch_args=launch_args,
        )
    except ValueError as exc:
        raise FleetError(f"{adapter.label} cannot be the captain here: {exc}") from exc
    if confined is None:
        raise FleetError(
            f"{adapter.label} cannot be the captain: its built-in tools cannot be switched off"
        )
    return confined


def _the_captains_environment(project: ProjectInfo) -> list[str]:
    """The captain's ``launch -e`` pairs (``services.captain.brain.launch_args``), for the
    home board ``project``: options of its window's ``aisquare launch``, so before the
    ``--`` that hands the rest of the command to the agent. After it, Codex died on
    ``unexpected argument '-e'`` and Claude Code on ``unknown option '-e'`` (SMOKE8 8a).
    Given at every launch and never recorded, as :func:`_the_captains_tools` are."""
    from aisquare.services.captain import brain

    return brain.launch_args(project.root)


def _fixed_label(role: str, wanted: str | None) -> str | None:
    """The one label ``role`` may run under, the manager's or the captain's, else ``None``.

    The rules :func:`next_label` applies before it asks who holds a label: ``wanted`` must
    be a label, and a reserved one belongs to its role. Asked on their own by a fork, whose
    label nothing at all may hold (:func:`_fork_label`), and by a take-over as another
    role, which must keep the label it has (:func:`_refuse_a_take_over_that_would_move`),
    so all three go by one copy of them.
    """
    if wanted is not None and not is_label(wanted):
        raise FleetError(
            f"label {wanted!r} is not valid — lowercase letters, digits and '-', "
            "2 to 24 characters, no '.', ':' or spaces"
        )
    if wanted == MANAGER_LABEL and role != "manager":
        raise FleetError(f"the label {MANAGER_LABEL!r} is reserved for the manager role")
    if role == "manager":
        return MANAGER_LABEL
    if not experimental.captain_enabled():
        return None  # off, `captain` is a label like any other, as on main (#240, R1-F1)
    if wanted == CAPTAIN_LABEL and role != CAPTAIN_ROLE:
        raise FleetError(f"the label {CAPTAIN_LABEL!r} is reserved for the captain")
    if role == CAPTAIN_ROLE:
        return CAPTAIN_LABEL
    return None


def next_label(
    project: ProjectInfo,
    role: str,
    *,
    wanted: str | None = None,
    task_id: str | None = None,
    store: ContextStore | None = None,
    held: Collection[str] = (),
) -> str:
    """The label a new agent gets: the asked one, or ``<role>-<task>`` / ``<role>-<n>``,
    suffixed ``-2``, ``-3`` while a LIVE agent already holds it (§5.7) — or one ``held`` by
    an agent whose row is ended in the store while it runs (review r9 of #113, finding 5)."""
    fixed = _fixed_label(role, wanted)
    if fixed is not None:
        return fixed

    def pick(store: ContextStore) -> str:
        live = {agent.label for agent in store.fleet_agents(project.id, live_only=True)}
        live |= set(held)
        if wanted is not None:
            base = wanted
        elif task_id:
            base = f"{role}-{task_id.removeprefix('tsk_')[:TASK_SHORT]}"
        else:
            base = f"{role}-1"
        if base not in live:
            return base
        stem = wanted if wanted is not None else role
        for n in range(2, 1000):
            candidate = f"{stem}-{n}"
            if candidate not in live:
                return candidate
        raise FleetError(f"no free label for {stem!r}")

    if store is not None:
        return pick(store)
    with store_session() as opened:
        return pick(opened)


def _fork_label(
    project: ProjectInfo,
    role: str,
    wanted: str | None,
    *,
    store: ContextStore,
    trees: Path | None,
    codename: str | None,
) -> str:
    """The label a FORK takes: ``wanted``, or the next ``<role>-<n>``, that NOTHING holds.

    :func:`next_label` keeps the LIVE agents apart, which is a spawn's rule: a label an
    ended agent left is free again, and the tree and the branch under it are the next
    agent's to carry on in (:func:`_worktree_for`). A fork is not that agent. The
    Spawn dialog prefills ``next_label``'s answer and sends ``None`` for it, so a fork of
    an EXITED teammate was handed that teammate's own label: it ran in the source's tree,
    on top of its uncommitted work, or moved that tree to another branch; spawn's
    :func:`_supersede` removed the source's last screen; ``fleet restart <label>`` meant
    the fork from then on; and under a label an earlier agent had left, the fork was that
    agent's stale branch checked out, while its first message said "you run in your own
    worktree on your own branch" (review of #240, finding 3).

    So a fork's label is held by nothing: no row of the project in ANY state (live,
    exited, lost or ended) and, where it gets a worktree (``trees`` is where those go,
    ``None`` when it gets none), no directory at the place its own would be made and no
    local branch it would be cut on, named by ``codename`` as :func:`spawn` names it.
    ``wanted`` that something holds is refused, naming the holder, where a spawn would
    suffix it or take it: the owner typed it, and nothing has been made yet. The manager's
    label is the only one its role has, so a fork as the manager is refused the same way
    while any manager's row holds it. With no ``wanted``, ``<role>-1``, ``-2`` … are
    walked to the first that is free, however far that is: every label the project ever
    used is held, so the walk has no count to run out at.

    Asked where :func:`spawn` picks every label, in the same store session as its other
    refusals; a parallel spawn that takes the label after that is :func:`_record`'s to
    settle, as it is for any spawn.
    """
    # The newest row under each label (the store lists them oldest first): the live one
    # when there is one.
    holders = {row.label: row for row in store.fleet_agents(project.id, live_only=False)}

    def held_by(label: str) -> str | None:
        """What holds ``label``, in a refusal's words; ``None`` when nothing does."""
        holder = holders.get(label)
        if holder is not None:
            state = "a live agent" if holder.ended_at is None else "an agent that has ended"
            return f"it is the label of {state} ({holder.id})"
        if trees is None:
            return None
        if (trees / label).exists():
            return f"its worktree would be {trees / label}, which already exists"
        if codename is None:
            return None  # no branch can be named yet; `_worktree_for` asks once it can
        branch = branch_name(codename, task_id=None, title=label)
        found = _git(project.root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        if found.returncode == 0:
            return f"its branch would be {branch}, which already exists"
        return None

    fixed = _fixed_label(role, wanted)
    label = fixed if fixed is not None else wanted
    if label is None:
        n = 1
        while held_by(f"{role}-{n}") is not None:
            n += 1
        return f"{role}-{n}"
    held = held_by(label)
    if held is None:
        return label
    way_out = (
        f", and the {role} has no other label; take it over or spawn one instead"
        if fixed is not None
        else "; pick another label"
    )
    raise FleetError(
        f"cannot fork as {label!r}: {held} — a fork takes a label, a worktree and a branch "
        f"that nothing holds{way_out}"
    )


# --- git: the fleet's one process seam besides tmux (§3.5) --------------------------


@dataclass(frozen=True)
class _GitResult:
    returncode: int
    stdout: str
    stderr: str


def _git(cwd: Path, *args: str) -> _GitResult:
    """THE git seam: worktree add/remove/list and the branch queries behind ``reap``.

    Registered in ``core.spawn.SEAMS`` as EXCLUDED and stripped, as every git seam is: git
    starts no model process, exactly like the ``rev-parse`` seams in ``core``. A non-zero
    exit is an answer (callers read ``returncode``); a missing git or a hang is not, and
    becomes a :class:`FleetError` with the reason.

    Every command runs in every git's environment (``core.spawn.git_env``), the identity
    stripped and ``GIT_OPTIONAL_LOCKS=0``: a ``git status`` that only reads still
    takes the tree's ``index.lock`` to write back what it refreshed, and a reap's status of
    an ended agent's tree, asked without the branch lock, made a respawn's ``git checkout``
    in that tree fail on the lock it held (77 of 200 overlapping checkouts, review of #259,
    round 20). A command that must write takes its locks all the same. The environment's
    switch, not ``--no-optional-locks``: the flag came in git 2.15, and a git from 2.5 to
    2.14 refused it, so every fleet git command exited 129, a ``--worktree`` spawn failed
    and a reap read each refusal as an answer; such a git ignores the variable (round 27,
    4/7). That is all the switch buys an old git. Fleet worktrees need git 2.20, the floor
    docs/fleet.md names; below 2.5 there is no ``git worktree`` or ``--git-common-dir``, and
    below 1.8.5 no ``git -C``, so every command here fails whatever the switch (round 28,
    6/10).

    No hook of the repository's runs: ``core.hooksPath`` is pointed at nothing for every
    call, so a ``post-checkout`` that lives in the tree (``.husky/``, ``.githooks/``), which
    a sandboxed agent may write, is never run by the fleet outside its sandbox (review r11
    of #113, finding 4). A commit of the agent's own, or the operator's, runs them as ever.
    """
    try:
        completed = subprocess.run(
            git_argv("-C", str(cwd), *args),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
            check=False,
            env=git_env(),
        )
    except FileNotFoundError as exc:
        raise FleetError("git is not installed (or not on PATH) — worktrees need it") from exc
    except subprocess.TimeoutExpired as exc:
        raise FleetError(f"git {args[0]} did not finish within {_GIT_TIMEOUT:.0f} s") from exc
    _moved_on()  # under the branch lock, each git command finished is a step a waiter sees
    return _GitResult(completed.returncode, completed.stdout, completed.stderr)


def _git_ok(cwd: Path, *args: str) -> str:
    """Run git and raise :class:`FleetError` carrying git's own words on failure."""
    result = _git(cwd, *args)
    if result.returncode != 0:
        reason = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise FleetError(f"git {' '.join(args)} failed: {reason}")
    return result.stdout


def _git_common_dir(root: Path) -> Path:
    """The repository's own ``.git`` directory, which every worktree of it shares: where the
    branch lock lives (:func:`_branches_locked`). One git process, so a spawn or a reap asks
    it once and passes it on: asked at each taking of the lock, a reap paid one for every
    tree it removed (review of #259, round 18)."""
    common = Path(_git_ok(root, "rev-parse", "--git-common-dir").strip())
    return common if common.is_absolute() else root / common


#: How long a spawn waits on a holder of the branch lock (:func:`_branches_locked`) that says
#: nothing before it calls that holder stuck. A holder says it moved on as each git command it
#: runs under the lock finishes (:func:`_moved_on`), and every step between two of them is
#: bounded on its own, far inside this: a git command by :data:`_GIT_TIMEOUT`, tmux's by
#: tmux's own timeout, the row by the store's busy timeout. So this is the patience for one
#: step, with room to spare. It was the patience for one holder, sized for a few git
#: commands, and a spawn holds the lock through its tree, its window and its row (review of
#: #259, round 18): on a large repo a legitimate holder went past it, and a restart, its agent
#: already stopped, was refused.
_BRANCHES_LOCK_WAIT = 4 * _GIT_TIMEOUT

_BRANCHES_HELD: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "_BRANCHES_HELD", default=None
)
"""The repository's own ``.git`` whose branch lock this context holds, for :func:`_moved_on`."""

#: How often a waiter looks at who holds the branch lock (:func:`_lock_holder`).
_HOLDER_LOOK = 1.0


@contextlib.contextmanager
def _branches_locked(common: Path, *, wait: float) -> Iterator[None]:
    """Hold the lock a spawn and a reap share over a repository's branches, for the block:
    the repository whose own ``.git`` is ``common`` (:func:`_git_common_dir`).

    ``git worktree add -b`` makes the branch and only then registers the tree that has it
    out, and a spawn that finds the branch there checks it out one git command after
    asking: a reap's sweep between the two read the branch as out of every tree and
    deleted it, and the spawn failed, or made its tree on a branch that was gone (review
    of #259, round 16). So :func:`spawn` holds this from its first question about a branch
    until its agent's row is written, :func:`_sweep_fleet_branches` deletes only while it
    holds it, asking the trees again first, and :func:`_reap_tree` records a tree's branch
    and removes the tree only while it holds it, asking the rows again first (rounds 17
    and 18). A file in
    ``common``, which every worktree of the repository shares.

    ``wait`` is the patience for a holder that does not move, not for the lock: each holder
    says beside the lock who it is (:func:`_announce_holder`), and again as each git command
    it runs under it finishes (:func:`_moved_on`), and a waiter that sees another word than
    the one it last saw starts its wait again. A queue of spawns is busy, not stuck: with one
    120 s wait for the lock, the third of three spawns in a repo whose checkout is slow was
    refused behind two ``git worktree add``, though nothing was stuck (review of #259, round
    17). Nor is a spawn that holds it through a slow checkout, its window and its row (round
    18).

    The word moves with the holder's work, never on a timer: said every :data:`_HOLDER_LOOK`
    from a thread of the hold's own (round 19), it showed the process alive, not moving, and
    a holder blocked under the lock (a state-file ``flock`` on an NFS home whose lock manager
    is gone, a ``git worktree add`` that will not die on Windows) made every waiter wait on it
    with no limit (round 20). A step that is not git says nothing and needs to remember
    nothing: each is bounded far inside ``wait``, which a step that is not, stuck, runs out.
    Raises :class:`FleetError` when a holder says nothing for ``wait`` seconds (``0`` asks
    once), or it cannot be taken at all.
    """
    path = common / "aisquare-fleet-branches.lock"
    taken = contextlib.ExitStack()
    seen = _lock_holder(common)
    deadline = time.monotonic() + wait
    while True:
        try:
            left = max(0.0, deadline - time.monotonic())
            taken.enter_context(holding(path, wait_s=min(left, _HOLDER_LOOK)))
            break
        except TimeoutError as exc:
            now = _lock_holder(common)
            if wait > 0 and now != seen:
                seen, deadline = now, time.monotonic() + wait  # another holder: still moving
            elif time.monotonic() >= deadline:
                raise FleetError(
                    f"another aisquare (a spawn or a reap) holds {path}"
                    + (
                        f", and has held it for {wait:g} s without letting go — it may be "
                        "stuck; try again once it ends"
                        if wait > 0
                        else " — try again"
                    )
                ) from exc
        except OSError as exc:
            raise FleetError(f"{path} could not be locked ({exc})") from exc
    with taken:
        _announce_holder(common)
        held = _BRANCHES_HELD.set(common)
        try:
            yield
        finally:
            _BRANCHES_HELD.reset(held)


def _holder_file(common: Path) -> Path:
    return common / "aisquare-fleet-branches.holder"


def _announce_holder(common: Path) -> None:
    """Say, beside the branch lock this taking holds, that it does, as of now: a token no
    other taking, nor any other step of this one (:func:`_moved_on`), has, which a waiter
    compares (:func:`_branches_locked`). Not in the lock file,
    which Windows locks byte 0 of against every other handle. Best effort: unsaid, a waiter
    is only as patient as it was before holders said."""
    with contextlib.suppress(OSError):
        _holder_file(common).write_text(f"{os.getpid()} {time.time_ns()}\n", encoding="utf-8")


def _moved_on() -> None:
    """Say, beside the branch lock this context holds, that its holder has finished a step: a
    new word, which a waiter reads as a holder still moving (:func:`_branches_locked`).
    Nothing when it holds none."""
    common = _BRANCHES_HELD.get()
    if common is not None:
        _announce_holder(common)


def _lock_holder(common: Path) -> str:
    """The token the branch lock's latest holder announced, or ``""`` when none can be read."""
    try:
        return _holder_file(common).read_text(encoding="utf-8")
    except OSError:
        return ""


def _exclude_worktrees(common: Path, worktree_dir: str, notes: list[str]) -> None:
    """Add ``<worktree_dir>/`` to ``.git/info/exclude`` once — never to a tracked file.
    ``common`` is the repository's own ``.git`` (:func:`_git_common_dir`).

    Fail-open: a read-only ``.git`` costs a note and an untracked directory in
    ``git status``, never the spawn.
    """
    line = worktree_dir.rstrip("/") + "/"
    try:
        exclude = common / "info" / "exclude"
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if line in existing.splitlines():
            return
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write(f"# aisquare fleet worktrees (docs/plans/fleet-tui.md §3.5)\n{line}\n")
    except OSError as exc:
        notes.append(
            f"could not add {line} to .git/info/exclude ({exc}) — "
            "git status will list the worktrees as untracked"
        )


def _empty_dir(path: Path) -> bool:
    """Whether ``path`` is a directory with nothing in it (an unreadable one is not)."""
    try:
        return path.is_dir() and next(path.iterdir(), None) is None
    except OSError:
        return False


def _reuse_worktree(
    root: Path,
    path: Path,
    branch: str,
    notes: list[str],
    *,
    refuse_if_taken: Callable[[], None] | None = None,
) -> None:
    """Put an existing worktree ON ``branch`` before an agent is started in it.

    A label outlives one task — an ended agent frees it (§5.7) — so the tree
    left behind may still sit on the PREVIOUS task's branch. Handing it over as
    it is would make an agent spawned for task B commit to
    ``fleet/<codename>/<taskA>-…``, which is the one thing
    :func:`branch_name` promises does not happen. Clean and on another branch:
    switched. Dirty and on another branch: refused, because the uncommitted
    work is somebody's and neither switching over it nor ignoring it is this
    function's call to make.

    ``refuse_if_taken`` is asked again in the last moment before the checkout —
    it raises when a LIVE agent holds this path. A spawn's row is written only
    after its tmux window exists, so the loser of a label race can pass
    :func:`_refuse_occupied_worktree` with no holder in sight and arrive here
    while the winner's row is still on its way; ``git checkout`` is then run in
    a directory another agent is already working in, and :func:`_relabel`'s
    later refusal of the ROW cannot undo it. Asking as late as possible shrinks
    that window from "a ``git worktree add`` plus a tmux spawn plus a store
    write" to the two git commands below; closing it entirely needs the label
    reserved in the store before anything is created, which no index here can
    express.
    """
    try:
        current = _tree_branch(path)  # the one reading of a tree's branch
    except FleetError:
        current = None  # an unborn HEAD: no branch to name, as on a detached one
    where = f"on {current}" if current is not None else "on a detached HEAD"
    if current == branch:
        notes.append(f"reusing the existing worktree at {path} (already on {branch})")
        return
    dirty = _git(path, "status", "--porcelain")
    if dirty.returncode != 0 or dirty.stdout.strip():
        raise FleetError(
            f"the worktree at {path} holds uncommitted work {where} and this agent needs "
            f"{branch} — commit or stash it there, or spawn under another label"
        )
    if refuse_if_taken is not None:
        refuse_if_taken()
    if _branch_exists(root, branch):
        _git_ok(path, "checkout", branch, "--")  # the branch, never a path of that name
    else:
        _git_ok(path, "checkout", "-b", branch)
    notes.append(f"reusing the existing worktree at {path} — it was {where}, now on {branch}")


def _worktree_for(
    root: Path,
    worktree_dir: str,
    label: str,
    branch: str,
    notes: list[str],
    *,
    refuse_if_taken: Callable[[], None] | None,
    own: Path | None,
    own_branch: str | None,
    common: Path,
    start_point: str | None = None,
) -> Path:
    """``<root>/<worktree_dir>/<label>`` on ``branch``, created or reused.

    ``start_point`` is a FORK's (:func:`hand_off`): the commit its source's tree is on,
    where the fork's own ``branch`` is cut (``git worktree add -b``). With it nothing is
    reused: a tree already at the path, or a branch already under the name, is another
    agent's work, and the spawn is refused with that tree not started in or switched, and
    that branch not checked out. A fork handed the label of its exited source ran in that
    source's tree, on top of its uncommitted work, or moved that tree to another branch;
    under a label an earlier agent had left, its "own" worktree was that agent's stale
    branch, not its source's commit (review of #240, finding 3). :func:`_fork_label` picks
    a label that has neither, so what is refused here was made since, by a parallel spawn.
    Without a start point, every other spawn's case, a new branch is cut at the checkout's
    HEAD and everything below applies.

    Reuse is deliberate: a coder respawned on the same task after review
    findings must land in the tree that holds its branch, not beside it. A
    branch that already exists is checked out rather than recreated, so the
    second spawn continues the first one's work — and a reused tree is put on
    the branch THIS spawn asked for (see :func:`_reuse_worktree`), never handed
    over on whatever branch the last agent left it on.

    A restarted or switched agent's replacement never comes here: it works in the
    tree its launch record names, as it stands (``spawn(own_worktree=…)``). Put on a
    branch rebuilt from the row's task and the codename, a tree holding uncommitted
    work was refused AFTER ``restart`` or ``switch`` had stopped its agent — lost,
    its claims released — and a clean one was moved to a branch the agent never
    worked on (review of #203, final round, FLEET-2).

    A tree ``own`` names that is gone is made again on ``own_branch``, the branch its row
    recorded it on, or refused when there is none or it is gone (:func:`_remake_worktree`):
    never made anew from the main checkout's HEAD, without the agent's commits.

    ``refuse_if_taken`` reaches :func:`_reuse_worktree`, which asks it once more
    before switching branches; a tree this call CREATES cannot be another
    agent's, because ``git worktree add`` refuses an existing directory.

    The caller holds the branch lock (:func:`_branches_locked`), which a reap deletes
    branches and removes trees under: :func:`spawn` from before the first question about a
    branch until the agent's row is written. ``common`` is the repository's own ``.git``,
    which it was taken in.
    """
    if own is not None and not own.exists():
        return _remake_worktree(root, own, own_branch, notes)
    path = root / worktree_dir / label
    if path.exists():
        if start_point is not None:
            raise FleetError(
                f"{path} already exists — a fork's worktree is made new, at the commit its "
                "source is on, and never one that is already there; fork under another label"
            )
        if (path / ".git").exists():
            _reuse_worktree(root, path, branch, notes, refuse_if_taken=refuse_if_taken)
            return path
        # A `git worktree add` still in flight has made the directory and not
        # yet its .git; saying "remove it" about that would be wrong advice.
        detail = (
            "it is empty — another spawn may be creating it right now; try again"
            if _empty_dir(path)
            else "remove it or pick another label"
        )
        raise FleetError(f"{path} exists and is not a git worktree — {detail}")
    if _branch_exists(root, branch):
        if start_point is not None:
            raise FleetError(
                f"branch {branch} already exists — a fork's branch is cut new, at the commit "
                "its source is on, and never one that is already there; fork under another "
                "label"
            )
        _add_worktree(root, path, branch)
        notes.append(f"branch {branch} already existed — checked it out")
    else:
        if start_point is None:
            _add_worktree(root, path, branch, new=True)
        else:
            _add_worktree(root, path, branch, new=True, start_point=start_point)
    _exclude_worktrees(common, worktree_dir, notes)
    return path


def _branch_exists(root: Path, branch: str) -> bool:
    """Whether the local branch ``branch`` is there."""
    return _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0


def _add_worktree(
    root: Path, path: Path, branch: str, *, new: bool = False, start_point: str | None = None
) -> None:
    """``git worktree add`` at ``path`` on ``branch`` — a new one when ``new``, cut at
    ``start_point`` (a fork's, :func:`_worktree_for`) or else at HEAD.

    A worktree whose directory was deleted by hand stays registered and blocks ``add`` at
    the same path, and holds its branch from any other tree; ``git worktree prune`` is
    idempotent and cheap. It forgets every such tree's branch, which nothing here reads:
    an agent's branch is on its row (:attr:`FleetAgent.branch`)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "prune")
    if new:
        _git_ok(root, "worktree", "add", str(path), "-b", branch, *filter(None, [start_point]))
    else:
        _git_ok(root, "worktree", "add", str(path), branch)


def _worktree_branches(root: Path) -> dict[Path, str]:
    """Resolved worktree path → branch name, from ``git worktree list --porcelain``."""
    listed = _git(root, "worktree", "list", "--porcelain")
    if listed.returncode != 0:
        return {}
    branches: dict[Path, str] = {}
    current: Path | None = None
    for line in listed.stdout.splitlines():
        if line.startswith("worktree "):
            current = Path(line.removeprefix("worktree ")).resolve()
        elif line.startswith("branch ") and current is not None:
            branches[current] = line.removeprefix("branch ").removeprefix("refs/heads/")
        elif not line:
            current = None
    return branches


def _branches_in_use(root: Path, common: Path) -> set[str] | None:
    """The branches ``git branch -D`` refuses to delete, as git's own ``branch_checked_out``
    reads them: each one a worktree of the repository has out, is rebasing (its
    ``rebase-merge`` or ``rebase-apply`` ``head-name``, and the refs a ``rebase
    --update-refs`` moves when it ends) or is bisecting from (``BISECT_START``). ``None``
    when git would not list the worktrees, or a tree's state could not be read: nothing is
    known to be free then. ``common`` is the repository's own ``.git``
    (:func:`_git_common_dir`), which holds the main checkout's state, and each linked tree's
    under ``worktrees/``.

    ``git worktree list`` says ``detached`` of a tree mid-rebase or mid-bisect. Asked of it
    alone, the sweep deleted a merged ``fleet/x`` that ``../wt`` was rebasing, which ``branch
    -D`` refuses; the ``rebase --continue`` there failed on the ref, and the rebased commits
    were left on a detached HEAD (review of #259, round 34, 1/10). Read as no tree at all,
    a ``worktree list`` that failed let every branch go.
    """
    listed = _git(root, "worktree", "list", "--porcelain")
    if listed.returncode != 0:
        return None
    heads = "refs/heads/"
    used = {
        line.removeprefix("branch " + heads)
        for line in listed.stdout.splitlines()
        if line.startswith("branch " + heads)
    }
    trees = common / "worktrees"
    try:
        for state in [common, *(sorted(trees.iterdir()) if trees.is_dir() else [])]:
            rebasing = [
                _state_file(state / "rebase-merge" / "head-name"),
                _state_file(state / "rebase-apply" / "head-name"),
                # A ref, its tip before, its tip after: each third line is a ref.
                *_state_file(state / "rebase-merge" / "update-refs").splitlines()[::3],
            ]
            used.update(ref.strip().removeprefix(heads) for ref in rebasing if ref.strip())
            bisecting = _state_file(state / "BISECT_START").strip()  # a branch, or a commit
            if bisecting:
                used.add(bisecting)
    except OSError:
        return None
    return used


def _state_file(path: Path) -> str:
    """What a worktree's state file at ``path`` says; ``""`` when there is none."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, NotADirectoryError):
        return ""


def _default_branch(root: Path) -> str:
    """The branch fleet work merges into: origin's HEAD, else main/master, else HEAD."""
    head = _git(root, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD")
    if head.returncode == 0 and head.stdout.strip():
        return head.stdout.strip()
    for name in ("main", "master"):
        if _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}").returncode == 0:
            return name
    return "HEAD"


#: The prefix of every branch :func:`branch_name` makes: the only branches reap deletes.
_FLEET_BRANCH_PREFIX = "fleet/"


def _tree_branch(tree: Path) -> str | None:
    """The branch the worktree ``tree`` is on, as git says now; ``None`` on a detached HEAD.

    Raises :class:`FleetError` when git cannot say: no worktree there (asked in a
    directory without its ``.git``, git would answer for the main checkout above it), or
    no answer — an unborn HEAD, say. A detached HEAD is not a failure: ``--abbrev-ref``
    exits 0 and prints the literal ``HEAD`` (measured with git 2.x). Every question about
    a tree's branch is asked here: at spawn, at reuse, at stop and at reap."""
    if not (tree / ".git").exists():
        raise FleetError(f"no worktree at {tree}")
    head = _git(tree, "rev-parse", "--abbrev-ref", "HEAD")
    if head.returncode != 0:
        raise FleetError(f"git could not say which branch {tree} is on")
    current = head.stdout.strip()
    return None if current == "HEAD" else current  # --abbrev-ref says HEAD when detached


def _spawned_on(tree: Path) -> str | None:
    """The branch a spawn's worktree is on, for its row; ``None`` when git cannot say,
    which costs the spawn nothing but a restart's way back to a tree that is gone."""
    try:
        return _tree_branch(tree)
    except FleetError:
        return None


def _in_its_recorded_tree(placement: Placement) -> FleetAgent:
    """The row with the tree its launch record names (``cwd``, ``worktree``): the one git is
    asked about at stop and at reap (:func:`_record_the_branch`), never a path a row
    rewritten in the store names (``core.launch_seal``). A row no record places is its own."""
    if isinstance(placement, Placed):
        return placement.row.model_copy(
            update={"cwd": Path(placement.record.cwd), "worktree": placement.record.worktree}
        )
    return placement.row


def _record_the_branch(store: ContextStore, agent: FleetAgent) -> None:
    """Record on ``agent``'s row the branch its worktree is on now (:attr:`FleetAgent.branch`),
    while the tree is there to ask: at stop, and at reap before the tree can go. An agent
    that ran ``git checkout -b fix/auth`` is restarted there, not on the branch it was
    spawned on. A tree that is gone, or that git cannot read, leaves what was recorded, and
    so does a detached HEAD (mid-rebase, mid-bisect, a ``git checkout <sha>``): written over
    with no branch, the restart of its gone tree was refused while its branch still held the
    agent's commits (review of #259, round 16)."""
    if not agent.worktree:
        return
    try:
        branch = _tree_branch(agent.cwd)
    except FleetError:
        return
    if branch is not None and branch != agent.branch:
        store.record_fleet_agent_branch(agent.id, branch)


def _branch_of_a_gone_tree(root: Path, own: Path, branch: str | None) -> str:
    """``branch``, the one its row recorded ``own`` on — the worktree an agent worked in,
    gone now — when :func:`_remake_worktree` can make the tree again there.

    Asked of the row, never of git's worktree list, which every spawn's prune empties, nor
    of anything keyed by the tree's path, which a later agent under the same label reuses
    (review of #259, round 15). Writes nothing, so a restart refused afterwards leaves
    nothing behind. Raises :class:`FleetError`, saying what it found, when the row recorded
    no branch, the branch is gone, or another tree has it out: carried on in a new tree,
    the agent would work without its commits.
    """
    instead = (
        "carried on in a new tree, it would work without its commits. Spawn a new agent "
        "for its task instead, which checks out the task's branch when there is one"
    )
    if branch is None:
        raise FleetError(
            f"its worktree {own} is gone, and its row records no branch it was on (a "
            "detached HEAD, or an agent spawned before 0.8.1): " + instead
        )
    if not _branch_exists(root, branch):
        raise FleetError(f"its worktree {own} is gone, and so is its branch {branch}: " + instead)
    gone = own.resolve()
    holder = next(
        (
            tree
            for tree, b in _worktree_branches(root).items()
            if b == branch and tree != gone and tree.exists()
        ),
        None,
    )
    if holder is not None:
        # The main checkout cannot be removed — trying an ended agent's work there is common.
        way = (
            "switch that checkout to another branch"
            if holder == root.resolve()
            else "remove that tree"
        )
        raise FleetError(
            f"its worktree {own} is gone, and its branch {branch} is checked out in {holder} "
            f"now — carry on there, or {way} first"
        )
    return branch


def _remake_worktree(root: Path, own: Path, branch: str | None, notes: list[str]) -> Path:
    """Make ``own`` again — the worktree a restarted or switched agent worked in, gone
    now — on ``branch``, the one its row recorded (:func:`_branch_of_a_gone_tree`), so the
    replacement carries on with every commit there.

    A tree deleted by hand, its branch still holding the work, was made again on it until
    round 13 of #259 refused every tree that was gone and blamed ``fleet reap`` (round
    14). One reap removed is made again on the branch reap kept; a branch reap deleted
    had nothing the default branch lacks.
    """
    branch = _branch_of_a_gone_tree(root, own, branch)
    _add_worktree(root, own, branch)
    notes.append(f"its worktree {own} was gone — made it again on its branch {branch}")
    return own


def _pushed(root: Path, rev: str, default: str) -> bool:
    """Whether commit ``rev`` is on the default branch or on any remote-tracking branch:
    pushed somewhere, so the worktree is not where it lives alone."""
    if _git(root, "merge-base", "--is-ancestor", rev, default).returncode == 0:
        return True
    remote = _git(root, "for-each-ref", "--contains", rev, "--format=%(refname)", "refs/remotes")
    return remote.returncode == 0 and bool(remote.stdout.strip())


def _why_kept(root: Path, tree: Path, default: str) -> str | None:
    """Why ``tree`` must stay — what in it exists only there — or ``None`` when nothing does.

    Untracked files are asked for by name: ``status.showUntrackedFiles=no`` hides them
    from a plain ``status``, and ``git worktree remove`` honours it too, so the tree read
    clean and the file went with it."""
    status = _git(tree, "status", "--porcelain", "--untracked-files=normal")
    if status.returncode != 0:
        return f"git status failed there ({status.stderr.strip() or status.returncode})"
    if status.stdout.strip():
        return "uncommitted changes"
    head = _git(tree, "rev-parse", "--verify", "--quiet", "HEAD")
    if head.returncode != 0 or not head.stdout.strip():
        return "no commit to check"
    if not _pushed(root, head.stdout.strip(), default):
        return "commits on no remote branch yet — push them, then reap again"
    return None


def _remove_finished_worktrees(
    store: ContextStore, project: ProjectInfo, report: ReapReport
) -> None:
    """Remove the worktree of each ENDED agent that holds nothing found only there.

    Nothing only there: the tree is clean (no uncommitted change, no untracked file that
    is not ignored) and its HEAD is on the default branch or on ANY remote-tracking
    branch — pushed somewhere. Merged into the default branch was the rule, and a
    coder that pushes onto an integration branch (``push HEAD:feat/…``) never was:
    its worktrees stayed for good, 15 of them and 1.2 GB on one night. Each tree kept
    is on the report with why. A tree a live agent still sits in (a respawn reused it)
    is never touched.

    A tree, or a project root, that cannot be looked at (a folder above it this user may not
    search, a mount that answered ``EIO``) is kept and said so, and the sweep goes on. Asked
    with ``Path.exists``, which raised ``PermissionError`` for one up to 3.13, one such root
    ended ``reap --all`` in a traceback, every later project unreaped and ``--json`` printing
    nothing, and from 3.14 it was passed over without a word (review of #259, round 43, 1/7).

    The tree goes, and its branch stays, recorded on the newest row at its path just before
    the tree went, under the branch lock (:func:`_reap_tree`), whatever its name: a spawn for
    the task checks it out (:func:`_worktree_for`), and a restart of the agent makes its tree
    again there (:func:`_remake_worktree`). Deleted when pushed anywhere, as the tree is, it
    took the task's commits with it: a coder spawned again for the task, after a reviewer
    reopened it, asks for the same branch (:func:`branch_name`), found none, and started a
    new one from the main checkout's HEAD, without its own pushed work, and nothing said so
    (review of #259, round 14). Which ``fleet/…`` branches go is :func:`_sweep_fleet_branches`,
    asked on every reap of a project an agent with a tree has ended in, whether or not a tree
    was removed.
    """
    root = project.root
    read_at = _now()
    rows = store.fleet_agents(project.id)
    if not any(row.ended_at is not None and row.worktree for row in rows):
        # No ended agent ever had a tree here — a project no agent ran in, the captured
        # directories `reap()` walks included: no tree and no branch is the fleet's to take,
        # and git is not asked (review of #259, round 16).
        return
    try:
        if not _present(root / ".git"):  # `is_git_project`, asked alike on every Python
            return
    except OSError as exc:
        report.worktrees_kept.append((root, f"not looked at, nor any tree in it: {_why(exc)}"))
        return
    try:
        default = _default_branch(root)
        common = _git_common_dir(root)  # once: the branch lock is taken for each tree
    except FleetError:
        return  # no git here: nothing can be removed safely, so nothing is
    # The row whose tree a path is now: the NEWEST there, the one a restart of its label
    # acts on (`fleet_agent_by_label`). A label reused — a restart, a switch, a respawn
    # for its task — leaves older rows at the same path, each with the branch it recorded
    # when it ended; the tree's branch written to the oldest was a restart's of the newest
    # rebuilding on the branch before it (review of #259, round 16).
    trees = [(_tree_at(agent.cwd), agent) for agent in rows]
    owners = {here: agent for here, agent in trees if here is not None and agent.worktree}
    in_use = {here for here, agent in trees if here is not None and agent.ended_at is None}
    in_use.add(root.resolve())
    for path, agent in owners.items():
        if path in in_use or agent.ended_at is None:
            continue
        try:
            if not _present(agent.cwd):
                continue
        except OSError as exc:
            report.worktrees_kept.append((agent.cwd, f"not looked at: {_why(exc)}"))
            continue
        removed, kept = _reap_tree(store, project, agent, default, common, read_at)
        if removed:
            report.worktrees_removed.append(agent.cwd)
        elif kept is not None:
            report.worktrees_kept.append((agent.cwd, kept))
    # Read again: the branches recorded just now are the ones the sweep asks about.
    _sweep_fleet_branches(root, common, default, store.fleet_agents(project.id), report)


def _present(path: Path) -> bool:
    """Whether ``path`` is there, as ``Path.exists`` asks, with what cannot be asked raised as
    the ``OSError`` it is on every Python: ``Path.exists`` raised it up to 3.13 and says False
    for it from 3.14. Gone (``workspace._gone``) is False."""
    try:
        os.stat(path)
    except OSError as exc:
        if _gone(exc):
            return False
        raise
    return True


def _tree_at(path: Path) -> Path | None:
    """``path`` resolved, as a stored root is (``workspace._resolved``); ``None`` through a
    symlink loop, where no tree is. Resolved raw, a loop raised ``RuntimeError`` on 3.11 and
    3.12, out of the reap of every project (review of #259, round 43, 1/7)."""
    try:
        return _resolved(path)
    except OSError:
        return None


def _why(exc: OSError) -> str:
    """What the system said of a path it would not look at, in its own words."""
    return exc.strerror or str(exc)


def _reap_tree(
    store: ContextStore,
    project: ProjectInfo,
    agent: FleetAgent,
    default: str,
    common: Path,
    read_at: datetime,
) -> tuple[bool, str | None]:
    """Reap ``agent``'s tree: ``agent`` ended, and was the newest row at the tree's path when
    :func:`reap` read the rows at ``read_at``. Record on its row the branch the tree is on
    (:func:`_record_the_branch`), then ``git worktree remove`` it when it holds nothing found
    only there (:func:`_why_kept`). Whether it went, and when it did not, why (``None`` for a
    tree another agent took, left unsaid, as a tree in use always was).

    All of it under the branch lock, which :func:`spawn` makes or reuses a tree under and
    holds until its agent's row is written, and only while no other row has been at the
    tree since ``read_at``: none live there, and none that ended since. Reap decided from
    the rows it read before its loop, and each tree costs it a few git calls. A spawn or
    restart for the label reused the tree meanwhile, and the tree, read as ended, clean and
    pushed, was removed under the new agent (review of #259, round 17), and the branch the
    spawn checked out there was recorded on the ended row. The branch that row had
    recorded, then named by no row, was never swept, and a restart of a gone tree was made
    on the spawn's (round 18). Never waited for: a spawn holds the lock through a ``git
    worktree add`` and a window, and the next reap asks again.

    What the tree holds (:func:`_why_kept`) is asked first, without the lock: it only says
    whether to try. Asked under it, a reap held the lock through ``git status`` and the push
    check of every tree it kept, and while a spawn held it, a tree with uncommitted work was
    reported as "try again" rather than as what to commit (review of #259, round 19). So a
    tree that holds something is said to, held lock or not; one that looked removable is
    removed only once the rows say, under the lock, that no other agent took it.

    With the lock busy, what the tree held is said as what it held when asked, beside "not
    removed now", never as a settled reason to keep it: the holder may be a spawn reusing
    this very tree, mid-checkout, which the rows read under the lock would have left unsaid
    as another agent's. Said as the reason, a reap during a respawn reported the new agent's
    half-checked-out tree as kept for the ended one's uncommitted changes (round 20).
    """
    tree = agent.cwd
    here = tree.resolve()
    try:
        kept = _why_kept(project.root, tree, default)
    except FleetError as exc:
        kept = f"its contents could not be asked of git ({exc})"
    try:
        with _branches_locked(common, wait=0):
            since = [
                *store.fleet_agents(project.id, live_only=True),
                *store.fleet_agents_ended_since(project.id, read_at),
            ]
            if any(row.id != agent.id and _tree_at(row.cwd) == here for row in since):
                return False, None
            _record_the_branch(store, agent)
            if kept is not None:
                return False, kept
            removal = _git(project.root, "worktree", "remove", str(tree))
    except FleetError as exc:
        held = f"; when asked just before, it held: {kept}" if kept is not None else ""
        return False, f"not removed now: {exc}{held}"
    if removal.returncode != 0:
        return False, f"git worktree remove refused: {removal.stderr.strip() or 'no reason'}"
    return True, None


def _fleet_branches(root: Path, *flags: str) -> dict[str, str] | None:
    """The local ``fleet/…`` branches and their tips, as ``git for-each-ref <flags>`` lists
    them; ``None`` when git would not."""
    listed = _git(
        root, "for-each-ref", "--format=%(objectname) %(refname)", *flags, "refs/heads/fleet"
    )
    if listed.returncode != 0:
        return None
    tips: dict[str, str] = {}
    for line in listed.stdout.splitlines():
        tip, _, ref = line.partition(" ")
        name = ref.removeprefix("refs/heads/")
        if name.startswith(_FLEET_BRANCH_PREFIX):
            tips[name] = tip
    return tips


def _sweep_fleet_branches(
    root: Path, common: Path, default: str, rows: Sequence[FleetAgent], report: ReapReport
) -> None:
    """Delete each ``fleet/…`` branch an ENDED row of ``rows`` recorded
    (:attr:`FleetAgent.branch`) that no worktree has out, once the default branch has its
    work; name the rest, kept, in ``branches_kept``.

    Its work is there when the default branch holds its tip, or when it is pushed and
    every change it makes is on the default branch already (:func:`_squashed`): a squash
    or rebase merge, whose tip the default branch never holds. Asked of every such branch
    on every reap: a branch kept when its tree went was never looked at again, so it stayed
    for good once merged, and the repo collected one a task (review of #259, round 15).
    A branch some worktree has out, the tree gone by hand or not, or is rebasing or
    bisecting (:func:`_branches_in_use`, what ``branch -D`` refuses: review of #259, round
    34, 1/10), is left alone, and so is one a LIVE row names; when the trees cannot be
    asked, every branch is. The deletes are made under the lock a spawn makes its tree under
    (:func:`_branches_locked`), after asking the trees again: never meanwhile, as a branch
    a spawn has just made, or is about to check out, reads as out of every tree. Each is
    deleted at the tip that was judged and no other (``update-ref -d <ref> <tip>``, which
    git refuses once the ref moved), and the next reap judges it again: ``branch -D``
    deleted whatever the ref held by then, and a tip moved after the check (a ``git fetch
    origin fleet/x:fleet/x``, a ``branch -f``) went with commits nobody judged (review of
    #259, round 33, 6/10). Its ``branch.<name>`` config goes too, as ``branch -D`` takes it.

    Only a branch a row recorded is the fleet's. Any ``fleet/…`` branch was, and every reap
    deleted a merged ``git branch fleet/my-own-notes`` in each project it walked, one no
    agent ever ran in included (review of #259, round 16). A branch no row names — made by
    hand, or one its agent left for another — is left as it is, and so is a recorded one
    that is not ``fleet/…`` (``fix/auth``): only branches :func:`branch_name` makes go.
    """
    live = {row.branch for row in rows if row.ended_at is None}
    recorded = {
        row.branch
        for row in rows
        if row.ended_at is not None
        and row.branch is not None
        and row.branch.startswith(_FLEET_BRANCH_PREFIX)
        and row.branch not in live
    }
    if not recorded:
        return
    # Of the recorded ones, those still there: ended rows are kept for good, so `recorded`
    # is never empty again once a project ran a coder, and with them all swept, the trees
    # were asked (`git worktree list` and their state files) on every reap for nothing
    # (review of #259, round 35, 5/8).
    every = {b: tip for b, tip in (_fleet_branches(root) or {}).items() if b in recorded}
    if not every:
        return
    merged = _fleet_branches(root, f"--merged={default}")
    out = None if merged is None else _branches_in_use(root, common)
    if merged is None or out is None:
        return
    asked = {b: tip for b, tip in every.items() if b not in out}
    squashed = _squashed(root, {b: tip for b, tip in asked.items() if b not in merged}, default)
    done: list[str] = []
    for branch in asked:
        if branch in merged or branch in squashed:
            done.append(branch)
        else:
            report.branches_kept.append((root, branch))
    if not done:
        return
    try:
        # Never waited for: a spawn holds it through a whole `git worktree add`, and the
        # next reap deletes what this one could not.
        with _branches_locked(common, wait=0):
            out = _branches_in_use(root, common)  # asked again, under the lock
            for branch in done:
                if out is None or branch in out:
                    continue
                ref = f"refs/heads/{branch}"
                if _git(root, "update-ref", "-d", ref, every[branch]).returncode == 0:
                    _git(root, "config", "--remove-section", f"branch.{branch}")  # none: fine
                    report.branches_deleted.append((root, branch))
    except FleetError:
        return


def _squashed(root: Path, tips: Mapping[str, str], default: str) -> set[str]:
    """The branches of ``tips`` (name → tip, none merged into ``default``) that are pushed
    and that merging into ``default`` would change nothing: every change each makes is there
    already, though ``default`` lacks its tip.

    Pushed, so its commits are safe on the remote once the branch goes: asked of them all
    in one ``rev-list``, which prints each tip neither ``default`` nor a remote-tracking
    branch reaches (with the commits under it only it has). Then, of each pushed one, git's
    own merge (``merge-tree --write-tree``, git 2.38): its tree is ``default``'s. An older
    git, or a merge with conflicts, says no, and the branch is kept. One git call a pushed
    branch and none an unpushed one, on every reap: five each were asked, the kept ones'
    every time (review of #259, round 16)."""
    if not tips:
        return set()
    # Ended by `--`: a top-level `main/` made `main` "both revision and filename", rev-list
    # exited 128, and every squash-merged branch read as unpushed (review of #259, round 17).
    alone = _git(root, "rev-list", *tips.values(), "--not", default, "--remotes", "--")
    tree = _git(root, "rev-parse", "--verify", "--quiet", f"{default}^{{tree}}")
    if alone.returncode != 0 or tree.returncode != 0:
        return set()
    unpushed = set(alone.stdout.split())
    same: set[str] = set()
    for branch, tip in tips.items():
        if tip in unpushed:
            continue
        merge = _git(root, "merge-tree", "--write-tree", default, tip)
        if merge.returncode == 0 and merge.stdout.split()[:1] == tree.stdout.split():
            same.add(branch)
    return same


# --- what tmux knows about a pane -----------------------------------------------------


@dataclass(frozen=True)
class _PaneView:
    """The facts state derivation needs, whichever tmux query produced them."""

    dead: bool
    dead_status: int | None
    current_command: str
    last_output: datetime | None
    """When the window last produced output, from ``#{window_activity}``; ``None``
    when tmux did not say."""


class _Held:
    """What :func:`_observe` says of a live row whose server is NOT the one that answered at its
    socket: held as unasked (:func:`_on_another_server`), its agent perhaps running where no
    tmux client reaches it. :data:`HELD` is the one instance."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "HELD"


HELD = _Held()
"""Under a held row's id in :data:`_Observed`. Not ``None``, which is what a plain ``.get()``
gives for a row whose pane is gone too: held rows were ``None`` there, so a reader that took
the map's word without :func:`_own_view` read a held row as gone, and ended it under its
running agent; ``reap`` was safe only because :func:`_view` screened the row first (review of
#259, round 3). A reader that reaches a pane's facts now has to say what a held row is."""

_Observed = dict[str, "_PaneView | _Held"]
"""What one server said, by row id: the facts of the row's pane; :data:`HELD` for a row whose
server is NOT the one that answered at its socket (:func:`_observe`); and no entry for a row
whose pane is gone."""


def _agent_running(current_command: str) -> bool:
    """Whether the pane's foreground process is the agent, not a shell or our launcher.

    ``python…`` is ``aisquare launch`` still resolving the role; ``tmux`` is the
    pane before its first exec; a shell is what remains when an agent started
    through one has exited. None of them is a place to type into.
    """
    command = current_command.strip()
    return bool(command) and command not in _NOT_AN_AGENT and not command.startswith("python")


def _activity_times(srv: TmuxServer) -> dict[str, datetime]:
    """Pane id → when its window last produced output, for every pane on the server.

    One ``list-panes -a`` covers every session, so a pane living under a session
    name tmux was never told to rename is found too. Fail-open: an unreadable
    answer costs the ``working`` / ``waiting`` distinction for hookless agents
    (they read as waiting), never the listing.
    """
    try:
        out = srv.run("list-panes", "-a", "-F", _ACTIVITY_FORMAT)
    except TmuxError:
        return {}
    times: dict[str, datetime] = {}
    for line in out.splitlines():
        pane_id, _, epoch = line.partition(_SEP)
        if pane_id and epoch.strip().isdigit():
            times[pane_id] = datetime.fromtimestamp(int(epoch), tz=UTC)
    return times


def _server_answers(srv: TmuxServer) -> bool:
    """Whether this tmux server answers a question at all — not what it answers.

    ``list_windows``, ``pane_facts`` and :func:`_activity_times` report a server
    that cannot be REACHED exactly as they report a pane that is not there: an
    empty answer, no exception. tmux exits 1 with ``error connecting to
    /tmp/tmux-<uid>/<socket>`` for ``list-panes -a``, ``display-message`` and
    ``list-sessions`` alike (measured on 3.7c) and each wrapper swallows the
    non-zero exit. So "no pane answered" is not evidence of dead panes until the
    server itself has said something.

    Delegates to :meth:`~aisquare.core.tmux.TmuxServer.answers`, which asks the
    one question that separates every state exactly. This used to stand in with
    ``list_sessions()``, whose empty list means either "no server" or "a server
    holding nothing" — safe on the fleet's own server (``exit-empty`` is on, so
    the second state barely exists) but not a distinction worth guessing.
    """
    return srv.answers()


def _outlived(agent: FleetAgent, server_started: datetime | None) -> bool:
    """Whether ``agent``'s row was written before its server started — so no pane there is
    its. ``agent`` is the row as its launch record places it (:attr:`Placed.agent`), its
    times the record's (:func:`_started_after`).

    A pane id names a pane of one server's lifetime only: the next server on
    the socket numbers from ``%0`` again. A reboot or a hand-run ``tmux -L asq
    kill-server`` leaves live rows behind, and the first spawn in ANY project
    starts a fresh server on the shared socket, whose panes take those rows'
    ids: another project's manager, or a newer agent of the row's own project.
    Asked about by id, that pane answered for the stale row, and ``stop``,
    ``shutdown``, ``restart`` and ``switch`` typed ``/exit`` into it and killed
    it (review of #203, final round, FLEET-1).

    The row is written after its window exists (``_record``), so the server
    that holds its pane was already running at ``created_at``; a server that
    started later holds none of its panes. tmux reports the start rounded down
    to the second, which can only err toward the row. Neither name tmux keeps
    could say this: both projects' managers are called ``manager``, a
    relabelled agent's window keeps its first name (:func:`_relabel`), and a
    codename ``rename`` that tmux never heard of leaves the row's own panes
    under another session's name. ``None`` — tmux did not say when it started —
    judges nothing, and the id alone decides as it did before.

    A row from 0.8.1 on records when its server started (``FleetAgent.server_started_at``),
    and that decides exactly: a server that started at any other time is another, whatever
    the row's ``created_at`` says of it. The next server at a socket path a running server
    lost started AFTER the row too, but so did one that replaced a server that exited, and
    only the recorded start tells the two apart at every later read (follow-up of #257).
    """
    return _started_after(agent.created_at, agent.server_started_at, server_started)


def _started_after(
    created_at: datetime, recorded: datetime | None, server_started: datetime | None
) -> bool:
    """:func:`_outlived` of a launch record's own times: a window written at ``created_at``
    on a server that started at ``recorded`` (``None`` in a record from before it was kept),
    against a server that says it started at ``server_started``. What :class:`PlacedPane`,
    :class:`PlacedSession` and the project's window index judge their ids by."""
    if server_started is None:
        return False
    if recorded is not None:
        return server_started != recorded
    return server_started > created_at


def _on_another_server(
    agent: FleetAgent,
    server_started: datetime | None,
    reads: tmux_core.SocketReads | None = None,
) -> bool:
    """Whether the server that answered at ``agent``'s socket, started at ``server_started``,
    is not the one its row was started on, and that one may still run: it recorded its
    server's start, which is another, and it is from this boot. Such a row is held as
    unasked, not read as lost: its server lost its socket file to this one and runs on
    where no client reaches it, as far as anything here can tell (follow-up of #257). A
    row from another boot cannot be running, and its pane is simply gone.

    ``agent`` is the row as its launch record places it (:attr:`Placed.agent`): the start,
    the boot and the time are the record's, never the store's word for them.

    With no boot id to compare (the BSDs, a macOS whose ``sysctl`` failed), the row is
    from another boot when it was created before this one started, as
    :func:`~aisquare.core.tmux.server_may_run` judges it: counted from this boot instead,
    every row was held after a reboot against the server the next spawn started, and
    ``reap``, ``shutdown`` and ``uninstall`` could not end it (review of #259, round 1).

    The boot is asked only here, once a row has passed the test that makes it matter, and
    once per read of the fleet (``reads``, :class:`~aisquare.core.tmux.SocketReads`, which
    :func:`~aisquare.core.tmux.server_may_run` asks it of too). :func:`_observe` asked it of
    every read with live rows, asq's 2 s tick, needs-you's 3 s scan and every ``status_of``,
    though only a row that may be held needs it, which is rare; and ``boot_started`` once
    more per such row (review of #259, round 2). A cache of its own for it was a second
    one of one machine fact (review of #259, round 6, 9/10).
    """
    if agent.server_started_at is None or not _outlived(agent, server_started):
        return False
    reads = reads or tmux_core.SocketReads()
    this_boot = reads.boot_id()
    if agent.boot_id is not None and this_boot is not None:
        return agent.boot_id == this_boot
    booted_at = reads.boot_started()
    return booted_at is None or agent.created_at >= booted_at


def _observe(
    placements: Sequence[Placed],
    also: Collection[str] = (),
    reads: tmux_core.SocketReads | None = None,
) -> _Observed | None:
    """Row id → the facts of that row's pane, or ``None`` when tmux cannot be asked at all.
    ``placements`` are rows on ONE server, where their launch records place them.

    One ``list-panes`` per session their records name (by tmux's id) answers for every
    window in it; a pane missing there is asked about individually, as is every pane of a
    record that named no session. The ``activity`` flag those answers carry is not used
    (see :data:`ACTIVITY_WINDOW`); the last-output time comes from one more
    ``list-panes`` over the server.

    "Cannot be asked" is not only a :class:`TmuxError`: an unreachable server
    answers every one of those queries with an empty answer and no exception, so
    an empty result set is never taken as "every pane is gone" until
    :func:`_server_answers` has heard the server speak. That probe runs only in
    the one case it can change — no pane answered at all.

    Keyed by the ROW, not by the pane id, because an id alone does not name the
    row's pane: a row that outlived its server holds an id the next server gives
    to another agent (:func:`_outlived`). Such a row is given no view, so it
    reads ``lost`` and not as the other agent's state, and nothing that acts on
    a view — a listing's reconcile, ``reap``, a restart — takes that agent's
    pane for the row's. The server is asked when it started once a pane has
    answered, or once it has answered for live rows it holds no pane of: the
    answer says whether the rows' server is this one (:func:`_outlived`). Read
    where tmux already said it, in the window list or a pane's facts
    (``#{start_time}``); asked in a ``display-message`` of its own only when no
    answer carried it (review of #259, round 4).

    A live row the answering server is NOT the server of — one that recorded its
    server's start, and this one started at another time, in the same boot — is
    HELD, as :data:`HELD` under its id: its server lost its socket file to this one and
    may run on where no client reaches it, so nothing reads it as lost or ends it
    (:func:`_on_another_server`). Readers take it through :func:`_own_view`, which
    answers ``None`` for it, as for a server that could not be asked.
    """
    if not placements:
        return {}
    srv = placements[0].pane.server()
    try:
        windows = {
            window.pane_id: window
            for session in _sessions_of(placements)
            for window in session.panes()
        }
        live = [placement for placement in placements if placement.row.ended_at is None]
        output_at = _activity_times(srv) if live else {}
        said = [w.server_started for w in windows.values() if w.server_started is not None]
        seen: dict[str, _PaneView] = {}
        for placement in placements:
            pane_id = placement.pane.pane_id
            window = windows.get(pane_id)
            if window is not None:
                seen[placement.row.id] = _PaneView(
                    window.dead,
                    window.dead_status,
                    window.current_command,
                    output_at.get(pane_id),
                )
                continue
            if placement.row.ended_at is not None and placement.row.id not in also:
                # An ENDED row is looked for in the window list only (#138): its
                # pane still being there — `remain-on-exit` keeps a dead agent's
                # last screen — is what keeps it on the sidebar as 💤 exited;
                # a pane that is gone costs no second tmux question.
                continue
            facts = placement.pane.facts()
            if facts is not None:
                seen[placement.row.id] = _PaneView(
                    facts.dead,
                    facts.dead_status,
                    facts.current_command,
                    output_at.get(pane_id),
                )
                if facts.server_started is not None:
                    said.append(facts.server_started)
        if live and not seen and not _server_answers(srv):
            return None
        started = None
        if seen or live:  # one server, one start: any answer that carried it says it
            started = said[0] if said else srv.started_at()
        reads = reads or tmux_core.SocketReads()  # the boot: only for a row that may be held
        views: _Observed = {}
        for placement in placements:
            row_id = placement.row.id
            if row_id in seen and not placement.pane.outlived(started):
                views[row_id] = seen[row_id]
            elif placement.row.ended_at is None and _on_another_server(
                placement.agent, started, reads
            ):
                views[row_id] = HELD
        return views
    except TmuxError:
        return None


def _observe_sockets(
    placements: Sequence[Placement],
    also: Collection[str] = (),
    reads: tmux_core.SocketReads | None = None,
) -> dict[_Address, _Observed | None]:
    """Server → what THAT server says about the rows its launch records place on it; a row
    no record places is asked about nowhere (:func:`resolve_place`).

    Rows can span sockets — ``[fleet] tmux_socket`` may have changed since they
    were started — and each must be asked of the server it lives on, where its
    spawn said it listened (:func:`_address`): ``tmux -L`` by the name alone,
    from a shell under another ``TMUX_TMPDIR``, asks another server, whose dead
    ``%N`` ended a live row (round 28 of #257). A ``None`` value is one server
    that could not be asked at all, which is per server and not per fleet: an
    unreachable server must not make the agents on a reachable one read as
    gone, nor the reverse. Looked up with :func:`_view`. ``also`` are ended rows whose
    windows run (:func:`_running_ids`), asked about as a live row is. ``reads`` is the
    caller's for the whole read, so the boot is asked once for it (:func:`_on_another_server`).
    """
    reads = reads or tmux_core.SocketReads()
    groups: dict[_Address, list[Placed]] = {}
    for placement in _placed_only(placements):
        groups.setdefault(placement.pane.address, []).append(placement)
    return {address: _observe(group, also, reads) for address, group in groups.items()}


def _view(placement: Placement, views: Mapping[_Address, _Observed | None]) -> _Observed | None:
    """What ``placement``'s own server said (:func:`_observe_sockets`); ``None`` when it
    could not be asked, or was not, as for a row no record places, or when the server that
    answered is not its own (:func:`_own_view`)."""
    if not isinstance(placement, Placed):
        return None
    return _own_view(placement.agent, views.get(placement.pane.address))


def _own_view(agent: FleetAgent, observed: _Observed | None) -> _Observed | None:
    """``observed`` as ``agent``'s own server's word, or ``None`` when it is not: the server
    could not be asked, or it is another server than the row's, which :func:`_observe`
    holds under the row's id as :data:`HELD`. The one way a reader takes a server's answer
    for a row, so none of them reads a held row as a pane that is gone."""
    if observed is None or _held_by_another(agent, observed):
        return None
    return observed


def _held_by_another(agent: FleetAgent, observed: _Observed | None) -> bool:
    """Whether ``observed`` holds ``agent`` as a row of another server's (:func:`_observe`)."""
    return observed is not None and observed.get(agent.id) is HELD


def _pane_in(agent: FleetAgent, observed: _Observed) -> _PaneView | None:
    """``agent``'s pane in its own server's word (:func:`_own_view`), or ``None`` when that
    server holds none: the pane is gone. A held row's entry is no pane, and it is not gone:
    a caller that did not screen it out raises here, rather than end it under its agent."""
    pane = observed.get(agent.id)
    if isinstance(pane, _Held):
        raise FleetError(f"{agent.label}'s row is held as another server's: not its own view")
    return pane


def _exit_detail(status: int | None) -> str | None:
    return f"exit {status}" if status is not None else None


def exit_code_of(status: FleetAgentStatus) -> int | None:
    """The exit code of a row that ended: the one recorded, else, for a dead pane not
    recorded yet, the one its pane holds (``status.detail``, :func:`_exit_detail`'s words).
    Read from the row alone, Restart all's list showed none for the second (#274 delta
    review 2, n6)."""
    if status.agent.exit_status is not None:
        return status.agent.exit_status
    if status.state != "exited" or status.detail is None:
        return None
    said = status.detail.removeprefix("exit ")
    return int(said) if said != status.detail and said.lstrip("-").isdigit() else None


def _derive(
    agent: FleetAgent,
    session: TeamSession | None,
    pane: _PaneView | None,
    *,
    observed: bool,
    now: datetime,
    screen: Callable[[], Sequence[str] | None] | None = None,
    held: bool = False,
) -> tuple[FleetAgentState, str | None]:
    """State from the board first, tmux second (§5.1) — with two honest exceptions.

    A dead or vanished pane is a hard fact no board row can contradict: a
    process that has exited cannot be waiting, however recently its hooks
    fired — and a killed agent fires no ``SessionEnd``, so its row would say
    ``waiting`` for ``_STALE_AFTER`` otherwise. So death and absence are read
    first; then a fresh ``team_session`` row (not ended, seen within
    ``_STALE_AFTER``) decides between working, waiting and attention, and the
    attention of an agent that redraws while it asks (Codex) holds, fresh or not, until
    ``screen`` (the pane's visible rows, read only then) shows it working again; then
    recent output (:data:`ACTIVITY_WINDOW`) means working; and when tmux cannot
    be asked at all the answer is ``unknown``, never a guess.

    So is another server at the row's socket (``held``, :func:`_observe`): it reads
    ``unknown`` whatever its board says, saying why (:data:`ANOTHER_SERVER`). Its agent is
    one no tmux client reaches, if it runs at all, and its pane id there is another
    agent's. Derived from a fresh board, it read a healthy ``waiting`` with no reason for
    half an hour, under which ``fleet tell`` typed into that other agent, needs-you
    pushed prompts every answer to was refused, and a held manager counted as one that
    takes its coders' questions (review of #259, round 2).
    """
    if agent.ended_at is not None:
        return "exited", _exit_detail(agent.exit_status)
    if observed:
        if pane is None:
            return "lost", "pane gone"
        if pane.dead:
            return "exited", _exit_detail(pane.dead_status)
    hooks = "no hooks" if agent.session_id is None else None
    if held:
        return "unknown", ANOTHER_SERVER if hooks is None else f"{ANOTHER_SERVER}; {hooks}"
    if session is not None and session.ended_at is None:
        fresh = now - session.last_seen_at <= _team()._STALE_AFTER
        board_state = _BOARD_STATES.get(session.state)
        if board_state == "limited":
            # A parked agent fires no hook for as long as its limit lasts, which
            # can be hours; the row is trusted until the reset it named has
            # passed (plus a little), not for the ordinary stale window — after
            # that, tmux's facts take over as for any quiet session (#146).
            until = session.limit_resets_at
            parked = until is not None and now <= until + _LIMIT_GRACE
            if fresh or parked:
                return "limited", _limit_detail(until, now)
        elif board_state == "attention" and _redraws_while_asking(agent):
            # Codex redraws its approval view every second, the screen unchanged, for
            # as long as it waits (0.162.1), so its pane's output answers nothing and
            # a wait past the stale window fires no hook either: the bell holds until
            # a hook moves the row — Interrupt on Esc, Stop, the next prompt — or the
            # screen shows it working: the view gives way to its working line the
            # moment the human allows, and the first hook after that is the command's
            # PostToolUse, when it finishes (review r2 of #113, finding 7).
            if _working_on_screen(agent, screen):
                return "working", None
            return "attention", None
        elif fresh and board_state == "attention":
            # The bell clears when the pane moves on, not only on the next human
            # prompt (#153): a permission that was granted — or an action the
            # classifier approved — produces output after the notification, and
            # an agent that is working again is not waiting for anyone. Output
            # BEFORE the notice is the prompt being drawn and does not count —
            # and since tmux reports activity in whole seconds while the hook
            # stamped `last_seen_at` with microseconds, "after" means a LATER
            # second, which keeps the drawing of the prompt (the same second as
            # the hook) from reading as its answer. And the answer holds only
            # while the pane is STILL producing output (ACTIVITY_WINDOW): one
            # burst after the notice is not a working agent. A prompt dismissed
            # with Esc fires no Stop, and the idle notice after it is quiet; the
            # redraw when the UI resizes the window on attach, or arrow keys
            # moving through the dialog, print too. Without the bound each held
            # an unanswered prompt at `working` for the row's whole
            # `_STALE_AFTER`; a granted permission keeps the pane moving until
            # its turn's Stop.
            answered = (
                pane is not None
                and pane.last_output is not None
                and pane.last_output > session.last_seen_at
                and now - pane.last_output <= ACTIVITY_WINDOW
            )
            if answered:
                return "working", None
            return "attention", None
        elif fresh and board_state is not None:
            return board_state, None
    if not observed or pane is None:
        # tmux is WHY the state is unknown, and ``detail`` is "why the state is
        # what it is" (models.py). A missing board join is a second, smaller
        # fact: reported after the reason, never instead of it — an operator
        # told only "no hooks" reads a healthy fleet with an odd binary.
        unasked = "tmux unavailable"
        return "unknown", unasked if hooks is None else f"{unasked}; {hooks}"
    busy = pane.last_output is not None and now - pane.last_output <= ACTIVITY_WINDOW
    return ("working" if busy else "waiting"), hooks


def _redraws_while_asking(agent: FleetAgent) -> bool:
    """Whether ``agent``'s agent keeps drawing while it waits on the human
    (``AgentCapabilities.redraws_while_asking``). ``agent`` is the placed row, whose agent
    is its launch record's (:func:`resolve_place`): the board session's, a store column,
    always won over it, and a Codex row whose session read NULL there read its approval wait
    as working, its bell gone (review r7 of #113, finding 7)."""
    adapter = recorded_adapter(agent.agent)
    return adapter is not None and adapter.capabilities.redraws_while_asking


def _working_on_screen(
    agent: FleetAgent, screen: Callable[[], Sequence[str] | None] | None
) -> bool:
    """Whether ``screen`` shows ``agent``'s agent (its launch record's, as for
    :func:`_redraws_while_asking`) running a turn unblocked
    (``AgentCapabilities.working_on_screen``); ``False`` when it cannot be read."""
    adapter = recorded_adapter(agent.agent)
    working = screen_of(adapter).working_on_screen if adapter is not None else None
    if working is None or screen is None:
        return False
    rows = screen()
    return rows is not None and working(rows)


def _screen_of(pane: PlacedPane) -> list[str] | None:
    """The rows on ``pane``'s screen now; ``None`` when its server does not answer."""
    try:
        return pane.screen()
    except TmuxError:
        return None


_LIMIT_GRACE = timedelta(minutes=10)
"""How long past its named reset a ``limited`` row is still believed (the reset
is a clock time from an error message; Claude Code's own continue lands shortly
after it, and a session that continues turns ``working`` on its next prompt)."""


def _limit_detail(until: datetime | None, now: datetime) -> str:
    """``limit resets in 2h 10m (12:30)`` / ``limit resets in 2d (Mon 00:00)`` / ``usage limit``.

    Through ``core.claude_accounts.format_reset``, the one formatter every
    surface that shows a reset goes through (#152; review of #205, finding 9).
    """
    if until is None:
        return "usage limit"
    return f"limit resets {claude_accounts_core.format_reset(until, now=now)}"


ANOTHER_SERVER = "another tmux server on its socket"
"""A held row's detail (:func:`_observe`), whose state is ``unknown`` whatever its board
says (:func:`_derive`): a server answers at its socket, and it is not the one the row was
started on, which may run on where no client reaches it."""

TRUST_PROMPT = "waiting for folder trust"
"""How an agent at its folder-trust question reads (:func:`_with_trust_prompts`): Claude
Code's, and Codex's as its adapter reads it off its screen. ``attention``, its detail starting
with this. ``remote_needs`` keys Claude Code's card on it."""

AT_A_DIALOG = "at a dialog of its own"
"""How an agent reads at any other dialog it opens before its first hook, which its adapter
reads off its screen (``AgentScreen.dialog_on_screen``: Codex's hook review, its ``Working
directory · fork``): ``attention``, its detail this and the dialog's words
(:func:`_with_trust_prompts`)."""


def trust_prompt_detail(folder: Path) -> str:
    """The detail of an agent at the folder-trust question in ``folder``: what unblocks it."""
    return f"{TRUST_PROMPT} — approve it once, or `{claude_accounts_service.trust_command(folder)}`"


def _with_trust_prompts(
    statuses: list[FleetAgentStatus],
    placements: Sequence[Placement],
    by_id: Mapping[str, TeamSession | None],
) -> list[FleetAgentStatus]:
    """``statuses``, every agent at Claude Code's folder-trust question read ``attention``, and
    every one at its first-run Security notes screen (#32, L2).

    An agent there fires no hook — Claude Code runs none in a folder nobody trusted — so
    it read a healthy ``waiting`` (or ``working``, while the dialog drew) with no reason,
    for as long as nobody looked: the board, the sidebar and the Remote never said it
    needed the human, and ``fleet tell`` typed into it, where Enter picks "No, exit".
    Asked only of a live row that tmux reports and whose session has fired no hook since
    the row began (:func:`_maybe_at_trust_prompt`): one capture each, which an agent
    whose hooks run never costs. A screen that cannot be read is no question.

    A row the listing has no session for is asked with the one ``by_id`` holds for its
    session id, read by the caller in its own store session: :func:`list_agents` joins the
    sessions of the project's own board, and under ``AISQUARE_TEAM_HUB`` every session is
    the hub's. Every row there read as silent, so every listing captured every pane, and a
    pane that merely showed the question's words (a reviewer reading ``claude_sharing``)
    read as at it (review of #259, round 13). Looked up here, they cost a second store
    session, with the store's migration checks, on every listing that had such a row —
    under a hub, every sidebar tick, Remote scan and ``status_of`` (round 16).

    The screen read is the pane each row's launch record places (``placements``,
    :func:`resolve_place`); a row no record places is not asked.
    """
    panes = {placement.row.id: placement.pane for placement in _placed_only(placements)}
    shown: list[FleetAgentStatus] = []
    for status in statuses:
        session = status.session or by_id.get(status.agent.session_id or "")
        pane = panes.get(status.agent.id)
        if pane is not None and _maybe_at_trust_prompt(status, session):
            detail = _start_dialog(status.agent, pane)
            if detail is not None:
                status = status.model_copy(update={"state": "attention", "detail": detail})
        shown.append(status)
    return shown


def _start_dialog(agent: FleetAgent, pane: PlacedPane) -> str | None:
    """The detail of ``agent`` at a dialog on ``pane`` before its first hook
    (:func:`_with_trust_prompts`); ``None`` with none up, or a screen tmux would not show.

    Claude Code's folder-trust question, by its words (``claude_sharing.shows_trust_prompt``),
    and its first-run Security notes (#32, L2). An agent whose adapter reads its own dialogs
    off its screen (``capabilities.dialog_on_screen``) is read by it: Codex's folder trust
    (``screen_prompt``'s ``trust``) reads as Claude Code's does, and any other dialog it
    opens with no hook as :data:`AT_A_DIALOG` with its words. Before, a Codex at its folder
    trust, its hook review or the ``Working directory · fork`` a bare ``codex fork`` opened
    read a healthy ``waiting … no hooks`` (SMOKE8 of #113)."""
    adapter = recorded_adapter(agent.agent) if agent.agent is not None else None
    read = screen_of(adapter).dialog_on_screen if adapter is not None else None
    try:
        lines = pane.screen() if read is not None else pane.captured()
    except TmuxError:
        return None
    if adapter is None or read is None:
        if claude_sharing.shows_trust_prompt(lines):
            return trust_prompt_detail(agent.cwd)
        if claude_sharing.shows_security_notes(lines):
            # Its only key is Enter, which a spawn presses (#32, L2): still there, it is one
            # nobody typed into — a window the human opened first, or a refused key.
            return f"{SECURITY_NOTES} — press Enter in its window"
        return None
    words = read(lines)
    if words is None:
        return None
    prompt = screen_of(adapter).screen_prompt
    if prompt is not None and (asked := prompt(lines)) is not None and asked.shape == "trust":
        return trust_prompt_detail(agent.cwd)
    return f"{AT_A_DIALOG}: {words}"


def _maybe_at_trust_prompt(status: FleetAgentStatus, session: TeamSession | None = None) -> bool:
    """A live Claude Code row tmux reports, whose session has said nothing since the row
    began: ``session``, the row's found by its id, else the listing's. Or such a row of an
    agent whose adapter reads its own dialogs off its screen (``capabilities.
    dialog_on_screen``: Codex's), one with no session yet included, as none of those dialogs
    fires a hook (:func:`_start_dialog`).

    Claude Code alone asks the question, so a row is asked about only when it is Claude
    Code: one with a session (only Claude Code's hooks write one), one started on a
    session id (only Claude Code is: minted for it by name, or named by its caller or a
    resume, as the ``claude2`` parallel install is — review of #259, round 15), or by its
    binary's name, when that names an agent with such a dialog (``adapter_for_binary``, by
    which its flags and ``--session-id`` go too). Another harness's agent fires no hook,
    ever, so its pane was
    captured on every listing — the sidebar's tick, Remote's scan, each action's
    ``status_of`` — and read as at the question when its screen held the words (round 14).
    """
    agent, session = status.agent, session or status.session
    if agent.ended_at is not None or status.held or status.state not in ("waiting", "working"):
        return False
    adapter = recorded_adapter(agent.agent) if agent.agent is not None else None
    reads = adapter is not None and screen_of(adapter).dialog_on_screen is not None
    if adapter is not None and screen_of(adapter).start_dialog_mark is None and not reads:
        # This is Claude Code's question (`claude_sharing.shows_trust_prompt`), and an agent
        # with no mark under such a dialog, and none it reads off its screen, is not captured
        # on every listing for it.
        return False
    if session is None:
        named = adapter_for_binary(agent.binary)
        asks = named is not None and screen_of(named).start_dialog_mark is not None
        return reads or agent.session_id is not None or asks
    return session.last_seen_at < agent.created_at


def _status(
    placement: Placement,
    session: TeamSession | None,
    views: Mapping[_Address, _Observed | None],
    now: datetime,
    running: Collection[str] = (),
) -> FleetAgentStatus:
    """``placement``'s derived state, as a listing shows it. A row no launch record places
    reads ``lost``, saying why, with no pane to show: its pane is the store's word, which
    asq's pane view would have shown and typed the human's keys into (review r5 of #113,
    finding 1)."""
    if isinstance(placement, Unplaced):
        if placement.row.ended_at is not None:
            # Its pane is never asked about: an ended row reads exited, as it always did
            # (review r8 of #113, finding 8).
            state, detail = _derive(placement.row, session, None, observed=False, now=now)
            return FleetAgentStatus(
                agent=placement.row, state=state, detail=detail, session=session
            )
        return FleetAgentStatus(
            agent=placement.row,
            state="lost",
            detail=f"not placed: {placement.why}",
            session=session,
        )
    if _ended_but_running(placement, views, running):
        placement = _as_running(placement)
    # The server's word AS GIVEN (`_observe_sockets`' view for the row's address), never
    # through `_view`: a row the answering server is not the server of is held there under
    # its id, which is the one fact that decides its state and detail below.
    given = views.get(placement.pane.address)
    held = _held_by_another(placement.agent, given)
    observed = _own_view(placement.agent, given)
    pane = _pane_in(placement.agent, observed) if observed is not None else None
    state, detail = _derive(
        placement.agent,
        session,
        pane,
        observed=observed is not None,
        now=now,
        screen=(lambda: _screen_of(placement.pane)) if pane is not None else None,
        held=held,
    )
    return FleetAgentStatus(
        agent=placement.agent,
        state=state,
        detail=detail,
        session=session,
        tmux_session=(
            placement.session.name
            if placement.session is not None
            else placement.record.window.session
            if placement.record.window is not None
            else None
        ),
        socket_at=placement.pane.address.path,
        held=held,
    )


def _require_tmux(srv: TmuxServer) -> None:
    try:
        srv.require()
    except TmuxUnavailable as exc:
        raise FleetUnavailable(str(exc)) from exc


def role_ok(role: str) -> bool:
    """The seat rule: whether ``spawn`` accepts ``role``.

    A role is accepted when it is a fleet or harness role, a numbered seat of one
    (``coder2``), or a role declared in ``team.profiles`` (``aisquare team bind``).
    ``spawn`` refuses anything else, ``aisquare launch`` applies the same rule, and
    the UI's New bind form asks it before saving a seat, so all three agree.
    """
    return _role_ok(role)


def _role_ok(role: str) -> bool:
    """A fleet role, a harness role, or anything ``aisquare launch`` would accept."""
    if role in FLEET_ROLES or role in harness.ROLE_PROFILES:
        return True
    from aisquare.cli import launch  # lazy: the CLI package imports this service

    return launch._role_ok(role)


def _require_role(role: str) -> None:
    """Refuse a role :func:`_role_ok` does not know — ``spawn``'s first refusal, and
    ``restart``'s before it stops anything."""
    if not _role_ok(role):
        raise FleetError(
            f"unknown role {role!r} — expected one of: {', '.join(FLEET_ROLES)}, a harness "
            f"role, one bound with `aisquare team bind`, or {PLAIN_ROLE!r} for no role"
        )


def _board_for(project: ProjectInfo) -> str:
    """The board an agent this process spawns into ``project`` reads: the one its window's
    ``launch`` and hooks resolve in the project's root (:func:`orchestrator.team_project`),
    under the hub the window is given (:func:`orchestrator.window_team_hub`). That is the
    hub's when this process honours one, else that of the checkout the root belongs to, so
    a project rooted in a sub-folder of its repository, or at a linked worktree, reads its
    principal checkout's board (review of #259, round 25, 3/10).

    Asked once, at spawn, and recorded on the row (``FleetAgent.board``); every later
    question of the row's board asks the row (:func:`_board_of_row`). A root whose checkout
    cannot be worked out is a ``FleetError`` (:func:`_unaskable_root`).
    """
    try:
        return orchestrator.team_project(project.root).id
    except OSError as exc:
        raise _unaskable_root(project, exc) from exc


def _spawn_board_and_hub(
    project: ProjectInfo, role: str, hub: str | None = None
) -> tuple[str, str]:
    """The board a NEW agent of ``role`` in ``project`` reads, and the ``AISQUARE_TEAM_HUB`` its
    window is given so that its ``launch`` and hooks resolve that board. A replacement's are
    its row's instead (:func:`_board_and_hub`).

    The board this process gives it (:func:`_board_for`), under the hub this process honours
    (:func:`orchestrator.window_team_hub`), unless the caller names the ``hub``: ``""`` for
    none, where a window with no hub reads, the checkout's (:func:`_checkout_board`).

    The captain names none for the seats it spawns (``services.captain.actions``). Its own
    processes carry the home's root as their hub, which is how the captain itself lands on
    the home board; honoured as a hub for its seats, every seat it spawned for a project
    read the home board, where neither its manager nor its teammates read. And the
    captain's own window is given the home's root, whatever hub this process honours: the
    home is no checkout, and ``.aisquare`` is itself a project-root marker, so from the
    home's root a window with no hub read ``$HOME``'s board, and under a shell's hub, the
    hub's (T2).
    """
    if experimental.captain_role(role):
        hub = str(project.root)
    if hub is None:
        return _board_for(project), orchestrator.window_team_hub()
    if not hub:
        return _checkout_board(project), ""
    return orchestrator.hub_project(hub).id, hub


def _checkout_board(project: ProjectInfo) -> str:
    """The board of the checkout ``project``'s root belongs to
    (:func:`orchestrator.directory_project`), where a window with no hub reads; a root whose
    checkout cannot be worked out is a ``FleetError`` (:func:`_unaskable_root`)."""
    try:
        return orchestrator.directory_project(project.root).id
    except OSError as exc:
        raise _unaskable_root(project, exc) from exc


def _unaskable_root(project: ProjectInfo, exc: OSError) -> FleetError:
    """The refusal of a command that has to work out ``project``'s board from its root and
    cannot: a root that became a symlink loop raises ``OSError`` on every Python (round 40,
    3/6), and so does one under a folder this user may not search (round 42, 5/11). The
    ``fleet`` commands catch ``FleetError`` alone, and ``fleet pause``, ``restart`` and
    ``switch`` ended in a traceback, ``--json`` printing nothing (review of #259, round 43,
    4/7)."""
    return FleetError(
        f"the board of {_name(project)} cannot be worked out: its root {project.root} cannot "
        f"be looked at ({exc.strerror or exc})"
    )


def _board_of_row(agent: FleetAgent) -> str:
    """The board ``agent``'s sessions read, as its spawn recorded it (``FleetAgent.board``).

    docs/fleet.md, "Under a hub: one board, many fleet projects": a task, the fleet's own
    feed lines (``agent_exited``, ``switched``, ``restarted``), the entries a hand-off prompt
    quotes, the ``fleet-paused`` signal and the exits Remote reads are the board's. Rounds 21
    to 24 of the review of #259 worked that board out again at each call site, from the
    process's ``AISQUARE_TEAM_HUB``, the bound session, the cwd or Remote's own
    environment, and where two disagreed, a switch was refused its own task, an exit went to
    a board its manager does not read, and a pause to one it does not ask (round 25, 10/10).
    The row says, whoever asks and from wherever.
    """
    return agent.board or agent.project_id


def _board_and_hub(
    store: ContextStore, project: ProjectInfo, agent: FleetAgent, session: TeamSession | None
) -> tuple[str, str]:
    """The board a replacement of ``agent`` in ``project`` reads, and the
    ``AISQUARE_TEAM_HUB`` its window is given so that its ``launch`` and hooks resolve that
    board. ``session`` is the agent's, as its row binds it.

    The board the row recorded (:func:`_board_of_row`), and the hub its window was given,
    kept in the launch it replays (``LaunchSpec.hub``). A ``restart`` or ``switch`` from
    ``asq``, the phone or a shell that does not export the hub the agent was spawned under
    asked for the task on the caller's board and refused it, and the replacement's window
    took the caller's hub, moving the agent off the board its manager reads (review of #259,
    round 25, 1/10). The hub is read, never inferred: worked out from the board, as "a board
    that is not the checkout's is a hub", a row that read its own project's board took that
    project's root for a hub, and the replacement, and every one after it, read a private
    board its manager does not (round 26, 8/11).

    A spec recorded before it kept the hub (every row 0.8.0 spawned) says nothing, and the
    board is read with the hub that gives it: none for the checkout's, a known project's
    root for any other. The project's own board, where the checkout is another's, is the
    one case the row's board may not be evidence: 0.8.0 recorded no board, and a row whose
    session never registered reads as its project's (``store._UNRECORDED_BOARD``). It is
    kept when the row's session registered on it, or the task the row names is on it: 0.8.0
    gave a project rooted at ``repo/api`` a board of its own only under the hub
    ``repo/api``, and spawned for a task only on the board it gave. Read from the checkout
    there, a restart of such an agent with a task stopped it and then refused its
    replacement the task, and without one moved it off its manager's board (round 27, 2/7).
    Else it is read as a window with no hub reads it, from the checkout. A board whose root
    the store no longer has reads the board this process gives (:func:`_board_for`). One
    forgotten since is still held, its tasks and events kept, and still read by the agents
    whose rows recorded it, as Remote reads it (``remote_server._remote_recorded_board``):
    taken for one the store does not hold, a hub forgotten to tidy the project list was
    replaced by the caller's board and hub, a restart with a task on the hub refused it, and
    one without moved the agent off its manager's board (review of #259, round 31, 3/7).

    Asked before the agent is stopped, and the task checked against it then
    (:func:`restart`, :func:`switch`): the replacement's spawn reads the same answer.
    """
    spec = agent.launch_spec
    if spec is not None and spec.hub is not None:
        return _board_of_row(agent), spec.hub
    checkout = _checkout_board(project)
    board = _replacement_board(store, agent, lambda: session, lambda: checkout)
    if board == checkout:
        return checkout, ""
    known = store.get_project(board, include_forgotten=True)
    if known is not None:
        return board, str(known.root)
    return _board_for(project), orchestrator.window_team_hub()


def _replacement_board(
    store: ContextStore,
    agent: FleetAgent,
    session: Callable[[], TeamSession | None],
    checkout: Callable[[], str],
) -> str:
    """The board a replacement of ``agent`` reads, as the store says it: the one its row
    recorded (:func:`_board_of_row`), but for a row whose launch kept no hub, whose board is
    its own project's and that does not show it read it (:func:`_read_its_own_board`), whose
    replacement reads its project's ``checkout``'s. ``session`` (the row's) and ``checkout``
    are asked for such a row alone, the store first: the checkout is a ``git`` call, and a
    row that read its own board keeps it whatever the checkout is. Asked after it, every
    0.8.0 row, whose session registered on its own project's board, cost a ``git`` call a
    project on each ``project forget``, prune and ``asq`` start (round 39, 1/9).

    The one place the rule is written: :func:`_board_and_hub` reads it for a restart, and
    adds the hub, and :func:`restart_board` for the holds ``project forget`` and prune judge.
    Written out in each, the next change to one copy would leave the other judging holds by
    the old rule, and forget a hub a restart adds back (review of #259, round 39, 3/9)."""
    board = _board_of_row(agent)
    spec = agent.launch_spec
    if board != agent.project_id or (spec is not None and spec.hub is not None):
        return board
    if _read_its_own_board(store, agent, session()):
        return board
    return checkout()


def _read_its_own_board(
    store: ContextStore, agent: FleetAgent, session: TeamSession | None
) -> bool:
    """Whether ``agent``, a row whose board is its own project's and whose launch kept no hub,
    is shown to have read that board: its session registered on it, or the task it names is
    on it (:func:`_replacement_board`). A store without ``team_task`` shows no task."""
    board = agent.project_id
    if session is not None and session.project_id == board:
        return True
    return (
        agent.task_id is not None
        and store.has_table("team_task")
        and store.get_task(agent.task_id, project_ids=[board]) is not None
    )


def restart_board(store: ContextStore, agent: FleetAgent, checkouts: dict[str, str]) -> str:
    """The board a ``restart`` of ``agent`` starts its replacement on, as the store says it
    (:func:`_replacement_board`, the rule :func:`_board_and_hub` reads too). A board the
    store does not hold stays the row's: a restart reads the one its caller's process gives
    then (:func:`_board_for`), which is not the store's to say.

    ``checkouts`` keeps each project's checkout (a ``git`` call) as it is worked out, for a
    caller asking of every row (``project.reading_rows``). Filed under the row's own board, a
    row that read ``repo``'s held ``repo/api``'s instead, and ``project forget repo`` was not
    refused, though a restart of it brought ``repo`` back (review of #259, round 38, 3/4).
    Asked first once it is known: the rule's answer is the row's board or the checkout, so
    where the two are one, the row is filed with no lookup. Asked of the store first, every
    0.8.0 row ran a session read, and a task read, the checkout already settled, though
    before round 39 a project's rows after its first ran none (round 40, 4/6).

    A root whose checkout cannot be worked out, a symlink loop (an ``OSError`` on every
    Python, as ``find_project_root`` resolves it: round 40, 3/6) or a mount that answers
    ``EIO``, is no evidence either, and the row's board stands. Uncaught, one such project's
    root ended every other's ``project forget`` in a traceback, and doctor's capture-off line
    went without a word (round 39, 2/9). A root that is gone is its own checkout
    (``find_project_root``, review of #259, round 42, 3/11).

    Nor is a table the store lacks (doctor's damaged-store case, which every fleet read
    tolerates): without ``team_session`` the row shows no session, without ``team_task`` no
    task, and its board is read from the checkout. Asked anyway, a 0.8.0 row with a session
    raised "no such table" there, and the forget, the prune and doctor's line with it
    (round 40, 1/6)."""

    def checkout() -> str:
        board = agent.project_id
        if board not in checkouts:
            checkouts[board] = board
            project = store.get_project(board, include_forgotten=True)
            if project is not None:
                with suppress(OSError):
                    checkouts[board] = orchestrator.directory_project(project.root).id
        return checkouts[board]

    def session() -> TeamSession | None:
        if not agent.session_id or not store.has_table("team_session"):
            return None
        return store.get_session(agent.session_id)

    known = checkouts.get(agent.project_id)
    if known is not None and known == _board_of_row(agent):
        return known
    return _replacement_board(store, agent, session, checkout)


def _task_for(store: ContextStore, board_id: str, task_id: str | None) -> TeamTask | None:
    """The board task an agent is spawned for — on the board it will read
    (:func:`_board_and_hub`, :func:`_board_of_row`), or refused."""
    if task_id is None:
        return None
    try:
        task: TeamTask | None = _team().find_task(store, task_id, board_id)  # `12`, `#12`
    except AmbiguousIdError as exc:
        raise FleetError(f"task {task_id!r} is ambiguous — use more of the id") from exc
    except _team().NoSuchTaskNumber as exc:
        raise FleetError(str(exc)) from exc
    if task is None:
        raise FleetError(f"no task matches {task_id!r}")
    if task.project_id != board_id:
        raise FleetError(f"task {task_id!r} belongs to another project's board")
    if task.status in CLOSED_STATUSES:
        # Harmless when the id only named a label; now it reaches the agent as
        # its assignment, and an agent spawned for finished work would be told
        # so on arrival and hold a slot for nothing.
        raise FleetError(
            f"task {task.id} is {task.status} — spawn for a task that still needs work"
        )
    return task


def _by_label(
    store: ContextStore, project_id: str, label: str, *, live_only: bool
) -> tuple[FleetAgent, launch_seal.LaunchRecord | str] | None:
    """The row ``label`` names, with its launch record (read once, for the caller): the
    row whose record holds that label, as its spawn recorded it. The row's own ``label``
    is a store column, and two rows' labels swapped made ``fleet stop coder-1`` stop the
    manager (review r8 of #113, finding 5): it is taken at its word only where its record
    keeps no label (one written before it did) or there is none. A row the store calls
    ``label`` whose record holds another is passed over for the one whose record holds
    it, live rows first, then the latest to end."""
    row = store.fleet_agent_by_label(project_id, label, live_only=live_only)
    decoy: tuple[FleetAgent, launch_seal.LaunchRecord | str] | None = None
    if row is not None:
        record = _read_record(row.id)
        if isinstance(record, launch_seal.LaunchRecord) and record.label in (None, label):
            return row, record
        if not isinstance(record, launch_seal.LaunchRecord):
            # No record: taken at its word only when no record holds the label. A row the
            # sandbox inserted under the label of one it relabelled was stopped, its claims
            # released, while the agent ran on (review r9 of #113, finding 7).
            decoy = row, record
    rows = store.fleet_agents(project_id, live_only=live_only)
    ordered = sorted(
        rows,
        key=lambda candidate: (
            candidate.ended_at is not None,
            -(candidate.ended_at or candidate.created_at).timestamp(),
        ),
    )
    for candidate in ordered:
        if row is not None and candidate.id == row.id:
            continue
        record = _read_record(candidate.id)
        if isinstance(record, launch_seal.LaunchRecord) and record.label == label:
            if decoy is not None:
                _say_refused(
                    project_id,
                    decoy[0],
                    "passed over",
                    f"it has no launch record, and the label {label!r} is another agent's "
                    "in that agent's record",
                    f"`{label}` names that agent",
                )
            return candidate, record
    return decoy


def _live_row(
    store: ContextStore, project: ProjectInfo, label: str, *, agent_id: str | None = None
) -> tuple[FleetAgent, launch_seal.LaunchRecord | str]:
    """The live row ``label`` names (:func:`_by_label`) and its record; with ``agent_id``,
    only if it is THAT row (:func:`stop`)."""
    found = _by_label(store, project.id, label, live_only=True)
    if found is None:
        raise NoSuchAgent(
            f"no live agent {label!r} in {_name(project)} — "
            f"`{project_service.fleet_command('ls', project)}` shows who is running"
        )
    agent, record = found
    if agent_id is not None and agent.id != agent_id:
        raise _replaced(project, label, agent, agent_id)
    return agent, record


def _live_agent(
    store: ContextStore, project: ProjectInfo, label: str, *, agent_id: str | None = None
) -> FleetAgent:
    """The live row ``label`` names; with ``agent_id``, only if it is THAT row (:func:`stop`)."""
    return _live_row(store, project, label, agent_id=agent_id)[0]


def _replaced(project: ProjectInfo, label: str, current: FleetAgent, agent_id: str) -> NoSuchAgent:
    """The refusal for a caller pinned to a row whose label has since passed to another."""
    return NoSuchAgent(
        f"{label!r} is another agent now ({current.id}) — {agent_id} ended and was "
        "replaced since; nothing was done to either "
        f"(`{project_service.fleet_command('ls', project)}` shows who is running)"
    )


def _in_hand_over(project: ProjectInfo, verb: str, label: str) -> FleetError:
    """The refusal of a ``restart`` or a ``switch`` of a row a hand-over is already moving
    (:func:`_handed_over`) — the automatic path's own brake, for the commands. It names
    the grace, since a hand-over interrupted on its way holds the row until that lapses."""
    minutes = int(HANDOVER_GRACE.total_seconds() // 60)
    return FleetError(
        f"cannot {verb} {label!r}: a hand-over is already moving it and starts the "
        "replacement itself — nothing was done "
        f"(`{project_service.fleet_command('ls', project)}` shows it come back; "
        f"one that was interrupted lets go of it within {minutes} min)"
    )


# --- lifecycle ----------------------------------------------------------------------


def spawn(
    project: ProjectInfo,
    role: str,
    *,
    label: str | None = None,
    task_id: str | None = None,
    worktree: bool | None = None,
    permission_mode: str | None = None,
    sandbox: str | None = None,
    approval_policy: str | None = None,
    binary: str | None = None,
    agent: str | None = None,
    prompt: str | None = None,
    agent_args: Sequence[str] = (),
    spawned_by: str = "user",
    account: str | None = None,
    persona: str | None = None,
    resume: ResumeSpec | None = None,
    size: tuple[int, int] | None = None,
    spec: LaunchSpec | None = None,
    family: str | None = None,
    takes_over: str | None = None,
    cwd: Path | None = None,
    onboard: bool = True,
    bin_flag: bool = True,
    own_worktree: Path | None = None,
    own_branch: str | None = None,
    board_and_hub: tuple[str, str] | None = None,
    session: launch_seal.SessionFacts | None = None,
    start_point: str | None = None,
    fork: bool = False,
    hub: str | None = None,
    replaces: str | None = None,
) -> SpawnReceipt:
    """Start an agent for ``project`` in the fleet's tmux server and record it.

    ``spec`` REPLAYS a recorded launch (#144): the binary, permission mode,
    worktree choice and arguments come from what the agent was started with,
    not from today's config, so a role edited between runs cannot change what
    a restart means. An explicit ``binary`` / ``permission_mode`` / ``worktree``
    argument still wins over the spec, as it wins over the config; a recorded
    binary that has left the PATH is resolved again as a spawn resolves it
    when that lands on the same kind of program (``_in_place_of_recorded``),
    and the receipt names it. Every spawn records the spec it ended up with on
    the row. ``family`` is the replayed agent's adapter id whatever its binary is
    called, shown by the board session its own hook joined the row to — so the spec's
    session flags are not replayed where they are that agent's, and that fallback may
    land on today's binary of the same agent. ``bin_flag`` is
    whether the caller takes ``--bin``: a replacement :func:`_respawn` starts
    for ``restart`` or ``switch`` does not, so a refusal of its binary names
    ways out those commands have (:func:`_launch_binary`). ``own_worktree`` is
    such a replacement's too: the tree its launch record names, which it works in
    as it stands — branch, uncommitted work and all — never one the row's label
    names, never put on the branch today's task and codename would name; gone, it is
    made again on ``own_branch``, the branch the replaced row recorded it on
    (:func:`_worktree_for`). ``board_and_hub`` is such a replacement's too: the board it
    reads and records in turn, and the hub its window is given to resolve it, as
    :func:`_board_and_hub` answered before the replaced agent was stopped; the task is
    looked up there. A new agent reads the board this process gives it
    (:func:`_spawn_board_and_hub`): the board under the hub this process honours, or
    under ``hub`` when the caller names one, ``""`` for none. ``session`` is what the
    replaced agent's record holds of the session a replacement resumes, carried into the
    replacement's own record.

    Every spawn writes its launch record (``core.launch_seal``) before the window
    starts, from what it resolved itself: what a restart, a switch and the hand-over
    replay, and ``fleet tell`` queues with. An agent that sandboxes its tools is not
    started without one.

    Every ``None`` means "the role's default" (config, then built-in). Refuses
    past ``max_agents_per_project``, a second manager, a worktree in a non-git
    project, and an unknown role — each with the reason in the message.

    The window runs ``python -P -m aisquare launch <role> …`` (§3.4; ``-P`` is
    :func:`aisquare.core.selfcli.argv_for`'s guard against a project's own
    ``aisquare/`` package, and travels in the command because a window inherits
    the tmux SERVER's environment, not the spawner's): permission
    mode as ``--permission-mode`` (flag > role config > ``auto``; the empty
    string passes no flag), the minted ``--session-id`` unless the caller
    already named or resumed a session, ``--name <label>``, then the role's
    ``extra_args`` and the caller's ``agent_args``. ``AISQUARE_FLEET_AGENT``
    carries the row id into the window; ``CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=0``
    keeps Claude's native teams out of the fleet unless configured otherwise (§7.6);
    ``AISQUARE_TEAM_HUB`` is set to the hub that gives the board the agent reads
    (:func:`_spawn_board_and_hub`), a replacement's to the hub of the board it keeps, and
    ``AISQUARE_EXPLAINABILITY_TARGET`` to this process's value, or blank.

    Every variable set here is THIS window's: none reaches the tmux session's
    environment, so a later spawn sets its own account and opt-out rather than
    inheriting the first spawn's, and a window the operator opens by hand keeps
    whatever the server had (review of #203, round 4).
    ``account`` is resolved through the one account resolver (flag, role
    binding, project default, machine default — #145) and the chosen slot is
    both passed to ``launch`` as ``--account`` and recorded on the row.
    ``persona`` — the flag, else ``[fleet.roles.<role>].persona``, else none — is
    checked against the project's personas before anything starts, travels to the
    window as ``launch --persona`` and is recorded on the row
    (docs/plans/spawn-personas.md §3.7, §3.8).

    The row is written AFTER the window starts (``_record``: the window's
    ``pane_id`` is part of the row, and a label or cap race is settled against
    the window that exists). What keeps the agent's first hook from arriving
    before the row does is the launcher: ``aisquare launch``, inside the
    window, waits for the row ``AISQUARE_FLEET_AGENT`` names before it execs
    the agent (:func:`aisquare.cli.launch._await_fleet_row`), so a slow or
    locked store, a relabel or the cap check can delay the agent's start, never
    strip its briefing (review of #135, second round, cut item). ``replaces`` is a
    restart's, of an agent that ended: the row it brings back. Recorded stopped by the
    time the new row is written — the owner's ``fleet stop`` of its 💤 row during the
    start's seconds — the new row is not written and its window is killed
    (:class:`NoSuchAgent`): a stop that lands before the replacement exists wins (last
    delta review of #274, m3).

    A spawn is a deliberate add (#139): the project is onboarded with the row,
    once the window exists, and not before a refusal could still come (review
    of #168, round 2). A forget keeps the codename, so the onboarding cannot
    ride on the codename's assignment. ``onboard=False`` is the automatic
    usage-limit hand-over's (:func:`switch` with ``automatic``): nobody asked
    for that spawn, so a project a forget left captured stays off the list.

    ``takes_over`` is a FRESH hand-over's (:func:`switch` with no transcript to
    resume, or ``--fresh``; never with ``resume``): the id of the session the
    agent ran as until now, whose claims wait for the replacement
    (``team.HANDOVER_STATE``). The row is recorded on that id and then moved
    onto the new session's minted id together with the claims, in one store
    transaction — the move a ``/clear`` makes (:func:`_take_over`) — right
    after the insert. The launcher waits for the row, not for that move, so
    nothing ORDERS the move before the agent's first hook: in practice the hook
    comes seconds later (the launcher is still starting ``claude``, whose hook
    starts an interpreter of its own), and a start hook that does get there
    first makes the move itself when it can prove the pane is its own (rule 1
    of ``services.team``'s fleet-row section). One that cannot is briefed
    without its ASSIGNED TO YOU block — ``_late_assignment`` does not re-brief
    a bound row — and the hand-off prompt still names the task (review of
    #205, fifth round: this used to promise the move came first).

    ``fork`` and ``start_point`` are a fork's (:func:`hand_off`), which takes nothing that
    is already there. Its label is one NOTHING holds (:func:`_fork_label`): no row of the
    project in any state, no worktree and no branch, where every other spawn's is kept
    apart from the live agents only and carries on in what an ended one left. And
    ``start_point`` is the commit its source's tree is on, where its own worktree branch
    is cut instead of at the checkout's HEAD: that worktree is made new or the spawn is
    refused, since with a start point no tree and no branch that already exists is reused
    (:func:`_worktree_for`).

    ``size`` is the ``(columns, rows)`` the window is born with. The UI passes
    the pane it is about to attach, so the agent never runs wider than it will
    be shown; a headless spawn (a manager starting coders, `fleet spawn` with
    no UI open) takes ``core.tmux``'s default, which is kept under the 144
    columns at which Claude Code opens its diff panel on its own (#149). A
    window tmux would not resize to that size still runs, at its session's
    size, and a note says so (``WindowInfo.resize_refused``).
    """
    config = settings()
    _require_role(role)
    if experimental.captain_role(role) and not _is_home_project(project):
        raise FleetError(
            "the captain lives on the home board, one per home — `aisquare captain` starts "
            f"it; it cannot be spawned into {_name(project)}"
        )
    replayed_args = spec is not None and not agent_args
    if spec is not None:
        # The recorded launch stands in for the role's config, argument by argument
        # (the binary: `_launch_binary`, below). A spec's `None` permission mode is
        # "no flag was passed" — the model's contract, and what the first specs
        # recorded for `""`. Read as "not recorded" it handed the restart today's
        # config mode, `auto` by default: the silent change of permissions a replay
        # exists to prevent.
        if permission_mode is None:
            permission_mode = spec.permission_mode or ""
        worktree = worktree if worktree is not None else spec.worktree
        if replayed_args and not experimental.captain_role(role):
            agent_args = _replayed_args(role, spec, config)
    if experimental.captain_role(role) and not _is_the_captains_launch(cwd):
        # `fleet spawn captain` on the home was a captain with every tool, in the
        # home itself — and `aisquare captain` then attached to it and typed into it.
        raise FleetError(
            "the captain is started by `aisquare captain`, which gives it its brain folder "
            "and no tool but its own server"
        )
    srv = server(config)
    _require_tmux(srv)
    notes: list[str] = []
    # A replay starts the binary its spec recorded or, once that has left the PATH,
    # today's of the same kind (`_launch_binary`); the family follows that binary.
    chosen = binary
    if spec is not None and binary is None:
        replayed = _launch_binary(
            project, role, binary=None, spec=spec, family=family, notes=notes, bin_flag=bin_flag
        )
        # Today's default stays the default: carried as `--command` only when chosen.
        chosen = replayed.binary if replayed.source != "default" else None
    try:
        selected = agent_launch.resolve(role, agent=agent, binary=chosen, cwd=project.root)
    except ValueError as exc:
        raise FleetError(str(exc)) from exc
    resolution = selected.binary
    if agent_launch.executable(selected) is None:
        way_out = (
            "install it, pass --bin, or change the role's binding"
            if bin_flag
            else "install it, or change the role's binding"
        )
        raise FleetError(
            f"{resolution.binary!r} is not on your PATH (chosen by: {resolution.source}) — "
            + way_out
        )
    _refuse_an_env_file_it_cannot_read(role)
    role_config = role_settings(role, config)
    chosen_persona = _chosen_persona(project, role, persona, role_config, notes)
    # A server that exited at the socket path left its rows live, and the server this spawn
    # starts there would hold them for good: they read lost from now on, in every project,
    # decided while the stale file still says so. And a socket path a running server lost
    # is not one to start a server at: the agents on it would be stranded for good, and the
    # new server would answer for their rows. Asked here so the refusal comes before the
    # project is captured or a worktree made (#139), and again as the window opens
    # (`open_window`), as a server can exit while the worktree is checked out.
    exited = _before_a_server_starts(srv, config)
    if exited:
        notes.append(_exited_note(srv, exited))
    from aisquare.core.agent_adapters.types import fleet_extra_args

    # A replayed spec already holds the role's arguments as they were at spawn;
    # adding today's would double them (or add ones the agent never had). The
    # captain's are never replayed (above): its tools and its environment are given at
    # every launch (below), and a row 0.8.2 recorded holds its old confinement, a
    # second --mcp-config naming a frozen mcp.json (review r17 of #113, finding 6).
    role_args = (
        []
        if spec is not None and not experimental.captain_role(role)
        else fleet_extra_args(selected.adapter, role_config.extra_args, role_config.agent_args)
    )
    extra = list(agent_args)
    if replayed_args:
        # Filtered on the way OUT as well as on the way in: a spec written before
        # session choices were left out of it still holds them, and the board session
        # its hook joined is what shows the binary to be that agent's whatever its name.
        extra = _without_session_choice(resolution.binary, extra, family=family)
    # What the spec records: the agent's own arguments, BEFORE this launch's
    # `--resume` is prepended — a resume is per launch, and a restart of the
    # restarted agent must not carry an old transcript path into the new one.
    # The same goes for a session the CALLER chose (`--session-id`, `--resume`,
    # `--continue`): see `_without_session_choice`.
    recorded_args = _without_session_choice(resolution.binary, [*role_args, *extra])
    forked: ForkArgs | None = None
    if resume is not None:
        # The agent's resume (`--resume <transcript path>`) keeps the ORIGINAL session
        # id (#146), so the row is joined to it here rather than minted or learned
        # later; the identity planner would otherwise read the path as the id. A fork's
        # is the agent's own (`fork_args`): a new session from the source's conversation.
        native_id = resume.native_session_id or resume.session_id
        if resume.fork:
            forked = selected.adapter.fork_args(native_id, resume.transcript_path)
            if forked is None:
                raise FleetError(f"{selected.adapter.label} cannot fork a session")
            lead = list(forked.args)
        else:
            lead = selected.adapter.resume_args(native_id, resume.transcript_path)
        extra = [*lead, *extra]
    # Validate native model pins before creating a worktree/window or live row.
    model_resolution = None
    try:
        defaults = selected.adapter.role_defaults(
            role, binary=resolution.binary, args=[*selected.profile.args, *role_args, *extra]
        )
        arguments = agent_launch.prepare_arguments(
            selected, [*defaults.args, *selected.profile.args, *role_args], extra
        )
        # The role's ladder and pins, as `team spawn` resolves them (`team harness`).
        model_resolution = agent_launch.launch_model_for(selected, role, arguments, ladder=True)
        model_args = agent_launch.resolved_model_args(selected, model_resolution, arguments)
    except ValueError as exc:
        raise FleetError(str(exc)) from exc
    notes.extend(model_resolution.notes if model_resolution else [])
    # Not for the role's built-in arguments (the reviewer's `--disallowedTools`), which
    # `init` writes and nobody chose: every Codex reviewer spawn on a fresh config said it,
    # in a Codex manager's tool output too, with nothing for anyone to change.
    if (
        spec is None
        and role_config.extra_args
        and role_config.extra_args != built_in_extra_args(role)
        and not selected.adapter.capabilities.legacy_fleet_args
    ):
        notes.append(
            f"Legacy fleet extra_args apply only to Claude Code; use "
            f"fleet.roles.{role}.agent_args.{selected.adapter.id} for native arguments"
        )
    # Nothing up to the codename below writes the project's registration: every
    # refusal that needs no window is given first (#139). A spawn refused for its
    # account, its worktree, its task, the cap or a second manager had already
    # onboarded the project, which listed a captured directory, and brought back a
    # forgotten one, for an agent that never started (review of #168, round 2).
    # The deliberate add is made with the agent's row (`_record`).
    # The census of the rows spawned before launch records were kept, before this spawn
    # starts any window, so before any agent it starts could write the store (`take_census`).
    take_census()
    with store_session() as store:
        known = store.get_project(project.id)
        rows = store.fleet_agents(project.id, live_only=False)
    # The codename a fork's label must not be a branch of (`_fork_label`).
    observed = project.codename or (known.codename if known is not None else None)
    # Observed, ended and superseded where their launch records place them, each record
    # read once: the live rows and those ended within RECENTLY_ENDED, whose 💤 windows the
    # listing shows and a replacement supersedes, never every row the project ever had
    # (review r7 of #113, finding 10).
    cutoff = _now() - RECENTLY_ENDED
    # Running is what the panes say, whatever a row's `ended_at` (review r9 of #113, 6).
    still = _running_ids(project.id, {row.id for row in rows if row.ended_at is not None}, config)
    placements = _resolve_all(
        [
            row for row in rows
            if row.ended_at is None or row.ended_at >= cutoff or row.id in still
        ],
        config,
    )  # fmt: skip
    live = [placement for placement in placements if placement.row.ended_at is None]
    # A row whose pane has died is ended HERE, before the checks below read it
    # (#138): a manager killed with ctrl+c in its window left a "live" row that
    # refused `fleet spawn manager` with "already has a manager" until a hand-run
    # `reap`, while everyone could see the pane was dead. Absence is not
    # evidence and is left to `reap` (see `list_agents`). Every row is observed,
    # not only the live ones, because the ended rows' windows are read below.
    views = _observe_sockets(placements, still)
    for ended in _end_dead_rows(live, views):
        again = resolve_place(ended, config=config)  # the store's row again, placed
        live = [placement for placement in live if placement.row.id != ended.id]
        placements = [
            again if placement.row.id == ended.id else placement for placement in placements
        ]
    # Running is what the window says, never the row's `ended_at` (review r8 of #113): a row
    # ended in the store whose window runs is still an agent of the project's.
    running = [*live, *(p for p in placements if _ended_but_running(p, views, still))]
    # Their labels and their trees are theirs too, the record's (review r9 of #113, 5).
    held = {placement.agent.label for placement in running}
    occupied = [
        Path(placement.record.cwd)
        for placement in _placed_only(running)
        if placement.record.worktree
    ]
    if role == "manager":
        # The record's role, not the row's: a manager row set to `coder` was no manager
        # to this guard, and a second started beside it (review r9 of #113, finding 9).
        existing = next(
            (placement.row for placement in running if placement.agent.role == "manager"),
            None,
        )
        if existing is not None:
            raise FleetError(
                f"{_name(project)} already has a manager ({existing.id}) — one per "
                f"project; `{project_service.fleet_command('stop', project, 'manager')}` "
                "first"
            )
    if experimental.captain_role(role):
        # The record's role, as the manager's guard above reads it.
        captain = next(
            (placement.row for placement in running if placement.agent.role == CAPTAIN_ROLE),
            None,
        )
        if captain is not None:
            raise FleetError(
                f"the home already has a captain ({captain.id}) — one per home; "
                "`aisquare captain` attaches to it"
            )
    if len(running) >= config.max_agents_per_project:
        raise FleetError(
            f"{_name(project)} already runs {len(running)} agents "
            f"(max_agents_per_project = {config.max_agents_per_project}) — stop one, "
            "or raise the limit in [fleet]"
        )
    # Before the store opens: with no hub, `_board_for` runs git (review of #259, round 29,
    # 5/8), and the refusals above need neither.
    board, hub = (
        board_and_hub if board_and_hub is not None else _spawn_board_and_hub(project, role, hub)
    )
    use_worktree = role_config.worktree if worktree is None else worktree
    with store_session() as store:
        task = _task_for(store, board, task_id)
        resolved_task_id = task.id if task is not None else None
        if fork:
            # Held by nothing, where a spawn's label is kept apart from the live agents
            # only: a fork is not the next agent under a label (review of #240, finding 3).
            picked = _fork_label(
                project,
                role,
                label,
                store=store,
                trees=project.root / config.worktree_dir if use_worktree else None,
                codename=observed,
            )
        else:
            picked = next_label(
                project, role, wanted=label, task_id=resolved_task_id, store=store, held=held
            )
    if label is not None and picked != label:
        if role == "manager":
            notes.append(f"the manager is always labelled {MANAGER_LABEL!r} (asked: {label!r})")
        else:
            notes.append(f"label {label!r} is held by a live agent — using {picked!r}")

    def refuse_if_taken() -> None:
        _refuse_occupied_worktree(project, config.worktree_dir, picked, occupied)

    if use_worktree:
        if not is_git_project(project.root):
            raise FleetError(
                "not a git repository — spawn without --worktree or pick a repo inside it"
            )
        refuse_if_taken()

    # The agent's own flags, refused (a sandbox for Claude Code, a Claude mode for Codex,
    # an intent the agent cannot express) before a worktree or a codename is made for a
    # spawn that cannot start.
    native, native_mode = permission_flags(
        selected.adapter,
        role,
        picked,
        role_config,
        permission_mode=permission_mode,
        sandbox=sandbox,
        approval_policy=approval_policy,
        native=arguments.argv,
        notes=notes,
    )
    # WHICH ACCOUNT, decided here and carried into the window as an explicit
    # `--account <slot>` (#145). The one resolver `launch` itself uses — flag,
    # role binding, project default, machine default — runs HERE rather than
    # only inside the window because the window's environment is the tmux
    # server's, not the spawner's: a default of "slot 1" means THIS shell's
    # claude, and only this process knows what that is (`carry_environment`
    # below). It also lets the row record the slot before the agent has said a
    # word, which a restart (#144) or a hand-over (#146) reads. An account the
    # ladder names and the machine does not have refuses the spawn now, with
    # the rung named, rather than starting a window that dies on its first line.
    # The account is the role's agent's: a Codex role on a Codex slot, and another
    # agent's account refused here, naming both.
    choice = claude_accounts_service.AccountChoice(account=None, source=None)
    if selected.adapter.accounts is not None:
        try:
            choice = claude_accounts_service.choose(
                account, role=role, project=project, agent=selected.adapter.id
            )
        except claude_accounts_service.NoSuchAccount as exc:
            raise FleetError(str(exc)) from exc
    elif account is not None:
        raise FleetError(
            f"--account selects an account, and {selected.adapter.label} has no account slots"
        )
    if experimental.captain_role(role):
        # Asked of the home the window runs under, the account's, else this process's,
        # which the window is given below: its operator's own MCP servers are the ones the
        # captain's session must not load.
        native += _the_captains_tools(
            selected.adapter,
            choice.account.config_dir if choice.account is not None else selected.config_dir,
            cwd or project.root,
            arguments.argv,
        )

    # Whether its launch record can be written, asked before the codename and the worktree,
    # as every refusal that needs no window is (#139): refused once they were made, a spawn
    # left a captured project and a new worktree and branch for an agent that never started
    # (review r5 of #113, finding 9), a directory that is there but takes no write included
    # (SMOKE4). What the record holds is written below.
    unrecordable = launch_seal.writable()
    if unrecordable is not None:
        raise FleetError(_unrecorded_refusal(unrecordable))
    # Whether its sandbox may write an aisquare its hooks or broker run, asked here as the
    # window's own launch would ask it: refused there, the spawn reported a live agent whose
    # window had already exited (review r12 of #113, finding 9).
    if selected.adapter.capabilities.sandboxes_tools:
        if not use_worktree:
            prospective = project.root
        elif own_worktree is not None:
            prospective = own_worktree
        else:
            prospective = project.root / config.worktree_dir / picked
        home = choice.account.config_dir if choice.account is not None else selected.config_dir
        _refuse_an_install_the_sandbox_writes(
            [resolution.binary, *native, *arguments.argv],
            prospective,
            selected.adapter,
            home,
            _window_path(config, selected.profile.env),
        )
    # The spawn goes ahead: the codename names its tmux session and its branch. A
    # directory with no live row, never seen or forgotten, is captured for it
    # (`ensure_codename`), not listed, and a worktree or a window that then cannot
    # be made leaves it captured. With `[projects] capture` off nothing is written
    # here: the codename is worked out, and stored with the add and the agent's row
    # (`_write_row`), so a refusal from here on leaves no row behind (review of #259,
    # round 17).
    with store_session() as store:
        project = ensure_codename(project, store)
        codename = project.codename or _codename_to_add(store, project)
    notes.extend(f"accounts: {note}" for note in choice.notes)
    if forked is not None and forked.mints_id:
        # A FORK (card tsk_01m3ns5a736s) by an agent the launch names (`ForkArgs.mints_id`,
        # Claude Code's `--resume <transcript> --fork-session`): the source's conversation
        # under a NEW id, minted here as a fresh start's is (`minted_identity`, passed in
        # its own flag, `--session-id`: Claude Code takes the two together only when it
        # forks), so the row joins the fork's own session. The identity planner is not
        # asked: it reads `--resume <path>` as the RESUMED id and knows nothing of
        # `--fork-session`. The fork's arguments come before the recorded ones and are never
        # recorded (`_without_session_choice`), so a restart of the fork resumes the fork,
        # never its source.
        identity = explainability_service.minted_identity(selected.adapter)
    elif forked is not None:
        # A fork the agent names itself (Codex's `fork <id>`): a new thread, whose id its
        # first hook brings, as a fresh start's does.
        identity = explainability_service.SessionIdentity(
            None, note="native ID binds at session start"
        )
    elif resume is not None:
        identity = explainability_service.SessionIdentity(resume.session_id)
    elif selected.adapter.capabilities.assigns_session_id:
        identity = explainability_service.plan_session_identity(resolution.binary, arguments.argv)
    else:
        identity = explainability_service.SessionIdentity(
            None, note="native ID binds at session start"
        )
    if identity.session_id is None and selected.adapter.capabilities.assigns_session_id:
        notes.append(
            f"no board join for this agent ({identity.note}) — its state comes from tmux alone"
        )
    agent_id = new_agent_id()
    flags: list[str] = []
    # A replay that fell back to today's default binary resolved it for the row's agent
    # (`_respawn` passes it as `agent`), which labels it that choice's; the window's
    # `launch`, given the same `--agent`, lands on the same binary by itself.
    replayed_default = (
        spec is not None
        and binary is None
        and chosen is None
        and resolution.binary == selected.adapter.binary
    )
    if resolution.source != "default" and not replayed_default:
        # `launch` re-resolves the binary inside the window — and the window's
        # environment is the long-lived tmux SERVER's, which never carries
        # `AISQUARE_BIN_<ROLE>` or `AISQUARE_AGENT_BIN` (`core/tmux.py` spawns
        # with `untraced_env()` and passes exactly two per-window keys). So
        # ANYTHING but the default has to be carried explicitly, not just an
        # explicit `--bin`: with the variable exported in this shell and not in
        # the server's, the row recorded `claude2` while the pane silently ran
        # `claude`. Measured on a role whose flags are keyed on the binary
        # (the adapter's `role_defaults`), that also decided the flag question against
        # the wrong executable — a ui-tester launched without `--chrome`, exit 0,
        # nothing printed. `docs/fleet.md` promises `AISQUARE_BIN_<ROLE>` works
        # for a fleet launch; this is what makes that true.
        flags += ["--command", resolution.binary]
    # The parent already resolved this family. Carry that decision through
    # tmux so a legacy wrapper is not reclassified against inherited defaults.
    flags += ["--agent", selected.adapter.id, "--no-bound-args"]
    native += list(identity.inject_args)
    if choice.account is not None:
        # Carried to `launch` as the RESOLVED slot, so the window's own pass
        # through the resolver lands on the same account whatever its
        # environment says, and sets the account's variables there.
        flags += ["--account", str(choice.account.slot)]
    if chosen_persona is not None:
        # A flag, not an env var: the window's environment is the tmux SERVER's.
        flags += ["--persona", chosen_persona]
    env = {
        orchestrator.FLEET_AGENT_ENV_VAR: agent_id,
        "AISQUARE_LAUNCH_ID": "",
        agent_launch.LAUNCH_AGENT_ENV: selected.adapter.id,
    }
    if experimental.captain_role(role):
        flags += _the_captains_environment(project)
        # The launcher activates a board BEFORE it hands the agent its `-e` pairs,
        # from its cwd — the brain folder, where `.aisquare` above it is a project
        # marker. With the hub only in `-e`, it onboarded the brain folder (or
        # `$HOME`, under `~/.aisquare`) as a project; the window's hub, the home's
        # root (`_spawn_board_and_hub`), lands the launcher on the home board, which
        # the store keeps captured (T2).
        env["AISQUARE_HOME"] = str(paths.aisquare_home().resolve())
        # Nobody may be watching its pane, which is answered from a phone: a prompt of the
        # agent's own there holds it (`unattended_env`). Claude Code's session-rating survey
        # drew in the captain's pane mid-conversation, and send (rightly) never types into a
        # dialog: every voice line was refused, and from the phone the owner cannot answer
        # it (T1e). The screen's refusal (`modal_on_screen`) stays the backstop.
        env.update(selected.adapter.unattended_env())
        # The experimental switch as THIS process has it, when it has it. The window's
        # launcher looks the bundled `captain` persona up again, and with the switch set
        # only in the starting shell — the server started without it — the window said
        # "no persona named 'captain'" and exited under "started the captain" (review of
        # #240, finding 13). A window pair, never a `launch -e` one: those reach the
        # agent after that lookup. Read at each start, a restart's included.
        env.update(experimental.captain_environment())
        if choice.account is None and selected.adapter.capabilities.exports_home_env:
            # The home its tools were confined against (`_the_captains_tools`), above: the
            # window's own is the tmux server's, which carries no spawner's (a Codex tool
            # shell's is stripped, another shell's never there). A Codex captain's off-list
            # read from this one switched off a server the window's config does not define,
            # and Codex refused that config (review r17 of #113, finding 8).
            env[selected.adapter.home_env] = str(selected.config_dir)
    # The label's and the teams' own environment and settings beside their arguments (an
    # agent whose seams are its variables and settings files), for the window's `launch` to
    # give as the agent's settings layer (`agent_launch.compose`).
    agent_launch.carry_settings(env, replace(selected.adapter.label_args(picked), args=()))
    if config.disable_native_agent_teams:
        teams = selected.adapter.disable_native_teams()
        native += teams.args
        agent_launch.carry_settings(env, teams)
    # `-E`: the window's `aisquare launch` ignores the tmux server's PYTHONPATH, which may
    # name a directory a sandboxed agent writes, while the agent it execs keeps it (review
    # r14 of #113, finding 4).
    command = selfcli.isolated_argv_for(
        ["launch", role, *flags, "--", *native, *model_args, *arguments.argv]
    )
    # A spawn for a task with no prompt of its own is told to read it (#32, L1; the owner's
    # D2): worked out before the window's command, which carries the prompt of an agent
    # that takes it as its argument.
    if prompt is None and task is not None and resume is None and takes_over is None:
        prompt = task_prompt(task)
    if prompt and selected.adapter.capabilities.positional_prompt:
        command += ["--", prompt]
    # The hub as THIS process resolves it, blank for none: the window's
    # `launch` joins the project the spawner — and the UI's Explainability tab
    # beside it — resolves, and takes that project's key, not the one the tmux
    # server's frozen environment would name (review of #170, D1b round 2, B1).
    # A replacement's is the hub of the board its row recorded (`_board_and_hub`).
    env[orchestrator.TEAM_HUB_ENV_VAR] = hub
    # The persona as THIS spawn chose it, blank for none. ``launch`` exports
    # AISQUARE_PERSONA only when it is given one, and the private tmux server keeps the
    # environment of whoever started it: a manager launched with ``--persona skeptic``
    # that started the server gave every persona-less seat on it, and every replay of
    # one, its skeptic briefing (review of #201, F3).
    env[orchestrator.PERSONA_ENV_VAR] = chosen_persona or ""
    # The deployment override too, for the same reason: the tab resolves the
    # key's deployment, and shows the target, with THIS process's variable,
    # and the window's `launch` has to trace where the tab says. Blank for
    # none, which `resolve_target` reads as unset (review of #170's Setup-form
    # merge, G2).
    env[explainability_ops.TARGET_ENV_VAR] = os.environ.get(
        explainability_ops.TARGET_ENV_VAR, ""
    ).strip()
    # The desktop as THIS process sees it (#147). A window inherits the tmux
    # server's environment, frozen at the server's first start, so after a
    # re-login every new agent had a stale DISPLAY, WAYLAND_DISPLAY, bus and SSH
    # agent socket — ctrl+v image paste, desktop notifications and `gh` over an
    # agent-forwarded key failed in silence. Set per window, the current values
    # win over the server's for this agent alone.
    env.update(tmux_core.desktop_environment())
    if choice.account is not None:
        # `launch --account 1` restores "this shell's" login, and inside the
        # window that shell would be whoever started the private server — so
        # the CALLER's aisquare home and account variables travel with the
        # window (set as absolute paths, or unset through `env -u`), exactly as
        # the Accounts page's sign-in window carries them.
        command, carried = claude_accounts_service.carry_environment(
            command, agent=choice.account.agent
        )
        env.update(carried)
    # `auto` behind the explainability proxy is refused from the first tool call
    # once the session baseline is past what the proxy's non-streaming forward
    # survives (#150). The evidence is read NOW, before the window starts: past
    # that the agent is starting, and neither its own session nor the store and
    # transcript reads may land between the row and `_take_over` (review of
    # #169, round 2). The note is said once the row is written, below.
    evidence = (
        auto_mode.spawn_evidence(native_mode) if selected.adapter.capabilities.model_proxy else None
    )
    # The session the window joins is one a launch record of this project's names, by
    # tmux's id, on this server; with none, a session of its own is made, never one found
    # under `asq-<codename>`, a name read from the store: two projects' codenames swapped,
    # the window went into the other's session, its record named it, and `shutdown` took
    # that session down with the other project's agents in it (review r7 of #113, 2).
    joined = _session_to_join(project.id, placements, config)
    width, height = size if size is not None else (DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT)
    # Before the window: a session start since this moment is this launch's (`_type_prompt`).
    launched = _now()
    # The tree, made or reused, and the row that says an agent works in it, under one hold
    # of the branch lock a reap removes trees under (`_reap_tree`): a reap that
    # landed between a reuse and the row read the tree as an ended agent's, clean and
    # pushed, and removed it from under the agent starting in it (review of #259, round 17).
    made: list[str] = []
    common = _git_common_dir(project.root) if use_worktree else None
    # Where the agent will start: the tree its launch record names for a replay, else the
    # worktree `_worktree_for` makes or reuses, else the root, or a non-worktree agent's own
    # ``cwd`` when it is not the project's root: the captain runs from its brain folder under
    # the home, so no project's CLAUDE.md or hooks brief it as a worker (T2, 13121). The row
    # records it and a restart replays it.
    unworked = cwd if cwd is not None else project.root
    start = (
        unworked
        if common is None
        else own_worktree
        if own_worktree is not None
        else project.root / config.worktree_dir / picked
    )
    settled: str | None = None
    if choice.account is not None:
        # Here as well as in the window's `aisquare launch`, so the receipt — the manager's
        # one view of the spawn — says when the folder's trust could not be shared and the
        # agent will stop at Claude Code's question. Before the branch lock, which a reap
        # and every other spawn in the repository queue on (review of #259, round 21): for
        # the folder the agent starts in, with the entries that will cover it once made.
        # Asked of the project's root instead, it copied an entry on a root that is a
        # sub-folder of its repository, which does not cover the tree, and missed one on the
        # tree itself (round 22). And the window is told, so its launch does not read the
        # same files again; with no account chosen, only the window knows its environment.
        # Only between accounts of the launch's own agent, in its own file (`share_for_launch`).
        scope = claude_sharing.worktree_scope(project.root, start) if common is not None else start
        shared = claude_accounts_service.share_for_launch(scope, account=choice.account)
        notes.extend(f"accounts: {note}" for note in shared)
        made = [
            f"accounts: {note}"
            for note in shared
            if isinstance(note, claude_accounts_service.SharedNote)
        ]
        if len(made) == len(shared):  # nothing it could not share, which the launch tries again
            settled = str(choice.account.slot)
    tree_lock = (
        _branches_locked(common, wait=_BRANCHES_LOCK_WAIT)
        if common is not None
        else contextlib.nullcontext()
    )
    with tree_lock:
        cwd = unworked
        if common is not None and own_worktree is not None and own_worktree.exists():
            # A replay works in the tree its launch record names: one the row's label
            # named was the store's to choose, and `_worktree_for` made a new one there.
            if not (own_worktree / ".git").exists():
                raise FleetError(
                    f"{own_worktree}, the worktree it was spawned in, is no longer a git "
                    "worktree — spawn a new agent"
                )
            cwd = own_worktree
            notes.append(f"kept the agent's worktree at {cwd} as it stands")
        elif common is not None:
            branch = branch_name(
                codename,
                task_id=resolved_task_id,
                title=task.title if task is not None else picked,
            )
            # A replay's tree that is gone is made again where its record names it, on the
            # branch its row recorded (`_remake_worktree`).
            cwd = _worktree_for(
                project.root,
                config.worktree_dir,
                picked,
                branch,
                notes,
                refuse_if_taken=refuse_if_taken,
                own=own_worktree,
                own_branch=own_branch,
                common=common,
                start_point=start_point,
            )
        if settled is not None and os.path.realpath(cwd) == os.path.realpath(start):
            # Only for the folder it shared for: a restart's own tree can be elsewhere.
            env[claude_accounts_service.SHARED_ENV] = settled
        launch_spec = LaunchSpec(
            binary=resolution.binary,
            permission_mode=native_mode,
            extra_args=recorded_args,
            account_slot=choice.account.slot if choice.account is not None else None,
            worktree=use_worktree,
            command=list(command),
            recorded_by=__version__,
            hub=hub,
        )
        if session is not None:
            # The predecessor's start is not this launch's: its own hook says when it started.
            session = session.model_copy(update={"started_at": None})
        elif identity.session_id is not None:
            session = launch_seal.SessionFacts(id=identity.session_id, agent=selected.adapter.id)
        # Everything a replay starts this agent with, written before it runs, by this
        # process alone (`core.launch_seal`): the records, as the whole store now, are
        # read-only to an agent's sandbox, and nothing reads the store to write one.
        launch_record = launch_seal.LaunchRecord(
            id=agent_id,
            project_id=project.id,
            project_root=str(project.root),
            role=role,
            agent=selected.adapter.id,
            binary=resolution.binary,
            cwd=str(cwd),
            worktree=use_worktree,
            account_slot=choice.account.slot if choice.account is not None else None,
            account_home=str(choice.account.config_dir) if choice.account is not None else None,
            spec=launch_spec,
            session=session,
            label=picked,
        )
        unrecorded = launch_seal.write(launch_record)
        if unrecorded is not None:
            raise FleetError(_unrecorded_refusal(unrecorded))
        try:
            # Every pair is this window's alone: `spawn_window` keeps them out of
            # the session environment, so a later spawn — or a window opened by hand
            # — starts from the server's environment and sets its own (review of
            # #203, rounds 3 and 4; §7.6 for the opt-out).
            window = (
                joined.new_window(
                    name=picked,
                    cwd=cwd,
                    command=command,
                    env=env,
                    width=width,
                    height=height,
                    notes=notes,
                )
                if joined is not None
                else _new_session_window(
                    srv,
                    session_name(codename),
                    name=picked,
                    cwd=cwd,
                    command=command,
                    env=env,
                    width=width,
                    height=height,
                    config=config,
                    notes=notes,
                )
            )
        except TmuxError as exc:
            raise FleetError(f"tmux could not start the window: {exc}") from exc
        tmux_session = window.session
        if window.resize_refused is not None:
            # The agent runs either way; what it costs is #149 itself, so it is said
            # rather than left for the diff panel to explain (review of #162, round 1).
            notes.append(
                f"tmux would not size the window {width}x{height} ({window.resize_refused}) — "
                "it runs at its session's size until a pane shows it, and at 144 columns or "
                "wider Claude Code opens its diff panel on its own"
            )
        # Where this server listens and in which boot, as the window exists: what uninstall
        # and upgrade ask whether the row may still run, rather than inferring it from the
        # shell they run in (round 25 of #257). Not saying is not failing the spawn.
        try:
            socket_path = srv.server_socket_path()
        except TmuxError:
            socket_path = None
        # And when it started: the identity of the server the pane id names, since every server
        # numbers from %0. A server at that socket started at another time is not this one
        # (`_outlived`, `server_may_run`): it took the path from this one, which lost its socket
        # file and runs on (follow-up of #257). Not saying is not failing the spawn either.
        try:
            server_started = srv.started_at()
        except TmuxError:
            server_started = None
        created_at, boot = _now(), tmux_core.boot_id()
        # Where the window is, beside its record, before the row names it: every line the
        # fleet types into the agent, and every kill of its window, is aimed there, never at
        # the row's pane and socket, which a sandbox could write before its broker (review
        # r5 of #113, finding 1). A window whose place cannot be recorded is one no `tell`
        # or `stop` could reach, so it is not left running.
        unplaced = launch_seal.placed(
            agent_id,
            launch_seal.WindowPlace(
                tmux_socket=config.tmux_socket,
                pane_id=window.pane_id,
                socket_path=str(socket_path) if socket_path is not None else None,
                boot_id=boot,
                created_at=created_at,
                server_started_at=server_started,
                session=tmux_session,
                session_id=window.session_id,
            ),
        )
        if unplaced is not None:
            _kill_unrecorded(srv, window.pane_id)
            raise FleetError(
                f"its launch record could not say where its window is ({unplaced}) — its tmux "
                "window was killed rather than left where no `fleet tell` or `fleet stop` "
                "could reach it"
            )
        if window.session_id is not None:
            # The project's sessions, beside its records: what its later spawns join and
            # `shutdown` takes down, however long its last agent has been gone (review r8 of
            # #113, findings 2 and 4). One that cannot be written is still named by this
            # agent's record.
            launch_seal.add_project_session(
                project.id,
                launch_seal.SessionPlace(
                    tmux_socket=config.tmux_socket,
                    socket_path=str(socket_path) if socket_path is not None else None,
                    session_id=window.session_id,
                    name=tmux_session,
                    created_at=created_at,
                    server_started_at=server_started,
                ),
                # Its window too: which of the project's rows run is read from the panes,
                # never from their `ended_at` (review r9 of #113, findings 5 and 6).
                launch_seal.WindowEntry(
                    row_id=agent_id,
                    tmux_socket=config.tmux_socket,
                    socket_path=str(socket_path) if socket_path is not None else None,
                    session_id=window.session_id,
                    pane_id=window.pane_id,
                    created_at=created_at,
                    server_started_at=server_started,
                ),
            )

        fleet_agent = FleetAgent(
            id=agent_id,
            project_id=project.id,
            label=picked,
            role=role,
            binary=resolution.binary,
            agent=selected.adapter.id,
            tmux_socket=config.tmux_socket,
            pane_id=window.pane_id,
            # A fresh hand-over's row starts on the session whose claims it inherits,
            # the state `_take_over` moves it out of (and the agent's own start hook
            # would, should that move not happen).
            session_id=takes_over if takes_over is not None else identity.session_id,
            cwd=cwd,
            worktree=use_worktree,
            task_id=resolved_task_id,
            spawned_by=spawned_by,
            account_slot=choice.account.slot if choice.account is not None else None,
            launch_spec=launch_spec,
            socket_path=socket_path,
            boot_id=boot,
            server_started_at=server_started,
            branch=_spawned_on(cwd) if use_worktree else None,
            board=board,
            created_at=created_at,
            persona=chosen_persona,
        )
        stored = _record(
            fleet_agent,
            project,
            srv,
            wanted=label,
            notes=notes,
            cap=config.max_agents_per_project,
            onboard=onboard,
            codename=codename,
            replaces=replaces,
        )
    if stored.label != picked:
        # Re-picked on a collision: the record names the label the row was stored under,
        # which `fleet stop <label>` and `tell` resolve by (review r8 of #113, finding 5).
        # Under the record's lock, beside what the agent's hook adds (review r9, 13).
        launch_seal.relabel(agent_id, stored.label)
    # The auto-mode note (#150), from the evidence read before the window: on the
    # receipt when this machine's transcripts say so, a warning, never a reason
    # not to spawn. Worded once the row is written, so the restart it names is
    # under the label the agent was recorded with (`_record` re-picks one taken
    # meanwhile); a sentence, no store or file read.
    with contextlib.suppress(Exception):
        warning = auto_mode.spawn_note(
            evidence, role=role, label=stored.label, config=config, project=project
        )
        if warning is not None:
            notes.append(warning)
    # Every note `_take_over` writes is a move that did not happen; `_type_prompt`'s are
    # when the prompt was not typed (`SpawnReceipt.failures`).
    moved: list[str] = []
    if takes_over is not None and identity.session_id is not None:
        stored = _take_over(stored, takes_over, identity.session_id, moved)
    # Where its record places it, as every other row: the pane tmux made and the agent
    # resolved here, never the row as the store reads them back.
    started = resolve_place(stored, config=config)
    _supersede(placements, views, started)
    typing: list[str] = []
    # The keys pressed so the prompt went in (#32): done, not a warning.
    done: list[str] = []
    # A prompt an agent takes on its command line went into the window's command: it
    # reaches the agent as its argument, and the restart or switch that passed it reads
    # that as delivered.
    typed: Typed | None = (
        None
        if not prompt
        else "yes"
        if selected.adapter.capabilities.positional_prompt
        else _type_prompt(
            started,
            prompt,
            typing,
            project=project,
            session_id=identity.session_id or stored.session_id,
            launched=launched,
            folder=cwd,
            done=done,
        )
    )
    return SpawnReceipt(
        agent=stored,
        asked_label=label,
        tmux_session=tmux_session,
        notes=[*notes, *moved, *done, *typing],
        prompt_typed=typed,
        failures=[*moved, *(typing if typed not in (None, "yes") else [])],
        shared=[*made, *done],
    )


def permission_flags(
    adapter: AgentAdapter,
    role: str,
    label: str,
    role_config: FleetRoleSettings,
    *,
    permission_mode: str | None = None,
    sandbox: str | None = None,
    approval_policy: str | None = None,
    native: Sequence[str] = (),
    notes: list[str] | None = None,
) -> tuple[list[str], str | None]:
    """The permission flags (and the label's) a spawn of ``role`` passes ``adapter``'s agent,
    with the value its launch spec records (``LaunchSpec.permission_mode``), and in ``notes``
    what the receipt says of them (``PermissionArgs.notes``: an ``auto`` run as ``ask`` by
    an agent with no reviewer of its own).

    One permission for every agent — the role's ``permission``, or ``WRITE:APPROVALS``
    as this spawn's (or a replayed) ``permission_mode`` — is the adapter's to map
    (``AgentAdapter.permission_args``), and what the ``native`` arguments already settle
    wins. A value in the agent's own words, and this spawn's ``sandbox``/
    ``approval_policy``, take the agent's own path (``AgentAdapter.fleet_args``) with the
    role's agent-specific settings, as they always did. Raises :class:`FleetError` for
    what the agent refuses. ``spawn`` and the Settings page's launch line both ask this.
    """
    intent = _permission_intent(
        role,
        role_config,
        permission_mode,
        native_given=sandbox is not None or approval_policy is not None,
    )
    if intent is not None:
        try:
            granted = adapter.permission_args(intent, role, native)
        except ValueError as exc:
            raise FleetError(f"{adapter.label}: {exc}") from exc
        flags = [*granted.args, *adapter.label_args(label).args]
        if notes is not None:
            notes.extend(granted.notes)
        # Recorded as written, so a restart maps it again for whichever agent it starts.
        return flags, str(intent)
    mode = role_config.permission_mode if permission_mode is None else permission_mode
    # The role config's modes are in the legacy (Claude Code's) vocabulary; another agent
    # takes only an explicit one, in its own (a replay's "" is "none recorded": its default).
    native_mode = mode if adapter.capabilities.legacy_fleet_args else (permission_mode or None)
    sandboxed = adapter.capabilities.sandbox_permissions
    chosen_sandbox = sandbox if sandbox is not None else role_config.sandbox if sandboxed else None
    chosen_approval = (
        approval_policy
        if approval_policy is not None
        else role_config.approval_policy
        if sandboxed
        else None
    )
    # A sandbox or a policy chosen for this spawn wins over the role's setting.
    chosen_reviewer = (
        role_config.approve_for_me
        if sandboxed and sandbox is None and approval_policy is None and native_mode is None
        else None
    )
    try:
        flags = adapter.fleet_args(
            role,
            label,
            native_mode,
            sandbox=chosen_sandbox,
            approval=chosen_approval,
            approve_for_me=chosen_reviewer,
            native=native,
        )
    except ValueError as exc:
        raise FleetError(str(exc)) from exc
    chosen = (native_mode, chosen_sandbox, chosen_approval, chosen_reviewer)
    if notes is not None and all(value is None for value in chosen):
        # Nothing native chose: the agent ran the role's built-in intent, which a receipt
        # names as it is run (``stepped_down``: ``auto`` as ``ask`` without a reviewer).
        built_in = PermissionIntent.for_role(role)
        notes.extend(stepped_down(adapter.capabilities, adapter.label, built_in)[1])
    return flags, native_mode


def _permission_intent(
    role: str,
    role_config: FleetRoleSettings,
    permission_mode: str | None,
    *,
    native_given: bool,
) -> PermissionIntent | None:
    """The permission a spawn maps per agent (``PermissionIntent``), or ``None`` for the
    agent's own path.

    ``permission_mode`` is this spawn's, or a replay's recorded one: written
    ``WRITE:APPROVALS`` it is the intent, refused beside a native ``--sandbox`` or
    ``--approval`` for the same spawn (one or the other); in an agent's own words it is
    that agent's. Without one, the role's ``permission``, unless this spawn chose a
    native sandbox or policy, which wins over the role as any per-spawn flag does.
    """
    if permission_mode is not None:
        if not PermissionIntent.written(permission_mode):
            return None
        if native_given:
            raise FleetError(
                f"--permission-mode {permission_mode} is mapped to each agent's own flags; "
                "pass it without --sandbox or --approval, or pass those alone"
            )
        try:
            return PermissionIntent.parse(permission_mode)
        except ValueError as exc:
            raise FleetError(str(exc)) from exc
    if native_given or not role_config.permission:
        return None
    try:
        return PermissionIntent.parse(role_config.permission)
    except ValueError as exc:
        raise FleetError(f"fleet.roles.{role}.permission: {exc}") from exc


def _chosen_persona(
    project: ProjectInfo,
    role: str,
    flag: str | None,
    role_config: FleetRoleSettings,
    notes: list[str],
) -> str | None:
    """The spawn's persona — flag > ``[fleet.roles.<role>].persona`` > none — checked
    against the project's personas before any window or worktree exists.

    A name nothing resolves refuses with the known names; one that came from the
    config names the key, so a stale default is found at the first spawn rather
    than after a day of work (§3.7). ``persona-roles`` is advisory: a persona
    written for other roles is a receipt note, never a refusal. A seat
    (``coder2``) counts as its role (``harness.base_role``).
    """
    # ``""`` is a flag too: a replay's "the row had none" (``_persona_for_replay``),
    # which must not fall through to a role default adopted since the spawn.
    name = flag if flag is not None else role_config.persona
    if not name:
        return None
    try:
        found = personas.resolve(name, personas.project_root(project))
    except personas.PersonaError as exc:
        if flag is not None:
            raise FleetError(exc.rule) from exc
        raise FleetError(
            f"[fleet.roles.{role}].persona = {name!r}: {exc.rule} — fix the key or pass --persona"
        ) from exc
    # ``harness.base_role``: a seat of a profiled role counts as it, and a declared role
    # that ends in a digit stays itself, as everywhere else (review of #201, R1-F8).
    if found.roles and role not in found.roles and harness.base_role(role) not in found.roles:
        notes.append(
            f"persona {name} is written for {', '.join(found.roles)}, not {role} — spawned "
            "with it anyway"
        )
    return name


def _launch_binary(
    project: ProjectInfo,
    role: str,
    *,
    binary: str | None,
    spec: LaunchSpec | None,
    family: str | None,
    notes: list[str],
    bin_flag: bool = True,
) -> harness.BinaryResolution:
    """The executable a spawn of ``role`` starts — refused when there is none to start.

    An explicit ``binary`` (``--bin``) wins. Otherwise a replayed ``spec``
    starts the binary it recorded or, once that has left the PATH, today's
    resolution of the same kind of program (:func:`_in_place_of_recorded`);
    and a launch with no spec — a new agent, or the replay of a row spawned
    before v18 — starts the role's as it resolves today (:func:`_binary_for`).

    :func:`spawn` asks this, and so does :func:`_refuse_a_replay_that_cannot_start`
    before a running agent is stopped: two copies of the choice, kept in step by
    hand, had already drifted apart once (review of #169, round 1). ``bin_flag``
    is whether the caller takes ``--bin``; a refusal names it only then.
    """
    if spec is None or binary is not None:
        return _binary_for(role, override=binary, bin_flag=bin_flag)
    resolution = harness.resolve_binary(role, override=spec.binary)
    if shutil.which(resolution.binary) is None:
        # Replayed, and gone since: today's resolution, when it is the same kind of program.
        resolution = _in_place_of_recorded(
            project, role, resolution.binary, family=family, notes=notes
        )
    return resolution


def _binary_for(
    role: str, *, override: str | None = None, bin_flag: bool = True
) -> harness.BinaryResolution:
    """The executable ``role`` launches with — refused when it is not on ``PATH``.

    The refusal names ``--bin`` only when ``bin_flag`` says the caller takes one.
    """
    resolution = harness.resolve_binary(role, override=override)
    if shutil.which(resolution.binary) is None:
        way_out = (
            "install it, pass --bin, or change the role's binding"
            if bin_flag
            else "install it, or change the role's binding"
        )
        raise FleetError(
            f"{resolution.binary!r} is not on your PATH (chosen by: {resolution.source}) — "
            + way_out
        )
    return resolution


def _in_place_of_recorded(
    project: ProjectInfo, role: str, recorded: str, *, family: str | None, notes: list[str]
) -> harness.BinaryResolution:
    """What a replay starts when ``recorded``, the binary its spec holds, has left the PATH.

    Refusing outright would strand the agent — `fleet restart`, `fleet switch`
    and Restart take no `--bin`, and `--fresh` replays the spec too — so the
    role is resolved as a spawn resolves it today (per-role env, binding,
    default), and the receipt names both. But only onto the same KIND of
    program: the spec's arguments, and a resume's `--resume <transcript>`, were
    written for the recorded one, and handed to another they run "the WRONG
    AGENT under the right role name" (`harness.resolve_binary`) — aider's
    `-c a.yml` is Claude Code's `--continue` and a prompt, and codex's config
    override. Two programs that are both not Claude Code are not thereby the
    same one — aider's arguments are not codex's — so the same kind is known
    two ways only. By name: the same basename somewhere else (`/opt/old/aider`,
    then `aider`). Or as the same agent: the recorded binary's by its name
    (`agent_adapters.adapter_for_binary`) or by ``family`` — the board session its
    own hook joined the row to — and today's by its name. So a removed
    `claude2` parallel install its hook joined comes back on `claude`, resumed or
    `--fresh`, a `codex2` on `codex` and never on `claude`, and one with nothing
    to show what it was is refused.

    Every refusal here names a way out that a restart has; `--bin` is not one.
    """
    today = harness.resolve_binary(role)
    gone = f"{recorded!r}, the binary it was launched with, is no longer on your PATH"
    if today.binary == recorded:
        raise FleetError(
            f"{gone}, and the role still resolves to it (chosen by: {today.source}) — "
            "install it, or change the role's binding"
        )
    same_name = Path(today.binary).name == Path(recorded).name
    was = family or getattr(adapter_for_binary(recorded), "id", None)
    now = adapter_for_binary(today.binary)
    if not same_name and not (was is not None and now is not None and now.id == was):
        raise FleetError(
            f"{gone}, and the role resolves to {today.binary!r} today (chosen by: "
            f"{today.source}), not known to be the same kind of program: the recorded "
            f"arguments are {recorded!r}'s — put {recorded!r} back on your PATH, or start "
            "a new agent under today's config with "
            f"`{project_service.fleet_command('spawn', project, role)}`"
        )
    if shutil.which(today.binary) is None:
        raise FleetError(
            f"{gone}, and neither is {today.binary!r}, which the role resolves to today "
            f"(chosen by: {today.source}) — install one of them, or change the role's binding"
        )
    notes.append(f"{gone} — starting {today.binary!r} (chosen by: {today.source}) instead")
    return today


def _replayed_args(role: str, spec: LaunchSpec, config: FleetSettings) -> list[str]:
    """The arguments a replay of ``spec`` launches with: the recorded ones, as recorded.

    Less one list nobody chose. 0.6.0 and 0.7.0 launched every reviewer with their
    built-in ``--restricted``, which removes Bash, and its spec recorded it, so after the
    upgrade ``fleet restart``, ``fleet switch`` and asq's Restart brought each one back
    with no shell, every time: changing the config file's default reached fresh spawns
    alone (review of #257). Where it leads the list, as it does before the caller's own
    arguments (round 19), and unless today's ``config`` leads with it too, which says it
    was chosen (round 20). A spec recorded since then (``recorded_by``) is replayed as it
    is, so a ``--restricted`` chosen in today's config survives a restart.
    """
    args = list(spec.extra_args)
    if spec.recorded_by is None and role == "reviewer":
        return replacing_the_former_reviewer_default_first(
            args, role_settings(role, config).extra_args
        )
    return args


def _without_session_choice(
    binary: str, args: Sequence[str], *, family: str | None = None
) -> list[str]:
    """``args`` less the flags that pick WHICH session a launch runs — for the launch spec.

    ``--session-id <id>``, ``--resume``/``-r [<id>]``, ``--continue``/``-c`` and
    ``--fork-session`` (``explainability.without_session_choice``, beside the
    planner that reads them) are per launch, like the restart's own
    ``--resume`` (#144). Replayed from the spec they sat beside it — Claude
    Code refuses ``--session-id`` with ``--resume`` unless it forks — or, on
    ``--fresh``, started the replacement on the old session id.

    These are the flags of an agent whose session AISquare names at launch
    (``capabilities.assigns_session_id``), its own (``capabilities.session_flags``: Claude
    Code's above), so they go only where they are known to be: another program's ``-c`` is
    its own — aider's config file, codex's config override — and is replayed as given. Known
    three ways. By name (``agent_adapters.adapter_for_binary``, as the role's
    ``default_args`` and ``--session-id`` pinning know Claude Code). By ``family``: the
    board session its hook joined, so a ``--fresh`` restart of a ``claude2`` spawned with
    ``-c`` does not continue the old conversation under the hand-off prompt. And by the
    arguments NAMING a session (``--session-id <id>``, ``--resume <id>``): the identity
    planner reads those whatever the binary is called and joins the row to that session, so
    a restart resumes it with its own ``--resume`` — the ``claude2`` parallel install
    (``AISQUARE_BIN_CODER=claude2``) spawned on a session its caller chose.
    """
    kind = recorded_adapter(family) if family else adapter_for_binary(binary)
    if kind is not None and kind.capabilities.assigns_session_id:
        return explainability_service.without_session_choice(args, kind.capabilities.session_flags)
    # An id the planner READ from the arguments, not one it minted — it mints
    # only for an agent it knows by name, answered above.
    planned = explainability_service.plan_session_identity(binary, args)
    if planned.session_id is not None and not planned.inject_args:
        flags = explainability_service.session_flags_of(binary)
        return explainability_service.without_session_choice(args, flags)
    return list(args)


def _supersede(
    placements: Sequence[Placement],
    views: Mapping[_Address, _Observed | None],
    started: Placement,
) -> None:
    """Remove the dead windows of the ended rows ``started`` replaces — those under its label.

    `remain-on-exit` kept them so the last screen stayed readable and the
    listing kept their 💤 rows for it (#138); once a replacement has the name,
    an old window only makes the sidebar show two rows called manager.

    Only once the replacement is up AND recorded: a spawn refused on the way —
    a finished task, a missing binary, an unknown account, tmux itself — must
    leave the 💤 row and its last screen where they were (review of #138: a
    refused restart used to take them with it). Which dead window is a row's
    is :func:`_lingering_windows`' rule, the one the listing shows it by, and
    the pane is asked again at the kill (:func:`_kill_dead`). A window already
    gone, or a server that will not answer, costs nothing here.
    """
    own = (started.pane.address, started.pane.pane_id) if isinstance(started, Placed) else None
    for key, owner in _lingering_windows(placements, views).items():
        if owner.row.label == started.row.label and key != own:
            _kill_dead(owner.pane)
    # One no record places: only a dead window tmux lists in a session a record of the
    # project names (`_recordless_pane`), never where its row points (review r7 of #113).
    live = [placement for placement in [*placements, started] if placement.row.ended_at is None]
    config = started.pane.config if isinstance(started, Placed) else settings()
    for placement in placements:
        row = placement.row
        if (
            isinstance(placement, Unplaced)
            and row.ended_at is not None
            and row.label == started.row.label
            and (found := _recordless_pane(row, live, config))
        ):
            _kill_dead(found[0])


def _session_to_join(
    project_id: str, placements: Sequence[Placement], config: FleetSettings
) -> PlacedSession | None:
    """The session a new window of this project joins: one its spawns made, by tmux's id,
    however long ago its last agent ended (``launch_seal.project_sessions``; review r8 of
    #113, finding 4), one its rows' records name, or the session tmux says holds a pane the
    census placed for an agent spawned before records were kept (finding 1) — on this
    server, and still there (:func:`_project_sessions`). ``None`` when there is none: the
    window makes a session of its own (:func:`_new_session_window`).

    By induction every such session is this project's: a spawn makes a new one or joins
    one the project's spawns made, so no name read from the store — the project's
    codename — ever puts an agent into another project's session (review r7 of #113,
    finding 2). Only on the server today's config names, where this spawn starts its
    window: a record on another socket names a session there."""
    here = _Address(config.tmux_socket)
    for session in _project_sessions(project_id, placements, config):
        if session.address == here and session.present():
            return session
    return None


def _new_session_window(
    srv: TmuxServer,
    session: str,
    *,
    config: FleetSettings | None = None,
    notes: list[str] | None = None,
    **window: Any,
) -> WindowInfo:
    """A window in a NEW session named ``session`` — or, when tmux holds a session by that
    name already, ``session-2``, ``session-3``…: a session found under a name is joined by
    nothing, since the name is read from the store (:func:`_session_to_join`). One that a
    spawn of this project made is joined by its id, which its record holds. Opened through
    :func:`open_window`, as every window at the fleet's socket is (``config``, ``notes``)."""
    for attempt in range(1, _CODENAME_RETRIES + 1):
        candidate = session if attempt == 1 else f"{session}-{attempt}"
        if srv.has_session(candidate):
            continue
        return open_window(srv, candidate, config=config, notes=notes, create=True, **window)
    raise TmuxError(
        f"tmux already holds sessions named {session} to {session}-{_CODENAME_RETRIES}, none "
        "of them one a launch record of this project names — end them, or rename this project"
    )


def _take_over(agent: FleetAgent, previous: str, session_id: str, notes: list[str]) -> FleetAgent:
    """Move a fresh replacement's row, and the claims parked for it, onto its own session id.

    The claims were ``previous``'s — the session the agent ran as before
    :func:`switch` stopped it — and moving them is ``adopt_fleet_agent_session``,
    the one transaction a ``/clear`` hands its claims over in (rule 2 of
    ``services.team``'s fleet-row section): the row and the claims can never
    disagree about who holds the work. The ``doing`` ones take a fresh lease.
    ``previous``'s presence is retired FIRST, releasing nothing: its process
    is dead (``stop`` verified the pane), and a kill fires no ``SessionEnd``,
    so it would otherwise sit on the board as a session mid-switch. First, so
    that a move the store refuses still leaves the state a ``/clear`` leaves
    between its two hooks — an ended id whose claims wait for the start hook —
    and not a live ``switching`` presence nothing retires before the prune
    (review of #205, fifth round). A store that refuses the retirement itself
    costs only the retirement: the move still runs, and a note says the old id
    stays on the board as a live session. Not "until the prune": it heartbeat
    up to the switch, and ``aisquare team prune`` spares a session until it has
    been silent past the board's stale mark (``services.team._STALE_AFTER``),
    so the note names that wait (review of the #205 fold, A2).

    Fail-open, because the window is running and its row is recorded: a store
    that refuses the move leaves the row on ``previous`` with the claims, and
    the agent's own start hook moves them (the pane's process adopts its row);
    the row ending releases them either way (``release_agent_claims`` reads the
    row's session). The note says so.
    """
    lease = _now() + timedelta(minutes=orchestrator.lease_minutes())
    try:
        with store_session() as store:
            parked = store.get_session(previous)
            if parked is not None and parked.ended_at is None:
                # The retirement is the board's courtesy and the move is the record.
                # Each commits on its own, and a store that refuses the first must
                # not cost the second (review of #205, sixth round). Said, though:
                # the old id is otherwise a live session on the board with nothing
                # to explain it (review of #205, seventh round).
                try:
                    store.end_session(previous, release_claims=False)
                except sqlite3.Error as exc:
                    stale = int(_team()._STALE_AFTER.total_seconds() // 60)
                    notes.append(
                        f"the previous session {_team().short_id(previous)} was not marked ended "
                        f"({type(exc).__name__}: {exc}) — it shows on the board as a live "
                        f"session, marked (stale) once it has been silent for {stale} minutes; "
                        "from then on `aisquare team prune` retires it"
                    )
            store.adopt_fleet_agent_session(agent.id, previous, session_id, lease)
            current = store.get_fleet_agent(agent.id)
    except Exception as exc:  # the row and the window stand; the start hook is the second door
        notes.append(
            "the previous session's claims were not moved to this one "
            f"({type(exc).__name__}: {exc}) — the agent's start hook takes them over, "
            "and they go back to the pool if its row ends first"
        )
        return agent
    return current if current is not None else agent


def _live_rows_here(
    store: ContextStore,
    srv: TmuxServer,
    config: FleetSettings,
    reads: tmux_core.SocketReads | None = None,
) -> Iterator[tuple[FleetAgent, SocketAnswer | None]]:
    """Every live row recorded at this shell's socket path, in every project, with what the
    one rule says of its agent (:func:`~aisquare.core.tmux.server_may_run`): the rows a
    server started at that path would answer for.

    The server is shared by every project, so every project's rows are asked, forgotten
    ones' included, in one query (``live_fleet_agents``; a query per project before round 4
    of #259); a row asked at another path (:func:`_address`) is another server's. The one
    walk :func:`_refuse_a_freed_socket` and :func:`_forget_an_exited_server` take, so the
    two cannot come to count different rows (review of #259, round 3).

    Every such row is asked at this shell's path, where :func:`_address` found the one it
    recorded, so ``reads``, the caller's, which read that path already
    (:meth:`~aisquare.core.tmux.SocketReads.heard_listener`), answers for all of them. A
    ``SocketReads`` of its own here, keyed by each row's spelling of the path, connected to
    the socket the caller had just read (review of #259, round 7, 6/9).
    """
    here = _Address(config.tmux_socket)
    reads = reads or tmux_core.SocketReads()  # one socket: asked once, not per row
    for agent in store.live_fleet_agents():
        if _address(agent.tmux_socket, agent.socket_path, config) != here:
            continue
        answer = tmux_core.server_may_run(
            srv,
            None,  # this shell's path: the row's, as `_address` says
            agent.boot_id,
            agent.created_at,
            agent.server_started_at,
            reads=reads,
        )
        yield agent, answer


@dataclass(frozen=True)
class _RowProjects:
    """The projects of live rows from any project, as a message about those rows names them
    (:func:`_row_projects`)."""

    named: str
    """Each row as ``<label> (<its project's name>)``, in row order, comma-separated."""
    reached: dict[str, ProjectInfo]
    """The rows' projects a fleet command reaches (a registration, captured or not), by id."""
    forgotten: list[str]
    """For each forgotten project of the rows, in row order: ``<name> is forgotten: <onboard>
    adds it back first``, which the commands for its rows wait on."""
    unreached: str | None
    """For those rows, that no fleet command reaches them; ``None`` when there are none."""


def _row_projects(store: ContextStore, rows: Sequence[FleetAgent]) -> _RowProjects:
    """What a message about ``rows``, live rows from any project, says of their projects.

    :func:`_refuse_a_freed_socket` and :func:`_exited_note` each read the projects and wrote
    the forgotten sentence, and the copies had drifted: "adds it back first" against "for
    both", one sorted by id, the other in row order (review of #259, round 22). One reading
    keeps the two messages in step.

    A live row can be in a project with no record at all (``live_fleet_agents`` reads every
    row), and every fleet command finds its rows through a project, so none reaches it.
    Such rows are said to be out of reach: with no other way to give, the refusal ended
    mid-sentence, "if it is not, " (round 22)."""
    known = {each.id: each for each in store.list_projects(include_forgotten=True)}
    live = {each.id for each in store.list_projects(all=True)}
    names = {each.id: _name(each) for each in known.values()}
    theirs = list(dict.fromkeys(row.project_id for row in rows))
    orphans = [row for row in rows if row.project_id not in known]
    unreached = None
    if orphans:
        one = len(orphans) == 1
        out_of_reach = ", ".join(f"{row.label} ({row.project_id})" for row in orphans)
        unreached = (
            f"no fleet command reaches {out_of_reach}: "
            f"{'its project is' if one else 'their projects are'} not one this machine has a "
            "record of"
        )
    return _RowProjects(
        named=", ".join(
            f"{row.label} ({names.get(row.project_id, row.project_id)})" for row in rows
        ),
        reached={each: known[each] for each in theirs if each in live},
        forgotten=[
            f"{names[each]} is forgotten: {project_service.onboard_command(known[each].root)} "
            "adds it back first"
            for each in theirs
            if each in known and each not in live
        ],
        unreached=unreached,
    )


def _refuse_a_freed_socket(
    srv: TmuxServer,
    config: FleetSettings,
    answer: SocketAnswer | None,
    reads: tmux_core.SocketReads | None = None,
) -> None:
    """Refuse to start a server at a socket path a running server lost (follow-up of #257).

    ``tmux -L`` finds no file at the path and starts a server there. A live row whose
    server said at spawn that it listened at that path, in this boot, and whose socket
    file is gone (:func:`~aisquare.core.tmux.server_may_run`: ``"gone"``) is an agent that
    may run on a server no tmux client reaches: systemd-tmpfiles or logind took the file
    from under it, which the server outlives. A spawn then started a second server at the
    freed path, which answered for the row from then on, and the agent kept running
    where nothing could reach it again. The server is shared by every project, so every
    project's live rows are asked, not only this one's (:func:`_live_rows_here`).

    Asked only when this shell's path holds no file: a server listening there is the one
    the spawn joins, and a stale file is a server that exited, which tmux replaces. That is
    ``answer``, what the path said (:meth:`~aisquare.core.tmux.TmuxServer.listener`), read
    by the caller: read here again, a path with no file cost two reads a check (review of
    #259, round 6, 7/10). It answers for the rows recorded there too (``reads``).
    """
    if answer != "gone":
        return
    try:
        here = srv.socket_path()
    except TmuxUnavailable:
        return  # no POSIX uid: no tmux server can exist here
    reads = reads or tmux_core.SocketReads()
    reads.heard_listener(srv, None, answer)
    with store_session() as store:
        rows = _live_rows_here(store, srv, config, reads)
        gone = [agent for agent, answer in rows if answer == "gone"]
        if not gone:
            return
        projects = _row_projects(store, gone)
    one = len(gone) == 1
    # Each project's own shutdown: the rows are any project's, and a bare one is the active
    # project's (review of #259, round 21). A forgotten project's is reached once it is back.
    forgets = [_forget(each) for each in projects.reached.values()]
    ways = [f"{' and '.join(forgets)} {'ends its row' if one else 'end their rows'} on your word"]
    ways = ways if forgets else []
    ways += projects.forgotten
    if projects.unreached is not None:
        # The rule that holds a spawn here reads a row from another boot as not running.
        ways.append(f"{projects.unreached}, so a spawn here is refused until the machine restarts")
    raise FleetError(
        f"not started: the tmux server that {projects.named} {'runs' if one else 'run'} "
        f"on lost its socket file {here}, so no tmux client reaches it, and it may still be "
        f"running; a spawn now would start a second server at that path and leave "
        f"{'that agent' if one else 'those agents'} where nothing can reach "
        f"{'it' if one else 'them'} again. If it is running, send that server SIGUSR1, which "
        f"makes tmux create its socket again, and spawn again; if it is not, {'; '.join(ways)}"
    )


def _forget_an_exited_server(
    srv: TmuxServer, config: FleetSettings, reads: tmux_core.SocketReads | None = None
) -> list[FleetAgent]:
    """Forget the recorded start of a server that exited at this shell's socket path, before a
    spawn starts the next server there, so that its rows read lost under the new one rather
    than held as its; the rows it changed.

    A socket file with nothing listening is a server that exited (tmux leaves the file:
    :func:`~aisquare.core.tmux.socket_answer`), and the spawn's ``new-session`` starts the
    next server at that path. The agents went with the old one (a hand-run ``kill-server``,
    a crash). Once the next server is up, each of their rows, having recorded its server's
    start, was held as another server's (:func:`_on_another_server`), since from outside a
    killed server looks like one that lost its socket file and runs on. Every such row, in
    every project, was held for good: ``reap`` left it, ``stop`` refused it, and it still
    counted toward ``max_agents_per_project`` and the one-manager rule until ``shutdown
    --forget`` (review of #259, round 2). 0.8.0 read them lost.

    The evidence is there only until the new server takes the path, so it is acted on here:
    each live row recorded at this path (:func:`_live_rows_here`) that the one rule says
    cannot be running (:func:`~aisquare.core.tmux.server_may_run` is ``None``: the stale
    file, or another boot) forgets its server's start, as a row from before 0.8.1 has none.
    It then reads ``lost`` under the next server, as in 0.8.0: ``reap`` records it, ``stop``
    ends it, and Welcome's top-up restarts a coder under its own label, its session resumed.
    Ending it here instead would cost that. Asked per row, and written as a compare-and-set
    (``forget_fleet_agent_server_start``), so a server another process brought up meanwhile
    leaves its rows and any held one alone.

    Asked by :func:`_before_a_server_starts` once it read a stale file at the path, with
    ``reads`` holding that answer.
    """
    forgotten: list[FleetAgent] = []
    with store_session() as store:
        for agent, answer in _live_rows_here(store, srv, config, reads):
            started = agent.server_started_at
            if (
                answer is None
                and started is not None
                and store.forget_fleet_agent_server_start(agent.id, started)
            ):
                forgotten.append(agent)
    return forgotten


def _before_a_server_starts(srv: TmuxServer, config: FleetSettings) -> list[FleetAgent]:
    """What must hold before anything starts a tmux server at the fleet's socket path, asked in
    this one place; the rows whose exited server's start it forgot. ``srv`` is the fleet's
    server for ``config`` (:func:`server`).

    ``new-session`` starts a server where none listens. Over a stale file, a server that
    exited, that is the next server, and the rows of the one that exited forget its start
    first, or the new one holds them for good (:func:`_forget_an_exited_server`). At a path
    with no file, a running server may have lost it, and a server started there strands its
    agents (:func:`_refuse_a_freed_socket`, which raises). Only ``spawn`` asked, and the
    Accounts sign-in window, on the same server, started one past both (review of #259,
    round 3): so every window opened there goes through :func:`open_window`, which asks.

    One read of the socket says which, if either, applies. Each asked it, so a check cost
    two connects, and a spawn checks twice (review of #259, round 5). A server listening
    there, the usual case, is one connect and nothing else.
    """
    try:
        answer = srv.listener(srv.socket_path())
    except TmuxUnavailable:
        return []  # no POSIX uid: no tmux server can exist here
    reads = tmux_core.SocketReads()  # that answer, for every row recorded at the path
    reads.heard_listener(srv, None, answer)
    if answer is None:
        return _forget_an_exited_server(srv, config, reads)
    _refuse_a_freed_socket(srv, config, answer, reads)
    return []


def _exited_note(srv: TmuxServer, exited: Sequence[FleetAgent]) -> str:
    """What a spawn says of the rows :func:`_before_a_server_starts` forgot the start of: they
    read lost from now on, named with their projects, forgotten ones' included.

    The rows are any project's, so the commands it names reach every project: ``reap --all``,
    and a ``restart`` naming each row's project (:func:`project_service.fleet_command`).
    ``aisquare fleet reap`` alone walked the active project's rows, and a ``restart`` with
    no label is a usage error (review of #259, round 21). A forgotten project is reached by
    neither until it is added back, so its rows are told that instead. ``reap --all`` is
    offered for the rows it walks, those in a project it reaches (``projects.reached``): it
    was offered for every row with any record, and said to record a forgotten project's
    rows (round 23)."""
    one = len(exited) == 1
    with store_session() as store:
        projects = _row_projects(store, exited)
    reached = [row for row in exited if row.project_id in projects.reached]
    restarts = [
        f"`{project_service.fleet_command('restart', projects.reached[row.project_id], row.label)}`"
        for row in reached
    ]
    ways = (
        [f"`aisquare fleet reap --all` records {'it' if len(reached) == 1 else 'them'}"]
        if reached
        else []
    )
    if restarts:
        again = "starts it again" if len(restarts) == 1 else "start them again"
        ways.append(f"{' and '.join(restarts)} {again}")
    ways += projects.forgotten
    if projects.unreached is not None:
        ways.append(projects.unreached)
    return (
        f"the tmux server at {srv.socket_path()} had exited, leaving its socket file: "
        f"{projects.named} {'reads' if one else 'read'} lost from now on, as "
        f"{'its' if one else 'their'}"
        f" {'pane' if one else 'panes'} went with it ({'; '.join(ways)})"
    )


def open_window(
    srv: TmuxServer,
    session: str,
    *,
    name: str,
    cwd: Path,
    command: Sequence[str],
    env: Mapping[str, str] | None = None,
    width: int = DEFAULT_WINDOW_WIDTH,
    height: int = DEFAULT_WINDOW_HEIGHT,
    config: FleetSettings | None = None,
    notes: list[str] | None = None,
    create: bool = False,
) -> WindowInfo:
    """A window on the fleet's tmux server ``srv`` (:func:`server`), opened once what must hold
    before a server starts there does (:func:`_before_a_server_starts`): the one way anything
    opens a window at the fleet's socket, a spawn's agent and the Accounts sign-in window
    alike (tests/test_fleet_service.py names every caller). :class:`FleetError` when it may
    not; :class:`~aisquare.core.tmux.TmuxError` when tmux would not
    (:meth:`~aisquare.core.tmux.TmuxServer.spawn_window`). ``config`` is the one ``srv`` was
    made for, the caller's, so the check asks the rows of the socket it probes. ``notes``,
    when given, gets the rows whose exited server's start the check forgot, for the caller
    to say (:func:`_exited_note`). ``create`` opens it only in a session it creates
    (``TmuxServer.spawn_window``), as a spawn that joins no recorded session does
    (:func:`_new_session_window`)."""
    exited = _before_a_server_starts(srv, config or settings())
    if exited and notes is not None:
        notes.append(_exited_note(srv, exited))
    return srv.spawn_window(
        session,
        name=name,
        cwd=cwd,
        command=command,
        env=env,
        width=width,
        height=height,
        create=create,
    )


def _refuse_occupied_worktree(
    project: ProjectInfo, worktree_dir: str, label: str, occupied: Sequence[Path] = ()
) -> None:
    """Refuse the tree a LIVE agent is working in — before anything touches it.

    ``next_label`` keeps live labels apart, so the only way to arrive here is a
    parallel spawn that picked this label from a snapshot taken a moment
    earlier. The tree at ``<worktree_dir>/<label>`` is then the winner's, and
    neither starting a second agent in it nor (worse) checking another branch
    out from under the first one is acceptable.

    The winner's row can still appear AFTER this read, and the two halves of
    that race need two different guards: :func:`_relabel` refuses the second
    ROW, which is enough for a tree nothing has touched, but a checkout it
    cannot undo has by then already moved the first agent's branch. So
    :func:`_reuse_worktree` asks this function again immediately before its
    ``git checkout`` (``refuse_if_taken``).
    """
    path = (project.root / worktree_dir / label).resolve()
    with store_session() as store:
        live = store.fleet_agents(project.id, live_only=True)
    holder = next((a for a in live if a.worktree and a.cwd.resolve() == path), None)
    if holder is None and any(_same_dir(tree, path) for tree in occupied):
        # The tree of an agent whose row is ended in the store while it runs, as its record
        # places it: a second agent in it, or a checkout under it (review r9 of #113, 5).
        raise FleetError(
            f"{path} is the worktree of an agent that still runs (its row reads ended in the "
            f"store) — spawn under another label, or stop that agent first"
        )
    if holder is not None:
        raise FleetError(
            f"{path} is the live worktree of {holder.label} ({holder.id}) — a parallel spawn "
            f"took the label {label!r} while this one was starting; spawn again"
        )


def _record(
    agent: FleetAgent,
    project: ProjectInfo,
    srv: TmuxServer,
    *,
    wanted: str | None,
    notes: list[str],
    cap: int,
    onboard: bool,
    codename: str,
    replaces: str | None = None,
) -> FleetAgent:
    """Write the row; on a live-label collision re-pick the label and retry.

    The window already exists by now, so what can still be wrong is the label
    and the count: a parallel spawn may have taken either between ``spawn``'s
    checks and this write. The partial unique index reports the label with
    ``IntegrityError``; the suffixed label is recorded and the receipt says
    which. The cap has no index behind it — an index cannot count rows — so it
    is settled in :func:`_verify_cap`, after the insert.

    Whatever goes wrong, the window does not stay up unrecorded: a label with
    nothing to fall back on, a store that refuses (``context.db`` locked past
    its busy timeout, or damaged), the cap — every one of them kills the window
    and raises :class:`FleetError`, because a live agent no row knows about is
    one no ``fleet ls`` shows and no ``fleet stop`` can address.

    The spawn's deliberate add (#139) is made in the same store session, before
    the insert, so a store that refuses it refuses the row too and the window
    goes with it: onboarded after the insert, the refusal would leave a live
    row on a killed window. ``onboard`` is :func:`spawn`'s, and so is ``codename``, the
    one its session and branch were named by, which the add stores on a row that has
    none (``[projects] capture`` off: nothing was written before the window).
    """
    try:
        return _write_row(
            agent,
            project,
            wanted=wanted,
            notes=notes,
            cap=cap,
            onboard=onboard,
            codename=codename,
            replaces=replaces,
        )
    except FleetError:
        _kill_unrecorded(srv, agent.pane_id)
        raise
    except Exception as exc:  # a locked or damaged store — the duty is the same
        _kill_unrecorded(srv, agent.pane_id)
        raise FleetError(
            f"could not record the agent ({exc}) — its tmux window was killed rather than "
            "left running unrecorded"
        ) from exc


def _kill_unrecorded(srv: TmuxServer, pane_id: str) -> None:
    """Kill a window whose row could not be written. Best effort: it may be gone."""
    with contextlib.suppress(TmuxError):
        srv.kill_window(pane_id)


def _write_row(
    agent: FleetAgent,
    project: ProjectInfo,
    *,
    wanted: str | None,
    notes: list[str],
    cap: int,
    onboard: bool,
    codename: str,
    replaces: str | None = None,
) -> FleetAgent:
    """The store half of :func:`_record`: onboard, insert, relabelling past a live collision.
    ``replaces`` is :func:`spawn`'s: recorded stopped meanwhile, nothing is inserted."""
    with store_session() as store:
        if onboard:
            _add_named(store, project, codename)
        for _ in range(_LABEL_RETRIES):
            try:
                written = (
                    store.upsert_fleet_agent(agent)
                    if replaces is None
                    else store.upsert_fleet_agent_unless_stopped(agent, replaces)
                )
            except sqlite3.IntegrityError:
                agent = _relabel(agent, project, store, wanted=wanted, notes=notes)
                continue
            if written is None:
                raise NoSuchAgent(
                    f"{agent.label} ({replaces}) was stopped meanwhile — its replacement's "
                    "window was killed"
                )
            stored = written
            _verify_cap(store, stored, cap)
            return stored
    raise FleetError(f"could not record {agent.label!r}: every label tried was taken")


def _add_named(store: ContextStore, project: ProjectInfo, codename: str) -> None:
    """A spawn's deliberate add, with ``codename`` — the one its window's session and its
    branch were named by — stored on a row that has none, in the same statement.

    Refused (:class:`FleetError`, and :func:`_record` kills the window) when another
    project took that codename while the agent was starting, or another aisquare gave this
    project another one meanwhile: either way the window sits in a session that is not this
    project's, where ``fleet attach`` would never find it."""
    try:
        added = store.onboard_project(project, codename=codename)
    except sqlite3.IntegrityError as exc:
        raise FleetError(
            f"another project took the codename {codename} while this agent was starting — "
            "spawn again"
        ) from exc
    if added.codename != codename:
        raise FleetError(
            f"{_name(project)} was named {added.codename} by another aisquare while this agent "
            f"was starting in session {session_name(codename)} — spawn again"
        )


def _relabel(
    agent: FleetAgent,
    project: ProjectInfo,
    store: ContextStore,
    *,
    wanted: str | None,
    notes: list[str],
) -> FleetAgent:
    """The same agent under a free label — or a refusal when it cannot be renamed.

    A worktree agent cannot be: its cwd is ``<worktree_dir>/<label>``, chosen
    for the label this spawn LOST, and the agent that won it is being started
    in that very tree. Recording this one under a suffixed label would leave
    two live agents editing one checkout on one branch — the thing worktrees
    exist to prevent — with only the row's label to say otherwise. So the
    spawn is refused instead; the window is killed by the caller.
    """
    if agent.role == "manager":
        raise FleetError(f"{_name(project)} already has a manager — one per project")
    if experimental.captain_role(agent.role):
        raise FleetError("the home already has a captain — one per home")
    if agent.worktree:
        raise FleetError(
            f"label {agent.label!r} was taken while this agent was starting, and "
            f"{agent.cwd} is that label's worktree — the agent that won the label works "
            "there, so this one is refused rather than made to share a checkout; spawn again"
        )
    relabel = next_label(project, agent.role, wanted=wanted, task_id=agent.task_id, store=store)
    notes.append(
        f"label {agent.label!r} was taken while starting — recorded as "
        f"{relabel!r} (the tmux window keeps its name)"
    )
    return agent.model_copy(update={"label": relabel})


def _verify_cap(store: ContextStore, stored: FleetAgent, cap: int) -> None:
    """Back the row out if it is past ``max_agents_per_project``.

    ``spawn`` checks the cap before it starts anything, but that read and this
    write are different transactions with a worktree and a tmux window between
    them: N parallel spawns at ``cap - 1`` all pass the check. No index can
    express "at most N live rows", so the count is settled here, from the row's
    own place in the live list — ordered by ``created_at, id``, a total order
    every racer agrees on — so exactly the surplus backs out, and never both
    sides of a race. The row is ended rather than deleted (the store has no
    delete): its label is freed and no live listing shows it.
    """
    live = store.fleet_agents(stored.project_id, live_only=True)
    if len(live) <= cap or all(agent.id != stored.id for agent in live[cap:]):
        return
    store.end_fleet_agent(stored.id, ended_by="stopped")  # backed out on purpose
    raise FleetError(
        f"that would be {len(live)} live agents (max_agents_per_project = {cap}) — another "
        "spawn took the last slot while this one was starting; stop one, or raise the "
        "limit in [fleet]"
    )


def _type_prompt(
    placement: Placement,
    prompt: str,
    notes: list[str],
    *,
    project: ProjectInfo,
    session_id: str | None,
    launched: datetime,
    folder: Path | None = None,
    done: list[str] | None = None,
) -> Typed:
    """Wait (bounded) for the agent to come up, then paste the prompt and press Enter.

    Returns whether it was typed (:data:`Typed`); when it was not, a note says why.

    An agent whose ``SessionStart`` fires at rest at its prompt
    (``capabilities.starts_at_its_prompt``: Claude Code) is up when that start has
    come, since ``launched``, for ``session_id``: :func:`_type_at_its_start`. Its
    foreground process is up long before, at its folder-trust dialog, which an Enter
    answers ``No, exit``. Only where the board can see that start: it is on, and the
    launch names the session (``--continue`` does not, nor a wrapper the board does not
    join), or the wait for it never ends (review r4 of #113, finding 4).

    Otherwise ready means the pane's foreground process is no longer our launcher (or it
    has produced scrollback), and no dialog of the agent's before its start is on its
    screen (``AgentScreen.start_dialog_mark``). Past :data:`PROMPT_TIMEOUT` a SINGLE-LINE prompt
    is typed anyway and the receipt says so — a slow start must not turn into a
    hang, and one line sits harmlessly in the pty's input buffer until the
    agent reads it.

    A multi-line prompt does not: ``paste-buffer -p`` brackets a paste only
    "if the application has requested bracketed paste mode" (tmux 3.7c's own
    man page), and past the timeout the foreground is still the launcher, which
    has requested nothing — so tmux replaces every LF with a CR and the agent
    reads N submitted messages instead of one. That is not a slow start's cost
    to pay silently, so it is not typed; the note says what to do instead.

    Nor into Claude Code's folder-trust question (:func:`_past_trust_prompt`), whose
    highlighted answer is "No, exit": the prompt's Enter ended the agent there. Asked on
    the timeout's path too, with one look: an agent that came up at the question just past
    :data:`PROMPT_TIMEOUT` was typed into, and the receipt said ``yes`` for an agent the
    Enter had ended (review of #259, round 13). A question drawn after that look still
    reads what was typed; no look can close that.

    An agent whose screen reads its own box ready (:func:`_input_box_of`: Claude Code's
    input box) is typed into once that box is ready (:func:`_at_input_box`), not once its
    process runs, and read back after (:func:`_submitted`) (#32, L3). Until its
    prompt is mounted, Claude Code reads the keyboard with an early-input buffer that keeps
    the text and turns the Enter into a newline, then trims it: a prompt typed then sat in
    the input box, unsent. The same wait presses Enter past the first-run Security notes
    screen, which ate the prompt (#32, L2; the owner's D1 at board seq 671). A key pressed
    so is said in ``done``, not ``notes``: it is what made the prompt go in, not why it did
    not.

    Never raises: it runs after the row is recorded, and ``restart`` and
    ``switch`` read a raise out of ``spawn`` as "no replacement started" — they
    gave the claims parked for a replacement that was up back to the pool,
    announced its exit and woke the manager for a second worker (review of
    #203, final round, FLEET-4). So a poll tmux will not answer (a wedged
    server's timeout, an OS refusal) is a note, as a refused paste already was.

    The paste and its Enter say how they ended as a tell's do (:class:`PaneWrite`): one
    tmux did not answer in time may still arrive, and an Enter that did not go through
    leaves the prompt at the agent's, unsent (:func:`pasted_unsent`). Both were "could not
    type the prompt", and the prompt sent again with ``fleet tell`` went in beside the first
    (review of #259, round 10, 8/9). And the receipt says so too, ``unsent`` or ``maybe``: it
    said ``False``, "NOT typed", and the board's line and the page told the human to send
    it (round 11, 2/13). The commands a note gives name the agent's project and label
    (:func:`_send_it_later`); ``folder`` is the one it starts in, which a trust note names.

    Typed into the pane its launch record places (:func:`resolve_place`), by its agent's
    rules; a window whose record cannot be read back is typed nothing, as every row no
    record places.
    """
    if not isinstance(placement, Placed):
        notes.append(
            f"the prompt was NOT typed: {placement.why}. Send it once the agent is up: "
            f"{_send_it_later(project, placement.row.label)}"
        )
        return "no"
    agent, pane = placement.agent, placement.pane
    to = (project, agent.label)
    adapter = recorded_adapter(agent.agent)
    box = _input_box_of(adapter)
    if (
        adapter is not None
        and adapter.capabilities.starts_at_its_prompt
        and orchestrator.team_enabled()
        and session_id is not None
    ):
        return _type_at_its_start(
            pane, agent, adapter, prompt, notes, project, session_id, launched, folder, done
        )
    deadline = _monotonic() + PROMPT_TIMEOUT
    ready = False
    while True:
        try:
            facts = pane.facts()
        except TmuxError as exc:
            notes.append(
                f"tmux could not be asked whether the agent is up ({exc}) — the prompt was "
                f"NOT typed. Send it once the agent is up: {_send_it_later(*to)}"
            )
            return "no"
        if facts is None or facts.dead:
            notes.append("the agent exited before the prompt could be typed")
            return "no"
        if _agent_running(facts.current_command) or facts.history_size > 0:
            ready = True
            break
        if _monotonic() >= deadline:
            break
        _sleep(_POLL_INTERVAL)
    if ready:
        _sleep(_PROMPT_SETTLE)
        if _at_a_start_dialog(pane, adapter):
            # Up, and as quiet as at its prompt: the dialog's answer is the human's.
            while _at_a_start_dialog(pane, adapter):
                if _monotonic() >= deadline:
                    if _past_trust_prompt(pane, notes, folder=folder, to=to, wait=False):
                        notes.append(
                            f"the agent was still at a dialog of its own after "
                            f"{PROMPT_TIMEOUT:.0f} s, where an Enter would answer it for you "
                            "— the prompt was NOT typed. Answer it in its pane "
                            f"(`{project_service.fleet_command('attach', project)}`), then "
                            f"send the prompt: `{_tell(*to)} …`"
                        )
                    return "no"
                _sleep(_POLL_INTERVAL)
            _sleep(_PROMPT_SETTLE)
        if not _past_trust_prompt(pane, notes, folder=folder, to=to):
            return "no"
        if box is not None and not _at_input_box(pane, box, notes, done, folder=folder, to=to):
            return "no"
    elif "\n" in prompt:
        notes.append(
            f"the agent did not come up within {PROMPT_TIMEOUT:.0f} s and the prompt has "
            "several lines — NOT typed: tmux brackets a paste only for an application that "
            "asked for bracketed paste, and the launcher has not, so each line would arrive "
            f"as its own message. Send it once the agent is up: {_send_it_later(*to)}"
        )
        return "no"
    else:
        if not _past_trust_prompt(pane, notes, folder=folder, to=to, wait=False):
            return "no"
        if box is not None and not _at_input_box(
            pane, box, notes, done, folder=folder, to=to, wait=False
        ):
            return "no"
        notes.append(
            f"the agent did not come up within {PROMPT_TIMEOUT:.0f} s — prompt typed anyway"
        )
    return _typed_prompt(pane, prompt, notes, done, to=to if box is not None else None)


def _input_box_of(adapter: AgentAdapter | None) -> AgentScreen | None:
    """The screen a spawn's prompt waits on and reads the prompt back off (:func:`_at_input_box`,
    :func:`_submitted`; #32), or ``None``: an agent whose screen reads its own box ready
    (``AgentScreen.ready_on_screen``), Claude Code's input box. An agent that declares none
    (Codex) is typed into as before, once its process runs."""
    if adapter is None:
        return None
    screen = screen_of(adapter)
    return screen if screen.ready_on_screen is not None else None


def _typed_prompt(
    pane: PlacedPane,
    prompt: str,
    notes: list[str],
    done: list[str] | None = None,
    *,
    to: tuple[ProjectInfo, str] | None = None,
) -> Typed:
    """Type ``prompt`` into ``pane`` (:meth:`PlacedPane.type_then_enter`), and how it ended,
    a note saying so when it is not ``yes``: the last step of :func:`_type_prompt` and
    :func:`_type_at_its_start`. With ``to`` (an agent whose input box is read,
    :func:`_input_box_of`), a prompt that went in is read back (:func:`_submitted`)."""
    told = pane.type_then_enter(
        prompt, "the agent", press="press Enter in its pane", what="the prompt"
    )
    if isinstance(told, PaneWrite):
        if told.typed == "maybe":
            notes.append(told.late("the agent", then=" at its prompt, unsent"))
            return "maybe"
        notes.append(f"could not type the prompt: {told.error}")
        return "no"
    if told.typed != "yes":
        notes.append(f"the prompt: {told.how}")
        return told.typed
    return _submitted(pane, prompt, notes, done, to=to) if to is not None else "yes"


def _at_input_box(
    pane: PlacedPane,
    screen: AgentScreen,
    notes: list[str],
    done: list[str] | None = None,
    *,
    folder: Path | None = None,
    to: tuple[ProjectInfo, str],
    wait: bool = True,
) -> bool:
    """Whether Claude Code is at its input box, ready for the prompt: waited for (bounded).

    On the way, Claude Code's first-run Security notes screen ("Press Enter to continue")
    is pressed past, up to :data:`_SECURITY_PRESSES` times: typed there, the prompt was
    lost and its Enter only moved the screen on (#32, L2). Never twice on one unchanged
    screen within :data:`_SECURITY_REPRESS`, so a second Enter cannot land on what the first
    one brought; past it the first is read as swallowed and pressed again (on a box starved
    for longer than that, the accepted trade-off: the second may reach the trust question).
    The folder-trust question, which can follow it, is :func:`_past_trust_prompt`'s. A
    screen still at the notes, or at another dialog (``screen``'s :meth:`AgentScreen.modal`),
    is not typed into: its Enter would answer the dialog. Any other screen with no box ready
    (:meth:`AgentScreen.ready`) by the deadline is typed into as before, and the note says
    so. A screen tmux will not show is typed as before. Without ``wait``, one look. Read off
    the pane its launch record places (:meth:`PlacedPane.captured`).
    """
    deadline = _monotonic() + (PROMPT_TIMEOUT if wait else 0.0)
    pressed = 0
    pressed_on: list[str] | None = None
    pressed_at = 0.0
    while True:
        try:
            lines = pane.captured()
        except TmuxError:
            return True
        if claude_sharing.shows_trust_prompt(lines):
            if not _past_trust_prompt(pane, notes, folder=folder, to=to, wait=wait):
                return False
            continue
        notes_up = claude_sharing.shows_security_notes(lines)
        # Another Enter only for notes drawn anew: on the very screen the last one was pressed
        # on, it may still be on its way, and a second would land on whatever comes next —
        # the trust question, whose Enter is "No, exit" (delta review of #274, n1). Unless
        # that screen has stood unchanged far past a redraw: the first was swallowed, and the
        # agent waited out PROMPT_TIMEOUT at the notes (delta review 2 of #274, n1).
        swallowed = _monotonic() - pressed_at >= _SECURITY_REPRESS
        if notes_up and pressed < _SECURITY_PRESSES and (lines != pressed_on or swallowed):
            enter = pane_write("Enter", lambda: pane.press("Enter"))
            if enter.typed == "yes":
                pressed += 1
                pressed_on, pressed_at = list(lines), _monotonic()
                _sleep(_PROMPT_SETTLE)
                continue
        if pressed and not notes_up:
            (notes if done is None else done).append(f"pressed Enter past {SECURITY_NOTES}")
            pressed, pressed_on = 0, None  # said once: the notes are not drawn again
        if not notes_up and screen.ready(lines):
            return True
        if _monotonic() >= deadline:
            dialog = SECURITY_NOTES if notes_up else screen.modal(lines)
            if dialog is not None:
                notes.append(
                    f"the agent is at {dialog} — the prompt was NOT typed (its Enter would "
                    "answer it). Answer it in the agent's window "
                    f"(`{project_service.fleet_command('attach', to[0])}`), then send the "
                    f"prompt: `{_tell(*to)} …`"
                )
                return False
            if wait:
                notes.append(
                    f"Claude Code did not draw its input box within {PROMPT_TIMEOUT:.0f} s "
                    "— prompt typed anyway"
                )
            return True
        _sleep(_POLL_INTERVAL)


def _submitted(
    pane: PlacedPane,
    prompt: str,
    notes: list[str],
    done: list[str] | None = None,
    *,
    to: tuple[ProjectInfo, str],
) -> Typed:
    """How a prompt typed into Claude Code with its Enter ended, read back from the pane.

    ``yes`` once it has left the input box (:func:`_in_input_box`). Still there after
    :data:`_SUBMIT_GRACE`, its Enter was lost — read by an early-input buffer as a
    newline (:func:`_type_prompt`) — and Enter is pressed once more: the box holds the
    prompt, so that Enter sends it and answers nothing else. Still there at
    :data:`_SUBMIT_TIMEOUT`, ``unsent``, and the note says where to press Enter. A screen
    tmux will not show is ``yes``, as before this read (#32, L3).
    """
    start = _monotonic()
    pressed = False
    while True:
        _sleep(_POLL_INTERVAL)
        try:
            lines = pane.captured()
        except TmuxError:
            return "yes"
        if not _in_input_box(lines, prompt):
            return "yes"
        waited = _monotonic() - start
        if not pressed and waited >= _SUBMIT_GRACE:
            enter = pane_write("Enter", lambda: pane.press("Enter"))
            if enter.typed != "yes":
                notes.append(
                    "the prompt: typed it at its prompt, but it sat there unsent and "
                    + pasted_unsent(enter, "the agent", press="press Enter in its pane").how
                )
                return "maybe" if enter.typed == "maybe" else "unsent"
            pressed = True
            (notes if done is None else done).append(
                "the prompt sat in Claude Code's input box after its Enter — pressed Enter again"
            )
            continue
        if waited >= _SUBMIT_TIMEOUT:
            notes.append(
                "the prompt: typed it at its prompt, but it is still in the input box, unsent "
                f"— press Enter in its window (`{project_service.fleet_command('attach', to[0])}`)"
            )
            return "unsent"


def _in_input_box(lines: Sequence[str], prompt: str) -> bool:
    """Whether Claude Code's input box, drawn at the foot of ``lines``, holds ``prompt``: its
    first words, or the placeholder a long paste is shown as (``[Pasted text #1 …]``)."""
    rows = claude_screen.tail(lines)
    top = claude_screen.input_box_at(rows)
    if top is None:
        return False
    body: list[str] = []
    for row in rows[top + 1 :]:
        if claude_screen.RULE.match(row):
            break
        body.append(row)
    text = " ".join(" ".join(body).replace(claude_screen.PROMPT_MARK, " ").split())
    words = " ".join(prompt.split())[:_PROMPT_HEAD]
    return bool(text) and (text.startswith(words) or text.startswith("[Pasted text"))


_PROMPT_HEAD = 24
"""How much of a prompt :func:`_in_input_box` matches: enough to be the prompt, short enough
to fit on the box's first row."""

SECURITY_NOTES = "Claude Code's first-run Security notes screen"
"""How the Security notes screen is named in a note and an ``attention`` detail."""


def _tell(project: ProjectInfo, label: str) -> str:
    """``fleet tell`` to the agent ``label`` of ``project``, its text left for the reader."""
    return project_service.fleet_command("tell", project, label)


def _send_it_later(project: ProjectInfo, label: str) -> str:
    """How a prompt left untyped is sent once the agent is up: told, or typed in its window."""
    return f"`{_tell(project, label)} …`, or `{project_service.fleet_command('attach', project)}`"


def _past_trust_prompt(
    pane: PlacedPane,
    notes: list[str],
    *,
    folder: Path | None = None,
    to: tuple[ProjectInfo, str],
    wait: bool = True,
) -> bool:
    """Whether the agent is past Claude Code's folder-trust question, waited for (bounded).

    The question shows when no account trusts the folder (``share_for_launch`` copied
    nothing). A human at the window may answer it within :data:`PROMPT_TIMEOUT`; after
    that the prompt is left untyped, with a note — typed, its Enter picks the highlighted
    "No, exit". A screen tmux will not show is no question: typed as before. Without
    ``wait``, one look: for the prompt typed once that timeout has passed already. Asked
    of the pane its launch record places; another agent's screen never shows the question.
    """
    deadline = _monotonic() + (PROMPT_TIMEOUT if wait else 0.0)
    asked = False
    while True:
        try:
            lines = pane.captured()
        except TmuxError:
            return True
        if not claude_sharing.shows_trust_prompt(lines):
            if asked:
                _sleep(_PROMPT_SETTLE)  # answered: the session draws itself first
            return True
        asked = True
        if _monotonic() >= deadline:
            notes.append(_trust_note(folder, to))
            return False
        _sleep(_POLL_INTERVAL)


def _at_trust_question(pane: PlacedPane) -> bool:
    """Whether ``pane``'s screen shows Claude Code's folder-trust question; ``False`` when
    tmux will not show it."""
    try:
        return claude_sharing.shows_trust_prompt(pane.captured())
    except TmuxError:
        return False


def _trust_note(folder: Path | None, to: tuple[ProjectInfo, str]) -> str:
    """Why a prompt was left untyped at Claude Code's folder-trust question, and the way on."""
    trust = claude_accounts_service.trust_command(folder)
    return (
        "the agent is waiting at Claude Code's folder-trust question, which no "
        "account has said yes to for this folder — the prompt was NOT typed (Enter "
        'there picks "No, exit"). Approve it once in the agent\'s window '
        f"(`{project_service.fleet_command('attach', to[0])}`), or run `{trust}` and "
        f"restart it; then send the prompt: `{_tell(*to)} …`"
    )


def _type_at_its_start(
    pane: PlacedPane,
    agent: FleetAgent,
    adapter: AgentAdapter,
    prompt: str,
    notes: list[str],
    project: ProjectInfo,
    session_id: str | None,
    launched: datetime,
    folder: Path | None = None,
    done: list[str] | None = None,
) -> Typed:
    """:func:`_type_prompt` for an agent whose ``SessionStart`` fires at rest at its prompt.

    Typed once that start has come (:func:`_started_since`), at once: Claude Code took a
    two-line paste sent the instant its ``SessionStart`` fired as one first prompt
    (2.1.300). Its pane is no sign before then: the agent is in the foreground at its
    folder-trust dialog, which fires no hook, is as quiet as a prompt, and takes an
    Enter as its default, ``No, exit`` — every spawn with a prompt in a folder its
    account had not trusted (each account slot keeps its own) exited the agent.

    Past :data:`PROMPT_TIMEOUT` it waits on only while no such dialog is on its screen
    (``AgentScreen.start_dialog_mark``), up to :data:`START_TIMEOUT`: a start that just
    takes long, on a slow machine, is typed into once it comes, as the line restart,
    switch and the unattended hand-over type must be, or the replacement idles at its
    prompt (review r4 of #113, finding 8). A dialog is the human's to answer. With no
    start the prompt is never typed: it is filed as the board note to the agent's label,
    which its first prompt's delta carries (``services.team`` keeps a new session's
    cursor before the notes to its row), and the receipt says how to go on: at Claude Code's
    folder-trust question, which account to trust it on (:func:`_trust_note`). Never raises,
    as :func:`_type_prompt`.

    An agent whose input box is read (:func:`_input_box_of`) is typed into once that
    box is drawn and read back after, as :func:`_type_prompt` says (#32). Claude Code's
    first-run Security notes come before its start, and their only key is Enter: met while
    the start is awaited, they are pressed past (:func:`_at_input_box`), or no start comes.
    """
    to = (project, agent.label)
    box = _input_box_of(adapter)
    start = _monotonic()
    deadline, ceiling = start + PROMPT_TIMEOUT, start + START_TIMEOUT
    while True:
        try:
            facts = pane.facts()
        except TmuxError as exc:
            notes.append(
                f"tmux could not be asked whether the agent is up ({exc}) — the prompt was "
                f"NOT typed. Send it once the agent is up: {_send_it_later(*to)}"
            )
            return "no"
        if facts is None or facts.dead:
            notes.append("the agent exited before the prompt could be typed")
            return "no"
        if _started_since(session_id, launched):
            break
        if box is not None and _at_security_notes(pane):
            if not _at_input_box(pane, box, notes, done, folder=folder, to=to):
                return "no"
            continue
        now = _monotonic()
        at_a_dialog = now >= deadline and _at_a_start_dialog(pane, adapter)
        if at_a_dialog or now >= ceiling:
            try:
                how = _file_note(project, agent.label, prompt, None)
            except Exception as exc:  # never raises: the receipt says it
                how = f"not filed as a board note either ({exc})"
            waited = PROMPT_TIMEOUT if at_a_dialog else START_TIMEOUT
            attach = project_service.fleet_command("attach", project)
            if at_a_dialog and _at_trust_question(pane):
                # Claude Code's folder-trust question, which no account has said yes to for
                # this folder (`share_for_launch` copied nothing): the way past it named.
                trust = claude_accounts_service.trust_command(folder)
                then = (
                    "It is at Claude Code's folder-trust question, where an Enter picks "
                    f'"No, exit": approve it once in its window (`{attach}`), or run '
                    f"`{trust}` and restart it; then"
                )
            elif at_a_dialog:
                then = (
                    "It is at a dialog of its own, which fires no hook and where an Enter "
                    "would answer it for you (`No, exit` at its folder-trust one): answer the "
                    f"dialog in its pane (`{attach}`), then"
                )
            else:
                then = "Once it is at its prompt,"
            notes.append(
                f"its {adapter.label} session did not start within {waited:.0f} s, so the "
                f"prompt was NOT typed — {how}. {then} `{_tell(*to)} …` types its first "
                "prompt, which brings the note"
            )
            return "no"
        _sleep(_POLL_INTERVAL)
    # Claude Code starts once its folder-trust question is answered (measured on 2.1.300),
    # so the question is not on its screen now; one look all the same, as every prompt a
    # spawn types takes one (`_past_trust_prompt`): a start the board took for this launch's
    # cannot type an Enter into "No, exit".
    if not _past_trust_prompt(pane, notes, folder=folder, to=to):
        return "no"
    if box is not None and not _at_input_box(pane, box, notes, done, folder=folder, to=to):
        return "no"
    return _typed_prompt(pane, prompt, notes, done, to=to if box is not None else None)


def _at_security_notes(pane: PlacedPane) -> bool:
    """Whether ``pane``'s screen shows Claude Code's first-run Security notes
    (``claude_sharing.shows_security_notes``); ``False`` when tmux will not show it."""
    try:
        return claude_sharing.shows_security_notes(pane.captured())
    except TmuxError:
        return False


def _started_since(session_id: str | None, launched: datetime) -> bool:
    """Whether ``session_id``'s ``SessionStart`` has come since ``launched``: its board row
    open and seen since. A resumed session's row is its old one, ended by its last
    process's ``SessionEnd`` or last seen before this launch; a read that fails is no."""
    if session_id is None:
        return False
    try:
        with store_session() as store:
            session = store.get_session(session_id)
    except Exception:
        return False
    return session is not None and session.ended_at is None and session.last_seen_at >= launched


def _started_by_its_hook(record: launch_seal.LaunchRecord) -> bool:
    """Whether the agent of THIS launch has started, as its own hook said in the launch
    record (``SessionFacts.started_at``, :func:`~aisquare.core.launch_seal.observe`):
    ``tell``'s word for an agent whose ``SessionStart`` fires at rest at its prompt. A
    resumed replacement's record is its own, so its predecessor's start is not its start
    (review r5 of #113, finding 4); a session that was pruned or cleared since stays
    started (r6, finding 2). Never the board session's ``last_seen_at``: ``end_session``
    stamps it with its own time, so a predecessor ended by ``team prune`` after the launch
    read as this launch's start, and ``tell`` pasted into the replacement's folder-trust
    dialog, whose Enter exits it (r7, finding 5). A row spawned before records were kept
    (:attr:`~aisquare.core.launch_seal.LaunchRecord.legacy`) was running before this
    aisquare read it, and is started."""
    if record.legacy:
        return True
    return record.session is not None and record.session.started_at is not None


def _at_a_start_dialog(pane: PlacedPane, adapter: AgentAdapter | None) -> bool:
    """Whether the screen of ``pane`` shows a dialog its agent puts up before its start,
    where an Enter answers it for the human (``AgentScreen.start_dialog_mark``): the
    dialog's own row, its last line on the screen, starts with the mark (Claude Code's
    ``Enter to confirm · Esc to cancel``). Not the phrase anywhere on the screen: the agent's
    own output shows it as soon as it opens a file or a conversation that does — this
    repository's tests and docs, a ``--continue`` — and every ``tell`` became a note, a
    spawn's prompt held back (review r5 of #113, finding 8). A screen tmux will not give
    shows none; never raises."""
    mark = screen_of(adapter).start_dialog_mark if adapter is not None else None
    if not mark:
        return False
    try:
        screen = pane.screen()
    except TmuxError:
        return False
    last = next((row.strip() for row in reversed(screen) if row.strip()), "")
    return last.startswith(mark)


#: How long after its end an agent whose window is still on the tmux server stays in
#: the live listing as 💤 exited — `remain-on-exit` keeps the last screen readable,
#: and the row beside it is what offers Restart (#138). Older ended rows are history
#: (`--all`), whatever tmux still holds.
RECENTLY_ENDED = timedelta(hours=24)

#: How long a hand-over's ``team.HANDOVER_STATE`` mark keeps the row it is moving out
#: of every reconciler's hands (:func:`_handed_over`). The hand-over ends that row
#: within its stop — the grace (5 s) and a second for the status, then the kill; on a
#: wedged server a few tmux calls at their 30 s timeout each — so a mark older than
#: this is one whose hand-over died on the way, and the dead row it left is ended
#: like any other.
HANDOVER_GRACE = timedelta(minutes=5)


def _handed_over(agent: FleetAgent, session: TeamSession | None, now: datetime) -> bool:
    """Whether ``agent`` is the row a hand-over in flight is stopping — that stop's to end.

    ``switch``, and ``restart`` of a running agent, mark the SESSION
    (``team.HANDOVER_STATE``) before the ``/exit``, and take the mark back as
    soon as that stop returns or raises (a resumed replacement's own
    ``SessionStart`` may clear it first). Keyed on the mark alone, the guard
    also covered rows it was never meant for (review of #163, round 1): a
    resumed replacement shares the session id, so one whose ``claude --resume``
    died before its first hook — the failure ``switch`` names — was skipped by
    every listing and every ``spawn`` for good, and ``fleet spawn manager``
    refused with "already has a manager", the trap #138 removed. So the row
    must also PREDATE the mark: the mark and the old process's ``SessionEnd``
    both bump ``last_seen_at``, and a replacement is recorded only after the
    stop that ended the old row. And the mark must be younger than
    :data:`HANDOVER_GRACE`, which bounds the rest — the old row of a switch
    killed mid-way, and a fresh replacement left on the old id by a claims move
    the store refused after its predecessor fired no ``SessionEnd``.

    The one rule for who may touch such a row: the listing and ``reap`` leave
    it alone, ``restart`` and ``switch`` refuse it, and the hand-over's own stop
    reads a row ended WITHOUT it holding as a listing's record of its ``/exit``
    (review of #163, round 2).
    """
    return (
        session is not None
        and session.state == _team().HANDOVER_STATE
        and agent.created_at < session.last_seen_at
        and now - session.last_seen_at < HANDOVER_GRACE
    )


def list_agents(
    project: ProjectInfo,
    *,
    live_only: bool = True,
    looked_up: dict[str, TeamSession | None] | None = None,
) -> list[FleetAgentStatus]:
    """The project's agents with their DERIVED state (§5.1).

    ``looked_up``, when given, is filled with the sessions read by their ids for the live
    rows the project's own board holds none for (:func:`_with_trust_prompts`), ``None`` for
    an id with no session, for a caller that reads them too: Remote's scan derives those
    rows again with their sessions, and read them a second time (review of #259, round 14).

    Two things changed with #138, both so the store stops lagging the screen:

    - A live row whose pane is DEAD is recorded as ended right here, the way
      ``reap`` records it (exit status, the claims its session still held
      released, ``agent_exited`` on the board, the manager nudged). Death is
      the one fact no row may contradict (see :func:`_derive`), and a row that
      lagged it was the trap: ``fleet spawn manager`` refused because a
      manager everyone could see was dead was still "live", and the way out
      was a hand-run ``reap``. Absence is NOT
      reconciled: a vanished pane stays ``lost`` until ``reap``, because an
      empty answer from a server that did not speak is not evidence.
    - The live listing KEEPS an ended row while its window is still on the
      server (``remain-on-exit``) — a DEAD pane under its id, see
      :func:`_with_lingering_windows` — for :data:`RECENTLY_ENDED`: that is
      the 💤 row the UI shows the last screen behind and hangs **Restart** on.
      ``fleet ls`` shows the same rows, so the two never disagree.
    """
    with store_session() as store:
        agents = store.fleet_agents(project.id, live_only=False)
        sessions = {session.id: session for session in store.team_sessions(project.id)}
        # A live row's session another board keeps (under AISQUARE_TEAM_HUB, every one):
        # read by its id here, in this one store session, for the folder-trust question
        # (`_with_trust_prompts`), and for a caller that derives those rows again with it.
        by_id = {
            agent.session_id: store.get_session(agent.session_id)
            for agent in agents
            if agent.ended_at is None and agent.session_id and agent.session_id not in sessions
        }
    if looked_up is not None:
        looked_up.update(by_id)
    now = _now()
    config = settings()
    # Running is what the panes say, whatever a row's `ended_at` (review r9 of #113, 6).
    running = _running_ids(
        project.id, {agent.id for agent in agents if agent.ended_at is not None}, config
    )
    if live_only:
        cutoff = now - RECENTLY_ENDED
        agents = [
            agent for agent in agents
            if agent.ended_at is None or agent.ended_at >= cutoff or agent.id in running
        ]  # fmt: skip
    # Each row's launch record, read once for this listing (review r6 of #113, finding 11),
    # places it: asq's pane view types the human's keys into the pane a listed row names
    # (`FleetAgentStatus.socket_at`), and a row the sandbox pointed at another pane showed
    # that pane there, and was ended when that one was dead (r5, finding 1). One no record
    # places is asked about nowhere, and reads lost (`_status`).
    records = {agent.id: _read_record(agent.id) for agent in agents}
    placements = _resolve_all(agents, config, records)
    views = _observe_sockets(placements, running)
    ended = {
        row.id: resolve_place(row, records[row.id], config)  # the store's row again (r6, 8)
        for row in _end_dead_rows(placements, views, best_effort=True)
    }
    if ended:
        placements = [ended.get(placement.row.id, placement) for placement in placements]
    if live_only:
        placements = _with_lingering_windows(placements, views, running)
    reached = {placement.row.id for placement in placements if _view(placement, views) is not None}
    sessions = _record_failed_turns(
        [placement.agent for placement in placements], sessions, reached, records
    )
    statuses = [
        _status(
            placement,
            sessions.get(placement.row.session_id) if placement.row.session_id else None,
            views,
            now,
            running,
        )
        for placement in placements
    ]
    return _with_trust_prompts(statuses, placements, by_id)


def _record_failed_turns(
    agents: Sequence[FleetAgent],
    sessions: Mapping[str, TeamSession],
    reached: Collection[str],
    records: Mapping[str, launch_seal.LaunchRecord | str],
) -> dict[str, TeamSession]:
    """``sessions``, each live row's re-read once its transcript says its turn failed with
    no hook (``services.agent_events.failed_turn``), recorded on every read as
    :func:`_end_dead_rows` records a death.

    Codex 0.162.1 fires no ``Stop`` for a turn that fails — on its usage limit or any other
    error — so its row read ``working`` for good: no ``limited``, no manager told, no
    hand-over, and a ``tell`` queued for a turn that would never come (SMOKE3). Only a row
    whose board session says ``working`` is read, of an agent that records failures in
    its transcript (``capabilities.limits_in_transcript``), and only the transcript's last
    bytes. Recorded, the row reads ``limited`` or ``waiting`` (:func:`_derive`) from this
    very read on; one that could not be recorded is left to the next.

    Only by a reader whose tmux answered for the row (``reached``, the rows' ids): a
    listing in a Codex agent's sandbox — a Codex manager's ``fleet ls`` or ``tell`` — is
    refused tmux, and the failure it claimed was the turn's once: its hand-over ran in
    that sandbox and ended "not switched", and nothing retried it (review r5 of #113,
    finding 3). Left unclaimed, the next reader outside a sandbox records it — the app's
    tick, a ``fleet`` command of the human's. ``records`` are the rows' launch records,
    as the caller read them.
    """
    fresh = dict(sessions)
    for agent in agents:
        session = fresh.get(agent.session_id) if agent.session_id else None
        if (
            agent.ended_at is not None
            or agent.id not in reached
            or session is None
            or session.ended_at is not None
            or session.state != "working"
        ):
            continue
        if not agent_events.failed_turn(agent.id, session, records.get(agent.id)):
            continue
        with suppress(sqlite3.Error), store_session() as store:
            fresh[session.id] = store.get_session(session.id) or session
    return fresh


def _end_dead_rows(
    placements: Sequence[Placement],
    views: Mapping[_Address, _Observed | None],
    *,
    best_effort: bool = False,
    nudge: bool = True,
    ended_here: list[FleetAgent] | None = None,
) -> list[FleetAgent]:
    """Record every LIVE row whose observed pane is dead as ended — reap's rule, on every read.

    ``ended_here``, when given, is filled with the rows THIS call ended, apart from those
    another reader had (which it announced): for a caller that owes a held-back wake-up for
    its own alone (:func:`_still_to_restart`).

    Returns the ended rows (fresh from the store). Opens the store only when
    there is something to write, so the ordinary listing stays a read.

    Only a row THIS call ends gets reap's aftermath: its session's claims
    released (a crash fires no ``SessionEnd``, and once a listing has ended the
    row ``reap`` never sees it live again, so nothing else would return them
    before the orphan prune), the ``agent_exited`` event and the manager's
    nudge. Every read reconciles now — the UI's tick, the manager's own ``fleet
    ls``, a spawn, a restart — so two readers routinely see the same death, and
    the one whose write finds the row already ended
    (:meth:`~aisquare.core.store.ContextStore.end_fleet_agent_if_live`, a
    compare-and-set) stays quiet rather than wake the manager twice.

    A row a hand-over is stopping (:func:`_handed_over` — ``switch``, or the
    ``restart`` of a running agent) is left alone: its pane died of the
    hand-over's own ``/exit``, and the hand-over ends the row itself with
    nothing released or announced (``stop(handover=True)``). Ended here in that
    gap — the UI ticks every two seconds, the stop polls a dead pane for its
    status — the parked claims went back to the pool, the manager was woken for
    an agent on its way back, and the switch's stop found its row ended and
    abandoned the replacement. The guard is the moving row's alone, and only
    for :data:`HANDOVER_GRACE`: a replacement that died before its first hook,
    and the old row of a switch killed mid-way, are ended as any dead row.

    ``best_effort`` is a LISTING's: it must not fail for this write, so a store
    locked past its busy timeout leaves the row to the next read — it reads
    ``exited`` from its pane meanwhile (:func:`_derive`). A spawn or a restart
    decides on what this recorded (a dead manager still "live" refuses the new
    one), so there the store's error is the answer, not a refusal it explains.

    ``nudge=False`` is a RESTART's: the death is still announced, but the
    manager is not woken for an agent already on its way back — woken, it read
    "exited" before ``restarted`` was on the board and could restart the label
    itself, onto the replacement (review of #163, round 2).

    Each row's pane is read from ITS server's view (:func:`_view`), the one at the socket
    its spawn recorded: from a shell under another ``TMUX_TMPDIR`` the server ``tmux -L``
    reaches on the same name is another, and a dead ``%N`` there ended a live row under its
    running agent (round 28 of #257). Where its launch record places it: a row no record
    places has no view, and is ended by nothing here.
    """
    dead = [
        (placement.row, pane)
        for placement in placements
        if placement.row.ended_at is None
        and (observed := _view(placement, views)) is not None
        and (pane := _pane_in(placement.agent, observed)) is not None
        and pane.dead
    ]
    if not dead:
        return []
    ended: list[FleetAgent] = []
    mine: list[FleetAgent] = []
    # Locked past its busy timeout, or unopenable (StoreUnopenable is an sqlite3.Error):
    # what was recorded stands, the rest waits for the next read.
    tolerated = (sqlite3.Error,) if best_effort else ()
    now = _now()
    with suppress(*tolerated), store_session() as store:
        for agent, pane in dead:
            session = store.get_session(agent.session_id) if agent.session_id else None
            if _handed_over(agent, session, now):
                continue  # a hand-over is stopping it — see above
            row = store.end_fleet_agent_if_live(
                agent.id, exit_status=pane.dead_status, ended_by="died"
            )
            if row is not None:
                # A listing has no report to carry a refused release; the claims then
                # stay with the ended session until their lease lapses, as
                # `_release_or_say` tells the callers that have one.
                _release_or_say(store, row, why="agent exited")
                _emit_exit(store, row)
                mine.append(row)
            else:  # another reader ended it first, and announced it
                row = store.get_fleet_agent(agent.id)
            if row is not None:
                ended.append(row)
    if ended_here is not None:
        ended_here.extend(mine)
    if nudge:
        for row in mine:
            nudge_manager(row.project_id, reason=f"{row.label} exited", board=_board_of_row(row))
    return ended


def _lingering_windows(
    placements: Sequence[Placement],
    views: Mapping[_Address, _Observed | None],
) -> dict[tuple[_Address, str], Placed]:
    """``(server, pane id)`` → the ENDED row whose window each dead pane still is, as their
    launch records place them.

    "Its window" means a DEAD pane under the row's id on the row's server. The
    id alone is not the window: pane ids are unique only per server lifetime —
    the fleet's server exits with its last window and the next one numbers from
    ``%0`` again (measured live while building #138: three ended rows and the
    running manager all at ``%0``) — so a LIVE pane under an ended row's id is
    another agent's. And one dead pane is the window of at most ONE row: a live
    row naming it when there is one (its death is only not recorded yet), else
    the latest to end — a lingering window keeps its server alive, so any older
    row's pane under the same id went with an earlier server.

    The one rule for everything that acts on a lingering window — the listing
    that shows the 💤 row, ``stop`` that removes the window, ``spawn`` that
    supersedes it — so none of them takes another agent's last screen for a
    row's (review of #138: a reused id let ``stop`` kill another agent's
    window and report success). ``addresses`` as for :func:`_where`.
    """
    owners: dict[tuple[_Address, str], Placed] = {}
    for placement in _placed_only(placements):
        observed = _view(placement, views)
        # No pane, or a held row's (HELD), whose id is another agent's there.
        pane = _pane_in(placement.agent, observed) if observed else None
        if pane is None or not pane.dead:
            continue
        key = (placement.pane.address, placement.pane.pane_id)
        held = owners.get(key)
        row, kept = placement.row, held.row if held is not None else None
        if kept is None or (
            kept.ended_at is not None and (row.ended_at is None or row.ended_at > kept.ended_at)
        ):
            owners[key] = placement
    return {key: owner for key, owner in owners.items() if owner.row.ended_at is not None}


def _with_lingering_windows(
    placements: Sequence[Placement],
    views: Mapping[_Address, _Observed | None],
    running: Collection[str] = (),
) -> list[Placement]:
    """Every live row, plus each ended row whose window still stands (see
    :func:`_lingering_windows`), dead or, ended in the store only, running
    (:func:`_ended_but_running`)."""
    shown = {owner.row.id for owner in _lingering_windows(placements, views).values()}
    return [
        placement for placement in placements
        if placement.row.ended_at is None
        or placement.row.id in shown
        or _ended_but_running(placement, views, running)
    ]  # fmt: skip


def _ended_but_running(
    placement: Placement,
    views: Mapping[_Address, _Observed | None],
    running: Collection[str] = (),
) -> bool:
    """Whether ``placement``'s row is ended in the store while the pane its launch record
    places runs, as its server's view says: running, whatever ``ended_at``, a store column,
    says. One ``UPDATE`` of its own row made a sandboxed agent one that ``shutdown`` left up
    and ``restart`` started a second of (review r8 of #113, findings 2 and 6)."""
    if not isinstance(placement, Placed) or placement.row.ended_at is None:
        return False
    if placement.row.id in running:  # its window runs, as the panes say (`_running_ids`)
        return True
    observed = _view(placement, views)
    pane = _pane_in(placement.agent, observed) if observed else None
    return pane is not None and not pane.dead


def _as_running(placement: Placed) -> Placed:
    """``placement``, whose row is ended in the store while its window runs, as running:
    said on the board once (:func:`_say_ended_but_running`)."""
    _say_ended_but_running(placement.row)
    agent = placement.agent.model_copy(update={"ended_at": None, "exit_status": None})
    return replace(placement, agent=agent)


def _say_ended_but_running(row: FleetAgent) -> None:
    """Say on the board that ``row`` is ended in the store while its window runs, and is
    listed, stopped and shut down as running (:func:`_say_refused`, once per row)."""
    _say_refused(
        row.project_id,
        row,
        "listed, stopped and shut down as running",
        "its row was rewritten in the store: it is ended there while the pane its launch "
        "record places runs",
        "its `ended_at` decides nothing",
    )


def _kill_lingering_window(
    store: ContextStore, project: ProjectInfo, agent: FleetAgent
) -> bool | None:
    """Kill an ENDED row's window if it still stands: ``True`` when it did, ``False`` when tmux
    vouches that no window of the row's stands, ``None`` when it cannot be told.

    Whether it stands is decided as the listing decides it — the project's
    rows observed together, :func:`_lingering_windows` — never by asking tmux
    about the id alone: after the server restarted, a dead pane under that id
    is another agent's last screen. Never raises: a server that will not
    answer costs the tidy-up, and answers ``None``. Where its launch record places it
    (:func:`resolve_place`), the rows beside it too, each record read once: the live rows
    and those ended within :data:`RECENTLY_ENDED`, the ones whose window the listing shows
    (review r7 of #113, finding 10). A row no record places has no window the fleet may
    name.

    ``False`` is the row's own server's word that none stands (its view answered, or the
    pane went before the kill), or no server behind its socket at all. Neither a view that
    could not be read nor a kill tmux refused vouches for anything: either may leave a dead
    window standing (last delta review of #274, n2).
    """
    config = settings()
    cutoff = _now() - RECENTLY_ENDED
    rows = [
        row for row in store.fleet_agents(project.id, live_only=False)
        if row.id == agent.id or row.ended_at is None or row.ended_at >= cutoff
    ]  # fmt: skip
    placements = _resolve_all(rows, config)
    views = _observe_sockets(placements)
    mine = next((placement for placement in placements if placement.row.id == agent.id), None)
    if isinstance(mine, Unplaced):
        # No record places it: a dead window tmux lists in a session a record of the
        # project names, under the pane its row names, is the one it may have (review r7
        # of #113, finding 1; r6, finding 9).
        live = [placement for placement in placements if placement.row.ended_at is None]
        found = _recordless_pane(agent, live, config)
        return False if found is None else _kill_dead(found[0])
    if mine is None:
        return False
    owner = _lingering_windows(placements, views).get((mine.pane.address, mine.pane.pane_id))
    if owner is None or owner.row.id != agent.id:
        if _view(mine, views) is not None:
            return False
        try:
            return False if mine.pane.server().server_absent() else None
        except TmuxError:
            return None
    return _kill_dead(mine.pane)


def _kill_dead(pane: PlacedPane) -> bool | None:
    """Kill the window of ``pane`` only if that pane is DEAD now; never raises.

    Asked again at the kill because what decided it was an earlier look: a
    window gone meanwhile — and with it perhaps the server, whose next pane may
    carry the same id — is left alone, and a live pane is never killed from here.
    ``True`` when it killed it, ``False`` when no pane has that id now, ``None`` when one
    stands that it did not kill: live, or tmux refused.
    """
    try:
        facts = pane.facts()
        if facts is None:
            return False
        if not facts.dead:
            return None
        pane.kill()
    except TmuxError:
        return None
    return True


def status_of(
    agent: FleetAgent, *, record: launch_seal.LaunchRecord | str | None = None
) -> FleetAgentStatus:
    """One agent's derived state: board session first, tmux facts second. The row as its
    launch record places it (:func:`resolve_place`); ``record`` is that record, when the
    caller has read it."""
    with store_session() as store:
        agent = store.get_fleet_agent(agent.id) or agent
        session = store.get_session(agent.session_id) if agent.session_id else None
    config = settings()
    placement = resolve_place(agent, record, config)
    views = _observe_sockets([placement])
    if session is not None and isinstance(placement, Placed):
        reached = () if _view(placement, views) is None else (agent.id,)
        session = _record_failed_turns(
            [placement.agent], {session.id: session}, reached, {agent.id: placement.record}
        )[session.id]
    status = _status(placement, session, views, _now())
    by_id = {agent.session_id: session} if agent.session_id else {}
    return _with_trust_prompts([status], [placement], by_id)[0]


def manager_of(project: ProjectInfo) -> FleetAgent | None:
    """The project's live manager, if any."""
    with store_session() as store:
        found = _by_label(store, project.id, MANAGER_LABEL, live_only=True)
    if found is None:
        return None
    agent, record = found
    # The record's role, not the row's: a manager row set to `coder` was no manager to the
    # UI, which offered to start a second (review r9 of #113, finding 9).
    role = record.role if isinstance(record, launch_seal.LaunchRecord) else agent.role
    return agent if role == "manager" else None


def tell(
    project: ProjectInfo,
    label: str,
    text: str,
    *,
    sender: str | None = None,
    agent_id: str | None = None,
    note: bool = True,
) -> TellResult:
    """Type ``text`` into a WAITING agent; otherwise file it as a board note to it.

    Typing into a working agent would interleave with its turn; into one that
    needs the human, it could answer a permission prompt. So anything but
    ``waiting`` with a live pane goes to the board, addressed to the label,
    where the agent's next delta injection delivers it.

    ``waiting`` alone is not enough. A pane whose foreground is still
    ``aisquare launch`` reads waiting as soon as its last output is older than
    :data:`ACTIVITY_WINDOW` (five seconds — and ``PROMPT_TIMEOUT`` exists
    because a start takes longer than that), and the launcher has requested no
    bracketed paste, so tmux would turn every LF into a CR and the agent would
    read N submitted messages instead of one: the harm :func:`_type_prompt`
    documents and refuses to cause. So the same readiness test decides here —
    the one :func:`nudge_manager` already applies — and a pane that is not the
    agent's gets the board note instead.

    An agent whose ``SessionStart`` fires at rest at its prompt
    (``capabilities.starts_at_its_prompt``: Claude Code) and has not started is not
    typed into: it is at its folder-trust dialog, as quiet as a prompt. That takes the
    board seeing its start — the board on, the row naming its session; for one it
    cannot see (the board off, ``--continue``, a wrapper the board does not join) the
    rule above stands, provided no dialog of the agent's is on its screen
    (``AgentScreen.start_dialog_mark``). Every such ``tell`` had been a note, and with
    the board off a ``FleetError`` (review r4 of #113, finding 3). Started means seen
    since THIS launch, as its own hook wrote it in the launch record
    (:func:`_started_by_its_hook`): a resumed replacement keeps its session's id, whose
    board row is its predecessor's, so a row that merely exists said started while the
    replacement sat at the trust dialog of a slot that had not trusted the folder (review
    r5 of #113, finding 4), and that row's ``last_seen_at`` is moved by whoever ends it
    (r7, finding 5).

    Typed only into the pane its launch record places, by its record's agent's rules
    (:func:`resolve_place`): a row rewritten in the store, or with no record to place it,
    gets the board note, and the reason is said on the board (review r5 of #113,
    finding 1; r6, finding 1).

    An agent whose pane cannot be read for readiness
    (``capabilities.reads_pane_readiness``: Codex) is typed into only at its prompt as
    :func:`_at_its_prompt` decides it: its own hooks freshly say ``waiting``, or, with
    none fresh (none fire before its first prompt), its quiet pane shows its empty
    prompt. When they freshly say ``working``, the message goes through the agent's own
    queue for its next turn (``AgentAdapter.queue_args``: ``codex queue``, which reached
    an interactive session busy with a turn on 0.162.1), and a queue that fails, or an
    agent without one, leaves the board note. The queue is no way in before the first
    prompt: Codex writes the thread's rollout with it, and ``codex queue`` refused the
    thread a fresh TUI had open with "no rollout found for thread id".

    ``agent_id`` pins the row, as for :func:`stop`: the phone's tell means the
    agent it showed, and a replacement that took the label since (a manager's
    restart or switch, in another process) is refused, not typed into.

    And the pane must be the row's own (:func:`_its_agent`): on the server that took a row's
    socket, its pane id is another agent's. A held row read its board's ``waiting`` while
    that was fresh, and pasted into, that agent got the text and an Enter (review of #259,
    round 2).

    A paste tmux refused typed nothing, and the text goes to the board instead. Once the
    paste went through, the text sits at the agent's prompt, and an Enter that did not
    (:func:`pasted_unsent`) leaves it there, said so, and files no copy: the agent read the
    note at its next prompt, and the text left in its input box went out with whatever was
    typed there next (review of #259, round 10, 4/9).

    ``note=False`` types or says why not and files nothing: what becomes of a message that
    was not typed is then the caller's to decide. :func:`attach_persona` asks so, because
    its message is a persona's body, which the board never carries (review of #240,
    finding 7). Not typed, ``how`` is the reason alone.
    """

    def noted(why: str, to: FleetAgent) -> str:
        """``why``, and the board note filed in the message's place, when there is one."""
        if not note:
            return why
        return f"{why} — {_file_note(project, label, text, sender, agent=to)}"

    with store_session() as store:
        # Its record read once (r6, 11).
        row, record = _live_row(store, project, label, agent_id=agent_id)
    placement = resolve_place(row, record)
    why = placement.why if isinstance(placement, Unplaced) else placement.rewritten
    if not isinstance(placement, Placed) or why is not None:
        waits = "it waits on the board for the agent's next prompt"
        _say_refused(project.id, row, "a message told to it is not typed", why or "", waits)
        return TellResult("no", noted(f"not typed: {why}", row))
    agent, pane = placement.agent, placement.pane  # its place and its agent: the record's
    status = status_of(agent, record=placement.record)
    adapter = recorded_adapter(agent.agent)
    started = _started_by_its_hook(placement.record)
    if (
        adapter is not None
        and adapter.capabilities.starts_at_its_prompt
        and orchestrator.team_enabled()
        and agent.session_id is not None
        and not started
    ):
        # Quiet, and not at its prompt: Claude Code fires no hook at its folder-trust
        # dialog, whose default an Enter picks — `No, exit` (2.1.300). Only where the
        # board sees its start: the board on, the row naming its session.
        return TellResult(
            "no",
            noted(
                f"its {adapter.label} session has not started (it fires no hook at its "
                "folder-trust dialog, where an Enter would exit it)",
                agent,
            ),
        )
    if not (adapter is not None and adapter.capabilities.reads_pane_readiness):
        # The agent's own hooks say what the pane cannot. A fresh "working" is a busy
        # agent, which takes the message through its own queue where it has one.
        session = status.session
        if (
            note  # a message that may not reach the board is no turn of the human's either
            and _hooks_fresh(session, _now())
            and session is not None
            and session.state == "working"
            and adapter is not None
        ):
            queued = _queue_message(adapter, agent, session, text, placement.record)
            if queued is True:
                return TellResult("yes", "queued for its next turn (it was working)")
            if isinstance(queued, str):
                return TellResult("no", noted(queued, agent))
        if not _at_its_prompt(pane, agent, session, status.state, _now()):
            gone = _socket_gone(pane)
            if gone is not None:
                return TellResult("no", noted(gone, agent))
            if adapter is None or not screen_of(adapter).reads_empty_prompt:
                return TellResult("no", noted("no confirmed waiting native session", agent))
            if status.state == "waiting" and _hooks_fresh(session, _now()):
                # Its hooks say waiting, its screen does not: a dialog of the agent's
                # own, opened after its turn ended (Codex's `Switch to gpt-6-luna?`).
                return TellResult(
                    "no",
                    noted(
                        f"its hooks say waiting, but its pane is not at {adapter.label}'s "
                        "empty prompt (a dialog of its own is open, where an Enter would "
                        f"answer for you: `{project_service.fleet_command('attach', project)}`)",
                        agent,
                    ),
                )
            return TellResult(
                "no",
                noted(
                    "no confirmed waiting native session and its pane is not at "
                    f"{adapter.label}'s empty prompt",
                    agent,
                ),
            )
    if status.state == "waiting":
        try:
            facts = pane.facts()
        except TmuxError:
            facts = None  # tmux that cannot answer is not a pane to type into either
        if not _its_agent(agent, facts):
            if facts is not None and _outlived(agent, facts.server_started):
                why = (
                    "it reads as waiting, but the tmux server at its socket is not the one it "
                    "was started on, and its pane id there is another agent's"
                )
            else:
                why = (
                    "it reads as waiting, but its pane is not running the agent yet (the "
                    "launcher or a shell is in the foreground)"
                )
            return TellResult("no", noted(why, agent))
        if not started and _at_a_start_dialog(pane, adapter):
            # A start the board cannot see: as quiet at a dialog as at its prompt.
            why = (
                "its pane shows a dialog of the agent's, where an Enter would answer it "
                f"for you (answer it there: `{project_service.fleet_command('attach', project)}`)"
            )
            if not note:
                return TellResult("no", why)
            try:
                how = _file_note(project, label, text, sender, agent=agent)
            except FleetError as exc:
                raise FleetError(f"not typed: {why}; {exc}") from exc
            return TellResult("no", f"{why} — {how}")
        told = pane.type_then_enter(
            text,
            label,
            press="press Enter in its pane",
            done="typed into its pane (it was waiting)",
        )
        if not isinstance(told, PaneWrite):
            return told
        if told.typed == "maybe":
            # A wedged server may still run what it was asked (review of #259, round 9, 1/8),
            # which the remote's trail says as `delivered=maybe` (round 10, 6/9), a note that
            # could not be filed too: it was the note's error, `delivered=no` (round 11, 6/13).
            # Whatever the note raised: a store locked past its busy timeout is no `FleetError`
            # (round 12, 3/9), and the note is a courtesy once the paste may be on its way.
            if not note:
                late = told.late(label, then=" at its prompt, unsent", again="type into it")
                return TellResult("maybe", late)
            try:
                how = _file_note(project, label, text, sender, agent=agent)
            except Exception as exc:
                late = told.late(label, then=" at its prompt, unsent", again="type into it")
                return TellResult("maybe", f"{late}. It was not filed as a board note: {exc}")
            late = told.late(label, then=" at its prompt as well, unsent", again="type into it")
            return TellResult("maybe", f"{how}, and {late}")
        return TellResult("no", noted(f"tmux could not type it ({told.error})", agent))
    return TellResult("no", noted(f"it is {status.state}", agent))


_QUEUE_TIMEOUT = 30.0


def _queue_message(
    adapter: AgentAdapter,
    agent: FleetAgent,
    session: TeamSession,
    text: str,
    record: launch_seal.LaunchRecord | str | None = None,
) -> bool | str | None:
    """Queue ``text`` for the next turn of ``agent``'s session through the agent's own
    queue.

    ``True`` when it was queued; ``None`` when the agent has no queue
    (``AgentAdapter.queue_args``) or no thread and home are recorded to queue for;
    otherwise why it was not, for the board note that carries the message instead;
    never raises. ``session`` is the board's row for the busy session that sent the
    message here.

    It runs with the caller's rights, outside any sandbox, so everything it runs with
    is the row's launch record's (``core.launch_seal``): the binary, and the thread and
    the home the agent's own hook reported, never the store's columns, which a sandbox
    could write before its broker (review r3 of #113, finding 1; r4, finding 1's class).
    The thread, even so, only in the shape the agent makes it (r3, finding 2).
    """
    if adapter.queue_args(session.id, text) is None:
        return None  # no queue: asked before anything it would run with is read
    record = _read_record(agent.id) if record is None else record
    if isinstance(record, str):
        _say_refused(
            agent.project_id,
            agent,
            "a message told to it is not queued",
            record,
            "it waits on the board for the agent's next prompt",
        )
        return f"not queued: {record}"
    if record.agent != adapter.id:
        return f"not queued: its launch record is {record.agent}'s, not {adapter.label}'s"
    facts = record.session
    home = (facts.home if facts is not None else None) or record.account_home
    if facts is None or not facts.native_id or not home:
        return None
    thread = launch_seal.native_id(facts.native_id)
    if thread is None:
        return f"its recorded native id is not one {adapter.label} makes (a UUID)"
    argv = adapter.queue_args(thread, text) or []
    executable = shutil.which(record.binary)
    if executable is None:
        return f"{record.binary!r} is not on your PATH to queue it"
    try:
        done = _run_native([executable, *argv], env={**untraced_env(), adapter.home_env: home})
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{adapter.label} could not queue it ({type(exc).__name__}: {exc})"
    if done.returncode != 0:
        said = (done.stderr or done.stdout or "").strip().splitlines()
        return f"{adapter.label} could not queue it ({said[-1] if said else done.returncode})"
    return True


def _run_native(argv: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """One short-lived native command (``codex queue``), its output captured."""
    return subprocess.run(
        argv,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=_QUEUE_TIMEOUT,
        check=False,
    )


def _its_agent(agent: FleetAgent, facts: PaneFacts | None) -> bool:
    """Whether the pane these facts are of runs ``agent``'s own agent now: the agent is in its
    foreground (:func:`_runs_the_agent`), on the server the row was started on
    (:func:`_outlived`), from one ``display-message`` (``PaneFacts.server_started``).
    ``agent`` is the row as its launch record places it (:attr:`Placed.agent`), the
    facts its placed pane's (:meth:`PlacedPane.facts`).

    The one test every path that types into a row's pane puts: :func:`tell`, the manager's
    nudge (:func:`nudge_manager`), and the remote's keys, quick answers and agent actions
    (``remote_server._remote_facts_refusal``). ``tell`` asked only the first, and pasted
    into the pane another server gave a held row's id (review of #259, round 2).
    """
    return (
        facts is not None and _runs_the_agent(facts) and not _outlived(agent, facts.server_started)
    )


def _runs_the_agent(facts: PaneFacts | None) -> bool:
    """Whether the pane's foreground process is the agent right now: not dead, and not a
    shell or the launcher (:func:`_agent_running`). Half of :func:`_its_agent`."""
    return facts is not None and not facts.dead and _agent_running(facts.current_command)


def _hooks_fresh(session: TeamSession | None, now: datetime) -> bool:
    """Whether ``session``'s own hooks spoke within ``_STALE_AFTER``, so its state is theirs."""
    return session is not None and now - session.last_seen_at <= _team()._STALE_AFTER


def _at_its_prompt(
    pane: PlacedPane,
    agent: FleetAgent,
    session: TeamSession | None,
    state: FleetAgentState,
    now: datetime,
) -> bool:
    """Whether a line typed into ``agent``'s pane now lands at its prompt: the one test
    ``tell`` and :func:`nudge_manager` type by. ``state`` is :func:`_derive`'s.

    An agent whose pane reads its readiness (``capabilities.reads_pane_readiness``) is
    there when it reads ``waiting``. Another is there when its own hooks freshly say
    ``waiting`` — and when they say nothing fresh (none fire before its first prompt, as
    Codex's do not), when its pane is quiet (``waiting``) — and, for an agent whose empty
    prompt is read off its screen (``AgentScreen.reads_empty_prompt``), its screen shows
    it at that prompt (``AgentScreen.at_empty_prompt``), however fresh its hooks. Quiet
    alone is not enough: Codex's folder-trust and hook-review dialogs print nothing
    either, and an Enter there answers them for the human (``enter continue`` on the
    folder's). Nor are
    fresh hooks: after a turn's ``Stop`` Codex opens dialogs of its own, its ``Switch to
    gpt-6-luna for lower credit usage?`` past a turn near its limit, where the Enter
    after a typed line picked the cheaper model for the agent and the line was lost
    (measured on 0.162.1). Never raises.
    """
    if state != "waiting":
        return False
    adapter = recorded_adapter(agent.agent)
    if adapter is not None and adapter.capabilities.reads_pane_readiness:
        return True
    reads = adapter is not None and screen_of(adapter).reads_empty_prompt
    if _hooks_fresh(session, now):
        if session is None or session.state != "waiting":
            return False
    elif not reads:
        return False
    if adapter is None or not reads:
        return True
    try:
        screen = pane.screen()
    except TmuxError:
        return False
    return screen_of(adapter).at_empty_prompt(screen)


def pane_is_the_agent(srv: TmuxServer, pane_id: str) -> bool:
    """Whether the pane's foreground process is the agent — :func:`tell`'s readiness test
    (:func:`_runs_the_agent`). Never raises: tmux that cannot answer is not a pane to type
    into either.

    Public because it is a seam by use: the captain's ``press`` and ``paste``
    (``services.captain.actions``) type only where ``tell`` would.
    """
    try:
        facts = srv.pane_facts(pane_id)
    except TmuxError:
        return False
    return _runs_the_agent(facts)


def _file_note(
    project: ProjectInfo,
    label: str,
    text: str,
    sender: str | None,
    *,
    agent: FleetAgent | None = None,
) -> str:
    """File ``text`` as a board note to ``label`` on the board its agent reads, as its spawn
    recorded (:func:`_board_of_row`), whoever tells it and from wherever. ``agent`` is the
    live row ``label`` names, when the caller has it. Resolved from the caller (the phone's
    tell, ``asq``, a shell without the hub the agent was spawned under), the note went to the
    project's own board while the agent read the hub's, and was reported filed; with
    ``[projects] capture`` off, a project board never added refused it though the hub's was
    added (review of #259, round 26, 2/11). With no live row, it goes where the caller
    resolves, as before (:func:`_note_board`)."""
    team = _team()
    board: str | None
    if agent is not None:
        board = _board_of_row(agent)
    else:
        with store_session() as store:
            board = _note_board(store, project, label)
    try:
        event = team.add_note(
            text, session_ref=sender, to_role=label, cwd=project.root, board_id=board
        )
    except (team.TeamDisabledError, project_service.NotAdded) as exc:
        # NotAdded: with no row to name it, the board the note resolves to
        # (`AISQUARE_TEAM_HUB`'s, or the project's) is one nobody added, with
        # `[projects] capture` off (review of #259, round 17).
        raise FleetError(f"cannot file the message as a board note: {exc}") from exc
    except KeyError as exc:
        raise FleetError(f"unknown sender session {sender!r}") from exc
    return f"filed as board note #{event.seq} to {label}"


def attach_persona(
    project: ProjectInfo, label: str, name: str, *, sender: str | None = None
) -> AttachReceipt:
    """Give a RUNNING agent a persona (docs/plans/spawn-personas.md §4.7).

    The persona is resolved first — an unknown name lists the known ones before
    the store or tmux is touched — then the live agent. One ``persona_attached``
    board event is written (which also refuses an unknown ``sender`` before
    anything changes); the ``fleet_agent`` row and, when the agent has joined,
    its ``team_session`` row record the name. The rows are what make it last: the
    session-start hook reads the fleet row, so a ``/clear`` or a restart briefs
    the agent with it again.

    The briefing itself is only ever TYPED, into an agent that is waiting, by
    ``tell``'s own readiness test (``tell(..., note=False)``). For any other agent
    nothing is filed in its place. ``tell``'s board note would be the whole fenced
    body and its guard sentence as an ordinary ``note``: every OTHER session read
    it in its next delta as if it were addressed to it, ``aisquare board`` and a
    teammate's session-start briefing showed it, and the distiller wrote it into
    the project brain (review of #240, finding 7). The board names the persona
    and never carries its body (§3.1). So such an agent is OWED the briefing
    (``team.owe_persona_briefing``, a marker under its row): its own next prompt's
    hook hands it the same preface and block, once and to it alone, unless a
    session start briefs it first. The receipt says that instead of a delivery.
    """
    try:
        persona = personas.resolve(name, personas.project_root(project))
    except personas.PersonaError as exc:
        raise FleetError(exc.rule) from exc
    with store_session() as store:
        agent = _live_agent(store, project, label)
    replaced = agent.persona if agent.persona and agent.persona != persona.name else None
    team = _team()
    note = f"persona {persona.name} attached to {label}"
    try:
        # On the board the agent reads, as its spawn recorded it (``_board_of_row``), the
        # rule ``tell``'s note keeps: resolved from the caller, under a hub the line went to
        # whichever board the caller resolved, and a hub nobody added (with ``[projects]
        # capture`` off) raised ``NotAdded`` out of the command (review of #201, F4).
        team.add_note(
            f"{note} (replaces {replaced})" if replaced else note,
            session_ref=sender,
            to_role=label,
            kind="persona_attached",
            cwd=project.root,
            board_id=_board_of_row(agent),
        )
    except (team.TeamDisabledError, project_service.NotAdded) as exc:
        raise FleetError(f"cannot record the attachment on the board: {exc}") from exc
    except KeyError as exc:
        raise FleetError(f"unknown sender session {sender!r}") from exc
    with store_session() as store:
        agent = store.set_fleet_agent_persona(agent.id, persona.name)
        if agent.session_id is not None and store.get_session(agent.session_id) is not None:
            store.set_session_persona(agent.session_id, persona.name)
        # What an earlier attach still owes is superseded, and BEFORE anything is typed:
        # the prompt that types this briefing runs the very hook that hands that one over.
        # The agent never read what was owed, so the preface names the persona IT ran as.
        told = team.settle_persona_briefing(store, agent.id, replaced=replaced)
    told = told if told != persona.name else None
    preface = team.persona_attached_preface(persona.name, told)
    # Pinned to the row the attachment was recorded on: a replacement that took the
    # label meanwhile is refused, not briefed with a persona its row does not carry.
    typed = tell(
        project,
        label,
        "\n".join([preface, *personas.briefing(persona)]),
        sender=sender,
        agent_id=agent.id,
        note=False,
    )
    how = typed.how
    if typed.typed == "no":
        with store_session() as store:
            team.owe_persona_briefing(store, agent.id, replaced=told)
        how = (
            f"{how}, so the briefing was not typed: {persona.name} is recorded and reaches it "
            "with its next prompt, for it alone; the board carries the name only"
        )
    delivered: Literal["typed", "noted", "unsent", "maybe"] = (
        "typed" if typed.typed == "yes" else "noted" if typed.typed == "no" else typed.typed
    )
    return AttachReceipt(
        agent=agent, persona=persona.name, replaced=replaced, delivered=delivered, how=how
    )


def _note_board(store: ContextStore, project: ProjectInfo, label: str) -> str | None:
    """The board a note to ``label`` in ``project`` goes to, through ``store``: the one the
    live row ``label`` names recorded (:func:`_board_of_row`); ``None`` with no live row of
    that label, where the caller's own rule decides. ``fleet tell``'s note
    (:func:`_file_note`) and the phone's (``remote_server._remote_note_board``) both ask it:
    each wrote this routing out, and the phone's opened a store for the label and another
    for its fallback (review of #259, round 31, 6/7)."""
    agent = store.fleet_agent_by_label(project.id, label, live_only=True)
    return None if agent is None else _board_of_row(agent)


def stop(
    project: ProjectInfo,
    label: str,
    *,
    force: bool = False,
    grace: float = 5.0,
    handover: bool = False,
    agent_id: str | None = None,
    nudge: bool = True,
    dismiss_dialog: bool = False,
    live_only: bool = False,
    ended_by: EndedBy = "stopped",
) -> StopReceipt:
    """``/exit`` the agent named ``label``, wait ``grace`` seconds, then kill its window.

    Resolves the LIVE row by label and hands it to :func:`_stop_row`; the
    documentation of what a stop is — and of ``handover`` and ``dismiss_dialog`` — lives
    there.

    With no live row, an ENDED agent's window may still be on the server
    (``remain-on-exit``), which is what the UI's 💤 row and its Stop button
    stand for (#138): stopping it means killing that window, and the receipt
    carries the ended row with nothing released — its claims went when it
    ended. Never under ``handover``: a hand-over moves a RUNNING agent, and an
    agent that ended on its own is not one ``switch`` may start again.

    ``agent_id`` pins the ROW, for a caller that means one rather than whoever
    holds the label now: the agent view. A view outlives its row (the shell
    keeps a view whose row left the frame), and by the time Stop is pressed on
    a 💤 view the label may name a replacement — the manager, woken by that very
    ``agent_exited``, ran ``fleet restart coder-1``. By label, that Stop sent
    ``/exit`` to the new coder-1, killed it and reported "stopped" (review of
    #138). Pinned, it stops that row or removes that row's window, and refuses
    when neither is left.

    ``live_only`` refuses an ended row as ``handover`` does, its window left as it
    is: :func:`bulk`'s, which acts on running agents alone. A row that ended after
    its look and before this read is a 💤 row, never one it says it stopped.

    ``ended_by`` is how the row is recorded as ended (#38): ``stopped``, on aisquare's word,
    so Restart all leaves it. A 💤 row whose window this removes is marked so too: stopped
    after it died is stopped. So is a row recorded as died with no window left, its label's
    newest, which is all the stop does to it (``StopReceipt.marked_only``). :func:`restart`
    ends a vanished pane's row here as ``died``, and a row already ended is handed back to
    it as it is, window and all.
    """
    with store_session() as store:
        try:
            agent = _live_agent(store, project, label, agent_id=agent_id)
        except NoSuchAgent:
            found = _by_label(store, project.id, label, live_only=False)
            ended = (
                (found[0] if found is not None else None)
                if agent_id is None
                else store.get_fleet_agent(agent_id)
            )
            placed = resolve_place(ended) if ended is not None else None
            if (
                ended is not None
                and ended.ended_at is not None
                and isinstance(placed, Placed)
                and _pane_alive(placed.pane)
            ):
                # Ended in the store while its window runs: stopped all the same, saying
                # so — its `ended_at` decides nothing (review r8 of #113, rule B).
                _say_ended_but_running(ended)
                agent = ended
            else:
                if handover or live_only:
                    raise
                if ended is None or ended.ended_at is None:
                    raise
                if ended_by != "stopped":
                    # `restart`'s, for a vanished pane whose row another reader (`reap`, a
                    # look) ended meanwhile: it is ended, and nothing of it is the stop's to
                    # remove. Marked stopped below, a respawn that then failed left an agent
                    # Restart all leaves (#274 delta review 2, m1 and m3). Not one the owner
                    # stopped meanwhile: handed back, the restart started it again over
                    # their `fleet stop` (final delta review, m1).
                    if ended.ended_by == "stopped":
                        raise NoSuchAgent(f"{label} ({ended.id}) was stopped meanwhile") from None
                    return StopReceipt(ended, [])
                removed = _kill_lingering_window(store, project, ended)
                if removed:
                    return StopReceipt(store.mark_fleet_agent_stopped(ended.id), [])
                newest = _by_label(store, project.id, ended.label, live_only=False)
                head = newest[0] if newest is not None else None
                if ended.ended_by != "died" or head is None or head.id != ended.id:
                    # Stopped already (a hand-over's stop writes that too, so its gap before
                    # the replacement is recorded is refused as it was), ended before
                    # aisquare said how, or a row another took the label from (the pin's
                    # refusal): nothing is left of it to remove, and nothing to record.
                    if isinstance(placed, Unplaced):
                        raise NoSuchAgent(
                            f"{label!r} has ended, and {placed.why}: no window of its may be "
                            "named, and the fleet closes no other"
                        ) from None
                    raise  # no such row, or no window of its own left (gone, a reused id)
                # Ended with no window left (its tmux server gone, a row `reap` recorded):
                # the owner's word that it stays down is recorded all the same. Refused,
                # nothing could keep one dead agent down, and Restart all brought it back
                # (m3). Only when tmux said so: a server that will not answer reads as "no
                # window" too, and its dead window may stand (final delta review, n3), and so
                # may one whose kill tmux refused (last delta review, n2). A refusal to try
                # again, not "no such agent": `--json` and the Remote stop said the row was
                # gone or stale (n1).
                if removed is None:
                    raise FleetUnavailable(
                        f"{label} ({ended.id}) ended, and tmux did not answer whether its "
                        "window is left — nothing was changed; run the stop again"
                    ) from None
                return StopReceipt(store.mark_fleet_agent_stopped(ended.id), [], marked_only=True)
    return _stop_row(
        project,
        agent,
        force=force,
        grace=grace,
        handover=handover,
        nudge=nudge,
        dismiss_dialog=dismiss_dialog,
        ended_by=ended_by,
    )


def _end_store_ended(
    store: ContextStore, agent_id: str, exit_status: int | None, ended_by: EndedBy = "stopped"
) -> FleetAgent | None:
    """End the row ``agent_id``, ended in the store while its window ran, as this stop's:
    its end and status, and stopped on aisquare's word when the stop is (``ended_by``, #38);
    ``None`` when another stop ended it first. The compare-and-set
    ``end_fleet_agent_if_live`` is for a live row, so this one's is a mark taken once per
    row (``set_meta_once``): two stops at once (the UI's Stop beside ``shutdown``'s) wrote
    over each other's status, released twice and announced the exit twice (review r10 of
    #113, finding 5). The mark and the end are one transaction: an end that fails takes the
    mark back with it, so the next stop ends the row (r11, finding 10)."""
    with store.transaction():
        if not store.set_meta_once(f"fleet-stopped:{agent_id}", _now().isoformat()):
            return None
        current = store.get_fleet_agent(agent_id)
        if current is None:
            return None
        ended = store.upsert_fleet_agent(
            current.model_copy(update={"ended_at": _now(), "exit_status": exit_status})
        )
        return store.mark_fleet_agent_stopped(agent_id) if ended_by == "stopped" else ended


def _stop_row(
    project: ProjectInfo,
    agent: FleetAgent,
    *,
    force: bool = False,
    grace: float = 5.0,
    handover: bool = False,
    nudge: bool = True,
    placement: Placement | None = None,
    beside: Sequence[Placement] | None = None,
    dismiss_dialog: bool = False,
    ended_by: EndedBy = "stopped",
) -> StopReceipt:
    """``/exit`` the agent in ``agent``'s pane, wait ``grace`` seconds, then kill its window.

    Takes the ROW, so a caller that already holds one — ``shutdown`` over its
    snapshot — stops that row and no other: resolving by label again let a
    label reused between the snapshot and this row's turn (the row exited, the
    manager spawned a new ``coder-1``) stop the new agent under the old one's
    turn, count it as stopped, and then record it lost as a late row too
    (review of the fold).

    ``handover`` is the HAND-OVER's — :func:`switch`, and :func:`restart` of a
    running agent: the agent is about to start again — resuming the same
    session, or a fresh one that takes the claims over — so the ended row's
    claims are not released (the session was marked ``team.HANDOVER_STATE``
    first, and its own ``SessionEnd`` parks them the way a ``/clear`` does), no
    ``agent_exited`` goes out and the manager is not nudged — the caller closes
    the loop with its own event: ``switched``, a wake kind, or ``restarted``,
    which asks the manager for nothing. Released and announced here, a looper
    took the moving agent's task in the gap and the manager respawned an agent
    that was on its way back (review of #205, finding 6; the fresh start, fourth
    round). Its receipt's ``released`` is therefore empty and ``withheld`` set:
    the claims are PARKED for the replacement, not returned.

    ``dismiss_dialog`` is the AUTOMATIC hand-over's (:func:`switch` with ``automatic``),
    which nobody watches: an agent whose screen shows a dialog of its own, where no exit
    command is typed (:func:`_exit_withheld`), gets its ``dismiss_key`` once, and the exit
    command once its empty prompt is back (:func:`_dismissed`), as Remote's Switch with
    ``dismiss_dialog`` presses Esc (No) first. Killed there instead, a Codex at the
    ``Switch to gpt-6-luna?`` offer it opens at its limit fired no ``SessionEnd``, and
    the session it left stayed parked on its limit on the board, and in the doctor's
    warning, until the reset (SMOKE7 of #113). A screen whose prompt does not come back
    is killed as before, nothing else typed. A stop by hand, or a ``switch`` or
    ``restart`` by hand, still kills at a dialog: answering one is the human's to ask.

    The agent's own ``SessionEnd`` hook releases its claims when it exits
    cleanly; ``force`` skips the ``/exit`` and goes straight to the kill — and
    therefore records no exit status, since one is only ever read from a pane
    that already reads dead. Whatever the ended row's session STILL holds is
    released here once the pane is confirmed dead or gone (a kill fires no hook,
    and a ``/clear`` parks claims on the id it ended), and RETURNED with the
    row (:class:`StopReceipt`), so a caller that reports releases —
    ``shutdown`` — counts this one instead of making a second.

    Ending the row emits ``agent_exited`` and nudges the manager, exactly as
    ``reap`` does for a pane it found dead. It belongs HERE rather than in each
    caller: the §7.3 event the manager's Stop hook wakes on must not depend on
    which path ended the agent, and while it was reap's alone a stopped agent
    left the board no record of its exit while a merely crashed one did.

    ``nudge=False`` is a RESTART's, as for :func:`_end_dead_rows`: the exit is
    still announced, but the manager is not woken for an agent that is coming
    back — woken by "exited" before ``restarted`` was on the board, it could
    restart the label itself, onto the replacement (review of #163, round 2).

    tmux failing to ANSWER is not evidence that the agent died, so it does not
    end the row (see :func:`_verify_gone`): a row ended for a process still
    running is an agent no live listing shows and no ``stop`` can address, and
    the operator was told "stopped". Neither is tmux REFUSING the kill: the
    window kill goes through the same verdict, so this returns only for a pane
    tmux confirmed dead or gone, and raises otherwise.

    The pane typed into and killed is the one the row's launch record places
    (:func:`resolve_place`; ``placement``, when the caller placed the row already), never
    one the row names (review r5 of #113, finding 1). A row rewritten to name another is
    stopped there all the same, and the board says what was rewritten: refused, one
    ``UPDATE`` of its own row made a sandboxed agent one no stop, restart, switch,
    hand-over or ``shutdown`` could end (r6, finding 4). A row no record places (spawned
    before this version) has its window killed, with no exit command typed into a pane
    the fleet has no record of, only where tmux lists it in a session a launch record of
    its project names and no record places another agent (:func:`_recordless_pane`;
    ``beside`` is the project's live rows, placed, when the caller has them): killed
    in ``asq-<codename>``, a name read from the store, a sandbox's fake row had another
    project's window killed (r7, finding 1). Elsewhere it is refused, nothing typed or
    killed, saying why and the way out. The receipt's ``killed`` says it had no exit.
    """
    placement = placement if placement is not None else resolve_place(agent)
    unrecorded: str | None = None
    place: FleetAgent | None
    if isinstance(placement, Placed):
        if placement.rewritten is not None:
            _say_rewritten(agent, placement.rewritten)
        pane, session, place = placement.pane, placement.session, placement.agent
    else:
        unrecorded = placement.why
        config = settings()
        if beside is None:
            with store_session() as store:
                live = store.fleet_agents(project.id, live_only=True)
            beside = _resolve_all([row for row in live if row.id != agent.id], config)
        found = _recordless_pane(agent, beside, config)
        if found is None:
            if _cannot_run(agent):
                # Spawned before this boot: nothing to stop, and nothing asked of tmux
                # (review r8 of #113, finding 3).
                return _end_unreachable(
                    project, agent, _from_before_this_boot(placement), nudge, ended_by
                )
            raise FleetError(
                f"{agent.label!r} was not stopped: {unrecorded}, and no dead window of its "
                "is listed in a session of its project's — nothing was typed into or killed "
                "where its row points, and a live window is never killed on a row's word. "
                "End it in its own window; once it has exited, "
                f"{_forget(project)} ends its row on your word"
            )
        pane, held = found
        session, place = held, None
    srv = pane.server()
    exit_status: int | None = None

    def _window() -> WindowInfo | None:
        """The agent's window as ``list-panes -s`` reports it; ``None`` = really gone.

        The window LIST is asked first, not ``pane_facts`` (display-message):
        on some tmux versions a JUST-DEAD pane answers display-message as the
        wrong pane, which the guard there maps to None — indistinguishable from
        gone, and the exit status is lost. The window list names dead panes
        reliably on every version this repo has measured (3.3a, 3.4, 3.5a, 3.7c).

        But that list is per SESSION, and the session may not carry the
        codename: ``rename`` fails open when tmux is unreachable, and the
        escape hatch lets anyone rename a session by hand. So a pane the list
        does not name is asked about directly — ``pane_facts`` answers None for
        both shapes of gone, so a pane that answers at all is alive (or dead
        with a status) under some other session name, and hard-killing it
        without the graceful ``/exit`` would drop its ``SessionEnd`` hook, its
        claims and its exit status.

        Both reads are the STRICT twins, because ``None`` from here is what
        :func:`_verify_gone` acts on: the lenient pair answers ``[]``/``None``
        for every non-zero exit, so a socket that answered the shutdown probe
        and then refused this user (``Permission denied``, exit 1 — the live
        server of round 7) read as "the pane is really gone", the row was ended
        and, under ``shutdown``, a running agent's claims went back to the
        board. That is the door round 9 closed on ``kill_window`` while leaving
        this one open (review of #121, round 9 verification). A wedged server
        was already safe — ``_tmux`` raises on the timeout, through either twin
        — so what changes here is the answered-but-refused case, which now
        raises and lands in ``_verify_gone``'s "could not be asked" branch.

        Either read finds a pane by its id, and on a server that started after
        the row was written that pane is another agent's (:func:`_outlived`):
        the row's own is gone with the server it ran on, which is what ``None``
        says. When the server started comes in the same answer as the pane
        (``WindowInfo.server_started``, ``PaneFacts.server_started``): asked in a
        second ``display-message`` of its own, every look of a stop's grace period
        cost one more process (review of #259, round 3).

        Neither read names the pane when tmux found no server at the socket, and
        a socket file taken from under its server is one of those: that is not a
        pane that is gone (:func:`_refuse_unless_gone`). Nor is a pane on a server
        younger than the row when the row recorded its server's start: that server
        took the socket from the row's, which may run on (follow-up of #257), and
        the same rule refuses it.
        """
        for window in session.panes_or_raise() if session is not None else ():
            if window.pane_id == pane.pane_id:
                if pane.outlived(window.server_started):
                    if place is not None:
                        _refuse_unless_gone(project, place, srv)
                    return None
                return window
        facts = pane.facts_or_raise()
        if facts is None:
            if place is not None:  # a dead window a row's word found is gone, whatever
                _refuse_unless_gone(project, place, srv)
            return None
        if pane.outlived(facts.server_started):
            if place is not None:
                _refuse_unless_gone(project, place, srv)
            return None
        return WindowInfo(
            session=session.name if session is not None else "",
            window_id="",  # display-message was not asked for one; nothing here reads it
            name=agent.label,
            pane_id=facts.pane_id,
            dead=facts.dead,
            dead_status=facts.dead_status,
            current_command=facts.current_command,
            activity=False,
        )

    exited_here = False  # this stop's `/exit` reached a pane that was running
    killed: str | None = None  # why no `/exit` was typed, when one was not
    dismissed: str | None = None  # the key pressed at a dialog before the `/exit`
    try:
        window = _window()
        if unrecorded is not None and window is not None and not window.dead:
            killed = (
                f"{unrecorded}: its window was killed, with no exit command typed into a "
                "pane the fleet has no record of"
            )
        if not force and killed is None and window is not None and not window.dead:
            killed = _exit_withheld(pane, place) if place is not None else None
            if killed is not None and dismiss_dialog and place is not None:
                dismissed, killed = _dismissed(pane, place, killed)
                if dismissed is not None:
                    window = _window()  # a dialog whose key quits the agent left it dead
        if (
            not force
            and killed is None
            and place is not None
            and window is not None
            and not window.dead
        ):
            pane.type_line(_exit_command(place), command=True)
            exited_here = True
            deadline = _monotonic() + grace
            dead_seen: float | None = None
            while True:
                window = _window()
                if window is None:
                    break
                if window.dead:
                    if window.dead_status is not None:
                        break
                    # A dead pane usually gets its exit status a poll later —
                    # but tmux 3.4 sometimes NEVER exposes it (measured in its
                    # own -vv server log: dead=1 with `pane_dead_status` empty
                    # on every poll for a full 15 s grace, about one death in
                    # thirty under one CPU; 3.5a and 3.7c always expose it).
                    # One second is plenty for a status that will ever land.
                    if dead_seen is None:
                        dead_seen = _monotonic()
                    elif _monotonic() - dead_seen >= _STATUS_WINDOW:
                        break
                if _monotonic() >= deadline:
                    break
                _sleep(_POLL_INTERVAL)
        if window is not None and window.dead:
            exit_status = window.dead_status
        # NOT suppressed. A kill that FAILED is not a pane that died, and this was
        # the one tmux failure in this call that did not go through
        # :func:`_verify_gone`: a `kill-window` tmux refused (a wedged server's 30 s
        # timeout, a lost race with a server reload) ended the row anyway and
        # returned it as stopped — under `shutdown` that also released the running
        # agent's claims to the next worker (review of #121, round 9). The comment
        # it carried, "already gone — which is what the kill wanted", is still true
        # for a pane that really is gone: `_verify_gone` looks again and says so.
        # A window `_window` did not find is not killed at all: gone is what the kill
        # wanted, and on a server younger than the row the id names ANOTHER agent's
        # pane (`_outlived`), whose window a `--force` stop or a shutdown killed.
        if window is not None:
            pane.kill()
    except TmuxError as exc:
        confirmed = _verify_gone(_window, agent.label, exc)
        # …but never at the cost of a status already READ: a pane seen dead with
        # its status, whose window then vanished before the kill, is gone with a
        # known exit code, and the second look can only answer None for it.
        if exit_status is None:
            exit_status = confirmed
    with store_session() as store:
        # The row as it is NOW, in the one statement that ends it — a compare-and-set:
        # ended meanwhile — its own hook, a `reap`, another terminal — it went away
        # on its own, with the status it recorded, and this stop is nobody's
        # (`NoSuchAgent`, which `shutdown` reads as exactly that). A read before the
        # write left a statement for a listing in another process to land in, and
        # both then announced the one death (review of #163, round 1). Re-reading a
        # window earlier, in a connection of its own, neither closed the gap nor
        # paid for itself (round 5).
        _record_the_branch(store, _in_its_recorded_tree(placement))
        ended: FleetAgent | None
        if agent.ended_at is not None:
            # A row ended in the store while its window ran (review r8 of #113, rule B):
            # this stop is the one that ended its agent, and it ends the row as any stop
            # does — its exit status, its release, `agent_exited` and the manager's nudge
            # below — or the manager waited on a coder that was gone (r9, finding 15). Its
            # end and status are this stop's: the store's end was the sandbox's word.
            ended = _end_store_ended(store, agent.id, exit_status, ended_by)
        else:
            ended = store.end_fleet_agent_if_live(
                agent.id, exit_status=exit_status, ended_by=ended_by
            )
        if ended is None:
            current = store.get_fleet_agent(agent.id)
            if current is not None and exited_here and not handover:
                # …except the death THIS stop's `/exit` caused, recorded by whoever
                # looked while the loop above waited for its status — a listing
                # above all: every read reconciles a dead pane since #138
                # (`_end_dead_rows`), the UI's tick every two seconds — whose end
                # released and announced as this one would have. The agent is
                # stopped; "no longer live" failed a stop that worked. Its release
                # is asked for again, though: a listing has no report to carry one
                # the store refused, and this stop has (review of #163, round 1;
                # after the listing's went through it finds nothing).
                # And recorded as the stop it was: the listing wrote "died" for the pane
                # this `/exit` took down, and a non-zero exit after it (a hook error,
                # another binary) left an agent the owner stopped for Restart all to
                # bring back (#38; #274 delta review 2, m2).
                if ended_by == "stopped":
                    current = store.mark_fleet_agent_stopped(current.id)
                return StopReceipt(
                    current,
                    *_release_or_say(store, current, why="agent stopped"),
                    dismissed=dismissed,
                )
            if current is not None and exited_here and handover:
                # A hand-over's row is one no listing ends while its mark holds
                # (`_handed_over`), so with the mark holding, whoever ended it meanwhile
                # stopped it past the mark — the operator's `fleet stop`, a `shutdown` —
                # and this stop is nobody's. With no mark to hold — a session not on the
                # board yet, which nothing could mark, or a mark a writer that does not
                # know it replaced — a listing records the death this `/exit` caused,
                # releases and announces it, and the hand-over is a stop that worked:
                # raised here, after the kill, the agent was gone and nothing started
                # (review of #163, round 2). Nothing is withheld, so there is nothing to
                # give back, and nothing is asked again: what that release left on the
                # session is the replacement's to inherit.
                marked = store.get_session(current.session_id) if current.session_id else None
                if not _handed_over(current, marked, _now()):
                    return StopReceipt(current, [], dismissed=dismissed)
            gone = EndedMeanwhile if exited_here else NoSuchAgent
            raise gone(f"{agent.label} ({agent.id}) is no longer live")
        # The process is dead or gone by here, so whatever its session still
        # holds is nobody's: a clean ``/exit`` released through the agent's own
        # hook already, a kill fired no hook, and a stop landing in a
        # ``/clear``'s gap found the claims parked on the id that just ended
        # (review of #135, second round, finding 5). ONE release, here, in the
        # store session that ended the row; what it returned rides on the
        # receipt for whoever counts it. A hand-over releases and announces
        # nothing: the replacement inherits, and ``switch`` says ``switched``.
        released: list[TeamTask] = []
        release_failed: str | None = None
        if not handover:
            released, release_failed = _release_or_say(store, ended, why="agent stopped")
            _emit_exit(store, ended)
    # Outside the store session, as ``reap`` does: the nudge types into a pane
    # and must not hold the write lock the woken manager's own hooks will want.
    if not handover and nudge:
        nudge_manager(ended.project_id, reason=f"{ended.label} exited", board=_board_of_row(ended))
    return StopReceipt(
        ended, released, release_failed, killed=killed, dismissed=dismissed, withheld=handover
    )


def _end_unreachable(
    project: ProjectInfo, agent: FleetAgent, why: str, nudge: bool, ended_by: EndedBy
) -> StopReceipt:
    """End ``agent``'s row, which no launch record places and whose agent cannot be running
    (:func:`_cannot_run`), with nothing asked of tmux: as a stop ends a row, its claims
    released, ``agent_exited`` emitted, the manager nudged, recorded as the stop says
    (``ended_by``, #38)."""
    with store_session() as store:
        ended = store.end_fleet_agent_if_live(agent.id, exit_status=None, ended_by=ended_by)
        if ended is None:
            raise NoSuchAgent(f"{agent.label} ({agent.id}) is no longer live")
        released, release_failed = _release_or_say(store, ended, why="agent stopped")
        _emit_exit(store, ended)
    if nudge:
        nudge_manager(ended.project_id, reason=f"{ended.label} exited", board=_board_of_row(ended))
    killed = f"{why}: its row was ended, nothing typed or killed"
    return StopReceipt(ended, released, release_failed, killed=killed)


def _exit_withheld(pane: PlacedPane, agent: FleetAgent) -> str | None:
    """Why :func:`_stop_row` types no exit command into ``agent``'s pane, or ``None`` to
    type it.

    An agent whose empty prompt is read off its screen (``AgentScreen.reads_empty_prompt``)
    and whose screen does not show it there (``AgentScreen.at_empty_prompt``) is showing
    something its Enter answers: a dialog of its own — Codex's ``Switch to gpt-6-luna for
    lower credit usage?`` after a turn near its limit, where ``/exit`` and its Enter picked
    the cheaper model and exited nothing (measured on 0.162.1) — or a draft the exit command
    would join. Its window is then killed at once, as ``--force`` kills it, and the receipt
    says why. A screen tmux cannot read is no such evidence: the exit command is typed, as
    it always was. Never raises.
    """
    adapter = recorded_adapter(agent.agent)
    if adapter is None or not screen_of(adapter).reads_empty_prompt:
        return None
    try:
        screen = pane.screen()
    except TmuxError:
        return None
    if screen_of(adapter).at_empty_prompt(screen):
        return None
    return (
        f"its pane showed {adapter.label} at a dialog or a draft, not its empty prompt, "
        f"where {adapter.exit_command} and its Enter would have answered it: its window "
        "was killed without one"
    )


_DISMISS_WAIT = 3.0
"""How long :func:`_dismissed` waits for the agent's empty prompt after its dismiss key:
Codex 0.162.1 redraws it within a poll (measured under tmux 3.7c)."""


def _dismissed(pane: PlacedPane, agent: FleetAgent, withheld: str) -> tuple[str | None, str | None]:
    """Press ``agent``'s ``dismiss_key`` once at the dialog :func:`_exit_withheld` withheld
    the exit command from (``withheld``, why), and read its screen until its empty prompt
    is back (``AgentScreen.at_empty_prompt``), for up to :data:`_DISMISS_WAIT`.

    Returns ``(what was pressed, why the window is killed)``: ``(pressed, None)`` with the
    prompt back, where :func:`_stop_row` types the exit command and the agent's
    ``SessionEnd`` runs; ``(pressed, why)`` for a screen still not at that prompt (a
    draft, which the key leaves, or a dialog it does not close), killed as before with
    nothing else typed; ``(None, withheld)`` when tmux took no key. One key, never two: a
    second Esc at Codex's empty prompt opens its transcript, where Enter rewinds. Never
    raises.
    """
    adapter = recorded_adapter(agent.agent)
    if adapter is None:
        return None, withheld
    key = adapter.dismiss_key
    try:
        pane.press(key)
    except TmuxError:
        return None, withheld
    pressed = f"{key} at a dialog of its own, then {adapter.exit_command}"
    deadline = _monotonic() + _DISMISS_WAIT
    while True:
        _sleep(_POLL_INTERVAL)
        try:
            if screen_of(adapter).at_empty_prompt(pane.screen()):
                return pressed, None
        except TmuxError:
            break
        if _monotonic() >= deadline:
            break
    return f"{key} at a dialog of its own", (
        f"its pane showed {adapter.label} at a dialog or a draft, not its empty prompt, and "
        f"still did after {key}: its window was killed without {adapter.exit_command}, "
        "whose Enter would have answered it"
    )


def _refuse_unless_gone(project: ProjectInfo, agent: FleetAgent, srv: TmuxServer) -> None:
    """Raise unless ``agent``'s pane, which tmux did not find on ``srv``, is really gone.
    ``agent`` is the placed row whose launch record holds the server's socket path, boot,
    time and start (:attr:`Placed.agent`).

    tmux's strict reads answer "no such pane" for a live server that does not hold it, and
    the same for no server at the socket, ``error connecting to <path> (No such file or
    directory)`` included. That last one is a socket file taken from under a server that
    runs on (systemd-tmpfiles, logind), and its agent is not gone. Read as gone, ``fleet
    stop`` and asq's Stop ended a running agent's row, so ``uninstall`` went on under it,
    and ``restart`` started a second agent in its worktree (round 29 of #257). So this
    asks the rule ``uninstall`` and ``shutdown`` ask
    (:func:`~aisquare.core.tmux.server_may_run`): a row from another boot, a stale socket
    file, or a server that answers on the socket, where tmux asked, shows the pane gone.
    Anything else is refused, as :func:`_verify_gone` refuses a question it could not put.
    """
    answer = tmux_core.server_may_run(
        srv, agent.socket_path, agent.boot_id, agent.created_at, agent.server_started_at
    )
    if answer is None or answer == "answers":
        return
    try:
        path = agent.socket_path or srv.socket_path()
    except TmuxUnavailable:
        return  # no POSIX uid: no tmux server can exist here
    if answer == "unasked":
        raise FleetError(
            f"{agent.label!r} was not stopped: its server's socket {path} could not be asked, "
            "so it may still be running, and its row is left live; run this again where it "
            "can be asked"
        )
    why, way = _unreachable_why(answer, path, forget=_forget(project))
    raise FleetError(f"{agent.label!r} was not stopped: {why}, and its row is left live. {way}")


def _forget(project: ProjectInfo) -> str:
    """``fleet shutdown --forget`` for ``project``, as a way out names it: the operator's word
    that ends a row whose agent no tmux client reaches."""
    return f"`{project_service.fleet_command('shutdown', project, '--forget')}`"


def _unreachable_why(answer: SocketAnswer, path: Path, *, forget: str) -> tuple[str, str]:
    """``(why, what to do)`` for a row whose agent may still run though no tmux client
    reaches its server, as :func:`~aisquare.core.tmux.server_may_run` said (``answer``):
    its socket file is gone, or another server took its socket. ``forget`` names the
    operator's word that ends the row, as the command saying this spells it. The one
    wording ``stop``, ``shutdown`` and ``reap`` give, so an operator reads the same
    way out from each.
    """
    if answer == "another":
        return (
            f"another tmux server answers on its socket {path}, started at another time than "
            "the one it was started on, which lost its socket file and may still be running "
            "where no tmux client reaches it",
            "If it is, send that server SIGUSR1 once that socket is free, which makes tmux "
            f"create its socket again, and run this again; if it is not, {forget} ends its "
            "row on your word",
        )
    if answer == "gone":
        return (
            f"its server's socket {path} is gone, so no tmux client can reach the server, "
            "which may still be running",
            "If it is, send that server SIGUSR1, which makes tmux create its socket again, "
            f"and run this again; if it is not, {forget} ends its row on your word",
        )
    return (
        f"its server's socket {path} could not be asked, so its agent may still be running",
        f"Run this again where it can be asked, or, if it is not running, {forget} ends its "
        "row on your word",
    )


def _socket_gone(pane: PlacedPane) -> str | None:
    """Why nothing reaches ``pane``: its server's socket is gone (moved or removed; the
    server may still run), as ``stop`` says it (:func:`_refuse_unless_gone`). ``None`` when
    the socket is there, or could not be asked. ``tell`` read the screen it could not get as
    a dialog of the agent's own (SMOKE5 of #113)."""
    srv = pane.server()
    try:
        if srv.listener(pane.address.path) != "gone":
            return None
        path = pane.address.path or srv.socket_path()
    except (TmuxError, TmuxUnavailable):
        return None
    return (
        f"its server's socket {path} is gone, so no tmux client can reach the server, which "
        "may still be running: send that server SIGUSR1, which makes tmux create its socket "
        "again"
    )


def _manager_first(agents: list[FleetAgent]) -> list[FleetAgent]:
    """The manager before its team — it is the one agent that spawns others.

    Stops are sequential and each can cost the whole ``grace``, so a six-agent
    shutdown spends half a minute in the loop; the manager staffing the project
    through that window is what puts a fresh row on a socket that is coming
    down. Taking it down first narrows the window (``_record_late_rows`` closes
    it). ``sorted`` is stable, so the rest keep their spawn order.
    """
    return sorted(agents, key=lambda agent: agent.label != MANAGER_LABEL)


def _shutdown_targets(
    project: ProjectInfo | None, *, say: bool = False
) -> list[tuple[ProjectInfo, list[Placement]]]:
    """Every project in scope with its live rows — FORGOTTEN registrations included —
    each where its launch record places it (:func:`resolve_place`).

    ``list_projects()`` hides a tombstoned registration (and, since #139, a
    captured one), and a forgotten project can still hold live rows: ``project
    forget`` reads liveness and writes the tombstone in separate statements, and
    a concurrent ``fleet spawn`` revives the row (``onboard_project`` clears
    ``forgotten_at``) and records its agent.
    Hidden here, that agent's pane would be taken down with its session while its
    row stayed live — and nothing could ever reconcile it: ``reap`` correctly
    refuses a socket it cannot reach, and a second ``shutdown`` would not see the
    project either. That is precisely the permanently-wrong row this command
    exists to eliminate, so the scan reads past the tombstone.

    The placements are everything the rest of the run acts on: the rows stopped, the
    servers probed, the sessions taken down (:func:`_kill_fleet_sessions`), each record
    read once, here, where the store is opened behind the run's handlers: opened again in
    the kill phase, a store that failed after the rows were stopped raised out of the run
    with the sessions up (review r7 of #113, findings 4 and 10). ``say``, the run itself
    (not its plan), says on the board which row was rewritten to name another (r6, 4).
    """
    with store_session() as store:
        if project is not None:
            projects = [store.get_project(project.id) or project]
        else:
            projects = store.list_projects(include_forgotten=True)
        scanned = [(p, store.fleet_agents(p.id, live_only=True)) for p in projects]
    config = settings()
    targets: list[tuple[ProjectInfo, list[Placement]]] = []
    for current, rows in scanned:
        placements = _resolve_all(_manager_first(rows), config)
        if say:
            for placement in _placed_only(placements):
                if placement.rewritten is not None:
                    _say_rewritten(placement.row, placement.rewritten)
        targets.append((current, placements))
    return targets


def _say_rewritten(row: FleetAgent, why: str) -> None:
    """Say on the board that ``row`` was rewritten in the store and is acted on where its
    launch record places it (:func:`_say_refused`, once per row and reason)."""
    _say_refused(
        row.project_id,
        row,
        "stopped where its launch record places it",
        why,
        "nothing was typed into or killed where its row points",
    )


def _shutdown_sockets(
    targets: Sequence[tuple[ProjectInfo, list[Placement]]], config: FleetSettings
) -> list[_Address]:
    """Every server in scope: where the rows' launch records place them, plus today's socket.

    Today's is included even with no rows on it, because a session can outlive
    every row that was recorded in it (``rename`` fails open, and the escape
    hatch lets anyone rename a session by hand). A server only a row's store columns
    name is none of them (:func:`resolve_place`).
    """
    addresses = {
        placement.pane.address
        for _, placements in targets
        for placement in _placed_only(placements)
    }
    addresses.add(_Address(config.tmux_socket))
    return sorted(addresses, key=lambda address: (address.name, str(address.path or "")))


def _require_usable_tmux(config: FleetSettings) -> None:
    """Refuse before ANY row is touched unless tmux itself is usable.

    ``answers()`` is False for "no usable tmux" as well as for "no server"
    (``TmuxServer.answers`` swallows :class:`TmuxUnavailable`), and the first is
    never evidence a pane died: measured on a live server holding a live
    session, ``answers()`` reads True with a normal ``PATH`` and False with tmux
    off it. Without this guard a ``tmux`` that left ``PATH`` — or a
    ``TMUX_TMPDIR`` set at spawn and unset here — made ``shutdown`` end every
    live row as lost while every agent kept running, and the next ``reap`` then
    handed those agents' worktrees to ``git worktree remove``.

    ``_require_tmux`` maps only :class:`TmuxUnavailable`; ``tmux -V`` can also
    time out (``_COMMAND_TIMEOUT``) and raise a bare :class:`TmuxError`, which
    is not a :class:`FleetError` and would reach the operator as a traceback
    with no ``--json`` output at all.
    """
    srv = server(config)
    try:
        _require_tmux(srv)
    except TmuxError as exc:  # a timeout or an OSError from `tmux -V`
        raise FleetUnavailable(
            f"tmux could not be asked for its version ({exc}) — no row was touched; "
            "try again once tmux answers"
        ) from exc


def _shutdown_tmux(config: FleetSettings, *, forget: bool) -> str | None:
    """``None`` when tmux is usable (:func:`_require_usable_tmux`); else, with ``forget``,
    why not, and every row is ended unchecked on the operator's word, but for one whose
    socket answers (:func:`_unreached`, which needs no tmux).

    Without ``forget`` an unusable tmux still refuses, and the refusal names it. A fleet
    whose tmux is gone had no way to end its rows otherwise, and ``uninstall`` refuses for
    as long as one may be running (round 27 of #257). Not "no tmux, so no server": a
    server outlives its program leaving ``PATH``, which only the operator can rule out.
    """
    try:
        _require_usable_tmux(config)
    except FleetUnavailable as exc:
        if forget:
            return str(exc)
        raise FleetUnavailable(
            f"{exc}; if the fleet's agents are not running, `--forget` ends their rows on "
            "your word without tmux"
        ) from exc
    return None


def _same_socket(left: Path, right: Path) -> bool:
    """Whether two socket paths name one socket, symlinks resolved (macOS ``/tmp``).

    Through :func:`~aisquare.core.agents.dir_identity`, which reads a path it cannot
    resolve as written: pathlib raises ``RuntimeError``, not ``OSError``, for a symlink
    loop on 3.11 and 3.12, and :func:`_address` asks this on every read of a row whose
    recorded path is spelled otherwise than this shell's (every row on macOS, whose tmux
    records ``/private/tmp``). A loop on the way to either path ended the listing, asq's
    tick, spawn, stop, ``tell``, ``reap`` and the doctor in a traceback (round 29 of #257);
    now the two paths read as different, and the row is asked at the one it recorded.
    """
    if left == right:
        return True
    return agent_core.dir_identity(left) == agent_core.dir_identity(right)


def _refuse_from_inside(sockets: Sequence[_Address], config: FleetSettings) -> None:
    """Refuse when this very process runs INSIDE a server whose sessions are going.

    ``fleet attach`` is the documented full-fidelity escape hatch and every
    fleet window runs ``aisquare`` inside that same server, so an operator (or
    the manager agent itself) typing ``aisquare fleet shutdown`` in a fleet pane
    is ordinary use, not abuse. Killing that session SIGHUPs the pane running the
    command: the store writes have already happened, none of the report prints,
    and no exit code is observable — losing the one thing this command offers
    over ``tmux kill-server``.

    ``$TMUX`` is tmux's own ``<socket-path>,<pid>,<session-index>``;
    :meth:`~aisquare.core.tmux.TmuxServer.socket_path` reproduces the path for a
    server that is not running, and the two are compared with symlinks resolved
    because ``/tmp`` is one on macOS.
    """
    # `<socket-path>,<pid>,<session-index>`: the LAST two fields are the numbers,
    # and the path may itself contain commas (a socket name or TMUX_TMPDIR with
    # one), which tmux preserves — a `split(",")[0]` truncated such a path and
    # let this guard accept the pane it was about to kill (review of #121,
    # round 7).
    inside = os.environ.get("TMUX", "").rsplit(",", 2)[0]
    if not inside:
        return
    here = Path(inside)
    for socket in sockets:
        path: Path | None = None
        with suppress(TmuxUnavailable, OSError):
            path = _server_at(socket, config).socket_path()
        if path is not None and _same_socket(path, here):
            raise FleetError(
                f"fleet shutdown is running INSIDE the fleet's own tmux server ({socket}) — "
                "killing its sessions would kill this pane before it could report anything. "
                "Nothing was touched: detach (your tmux prefix, then `d`) or open a terminal "
                "outside the fleet and run it there"
            )


def _shutdown_probe(sockets: Sequence[_Address], config: FleetSettings) -> dict[_Address, bool]:
    """Which sockets have a server listening — refusing rather than guessing.

    Asked with :meth:`TmuxServer.reachable`, the probe that RAISES on a client
    that cannot be run, rather than :func:`_server_answers` / ``answers()``,
    which swallow that into the same ``False`` as "no server". Both kinds of
    failure — a 30 s command timeout on a wedged server (a bare
    :class:`TmuxError`) and an unavailable client (:class:`TmuxUnavailable`) —
    become a :class:`FleetError` refusal with nothing touched. "The socket did
    not answer" and "we could not ask" are different facts: the first ends rows
    on the operator's word, the second refuses.
    """
    answering: dict[_Address, bool] = {}
    for socket in sockets:
        try:
            # The STRICT probe (review of #121, round 6): `answers()` swallows an
            # unavailable client into False, and a client that disappears or
            # fails at execution between `_require_usable_tmux` and this call
            # would have read as "no server" — every row on that socket ended
            # as lost before a single stop was attempted, panes alive.
            # `reachable()` raises `TmuxUnavailable` (a `TmuxError`) instead,
            # which the handler below turns into a refusal with nothing touched.
            answering[socket] = _server_at(socket, config).reachable()
        except TmuxError as exc:
            raise FleetError(
                f"tmux could not be asked whether a server is running on '{socket}' ({exc}) "
                "— no row was touched, because a socket that cannot be ASKED is not a socket "
                "with no server; try again once tmux answers"
            ) from exc
    return answering


def _unreached(
    placed: Placed,
    *,
    unusable: bool = False,
    answering: bool = False,
    forget: str = "--forget",
    reads: tmux_core.SocketReads | None = None,
) -> tuple[SocketAnswer, str, str] | None:
    """``(what its socket said, why, what to do)`` for a row whose server did not answer
    through tmux, or could not be asked as tmux is ``unusable`` here, but whose agent may
    still run; ``None`` when it cannot be running.

    The rule ``uninstall`` asks (:func:`~aisquare.core.tmux.server_may_run`): only a row
    from another boot or a stale socket file shows the agent gone. Ending a row on less
    stopped nothing, and the next ``uninstall`` went on under the running agents: a socket
    file taken from under its server (round 27 of #257). The socket asked is the one the
    row recorded, where tmux was asked too (:func:`_address`), else where this shell's tmux
    looks; one that answers now came up after tmux was asked. The socket, its path, the
    boot, the time and the server's start are the row's launch record's
    (:func:`resolve_place`).

    ``answering`` is a row on a socket the probe found a server on: asked whether that
    server is the row's (``"another"``: it started at another time than the one the row
    recorded, which lost its socket file to it and may run on, follow-up of #257). Any other
    answer is then ``None`` here and the row is stopped as any other: tmux reached that
    server, so a socket that read ``"unasked"`` (a connect that timed out on a busy server)
    or ``"gone"`` (its file removed since) holds no agent out of reach (review of #259,
    round 5): held, the row spared no session, so the kill phase ended its agent while the
    report said it was left live. ``forget``
    is how the way out spells the operator's word: ``shutdown``'s own flag, or the command
    for ``reap``. ``reads`` is the caller's for every row it asks, so rows on one server ask
    it once (:class:`~aisquare.core.tmux.SocketReads`).
    """
    agent, srv = placed.agent, placed.pane.server()
    answer = tmux_core.server_may_run(
        srv,
        agent.socket_path,
        agent.boot_id,
        agent.created_at,
        agent.server_started_at,
        reads=reads,
    )
    if answer is None or (answering and answer != "another"):
        return None
    try:
        path = agent.socket_path or srv.socket_path()
    except TmuxUnavailable:
        return None  # no POSIX uid: no tmux server can exist here
    if answer == "answers" and unusable:
        return (
            answer,
            f"a server listens on its socket {path}, and no usable tmux here can stop it",
            "put a usable tmux back and run this again",
        )
    if answer == "answers":
        return answer, f"a server came up on its socket {path} after it was asked", "run this again"
    why, way = _unreachable_why(answer, path, forget=forget)
    return answer, why, way[0].lower() + way[1:]


def _held_rows(
    targets: Sequence[tuple[ProjectInfo, list[Placement]]],
    answering: Mapping[_Address, bool],
    *,
    unusable: bool = False,
) -> dict[str, tuple[SocketAnswer, str, str]]:
    """:func:`_unreached` for each placed row on a socket that did not answer, or for every
    placed row where tmux is ``unusable``, by id, where its agent may still run. A row no
    record places has no socket to ask: :func:`_stop_row` decides it.

    And for each placed row on a socket that DID answer, when its record holds its server's
    start: the server that answers may be another than the row's, which lost its socket
    file to it and runs on (follow-up of #257). Stopped as any other, the row ended under
    its agent: its pane read as gone, and the socket answered.
    """
    reads = tmux_core.SocketReads()  # one per server, and the boot once, not per row
    held: dict[str, tuple[SocketAnswer, str, str]] = {}
    for _, placements in targets:
        for placed in _placed_only(placements):
            if unusable or not answering.get(placed.pane.address):
                unreached = _unreached(placed, unusable=unusable, reads=reads)
            elif placed.agent.server_started_at is not None:
                unreached = _unreached(placed, answering=True, reads=reads)
            else:
                continue
            if unreached is not None:
                held[placed.row.id] = unreached
    return held


def _swept_sessions(
    srv: TmuxServer, address: _Address, config: FleetSettings, *, skip: Collection[str]
) -> list[PlacedSession]:
    """``--all``'s sweep: every ``asq-`` session tmux lists on this server, by the name it
    listed, but those in ``skip`` (taken down by their records' ids already). A ``rename``
    that failed open, a session renamed by hand, one of an agent spawned before this
    version: the fleet's session either way, and the sweep needs no record to name it."""
    listed = [name for name in srv.sessions_or_raise() if name.startswith(SESSION_PREFIX)]
    return [_listed_session(address, name, config) for name in sorted(listed) if name not in skip]


Spared = set[tuple[_Address | None, str]]
"""``(server, pane)`` a session holding which is not taken down; ``None`` for the server
when the pane is a row's word only, which may spare, never kill."""


def _spared_panes(report: ShutdownReport, placements: Mapping[str, Placement]) -> Spared:
    """``(server, pane)`` of every row this run left LIVE, where its launch record places it
    (``placements``, by row id): a session holding one is not taken down. A row no record
    places is spared by the pane id its row names, on any server: that word only ever
    keeps a session up. Unspared, ``--all``'s sweep killed the window of an agent the same
    report told the operator to end in its own window (review r8 of #113, finding 10).

    Not a row left live as held (:attr:`ShutdownRow.held`): its agent may run where no
    client reaches it, and its pane is on no server this run asks. A row another server
    answers for spared its project's session on that server, which the held agent does
    not run on, with every window there and the server itself (review of #259, round 3).
    A row whose silent socket answers now (``"answers"``) is not held so, and is spared: its
    pane may be on that server, and a late row's take-down there killed the session of an
    agent the report said it left live (review of #259, round 6, 3/10)."""
    spared: Spared = set()
    for row in report.failed:
        if row.held:
            continue
        placement = placements.get(row.agent.id)
        if isinstance(placement, Placed):
            spared.add((placement.pane.address, placement.pane.pane_id))
        else:
            spared.add((None, row.agent.pane_id))
    return spared


def _kill_fleet_sessions(
    targets: Sequence[tuple[ProjectInfo, list[Placement]]],
    sockets: Sequence[_Address],
    answering: Mapping[_Address, bool],
    report: ShutdownReport,
    config: FleetSettings,
    *,
    every: bool,
    lingering: Sequence[tuple[ProjectInfo, list[Placement]]] = (),
) -> None:
    """Kill the fleet's OWN sessions — never the server.

    ``tmux kill-server`` is server-scoped, but the fleet's claim is only ever to
    its own sessions. Reproduced while reviewing the first draft: a
    socket holding one hand-made session and no fleet rows at all reported
    ``0 stopped, 0 recorded lost`` and destroyed that session unmentioned — and
    ``[fleet] tmux_socket`` is a free-form string with no validator, so an
    operator who points it at ``default`` loses their personal tmux server to a
    command that reported destroying nothing. Killing sessions still takes the
    server down when the fleet is all it held: ``BUNDLED_CONF`` does not set
    ``exit-empty off``, so a server with no session exits on its own.

    A project's sessions are the ones its LIVE rows' launch records name, by tmux's id on
    the server each record places it (:attr:`Placed.session`), from the run's snapshot
    (``targets``), so nothing here opens the store: never ``asq-<codename>``, a name read
    from the store (review r6 of #113, finding 7), nor a name a record kept after a rename
    another project's session then took (r7, finding 2). Every such session was made for
    this project or joined by its id (:func:`_session_to_join`). The dead windows of its
    rows that ended within :data:`RECENTLY_ENDED` (``lingering``, read with the snapshot,
    :func:`_lately_ended`) are removed where their records place them
    (:func:`_remove_lingering_windows`), which takes a session that only they held with
    them. ``--all`` also sweeps every other ``asq-`` session tmux lists on a server in scope
    (:func:`_swept_sessions`).

    A session holding a row this LEFT LIVE is spared (:func:`_spared_panes`). Killing it
    would end an agent whose pane ``_verify_gone`` had just confirmed alive, and leave its
    live row on a dead pane — the state this whole command exists to end.
    """
    placements = {
        placement.row.id: placement for _, rows in targets for placement in rows
    }  # fmt: skip
    spared_panes = _spared_panes(report, placements)
    spared = {
        (placement.pane.address, placement.row.project_id)
        for row in report.failed
        if not row.held and isinstance(placement := placements.get(row.agent.id), Placed)
    }
    taken: dict[_Address, set[str]] = {}
    ended = {project.id: rows for project, rows in lingering}
    for project, rows in targets:
        # Every session of the project's: the ones its spawns made, whatever rows are left
        # or how long ago they ended, the ones its rows' records name, and the legacy one
        # holding its agents spawned before records were kept (review r8 of #113, rule D).
        expected = {(session.address, session.target) for session in _sessions_of(rows)}
        mine = [*rows, *ended.get(project.id, [])]
        for session in _project_sessions(project.id, mine, config, every_legacy=True):
            if not answering.get(session.address):
                continue
            taken.setdefault(session.address, set()).add(session.name)
            qualified = f"{session.address}:{session.name}"
            if (session.address, project.id) in spared:
                if qualified not in report.sessions_left_up:
                    report.sessions_left_up.append(qualified)
                _not_down(report, project.id)
                continue
            _take_down_session(
                session,
                project.id,
                spared_panes,
                report,
                expected=(session.address, session.target) in expected,
            )
        _forget_gone_sessions(project.id, config)
    _remove_lingering_windows(lingering, answering, report)
    if not every:
        return
    for socket in sockets:
        if not answering.get(socket):
            continue
        srv = _server_at(socket, config)
        try:
            swept = _swept_sessions(srv, socket, config, skip=taken.get(socket, set()))
        except TmuxError as exc:
            # tmux stopped answering between the probe and here (it can leave
            # PATH mid-run): nothing on this socket can be reported killed, and
            # nothing may be CLAIMED killed either — INCLUDING by omission. An
            # empty report over a surviving session read as "✓ fleet shut down"
            # (review of #121, round 2), so the failure is recorded as a failed
            # session kill and every project in scope is not confirmed down.
            report.sessions_failed.append(f"{socket}:* (could not list sessions: {exc})")
            for project, _ in targets:
                _not_down(report, project.id)
            continue
        for session in swept:
            _take_down_session(session, None, spared_panes, report, expected=True)


def _lately_ended(
    targets: Sequence[tuple[ProjectInfo, list[Placement]]],
) -> list[tuple[ProjectInfo, list[Placement]]]:
    """The rows of the projects in scope that ended within :data:`RECENTLY_ENDED`, where
    their launch records place them, each record read once: whose dead windows
    (``remain-on-exit``) the kill phase removes (:func:`_remove_lingering_windows`). Read
    with the snapshot, before anything is stopped, so the kill phase opens no store: one
    that failed after the rows were stopped raised out of the run with the sessions up
    (review r7 of #113, finding 4), and nothing older is read (finding 10) — but a row
    whose window the panes say runs (:func:`_running_ids`), however long ago its row was
    ended: :func:`_still_running` stops it (r9, finding 6)."""
    cutoff = _now() - RECENTLY_ENDED
    with store_session() as store:
        ended = [
            (project, [
                row for row in store.fleet_agents(project.id, live_only=False)
                if row.ended_at is not None
            ])
            for project, _ in targets
        ]  # fmt: skip
    config = settings()
    lingering: list[tuple[ProjectInfo, list[Placement]]] = []
    for project, rows in ended:
        old = {row.id for row in rows if row.ended_at is not None and row.ended_at < cutoff}
        still = _running_ids(project.id, old, config) if old else set()
        kept = [row for row in rows if row.id not in old or row.id in still]
        lingering.append((project, _resolve_all(kept, config)))
    return lingering


def _forget_gone_sessions(project_id: str, config: FleetSettings) -> None:
    """Drop from the project's index (``launch_seal.project_sessions``) the sessions tmux
    CONFIRMS gone and the windows no longer in them, so neither list grows with every
    spawn. A session tmux could not be asked about is kept: dropped, nothing took it down
    once its rows had ended a day ago (review r9 of #113, finding 11). Removed from the
    index as it stands at the write, so a spawn landing meanwhile keeps its entry (r9, 12).
    Never raises."""
    gone: set[tuple[str, str]] = set()
    closed: set[tuple[str, str]] = set()
    windows = launch_seal.project_windows(project_id)
    for session in _indexed_sessions(project_id, config):
        try:
            if not session.present_or_raise():
                gone.add((session.address.name, session.target))
                continue
            listed = {window.pane_id for window in session.panes_or_raise()}
        except TmuxError:
            continue
        closed |= {
            (entry.tmux_socket, entry.pane_id)
            for entry in windows
            if entry.session_id == session.target
            and entry.tmux_socket == session.address.name
            and entry.pane_id not in listed
        }
    if gone or closed:
        launch_seal.forget_project_sessions(project_id, gone, closed)


def _still_running(
    targets: Sequence[tuple[ProjectInfo, list[Placement]]],
    lingering: Sequence[tuple[ProjectInfo, list[Placement]]],
    *,
    say: bool,
) -> tuple[list[tuple[ProjectInfo, list[Placement]]], list[tuple[ProjectInfo, list[Placement]]]]:
    """``targets`` with each row of ``lingering`` (ended in the store within
    :data:`RECENTLY_ENDED`) whose window its launch record places still runs, and
    ``lingering`` without them: such a row is stopped as a running one, its ``ended_at``
    deciding nothing; ``say``, the run itself, says so on the board. One ``UPDATE`` of its
    own row made a sandboxed agent one ``shutdown`` left running (review r8 of #113, finding
    2). A row ended longer ago goes down with its project's sessions (rule D)."""
    running: dict[str, list[Placed]] = {}
    for project, rows in lingering:
        for placed in _placed_only(rows):
            if _pane_alive(placed.pane):
                if say:
                    _say_ended_but_running(placed.row)
                running.setdefault(project.id, []).append(placed)
    if not running:
        return list(targets), list(lingering)
    moved = {placed.row.id for rows in running.values() for placed in rows}
    return (
        [(project, [*rows, *running.get(project.id, [])]) for project, rows in targets],
        [
            (project, [row for row in rows if row.row.id not in moved])
            for project, rows in lingering
        ],
    )


def _remove_lingering_windows(
    lingering: Sequence[tuple[ProjectInfo, list[Placement]]],
    answering: Mapping[_Address, bool],
    report: ShutdownReport,
) -> None:
    """Remove the dead windows of ``lingering``'s rows (:func:`_lately_ended`) where their
    launch records place them: a session only such windows held, which no live row's record
    names, stood under "✓ fleet shut down" (review of the fold). A pane that is not dead,
    or gone, or on a server younger than its record, is left alone; a dead one tmux would
    not remove is reported, the project not confirmed down (review r7 of #113, finding 9)."""
    for project, rows in lingering:
        for placed in _placed_only(rows):
            pane = placed.pane
            if not answering.get(pane.address):
                continue
            try:
                facts = pane.facts_or_raise()
                if facts is None or not facts.dead or pane.outlived(pane.server().started_at()):
                    continue
                pane.kill()
            except TmuxError as exc:
                where = placed.session.name if placed.session is not None else "*"
                report.sessions_failed.append(
                    f"{pane.address}:{where} (the dead window {pane.pane_id} of "
                    f"{placed.row.label} could not be removed: {exc})"
                )
                _not_down(report, project.id)


def _take_down_session(
    session: PlacedSession,
    project_id: str | None,
    spared_panes: Spared,
    report: ShutdownReport,
    *,
    expected: bool,
    absent_is_news: bool | None = None,
    failure_note: str | None = None,
) -> None:
    """Take one of the fleet's sessions down, or say why it stands — the ONE sequence.

    Presence first, as tmux CONFIRMS it (and, for a session a record names, on the server
    that record was written on). A name asked for just-in-case (``expected=False``) that
    cannot be asked is not a failed kill and marks nothing not-confirmed-down: nothing was
    claimed to be there. Round 4 silenced only the confirmed-absent answer while widening
    the probe to every in-scope project, so a transient ``TmuxError`` on a socket the
    project never lived on read as PARTLY shut down and exit 1 (round 5). A session that
    IS there is spared when one of its panes is a row this run left live —
    killing it would end an agent tmux had just confirmed alive — and otherwise
    killed; a refused kill is a failed one, never a killed one.

    Written once for the kill phase and for the late-exit reconciliation: the
    two copies had already drifted, the spare rule living in one and not the
    other (review of #121, round 9; round 6 of #203). The late path expects its
    session (its pane was just removed, so a refusal to re-check IS a failed
    kill) but an absent one is the ordinary shape there — the window took the
    session with it — hence ``absent_is_news`` apart from ``expected``.
    ``failure_note`` is the caller's word for a question tmux would not answer.
    """
    if absent_is_news is None:
        absent_is_news = expected
    socket = session.address
    qualified = f"{socket}:{session.name}"
    try:
        present = session.present_or_raise()
    except TmuxError as exc:
        if not expected:
            return
        report.sessions_failed.append(
            f"{qualified} ({failure_note or 'could not be asked'}: {exc})"
        )
        _not_down(report, project_id)
        return
    if not present:
        if absent_is_news and qualified not in report.sessions_absent:
            report.sessions_absent.append(qualified)
        return
    if any(spared_socket in (socket, None) for spared_socket, _ in spared_panes):
        try:
            hosted: Spared = {
                (where, window.pane_id)
                for window in session.panes_or_raise()
                for where in (socket, None)
            }
        except TmuxError as exc:
            # Cannot tell whether a left-live pane lives here: not killed, and
            # said so — never a kill on a guess. The session IS there, so this
            # is a real failed kill whatever brought it here.
            note = failure_note or "could not list its panes"
            report.sessions_failed.append(f"{qualified} ({note}: {exc})")
            _not_down(report, project_id)
            return
        if hosted & spared_panes:
            if qualified not in report.sessions_left_up:
                report.sessions_left_up.append(qualified)
            _not_down(report, project_id)
            return
    try:
        session.kill()
    except TmuxError:
        # Tried and did not happen: a wedged server times out at 30 s, and tmux
        # can leave PATH between the probe and the kill.
        report.sessions_failed.append(qualified)
        _not_down(report, project_id)
    else:
        if qualified not in report.sessions_killed:
            report.sessions_killed.append(qualified)


def _not_down(report: ShutdownReport, project_id: str | None) -> None:
    """Record that ``project_id`` was NOT confirmed down (once, in order)."""
    if project_id is not None and project_id not in report.incomplete_projects:
        report.incomplete_projects.append(project_id)


def _release_or_say(
    store: ContextStore, agent: FleetAgent, *, why: str
) -> tuple[list[TeamTask], str | None]:
    """Release the ended ``agent``'s session's claims, or say why that did not happen.

    The one shape behind ``stop`` and the shutdown helpers: the row is ended
    and the pane dead by the time this runs, so a store that refuses the
    release is the courtesy owed after the stop — reported as ``<Type>: <exc>``,
    never raised (review of the fold, twice). The releases themselves are
    committed before their board events, and a failed event is not a failed
    release (``services.team._release_session``).
    """
    if agent.session_id is None:
        return [], None
    try:
        outcome = _team().release_agent_claims(store, agent, why=why)
    except Exception as exc:
        return [], (
            f"could not be released ({type(exc).__name__}: {exc}) — they stay with the ended "
            "session until the lease lapses; `aisquare task release <id>` frees one now"
        )
    if outcome.unannounced:
        # Released — the rows say so — but the board was not told, and the
        # manager learns a task is free from that event (round 5).
        return list(outcome.tasks), (
            f"released, but the board was not told about {', '.join(outcome.unannounced)} "
            f"({outcome.reason}) — the manager will not see them come free until it lists "
            "the pool"
        )
    return list(outcome.tasks), None


def _release_in(store: ContextStore, agent: FleetAgent, report: ShutdownReport) -> None:
    """RELEASE the ended agent's board session's claims, through ``store``, and count them.

    ``services.team`` names exactly this caller: a caller that "genuinely knows
    the session is dead passes ``release_claims=True``". Without it a task the
    agent held in ``doing`` stays claimed by a session that no longer exists —
    ``prune_sessions`` defaults to ``release_claims=False`` and waits
    ``_CLAIM_ORPHAN_AFTER`` (4 h) — so the next fleet's manager sees its work
    held by the dead. Under ``--force`` there is no ``/exit`` and therefore no
    ``SessionEnd`` hook to do it, and a recorded row's pane was never reached at
    all. Through the store the caller already holds open for the row it just
    ended, rather than a connection of its own per row (review of #203, round
    4). A courtesy, never the shutdown: a store that will not answer here costs
    the release, not the report — and is NAMED in it (``release_failures``),
    where a ``suppress(Exception)`` used to leave the stuck claim unmentioned.
    """
    released, failed = _release_or_say(store, agent, why="fleet shutdown")
    _count_release(report, agent, released, failed)


def _count_release(
    report: ShutdownReport | ReapReport,
    agent: FleetAgent,
    released: list[TeamTask],
    failed: str | None,
) -> None:
    """One place that turns a release (or what went wrong with it) into a report's fields."""
    report.claims_released.extend(task.id for task in released)
    if failed:
        report.release_failures.append(f"{agent.label}: {failed}")


def _record_lost(agent: FleetAgent, reason: str, report: ShutdownReport) -> None:
    """End one row with no exit status, on the operator's word, saying why."""
    try:
        with store_session() as store:
            store.end_fleet_agent(agent.id, exit_status=None, ended_by="stopped")
            # Down as the shutdown wanted it, even when a listing recorded it first (m2).
            ended = store.mark_fleet_agent_stopped(agent.id)
            _release_in(store, ended, report)
    except Exception as exc:  # a vanished row (KeyError), a locked store
        report.failed.append(ShutdownRow(agent, f"its row could not be ended ({exc})"))
        _not_down(report, agent.project_id)
        return
    report.recorded.append(ShutdownRow(ended, reason))


def _record_self_exit(agent: FleetAgent, dead_status: int | None, report: ShutdownReport) -> None:
    """End a late row whose pane tmux still holds DEAD, keeping the status tmux kept.

    A spawn that landed AFTER the kill phase brought a session of its own up and
    then EXITED on its own before this reconciliation; ``remain-on-exit`` keeps
    its pane with the real exit status. Ending it through :func:`_record_lost`
    discarded that status (``exit_status=None``) and told the operator "its
    session was killed under it" — a kill that never targeted this late session
    (review of #121, round 8). So it is ended the way every other self-exit is
    (``reap``, :func:`_row_that_went_away`): the observed status is recorded,
    ``agent_exited`` is emitted, and the row joins ``stopped`` — whose contract
    already covers "exited on its OWN … keeps the exit status it recorded".
    """
    try:
        with store_session() as store:
            # Down as the shutdown wanted it: not one for Restart all to bring back (#38),
            # even when a listing recorded its death first and kept that end (m2).
            store.end_fleet_agent(agent.id, exit_status=dead_status, ended_by="stopped")
            ended = store.mark_fleet_agent_stopped(agent.id)
            _release_in(store, ended, report)
            _emit_exit(store, ended)
    except Exception as exc:  # a vanished row (KeyError), a locked store
        report.failed.append(ShutdownRow(agent, f"its row could not be ended ({exc})"))
        _not_down(report, agent.project_id)
        return
    report.stopped.append(ended)


def _row_that_went_away(agent: FleetAgent, reason: str, report: ShutdownReport) -> None:
    """``stop`` raised ``NoSuchAgent``: the row is no longer LIVE, so re-read it.

    The agent exited on its own between the snapshot and its turn and its own
    ``SessionEnd`` (or a ``reap``) ended the row — with its real exit status.
    Calling that "left live" would be false in every part: the row IS ended,
    nothing needs stopping, and one self-exit would make a clean shutdown exit 1.
    Calling it "recorded lost" would throw away the status the row already has.
    Only a row that is somehow still live falls through to ``failed``.
    """
    try:
        with store_session() as store:
            current = store.get_fleet_agent(agent.id)
            if current is not None and current.ended_at is not None:
                # Its own hook or a `reap` released already; this finds nothing
                # more, and says so through the same count if it does.
                _release_in(store, current, report)
    except Exception:  # the store is the only witness; without it, claim nothing new
        current = None
    if current is not None and current.ended_at is not None:
        report.stopped.append(current)
        return
    report.failed.append(ShutdownRow(agent, reason))
    _not_down(report, agent.project_id)


@dataclass(frozen=True)
class _Verdict:
    """What ``shutdown`` does with one row (:func:`_shutdown_verdict`)."""

    end: Literal["stop", "live", "lost"]
    """``stop`` it through its server; leave it ``live``; or record it ``lost`` unstopped."""
    reason: str = ""
    """Why, as the run reports it: a row left live's reason (``ShutdownReport.failed``), or
    the one a row recorded lost is ended with (``ShutdownReport.recorded``)."""
    plan: str = ""
    """Why, as the plan shows it (``ShutdownPlan.held``, ``ShutdownPlan.lost_why``)."""
    held: bool = False
    """A row left ``live`` as one whose agent may run where no tmux client reaches it
    (:attr:`ShutdownRow.held`): never one held as ``"answers"``, a server that came up on
    its socket since the probe, where its pane may be."""


def _shutdown_verdict(
    agent: FleetAgent,
    address: _Address,
    answering: Mapping[_Address, bool],
    held: Mapping[str, tuple[SocketAnswer, str, str]],
    *,
    forget: bool,
    unusable: str | None,
) -> _Verdict:
    """What ``shutdown`` does with ``agent``'s row, the one verdict its plan shows and its run
    carries out (:func:`shutdown_plan`, :func:`_shutdown_row`), so the two cannot disagree.

    Stopped where its server answers. Recorded lost where none answers, unless its agent may
    still run (``held``, :func:`_held_rows`): left live then, saying why and what to do, or
    under ``forget`` ended on the operator's word, but never one whose socket answers (round
    28 of #257). A row held on an answering socket is one whose server is another than the
    one that answers there: left live, or ended on the word, as a row on a silent socket
    is, and never stopped through the other server. The plan decided that last case apart:
    under ``--forget`` it listed such a row neither as held nor as lost, so it said
    ``stopped (%3)``, the other server's pane id, while the run recorded it lost (review of
    #259, round 3; round 29 of #257 was the same mismatch for silent sockets).

    ``address`` is where the row's launch record places its server (:attr:`PlacedPane.address`);
    a row no record places has no verdict here (:func:`_shutdown_row`).
    """
    unreached = held.get(agent.id)
    if unusable is None and answering.get(address) and unreached is None:
        return _Verdict("stop")
    if unreached is not None and (unreached[0] == "answers" or not forget):
        said, why, way = unreached
        kept = ", which --forget does not end as a server answers there" if forget else ""
        return _Verdict(
            "live", f"{why} — left live{kept}: {way}", f"{why} — {way}", said != "answers"
        )
    if unusable is not None:
        reason = f"ended on your word (--forget), unchecked: {unusable}"
    elif unreached is None:
        reason = f"no server answered on socket '{address}'"
    else:
        reason = f"ended on your word (--forget): {unreached[1]}"
    return _Verdict("lost", reason, reason)


def _shutdown_row(
    project: ProjectInfo,
    placement: Placement,
    answering: Mapping[_Address, bool],
    report: ShutdownReport,
    *,
    force: bool,
    grace: float,
    unusable: str | None = None,
    held: Mapping[str, tuple[SocketAnswer, str, str]] | None = None,
    forget: bool = False,
    beside: Sequence[Placement] = (),
) -> None:
    """Stop one row, leave it live or record it lost, as :func:`_shutdown_verdict` says, the
    verdict the plan showed: never ending one whose socket answers, as a server listens
    there, which is no word's to deny, and ``uninstall`` counts it, so ending it let the
    next ``uninstall`` go on under it (round 28 of #257).

    ``stop``'s refusal is not swallowed. It refuses on exactly one verdict —
    ``_verify_gone`` looked again and the pane is VERIFIABLY still alive — which
    is a positive observation, not an absence of evidence, and ending the row on
    it is what ``stop``'s own docstring exists to prevent: "a row ended for a
    process still running is an agent no live listing shows and no ``stop`` can
    address, and the operator was told 'stopped'". Its message becomes the row's
    reason instead, and the row stays live. ``NoSuchProject`` and
    ``FleetUnavailable`` are carried the same way rather than vanishing into a
    bare ``pass``; ``NoSuchAgent`` — the agent exited on its own between the
    snapshot and its turn — goes through :func:`_row_that_went_away`, which reads
    what actually became of the row instead of guessing. ``unusable`` is why tmux could
    not be used under ``--forget`` (:func:`_shutdown_tmux`): no pane was asked, and only a
    row whose socket answers is kept.

    The row is stopped where its launch record places it (``placement``). One no record
    places is stopped only in a session a record of its project names (:func:`_stop_row`,
    ``beside`` being the project's rows in the snapshot); refused, it is left live with
    the reason, or under ``forget`` ended on the operator's word, as a row the fleet
    cannot reach is (review r7 of #113, finding 1).
    """
    agent = placement.row
    if isinstance(placement, Unplaced):
        if unusable is not None:
            _record_lost(agent, f"ended on your word (--forget), unchecked: {unusable}", report)
            return
        if _cannot_run(agent):
            _record_lost(agent, _from_before_this_boot(placement), report)
            return
    verdict = (
        _shutdown_verdict(
            agent,
            placement.pane.address,
            answering,
            held or {},
            forget=forget,
            unusable=unusable,
        )
        if isinstance(placement, Placed)
        else _Verdict("stop")  # stopped only where `_stop_row` finds its dead window
    )
    if verdict.end == "live":
        report.failed.append(ShutdownRow(agent, verdict.reason, held=verdict.held))
        _not_down(report, agent.project_id)
        return
    if verdict.end == "lost":
        _record_lost(agent, verdict.reason, report)
        return
    try:
        # The snapshot's row is the row stopped — THAT pane, whatever label a
        # later spawn reused. Whether it ended meanwhile is judged inside the
        # store session that ends it (`_stop_row` raises `NoSuchAgent` there),
        # not in a connection of its own a window ahead of it (round 5).
        receipt = _stop_row(
            project, agent, force=force, grace=grace, placement=placement, beside=beside
        )
    except NoSuchAgent as exc:
        _row_that_went_away(agent, str(exc), report)
    except FleetError as exc:
        if isinstance(placement, Unplaced) and forget:
            _record_lost(agent, f"ended on your word (--forget): {exc}", report)
            return
        report.failed.append(ShutdownRow(agent, str(exc)))
        _not_down(report, agent.project_id)
    except Exception as exc:  # a store write, a codename race — the kill phase still runs
        report.failed.append(ShutdownRow(agent, f"{type(exc).__name__}: {exc}"))
        _not_down(report, agent.project_id)
    else:
        # `stop` released what the ended session held and says what; counted
        # here rather than released again (review of #203, round 4).
        report.stopped.append(receipt.agent)
        _count_release(report, receipt.agent, receipt.released, receipt.release_failed)


def _cannot_run(row: FleetAgent) -> bool:
    """Whether the agent of ``row``, which no launch record places, cannot be running:
    spawned in another boot, or, recording none, before this boot started with its socket
    gone (``tmux.server_may_run``, the rule ``uninstall`` asks). Only ever a reason to END
    the row, never to type into or kill anything: a sandbox that wrote these columns
    could end its row itself."""
    with suppress(TmuxError):
        return (
            tmux_core.server_may_run(
                server(), row.socket_path, row.boot_id, row.created_at, row.server_started_at
            )
            is None
        )
    return False


def _from_before_this_boot(placement: Unplaced) -> str:
    return f"{placement.why}, and it cannot be running: it was spawned before this boot"


def _retire_late_panes(
    late: Sequence[tuple[ProjectInfo, Placed]],
    report: ShutdownReport,
    spared_panes: Spared,
) -> None:
    """Take down what a row that exited LATE left standing, or report what survives.

    ``remain-on-exit`` keeps the pane of an agent that exited on its own, so its
    window — and the session holding that window, and the server behind the
    session — all outlive the row :func:`_record_self_exit` just ended. The kill
    phase ran BEFORE that row existed, so nothing else ever takes them down:
    measured on real tmux, status 42 was recorded correctly while the fleet's
    session and its server stayed up under a report that read "✓ fleet shut down"
    (review of #121, round 9).

    So the pane is removed and its session reconciled — with the kill phase's own
    spare rule, because a session can outlive the pane by holding a row this run
    LEFT LIVE, and killing it there would end an agent tmux had just confirmed
    alive. Anything tmux will not confirm gone is reported, never assumed: a kill tmux
    refused included (review r7 of #113, finding 9). Only rows their launch records place
    come here, with the session the record names by tmux's id: a late row no record
    places is left live and reported (:func:`_record_late_rows`), never taken down by
    ``asq-<codename>``, a name read from the store (r7, finding 6).
    """
    if not late:
        return
    sessions: list[tuple[PlacedSession, str]] = []  # (session, project id)
    for project, placed in late:
        pane = placed.pane
        name = placed.session.name if placed.session is not None else "*"
        try:
            pane.kill()
        except TmuxError as exc:
            report.sessions_failed.append(
                f"{pane.address}:{name} (the dead pane {pane.pane_id} of a row that "
                f"exited late could not be removed: {exc})"
            )
            _not_down(report, project.id)
            continue
        if placed.session is not None and (placed.session, project.id) not in sessions:
            sessions.append((placed.session, project.id))
    for session, project_id in sessions:
        # The one take-down sequence. The session IS expected — its pane was just
        # removed, so a refusal to re-check is a failed kill — but an absent one is
        # the ordinary shape here: the window took the session with it, as tmux does.
        _take_down_session(
            session,
            project_id,
            spared_panes,
            report,
            expected=True,
            absent_is_news=False,
            failure_note="could not be re-checked after a row exited late",
        )


def _record_late_rows(
    project: ProjectInfo | None,
    handled: Collection[str],
    report: ShutdownReport,
    config: FleetSettings,
    *,
    snapshot: Sequence[tuple[ProjectInfo, list[Placement]]] = (),
) -> None:
    """Rows that appeared DURING the run, re-read on EVERY socket in scope.

    The snapshot is taken before the first stop and each stop can cost the whole
    grace, so the window is real: the manager (stopped first, to narrow it), a
    second terminal or the UI can ``fleet spawn`` into it. Such a row is in no
    snapshot, so without this pass it is neither stopped nor recorded, and its
    pane goes down with its session — a live row on a dead pane reading
    ``unknown (tmux unavailable)``, which is the bug this command was written to
    remove.

    Its pane is asked about before the row is ended: a spawn that landed AFTER
    the kill phase brought a session of its own back up, and that agent is
    running. Those stay live and are reported, never recorded. A pane tmux holds
    DEAD is ended with its status AND removed (:func:`_retire_late_panes`) —
    otherwise the window it kept would hold its session, and the session the
    server, under a report claiming the fleet is down.

    Asked where its launch record places it. A late row no record places is asked about
    nowhere: it is left live and reported, as ``stop`` refuses it (review r7 of #113,
    finding 6).
    """
    try:
        targets = _shutdown_targets(project)
    except Exception as exc:
        # A store that died mid-run: the report is still owed to the caller —
        # but a scan that did not run cannot vouch for anything. Silently
        # returning left a late-spawned agent unreported and its project
        # "down" with its pause cleared (review of #121, round 7): the failure
        # is recorded and every snapshot project stays not confirmed down.
        report.late_scan_failed = f"{type(exc).__name__}: {exc}"
        for current, _ in snapshot:
            _not_down(report, current.id)
        return
    # Reachability is re-read for every socket in scope, including the ones the
    # initial probe found absent: another terminal can bring a replacement
    # manager up on exactly that socket during the run, and skipping it left a
    # live row unreported, the project "down" and its pause cleared (review of
    # #121, round 3). `None` is "could not be asked", which is never "gone".
    fresh: dict[_Address, bool | None] = {}
    for socket in _shutdown_sockets(targets, config):
        srv = _server_at(socket, config)
        try:
            # `answers()` swallows an unavailable client into False — the same
            # answer as "no server" — and a `binary()` pre-check (round 4) only
            # covered a client `which` cannot find, not one that fails at
            # EXECUTION (deleted between check and exec, a shim whose
            # interpreter is gone). `reachable()` lets that `TmuxUnavailable`
            # through, so tmux leaving PATH after the initial guard is "cannot
            # ask", never "gone", and a late agent on that socket is not
            # recorded lost on the strength of it (review of #121, rounds 4-5).
            fresh[socket] = srv.reachable()
        except TmuxError:  # TmuxUnavailable included
            fresh[socket] = None
    late_dead: list[tuple[ProjectInfo, Placed]] = []
    for current, placements in targets:
        for placement in placements:
            agent = placement.row
            if agent.id in handled:
                continue
            if isinstance(placement, Unplaced):
                report.failed.append(
                    ShutdownRow(
                        agent,
                        f"it was spawned during the shutdown and {placement.why}, so no pane "
                        "of its may be asked about or killed — its row is left live; end it "
                        f"in its own window, then {_forget(current)} ends it",
                    )
                )
                _not_down(report, agent.project_id)
                continue
            address = placement.pane.address
            reachable = fresh.get(address)
            if reachable is None:
                report.failed.append(
                    ShutdownRow(
                        agent,
                        "it was spawned during the shutdown and tmux could not be asked whether "
                        f"a server is up on socket '{address}' — its row is left live; "
                        "run shutdown again once tmux answers",
                    )
                )
                _not_down(report, agent.project_id)
                continue
            if reachable is False:
                # The rule every other row is held to (`_unreached`), and the one verdict on
                # it (`_shutdown_verdict`), held included; never forgotten, as no plan the
                # operator saw named it (review of #259, round 7, 7/9).
                unreached = _unreached(placement)
                verdict = _shutdown_verdict(
                    agent,
                    address,
                    {address: False},
                    {} if unreached is None else {agent.id: unreached},
                    forget=False,
                    unusable=None,
                )
                if verdict.end == "live":
                    report.failed.append(
                        ShutdownRow(
                            agent,
                            f"it was spawned during the shutdown; {verdict.reason}",
                            held=verdict.held,
                        )
                    )
                    _not_down(report, agent.project_id)
                    continue
                _record_lost(
                    agent,
                    f"it was spawned during the shutdown and no server answers on socket "
                    f"'{address}' now",
                    report,
                )
                continue
            try:
                facts = placement.pane.facts_or_raise()
            except TmuxError as exc:
                # "Could not query" is not "dead" (review of #121, round 2): a
                # timeout on the pane of an agent another terminal just spawned
                # used to fall through to `_record_lost`, ending a running
                # agent's row and releasing its claims. Left live, said so.
                report.failed.append(
                    ShutdownRow(
                        agent,
                        f"it was spawned during the shutdown and tmux could not be asked about "
                        f"its pane ({exc}) — its row is left live; run shutdown again once tmux "
                        "answers",
                    )
                )
                _not_down(report, agent.project_id)
                continue
            if facts is not None and facts.pane_id == placement.pane.pane_id and not facts.dead:
                report.failed.append(
                    ShutdownRow(
                        agent,
                        "it was spawned during the shutdown and its pane is still alive — "
                        "stop it, or run shutdown again",
                    )
                )
                _not_down(report, agent.project_id)
                continue
            if facts is not None and facts.dead:
                # A late spawn that came up and then EXITED on its own before
                # this pass: tmux (remain-on-exit) still holds its pane with the
                # real status. Recording it lost discarded that status and
                # claimed a kill that never targeted this late session (review
                # of #121, round 8). `_record_lost` below is now reserved for a
                # pane that is genuinely GONE (`facts is None`).
                _record_self_exit(agent, facts.dead_status, report)
                # …and the pane tmux is still HOLDING is not left standing under a
                # report that says the fleet is down (round 9): see
                # :func:`_retire_late_panes`, run once the left-live set is final.
                late_dead.append((current, placement))
                continue
            _record_lost(
                agent, "it was spawned during the shutdown; its session was killed under it", report
            )
    placed_by_id = {
        placement.row.id: placement
        for scan in (snapshot, targets)
        for _, placements in scan
        for placement in placements
    }
    _retire_late_panes(late_dead, report, _spared_panes(report, placed_by_id))


def _clear_pause(projects: Sequence[ProjectInfo], report: ShutdownReport) -> None:
    """Clear each project's ``fleet-paused`` signal: the fleet it paused is gone.

    Board state survives shutdown by design, which is what makes this signal a
    trap rather than a nit. ``fleet pause`` then ``fleet shutdown`` used to leave
    it set, and the next ``fleet spawn manager`` came up staffing nothing — the
    manager's own instructions tell it to spawn nothing while the signal is set
    (``core/harness.py``) — with no output from either command pointing at
    ``fleet resume``. Only a signal that is actually ON is written, so a shutdown
    does not put a ``fleet-paused`` row on every project's board — and only for a
    project this run CONFIRMED down: one with a row left live, a session left up
    or a listing that failed keeps its signal and is named in ``paused_kept``.
    A signal this could not READ or WRITE is reported rather than passed over —
    the pause outlives the shutdown either way, and the operator has to know.

    ONE store connection for the whole pass. A read of the pause and ``resume`` each
    opened their own — connect, WAL switch, migrations — so an ``--all`` shutdown
    paid two opens per project here on top of the rows'; the signals are read
    and cleared through the store this holds, by project id
    (:func:`services.team.board_signals_in`, :func:`services.team.set_signals_in`),
    which also keeps the shutdown from contending with the hooks of the agents
    it is still stopping (review of #203, round 4).

    The signals are read once for the pass, and each project's pause is found among them by
    its name (:func:`_pause_where_on`), so no ``git`` runs. Its boards were worked out per
    project from its checkout's (:func:`_pause_homes`, a ``git rev-parse`` with a 5 s
    timeout), serially, for every visible project, every directory 0.8.0 captured included,
    each for a pause it almost never has (review of #259, round 30, 7/8, and round 31, 5/7).
    """
    if not orchestrator.team_enabled():
        return  # no board, no signals: nothing is paused anywhere
    team = _team()
    try:
        with store_session() as store:
            # A pause resolved through `ensure_project` CLEARED a registration's
            # tombstone (review of #121, round 7). `shutdown --all` reads
            # past tombstones on purpose (a forgotten project can hold live
            # rows), so touching the pause of one would silently undo `project
            # forget`. A forgotten project's pause is
            # nobody's standing order: skipped, not read. A CAPTURED project's
            # pause is one like any other (#139), hence `all=True`: the default
            # list would skip every pause set where nothing was ever onboarded,
            # and the shutdown would leave it on with nothing saying so.
            visible = {p.id for p in store.list_projects(all=True)}
            failures = _reconcile_pauses(store, team, projects, visible, report)
    except Exception as exc:
        # The store `_record_late_rows` already found locked is still locked (or
        # a fresh failure). The report is owed to the caller regardless, and this
        # lookup used to raise straight through it — empty stdout under `--json`
        # after agents were already stopped (review of #121, round 8). Without
        # the project list a forgotten project (whose pause is not ours to touch)
        # cannot be told from a live one, so every pause is KEPT, said so, and
        # the partial report is returned.
        report.pause_scan_failed = f"{type(exc).__name__}: {exc}"
        return
    if failures:
        report.pause_scan_failed = "; ".join(failures)


def _reconcile_pauses(
    store: ContextStore,
    team: Any,
    projects: Sequence[ProjectInfo],
    visible: Collection[str],
    report: ShutdownReport,
) -> list[str]:
    """The per-project half of :func:`_clear_pause`, of each visible project, its pause found
    by name among the signals read once for the pass (:func:`_pause_where_on`); returns what
    could not be done.

    Read/write failures are surfaced the way the lookup's is. A
    blanket ``suppress(Exception)`` here left the signal ON with
    ``paused_kept=[]``, ``pause_scan_failed=null`` and exit 0 — the next manager
    still under orders to spawn nothing, and nothing in the report saying so
    (review of #121, round 9). A disabled board is NOT one of these: it holds no
    signals (nothing can be paused where no signal can exist), so an ordinary
    run on a project with no board still reports clean.
    """
    failures: list[str] = []
    unread: Exception | None = None
    unreadable: list[tuple[str, str]] = []
    try:
        signals = team.board_signals_in(store, unreadable=unreadable)
    except team.TeamDisabledError:
        return failures  # no board, no signals: nothing is paused anywhere
    except Exception as exc:
        signals, unread = {}, exc
    for project in projects:
        if project.id not in visible:
            continue
        if unread is not None:
            # The signals could not even be READ, so nothing is known about this
            # project's: the failure is named and the project is not confirmed down,
            # and neither `paused_kept` nor `paused_cleared` claims anything.
            why = f"{type(unread).__name__}: {unread}"
            failures.append(f"{_name(project)}: could not be read ({why})")
            report.pause_unsettled.append(project)
            _not_down(report, project.id)
            continue
        # A blob of its own pause that does not parse: whether it is on is not known. Another
        # project's is no concern of this one's (review of #259, round 32, 6/9).
        broken = _pause_where_on(project, unreadable)
        if broken:
            named = ", ".join(f"{name} on {board}" for board, name in broken)
            failures.append(f"{_name(project)}: could not be read ({named})")
            report.pause_unsettled.append(project)
            _not_down(report, project.id)
            continue
        # Its own pause wherever it is on: under a hub, never another project's on the board.
        paused = _pause_where_on(project, signals)
        if not paused:
            continue
        if project.id in report.incomplete_projects:
            # A surviving or unverified agent keeps its standing order: the
            # manager's "spawn nothing while fleet-paused" is exactly the
            # instruction wanted while the operator is trying to stop it
            # (review of #121, round 2).
            report.paused_kept.append(_name(project))
            continue
        try:
            _clear_pause_signal(store, paused)
        except Exception as exc:
            # The signal was just read ON and the clear did not happen: it IS
            # kept, so it is named as kept as well as reported failed.
            failures.append(f"{_name(project)}: could not be cleared ({type(exc).__name__}: {exc})")
            report.pause_unsettled.append(project)
            report.paused_kept.append(_name(project))
            _not_down(report, project.id)
            continue
        report.paused_cleared.append(_name(project))
    return failures


def shutdown_plan(project: ProjectInfo | None = None, *, forget: bool = False) -> ShutdownPlan:
    """What :func:`shutdown` would end — read-only, and it refuses the same way.

    The guards run here too, so the plan an operator is asked to confirm is one
    that can actually be carried out: no usable tmux (unless ``forget``), a probe
    that cannot be asked, or a call from inside the fleet's own server all refuse
    before anything is printed. A session query that fails AFTER a good probe refuses
    the same way: the operator confirms this list, so a list missing a session
    the run would still kill is worse than no list at all.

    Each row's disposition is the run's own verdict (:func:`_shutdown_verdict`), so what
    the plan says it stops, leaves live or records lost is what the run does with it.
    """
    config = settings()
    unusable = _shutdown_tmux(config, forget=forget)
    targets = _shutdown_targets(project)
    if unusable is None:
        targets, _ = _still_running(targets, _lately_ended(targets), say=False)
    rows = [placement.agent for _, placements in targets for placement in placements]
    if unusable is not None:
        # Every row but those whose socket answers is ended unchecked (`_record_lost`), so
        # the plan says so: a script reading `--json`'s `lost` saw nothing being ended
        # (round 29 of #257). Nothing is asked of tmux, and no session is killed.
        return _planned(
            ShutdownPlan(
                projects=[current for current, placements in targets if placements],
                agents=rows,
                tmux_unusable=unusable,
            ),
            targets,
            {},
            _held_rows(targets, {}, unusable=True),
            forget=forget,
            unusable=unusable,
        )
    sockets = _shutdown_sockets(targets, config)
    _refuse_from_inside(sockets, config)
    answering = _shutdown_probe(sockets, config)
    sessions: list[str] = []
    try:
        # The STRICT check, and no suppression around the enumeration. The
        # lenient `has_session` reads EVERY non-zero exit as "not there", and
        # `suppress(TmuxError)` dropped a whole socket's listing: with two
        # sessions and one transient failure the operator confirmed a plan
        # naming one session and the run then killed both, and a scope holding
        # only the inaccessible session read "nothing to shut down" (review of
        # #121, round 9). A plan that cannot be completed is refused, not
        # silently shortened — nothing has been touched at this point. The sessions
        # are the ones the run takes down (`_kill_fleet_sessions`): those the live
        # rows' launch records name, by tmux's id, and under `--all` every `asq-`
        # session tmux lists.
        taken: dict[_Address, set[str]] = {}
        for current, placements in targets:
            for session in _project_sessions(current.id, placements, config, every_legacy=True):
                if answering.get(session.address) and session.present_or_raise():
                    sessions.append(f"{session.address}:{session.name}")
                    taken.setdefault(session.address, set()).add(session.name)
        if project is None:
            for socket in sockets:
                if not answering[socket]:
                    continue
                skip = taken.get(socket, set())
                for swept in _swept_sessions(_server_at(socket, config), socket, config, skip=skip):
                    sessions.append(f"{socket}:{swept.name}")
    except TmuxError as exc:
        raise FleetError(
            f"could not list the fleet's sessions ({exc}) — refusing to show a plan that may "
            "be short of what a shutdown would kill; try again once tmux answers"
        ) from exc
    return _planned(
        ShutdownPlan(
            projects=[current for current, placements in targets if placements],
            agents=rows,
            sessions=sessions,
            absent_sockets=[str(socket) for socket in sockets if not answering[socket]],
        ),
        targets,
        answering,
        _held_rows(targets, answering),
        forget=forget,
        unusable=None,
    )


def _where(placement: Placement) -> str:
    """Where a plan says a row is asked: its record's server, or, for a row no record
    places, that none does."""
    if isinstance(placement, Placed):
        return str(placement.pane.address)
    return f"nowhere: {placement.why}"


def _planned(
    plan: ShutdownPlan,
    targets: Sequence[tuple[ProjectInfo, list[Placement]]],
    answering: Mapping[_Address, bool],
    held: Mapping[str, tuple[SocketAnswer, str, str]],
    *,
    forget: bool,
    unusable: str | None,
) -> ShutdownPlan:
    """``plan`` with each of its rows' verdict (:func:`_shutdown_verdict`): those the run
    leaves live in :attr:`ShutdownPlan.held`, those it records lost in
    :attr:`ShutdownPlan.lost`, each with why; every other row is stopped. A row no launch
    record places is recorded lost unchecked where tmux cannot be used, or when its agent
    cannot be running; otherwise it is stopped where :func:`_stop_row` finds its window,
    as :func:`_shutdown_row` does."""
    for _, placements in targets:
        for placement in placements:
            agent = placement.row
            if isinstance(placement, Unplaced):
                if unusable is not None:
                    why = f"ended on your word (--forget), unchecked: {unusable}"
                elif _cannot_run(agent):
                    why = _from_before_this_boot(placement)
                else:
                    continue
                plan.lost[agent.id] = _where(placement)
                plan.lost_why[agent.id] = why
                continue
            verdict = _shutdown_verdict(
                agent,
                placement.pane.address,
                answering,
                held,
                forget=forget,
                unusable=unusable,
            )
            if verdict.end == "live":
                plan.held[agent.id] = verdict.plan
            elif verdict.end == "lost":
                plan.lost[agent.id] = _where(placement)
                plan.lost_why[agent.id] = verdict.plan
    return plan


def shutdown(
    project: ProjectInfo | None = None,
    *,
    force: bool = False,
    grace: float = 5.0,
    forget: bool = False,
) -> ShutdownReport:
    """Stop this project's live agents (every project's with ``project=None``).

    The one place a row may be ended without tmux confirming its pane died. Every
    other path — ``stop``, ``reap`` — refuses that: a server that cannot be reached
    is not evidence of a dead pane, and ending a live agent's row hides it from
    every listing and hands its worktree to ``_remove_finished_worktrees``. Here the
    ambiguity is resolved by the operator, not by inference: they have ordered the
    fleet DOWN. Measured 2026-09-10 without this command — the operator ran ``tmux
    -L asq kill-server`` by hand (the only off switch they had), every manager
    row kept saying ``unknown (tmux unavailable)``, ``reap`` reaped 0 (correctly:
    it could not ask), and the UI showed dead managers for as long as they cared
    to look.

    Scope is ``reap``'s: this project by default, every project when ``project``
    is ``None``. An operator with two projects running who means "take this
    project's fleet down" must not lose the other project's agents, and the CLI
    asks before either.

    Order matters and is deliberate: agents on an answering server get the
    ordinary ``stop`` (graceful ``/exit`` unless ``force``, ``agent_exited``
    emitted, claims released) BEFORE their session is killed, so the graceful
    path's ``SessionEnd`` hook still runs. Rows on a socket with no server —
    killed outside the CLI, or one the config no longer names — are ended with no
    exit status and counted apart, each with the reason, so the report never
    reads "stopped" over a process nobody confirmed stopping.

    **Exit status is recorded where tmux exposes one, which ``force`` usually
    means it does not**: ``force`` skips the ``/exit`` and kills a live pane, and
    a status is only ever read from a pane that already reads dead. A forced row
    lands in ``stopped`` with ``exit_status`` ``None`` unless its agent had
    already exited on its own.

    What is killed is the fleet's own SESSIONS, never the server (see
    :func:`_kill_fleet_sessions`). Board tasks and notes are kept, but the ended
    rows' claims are released — a task held by a session that no longer exists is
    not "untouched", it is stuck — and a ``fleet-paused`` signal is cleared for
    every project this run confirmed down, because the fleet it paused is gone;
    a project with a surviving or unverified agent keeps it.

    Each row's server is asked where it said at spawn that it listened (:func:`_address`),
    whatever ``TMUX_TMPDIR`` this shell has: ``-L`` from here can reach another server on
    the same name, whose pane ids name other agents (round 28 of #257). A row on a socket
    with no server whose agent may still run — its socket file gone from under its server
    — is not ended: the rule ``uninstall`` asks (:func:`_unreached`) still counts it, so
    ending it stopped nothing and let the next ``uninstall`` go on under it. It is left
    live with what to do (round 27 of #257).

    ``forget`` is the operator's word that the agents it cannot reach are not running:
    such rows are ended as lost, but for a row whose socket answers, where a server
    listens. Where tmux cannot be used here every other row is ended unchecked
    (:func:`_shutdown_tmux`), with nothing stopped or killed: a socket is asked without
    tmux (round 28 of #257).
    """
    config = settings()
    report = ShutdownReport()
    # Where the run is, for the one sentence an interrupt owes: every
    # KeyboardInterrupt inside this function — before the first row, during a
    # row's stop, in the kill phase, the late scan or the pause pass — raises
    # `FleetInterrupted(report)` with the report of how far it got. A Ctrl-C in
    # the slow half used to escape as click's `Aborted!` with nothing printed,
    # after every row was ended and every claim released (rounds 5 to 7).
    phase = "before anything was stopped"
    try:
        unusable = _shutdown_tmux(config, forget=forget)
        targets = _shutdown_targets(project, say=True)
        lingering = _lately_ended(targets)
        if unusable is None:
            targets, lingering = _still_running(targets, lingering, say=True)
        sockets = _shutdown_sockets(targets, config)
        held: dict[str, tuple[SocketAnswer, str, str]] = {}
        if unusable is None:
            _refuse_from_inside(sockets, config)
            answering = _shutdown_probe(sockets, config)
            report.servers_absent.extend(str(socket) for socket in sockets if not answering[socket])
            held = _held_rows(targets, answering)
        else:
            answering = dict.fromkeys(sockets, False)  # none asked: nothing stopped or killed
            held = _held_rows(targets, answering, unusable=True)
        handled: set[str] = set()

        def finish() -> None:
            nonlocal phase
            phase = "during the kill phase"
            _kill_fleet_sessions(
                targets,
                sockets,
                answering,
                report,
                config,
                every=project is None,
                lingering=lingering,
            )
            phase = "during the final scan for rows spawned during the shutdown"
            _record_late_rows(project, handled, report, config, snapshot=targets)
            phase = "while reconciling the fleet-paused signals"
            _clear_pause([current for current, _ in targets], report)

        try:
            for current, placements in targets:
                for placement in placements:
                    phase = f"while stopping {placement.row.label}"
                    handled.add(placement.row.id)
                    _shutdown_row(
                        current,
                        placement,
                        answering,
                        report,
                        force=force,
                        grace=grace,
                        unusable=unusable,
                        held=held,
                        forget=forget,
                        beside=placements,
                    )
        except Exception:
            # The kill phase runs even if a row could not be recorded: "shutdown
            # means down" must hold when the store refuses, and a caller that got
            # a traceback instead of a report was left with a half-recorded
            # fleet, a live server, and nothing saying how far it got.
            # `_shutdown_row` catches its own, so this is the belt for a fault in
            # the loop itself — an `Exception` only, never the operator's
            # interrupt, which the handler below keeps for them.
            finish()
            raise
        finish()
    except KeyboardInterrupt:
        # The operator's interrupt is theirs to have — nothing further is killed
        # — but rows before it are ended and their claims released, each
        # committed as it went, so the report of how far it got travels with the
        # interrupt instead of being thrown away with it.
        report.interrupted = f"interrupted {phase}; the rest of the fleet was left as it was"
        raise FleetInterrupted(report) from None
    return report


def _verify_gone(look: Callable[[], WindowInfo | None], label: str, cause: TmuxError) -> int | None:
    """A stop step failed: end the row only if the pane is VERIFIABLY dead or gone.

    ``TmuxError`` is not proof of death. A 30 s command timeout on a wedged
    server is one, and so is a tmux that left ``PATH`` — in both, the server
    and the pane may be perfectly alive, so one more look decides. What the old
    fail-open cost when that look would have said "alive": a row marked ended
    for a running process (invisible to every live listing, addressable by no
    ``stop``, holding its claims and its worktree) and a ``✓ stopped`` printed
    over it. A pane that answers "gone" or "dead" ends the row as before.
    """
    try:
        window = look()
    except TmuxError as exc:
        raise FleetError(
            f"tmux could not be asked whether {label!r} stopped ({exc}) — it may still be "
            "running, so its row is left live; try again once tmux answers"
        ) from cause
    if window is not None and not window.dead:
        raise FleetError(
            f"could not stop {label!r} ({cause}) — its pane is still alive, so its row is "
            "left live; try again, or `--force` once tmux is healthy"
        ) from cause
    return window.dead_status if window is not None else None


def switch(
    project: ProjectInfo,
    label: str,
    *,
    to: str | None = None,
    fresh: bool = False,
    reason: str | None = None,
    spawned_by: str = "user",
    automatic: bool = False,
    agent_id: str | None = None,
    before_stop: Callable[[], None] | None = None,
) -> SwitchReceipt:
    """Move a running agent to another Claude account — the hand-over of #146.

    The target is ``to`` (a slot, alias or email) or, without it, HEADROOM
    FIRST among the enabled, signed-in accounts that are NOT the one the agent
    is on (``services.claude_accounts.choose_for_handover``): the role binding
    and the project default are launch-time choices and do not get to send a
    limited agent somewhere just as full. ``automatic`` is the hook's
    hand-over: it takes only an account under ``switch_at`` and otherwise
    refuses — the least-bad answer, applied twice, moved an agent between two
    exhausted accounts for the whole window (review of #205, second round). A
    manual switch keeps the least-bad answer and falls back to the ladder
    (minus the account being left) when no usage can be read.

    The agent is stopped the way ``fleet stop`` stops it (``/exit``, a grace,
    then the kill) and started again under the same label, role, task and
    worktree on the target account. When its transcript is on disk and
    ``fresh`` is not asked, the replacement RESUMES it: ``claude --resume
    <transcript path>`` keeps the session id, so the board row, its cursor, its
    notes AND ITS CLAIMS carry on — the session is marked ``HANDOVER_STATE``
    before the ``/exit``, so its own ``SessionEnd`` parks the claims for the
    same id instead of releasing them (the mark is taken back as soon as
    ``stop`` returns or raises), and ``stop`` announces no exit — and the
    resumed agent is told, in one typed line, to continue (``claude --resume``
    opens the conversation at an idle prompt; review of #205, findings 5 and 6).
    Otherwise it starts fresh with a hand-off prompt built from the board — the
    task, its detail, the session's last notes — and is told to read the board
    and the working tree before continuing. A fresh start is a hand-over too:
    the session is marked the same way, no exit is announced, and the claims
    move onto the new session's id with the row (``spawn(takes_over=…)``).

    Run from the automatic path it is a DETACHED worker, not the limited
    agent's hook: inline, the ``/exit`` queued behind the still-running hook
    and the window kill took the hook — and this function — down before
    ``spawn`` ran (finding 1; ``services.hooks._detach``).

    A CODEX agent moves between Codex accounts, FRESH across homes: its thread is a
    rollout under the old ``CODEX_HOME``, which a session on another home cannot
    resume (``capabilities.resumes_across_homes``), so the replacement starts with the
    hand-off prompt and takes the claims over, as ``--fresh`` does for Claude Code — its
    first hook under the new window adopts them (``services.team``'s rule 1). A target
    whose home is the one the session ran under resumes ``codex resume <id>``.

    What is deliberately NOT promised: that Claude Code resumes a transcript
    written under one ``CLAUDE_CONFIG_DIR`` from another. The path form is
    documented and the id is preserved, but this machine has one login, so the
    cross-account leg is unverified; a resume that fails leaves a pane whose
    error is visible, and ``--fresh`` is the documented way around it. What
    ``spawn`` would refuse without the stop — a replay that cannot start
    (:func:`_refuse_a_replay_that_cannot_start`), the role, a task that is
    closed — is refused before the agent is stopped, as :func:`restart`
    refuses it.

    ``agent_id`` pins the row, as for :func:`stop` and :func:`restart`: the
    phone's Switch means the agent it showed, and a manager's switch or the
    automatic hand-over runs in another process, which can hand the label to
    a replacement between the phone's check and this read. By label, that
    replacement was stopped and moved again (sweep of #243). ``before_stop``
    is the caller's last check, asked once every refusal above has passed and
    before anything is marked or stopped. It raises to refuse, and nothing has
    been done then. The phone's dialog guard is one: the Escape it may send
    must not come before a refusal this function makes up front, such as a
    ``to`` that names no account.
    """
    with store_session() as store:
        agent = _live_agent(store, project, label, agent_id=agent_id)
        # First: every refusal after this one reads the role, the agent and the account,
        # and they are the launch record's (`core.launch_seal`), never the row's.
        record = _launch_record(project, agent, "switch")
        session = store.get_session(agent.session_id) if agent.session_id else None
        if _handed_over(agent, session, _now()):
            # The automatic path refuses a session already in flight
            # (`services.hooks.hand_over`); a switch by hand, or a restart, did not. The
            # second stopped the row itself — its pane already dead of the first one's
            # `/exit` — and started a replacement of its own, the first failed on its
            # own row, and its take-back wrote a stale state over the second's mark
            # (review of #163, round 2).
            raise _in_hand_over(project, "switch", label)
        # `spawn`'s refusal for the task (done, dropped, gone from the board), asked
        # before anything is stopped, as `restart` asks it. After the stop it left the
        # agent stopped and nothing started: a row spawned before rule 3 still names
        # its closed task until its next briefing, and switching one always lost it
        # (review of #203, final round, FLEET-3).
        # On the board the replacement will read (`_board_and_hub`), which a row 0.8.0
        # spawned may not have read: asked after the stop, the task was refused with the
        # agent already down (review of #259, round 27, 2/7).
        board_and_hub = _board_and_hub(store, project, agent, session)
        task = _task_for(store, board_and_hub[0], agent.task_id)
        recent = _handoff_events(store, agent)
    # The captain's row first: off, its role is unknown (``launch._role_ok``), and the one
    # line every captain refusal says is the answer, not "unknown role" (review of #240, F2).
    _refuse_the_captain_while_off(agent)
    _require_role(record.role)  # `spawn`'s first refusal, before the stop as the task's
    try:
        _refuse_a_replay_spawn_refuses(project, agent, record.role)
    except FleetError as exc:
        raise FleetError(f"cannot switch {label!r}: {exc}") from exc
    current = record.account_slot
    notes: list[str] = []
    failures: list[str] = []  # the notes that say what did not happen (`SwitchReceipt`)
    kind = record.agent
    adapter = recorded_adapter(kind)
    # The accounts service decides, as for every launch
    # (tests/test_one_account_resolver.py): `--to` is the flag rung; without it
    # headroom decides first, with the account the agent is leaving excluded.
    try:
        choice = claude_accounts_service.choose_for_handover(
            to,
            role=record.role,
            project=project,
            exclude=() if current is None else (current,),
            automatic=automatic,
            agent=kind,
        )
    except claude_accounts_service.NoSuchAccount as exc:
        raise FleetError(str(exc)) from exc
    notes.extend(choice.notes)
    if choice.account is None:
        where = f" (it is on slot {current})" if current is not None else ""
        # With the notes, every one: what each rung skipped and why is the only
        # account of how nothing was found (fourth round). The automatic refusal
        # kept only the `headroom:` ones, so a registry it could not read went
        # unsaid there, blamed on usage, with an empty `()` (the #205 fold, round 2).
        said = f" ({'; '.join(choice.notes)})" if choice.notes else ""
        if automatic:
            raise FleetError(
                f"no account under the line for {label!r}{where} — every other account is "
                "over switch_at or unreadable; it stays parked until a reset, or "
                f"`{project_service.fleet_command('switch', project, label)}` moves it by "
                f"hand{said}"
            )
        raise FleetError(
            f"no other account with headroom for {label!r}{where}"
            " — add or enable one (`aisquare accounts`), name one with --to, or wait for "
            f"the reset{said}"
        )
    target = choice.account
    if current is not None and target.slot == current:
        raise FleetError(
            f"{label!r} already runs on {claude_accounts_core.label(target)} (slot {target.slot})"
        )
    if (
        adapter is not None
        and not adapter.capabilities.resumes_across_homes
        and not fresh
        and not _same_home(record, target)
    ):
        # Its thread lives under the old home (a Codex rollout under its CODEX_HOME): no
        # session on another home resumes it. One on the same home does.
        fresh = True
        notes.append(
            f"{claude_accounts_core.agent_label(kind)} cannot resume a thread under another "
            "account's home — the replacement starts fresh with a hand-off prompt"
        )
    _refuse_a_replay_that_cannot_start(project, record, target.config_dir, branch=agent.branch)
    if before_stop is not None:
        before_stop()
    # A hand-over whether it resumes or not: the agent is coming back, so its
    # claims wait for the replacement and no exit is announced. A fresh start
    # used to stop the agent as `fleet stop` does — the task went back to the
    # pool and `agent_exited` woke the manager while `spawn` was still starting
    # the replacement, so a second worker took the same task (review of #205,
    # fourth round). Resumed, the same id carries the claims on; started fresh,
    # `spawn` moves them onto the new session's id (`_respawn(takes_over=True)`).
    # Pinned to the row read above (`agent_id`): the headroom lookup reads every
    # account's usage over the network, and an agent that exited and was started
    # again under this label meanwhile was the one stopped, then replaced by a
    # resume of the OLD session (review of #163, round 1). That refusal is asked
    # BEFORE the mark, too: a `fleet restart` that resumed the transcript shares the
    # session id, so the mark landed on the newcomer's live session and the
    # take-back wrote this switch's stale snapshot over it (round 2).
    with store_session() as store:
        _live_agent(store, project, label, agent_id=agent.id)
    if session is not None:
        _mark_handing_over(project, agent, session, "switch", label)
    try:
        # Nobody watches the automatic one, so a dialog on the agent's screen is answered
        # with its least answer and the agent exits through its own `SessionEnd`, as
        # Remote's Switch with `dismiss_dialog` does: killed, a Codex left its session
        # parked on its limit until the reset (`_stop_row`).
        handed_over = stop(
            project, label, handover=True, agent_id=agent.id, dismiss_dialog=automatic
        )
    finally:
        # Taken back however `stop` ends. The mark has one reader, the old process's
        # own SessionEnd, and by here that has run or never will: returned, the pane
        # is confirmed dead or gone (a kill fires no hook); raised — a Ctrl-C in the
        # grace included — the session keeps the state it had. Every state writer
        # keeps the mark (`SqliteStore.touch_session`), so held any longer it
        # outlived its reader: a Ctrl-C is no `Exception` and skipped the unmark, and
        # a resumed replacement whose start hook failed open stayed `switching`
        # through every later prompt — no `limited` recorded, no hand-over, no bell
        # (review of the #205 fold, round 2).
        if session is not None and (left := _unmark_handing_over(session)) is not None:
            notes.append(left)
            failures.append(left)
    # The replacement is started from the row as the stop ENDED it, as `restart`
    # starts one, not from the snapshot read before the headroom lookup and the
    # grace: a task that closed meanwhile is gone from the row (rule 3), and
    # `spawn` refused the snapshot's closed task with the agent already stopped
    # (review of #203, final round, FLEET-3).
    stopped = handed_over.agent
    if handed_over.dismissed is not None:
        notes.append(f"it was stopped with {handed_over.dismissed}")
    whose = claude_accounts_core.kind(kind).noun
    try:
        receipt, resumed, more = _respawn(
            project,
            stopped,
            session,
            task,
            recent,
            account=str(target.slot),
            fresh=fresh,
            # What a FRESH replacement is told it takes over from: the account move is
            # the one thing it cannot read off the board.
            reason=(
                f"it stopped on {reason or 'a usage limit'} under another {whose} account"
                if reason is not None or automatic
                # A manual switch with no reason given was the operator's move, not a
                # limit's (SMOKE5 of #113).
                else f"the operator moved it from another {whose} account (`fleet switch`)"
            ),
            resume_prompt=_resume_prompt(stopped, reason, automatic=automatic),
            takes_over=True,
            spawned_by=spawned_by,
            # The hook's hand-over is nobody's add (#139): `spawn`'s `onboard`.
            onboard=not automatic,
            board_and_hub=board_and_hub,
        )
    except Exception:
        if handed_over.withheld:
            _abandon_handover(stopped)  # no replacement is coming for the parked claims
        raise
    notes.extend(more)
    failures.extend(receipt.failures)
    labels = claude_accounts_service.slot_labels() if current is not None else {}
    if current is None:
        from_name = f"its shell's {get_adapter(kind).binary}"
    else:  # by name, as the target is: an account renamed is that name here too
        from_name = (
            f"{labels[current]} (slot {current})" if current in labels else f"slot {current}"
        )
    typed = receipt.prompt_typed or "no"
    how = _how_started(resumed, typed=typed)
    why = f" ({reason})" if reason else ""
    # The suppression is entered FIRST: a store that cannot be opened is the courtesy
    # lost too, never a moved agent reported as not moved (review of #205, fourth round).
    with contextlib.suppress(Exception), store_session() as store:  # the courtesy, not the record
        _team()._emit(
            store,
            _board_of_row(receipt.agent),
            "switched",
            f"{label} moved from {from_name} to {claude_accounts_core.label(target)} "
            f"(slot {target.slot}){why} — {how}",
            session_id=receipt.agent.session_id,
        )
    return SwitchReceipt(
        stopped=stopped,
        started=receipt.agent,
        from_slot=current,
        to_slot=target.slot,
        resumed=resumed,
        tmux_session=receipt.tmux_session,
        notes=notes,
        failures=failures,
        prompt_typed=typed,
    )


def _respawn(
    project: ProjectInfo,
    agent: FleetAgent,
    session: TeamSession | None,
    task: TeamTask | None,
    recent: list[TeamEvent],
    *,
    account: str | None,
    fresh: bool,
    reason: str,
    spawned_by: str,
    size: tuple[int, int] | None = None,
    resume_prompt: str | None = None,
    takes_over: bool = False,
    permission_mode: str | None = None,
    onboard: bool = True,
    role: str | None = None,
    persona: str | None = None,
    board_and_hub: tuple[str, str],
    team_line: str | None = None,
    replaces: str | None = None,
) -> tuple[SpawnReceipt, bool, list[str]]:
    """Start ``agent`` again — same label, role, task and worktree — resuming when it can.

    ``team_line`` is a restarted manager's (:func:`_dead_team_line`, #38): its dead team and
    their restart commands, ending the first message resumed or fresh.

    Shared by :func:`switch` (another account) and :func:`restart` (the same
    one). With the session's transcript on disk and ``fresh`` not asked, the
    replacement resumes it by path and keeps the session id (``ResumeSpec``)
    and is typed ``resume_prompt``, if any; otherwise it starts new with
    :func:`_handoff_prompt`. Returns the receipt, whether it resumed, and the
    notes to show.

    ``takes_over`` is the HAND-OVER's (``switch``, #205, and the restart of a
    RUNNING agent): the old session's claims were parked for the replacement,
    so one that starts new takes them over with its row
    (``spawn(takes_over=…)``); a resumed one keeps the id and with it the
    claims. The restart of an agent that exited, or was lost, is not a
    hand-over — its claims went back with its end — so it passes neither.

    The replacement replays the row's launch spec (#144; ``spawn(spec=…)``);
    ``permission_mode`` is a restart's explicit one, which wins over the spec
    as an explicit argument wins over the config, and is what the
    replacement's spec records. ``onboard`` is :func:`spawn`'s. A worktree
    agent's replacement works in the same tree, as it stands: its branch is
    the one the agent was on, whatever its task or the codename says now
    (``spawn(own_worktree=…)``). ``board_and_hub`` is the board it reads and its window's
    hub, asked before the stop (:func:`_board_and_hub`). ``replaces`` is :func:`spawn`'s.
    """
    notes: list[str] = []
    # Read again after the stop, which can outlast its agent's last hook: the replay
    # below takes everything it runs from the record, nothing from the row.
    record = _launch_record(project, agent, "replace")
    if task is not None and agent.task_id != task.id:
        # Read before the stop, and closed during it: the row the stop ended no longer
        # names it (rule 3), and a hand-off prompt naming it set the replacement on
        # finished work.
        task = None
    resume: ResumeSpec | None = None
    if not fresh:
        resume, why_not = _resumable(record, recorded_adapter(record.agent))
        if why_not is not None:
            notes.append(f"{why_not} — the replacement starts fresh with a hand-off prompt")
    prompt = resume_prompt if resume is not None else _handoff_prompt(agent, task, recent, reason)
    if team_line is not None:
        # Resumed, one line as its prompt is (`_resume_prompt`); fresh, a line of its own.
        prompt = (
            f"{prompt} {team_line}"
            if resume is not None and prompt
            else (f"{prompt}\n{team_line}" if prompt else team_line)
        )
    # A take-over from the Spawn dialog may run the label as another role (card
    # tsk_01m3ns5a736s): the recorded launch was the old role's, so it is replayed only
    # for the same role, and the new one launches as today's config says for it, still
    # the recorded agent's (its session is the one resumed).
    replay = role is None or role == record.role
    spec = record.spec if replay else None
    chosen = _persona_for_replay(project, agent, notes, role=role) if persona is None else persona
    began_as = _seam_persona(record)
    if resume is not None and (chosen or None) != began_as:
        # A thread that keeps its first system prompt across a resume (Codex's) is not given
        # the persona this launch passes, which reaches it through the briefing alone: said
        # at every resume while the seam holds another, not only the first after a change.
        seam = personas_service.resume_seam_note(record.agent, chosen or None, began_as)
        if seam is not None:
            notes.append(seam)
    if resume is not None and session is not None and (session.persona or "") != chosen:
        # The replacement RESUMES this session, and its start hook asks the row: a row with no
        # persona asks for nothing, so the session kept the persona it recorded, and the next
        # start briefed it again. A take-over to Persona (none) went on briefing the old one
        # (review of #240, "also confirmed": take-over with Persona "(none)"). Set before the
        # window starts, so its first hook already reads it.
        with store_session() as store:
            store.set_session_persona(session.id, chosen or None)
    receipt = spawn(
        project,
        record.role if replay or role is None else role,
        label=agent.label,
        task_id=agent.task_id,
        worktree=record.worktree,
        prompt=prompt,
        spawned_by=spawned_by,
        account=account,
        resume=resume,
        size=size,
        permission_mode=permission_mode,
        spec=spec,
        persona=chosen,
        # The agent the record names, never today's resolution of the role: a `--agent
        # codex --bin <wrapper>` row came back with Claude Code's flags, and a role bound
        # to another agent since refused only after the original was stopped (r1 of #113).
        agent=record.agent,
        # The agent whose own hook reported the session, whatever its binary is called,
        # resumed or `--fresh`: evidence of what KIND of program a recorded binary that
        # left the PATH was, which only consents to today's resolution (the read-only
        # config's) taking its place, its arguments the recorded ones.
        family=record.session.agent if record.session is not None else None,
        takes_over=session.id if takes_over and resume is None and session is not None else None,
        # The captain's own folder (its brain, under the home) is replayed, as its launch
        # record holds it; every other non-worktree agent starts at its project's root, as
        # on main (review of #240, R1-F10): a row records the root it was spawned at, and a
        # project re-rooted since would have restarted its agents in the old folder.
        cwd=(
            Path(record.cwd)
            if not record.worktree and experimental.captain_role(record.role)
            else None
        ),
        onboard=onboard,
        # Asked again here, after a stop that can outlast the binary (a relink in the
        # grace): the refusal names no `--bin`, which no command that gets here takes.
        bin_flag=False,
        own_worktree=Path(record.cwd) if record.worktree else None,
        own_branch=agent.branch,
        board_and_hub=board_and_hub,
        session=(
            record.session.model_copy(update={"seam_persona": began_as or ""})
            if resume is not None and record.session is not None
            else None
        ),
        replaces=replaces,
    )
    # Said once the replacement is up, for what it really took from the record: a
    # recorded binary that has left the PATH was replaced by today's resolution, which
    # the spawn's own note names, and "the binary is the recorded one" beside it said
    # the opposite (review of #169, F7).
    if spec is not None:
        kept = [
            *(["binary"] if receipt.agent.binary == spec.binary else []),
            *(["permission mode"] if permission_mode is None else []),
        ]
        said = f"{', '.join(kept)} and arguments" if kept else "arguments"
        notes.append(
            f"launched as recorded — {said} come from its launch record, not from today's "
            "config (#144)"
        )
    notes.extend(receipt.notes)
    return receipt, resume is not None, notes


def _launched_persona(record: launch_seal.LaunchRecord) -> str | None:
    """The persona ``record``'s launch passed (``launch --persona`` in its window's argv,
    ``LaunchSpec.command``), or ``None`` for none."""
    return next(reversed(option_values(record.spec.command, "--persona")), None) or None


def _seam_persona(record: launch_seal.LaunchRecord) -> str | None:
    """The persona the launch that began ``record``'s session passed: carried with the
    session across each resume (``SessionFacts.seam_persona``), else ``record``'s own
    launch's (:func:`_launched_persona`); ``None`` for none."""
    carried = record.session.seam_persona if record.session is not None else None
    return (carried or None) if carried is not None else _launched_persona(record)


def _same_home(record: launch_seal.LaunchRecord, target: ClaudeAccount) -> bool:
    """Whether ``target``'s home is the one the recorded agent ran under: its session's,
    as its own hook reported it, else the account's its spawn resolved."""
    home = (record.session.home if record.session is not None else None) or record.account_home
    if not home:
        return False
    return agent_core.dir_identity(Path(home)) == agent_core.dir_identity(target.config_dir)


def _moves_home(
    record: launch_seal.LaunchRecord, target: ClaudeAccount | None, adapter: AgentAdapter
) -> bool:
    """Whether a replacement of ``adapter``'s on ``target`` runs under another home than the
    recorded agent did. None chosen runs under the plain home as the launch environment names
    it, which a row on the plain slot (Codex's 0, ``~/.codex``) ran under too: asked whether
    the row had a slot, its take-over started fresh there (review r18 of #113, finding 7).
    Compared as the config dirs the agent reads in each (``AgentAccounts.config_dir``); a
    record with no home of its own ran under the plain one."""
    if adapter.accounts is None:
        return False
    plain = claude_accounts_core.plain_home(adapter.id)
    home = (record.session.home if record.session is not None else None) or record.account_home
    ran = Path(home) if home else plain
    goes = target.config_dir if target is not None else plain
    config_dir = adapter.accounts.config_dir
    return agent_core.dir_identity(config_dir(ran)) != agent_core.dir_identity(config_dir(goes))


def _resumable(
    record: launch_seal.LaunchRecord, adapter: AgentAdapter | None
) -> tuple[ResumeSpec | None, str | None]:
    """What resumes the session ``record`` holds, on ``adapter``, the agent the
    replacement runs — or why nothing does.

    Its transcript on disk, and the agent's own id for it: Claude Code's IS the board's
    (``capabilities.assigns_session_id``), resumed by the transcript's path; another
    agent's is its native id (Codex resumes ``codex resume <id>`` from its home). Both
    are the record's, as the agent's own hook reported them (``core.launch_seal``),
    never the board session's columns, which a sandbox could write before its broker:
    a transcript there was a conversation of the sandbox's making, its user turns
    included (review r4 of #113, finding 1). And the native id only in the shape the
    agent makes it (a UUID; ``codex resume`` reads one that starts with ``-`` as its own
    option, r3, finding 2). An agent that never reached a hook has no session recorded,
    and one recorded with no native id has nothing to name the thread by: both start
    fresh and say so. A census record (:attr:`~aisquare.core.launch_seal.LaunchRecord.
    legacy`) holds no session whether or not its agent reached a hook: its row was
    spawned before launch records were kept, and that is what it says (SMOKE6).
    """
    facts = record.session
    if facts is None and record.legacy:
        return None, (
            "it was spawned before this aisquare kept launch records, so no session is held for it"
        )
    if facts is None:
        return None, "no session is recorded for it (its agent never reached a hook)"
    transcript = Path(facts.transcript) if facts.transcript else None
    if transcript is None or not transcript.is_absolute() or not transcript.is_file():
        return None, "no transcript on disk to resume"
    native = launch_seal.native_id(facts.native_id)
    if adapter is not None and not adapter.capabilities.assigns_session_id:
        if native is None and facts.native_id:
            return None, (
                f"the native session id recorded for it is not one {adapter.label} makes (a UUID)"
            )
        if native is None or native == facts.id:
            return None, "no native session id is recorded for it"
    return ResumeSpec(facts.id, transcript, native), None


def _window_path(
    config: FleetSettings | None, bound: Mapping[str, str] | None = None
) -> str | None:
    """The ``PATH`` a fleet window's ``aisquare launch`` checks with: the role binding's
    (``bound``, the profile's ``env``, which that launch lays over its environment before
    it asks: review r15 of #113, finding 7), else the fleet server's, which every window
    inherits (``carry_environment`` carries none), while one answers; else this
    process's, which a server it starts takes (r14, finding 6: asked with the caller's, a
    spawn took a receipt for a window its launch refused)."""
    if bound and "PATH" in bound:
        return bound["PATH"]
    with suppress(Exception):
        found = server(config).global_environment("PATH")
        if found is not None:
            return found
    return os.environ.get("PATH")


def _refuse_an_install_the_sandbox_writes(
    argv: list[str],
    cwd: Path,
    adapter: AgentAdapter,
    home: Path | None,
    path: str | None,
) -> None:
    """``store_broker.refuse_inside`` for a window's launch, before any window or stop, with
    the ``PATH`` the window will have (``path``, :func:`_window_path`): its refusal as a
    :class:`FleetError`, and any other failure to work it out too, rather than a traceback
    or a window started unprotected (review r14 of #113, finding 7)."""
    from aisquare.services import store_broker

    try:
        store_broker.refuse_inside(argv, cwd, adapter, home, path)
    except store_broker.LaunchRefused as exc:
        raise FleetError(str(exc)) from exc
    except Exception as exc:
        raise FleetError(
            f"could not work out what {adapter.label}'s sandbox may write "
            f"({type(exc).__name__}: {exc}): nothing was started"
        ) from exc


def _replay_permission_args(
    adapter: AgentAdapter, record: launch_seal.LaunchRecord, label: str, mode: str
) -> list[str]:
    """The sandbox and approval arguments ``mode`` gives the replacement of ``record``'s agent
    (``fleet restart --permission-mode``), as :func:`spawn` turns it into them, the role's
    own settings included (:func:`permission_flags` with ``role_settings``: a mode in the
    agent's own words beside a role's ``sandbox`` is refused there, which the stop came
    before; review r15 of #113, finding 6). :class:`FleetError` for one spawn would refuse."""
    flags, _ = permission_flags(
        adapter, record.role, label, role_settings(record.role), permission_mode=mode
    )
    return flags


def _persona_for_replay(
    project: ProjectInfo, agent: FleetAgent, notes: list[str], *, role: str | None = None
) -> str:
    """The persona a replay runs as: the ROW's, then the role's default, then none; never a refusal.

    A restart is the same agent (#144), and the row is the record of how it ran
    (docs/plans/spawn-personas.md §3.8): the persona it was spawned with, or was
    given since (``attach_persona``), comes back with it. A row with no persona
    replays with none — the empty string, which :func:`_chosen_persona` reads as
    "none was asked" rather than "ask the role" — so a role default adopted since
    does not quietly change an agent that ran without one. Only when the row's
    persona no longer resolves (deleted or renamed since) does the ladder step
    down: to the role's current default when that resolves, else to none, and
    each step is said on the receipt. A replay never refuses over a persona —
    the agent can run without one, and refusing would leave it stopped, the
    failure :func:`_refuse_a_replay_that_cannot_start` exists to prevent
    (owner decision, 2026-09-24).

    ``role`` is the role the replacement runs as when a hand-off changes it
    (:func:`hand_off`): its default is the step down, and the row's persona is kept
    whatever the role, as the Spawn dialog shows it.
    """
    if not agent.persona:
        return ""
    try:
        personas.resolve(agent.persona, personas.project_root(project))
    except personas.PersonaError as exc:
        gone = f"persona {agent.persona} no longer resolves ({exc.rule})"
    else:
        return agent.persona
    fallback = role_settings(role or agent.role, settings()).persona
    if not fallback or fallback == agent.persona:
        notes.append(f"{gone} — started without a persona")
        return ""
    try:
        personas.resolve(fallback, personas.project_root(project))
    except personas.PersonaError as exc:
        notes.append(
            f"{gone}, and the role's default {fallback} does not either ({exc.rule}) — "
            "started without a persona"
        )
        return ""
    notes.append(f"{gone} — started with the role's default, {fallback}")
    return fallback


def _refuse_the_captain_while_off(agent: FleetAgent) -> None:
    """Refuse to start the captain's row again while the experimental captain is off.

    :func:`restart` and :func:`switch` replay the row, and off, the bundled ``captain``
    persona does not resolve: a replay drops a persona that no longer resolves rather
    than refuse (:func:`_persona_for_replay`), so the captain came back with its tools
    and without its rules. Asked before anything is stopped, and said in the one line
    every captain refusal says (review of #240, finding 13).
    """
    # Only the captain's own row: on the home board, launched as the captain is. Off, a
    # role the owner declared as `captain` restarts like any other, on a project (review
    # of #240, R1-F2) or in the home added as one (R2-m2), and no row is its role alone.
    if (
        agent.role == CAPTAIN_ROLE
        and not experimental.captain_enabled()
        and is_the_captains_row(agent)
    ):
        raise FleetError(experimental.CAPTAIN_OFF)


def is_the_captains_row(agent: FleetAgent) -> bool:
    """Whether ``agent``'s row is one the captain's start made: on the home board, working in
    the captain's own folder (``brain.brain_dir``), which only ``aisquare captain`` gives a
    spawn (:func:`_is_the_captains_launch`). Not by its recorded arguments: the captain's
    tools are its agent's own words, given at every launch and never recorded
    (:func:`_the_captains_tools`, #113)."""
    from aisquare.services.captain import brain  # lazy: the captain imports this module

    home = paths.aisquare_home().resolve()
    return agent.project_id == project_id_for(home) and _same_dir(
        Path(agent.cwd), brain.brain_dir()
    )


def _refuse_a_replay_spawn_refuses(project: ProjectInfo, agent: FleetAgent, role: str) -> None:
    """Raise now what :func:`spawn` refuses about ``agent``'s label and board — BEFORE it is
    stopped (review of #240, R2-m1). ``role`` is the one the replay starts as: its launch
    record's (#113), never the row's column.

    The captain switch can be flipped between a row's start and its restart: off, ``captain``
    is a label and a role like any other, so a coder labelled ``captain`` and a declared
    ``captain`` on a project both start; on, ``spawn`` refuses both, and a restart that
    stopped them first left the row exited. Asked here, as the take-over asks its own
    (:func:`_refuse_a_take_over_that_would_move`), so a running agent is left running.
    """
    keep = "nothing was stopped: stop it and spawn it again under another label"
    try:
        _fixed_label(role, agent.label)
    except FleetError as exc:
        raise FleetError(f"{exc} — {keep}") from exc
    if experimental.captain_role(role) and not _is_home_project(project):
        raise FleetError(
            "the captain lives on the home board, one per home — `aisquare captain` starts "
            f"it; {agent.label!r} cannot be started again in {_name(project)}, so nothing "
            "was stopped"
        )


def _refuse_a_replay_that_cannot_start(
    project: ProjectInfo,
    record: launch_seal.LaunchRecord,
    home: Path | None = None,
    permission_mode: str | None = None,
    branch: str | None = None,
) -> None:
    """Raise now what replaying ``record`` would refuse — BEFORE its agent is stopped.

    :func:`restart` and :func:`switch` stop a running agent and only then
    spawn its replacement, so a replay that ``_in_place_of_recorded`` refuses
    — the recorded binary gone from the PATH, and today's resolution missing
    or not the same kind of program — left the agent stopped and nothing
    started. Asked here first, with the evidence :func:`_respawn` hands
    :func:`spawn`, a running agent is left running.

    The binary asked about is the one :func:`spawn` will start, chosen by the function
    spawn asks (:func:`_launch_binary`): the recorded one, or today's of the same kind
    once it has left the PATH. Asking the role's refused the restart of an agent whose
    recorded binary was still there because the role's binding had moved to one that is
    not. A refusal names no ``--bin``: ``fleet restart``, ``fleet switch`` and Restart
    take none. The agent that binary runs as is asked too, as :func:`spawn` asks it
    (``agent_launch.resolve`` with the recorded agent): a binary another agent's, and a
    wrapper with nothing to name its agent, are refused here rather than after the stop.
    So is a sandbox that may write an aisquare the replacement's hooks or broker run
    (``store_broker.refuse_inside``, as :func:`spawn` asks it), under ``home``, the account
    home it starts under (a switch's target slot, whose hooks may run another install),
    else the record's: a switch to such a slot stopped the agent and started nothing
    (review r13 of #113, finding 7). With ``permission_mode`` (``fleet restart
    --permission-mode``), the replacement's sandbox is that mode's, not the recorded one:
    a read-only seat restarted as a workspace one was let through, then refused after the
    stop, and ``full``, which no launch refuses, was refused (r14, finding 5).

    And every other refusal :func:`spawn` gives before a window opens: a bound file it
    cannot read, and a socket path a running server lost (:func:`_refuse_a_freed_socket`).
    That one was asked only inside the spawn, after the stop had typed ``/exit``: an agent
    whose own server listens at another path was stopped, and its replacement refused,
    which left it down (review of #259, round 2). And a worktree that is gone with no
    branch to make it again on (:func:`_branch_of_a_gone_tree`, which writes nothing), in
    ``project``'s root; ``branch`` is the one its row recorded the tree on. A refusal names
    ``project`` in the commands it gives.
    """
    replayed = _launch_binary(
        project,
        record.role,
        binary=None,
        spec=record.spec,
        family=record.session.agent if record.session is not None else None,
        notes=[],
        bin_flag=False,
    )
    chosen = replayed.binary if replayed.source != "default" else None
    try:
        selected = agent_launch.resolve(
            record.role, agent=record.agent, binary=chosen, cwd=project.root
        )
    except ValueError as exc:
        raise FleetError(str(exc)) from exc
    if selected.adapter.capabilities.sandboxes_tools:
        # The agent's arguments as its window's `launch` passes them: after the `--` of
        # the recorded command line (the extra ones, for a record from before there was one);
        # with a new permission mode, that mode's in place of the recorded ones.
        command = list(record.spec.command)
        native = (
            command[command.index("--") + 1 :] if "--" in command else list(record.spec.extra_args)
        )
        if permission_mode:
            given = _replay_permission_args(
                selected.adapter, record, record.label or "", permission_mode
            )
            native = [*given, *record.spec.extra_args]
        account = home or (Path(record.account_home) if record.account_home else None)
        _refuse_an_install_the_sandbox_writes(
            [record.binary, *native],
            Path(record.cwd),
            selected.adapter,
            account or selected.config_dir,
            _window_path(None, selected.profile.env),
        )
    _refuse_an_env_file_it_cannot_read(record.role)
    tree = Path(record.cwd)
    if record.worktree and not tree.exists():
        _branch_of_a_gone_tree(project.root, tree, branch)
    config = settings()
    srv = server(config)
    _refuse_a_freed_socket(srv, config, srv.listener())


def _launch_record(project: ProjectInfo, agent: FleetAgent, verb: str) -> launch_seal.LaunchRecord:
    """The launch record a replacement of ``agent`` takes everything it runs from
    (``core.launch_seal``) — refused, with the reason, when there is none to take.

    The replacement runs outside any sandbox, and the store, the row and the project's
    with it, was writable from an agent's sandbox before its broker (read-only there
    now, ``types.store_read_only``): nothing it runs comes from there, so a
    row with no record is refused, never replayed on the row's values or on today's
    resolution of a role the row names (review r2 of #113, finding 3; r3, finding 5;
    r4, findings 2, 5 and 6). ``project`` is the store's word for the agent's project,
    whose directory decides where the replacement runs and where its worktree lives: it
    has to be the one the record names.
    """
    record = _read_record(agent.id)
    if isinstance(record, str):
        why = record
    elif record.project_id != project.id:
        why = "its launch record is another project's"
    elif not _same_dir(project.root, Path(record.project_root)):
        why = (
            f"the project's directory in the store ({project.root}) is not the one it was "
            f"spawned in ({record.project_root})"
        )
    elif not record.worktree and not _within(Path(record.cwd), Path(record.project_root)):
        # Its directory or one under it: the captain runs from its brain folder under the
        # home, its project's directory (0.8.2's `spawn(cwd=…)`).
        why = "its launch record runs it outside its project's directory"
    else:
        # A running agent is stopped first, where its record places it, a row rewritten
        # to name another pane included (`_stop_row`, review r6 of #113, finding 4).
        return record
    # The way out it names is one (r6, finding 6; r8, finding 1): `fleet stop` stops a row
    # its record places, however rewritten, and a row spawned before records were kept is
    # placed by the census; a row with no record at all has only a dead window of its
    # removed, so its agent is ended where it runs first.
    if agent.ended_at is not None:
        way_out = "spawn a new agent"
    elif isinstance(record, str):
        way_out = (
            "end its agent in its own window, then "
            f"`{project_service.fleet_command('stop', project, agent.label)}` "
            "removes the window and ends its row, and spawn a new agent"
        )
    else:
        stop_it = project_service.fleet_command("stop", project, agent.label)
        way_out = f"`{stop_it}`, then spawn a new agent"
    _say_refused(project.id, agent, f"cannot {verb} it", why, way_out)
    raise LaunchRefused(
        f"cannot {verb} {agent.label!r}: {why} — a replacement takes nothing from the store "
        f"that its launch record does not hold; {way_out}"
    )


def _unrecorded_refusal(why: str) -> str:
    """Why a spawn whose launch record could not be written starts nothing."""
    return (
        f"its launch record could not be written ({why}) — no agent is started without one: "
        "it holds what a restart or a switch runs, and the pane `fleet tell` types into "
        "and `fleet stop` kills"
    )


class LaunchRefused(FleetError):
    """A replay of a row, or a queue for it, refused for want of a launch record to take
    (``core.launch_seal``). Its reason is on the board already (:func:`_say_refused`)."""


def _say_refused(project_id: str, agent: FleetAgent, what: str, why: str, way_out: str) -> None:
    """Put a launch record's refusal on the board, once per row and reason: an unattended
    hand-over and a human reading the board see why the row is not replayed, not only the
    caller. Never raises: the refusal is the caller's either way.

    Marked said only once the note is written: marked first, an emit that failed (the
    store busy past its timeout) left the mark and no note, and the reason was never said
    for that row (review r5 of #113, finding 5). A mark that fails after the note costs
    a second note, later."""
    key = f"launch-refused:{agent.id}"
    with contextlib.suppress(Exception), store_session() as store:
        if store.get_meta(key) == why:
            return
        _team()._emit(
            store,
            project_id,
            "note",
            f"{agent.label}: {what} — {why}; {way_out}",
            session_id=agent.session_id,
        )
        store.set_meta(key, why)


def _same_dir(one: Path, other: Path) -> bool:
    """Whether ``one`` and ``other`` name the same directory, as far as can be told: each
    resolved, or as spelled where a symlink loop stops that (``workspace._resolved_or_spelled``,
    which every Python answers alike: 3.11 and 3.12 raise for a loop, 3.13 does not). Told
    apart for a loop, a project whose root became one read as moved on 3.11 and 3.12 alone,
    and its restart was refused for that before its board was asked (CI on 02eabdad)."""
    return _resolved_or_spelled(one) == _resolved_or_spelled(other)


def _within(path: Path, root: Path) -> bool:
    """Whether ``path`` is ``root`` or a directory under it, compared as :func:`_same_dir`
    compares two directories."""
    inside, top = _resolved_or_spelled(path), _resolved_or_spelled(root)
    return inside == top or top in inside.parents


def _exit_command(agent: FleetAgent) -> str:
    """What ends ``agent``'s session from its prompt (``AgentAdapter.exit_command``)."""
    adapter = recorded_adapter(agent.agent) or get_adapter(DEFAULT_AGENT)
    return adapter.exit_command


def _refuse_an_env_file_it_cannot_read(role: str) -> None:
    """Raise now what the window's ``aisquare launch`` would refuse: a variable ``role``'s
    binding reads from a file that cannot be read or holds nothing
    (``harness.resolve_profile``), such as a reviewer's ``GH_TOKEN``. Said by the spawn,
    restart or switch, before a window opens or a running agent is stopped, rather than by
    a window that ends as it starts."""
    refusal = harness.resolve_profile(role).refusal
    if refusal is not None:
        raise FleetError(refusal)


def _refuse_a_take_over_that_would_move(project: ProjectInfo, agent: FleetAgent, role: str) -> None:
    """Raise now what running ``agent`` on as another ``role`` would meet in :func:`spawn`.

    BEFORE its source is stopped, as :func:`_refuse_a_replay_that_cannot_start` asks about
    the binary. For a role change ``restart`` asked the new role's binary, the persona and
    the account first and left the rest of ``spawn``'s rules for that role to ``spawn``,
    which runs after the stop (review of #240, finding 6). *Take over* as ``manager`` from
    a worktree coder holding a task sent the coder ``/exit``; ``spawn`` then forced the
    label to ``manager`` and its ``git worktree add`` failed, the task's branch being
    checked out in the tree the coder had just left; the abandoned hand-over returned the
    task to the pool, and the project had no live agent. A coder with no task came back as
    a manager in a brand-new tree cut from the root's HEAD, away from what it had not
    committed.

    Asked in ``spawn``'s order: one manager per project, then the label, then the tree. A
    take-over is the same agent carrying on (:func:`_respawn`), under its source's label
    and in its source's tree, so a role that would move either is refused here: one whose
    label is fixed (the manager's, the captain's), one that cannot keep the label its
    source has (the manager's own, for any other role), and one whose worktree today's
    ``[fleet] worktree_dir`` puts somewhere else. That simple rule is the acting manager's
    ruling: the owner can stop the agent and spawn the other role by hand. A role that
    keeps both starts as it always did, and a restart with no role change never comes
    here.
    """
    existing = manager_of(project) if role == "manager" else None
    if existing is not None:
        raise FleetError(
            f"{_name(project)} already has a manager ({existing.id}) — one per project; "
            "nothing was stopped"
        )
    keeps = (
        "a take-over keeps its source's label and tree, so nothing was stopped: stop "
        f"{agent.label!r} and spawn the {role} by hand"
    )
    try:
        fixed = _fixed_label(role, agent.label)
    except FleetError as exc:
        raise FleetError(f"{exc}, which a {role} cannot keep — {keeps}") from exc
    if fixed is not None and fixed != agent.label:
        where = f", in that label's worktree rather than {agent.cwd}" if agent.worktree else ""
        raise FleetError(
            f"as {role} it would be labelled {fixed!r}, not {agent.label!r}{where} — {keeps}"
        )
    if agent.worktree:
        path = project.root / settings().worktree_dir / agent.label
        if path.resolve() != agent.cwd.resolve():
            raise FleetError(
                f"as {role} it would start in {path}, not in {agent.cwd} where it works now "
                f"— {keeps}"
            )


@dataclass
class RestartReceipt:
    """What ``fleet restart`` did: the row it replaced, the one it started, and how."""

    replaced: FleetAgent
    started: FleetAgent
    resumed: bool
    was_running: bool
    """True when the agent was still alive and had to be stopped first."""
    tmux_session: str
    notes: list[str] = field(default_factory=list)
    prompt_typed: Typed = "yes"
    """Whether the replacement's first line reached its pane, as on :class:`SwitchReceipt`."""
    failures: list[str] = field(default_factory=list)
    """The notes that say what did NOT happen, as on :class:`SwitchReceipt`: the stop's
    claims not released, the hand-over mark not taken back, the replacement's first line
    not typed (and why), its claims not moved. Each is in ``notes`` too, among the ones
    every restart has (the account ladder's, the launch replayed)."""

    @property
    def how(self) -> str:
        """How the replacement began, in the words of the board's ``restarted`` line."""
        return _how_started(self.resumed, typed=self.prompt_typed)


def restart(
    project: ProjectInfo,
    label: str,
    *,
    fresh: bool = False,
    size: tuple[int, int] | None = None,
    spawned_by: str = "user",
    agent_id: str | None = None,
    permission_mode: str | None = None,
    role: str | None = None,
    account: str | None = None,
    persona: str | None = None,
    before_stop: Callable[[], None] | None = None,
    team: Sequence[FleetAgentStatus] | None = None,
) -> RestartReceipt:
    """Start an agent again under its own label — the **Restart** of #138.

    Works on the row the label names whatever its state: an agent that exited
    (the 💤 row — its dead window goes once the replacement is up, see
    :func:`_supersede`), one that is lost, or one still running, which is
    stopped first and HANDED OVER as :func:`switch` hands one over: its claims
    wait for the replacement and no exit is announced (a replacement that never
    starts gives them back, and the exit is announced then). The replacement
    keeps the role, the task, the worktree and the account
    (``FleetAgent.account_slot``, #145; for a row older than that column, the
    one its session ran under, as ``switch`` reads it) and — when its
    transcript is on disk and ``fresh`` is not asked — RESUMES the same session
    (``claude --resume <transcript>``), so a manager killed with ctrl+c comes
    back with its intake, its contracts and the state of every coder it
    steered, and it is typed one line telling it to carry on (``claude
    --resume`` opens at an idle prompt, see :func:`_resume_prompt`); without a
    transcript it starts new with a hand-off prompt built from the board.
    For the manager that is "end the dead row, then spawn manager again" — the
    fix the issue asks for — with the session carried over when it can be.

    The replacement is launched as the agent was — the row's launch spec
    (#144): binary, permission mode and arguments as recorded at spawn, not as
    the role's config reads today. ``permission_mode`` is the one part of it a
    restart may change (``fleet restart --permission-mode``), and the
    replacement's own spec records it, so the next restart keeps it: it is how
    an agent that ``auto`` mode strands behind the explainability proxy comes
    back off it with its session (#150), where a change to the role's config
    reaches only the agents spawned after it.

    Every refusal that does not depend on the stop — the role, the task, the
    binary (the one the replay starts: :func:`_refuse_a_replay_that_cannot_start`),
    the account — is given BEFORE anything is stopped or recorded: a running
    agent stopped for a restart that is then refused is an agent lost for
    nothing, and a refused restart leaves the 💤 row and its last screen as
    they were. So is a row a hand-over is already moving (:func:`_handed_over`):
    that hand-over ends it and starts the replacement itself.
    ``agent_id`` pins the row, as for :func:`stop`: the agent view's Restart
    means the row it shows, never a replacement that took the label since.

    ``role``, ``account`` and ``persona`` are a take-over's changes (the Spawn dialog's
    *Take over*, :func:`hand_off`); ``None`` keeps the row's, and an account of
    :data:`THIS_SHELL_ACCOUNT` runs where a plain spawn with no ``--account`` runs.
    Another role launches as today's config says for it, since the recorded launch was
    the old role's; a persona of ``""`` is none. Each is checked with the rest, before
    anything is stopped, and so is what ``spawn`` asks of another role: a second manager
    is refused here, and so is a role under which the agent could not keep its label and
    its tree, the manager's fixed label above all
    (:func:`_refuse_a_take_over_that_would_move`).

    ``permission_mode`` is refused first when the row's agent does not take it
    (:func:`_refuse_a_permission_the_agent_cannot_take`: a ``WRITE:APPROVALS`` intent
    its adapter maps, or a value in its own words — Claude Code's
    :data:`~aisquare.core.config.CLAUDE_PERMISSION_MODES` — or ``""`` for no flag).
    The replacement records it, so a typo was replayed by every later restart, switch
    and hand-over, and it stopped a running agent for a replacement that could not
    start (review of #169, round 1).

    ``before_stop`` is the caller's last check, as for :func:`switch`: asked
    once every refusal above has passed, before anything is stopped or
    recorded, and it raises to refuse.

    A MANAGER's first message, resumed or fresh, ends with its dead team: each agent of the
    project that died and was not stopped on purpose, its state, its task and the command
    that brings it back (:func:`_dead_team_line`, #38). ``team`` is that team as the caller
    read it already (:func:`bulk`); ``None`` lists the project for it.
    """
    with store_session() as store:
        current = store.get_project(project.id) or project
        found = _by_label(store, project.id, label, live_only=False)
        if found is None:
            raise NoSuchAgent(
                f"no agent {label!r} in {_name(project)} — "
                f"`{project_service.fleet_command('ls', project, '--all')}` shows every row"
            )
        agent = found[0]
        if agent_id is not None and agent.id != agent_id:
            raise _replaced(project, label, agent, agent_id)
        # First: every refusal after this one reads the role, the agent and the account,
        # and they are the launch record's (`core.launch_seal`), never the row's.
        record = _launch_record(project, agent, "restart")
        session = store.get_session(agent.session_id) if agent.session_id else None
        _refuse_a_permission_the_agent_cannot_take(record, label, permission_mode)
        if agent.ended_at is None and _handed_over(agent, session, _now()):
            # A `switch` (or another restart) is between its `/exit` and its end: the
            # pane reads dead, the listing leaves the row to it, and a restart that
            # fell through to a plain `stop` here released the parked claims,
            # announced the exit and failed the switch on its own row — with the
            # restart's replacement not holding the task (review of #163, round 2).
            raise _in_hand_over(project, "restart", label)
        try:
            # `spawn`'s refusal for the task (done, dropped, gone from the board), on the
            # board the replacement will read, as `switch` asks it.
            board_and_hub = _board_and_hub(store, current, agent, session)
            task = _task_for(store, board_and_hub[0], agent.task_id)
        except FleetError as exc:
            raise FleetError(f"cannot restart {label!r}: {exc}") from exc
        recent = _handoff_events(store, agent)
    _refuse_the_captain_while_off(agent)
    # `spawn`'s refusals for the binary and the account, asked here for the same
    # reason. The account is the record's slot, whose number outlives a removed slot,
    # and none when its spawn chose none: never the one the session's row says it ran
    # under, a column a sandbox could write before its broker (review r4 of #113,
    # finding 6). The replacement is started on what was resolved here.
    slot = record.account_slot
    # An account of the agent the replacement runs: a Codex row's slot is a Codex
    # slot, which the resolver refused as "not a Claude Code account" for want of it.
    replays = recorded_adapter(record.agent)
    runs_as = role or record.role
    try:
        _require_role(runs_as)
        if runs_as == record.role:
            _refuse_a_replay_spawn_refuses(project, agent, record.role)
            _refuse_a_replay_that_cannot_start(
                current, record, permission_mode=permission_mode, branch=agent.branch
            )
        else:
            # What else `spawn` asks of another role, and the take-over's own rule: it
            # keeps its source's label and tree (review of #240, finding 6).
            _refuse_a_take_over_that_would_move(project, agent, runs_as)
            # Another role launches as today's config says for it, not as the record
            # holds the old one: that binary is the one to ask about.
            _launch_binary(
                project, runs_as, binary=None, spec=None, family=None, notes=[], bin_flag=False
            )
        if persona:
            _chosen_persona(project, runs_as, persona, role_settings(runs_as, settings()), [])
        choice = (
            claude_accounts_service.choose(
                _account_wanted(account, slot),
                role=runs_as,
                project=project,
                agent=replays.id if replays is not None else claude_accounts_core.AGENT,
            )
            if replays is None or replays.accounts is not None
            else claude_accounts_service.AccountChoice(account=None, source=None)
        )
    except (FleetError, claude_accounts_service.NoSuchAccount) as exc:
        raise FleetError(f"cannot restart {label!r}: {exc}") from exc
    account = str(choice.account.slot) if choice.account is not None else None
    # The ladder's notes travel with the slot it chose; with none chosen, `spawn`
    # asks the same ladder and gives them itself.
    notes = [f"accounts: {note}" for note in choice.notes] if account is not None else []
    if (
        replays is not None
        and not replays.capabilities.resumes_across_homes
        and not fresh
        # None chosen runs under the plain home, which a record on a managed slot did not
        # run under (review r17 of #113, finding 2) and one on the plain slot did.
        and _moves_home(record, choice.account, replays)
    ):
        # A take-over onto another account (the Spawn dialog's): its thread lives under the
        # old home (a Codex rollout under its CODEX_HOME), as `switch` says of it.
        fresh = True
        notes.append(
            f"{replays.label} cannot resume a thread under another account's home — the "
            "replacement starts fresh with a hand-off prompt"
        )
    was_running = False
    handed_over: StopReceipt | None = None
    # Set when THIS restart ended a dead or vanished pane's row with the manager's
    # wake-up held back for the replacement (below): a replacement that then never
    # starts owes the manager that wake-up.
    held_back = False
    failures: list[str] = []  # the notes that say what did not happen (`RestartReceipt`)
    if before_stop is not None:
        before_stop()
    # Where its launch record places it: whether it runs is asked there, and a dead pane is
    # ended there. Asked of the row, a row rewritten to name a dead window read "not
    # running", was ended unstopped, and its replacement resumed the session the agent
    # still ran at its record's pane (review r7 of #113, finding 3).
    placement = resolve_place(agent, record)
    # Measured BEFORE anything is done, so the operator does not read "stopped and
    # restarted" for an agent that was not running — at its record's pane, whatever the
    # row's `ended_at` says: a row ended in the store while its agent runs started a second
    # agent beside it, resuming the same session (review r8 of #113, finding 6). A row no
    # record places cannot be asked: it is stopped as `fleet stop` stops it (below), or
    # refused there.
    alive = isinstance(placement, Placed) and _pane_alive(placement.pane)
    if agent.ended_at is not None and alive:
        _say_ended_but_running(agent)
    if agent.ended_at is None or alive:
        was_running = alive
        if was_running:
            # A hand-over, as `switch` runs one (#205): the agent comes straight back
            # on the same task, so its claims wait for the replacement — the same id
            # when it resumes, moved onto the new one with its row when it starts
            # fresh — and no exit is announced; `restarted` below is the board's
            # word. Stopped as `fleet stop` stops it, the task went back to the pool
            # and `agent_exited` woke the manager while `_respawn` was still starting
            # the replacement: the second worker #205's fourth round shut out of
            # `switch` (review of #163, round 1). A session not on the board yet has
            # no claims to park and nothing to mark; a listing that records the death
            # meanwhile leaves the stop one that worked (`_stop_row`), not a failure.
            if session is not None:
                _mark_handing_over(project, agent, session, "restart", label)
            try:
                handed_over = stop(project, label, handover=True, agent_id=agent.id)
            finally:
                # Taken back however `stop` ends, as `switch` takes its own: the mark's
                # one reader, the old process's SessionEnd, has run by then or never
                # will. Taken back only on an `Exception`, a Ctrl-C in the grace left a
                # running agent marked, and after a completed resume the mark waited for
                # the replacement's start hook — which, failed open, left the agent
                # `switching` through every later prompt, since every state writer keeps
                # the mark (the #205 fold's rule, met by #163 at the stack's fold).
                if session is not None and (left := _unmark_handing_over(session)) is not None:
                    notes.append(left)
                    failures.append(left)
            agent = handed_over.agent
        else:
            # A dead pane no listing has recorded yet is recorded as a listing
            # records it — announced, but with no manager woken for an agent that
            # is coming back — and its window LEFT like any 💤 row's (below). Only
            # a pane that is gone — or a tmux that will not say — goes through
            # `stop`, which ends a vanished pane's row and refuses on a silent tmux,
            # and wakes no manager either: it is the same agent coming back.
            ended = _end_dead_rows([placement], _observe_sockets([placement]), nudge=False)
            if ended and ended[0].ended_by == "stopped":
                # Another reader's row, handed back as it is (`_end_dead_rows`): the owner
                # stopped it meanwhile, and the restart is not to undo that (final delta
                # review, m1).
                raise NoSuchAgent(f"{label} ({agent.id}) was stopped meanwhile")
            if ended:
                agent = ended[0]
            else:
                # A vanished pane, ended on its way back: died, not stopped (#38). A row
                # another reader ended meanwhile comes back from `stop` as it is.
                stopped = stop(project, label, agent_id=agent.id, nudge=False, ended_by="died")
                agent = stopped.agent
                if stopped.release_failed:
                    # Said, as `fleet stop` says it: a claim left with the ended
                    # session is one a fresh replacement cannot take back until its
                    # lease lapses.
                    notes.append(f"claims: {stopped.release_failed}")
                    failures.append(notes[-1])
            held_back = True
    # An exited agent's dead window is NOT removed here: `spawn` supersedes it
    # once the replacement is up and recorded, so a restart that is refused on
    # the way (tmux, a parallel spawn) leaves the 💤 row and its last screen as
    # they were.
    # A manager is told which of its team died with it, and the command that brings each
    # back (#38): after a crash nobody else knows to. Read once its own row is dealt with.
    team_line = (
        _dead_team_line(current, agent, team)
        if runs_as == "manager" and not experimental.captain_role(agent.role)
        else None
    )
    try:
        receipt, resumed, more = _respawn(
            project,
            agent,
            session,
            task,
            recent,
            account=account,
            fresh=fresh,
            team_line=team_line,
            reason="restarted",
            spawned_by=spawned_by,
            size=size,
            resume_prompt=_restart_prompt(agent),
            takes_over=handed_over is not None,
            permission_mode=permission_mode,
            role=role,
            persona=persona,
            board_and_hub=board_and_hub,
            # An agent that ended on its own: the owner's `fleet stop` of its 💤 row while
            # the replacement starts wins at the insert, as the guard above has it before
            # (last delta review, m3). Not a hand-over, whose own stop writes `stopped`, nor
            # a row stopped already, which this restart was asked to bring back.
            replaces=agent.id if handed_over is None and agent.ended_by != "stopped" else None,
        )
    except Exception:
        if handed_over is not None and handed_over.withheld:
            _abandon_handover(handed_over.agent)  # no replacement is coming for the parked claims
        elif held_back:
            # The exit went out with its wake-up held back for a replacement that has
            # now failed to start: left unwoken, a waiting manager never hears of the
            # agent it lost, as the hand-over's `_abandon_handover` wakes it for its own.
            nudge_manager(project.id, reason=f"{label} exited", board=_board_of_row(agent))
        raise
    notes.extend(more)
    typed = receipt.prompt_typed or "no"
    how = _how_started(resumed, typed=typed)
    # The suppression is entered FIRST, as in `switch`: a store that cannot be opened
    # costs the courtesy, never a restarted agent reported as not restarted.
    with contextlib.suppress(Exception), store_session() as store:  # the courtesy, not the record
        _team()._emit(
            store,
            _board_of_row(receipt.agent),
            "restarted",
            f"{label} restarted — {how}",
            session_id=receipt.agent.session_id,
        )
    return RestartReceipt(
        replaced=agent,
        started=receipt.agent,
        resumed=resumed,
        was_running=was_running,
        tmux_session=receipt.tmux_session,
        notes=notes,
        prompt_typed=typed,
        failures=[*failures, *receipt.failures],
    )


def _refuse_a_permission_the_agent_cannot_take(
    record: launch_seal.LaunchRecord, label: str, mode: str | None
) -> None:
    """Raise when the agent ``record``'s replacement would run refuses ``mode`` — before
    anything is stopped. The agent is the record's: asked of the session's, a Codex row
    bound to a session another agent's hook wrote had a mode checked for that agent, the
    original stopped, and the mode handed to a Codex that refused it (review r2 of #113,
    finding 8). One this install does not know is ``spawn``'s to refuse."""
    if not mode:
        return
    adapter = recorded_adapter(record.agent)
    if adapter is None:
        return
    why: str | None = None
    if PermissionIntent.written(mode):
        try:
            adapter.permission_args(PermissionIntent.parse(mode), record.role)
        except ValueError as exc:
            why = str(exc)
    elif adapter.capabilities.legacy_fleet_args:
        if mode not in CLAUDE_PERMISSION_MODES:
            why = (
                f"{mode!r} is not a {adapter.label} permission mode "
                f"({', '.join(CLAUDE_PERMISSION_MODES)}, or empty for no flag), nor "
                "WRITE:APPROVALS such as workspace:auto"
            )
    else:
        try:
            adapter.fleet_args(record.role, label, mode)
        except ValueError as exc:
            why = f"{exc}, or WRITE:APPROVALS such as workspace:auto"
    if why is not None:
        raise FleetError(f"cannot restart {label!r}: {why} — nothing was stopped")


HandoffMode = Literal["fork", "take_over"]

THIS_SHELL_ACCOUNT = "(this shell's)"
"""An ``account`` for :func:`hand_off` and :func:`restart`: run where a plain spawn with
no ``--account`` runs, the resolver's ladder (``claude_accounts.choose(None)``), and never
on the row's own slot, which ``None`` keeps. The Spawn dialog's *(this shell's)* sends it
(runner2-1's reopen of #234: sent as ``None``, it ran the new agent on the teammate's
slot). It is no slot, alias or email, so it can never name an account itself."""


@dataclass(frozen=True)
class HandoffReceipt:
    """What a hand-off from a teammate did (the Spawn dialog's, card tsk_01m3ns5a736s)."""

    mode: HandoffMode
    source: FleetAgent
    """The teammate handed off from, as read before anything started."""
    started: SpawnReceipt
    """The agent started. The dialog dismisses with it, so the shell attaches it as it
    attaches any spawn."""
    resumed: bool
    """Whether it resumed the source's transcript: a fork under its own new id, a
    take-over under the same id, with the claims."""
    stopped: FleetAgent | None = None
    """The source as a take-over stopped it; ``None`` for a fork, which touches nothing."""
    notes: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def handoff_sources(project: ProjectInfo) -> list[FleetAgentStatus]:
    """The teammates a spawn can hand off from: the project's listed rows, less the lost.

    The owner's words: "any current running or stopped but still open/attached
    teammate". That is the fleet's own listing (the sidebar's rows): the running,
    waiting and asking agents, and the exited ones whose dead window tmux still keeps.
    A ``lost`` row's window is gone, so it is nobody to hand off from. The captain
    lives on the home board, so no project lists it.
    """
    return [status for status in list_agents(project) if status.state != "lost"]


def hand_off(
    project: ProjectInfo,
    source: str,
    *,
    mode: HandoffMode = "fork",
    fresh: bool = False,
    role: str | None = None,
    account: str | None = None,
    persona: str | None = None,
    label: str | None = None,
    prompt: str | None = None,
    size: tuple[int, int] | None = None,
    spawned_by: str = "user",
    agent_id: str | None = None,
) -> HandoffReceipt:
    """Start an agent from teammate ``source``: the Spawn dialog's *Hand off from*.

    **Fork**, the default, touches nothing of the source: no mark, no stop, no claim, and
    neither its row, its window, its tree nor its branch. The new agent takes a label that
    nothing holds (:func:`_fork_label`: ``label``, refused when a row, a worktree or a
    branch already has it, else the next free one), its own worktree made new at the
    commit the source's tree is on, and no task. With the source's transcript on
    disk and ``fresh`` not asked, it resumes that conversation as a FORK
    (``ResumeSpec(fork=True)``: ``--resume <transcript> --fork-session`` under a new id),
    and is told in one line what it is. Otherwise it starts fresh, with a first message
    built from the board that names the source and its task as the source's work.

    **Take over** is the source's own hand-over, which :func:`restart` already is: a
    running source is stopped with its claims parked for the replacement (an exited one
    is simply started again), under the same label, task and tree, resumed or fresh as
    asked. The row read here is the row restarted (``agent_id``). A role that would move
    it out of that label or that tree is refused before the stop
    (:func:`_refuse_a_take_over_that_would_move`).

    ``role``, ``account`` and ``persona`` are the dialog's changes, ``None`` keeping the
    source's; a persona of ``""`` is none, and an account of :data:`THIS_SHELL_ACCOUNT`
    runs where a plain spawn with no ``--account`` runs. ``label`` and ``prompt`` are a
    fork's own: a take-over keeps the source's label, and continues its work.

    ``agent_id`` names the source's ROW, for a caller that means one teammate rather than
    whoever carries the label now, as it does for :func:`stop` and :func:`restart`. The
    label alone names the newest row under it, and two rows of a project can carry one
    label over time: by label, choosing an exited coder-1 forked, or stopped and
    restarted, the coder-1 started since (:func:`_handoff_row`). Given, the hand-off acts
    on exactly that row, which must be this project's and carry ``source``. A fork forks
    its conversation and its commit, live or ended, whatever newer row shares the label. A
    take-over acts on it only while it is still the row its label names: replaced by a
    newer one, it is refused as ``restart`` refuses it, with neither row touched. ``None``
    resolves the label as before.
    """
    with store_session() as store:
        agent = (
            store.fleet_agent_by_label(project.id, source, live_only=False)
            if agent_id is None
            else _handoff_row(store, project, source, agent_id)
        )
        if agent is None:
            raise NoSuchAgent(f"no teammate {source!r} in {_name(project)} to hand off from")
        try:
            # On the board the source reads, as `restart` looks its task up.
            task = _task_for(store, _board_of_row(agent), agent.task_id)
        except FleetError:
            task = None  # finished work: nothing to name in a fork's first message
        recent = _handoff_events(store, agent)
    if mode == "take_over":
        restarted = restart(
            project,
            source,
            fresh=fresh,
            role=role,
            account=account,
            persona=persona,
            size=size,
            spawned_by=spawned_by,
            agent_id=agent.id,
        )
        started = SpawnReceipt(
            agent=restarted.started,
            asked_label=source,
            tmux_session=restarted.tmux_session,
            notes=list(restarted.notes),
            prompt_typed=restarted.prompt_typed,
        )
        return HandoffReceipt(
            mode="take_over",
            source=agent,
            started=started,
            resumed=restarted.resumed,
            stopped=restarted.replaced,
            notes=list(restarted.notes),
        )
    notes: list[str] = []
    # What it forks is the source's launch record's, as every replay's is (`_launch_record`):
    # its launch, its tree, and the session its own hook reported, never the row's columns.
    record = _launch_record(project, agent, "fork")
    forks = recorded_adapter(record.agent)
    runs_as = role or record.role
    replay = runs_as == record.role
    slot = record.account_slot
    wanted = _account_wanted(account, slot)
    resume: ResumeSpec | None = None
    if not fresh:
        resume, wanted = _forkable(project, record, forks, runs_as, wanted, notes)
    own = is_git_project(project.root)
    if not own:
        notes.append(f"not a git repository — the fork works in {project.root}, beside {source}")
    first = _fork_prompt(agent, task, recent, resumed=resume is not None)
    if prompt and prompt.strip():
        # A resumed fork's first message stays one line (see :func:`_fork_prompt`).
        extra = " ".join(prompt.split()) if resume is not None else prompt.strip()
        first = f"{first} {extra}" if resume is not None else f"{first}\n\n{extra}"
    receipt = spawn(
        project,
        runs_as,
        label=label,
        task_id=None,
        worktree=own,
        prompt=first,
        spawned_by=spawned_by,
        account=wanted,
        resume=resume,
        size=size,
        spec=record.spec if replay else None,
        # ``None`` keeps the source's persona whatever role the fork runs as, as a take-over
        # keeps it (:func:`_respawn`): the Spawn dialog sends ``None`` when it shows the
        # teammate's own, and a fork with another role started without it (review of #240,
        # "also confirmed": fork with a changed role).
        persona=(
            _persona_for_replay(project, agent, notes, role=runs_as) if persona is None else persona
        ),
        agent=record.agent,
        family=record.session.agent if record.session is not None else None,
        start_point=_fork_point(project, agent, Path(record.cwd), notes) if own else None,
        fork=True,
    )
    resumed = resume is not None
    how = "resumed its session as a fork" if resumed else "started fresh"
    with contextlib.suppress(Exception), store_session() as store:  # the courtesy, not the record
        _team()._emit(
            store,
            project.id,
            "forked",
            f"{receipt.agent.label} forked from {source} — {how}",
            session_id=receipt.agent.session_id,
        )
    return HandoffReceipt(
        mode="fork",
        source=agent,
        started=receipt,
        resumed=resumed,
        notes=[*notes, *receipt.notes],
        failures=list(receipt.failures),
    )


def _forkable(
    project: ProjectInfo,
    record: launch_seal.LaunchRecord,
    forks: AgentAdapter | None,
    role: str,
    wanted: str | None,
    notes: list[str],
) -> tuple[ResumeSpec | None, str | None]:
    """What a hand-off's Fork resumes of the session ``record`` holds, on ``forks``, its agent
    — or ``None`` with why on ``notes``, the new agent then starting fresh from the board —
    and the account it is asked to run on (``wanted``, the one resolved here when it was).

    The agent's own fork (``fork_args``: Claude Code's ``--resume <transcript>
    --fork-session`` under an id the fleet mints, Codex's ``fork <id>`` under one its first
    hook brings) of what :func:`_resumable` would resume: the record's session, its
    transcript on disk and, for an agent the launch does not name, its native id. An agent
    whose sessions live in their home (``resumes_across_homes`` false: a Codex thread is a
    rollout under its ``CODEX_HOME``) forks only onto that home: asked onto another
    account's, it starts fresh with a hand-off prompt, as ``switch`` does, and the account
    resolved here is the one the spawn gets, so the two cannot differ."""
    named = forks.label if forks is not None else record.agent
    found, why = _resumable(record, forks)
    if forks is None or found is None:
        notes.append(
            f"{why or f'{named} is not known here'} — the new agent starts fresh from the board"
        )
        return None, wanted
    fork = forks.fork_args(found.native_session_id or found.session_id, found.transcript_path)
    if fork is None:
        notes.append(f"{named} cannot fork a session — the new agent starts fresh from the board")
        return None, wanted
    if not forks.capabilities.resumes_across_homes and forks.accounts is not None:
        try:
            choice = claude_accounts_service.choose(
                wanted, role=role, project=project, agent=forks.id
            )
        except claude_accounts_service.NoSuchAccount as exc:
            raise FleetError(str(exc)) from exc
        if choice.account is not None:
            wanted = str(choice.account.slot)
        # None chosen: the launch environment as it is, the plain home.
        if _moves_home(record, choice.account, forks):
            notes.append(
                f"{named} cannot fork a thread under another account's home — the new agent "
                "starts fresh with a hand-off prompt"
            )
            return None, wanted
    notes.append(f"forked as {named} forks a session: `{install_route.command_line(fork.args)}`")
    return replace(found, fork=True), wanted


def _handoff_row(
    store: ContextStore, project: ProjectInfo, label: str, agent_id: str
) -> FleetAgent:
    """The row a hand-off was GIVEN (``agent_id``): this project's, under ``label``, or refused.

    The label alone names the newest row that carries it, and two rows of a project can
    carry one label over time: an exited coder-1 and the coder-1 started since. The Spawn
    dialog's *Hand off from* can show the older one, so by label its fork forked, and its
    take-over stopped and restarted, the NEWER agent (review of #240, "also confirmed":
    hand-off by label). A caller that means one teammate names its row, as the agent view
    does for :func:`stop` and :func:`restart`: by its whole id, as theirs is. A row of
    another project, or one under another label, is a refusal that says which, before
    anything is stopped or made, and never a quiet fall back to the row the label names.
    """
    try:
        agent = store.get_fleet_agent(agent_id)
    except AmbiguousIdError:
        agent = None  # the start of several ids is no row's id
    if agent is None or agent.id != agent_id:
        raise NoSuchAgent(
            f"no agent {agent_id} to hand off from — "
            f"`{project_service.fleet_command('ls', project, '--all')}` shows every row; "
            "nothing was done"
        )
    if agent.project_id != project.id:
        raise FleetError(
            f"{agent_id} ({agent.label}) is another project's agent, not one of "
            f"{_name(project)}'s — nothing was done"
        )
    if agent.label != label:
        raise FleetError(
            f"{agent_id} is labelled {agent.label!r}, not {label!r} — nothing was done"
        )
    return agent


def _account_wanted(account: str | None, slot: int | None) -> str | None:
    """What the resolver is asked for a replacement or a fork: the dialog's account, the
    row's own slot for ``None``, or no flag at all for :data:`THIS_SHELL_ACCOUNT`."""
    if account == THIS_SHELL_ACCOUNT:
        return None
    if account is not None:
        return account
    return str(slot) if slot is not None else None


def _head_of(tree: Path) -> str | None:
    """The commit ``tree``'s checkout is on, or ``None`` when git cannot say."""
    done = _git(tree, "rev-parse", "--verify", "--quiet", "HEAD")
    return (done.stdout.strip() or None) if done.returncode == 0 else None


def _fork_point(
    project: ProjectInfo, source: FleetAgent, tree: Path, notes: list[str]
) -> str | None:
    """Where a fork's branch is cut: the commit its source's tree is on, else the tip of the
    branch its row recorded (review of #240, R1-F7: an exited source whose worktree was
    removed gave ``None``, and the fork was cut at the checkout's HEAD while it resumed a
    conversation about commits it lacked). Neither there, the fork starts at the checkout's
    HEAD, and its receipt says so. ``tree`` is the source's as its launch record names it
    (#113), never the row's column."""
    head = _head_of(tree) if tree.is_dir() else None
    if head is not None:
        return head
    if source.branch is not None:
        done = _git(
            project.root,
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{source.branch}^{{commit}}",
        )
        if done.returncode == 0 and done.stdout.strip():
            notes.append(
                f"{source.label}'s worktree is gone — the fork is cut at its branch {source.branch}"
            )
            return done.stdout.strip()
    notes.append(
        f"{source.label}'s worktree and branch are gone — the fork starts at "
        f"{project.root.name}'s HEAD, without any commit {source.label} made there"
    )
    return None


def _fork_prompt(
    source: FleetAgent, task: TeamTask | None, recent: list[TeamEvent], *, resumed: bool
) -> str:
    """The first message of a fork: what it is, what it is not, where to look.

    Resumed, ONE line, as :func:`_resume_prompt` is and for its reason: ``claude
    --resume`` opens at an idle prompt, and :func:`_type_prompt` types a single line past
    its timeout and refuses a multi-line one. It carries the source's conversation, so it
    is told only what changed. Fresh, it is built from the board as
    :func:`_handoff_prompt` is, with the source's task named as the source's.
    """
    if resumed:
        return (
            f"You are a fork of {source.label}: you have its conversation up to now, but you run "
            "in your own worktree on your own branch with no claims, and "
            f"{source.label} keeps working in its own tree, so never touch that tree or its "
            "task. Re-read `aisquare board` and `git status` here, then say what you will "
            "take on."
        )
    lines = [
        f"You are a fork of {source.label}, which keeps running: you start fresh, in your own "
        "worktree on your own branch, with no claims.",
    ]
    if task is not None:
        lines.append(f"{source.label} is working on {task.title} ({task.id}): its task, not yours.")
    tail = recent[-_HANDOFF_NOTES:]
    if tail:
        lines.append(f"{source.label}'s last board entries:")
        lines.extend(f"- {event.kind}: {event.text}" for event in tail)
    lines.append(
        "Start by reading `aisquare board`, then `git status` and `git log --oneline -5` in "
        "your own worktree, and say what you will take on."
    )
    return "\n".join(lines)


def _pane_alive(pane: PlacedPane) -> bool:
    """Whether the pane its launch record places exists and has not died — ``False`` when
    tmux cannot say.

    A pane under the record's id on a server younger than the record is another
    agent's (:func:`_outlived`), so it is not the row's pane alive. When the
    server started comes in the same answer (``PaneFacts.server_started``; a
    second ``display-message`` before, review of #259, round 3), and a start tmux
    does not report judges nothing. A refusal of that second question was read
    as "cannot say" once, and sent the restart of a RUNNING agent down the other
    branch: stopped as ``fleet stop`` stops it, its claims released and its exit
    announced, then replaced with no hand-over (review of the #203 final-round
    fixes, F5).
    """
    try:
        facts = pane.facts()
    except TmuxError:
        return False
    if facts is None or facts.dead:
        return False
    return not pane.outlived(facts.server_started)


def project_of(agent: FleetAgent) -> ProjectInfo:
    """The project an agent's row belongs to — what a UI action needs to call the service."""
    with store_session() as store:
        project = store.get_project(agent.project_id)
    if project is None:
        raise NoSuchProject(f"the project of {agent.label!r} is no longer registered")
    return project


def _mark_handing_over(
    project: ProjectInfo, agent: FleetAgent, session: TeamSession, verb: str, label: str
) -> None:
    """The session's own ``SessionEnd`` parks its claims for the replacement (rule 2).

    Marked for ONE hand-over. ``switch`` and ``restart`` refuse a session a
    hand-over holds (:func:`_handed_over`) when they first read it, and then
    marked it unconditionally. Two hand-overs of one agent (two restarts, a
    restart and a switch, a switch by hand and the automatic one) both got past
    that read and both marked it. Whichever stop came second found the row ended
    by the first, whose mark was already taken back, read that as a stop that
    worked (:func:`_stop_row`), and both started a replacement (review of
    #203). The mark is now a compare-and-set by the same rule
    (``SqliteStore.mark_handover``), and the hand-over that loses is refused as
    that read would have refused it, before it has stopped anything.
    """
    held_after = max(agent.created_at, _now() - HANDOVER_GRACE)
    with store_session() as store:
        marked = store.mark_handover(session.id, held_after=held_after)
    if not marked:
        raise _in_hand_over(project, verb, label)


def _unmark_handing_over(session: TeamSession) -> str | None:
    """Take the mark back; a note for the receipt when it could not be, else ``None``.

    Never raises: it runs in the ``finally`` around ``switch``'s and ``restart``'s
    stop, where a raise would replace the stop's own outcome. Not in silence,
    though: a mark left on reads ``switching`` until the session's own start
    replaces it, and one whose start hook failed open too stays there through
    every prompt (review of the #205 fold, round 3).
    """
    try:
        # Not through `touch_session`: every state writer but this one keeps the mark.
        with store_session() as store:
            store.unmark_handover(session.id, session.state)
    except Exception as exc:
        return (
            f"the hand-over mark was not taken back ({type(exc).__name__}: {exc}) — the board "
            "shows the agent `switching` until its session's start hook replaces it"
        )
    return None


#: What the exit :func:`_abandon_handover` announces says after its status. The hand-over's own
#: ``/exit`` ended the row with status 0, so the board read "exited (0)" as a ``fleet stop``
#: does, and whoever reads it (the manager, ``remote``'s needs-you for the manager itself) took
#: an agent nothing replaced for one whose work was done.
HANDOVER_FAILED = "its replacement did not start"


def _abandon_handover(stopped: FleetAgent) -> None:
    """The replacement never started: release what was parked and record the exit withheld,
    saying so (:data:`HANDOVER_FAILED`)."""
    with contextlib.suppress(Exception), store_session() as store:
        _team().release_agent_claims(store, stopped, why="hand-over failed")
        _emit_exit(store, stopped, why=HANDOVER_FAILED)
    nudge_manager(
        stopped.project_id, reason=f"{stopped.label} exited", board=_board_of_row(stopped)
    )


def _how_started(resumed: bool, *, typed: Typed) -> str:
    """How a replacement began, for the board's ``switched`` and ``restarted`` lines.

    And for every surface that reports the receipt (``RestartReceipt.how``,
    ``SwitchReceipt.how``). ``fleet restart`` and ``fleet switch`` printed
    "started fresh with a hand-off prompt" above a note saying it was NOT
    typed, and the agent view's toast said "started fresh" (review of the
    side/ff-fleet fold).

    Worded from what reached its pane as well as from how it was launched. A
    replacement whose first line was not typed — up too slowly for a multi-line
    paste, gone before it, refused by tmux — sits idle at an empty prompt, and
    the line said "started fresh with a hand-off prompt" over it. On the
    automatic hand-over that line was the only account anyone got (review of
    #203, final round, FLEET-5); the receipt's notes say why it was not typed.

    A line pasted at its prompt whose Enter was refused, or one tmux did not answer in time
    with, is not "NOT typed": the human, or the manager reading the board, sent it again,
    beside the first copy (review of #259, round 11, 2/13).
    """
    line = "the line telling it to continue" if resumed else "its hand-off prompt"
    began = "resumed its session" if resumed else "started fresh"
    if typed == "yes":
        return began if resumed else "started fresh with a hand-off prompt"
    if typed == "unsent":
        return f"{began}, but {line} waits unsent at its prompt: press Enter in its pane"
    if typed == "maybe":
        return (
            f"{began}, but tmux did not answer in time with {line}: it may still arrive — "
            "read its screen before you send it again"
        )
    return f"{began}, but {line} was NOT typed"


def _restart_prompt(agent: FleetAgent) -> str:
    """The first message of a RESUMED restart — one line, for :func:`_resume_prompt`'s reasons.

    Without it the replacement sat at the idle prompt ``claude --resume`` opens
    at: a running agent's holding its task's claim the while, and a manager —
    ``working`` from its start hook, with no turn to end — refused the board's
    nudges for as long as that row stayed fresh (review of #163, round 2).
    """
    if experimental.captain_role(agent.role):
        # The captain has no shell (T2): it picks up through its own tools.
        return (
            "You are the captain, restarted and resumed mid-session: call attention() to see "
            "what needs the owner now, then carry on from where you left off."
        )
    return (
        f"You are {agent.label}, restarted and resumed mid-session: re-read `aisquare board` "
        "and `git status`, then continue exactly where you left off without redoing work "
        "that is already committed."
    )


def _resume_prompt(agent: FleetAgent, reason: str | None, *, automatic: bool = True) -> str:
    """The first message of a RESUMED replacement — one line, so it is typed even past the wait.
    A manual switch with no reason given is said as the operator's move, never a limit.

    ``claude --resume`` opens the old conversation at an idle prompt and waits
    for input; without a line the "moved" agent never continued (review of
    #205, finding 5). One line on purpose: :func:`_type_prompt` types a
    single-line prompt past its timeout and refuses a multi-line one.
    """
    why = reason or ("a usage limit" if automatic else "its operator's `fleet switch`")
    return (
        f"You are {agent.label}, moved to another Claude account after {why} and resumed "
        "mid-task: re-read `aisquare board` and `git status`, then continue exactly where "
        "you left off without redoing work that is already committed."
    )


_HANDOFF_NOTES = 5
_HANDOFF_DETAIL = 600
_HANDOFF_SCAN = 60
"""How many of the session's newest entries the hand-off reads for its
:data:`_HANDOFF_NOTES`, the human board's among them."""


def _handoff_events(store: ContextStore, agent: FleetAgent) -> list[TeamEvent]:
    """The board entries a hand-off prompt quotes: the session's own newest (oldest first),
    not the project's last 60 filtered down, which on a busy board all belonged to other
    agents (review of #205, third round); and less its bells and notices, which are the
    human's and never an agent's (``team.HUMAN_BOARD_KINDS``): three "attention" lines
    of Codex's took the whole tail. Read on the board the agent read (:func:`_board_of_row`)."""
    if agent.session_id is None:
        return []
    events = store.filtered_events(
        _board_of_row(agent), session_id=agent.session_id, limit=_HANDOFF_SCAN
    )
    human = _team().HUMAN_BOARD_KINDS
    return [event for event in events if event.kind not in human][-_HANDOFF_NOTES:]


def task_prompt(task: TeamTask) -> str:
    """The first message of an agent spawned for ``task`` with no prompt of its own (#32, L1;
    the owner's D2, board seq 671): read the task, and do what its briefing's ASSIGNED TO YOU
    line says. Without it the agent sat idle at an empty prompt until someone typed this.

    It names no claim. Whether to claim is the briefing's to say, per role and per state
    (``team._assignment_lines``): a tester or reviewer is told a todo task is "not yours to
    claim", a task waiting on its needs is not to be claimed yet, and one in review is to
    be verified. "Claim it" here had a verifier take a coder's task, and fail on a review
    one (delta review of #274, m1).
    """
    ref = str(task.number) if task.number is not None else task.id
    named = f"#{ref}" if task.number is not None else ref
    return (
        f"Start on your assigned task {named}: {' '.join(task.title.split())}. Read its whole "
        f"contract with `aisquare task show {ref}`, then do what the ASSIGNED TO YOU line of "
        "your briefing says for it — claim it only if that line tells you to."
    )


def _handoff_prompt(
    agent: FleetAgent, task: TeamTask | None, recent: list[TeamEvent], reason: str | None
) -> str:
    """The first message of a FRESH replacement: who it is, what it inherits, where to look.

    Built from the board rather than the model — the account that just ran out
    is the one that cannot be asked for a summary (#146's own argument). Kept
    short on purpose: the board and the working tree are the source of truth,
    and the prompt points at them instead of retelling them.
    """
    if experimental.captain_role(agent.role):
        return (
            f"You are the captain, taking over from a previous session ({reason or 'it stopped'})."
            " Call attention() to see what needs the owner, and since(project) for what "
            "happened on a board while you were away; then wait for the owner."
        )
    lines = [
        f"You are {agent.label}, taking over from a previous session of this agent "
        f"({reason or 'it stopped'}).",
    ]
    if task is not None:
        lines.append(f"Your task: {task.title} ({task.id}).")
        if task.detail:
            detail = task.detail.strip()
            if len(detail) > _HANDOFF_DETAIL:
                detail = detail[:_HANDOFF_DETAIL].rstrip() + " …"
            lines.append(f"Contract: {detail}")
    tail = recent[-_HANDOFF_NOTES:]
    if tail:
        lines.append("The previous session's last board entries:")
        lines.extend(f"- {event.kind}: {event.text}" for event in tail)
    lines.append(
        "Start by reading `aisquare board`"
        + (f" and `aisquare task show {task.id}`" if task is not None else "")
        + ", then `git status` and `git log --oneline -5` in this directory; continue from "
        "where the previous session left off and do not redo work that is already committed."
    )
    return "\n".join(lines)


# --- every agent at once: ``fleet restart|stop --all`` and Settings ▸ Fleet ---------

PROCESS_STATES: frozenset[str] = frozenset(
    {"working", "waiting", "attention", "limited", "unknown"}
)
"""The states in which an agent has a process to stop: what the agent view's Stop is for, and
the agents :func:`bulk` acts on. An exited (💤) or lost row has none, and is left as it is."""

BulkAction = Literal["restart", "stop"]
BulkOutcome = Literal["done", "skipped", "failed"]

THIS_SESSION = "this session"
"""Why :func:`bulk` leaves the caller's own row: a ``restart --all`` run by a manager stopped
the manager, and the command with it, half-way through."""
ALREADY_EXITED = "already exited"
"""Why :func:`bulk` leaves a row the caller was shown that has no process by the time it acts."""
STOPPED_ON_PURPOSE = "stopped on purpose"
"""Why a restart by :func:`bulk` leaves a dead row aisquare ended itself (#38): ``fleet stop``,
Stop all, ``shutdown`` — the owner's word stands until they start it again."""
ENDED_CLEANLY = "ended by itself (exit 0)"
"""Why a restart by :func:`bulk` leaves a dead row whose agent exited 0 (#38): someone typed
``/exit`` in its window, or it finished — not a death to undo."""
DEAD_RESTART_WITHIN = timedelta(days=7)
"""How long after it died a row is still one :func:`bulk` restarts (#38): a reboot over a
long weekend is covered, and an agent that died weeks ago and was never missed is not
brought back by a Restart all meant for today's crash."""
GONE_SINCE = "its row is gone since it was shown"
"""Why a restart by :func:`bulk` leaves a row it was shown that the new read no longer has."""
PROJECT_FORGOTTEN = "its project was forgotten"
"""Why a restart by :func:`bulk` leaves a running agent of a forgotten project: the spawn of
its replacement is a deliberate add, and would bring the project back to every listing."""


@dataclass(frozen=True)
class BulkRow:
    """One agent a :class:`BulkPlan` read: its project, its status then, and why the action
    leaves it (``None`` when the action acts on it)."""

    project: ProjectInfo
    status: FleetAgentStatus
    skip: str | None = None

    @property
    def agent(self) -> FleetAgent:
        return self.status.agent

    @property
    def dead(self) -> bool:
        """Whether the agent had no process when the plan was read (💤 exited or lost): a
        restart starts it again rather than handing a running one over (#38)."""
        return self.status.state not in PROCESS_STATES


@dataclass(frozen=True)
class BulkPlan:
    """What :func:`bulk` would do right now, before it does anything (:func:`bulk_plan`).

    ``rows`` are every project's live rows (the managers' alone with ``managers_only``),
    grouped by project and each project's manager first (last for a restart, #38).
    ``unread`` are the projects whose agents could not be listed, with why: nothing is done
    there, and the report says so.
    """

    action: BulkAction
    managers_only: bool
    rows: tuple[BulkRow, ...]
    unread: tuple[tuple[ProjectInfo, str], ...] = ()
    dead_teams: Mapping[str, tuple[FleetAgentStatus, ...]] = field(default_factory=dict)
    """A restart's: by project id, the agents that died and were not stopped on purpose,
    whatever ``managers_only`` drew (:func:`dead_team`'s rule, read with the plan). A
    manager this run restarts is told those not restarted by it (#38)."""

    @property
    def targets(self) -> list[BulkRow]:
        """The rows the action acts on."""
        return [row for row in self.rows if row.skip is None]

    def managers(self) -> BulkPlan:
        """The same read, the managers' rows alone: the dialog's *Managers only* switch,
        which redraws from the read it has rather than asking tmux again."""
        rows = tuple(row for row in self.rows if row.agent.role == "manager")
        return replace(self, managers_only=True, rows=rows)


@dataclass(frozen=True)
class BulkReceipt:
    """What :func:`bulk` did to one agent: the ROW it meant (``agent.id``), never a label."""

    project: ProjectInfo
    agent: FleetAgent
    outcome: BulkOutcome
    why: str | None = None
    """Why it was skipped, or the error it failed with; ``None`` when it was done."""
    result: StopReceipt | RestartReceipt | None = None
    """The stop's or the restart's own receipt, when it was done."""
    dead: bool = False
    """Whether the agent had no process when the plan was read (:attr:`BulkRow.dead`): a
    restart started it again rather than handing a running one over (#38)."""

    @property
    def release_failed(self) -> str | None:
        """A done stop's claims that could not be given back (:attr:`StopReceipt.release_failed`):
        the agent is stopped, but not cleanly, as ``fleet stop`` exits 1 for it."""
        return self.result.release_failed if isinstance(self.result, StopReceipt) else None


@dataclass(frozen=True)
class BulkReport:
    """Every receipt of one :func:`bulk`, in the order the plan gave them."""

    action: BulkAction
    managers_only: bool
    receipts: tuple[BulkReceipt, ...]
    unread: tuple[tuple[ProjectInfo, str], ...] = ()
    interrupted: str | None = None
    """Set when the operator interrupted the run (:class:`BulkInterrupted`): the agent it was
    on, and that the rest were left as they were. The receipts are the ones made before."""

    def of(self, outcome: BulkOutcome) -> list[BulkReceipt]:
        return [receipt for receipt in self.receipts if receipt.outcome == outcome]

    @property
    def ok(self) -> bool:
        """Nothing failed: no agent, no project's listing, no stop's release, and the run was
        not interrupted. Exit 0."""
        return not (
            self.of("failed")
            or self.unread
            or self.interrupted
            or any(receipt.release_failed for receipt in self.receipts)
        )

    def summary(self) -> str:
        """``restarted 4 · skipped 1 · failed 0``, and the projects that could not be read."""
        done = "restarted" if self.action == "restart" else "stopped"
        acted = self.of("done")
        dead = sum(1 for receipt in acted if receipt.dead)
        # Running and dead apart, when any was dead (#38).
        split = f" ({len(acted) - dead} running, {dead} dead)" if dead else ""
        line = (
            f"{done} {len(acted)}{split} · skipped {len(self.of('skipped'))} · "
            f"failed {len(self.of('failed'))}"
        )
        if self.unread:
            line += f" · {len(self.unread)} project(s) not read"
        return line


def bulk_plan(
    action: BulkAction, *, managers_only: bool = False, acting_session: str | None = None
) -> BulkPlan:
    """Every agent :func:`bulk` would act on in every project, and every one it would leave.

    Read as ``fleet ls`` reads each project (:func:`list_agents`, which records a dead pane
    as it goes), across every registered directory as ``reap --all`` walks them. A row is
    acted on when it is in one of :data:`PROCESS_STATES`, and, for a restart, when it DIED:
    exited or lost for a reason nobody gave it (#38). A restart reads each label's newest
    row too (:func:`_dead_heads`), since a server that died with its agents, or a ``reap``,
    leaves no window for the listing to show. The rest are left, each with why:

    - the caller's own row (:data:`THIS_SESSION`): the one ``AISQUARE_FLEET_AGENT`` names,
      and the one ``acting_session`` (``--as``) is bound to;
    - for a stop, an exited or lost row, by its state: 💤 agents are left as they are;
    - for a restart, a dead row :func:`_left_dead` leaves: stopped on purpose, exited 0,
      dead for over :data:`DEAD_RESTART_WITHIN`, or a label started again since;
    - a row the listing holds as another server's (``FleetAgentStatus.held``): its pane id
      is that server's, and a ``/exit`` typed there is typed into another agent;
    - for a restart, a row whose task is done or dropped, which :func:`restart` refuses;
    - for a restart, a row of a forgotten project (:data:`PROJECT_FORGOTTEN`).

    Every directory is read, captured ones too, and a forgotten registration that holds a
    live row, as ``shutdown`` reads them (:func:`_shutdown_targets`): a ``project forget``
    that races a spawn leaves a tombstone over a running agent, which ``stop --all`` left
    running on a project nobody lists (review of #266, round 2). Only those with a fleet row
    are listed, live or recently ended (:meth:`~aisquare.core.store.ContextStore.fleet_label_heads`
    holds each label's newest): with capture on, every captured directory cost a full
    listing, a store session and the board's sessions included, for no agent at all (review
    of #266, round 3).
    """
    if action not in ("restart", "stop"):
        raise ValueError(f"bulk action {action!r} is neither restart nor stop")
    with store_session() as store:
        every = store.list_projects(include_forgotten=True)
        forgotten = store.forgotten_among([project.id for project in every])
        heads = store.fleet_label_heads()
        with_rows = {agent.project_id for agent in heads}
        live = {agent.project_id for agent in heads if agent.ended_at is None}
        projects = [
            project
            for project in every
            if project.id in with_rows and (project.id not in forgotten or project.id in live)
        ]
        own, own_session = _own_rows(store, acting_session)
    # Each project's label heads, from the one read above (`_label_heads` for one project):
    # read again per project, a plan asked the store 1 + N times (last delta review, n6).
    heads_of: dict[str, dict[str, FleetAgent]] = {}
    for agent in heads:  # oldest first, so the last of a label is its newest
        heads_of.setdefault(agent.project_id, {})[agent.label] = agent
    rows: list[BulkRow] = []
    unread: list[tuple[ProjectInfo, str]] = []
    dead_teams: dict[str, tuple[FleetAgentStatus, ...]] = {}
    for project in projects:
        try:
            statuses = list_agents(project)
            label_heads = heads_of.get(project.id, {}) if action == "restart" else {}
            if action == "restart":
                statuses = [*statuses, *_dead_heads(statuses, label_heads)]
                dead_teams[project.id] = tuple(_dead_of(statuses, label_heads))
            if managers_only:  # before the board is asked about any coder's task
                statuses = [status for status in statuses if status.agent.role == "manager"]
            closed = _closed_tasks(statuses) if action == "restart" else {}
        except Exception as exc:  # one project's listing is its line, never the others'
            unread.append((project, str(exc) or type(exc).__name__))
            continue
        if action == "restart":
            # The manager LAST: brought back after its team, its first message names only
            # the agents still dead (`_dead_team_line`), and it is never told to restart one
            # this run is about to (#38).
            statuses.sort(key=lambda status: status.agent.label == MANAGER_LABEL)
        else:
            # The manager first, as `shutdown` takes it down first: the one agent that spawns
            # others, so a stop of its coders before it has nobody to wake (`_manager_first`).
            statuses.sort(key=lambda status: status.agent.label != MANAGER_LABEL)
        for status in statuses:
            agent = status.agent
            skip: str | None
            if agent.id in own or (own_session is not None and agent.session_id == own_session):
                skip = THIS_SESSION
            elif status.state not in PROCESS_STATES and action == "stop":
                skip = status.state
            elif (
                status.state not in PROCESS_STATES
                and (left := _left_dead(status, label_heads)) is not None
            ):
                skip = left
            elif status.held:
                skip = f"held: {status.detail or ANOTHER_SERVER}"
            elif action == "restart" and project.id in forgotten:
                skip = PROJECT_FORGOTTEN
            else:
                skip = closed.get(agent.id)
            rows.append(BulkRow(project, status, skip))
    return BulkPlan(action, managers_only, tuple(rows), tuple(unread), dead_teams)


def _label_heads(project: ProjectInfo) -> dict[str, FleetAgent]:
    """Each label's newest row in ``project``: the one a restart of that label acts on.

    Picked in SQL (:meth:`~aisquare.core.store.ContextStore.fleet_label_heads`, oldest first,
    so the last of a label is its newest), not from every row the project ever had (#274
    delta review 2, n4). :func:`bulk_plan` takes every project's from its one read; a row's
    turn (:func:`_still_to_restart`) asks again, as it asks the row's status again: what it
    judges is the label as it stands at the turn, after the runs before it."""
    with store_session() as store:
        rows = store.fleet_label_heads(project.id)
    return {row.label: row for row in rows}


def _left_dead(status: FleetAgentStatus, heads: Mapping[str, FleetAgent]) -> str | None:
    """Why a restart by :func:`bulk` leaves this DEAD row, or ``None`` to start it again (#38).

    The owner's rule: an agent that ended for any reason it was not told to — killed,
    crashed, its tmux server gone, a reboot — is restarted; one aisquare stopped on purpose
    is not. A row still live (exited or lost, not recorded yet) died. An ended one says how
    (``FleetAgent.ended_by``); one ended before that column, by its exit status: a signal or
    an error died, and an end with no status may have been a stop. Left besides: a row
    another one took the label from, one whose agent exited 0 (:data:`ENDED_CLEANLY`), and
    one that died over :data:`DEAD_RESTART_WITHIN` ago.
    """
    agent = status.agent
    head = heads.get(agent.label)
    if head is not None and head.id != agent.id:
        return f"{agent.label} was started again since"
    if agent.ended_at is None:
        # Not recorded yet (a listing that could not write): its pane says how it ended.
        return ENDED_CLEANLY if status.detail == _exit_detail(0) else None
    if agent.ended_by == "stopped":
        return STOPPED_ON_PURPOSE
    if agent.exit_status == 0:
        return ENDED_CLEANLY
    if agent.ended_by is None and agent.exit_status is None:
        return status.state  # ended before aisquare said how, with nothing to tell it by
    if _now() - agent.ended_at > DEAD_RESTART_WITHIN:
        return f"died over {DEAD_RESTART_WITHIN.days} days ago"
    return None


def _dead_heads(
    shown: Sequence[FleetAgentStatus], heads: Mapping[str, FleetAgent]
) -> list[FleetAgentStatus]:
    """The dead rows a restart brings back that the live listing does not show (#38): each
    label's newest row, ended, whose window is gone — a server that died with its agents, a
    row ``reap`` recorded — and that :func:`_left_dead` does not leave. A row it would leave
    is not drawn: the listing never showed a stopped agent whose window is gone."""
    listed = {status.agent.id for status in shown}
    found: list[FleetAgentStatus] = []
    for head in heads.values():
        if head.ended_at is None or head.id in listed:
            continue
        status = FleetAgentStatus(agent=head, state="exited", detail=_exit_detail(head.exit_status))
        if _left_dead(status, heads) is None:
            found.append(status)
    return found


def dead_team(project: ProjectInfo, *, but: FleetAgent | None = None) -> list[FleetAgentStatus]:
    """The agents of ``project`` that died and were not stopped on purpose, less ``but``: the
    ones a Restart all brings back (:func:`_left_dead`), whose task is still open (#38)."""
    statuses = list_agents(project)
    heads = _label_heads(project)
    dead = _dead_of([*statuses, *_dead_heads(statuses, heads)], heads)
    return [status for status in dead if but is None or status.agent.label != but.label]


def _dead_of(
    statuses: Sequence[FleetAgentStatus], heads: Mapping[str, FleetAgent]
) -> list[FleetAgentStatus]:
    """The rows of ``statuses`` a Restart all brings back from the dead: no process, not left
    by :func:`_left_dead`, and their task still open (#38)."""
    dead = [
        status
        for status in statuses
        if status.state not in PROCESS_STATES and _left_dead(status, heads) is None
    ]
    closed = _closed_tasks(dead)
    return [status for status in dead if status.agent.id not in closed]


def _dead_team_line(
    project: ProjectInfo,
    manager: FleetAgent,
    team: Sequence[FleetAgentStatus] | None = None,
) -> str | None:
    """A restarted manager's line about its dead team (:func:`dead_team`), one line, or
    ``None`` when none died: each agent's label, state, task and the exact command that
    brings it back, so the manager can restart its team without anyone retyping it (#38).
    Never raises: a listing that fails costs the line, never the manager's restart. ``team``
    is the dead team as its caller read it already (:attr:`BulkPlan.dead_teams`); otherwise
    the project is listed for it."""
    try:
        if team is None:
            dead = dead_team(project, but=manager)
        else:
            dead = [status for status in team if status.agent.label != manager.label]
        if not dead:
            return None
        tasks: dict[str, str] = {}
        with store_session() as store:
            for status in dead:
                agent = status.agent
                if agent.task_id is None:
                    continue
                with suppress(Exception):
                    task = _team().find_task(store, agent.task_id, _board_of_row(agent))
                    if task is not None:
                        ref = f"#{task.number}" if task.number is not None else task.id
                        tasks[agent.id] = f"task {ref} {' '.join(task.title.split())}"
    except Exception:
        return None
    each = []
    for status in dead:
        agent = status.agent
        code = agent.exit_status if status.state == "exited" else None
        said = str(code) if code is not None else status.detail
        state = status.state + (f" ({said})" if said else "")
        about = ", ".join(filter(None, [state, tasks.get(agent.id)]))
        command = project_service.fleet_command("restart", project, agent.label)
        each.append(f"{agent.label} ({about}): `{command}`")
    return (
        f"{len(dead)} of your team died without being stopped on purpose — bring each back "
        f"with its command (it resumes its session): {'; '.join(each)}."
    )


def _own_rows(store: ContextStore, acting_session: str | None) -> tuple[set[str], str | None]:
    """The rows the caller runs in, and its session's full id: the row ``AISQUARE_FLEET_AGENT``
    names, and the live row bound to ``acting_session`` (an id or a prefix of one)."""
    own: set[str] = set()
    if (named := orchestrator.env_fleet_agent()) is not None:
        own.add(named)
    if not acting_session:
        return own, None
    session_id = acting_session
    with suppress(AmbiguousIdError):
        if (session := store.get_session(acting_session)) is not None:
            session_id = session.id
    if (row := store.fleet_agent_for_session(session_id)) is not None:
        own.add(row.id)
    return own, session_id


def _closed_tasks(statuses: Sequence[FleetAgentStatus]) -> dict[str, str]:
    """Why a restart would be refused for its task, by row id: ``task is done``, ``task is
    dropped``. Read on the board each row reads (:func:`_board_of_row`); a task that cannot
    be read is left for :func:`restart` to refuse, as it would without ``--all``."""
    closed: dict[str, str] = {}
    with store_session() as store:
        for status in statuses:
            agent = status.agent
            if agent.task_id is None:
                continue
            with suppress(Exception):
                task = _team().find_task(store, agent.task_id, _board_of_row(agent))
                if task is not None and task.status in CLOSED_STATUSES:
                    closed[agent.id] = f"task is {task.status}"
    return closed


def bulk(
    action: BulkAction,
    *,
    managers_only: bool = False,
    acting_session: str | None = None,
    shown: BulkPlan | None = None,
    on_receipt: Callable[[BulkReceipt], None] | None = None,
) -> BulkReport:
    """Restart or stop every running agent in every project: ``fleet restart|stop --all``,
    and Settings ▸ Fleet in asq. A restart brings back every agent that died too (#38).

    Plans first (:func:`bulk_plan`), then acts on each row in turn, pinned to its id
    (``agent_id``) as the agent view's buttons are: a label the row gave up meanwhile names
    another agent. Each row is looked at again just before it is acted on, and one that
    ended since the plan was read is skipped as :data:`ALREADY_EXITED`. A stop is
    :func:`stop`'s; a restart is :func:`restart`'s, so a running agent is handed over and
    resumes its session when its transcript is on disk. One agent's failure is its own
    receipt, never the end of the rest. ``on_receipt`` is called with each receipt as it is
    made, in this thread: the CLI prints it, asq toasts it.

    ``shown`` is the plan a person confirmed (asq's dialog). The plan is read again here
    all the same, and only the rows ``shown`` acts on are acted on: an agent started since
    was not shown and is left alone, and one shown that has no process by now is skipped as
    :data:`ALREADY_EXITED`. A row the new read leaves for another reason (its task closed
    meanwhile) is skipped with that reason, and a project it could not list is left out
    and named in the report's ``unread``.

    ``acting_session`` is the caller's ``--as``: its row is left (:data:`THIS_SESSION`),
    and a restart records it as the one that asked (``spawned_by``).

    The operator's interrupt (Ctrl-C) is theirs to have, but the agents before it were
    restarted or stopped, so it goes up as :class:`BulkInterrupted` with the report of
    how far it got. As a bare ``KeyboardInterrupt``, ``--json`` printed nothing at all,
    and a script had no record of which agents it had already acted on (review of #266,
    round 2).
    """
    if shown is not None and (shown.action, shown.managers_only) != (action, managers_only):
        raise ValueError("the plan shown is for another action or scope than the one asked")
    receipts: list[BulkReceipt] = []
    unread: tuple[tuple[ProjectInfo, str], ...] = ()
    doing = "restarting" if action == "restart" else "stopping"
    where = "before anything was done"
    try:
        now = bulk_plan(action, managers_only=managers_only, acting_session=acting_session)
        unread = now.unread
        rows = now.rows if shown is None else _as_shown(shown, now)
        for row in rows:
            named = f"{row.agent.label} ({_name(row.project)})"
            if row.skip is not None:
                receipt = BulkReceipt(row.project, row.agent, "skipped", row.skip, dead=row.dead)
            else:
                where = f"while {doing} {named}"
                # A manager is restarted after its team (`bulk_plan`): told of those this
                # run did not bring back (#38).
                back = {r.agent.id for r in receipts if r.outcome == "done"}
                team = [
                    status
                    for status in now.dead_teams.get(row.project.id, ())
                    if status.agent.id not in back
                ]
                receipt = _bulk_one(action, row, spawned_by=acting_session or "user", team=team)
            receipts.append(receipt)
            where = f"after {named}"
            if on_receipt is not None:
                on_receipt(receipt)
    except KeyboardInterrupt:
        said = f"interrupted {where}; the rest were left as they were"
        report = BulkReport(action, managers_only, tuple(receipts), unread, interrupted=said)
        raise BulkInterrupted(report) from None
    return BulkReport(action, managers_only, tuple(receipts), unread)


def _as_shown(shown: BulkPlan, now: BulkPlan) -> list[BulkRow]:
    """The rows ``shown`` drew, as the read made ``now`` finds them (:func:`bulk`).

    A project ``now`` could not list (``now.unread``) is left out, rows and all: nobody
    looked at its agents, so none is "already exited" (review of #266, round 2), and the
    report names the project and why, as it does with no dialog.
    """
    current = {row.agent.id: row for row in now.rows}
    unread = {project.id for project, _ in now.unread}
    rows: list[BulkRow] = []
    for row in shown.rows:
        found = current.get(row.agent.id)
        if row.project.id in unread:
            continue
        if row.skip is not None:
            rows.append(row)  # left when it was drawn, and left now for the reason it was shown
        elif found is None:
            rows.append(replace(row, skip=ALREADY_EXITED if shown.action == "stop" else GONE_SINCE))
        elif shown.action == "stop" and found.status.state not in PROCESS_STATES:
            rows.append(replace(row, skip=ALREADY_EXITED))
        else:
            # A restart acts on a row that died since it was shown (#38): it is started
            # again as a dead one is. The new read's reason to leave it stands.
            rows.append(found)
    return rows


def _bulk_one(
    action: BulkAction,
    row: BulkRow,
    *,
    spawned_by: str,
    team: Sequence[FleetAgentStatus] = (),
) -> BulkReceipt:
    """One row's stop or restart, as its receipt whatever happened.

    The row is looked at again just before it is acted on (:func:`_bulk_still_running`): a
    stop of six agents takes up to half a minute, and one that ended in that time was
    acted on as it stood by its turn. :func:`restart` works on a row whatever its state,
    so the exited agent was started again; :func:`stop` removed its 💤 window and said
    "stopped" (review of #266, round 2). A restart looks in ``before_stop``, after its
    own refusals and before anything is stopped; a stop looks first, and is pinned to a
    running row (``live_only``).
    """
    project, agent, dead = row.project, row.agent, row.dead

    def still_to_restart() -> None:
        _still_to_restart(row)

    try:
        result: StopReceipt | RestartReceipt
        if action == "stop":
            _bulk_still_running(row)
            result = stop(project, agent.label, agent_id=agent.id, live_only=True)
        else:
            result = restart(
                project,
                agent.label,
                agent_id=agent.id,
                spawned_by=spawned_by,
                before_stop=still_to_restart,
                team=team,
            )
    except EndedMeanwhile:
        # This turn's `/exit` took the agent down, and another writer (a `fleet stop`, a
        # shutdown) ended its row first: never "left alone", and a restart started no
        # replacement (review of #266, round 3).
        after = "; no replacement was started" if action == "restart" else ""
        why = f"its /exit was sent, and another command ended it meanwhile{after}"
        return BulkReceipt(project, agent, "failed", why, dead=dead)
    except NoSuchAgent as exc:
        # Pinned to the row: a stop's ended between the read and its turn, and its label is
        # free, still its own or another agent's now; a restart's was stopped on purpose or
        # started again meanwhile (#38). Nothing was done to any of them.
        why = ALREADY_EXITED if action == "stop" else str(exc)
        return BulkReceipt(project, agent, "skipped", why, dead=dead)
    except FleetError as exc:
        return BulkReceipt(project, agent, "failed", str(exc), dead=dead)
    except Exception as exc:  # one agent's failure is its receipt, never the rest's
        return BulkReceipt(project, agent, "failed", f"{type(exc).__name__}: {exc}", dead=dead)
    return BulkReceipt(project, agent, "done", result=result, dead=dead)


def _bulk_still_running(row: BulkRow) -> None:
    """Refuse ``row`` (``NoSuchAgent``) unless its agent, looked at now where its launch
    record places it, is in one of :data:`PROCESS_STATES` — the row's ``ended_at`` deciding
    nothing, as everywhere a row is placed (review r8 of #113).

    One row, read again and asked of its own server (:func:`status_of`). The project's
    whole listing, read before each agent, asked tmux about every pane of the project for
    one row's answer: a manager and eight coders were listed ten times in one ``restart
    --all`` (review of #266, round 3). A dead pane it finds is recorded as a listing
    records one (:func:`_end_dead_rows`): its claims released and the manager told now,
    not at the next read.
    """
    with store_session() as store:
        agent = store.get_fleet_agent(row.agent.id)
    now = None if agent is None else status_of(agent)
    if agent is not None and now is not None and now.state not in PROCESS_STATES:
        with suppress(Exception):  # the next listing records it; the skip stands either way
            placement = resolve_place(agent)
            _end_dead_rows([placement], _observe_sockets([placement]))
    if now is None or now.state not in PROCESS_STATES:
        raise NoSuchAgent(f"{row.agent.label!r} ({row.agent.id}) has no process any more")


def _still_to_restart(row: BulkRow) -> None:
    """Refuse ``row`` (``NoSuchAgent``, saying why) when a restart by :func:`bulk` should leave
    it now, read again just before it is acted on (#38).

    A row that died since the plan was read is restarted as a dead one is, by the same rule
    (:func:`_left_dead`): its death is recorded first, as :func:`_bulk_still_running` records
    one, where its launch record places it, so an agent that exited 0, or one stopped on
    purpose meanwhile, is left. One that is gone is left too. :func:`restart` refuses a row
    another took the label from (its pin).
    """
    with store_session() as store:
        agent = store.get_fleet_agent(row.agent.id)
        project = store.get_project(row.project.id) or row.project
    if agent is None:
        raise NoSuchAgent(GONE_SINCE)
    recorded: list[FleetAgent] = []
    if agent.ended_at is None:
        now = status_of(agent)
        if now.state in PROCESS_STATES:
            return
        with suppress(Exception):  # unrecorded, it reads as a death all the same
            # No manager woken for an agent coming back: `restart` holds the wake-up for
            # a replacement that fails to start, as it holds its own (m1).
            placement = resolve_place(agent)
            _end_dead_rows(
                [placement],
                _observe_sockets([placement]),
                nudge=False,
                ended_here=recorded,
            )
        with store_session() as store:
            agent = store.get_fleet_agent(agent.id) or agent
        status = now.model_copy(update={"agent": agent})
    else:
        status = FleetAgentStatus(
            agent=agent, state="exited", detail=_exit_detail(agent.exit_status)
        )
    left = _left_dead(status, _label_heads(project))
    if left is not None:
        # Not coming back after all: the wake-up THIS look held back is owed. A row another
        # reader ended was announced by it (final delta review, n2).
        for ended in recorded:
            nudge_manager(
                ended.project_id, reason=f"{ended.label} exited", board=_board_of_row(ended)
            )
        raise NoSuchAgent(left)


def reap(project: ProjectInfo | None = None, *, server_down: bool = False) -> ReapReport:
    """Record dead panes as ended, mark vanished panes lost, remove finished worktrees.

    When tmux cannot be asked nothing is marked: absence of evidence is not a
    dead pane, and an unreachable tmux must not end rows whose processes may
    still be running. That is decided per SERVER — each row is asked of the
    server it was started on, at the socket its spawn recorded (:func:`_address`)
    — so an operator who changed ``[fleet] tmux_socket`` does not thereby lose
    every agent still running on the old one, and a shell under another
    ``TMUX_TMPDIR`` does not read another server's panes as the rows'.

    ``server_down`` is the operator's word that a server which does not answer
    is genuinely gone (a reboot swept ``/tmp``; ``kill-server``). Even then the
    rows are marked only on a socket tmux ITSELF reports as having no server
    (:meth:`~aisquare.core.tmux.TmuxServer.server_absent`): a client that exits
    non-zero for a protocol mismatch, a wedged server or, for a row from before
    v25 that recorded no socket, a socket under another ``TMUX_TMPDIR`` is not a
    dead server, and the agents behind it are alive.
    And of the rows on such a socket, only those whose agent cannot be running
    by the one rule ``stop``, ``shutdown`` and ``uninstall`` ask
    (:func:`~aisquare.core.tmux.server_may_run`): tmux reports no server for a
    socket file taken from under a running one too, and the word marked those
    rows lost under their agents, on another server under another
    ``TMUX_TMPDIR`` as well (round 29 of #257 had only the doctor warn). The
    rest are HELD and named on the report (:func:`_hold_on_absent_server`), and
    ``shutdown --forget`` is the word that ends them. So is a row the listing holds as
    another server's, with the word or without it (:func:`_hold_on_another_server`).
    For the rows the word may act on the socket is then a server with no panes —
    no view for them, and the ordinary "pane is gone → lost" branch does the rest.
    A missing tmux binary marks nothing: that is a question that could not be put.

    A row a hand-over is stopping (:func:`_handed_over`) is left to it, dead or
    gone, as every listing leaves it.
    """
    config = settings()
    report = ReapReport()
    absent: dict[_Address, bool] = {}
    reads = tmux_core.SocketReads()  # one per server for the sweep, not per row
    now = _now()
    with store_session() as store:
        if project is not None:
            projects = [store.get_project(project.id) or project]
        else:
            projects = store.list_projects(all=True)  # a captured directory can hold agents
        for current in projects:
            # Observed and ended where their launch records place them (`resolve_place`); a
            # row no record places is asked about nowhere, and marked nothing.
            live = _placed_only(
                _resolve_all(store.fleet_agents(current.id, live_only=True), config)
            )
            if live:
                views = _observe_sockets(live, reads=reads)
                _hold_on_another_server(current, live, views, report)
                if server_down:
                    for address, view in views.items():
                        if view is None:
                            # Asked once per server for the whole sweep, so a
                            # server coming up mid-sweep cannot split the answer.
                            if address not in absent:
                                try:
                                    absent[address] = _server_at(address, config).server_absent()
                                except TmuxError:
                                    # Unreachable TODAY: `server_absent` promises
                                    # False for every question it could not put
                                    # (core/tmux, round 7 of #203). Kept as the
                                    # belt for a future predicate that raises
                                    # again — a question that could not be put is
                                    # no evidence of absence, and this sweep must
                                    # never end on one. Do not copy the pattern
                                    # to a call site that has the promise.
                                    absent[address] = False
                            if absent[address]:
                                # No server, tmux says: no panes for the rows whose agent
                                # cannot be running; the rest held, as `shutdown` holds them.
                                views[address] = _hold_on_absent_server(
                                    current,
                                    [placed for placed in live if placed.pane.address == address],
                                    report,
                                    reads,
                                )
                for placed in live:
                    agent = placed.row
                    observed = _view(placed, views)
                    if observed is None:
                        continue  # that server could not be asked: nothing is marked
                    pane = _pane_in(agent, observed)
                    if (pane is None or pane.dead) and _handed_over(
                        agent,
                        store.get_session(agent.session_id) if agent.session_id else None,
                        now,
                    ):
                        # A hand-over is stopping it, as for every read's reconcile
                        # (`_end_dead_rows`): dead between its `/exit` and its kill, gone
                        # between the kill and its end, the row is the hand-over's to end.
                        # Reaped in that gap, the parked claims went back to the pool, the
                        # manager was woken, and the switch failed on its own row with the
                        # agent already down (review of #163, round 1).
                        continue
                    if pane is None:
                        lost = store.end_fleet_agent(agent.id, exit_status=None, ended_by="died")
                        released, failed = _release_or_say(store, lost, why="agent lost")
                        _count_release(report, lost, released, failed)
                        report.lost.append(lost)
                    elif pane.dead:
                        # A compare-and-set, as for every read's reconcile: a listing
                        # that recorded this death since `live` was read announced it,
                        # and released what its session held (`_end_dead_rows`).
                        ended = store.end_fleet_agent_if_live(
                            agent.id, exit_status=pane.dead_status, ended_by="died"
                        )
                        if ended is not None:
                            # A crash fires no ``SessionEnd``; a clean exit already
                            # released, and finds nothing more to release here. The
                            # release is the courtesy after the row is ended, as in
                            # `stop`: refused, it is said, never raised out of the
                            # sweep (round 5).
                            released, failed = _release_or_say(store, ended, why="agent exited")
                            _count_release(report, ended, released, failed)
                            report.ended.append(ended)
                            _emit_exit(store, ended)
            _remove_finished_worktrees(store, current, report)
    gone: dict[str, list[str]] = {}
    for row in (*report.ended, *report.lost):
        gone.setdefault(row.project_id, []).append(row.id)
    for project_id, rows in gone.items():
        launch_seal.forget_row_windows(project_id, rows)
    for ended in report.ended:
        nudge_manager(ended.project_id, reason=f"{ended.label} exited", board=_board_of_row(ended))
    return report


def _hold_on_absent_server(
    project: ProjectInfo,
    placements: Sequence[Placed],
    report: ReapReport,
    reads: tmux_core.SocketReads | None = None,
) -> _Observed:
    """The view ``reap --server-down`` takes of a server tmux reports absent: no pane for a
    row whose agent cannot be running, which reads lost; every other row held, as a row of
    a server that may run (:func:`_unreached`), with why on the report.

    tmux reports no server for a socket file taken from under a running server too
    (``error connecting to … (No such file or directory)``), and ``server_absent`` alone
    marked such rows lost under their agents, on another server under another
    ``TMUX_TMPDIR`` as well (round 29 of #257 had only the doctor warn). The word may end
    only what the one rule says cannot be running
    (:func:`~aisquare.core.tmux.server_may_run`): a row from another boot, or one whose
    socket file is there with nothing listening. The way out names ``fleet shutdown
    --forget``, the operator's word that ends a held row. ``reads`` is the sweep's. The rows
    are placed where their launch records put them (:func:`resolve_place`).
    """
    view: _Observed = {}
    for placed in placements:
        unreached = _unreached(placed, forget=_forget(project), reads=reads)
        if unreached is not None:
            view[placed.row.id] = HELD
            report.held.append(
                ShutdownRow(placed.row, f"{unreached[1]} — left live: {unreached[2]}")
            )
    return view


def _hold_on_another_server(
    project: ProjectInfo,
    placements: Sequence[Placed],
    views: Mapping[_Address, _Observed | None],
    report: ReapReport,
) -> None:
    """Name on ``report`` each row ``views`` holds as another server's (:func:`_observe`),
    with why and the way out, as :func:`_hold_on_absent_server` names the rows it holds.

    ``reap`` leaves such a row live, as it leaves a row whose server could not be asked,
    and said nothing of it: ``0 ended, 0 lost``, while the row still counted toward
    ``max_agents_per_project`` and the one-manager rule, and ``stop`` refused it (review
    of #259, round 1). The way out names ``fleet shutdown --forget``.
    """
    for placed in placements:
        if not _held_by_another(placed.agent, views.get(placed.pane.address)):
            continue
        try:
            path = placed.agent.socket_path or placed.pane.server().socket_path()
        except TmuxUnavailable:
            continue  # no POSIX uid: no tmux server can exist here
        why, way = _unreachable_why("another", path, forget=_forget(project))
        report.held.append(
            ShutdownRow(placed.row, f"{why} — left live: {way[0].lower() + way[1:]}")
        )


def _emit_exit(store: ContextStore, agent: FleetAgent, *, why: str | None = None) -> None:
    """The ``agent_exited`` board event the manager's Stop hook wakes on (§7.3), with
    ``why`` after the status when the status does not say it all."""
    status = "?" if agent.exit_status is None else str(agent.exit_status)
    # The row is already ended; the event is the courtesy, not the record. On the board the
    # row recorded (`_board_of_row`), which its manager reads: under a hub not the row's
    # project's, and never the board of whichever process ends the row, a session or none
    # (review of #259, round 25, 4/10).
    with contextlib.suppress(Exception):
        _team()._emit(
            store,
            _board_of_row(agent),
            "agent_exited",
            f"{agent.label} exited ({status})" + (f": {why}" if why else ""),
            session_id=agent.session_id,
        )


def rename(project: ProjectInfo, codename: str, *, notes: list[str] | None = None) -> ProjectInfo:
    """Set the fleet codename (validated, unique) and rename the tmux sessions with it.

    The sessions renamed are the ones the project's rows' launch records name, by tmux's
    id (:attr:`Placed.session`), never ``asq-<old codename>``, a name read from the store
    (review r7 of #113, finding 2): those are the project's (:func:`_session_to_join`),
    and one taken down or joined later is found by its id, whatever it is called. The
    records take the new name whether or not tmux could be told, so none names a session
    by the old one (:func:`_rename_recorded_sessions`).

    The row is renamed even when tmux is unreachable — the panes are addressed by
    id, not by session name — but that fail-open is not free and no longer silent: a
    session left under the OLD name is what ``tmux`` itself shows. So ``notes`` collects
    what it cost, the way :class:`SpawnReceipt` does, naming the session that still holds
    the old name and the one tmux command that fixes it.
    """
    if not codenames.is_codename(codename):
        raise FleetError(
            f"codename {codename!r} must look like adjective-animal: two words of "
            "3 to 7 lowercase letters joined by '-'"
        )
    with store_session() as store:
        store.onboard_project(project)
        current = store.get_project(project.id) or project
        old = current.codename
        if old == codename:
            return current
        try:
            updated = store.set_codename(project.id, codename)
        except sqlite3.IntegrityError as exc:
            raise FleetError(f"codename {codename!r} is already taken by another project") from exc
        cutoff = _now() - RECENTLY_ENDED
        rows = [
            row for row in store.fleet_agents(project.id, live_only=False)
            if row.ended_at is None or row.ended_at >= cutoff
        ] if old else []  # fmt: skip
    if old:
        config = settings()
        new_session = session_name(codename)
        placements = _placed_only(_resolve_all(rows, config))
        # Every session of the project's (its spawns', its records', its legacy one), each
        # under a name of its own: two renamed alike, tmux refused the second as a
        # duplicate (review r8 of #113, finding 4).
        sessions = [
            session
            for session in _project_sessions(project.id, placements, config, every_legacy=True)
            if session.present()
        ]
        names: dict[tuple[_Address, str], str] = {}
        for index, session in enumerate(sessions, start=1):
            name = new_session if index == 1 else f"{new_session}-{index}"
            # Each record follows its own session, by tmux's id, whether or not tmux could
            # be told: one name written into every record named the other session (review
            # r9 of #113, finding 10).
            names[(session.address, session.target)] = name
            try:
                if session.present_or_raise():
                    session.rename(name)
                    launch_seal.rename_project_session(
                        project.id, session.target, session.address.name, name
                    )
            except TmuxError as exc:
                if notes is not None:
                    by = (
                        ["-L", session.address.name]
                        if session.address.path is None
                        else ["-S", str(session.address.path)]
                    )
                    remedy = install_route.command_line(
                        ["tmux", *by, "rename-session", "-t", session.target, name]
                    )
                    notes.append(
                        f"the codename is {codename}, but tmux could not be told ({exc}) — its "
                        f"session {session.name} on {session.address} keeps that name until it "
                        f"is renamed: `{remedy}` does that, on the fleet's server (review r8 of "
                        "#113, finding 7), and renaming again will not (it "
                        "renames from the stored codename, which is already the new one). "
                        "`fleet ls`, `tell`, `stop`, `shutdown` and `attach` are unaffected — "
                        "they find panes and sessions by tmux's ids"
                    )
        _rename_recorded_sessions(placements, names)
    return updated


def _rename_recorded_sessions(
    placements: Sequence[Placed], names: Mapping[tuple[_Address, str], str]
) -> None:
    """Each launch record of ``placements`` names its session as ``names`` now calls it (by
    the server its record places it on and tmux's id for it), whether or not tmux renamed
    it: a record left on an old name named, once another project took that codename, the
    other project's session (review r7 of #113, finding 2), and one name written into the
    records of two sessions named the other (r9, finding 10). The session itself is found by
    tmux's id (``WindowPlace.session_id``), so the name is what a report and ``attach`` call
    it. Written from here, outside any sandbox; one that cannot be written keeps the old
    name. Never raises."""
    for placed in placements:
        window = placed.record.window
        if window is None or window.session_id is None:
            continue
        name = names.get((placed.pane.address, window.session_id))
        if name is not None and name != window.session:
            with contextlib.suppress(Exception):
                launch_seal.placed(placed.row.id, window.model_copy(update={"session": name}))


def pause(project: ProjectInfo, *, session_ref: str | None = None) -> None:
    """Set the project's ``fleet-paused`` board signal — its manager spawns nothing while it
    is on.

    On the board its newest manager row recorded, the boards its live rows recorded, its
    checkout's and the one a spawn from here gives (:func:`_pause_boards`), by the project's
    own name there
    (:func:`_pause_signals`): under a hub the hub's board, where every other fleet project's
    manager reads its own project's pause, not this one."""
    _set_pause(project, "on", session_ref)


def resume(project: ProjectInfo, *, session_ref: str | None = None) -> None:
    """Set the project's ``fleet-paused`` signal off where it reads on (:func:`_set_pause`)."""
    _set_pause(project, "off", session_ref)


def _set_pause(project: ProjectInfo, value: str, session_ref: str | None) -> None:
    """Set ``project``'s pause to ``value``: ``on`` on every board it lives on
    (:func:`_pause_signals`), ``off`` wherever it reads on (:func:`_pause_where_on`).

    A resume whose own pause does not parse on some board is refused, naming it, and nothing
    is written: the write reads the value it replaces. So is a pause whose own does not parse
    on a board it is set on, every board worked out before any is written: set on the boards
    sorted before it, ``fleet pause`` then ended in a traceback, ``--json`` printed nothing,
    and the project was paused on some boards and not on others (review of #259, round 33,
    1/10). Another project's that does not parse is no concern of this one's
    (:func:`team.board_signals_in`). Every board's is written in one transaction through the
    store the boards were worked out in (:func:`team.set_signals_in`): one ``set_signal`` a
    board, each in a store of its own, a store error on a later board (``database is
    locked``) left the earlier ones set and ended in a traceback all the same (round 34,
    5/10); it rolls every one back now. A lock is said as a ``FleetError`` that says to try
    again (:func:`is_locked_error`), and nothing else is: a store found damaged
    (``database disk image is malformed``, a ``StoreUnopenable``) reaches the CLI's root
    group, which says to move it aside. Said as "try again" too, it was told to retry what
    never works, and never how to recover (round 35, 1/8). Any other store error (``database
    or disk is full``, a readonly store, ``disk I/O error``) is a ``FleetError`` that names
    it: the root group maps damage alone, and re-raised, it ended in a traceback, ``--json``
    printing nothing (round 36, 2/9).

    Each write is a ``signal`` event in the delta of every session on that board. Off on
    every board one of the project's rows ever recorded, ``fleet resume -P a`` put
    ``fleet-paused.a: off`` in the next prompt of every agent on every hub ``a`` ever ran
    under, where it had never been paused (review of #259, round 30, 5/8). With nothing on,
    ``--as`` is still checked (:func:`team.set_signals_in` resolves it with no signal to
    write): an unknown session is refused, as a write refuses it, and so is a prefix of
    several, as :func:`_task_for` refuses one of a task. Left to the CLI, which catches only
    ``FleetError``, ``fleet resume --as ses_a`` ended in a traceback, and ``--json`` printed
    nothing (round 31, 2/7). Checked here too, it was looked up twice on every resume (round
    35, 7/8).
    """
    team = _team()
    doing = "set" if value == "on" else "clear"
    try:
        if not orchestrator.team_enabled():
            raise team.TeamDisabledError  # refused before any store opens
        homes = _pause_homes(project) if value == "on" else set()
        unreadable: list[tuple[str, str]] = []
        with store_session() as store:
            states = team.board_signals_in(store, unreadable=unreadable)
            if value == "on":
                signals = _pause_signals(store, project, homes)
            else:
                signals = _pause_where_on(project, states)
            broken = [
                where
                for where in _pause_where_on(project, unreadable)
                if value == "off" or where in signals
            ]
            if broken:
                named = ", ".join(f"{name} on {board}" for board, name in broken)
                raise FleetError(f"cannot {doing} the pause: {named} could not be read")
            team.set_signals_in(store, signals, value, session_ref=session_ref)
    except sqlite3.Error as exc:
        if isinstance(exc, StoreUnopenable) or is_corrupt_error(exc):
            raise  # damage, which the CLI's root group says how to recover from
        again = "; try again" if is_locked_error(exc) else ""
        raise FleetError(  # every write rolled back
            f"cannot {doing} the pause: the store refused it ({type(exc).__name__}: {exc}) — "
            f"nothing was written{again}"
        ) from exc
    except team.TeamDisabledError as exc:
        raise FleetError(f"cannot set the pause signal: {exc}") from exc
    except AmbiguousIdError as exc:
        raise FleetError(f"session {session_ref!r} is ambiguous — use more of the id") from exc
    except KeyError as exc:
        raise FleetError(f"unknown session {session_ref!r}") from exc


def _pause_homes(project: ProjectInfo) -> set[str]:
    """The boards ``project``'s pause lives on whatever its rows recorded
    (:func:`_pause_boards`): its checkout's, where a spawn from a caller without a hub reads,
    and the one a spawn from here gives (:func:`_board_for`), the hub's under one.

    Asks git, before the store opens, and only to set the pause: reading or clearing it
    finds it by name (:func:`_pause_where_on`).
    """
    checkout = _checkout_board(project)
    return {checkout, _board_for(project)} if orchestrator.team_hub() is not None else {checkout}


def _pause_boards(store: ContextStore, project: ProjectInfo, homes: Collection[str]) -> list[str]:
    """The boards ``project``'s ``fleet-paused`` signal is set on: the ones a manager of it
    reads now or would read next. The board its newest manager row recorded, live or ended
    (:meth:`ContextStore.fleet_manager_board`, :func:`_board_of_row`), where it reads, or
    where a ``restart`` of it puts its replacement; the boards its live rows recorded, where
    the manager spawned next beside them reads; and ``homes`` (:func:`_pause_homes`), where
    the next manager a spawn from here gives reads.

    The live rows' boards whether or not it ever had a manager: its coders were spawned where
    its next manager will be, a hub this process may not export. Its homes alone, a pause
    from a plain shell went on the checkout's board, and the manager spawned next from
    ``asq`` under the coders' hub read none there and staffed (review of #259, round 33,
    4/10). Asked only before its first manager, any manager row, one stopped last week on
    the checkout's board included, left the coders' hub unpaused, as before (round 34,
    2/10).

    Whether that manager is live and whether the caller exports the hub or not. Taken from
    the rows live at the moment, a pause set on a hub where the manager read was left on
    there once that manager stopped, and the next manager spawned under the hub came up
    staffing nothing (review of #259, round 29, 1/8). ``fleet resume`` and ``fleet
    shutdown`` now find the pause wherever it is on, by its name
    (:func:`_pause_where_on`), so a board no manager of the project reads any more needs
    none: set on every board any row ever recorded, ``fleet pause -P a`` put ``a``'s pause,
    and its resume the ``off``, in the next prompt of every agent on every hub ``a`` ever
    ran under (round 32, 2/9). Only a manager reads the pause, so a board only a stopped
    coder, or a manager long since replaced, recorded is not one; a live coder's is, where
    the manager spawned next beside it reads (round 35, 8/8). A ``SELECT`` each, so no row
    is built: every spawn, restart and switch adds a row that is never deleted (round 26,
    10/11).

    A signal is the board's (docs/fleet.md, "Under a hub: one board, many fleet projects"),
    and the manager asks its own session's board, as its cycle line tells it to. Set, read
    and cleared on the project's own board, under a hub a paused fleet's manager kept
    spawning, and a pause set where it does read was never cleared by ``fleet shutdown``
    (review of #259, round 25, 2/10). By what name: :func:`_pause_signals`.
    """
    manager = store.fleet_manager_board(project.id)
    live = store.fleet_agent_boards(project.id, live_only=True)
    return sorted({*([manager] if manager is not None else []), *live, *homes})


def _pause_signals(
    store: ContextStore, project: ProjectInfo, homes: Collection[str]
) -> list[tuple[str, str]]:
    """``project``'s pause: each board it lives on (:func:`_pause_boards`), with the name
    of the project's ``fleet-paused`` signal there (:func:`harness.pause_signal`).

    The project's own, on a board other projects read too: under a hub, ``fleet pause -P a``
    paused every manager on the hub, and ``fleet shutdown -P a`` cleared the pause ``b``'s
    manager still obeyed (review of #259, round 26, 1/11).
    """
    return [
        (board, harness.pause_signal(project.id, board))
        for board in _pause_boards(store, project, homes)
    ]


def _pause_where_on(
    project: ProjectInfo, signals: Mapping[tuple[str, str], Any] | Sequence[tuple[str, str]]
) -> list[tuple[str, str]]:
    """``project``'s pause where it reads ON: each (board, name) of ``signals``, every board's
    named states (``team.board_signals_in``, read once on a caller's connection), that is the
    project's ``fleet-paused`` there (:func:`harness.pause_signal`). Of a sequence of (board,
    name), as ``board_signals_in`` lists those it could not read, each that is the project's.

    The read half of the pause. A signal's name says whose pause it is, on whichever board:
    ``fleet-paused`` on the project's own, ``fleet-paused.<id>`` on any other. Found from the
    boards its rows recorded and its homes (:func:`_pause_boards`, :func:`_pause_homes`),
    ``fleet resume`` and each visible project of ``fleet shutdown --all`` ran ``git`` for a
    pause they almost never found (review of #259, round 31, 5/7); now they
    run none, and a pause on a board no row of the project names any more is found too.
    """
    if not isinstance(signals, Mapping):
        return [(board, name) for board, name in signals if _is_pause_of(project, board, name)]
    return [
        (board, name)
        for (board, name), state in sorted(signals.items())
        if state.value == "on" and _is_pause_of(project, board, name)
    ]


def _is_pause_of(project: ProjectInfo, board: str, name: str) -> bool:
    """Whether ``name`` on ``board`` is ``project``'s pause (:func:`harness.pause_signal`)."""
    return name == harness.pause_signal(project.id, board)


def _clear_pause_signal(store: ContextStore, signals: Sequence[tuple[str, str]]) -> None:
    """Set each of ``signals`` (board, name) OFF through ``store``, all or none — the write
    half of the pause for the same pass; ``resume`` is the command's own door."""
    _team().set_signals_in(store, signals, "off")


def nudge_manager(project_id: str, *, reason: str, board: str | None = None) -> bool:
    """Wake a WAITING manager with one fixed line + Enter (§7.3); ``False`` if not sent.

    ``board`` is the board the news is on, an agent's exit on the one its row recorded: a
    manager that reads another is not woken. The line wakes it to its own board's delta,
    which does not hold the news, and Remote took the crash for handled (review of #259,
    round 26, 4/11). A board write's nudge is asked the same before it is sent
    (``team._nudge_manager``, which passes ``board``).

    Never raises and never touches a manager that is working or needs the
    human: this runs inside sub-agents' CLI processes, where a failure must
    cost nothing but the nudge. ``reason`` is for the caller's own log — the
    nudge text is fixed and carries nothing (the delta injection does).

    Waiting is decided by :func:`_derive` — the one derivation ``fleet ls`` and
    ``fleet tell`` also go through — and NOT by the board row's ``state``
    field, which that derivation trusts only while the row is fresh
    (``_STALE_AFTER``). A row frozen at ``waiting`` while the pane keeps
    printing (hooks that stopped firing: a Stop that never ran, a session id
    that no longer matches) is ``working`` everywhere else, and typing into it
    is the interleaving ``tell`` refuses to cause. The pane's last-output time it needs for
    that comes with the pane's facts (``PaneFacts.last_output``, ``#{window_activity}``),
    in the one ``display-message``. A ``list-panes -a`` over every pane on the server for it
    was one more tmux process per nudge (review of #259, round 5). And ``waiting`` must be
    at its prompt as ``tell`` decides it (:func:`_at_its_prompt`), the line typed as
    ``tell`` types one (:meth:`PlacedPane.type_line`): a Codex manager kept every nudge sent with
    its Enter in one burst unsent in its composer, and woke for none of them.

    A tmux that refuses to be asked is a writer that may not reach it: a command in
    Codex's sandbox, which runs in namespaces of its own where the fleet's socket is
    ``Operation not permitted`` (measured on 0.162.1). Every board write a sandboxed
    agent made there lost its nudge, and the fleet stalled on a waiting manager; its
    broker (``services.store_broker``) now runs those writes outside the sandbox, and
    this is for one tmux refuses all the same. The sandbox is not widened to the socket
    — that would let it type into any pane — so the nudge is left owed
    (:data:`NUDGE_OWED`) for a process outside it to send: :func:`deliver_owed_nudge`,
    from the writer's own hooks, which Codex runs outside.
    """
    return _nudge(project_id, owe=True, board=board)


def deliver_owed_nudge(project_id: str) -> bool:
    """Send the nudge a board write owed ``project_id``'s manager (:func:`nudge_manager`);
    ``False`` when none was owed or it is not due. Never raises.

    Run by the hooks of an agent that sandboxes its tools, which the agent runs outside
    the sandbox (measured on Codex 0.162.1: children of the agent's process, in its
    namespaces, where tmux answers): its ``PostToolUse`` fires as the write's command
    ends, its ``Stop`` at the turn's. Taken once, by whoever deletes the mark first, and
    not owed again when this one cannot reach tmux either. Due only while the manager
    has not read news it wakes for (:func:`services.team.wake_pending`): the mark says
    a write was made, not that it is unread, and a manager that has since read it is
    not woken for nothing. The nudge is the fixed line, decided as any other.
    """
    try:
        with store_session() as store:
            if not store.delete_meta(NUDGE_OWED + project_id):
                return False
    except Exception:
        return False
    return _nudge(project_id, owe=False)


def _nudge(project_id: str, *, owe: bool, board: str | None = None) -> bool:
    """:func:`nudge_manager`, leaving the nudge owed where tmux refused it when ``owe``;
    otherwise :func:`deliver_owed_nudge`'s, due only for unread news. Typed into the pane
    the manager's launch record places, by its record's agent's rules (:func:`resolve_place`),
    never its row's: a delivery runs from a sandboxed agent's own hooks, outside its
    sandbox, and the row was the sandbox's to write before its broker (review r5 of
    #113, finding 1; r6, finding 1). ``board`` as for :func:`nudge_manager`."""
    try:
        with store_session() as store:
            found = _by_label(store, project_id, MANAGER_LABEL, live_only=True)
            if found is None:
                return False
            row, record = found
            if row.session_id is None:
                return False
            placement = resolve_place(row, record)
            why = placement.why if isinstance(placement, Unplaced) else placement.rewritten
            if not isinstance(placement, Placed) or why is not None:
                _say_refused(
                    project_id,
                    row,
                    "it is not woken",
                    why or "",
                    "it reads the board when prompted",
                )
                return False
            manager, pane = placement.agent, placement.pane  # its place and agent: the record's
            if board is not None and _board_of_row(manager) != board:
                return False
            session = store.get_session(row.session_id)
            if session is None or session.ended_at is not None:
                return False
            if not owe and not _team().wake_pending(store, session):
                return False
            key = f"nudge:{session.id}"
            now = _now()
            last = store.get_meta(key)
            if last is not None and now - datetime.fromisoformat(last) < NUDGE_DEBOUNCE:
                return False
            try:
                facts = pane.facts_or_raise()
            except TmuxError:
                if owe:
                    store.set_meta(NUDGE_OWED + project_id, now.isoformat())
                return False
            if facts is None or not _its_agent(manager, facts):
                return False  # not the agent, or the id is another agent's on another server
            view = _PaneView(
                facts.dead, facts.dead_status, facts.current_command, facts.last_output
            )
            state, _ = _derive(manager, session, view, observed=True, now=now)
            if not _at_its_prompt(pane, manager, session, state, now):
                return False
            store.set_meta(key, now.isoformat())
            pane.type_line(NUDGE_TEXT)
            return True
    except Exception:
        return False


def attach_argv(project: ProjectInfo) -> list[str]:
    """The ``tmux attach`` command for the project's fleet session (the escape hatch).

    Refuses when the session does not exist. ``tmux attach`` would only answer
    with its own "can't find session" — and the CLI ``exec``s this argv, so
    nothing here may hand over an attach that has nothing to attach to.

    The session is one the project's live agents' launch records name, the manager's
    first, by tmux's id on the server each record places it (:func:`resolve_place`): by
    ``asq-<codename>``, a name read from the store, a swap of two projects' codenames
    attached the human to the other project's agents, and typed their keys there (review
    r7 of #113). By the socket's name alone (``tmux -L``), a shell under another
    ``TMUX_TMPDIR`` asks another server (round 29 of #257). One the project's spawns made
    is asked however long ago its last agent ended (``launch_seal.project_sessions``): an
    ended manager's window lingers (``remain-on-exit``) and holds the session, which is
    what the 💤 row's attach stands for (follow-up of #257). A project whose agents were
    spawned before this version has no record to name its session: the refusal names the
    tmux command that attaches to it by hand; one no agent ever ran in (``[projects]
    capture`` off, no row) has no session to name (review of #259, round 17).
    """
    config = settings()
    srv = server(config)
    _require_tmux(srv)
    project = ensure_codename(project)
    with store_session() as store:
        codename = project.codename or _kept_codename(store, project)
        live = store.fleet_agents(project.id, live_only=True)
    placements = _resolve_all(_manager_first(live), config)
    for session in _project_sessions(project.id, placements, config):
        if session.present():
            return session.attach_command()
    spawn = project_service.fleet_command("spawn", project, "manager")
    if codename is None:
        raise FleetError(
            f"no fleet session for {_name(project)} — nothing to attach to; `{spawn}` starts one"
        )
    raise FleetError(
        f"no fleet session a launch record of {_name(project)} names is running — nothing to "
        f"attach to; `{spawn}` starts one. Agents spawned before this version run in a "
        f"session no record names: `tmux -L {config.tmux_socket} attach -t "
        f"={session_name(codename)}` attaches to it by hand"
    )
