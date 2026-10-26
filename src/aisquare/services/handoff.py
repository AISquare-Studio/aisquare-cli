"""A hand-off across agent types: the brief one agent's work carries to another agent's
fresh start, and when a hand-over crosses agents (task #15 of #113).

A conversation does not move between agents: a Codex thread is a rollout under its home
that no Claude Code session resumes, and a Claude Code transcript is nothing Codex reads.
So a hand-off onto another agent starts the target FRESH, and what carries the work is a
**brief**: the source's task, contract and claims, its last board entries, the state of its
tree, and the newest of its conversation. ``fleet switch --agent`` (or ``--to`` an account
of another agent), the Spawn dialog's *Hand off from* with another Agent, and the automatic
hand-over once the agent's own accounts are all limited (:func:`pick_across`) all start
one.

Agent-neutral by construction, so a new adapter gets it by implementing what the adapter
contract already asks (docs/coding-agents.md, "Adding a terminal agent"):

- the conversation is read through the SOURCE adapter's transcript reader, as Remote's
  Transcript tab pages it (``services.transcript.read_page``: ``AgentTranscript.records``
  and ``page_lines``), from the transcript its own hook reported (the launch record's,
  never the board session's column, which a sandbox could write);
- the brief reaches the TARGET as its first message, ``spawn``'s ``prompt``, the way every
  first message goes (``services.delivery``, #295): on its command line through the prompt
  file where ``capabilities.positional_prompt`` says it takes one, typed into its prompt
  otherwise, and acknowledged by its first turn either way.

The first prompt holds the brief cut to :data:`PROMPT_MAX` bytes; the whole brief, up to
:data:`FILE_MAX`, is written under the aisquare home (:func:`brief_dir`), which a sandboxed
agent may read and never write, and the prompt names it. The brief's last two lines, where to
start and where the whole brief is kept, are never what a cut takes (:meth:`HandoffBrief.text`).

The brief is the human's and the agent's own words on their way to ANOTHER agent's provider,
so it is redacted as text leaving the machine is (``core.redaction.redact`` at
``[redaction] level``) before it is written or sent, and the files are pruned after
:data:`KEEP_DAYS` (:func:`prune`).
"""

from __future__ import annotations

import hashlib
import re
import shutil
import stat
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aisquare.core import atomic, paths
from aisquare.core import claude_accounts as claude_accounts_core
from aisquare.core.agent_adapters import get_adapter, recorded_adapter
from aisquare.core.agent_adapters.transcripts import lines_backwards, lines_forward
from aisquare.core.config import load_config
from aisquare.core.launch_seal import LaunchRecord
from aisquare.core.redaction import MARKER, redact
from aisquare.core.spawn import git_env
from aisquare.core.workspace import git_argv
from aisquare.models import FleetAgent, RedactionLevel, TeamEvent, TeamTask
from aisquare.services import claude_accounts as claude_accounts_service

PROMPT_MAX = 6_000
"""Most UTF-8 bytes of the brief a first prompt carries: the replacement's first turn, which
the task, the claims, the board and the newest of the conversation fill, with the whole
brief one file read away. No tmux limit bounds it since #295: a first message travels in a
prompt file, never in the window's command. The conversation is cut to fit, the oldest of
it first."""

FILE_MAX = 48_000
"""Most UTF-8 bytes of the brief file (:func:`write`)."""

TURNS = 80
"""The newest records of the source's transcript a brief reads."""

SCAN_BUDGET = 2_000_000
"""Most bytes read back from the end of the source's transcript (and as many ahead, for a
record a later line completes), as a Transcript page reads: one pasted attachment can be
megabytes on one line."""

WIDTH = 110
"""The columns the conversation is wrapped to, as the Transcript tab wraps a page."""

DETAIL_MAX = 1_200
"""Most characters of the task's contract a brief quotes."""

ENTRY_MAX = 400
"""Most characters of one board entry, or one line of the conversation, a brief quotes."""

CHANGED_MAX = 30
"""Most of the tree's uncommitted paths a brief lists."""

CLAIMS_MAX = 8
"""Most of the source's claims a brief lists, each cut to :data:`CLAIM_MAX`; the rest are
counted (``+N more``) and are on the board. A manager can hold twenty."""

CLAIM_MAX = 120
"""Most characters of one claim's title a brief quotes."""

LABEL_MAX = 80
"""Most characters of a teammate's label a brief quotes (the service bounds none; the Spawn
dialog's own is 24): a longer one would push the last two lines out of the first prompt."""

COMMITS = 5
"""The newest commits of the tree a brief lists."""

DIR_NAME = "handoffs"

KEEP_DAYS = 14
"""How long a brief file is kept: :func:`write` removes the ones older than this, by the
time they were written. The prompt holds the brief's newest part and names the file for the
rest, which a replacement reads in its first turns; two weeks outlasts a long task, and no
conversation of the human's stays on the disk for longer than that for a hand-off."""

_MIDDLE_ROOM = 200
"""The bytes kept back for the conversation's own heading, or its pointer when none of it fits."""

_GIT_TIMEOUT = 10.0
_ESCAPES = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_SLUG = re.compile(r"[^A-Za-z0-9-]+")


def brief_dir() -> Path:
    """Where the briefs are kept: under the aisquare home, which a sandboxed agent's
    profile makes read-only (``types.store_read_only``), so no agent's tools wrote one."""
    return paths.aisquare_home() / DIR_NAME


@dataclass(frozen=True)
class GitState:
    """The source's tree as git reads it when the brief is made."""

    tree: Path
    branch: str | None = None
    changed: tuple[str, ...] = ()
    """``git status --porcelain`` lines: the uncommitted paths, each with its status."""
    commits: tuple[str, ...] = ()
    """``git log --oneline`` lines, newest first."""


@dataclass(frozen=True)
class HandoffBrief:
    """What one agent's work carries onto another agent's fresh start."""

    label: str | None
    """The agent that starts with it: the source's own label for a take-over, a fork's own
    when it asked for one (``None``: the spawn picks it)."""
    source: str
    """The teammate the work comes from."""
    source_agent: str
    target_agent: str
    reason: str | None = None
    task: TeamTask | None = None
    claims: tuple[TeamTask, ...] = ()
    """The tasks the source held, which the new agent's row takes over with it."""
    entries: tuple[TeamEvent, ...] = ()
    git: GitState | None = None
    conversation: tuple[str, ...] = ()
    """The newest of the source's conversation as plain lines, oldest first."""
    fork: bool = False
    """The source keeps running, and the new agent has its own tree and no claims."""
    path: Path | None = None
    """Where :func:`write` kept the whole brief; ``None`` before, or when it could not."""
    redaction: RedactionLevel = RedactionLevel.standard
    """How hard :meth:`text` scrubs what it quotes (``[redaction] level``, which
    :func:`build` reads). The default is the config's own, so a brief made by hand is scrubbed
    as the default would."""

    def text(self, limit: int = PROMPT_MAX) -> str:
        """The brief in at most ``limit`` UTF-8 bytes, credentials scrubbed out of what it
        quotes (``core.redaction.redact``).

        The head (who, the task and contract, the claims, the board entries, the tree) is
        each section bounded; the conversation fills what it leaves, newest kept. When the
        head alone is over, the cut takes the conversation first (to a pointer), then the
        board entries (the oldest first), the tree, and the claims, and says which; the
        task and its contract go last. The last two lines, where to start and where the
        whole brief is kept, are never what it takes: they are what lets the agent find the
        rest."""
        tail = self._tail()
        room = limit - _size("\n".join(tail)) - 1
        reserve = _MIDDLE_ROOM if self.conversation else 0
        core, claims, entries, tree = (_scrubbed(part, self.redaction) for part in self._head())
        head = _fit_head(core, claims, entries, tree, room - reserve)
        # All of the conversation is scrubbed, then windowed and clipped: a key or a PEM block
        # whose start the window cuts off, or whose line the clip cuts, is already gone.
        lines, cut = _newest_that_fit(
            _scrubbed(self.conversation, self.redaction),
            room - _size("\n".join(head)) - 1 - reserve,
        )
        middle: list[str] = []
        if lines:
            source = claude_accounts_core.agent_label(self.source_agent)
            cut_note = ", its start cut to fit" if cut else ""
            middle = [
                f"{_name(self.source)}'s conversation on {source}, newest last{cut_note}:",
                *lines,
            ]
        elif self.conversation:
            where = "in the brief file, too long for here" if self.path else "too long for here"
            middle = [f"({_name(self.source)}'s conversation is {where})"]
        return _cut_bytes("\n".join([*head, *middle, *tail]), limit)

    def _head(self) -> tuple[list[str], list[str], list[str], list[str]]:
        """The head's four sections, in the order they read: who and the task, the claims, the
        board entries (a heading, then the oldest first), and the tree."""
        source = claude_accounts_core.agent_label(self.source_agent)
        target = claude_accounts_core.agent_label(self.target_agent)
        # Each text is scrubbed whole BEFORE it is cut to its bound, never after: a clip that
        # takes the end of a key leaves its start, which no pattern of the scrub matches.
        raw = self._raw
        label, name = _name(self.label), _name(self.source)
        why = f" ({raw(self.reason)})" if self.reason else ""
        if self.fork:
            who = f"You are {label}, a fork" if label else "You are a fork"
            core = [
                f"{who} of {name}, which keeps running on "
                f"{source}. You run on {target}: a conversation does not move between agents, "
                "so you start fresh, in your own worktree on your own branch with no claims, "
                f"and this brief carries {name}'s work so far."
            ]
        else:
            # A take-over keeps its source's label: it is the previous session of the same agent.
            who = "the previous session of this agent" if self.label == self.source else name
            core = [
                f"You are {label}, taking over from {who}, which ran on {source}"
                f"{why}. You run on {target}: a conversation does not move between agents, so "
                "you start fresh, and this brief carries its work."
            ]
        if self.task is not None:
            whose = f"{name}'s task, not yours" if self.fork else "your task"
            title = _clip(_single_line(raw(self.task.title)), ENTRY_MAX)
            core.append(f"Task ({whose}): {title} ({self.task.id}).")
            if self.task.detail and self.task.detail.strip():
                core.append(f"Contract: {_clip(raw(self.task.detail).strip(), DETAIL_MAX)}")
        claims: list[str] = []
        if self.claims and not self.fork:
            claims.append("Claims, now yours:")
            claims.extend(
                f"- {_task_ref(task)} {_clip(_single_line(raw(task.title)), CLAIM_MAX)}"
                for task in self.claims[:CLAIMS_MAX]
            )
            if len(self.claims) > CLAIMS_MAX:
                claims.append(f"- (+{len(self.claims) - CLAIMS_MAX} more, on the board)")
        entries: list[str] = []
        if self.entries:
            entries.append(f"{name}'s last board entries:")
            entries.extend(
                f"- {event.kind}: {_clip(_single_line(raw(event.text)), ENTRY_MAX)}"
                for event in self.entries
            )
        tree = (
            _git_lines(self._scrubbed_git(self.git), name if self.fork else None)
            if self.git is not None
            else []
        )
        return core, claims, entries, tree

    def _raw(self, text: str) -> str:
        """``text`` scrubbed at this brief's level, whole."""
        return redact(text, self.redaction)

    def _scrubbed_git(self, state: GitState) -> GitState:
        """The tree's facts scrubbed: a path or a commit subject can hold a token."""
        return replace(
            state,
            branch=self._raw(state.branch) if state.branch else None,
            changed=tuple(self._raw(line) for line in state.changed),
            commits=tuple(self._raw(line) for line in state.commits),
        )

    def _tail(self) -> list[str]:
        task = f" and `aisquare task show {self.task.id}`" if self.task is not None else ""
        name = _name(self.source)
        if self.fork:
            lines = [
                f"Start by reading `aisquare board`{task}, then `git status` and `git log "
                "--oneline -5` in your own worktree, and say what you will take on; never touch "
                f"{name}'s tree or its task."
            ]
        else:
            lines = [
                f"Start by reading `aisquare board`{task}, then `git status` and `git log "
                f"--oneline -5` in this directory; continue from where {name} left off "
                "and do not redo work that is already committed."
            ]
        if self.path is not None:
            lines.append(f"The whole brief is kept at {self.path}.")
        return lines


def build(
    *,
    label: str | None,
    source: FleetAgent,
    record: LaunchRecord,
    target_agent: str,
    reason: str | None = None,
    task: TeamTask | None = None,
    recent: Sequence[TeamEvent] = (),
    claims: Iterable[TeamTask] = (),
    fork: bool = False,
) -> HandoffBrief:
    """The brief of ``source``'s work, as its launch ``record`` holds it, for ``label`` on
    ``target_agent``. Never raises: a transcript or a tree that cannot be read is a brief
    without that section."""
    level = _redaction_level()
    return HandoffBrief(
        label=label,
        source=source.label,
        source_agent=record.agent,
        target_agent=target_agent,
        reason=reason,
        task=task,
        claims=tuple(claims),
        entries=tuple(recent),
        git=git_state(Path(record.cwd)),
        conversation=conversation(record, level=level),
        fork=fork,
        redaction=level,
    )


def conversation(
    record: LaunchRecord, *, turns: int = TURNS, level: RedactionLevel = RedactionLevel.standard
) -> tuple[str, ...]:
    """The newest ``turns`` records of the session ``record`` holds, as its agent's own
    transcript reader renders them, as plain lines (no terminal escapes), oldest first:
    the human's words, the agent's, and each call as one line. ``()`` for none.

    Every string of a record is redacted at ``level`` BEFORE the reader wraps it
    (:func:`scrub_entry`): the reader breaks a line at 110 columns, and none of the
    redaction's patterns matches a key broken over two lines.

    The walk Remote's Transcript tab pages with (``AgentTranscript.records`` over the lines
    read back from the end, then ``page_lines``), done here rather than through
    ``services.transcript``: a hand-over's worker is on the hook path, which reaches no
    Remote code (tests/test_remote_names_stay_off_the_hook_graph.py)."""
    facts = record.session
    adapter = recorded_adapter(record.agent)
    reader = adapter.transcript_reader if adapter is not None else None
    if facts is None or not facts.transcript or reader is None:
        return ()
    path = Path(facts.transcript)
    try:
        if not path.is_absolute() or not stat.S_ISREG(path.stat().st_mode):
            return ()  # a regular file only: a pipe would hold the hand-over for a writer
        end = path.stat().st_size
        walked = lines_backwards(path, end, budget=SCAN_BUDGET)
        ahead = lines_forward(path, end, budget=SCAN_BUDGET)  # read only if the reader asks
        turns_seen: list[list[str]] = []
        for _offset, entry in reader.records(walked, ahead):
            rendered = [] if entry is None else reader.page_lines(scrub_entry(entry, level), WIDTH)
            if rendered:
                turns_seen.append(rendered)
                if len(turns_seen) >= turns:
                    break
    except Exception:  # the brief goes without it, never the hand-off
        return ()
    return tuple(_plain(line) for rendered in reversed(turns_seen) for line in rendered)


def scrub_entry(value: Any, level: RedactionLevel, depth: int = 0) -> Any:
    """``value``, a record read from a transcript (JSON: dicts, lists, strings), with every
    string in it redacted at ``level``. A record is scrubbed whole, so a PEM block or a key
    that the reader would wrap over lines is one match; a structure nested past 64 levels is
    no transcript's, and is replaced."""
    if level is RedactionLevel.off:
        return value
    if isinstance(value, str):
        return redact(value, level)
    if depth >= 64:
        return MARKER
    if isinstance(value, list):
        return [scrub_entry(item, level, depth + 1) for item in value]
    if isinstance(value, dict):
        return {key: scrub_entry(item, level, depth + 1) for key, item in value.items()}
    return value


def git_state(tree: Path) -> GitState | None:
    """``tree``'s branch, uncommitted paths and newest commits; ``None`` where git cannot
    say (no tree, no repository, no git)."""
    if not tree.is_dir():
        return None
    inside = _git(tree, "rev-parse", "--is-inside-work-tree")
    if inside is None or inside.strip() != "true":
        return None
    branch = (_git(tree, "branch", "--show-current") or "").strip() or None
    changed = tuple(
        line for line in (_git(tree, "status", "--porcelain") or "").splitlines() if line
    )
    log = _git(tree, "log", "--oneline", f"-{COMMITS}") or ""
    return GitState(tree=tree, branch=branch, changed=changed, commits=tuple(log.splitlines()))


def write(brief: HandoffBrief, *, now: datetime | None = None) -> HandoffBrief:
    """``brief`` written whole (up to :data:`FILE_MAX`) under :func:`brief_dir`, owner-only,
    with its :attr:`~HandoffBrief.path` set; as it was, with none, when it could not be.
    The briefs older than :data:`KEEP_DAYS` are removed first (:func:`prune`)."""
    moment = now or datetime.now(tz=UTC)
    named = brief.label or f"{brief.source}-fork"
    slug = _SLUG.sub("-", named).strip("-")[:40] or "agent"
    digest = hashlib.sha256(f"{named}\0{moment.isoformat()}".encode()).hexdigest()[:8]
    target = brief_dir() / f"{moment.strftime('%Y%m%dT%H%M%SZ')}-{slug}-{digest}.md"
    kept = replace(brief, path=target)
    try:
        paths.ensure_home()
        target.parent.mkdir(mode=0o700, exist_ok=True)
        atomic.write_replacing(target, kept.text(FILE_MAX) + "\n", owner_only=True)
    except OSError:
        return brief
    prune(now=moment, spare=target)
    return kept


def prune(*, now: datetime | None = None, spare: Path | None = None) -> tuple[Path, ...]:
    """Remove the brief files in :func:`brief_dir` last written more than :data:`KEEP_DAYS`
    ago, and say which: nothing else in that folder, no link, no folder, and not ``spare``,
    the one just written. Never raises: a file that will not go is left for the next write."""
    cutoff = ((now or datetime.now(tz=UTC)) - timedelta(days=KEEP_DAYS)).timestamp()
    removed: list[Path] = []
    try:
        found = sorted(brief_dir().glob("*.md"))
    except OSError:
        return ()
    for path in found:
        if path == spare:
            continue
        try:
            status = path.lstat()
            if stat.S_ISREG(status.st_mode) and status.st_mtime < cutoff:
                path.unlink()
                removed.append(path)
        except OSError:
            continue
    return tuple(removed)


def prompt(brief: HandoffBrief) -> str:
    """The first prompt of the agent that starts with ``brief``: the brief, cut to
    :data:`PROMPT_MAX`, naming the file that keeps it whole."""
    return brief.text(PROMPT_MAX)


# --- when a hand-over crosses agents ----------------------------------------------------------


def crossing_on() -> bool:
    """``[accounts] cross_agent_handover``: an untargeted hand-over whose agent's own accounts
    are all limited may start the agent fresh on another agent's account."""
    return claude_accounts_service.accounts_settings().cross_agent_handover


def other_agents(
    agent: str, *, which: Callable[[str], str | None] = shutil.which
) -> tuple[str, ...]:
    """Every agent with account slots but ``agent`` whose binary is installed here."""
    return tuple(
        name
        for name in claude_accounts_core.account_agents()
        if name != agent and which(get_adapter(name).binary)
    )


def own_accounts_full(agent: str, leaving: int | None, *, family: str | None = None) -> bool:
    """Whether every other enabled, signed-in account of ``agent`` is full (or there is
    none): what lets a hand-over cross to another agent. Full is a limit mark still in
    force for the session's model ``family`` (the refusal memory, #277), or a reading over
    ``switch_at`` (with that family's week). An account whose usage cannot be read is not
    full, so it keeps the hand-over on its agent (a fresh Codex slot with no rollout yet
    reads nothing). Read from the readings a hand-over's own pick just took (the
    minute-deep cache), so no account is asked twice, and a marked one is not asked."""
    switch_at = claude_accounts_service.accounts_settings().switch_at
    try:
        marks = claude_accounts_service.limit_marks()
        unmarked = [
            account
            for account in claude_accounts_service.list_accounts()
            if account.agent == agent
            and not account.disabled
            and account.slot != leaving
            and claude_accounts_core.signed_in(account)
            and not any(mark.blocks(family) for mark in marks.get(account.slot, []))
        ]
        readings = (
            claude_accounts_service.cached_usage(
                unmarked, max_age=claude_accounts_service.PICK_MAX_AGE_SECONDS
            )
            if unmarked
            else {}
        )
    except Exception:
        # Slots that cannot be listed, or a reading that raised: no evidence the agent is
        # full, so the hand-over stays on it and its own pick says what it could not read.
        return False
    for account in unmarked:
        reading = readings.get(account.slot)
        used = (
            claude_accounts_service.headroom_percent(reading, family)
            if reading is not None
            else None
        )
        if used is None or used < switch_at:
            return False
    return True


def somewhere_across(leaving: int | None, agent: str) -> bool:
    """Whether a hand-over of ``agent``'s session on ``leaving`` could cross to another agent:
    the switch is on, and another installed agent has an enabled, signed-in account. The
    hook's gate beside ``somewhere_to_hand_over``, so a limit on a machine with one agent's
    accounts starts no worker that can only refuse."""
    if not crossing_on():
        return False
    return any(
        claude_accounts_service.somewhere_to_hand_over(leaving, agent=other)
        for other in other_agents(agent)
    )


def pick_across(agent: str, leaving: int | None) -> claude_accounts_service.AgentPick:
    """The account of another agent a hand-over of ``agent``'s session crosses to, by the one
    priority order and headroom, read live: only one under ``switch_at`` (a fresh start that
    loses the conversation is never made for a least-bad account)."""
    others = other_agents(agent)
    if not others:
        return claude_accounts_service.AgentPick(
            None, None, ("across agents: no other agent with accounts is installed",)
        )
    return claude_accounts_service.choose_any(
        exclude=() if leaving is None else (leaving,), agents=others, live=True, least_bad=False
    )


def crosses(record: LaunchRecord, target_agent: str | None) -> bool:
    """Whether a replacement on ``target_agent`` runs another agent than ``record``'s."""
    if target_agent is None:
        return False
    adapter = recorded_adapter(record.agent)
    return (adapter.id if adapter is not None else record.agent) != target_agent


# --- helpers --------------------------------------------------------------------------------


def _git(tree: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(
            git_argv("-C", str(tree), *args),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
            check=False,
            env=git_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def _git_lines(state: GitState, whose: str | None) -> list[str]:
    tree = f"{whose}'s tree" if whose else "The tree"
    branch = f" on branch {state.branch}" if state.branch else ""
    lines = [f"{tree} ({state.tree}){branch}:"]
    if state.changed:
        shown = state.changed[:CHANGED_MAX]
        more = len(state.changed) - len(shown)
        lines.append("- uncommitted: " + ", ".join(line.strip() for line in shown))
        if more:
            lines[-1] += f" (+{more} more)"
    else:
        lines.append("- nothing uncommitted")
    if state.commits:
        lines.append("- last commits: " + "; ".join(state.commits))
    return lines


def _redaction_level() -> RedactionLevel:
    """``[redaction] level`` as the config holds it now; the default when it cannot be read."""
    try:
        return load_config().redaction.level
    except Exception:  # a broken config keeps the default scrub, never costs the hand-off
        return RedactionLevel.standard


def _scrubbed(lines: Sequence[str], level: RedactionLevel) -> list[str]:
    """``lines`` with credentials (at ``strict``, who and where) redacted. Scrubbed as one
    text, so a key that spans lines (a PEM block, wrapped by the transcript) is one match."""
    if not lines:
        return []
    return redact("\n".join(lines), level).split("\n")


def _fit_head(
    core: list[str], claims: list[str], entries: list[str], tree: list[str], room: int
) -> list[str]:
    """The head's sections in at most ``room`` bytes, with a line that says what was cut.

    They give way in this order: the board entries (``entries`` is a heading, then the oldest
    first: its oldest go one by one), the tree, the claims; what stays of ``core`` (who, the
    task and its contract) is cut last, from its end, whole lines, and only a first line that
    is itself over is cut mid-line. Each is on the board or in the tree for the agent to
    read, which the brief's last lines tell it to."""
    core, claims, entries, tree = list(core), list(claims), list(entries), list(tree)
    gone: list[str] = []

    def put() -> list[str]:
        note = f"(cut to fit: {', '.join(gone)}; the board and the tree have them)"
        return [*core, *claims, *entries, *tree, *([note] if gone else [])]

    def over() -> bool:
        return _size("\n".join(put())) > room

    while over() and entries:
        if not gone:
            gone.append("the board entries")
        entries.pop(1)  # the oldest: entries[0] is the heading
        if len(entries) == 1:
            entries.clear()  # a heading alone says nothing
    if over() and tree:
        gone.append("the tree")
        tree.clear()
    if over() and claims:
        gone.append("the claims")
        claims.clear()
    while over() and len(core) > 1:
        if gone[-1:] != ["the task"]:
            gone.append("the task")
        core.pop()
    return [_cut_bytes("\n".join(put()), room)] if over() else put()


def _newest_that_fit(lines: Sequence[str], room: int) -> tuple[list[str], bool]:
    """The newest of ``lines`` (each clipped) that fit in ``room`` bytes, oldest first, and
    whether any older one was left out."""
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        clipped = _clip(line, ENTRY_MAX)
        cost = _size(clipped) + 1
        if used + cost > room:
            return list(reversed(kept)), True
        kept.append(clipped)
        used += cost
    return list(reversed(kept)), False


def _plain(line: str) -> str:
    return _ESCAPES.sub("", line).rstrip()


def _name(label: str | None) -> str:
    """A teammate's label as a brief quotes it: on one line, at most :data:`LABEL_MAX`."""
    return _clip(_single_line(label or ""), LABEL_MAX)


def _single_line(text: str) -> str:
    return " ".join(text.split())


def _clip(text: str, most: int) -> str:
    return text if len(text) <= most else text[:most].rstrip() + " …"


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def _cut_bytes(text: str, most: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= most:
        return text
    if most <= len(" …".encode()):
        return raw[: max(0, most)].decode("utf-8", errors="ignore")  # no room for the mark
    return raw[: most - len(" …".encode())].decode("utf-8", errors="ignore").rstrip() + " …"


def _task_ref(task: TeamTask) -> str:
    return f"#{task.number}" if task.number is not None else task.id
