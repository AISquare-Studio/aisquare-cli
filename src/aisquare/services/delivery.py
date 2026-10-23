"""The one door every write into an agent goes through (P0-W1a, #122).

A prompt the fleet sends either reaches the agent and starts a turn, or the board says it
did not. Three ways in, chosen per write:

- **The command line** (``argv``), for a Claude Code agent's first message: a spawn's
  ``--prompt``, a ``--task`` kickoff, a restart's or a switch's resume line, a fresh
  hand-off. Claude Code runs ``claude -- "<prompt>"`` (and ``claude --resume <path> --
  "<prompt>"``) as its first turn, behind the folder-trust question too, on 2.1.309 and
  on 2.1.223 (P0-W0, ``docs/plans/handoff-p0-reliability.md`` "W0 results"). Nothing is
  typed, so there is no Enter to lose. The text travels in a file
  (:func:`write_prompt_file`), never in the window's command: the tmux client refuses a
  command over 16 KB, and the row's recorded launch must not hold it.
- **Typed** (:func:`deliver_typed`), for every other binary, for ``[fleet] first_prompt =
  "type"``, and for ``tell`` until W2: pasted once Claude Code's input box is drawn,
  checked in the box, sent with Enter as its own tmux call, acknowledged by the agent's
  ``UserPromptSubmit``, and pressed again only while the text provably still waits. W0
  measured what the old one-Enter rule lost: an Enter that reaches Claude Code before it
  draws its input box is dropped while the text is kept (10 of 10, both versions), and
  one Enter once the box is drawn sends it within 0.3 s.
- **The inbox socket** (``socket``): W2's, behind ``[fleet] inbox_delivery``; nothing here
  posts to it yet.

None of them types over a human (P0-W2, #276). Every typed write asks :func:`draft_guard`
first: nothing goes in while Claude Code's input box holds a draft (read by structure, its
own dim placeholder and suggestion left out), or while someone has typed into the pane in
``asq`` within ``[fleet] draft_quiet_seconds`` (the typing lease ``asq`` touches,
:func:`touch_typing_lease`). A refused nudge waits and is sent once the box is clear; a
refused tell is filed as a note, which the agent is handed at the end of its turn.

How each write into a pane ended is said in one vocabulary (:data:`Typed`,
:class:`PaneWrite`, :class:`TellResult`), which lived in ``services.fleet`` and is
re-exported there under the same names.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Literal

from aisquare.core import claude_screen as screen
from aisquare.core import harness, paths
from aisquare.core.store import ContextStore, store_session
from aisquare.core.tmux import (
    TmuxError,
    TmuxPartlyTyped,
    TmuxServer,
    TmuxTimedOut,
    TmuxUnavailable,
)
from aisquare.models import FleetAgent

FIRST_PROMPT_FLAG = "--first-prompt-file"
"""``aisquare launch``'s option naming the file a first message waits in."""

Way = Literal["argv", "typed"]
"""How a first message reaches an agent: on its command line, or typed into its pane."""

Delivery = Literal["argv", "typed", "socket"]
"""How a write reached an agent, as a receipt's ``prompt_delivery`` says it."""

RENDER_POLL = 0.1
RENDER_WAIT = 3.0
"""How often, and for how long, a typed delivery reads the input box for its text before
it presses Enter."""
ENTER_GAP = 0.1
"""The pause between the text in the box and its Enter, sent as a tmux call of its own."""
ACK_POLL = 0.25
"""How often an acknowledgement is asked for."""
RE_PRESS: tuple[float, ...] = (2.0, 3.0, 5.0, 5.0, 5.0, 10.0, 10.0, 10.0)
"""The waits before each Enter pressed again, at most eight: only while no turn has
started, the input box still holds the text, and no dialog is drawn."""
LAST_WAIT = 10.0
"""The wait for an acknowledgement after the last press: 60 s in all with :data:`RE_PRESS`."""

_PASTE_CHIP = re.compile(r"^\[Pasted text #\d+(?: \+\d+ lines?)?\]")
"""What Claude Code draws in the box for a long paste, in place of its text."""


# --- the first message on the command line ---------------------------------------------------


def prompts_dir() -> Path:
    """Where first messages wait for their launcher: ``$AISQUARE_HOME/run/prompts``."""
    return paths.aisquare_home() / "run" / "prompts"


def write_prompt_file(agent_id: str, text: str) -> Path:
    """``text`` in a file only this user can read, named for the agent: the launcher reads
    and deletes it (:func:`take_prompt_file`). Written before the window opens, so the
    launch finds it; the spawn removes it when no window opened (:func:`discard_prompt_file`)."""
    folder = prompts_dir()
    folder.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        folder.chmod(0o700)
    path = folder / f"{agent_id}.txt"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


_PROMPT_FILE = re.compile(r"^agt_[0-9a-z]+\.txt$")
"""The name :func:`write_prompt_file` gives a first message: the agent's id, ``.txt``."""


class NotAPromptFile(ValueError):
    """A path ``launch --first-prompt-file`` was handed that no spawn wrote."""


def take_prompt_file(path: Path) -> str | None:
    """The first message at ``path``, and the file gone: read once, by the launch it was
    written for. ``None`` when it is not there, which is every launch after the first, a
    restart replaying the recorded command included: a restart is given a line of its own,
    never the old prompt.

    Only a file :func:`write_prompt_file` wrote is taken: ``<agent id>.txt`` directly in
    :func:`prompts_dir`, its real path (a link is followed first) and a regular file.
    Anything else raises :class:`NotAPromptFile` and is neither read nor deleted. The option
    is hidden, not private: pointed at ``~/.ssh/config``, the launch read it into the agent's
    first message and deleted it (review of #330, round 1)."""
    try:
        real = path.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, RuntimeError) as exc:  # a link loop, a folder it may not read
        raise NotAPromptFile(f"{path} cannot be resolved ({exc})") from exc
    folder = prompts_dir().resolve()
    if real.parent != folder or not _PROMPT_FILE.match(real.name) or not real.is_file():
        raise NotAPromptFile(f"{path} is not a first-message file the fleet wrote")
    try:
        text = real.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    with contextlib.suppress(OSError):
        real.unlink()
    return text


def discard_prompt_file(path: Path) -> None:
    """Remove a first message no launch will read: its window never opened."""
    with contextlib.suppress(OSError):
        path.unlink()


def first_prompt_way(binary: str, *, claude_code: bool, setting: str) -> Way:
    """How ``binary``'s first message reaches it: on the command line for Claude Code (a
    row a hook or a resume shows to be Claude Code, or a Claude-shaped name,
    ``harness.is_default_agent``), typed for any other binary, and typed for every agent
    under ``[fleet] first_prompt = "type"``."""
    if setting == "type":
        return "typed"
    return "argv" if claude_code or harness.is_default_agent(binary) else "typed"


def argv_tail(text: str) -> list[str]:
    """``text`` as Claude Code's positional prompt: after ``--``, without which a prompt that
    starts with ``-`` is read as an option and Claude Code exits (P0-W0)."""
    return ["--", text]


# --- acknowledgement -----------------------------------------------------------------------


def acked_reader(store: ContextStore, agent_id: str) -> Callable[[], bool]:
    """Whether ``agent_id``'s first message has started a turn, asked of ``store`` each time:
    its row reads ``acked`` (``store.mark_prompted``). The caller holds the one connection
    for the whole wait, as :func:`await_ack` does."""

    def acked() -> bool:
        row = store.get_fleet_agent(agent_id)
        return row is not None and row.first_prompt == "acked"

    return acked


def await_ack(
    agent_id: str,
    seconds: float,
    *,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> bool:
    """Whether ``agent_id``'s first message starts a turn within ``seconds``: its
    ``UserPromptSubmit`` flips the row to ``acked`` (``store.mark_prompted``). One store
    connection for the whole wait, asked every :data:`ACK_POLL` (a store opened per look
    contends with the very write it waits for: ``cli.launch._await_fleet_row``). ``0``
    asks once."""
    deadline = monotonic() + seconds
    with store_session() as store:
        while True:
            row = store.get_fleet_agent(agent_id)
            if row is None or row.first_prompt is None:
                return False
            if row.first_prompt == "acked":
                return True
            if monotonic() >= deadline:
                return False
            sleep(ACK_POLL)


def overdue(agent: FleetAgent, now: datetime, seconds: float) -> bool:
    """Whether ``agent``'s first message is still ``pending`` past ``seconds``."""
    given = agent.first_prompt_at
    return (
        agent.first_prompt == "pending"
        and given is not None
        and (now - given).total_seconds() > seconds
    )


# --- typed, acknowledged -------------------------------------------------------------------


@dataclass(frozen=True)
class Delivered:
    """How a typed delivery ended (:func:`deliver_typed`)."""

    told: TellResult | PaneWrite
    """The writes, as :func:`type_then_enter` says them: a :class:`PaneWrite` when the paste
    did not go through, else a :class:`TellResult`."""
    acked: bool | None
    """``True``, a turn started on it; ``False``, none had when the delivery stopped;
    ``None``, nothing could say (a pane whose screen this reader does not know)."""
    presses: int
    """How many times Enter was pressed: 1, or more when the first one was swallowed."""
    refused: str | None = None
    """Why nothing was typed at all: a human's draft in the input box, or a human typing in
    the pane (:func:`draft_guard`). ``told`` then says it too, as ``no``."""


def holds(composer: str | None, text: str) -> bool:
    """Whether the input box (``composer``, :func:`screen.composer_text`) holds ``text`` as it
    was pasted and nothing else: all of it, a part of it a wrapped box shows, or Claude
    Code's chip for a long paste. A box a human cleared, or typed into, holds something
    else, and is never pressed again."""
    if not composer:
        return False
    if _PASTE_CHIP.match(composer):
        return not _PASTE_CHIP.sub("", composer, count=1).strip()
    shown, ours = _flat(composer), _flat(text)
    return shown == ours or (len(shown) >= min(len(ours), 40) and shown in ours)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _box(server: TmuxServer, pane_id: str) -> list[str] | None:
    """The pane's screen, escapes kept, or ``None`` when tmux would not show it."""
    try:
        return server.capture(pane_id).lines
    except TmuxError:
        return None


def deliver_typed(
    server: TmuxServer,
    pane_id: str,
    text: str,
    label: str,
    *,
    press: str,
    acked: Callable[[], bool] | None,
    reads_box: bool,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    done: str = "typed into its pane at its prompt",
    what: str = "the text",
    guard: float | None = None,
    re_press: tuple[float, ...] = RE_PRESS,
    last_wait: float = LAST_WAIT,
) -> Delivered:
    """Paste ``text`` into a ready pane, check it is in the input box, press Enter, and see a
    turn start; press Enter again only while it provably did not.

    ``guard``, when given, is ``[fleet] draft_quiet_seconds``, and :func:`draft_guard` is
    asked first: a human's draft in the box, or a human typing, and nothing is typed
    (``Delivered.refused``). A box that already holds exactly this text — a nudge whose
    Enter was dropped last time — is not pasted into again: it is sent. ``re_press`` and
    ``last_wait`` are the schedule; a caller that must not wait a minute (a nudge, inside
    another agent's board write) passes a shorter one.

    The caller has waited for the pane to be ready (``fleet._type_prompt``: Claude Code's
    input box drawn, no dialog). ``reads_box``: the pane is Claude Code's, whose box this
    module reads (:func:`screen.composer_text`). Then:

    1. Paste (bracketed, as every paste is).
    2. Render check: read the box every :data:`RENDER_POLL`, up to :data:`RENDER_WAIT`,
       until it holds the text (:func:`holds`).
    3. :data:`ENTER_GAP`, then Enter as its own tmux call.
    4. Acknowledge: ``acked()`` (the agent's ``UserPromptSubmit``), asked every
       :data:`ACK_POLL`. With no ``acked`` (an agent without hooks), the box no longer
       holding the text is the screen's word that it was sent.
    5. Press again, at most eight times after the waits in :data:`RE_PRESS`, **only while**
       no acknowledgement has come, the box still holds the text, and no dialog is drawn.
       Stop at once when the box is empty, holds anything else, or a dialog shows: a human
       is typing, or the Enter brought up a question an Enter would answer.

    This narrows ``type_then_enter``'s "never a second Enter" rule (review of #259, round
    10) rather than dropping it: a second Enter is pressed only when the first one provably
    did not submit, with the text still there and nothing else on the screen (W0: one
    Enter after the box submitted every stranded prompt). Without ``reads_box`` nothing
    proves that, so one Enter is all there is, as before.
    """
    if guard is not None:
        why = draft_guard(server, pane_id, ours=text, quiet=guard)
        if why is not None:
            return Delivered(TellResult("no", why), None, 0, refused=why)
    # No box drawn is a screen this reader does not know (a layout it does not parse, a pane
    # past its wait): nothing can prove the text still waits there, so one Enter, as before.
    box = _composer(server, pane_id) if reads_box else None
    reads_box = reads_box and box is not None
    waiting = guard is not None and reads_box and holds(box, text)
    pasted = (
        PaneWrite("yes", what) if waiting else pane_write(what, lambda: server.paste(pane_id, text))
    )
    if pasted.typed != "yes":
        return Delivered(pasted, None, 0)
    if reads_box:
        deadline = monotonic() + RENDER_WAIT
        while not holds(screen.composer_text(_box(server, pane_id) or []), text):
            if monotonic() >= deadline:
                break
            sleep(RENDER_POLL)
    sleep(ENTER_GAP)
    presses = 0
    stopped: str | None = None
    for wait in (*re_press, last_wait):
        entered = pane_write("Enter", lambda: server.send_keys(pane_id, "Enter"))
        presses += 1
        if entered.typed != "yes":
            refused = replace(
                entered, typed="unsent" if entered.typed == "no" else entered.typed, went=(what,)
            )
            return Delivered(pasted_unsent(refused, label, press=press), False, presses)
        if not reads_box:
            taken = acked() if acked is not None else None
            return Delivered(TellResult("yes", done), taken, presses)
        if _ack_within(wait, acked, sleep=sleep, monotonic=monotonic):
            return Delivered(TellResult("yes", done), True, presses)
        lines = _box(server, pane_id)
        if lines is None:
            stopped = "tmux would not show its screen"
            break
        dialog = screen.modal_showing(lines)
        if dialog is not None:
            stopped = f"{dialog} came up"
            break
        if not holds(screen.composer_text(lines), text):
            if acked is None:  # no hook to ask: the box let go of it
                return Delivered(TellResult("yes", done), None, presses)
            stopped = "its input box no longer holds it"
            break
    if acked is not None and acked():
        return Delivered(TellResult("yes", done), True, presses)
    if stopped is None:
        return Delivered(
            TellResult(
                "unsent",
                f"pasted it at its prompt and pressed Enter {presses} times, and it is still in "
                f"its input box — {press} to send it",
            ),
            False,
            presses,
        )
    return Delivered(
        TellResult("yes", f"{done}, but no turn has started on it yet ({stopped})"),
        False,
        presses,
    )


def _composer(server: TmuxServer, pane_id: str) -> str | None:
    lines = _box(server, pane_id)
    return screen.composer_text(lines) if lines is not None else None


# --- the draft guard (P0-W2, #276) ----------------------------------------------------------


def typing_lease(server: TmuxServer, pane_id: str) -> Path:
    """The file ``asq`` touches while someone types into ``pane_id`` on ``server``:
    ``$AISQUARE_HOME/run/typing/<socket>-<server>-<pane>``, its mtime the last key.

    ``<server>`` is a digest of the socket's real path (:meth:`TmuxServer.socket_path`), not
    its name alone: every fleet server is named ``asq`` and numbers its panes from ``%0``, so a
    key typed into ``%1`` on one held every write to ``%1`` on another ``asq`` at another path
    (review of #337). The same server asked by name or at its path is one lease."""
    try:
        where = str(server.socket_path().resolve())
    except TmuxUnavailable:
        where = server.socket  # no POSIX uid: no tmux server either
    digest = hashlib.sha256(where.encode()).hexdigest()[:12]
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{server.socket}-{digest}-{pane_id.lstrip('%')}")
    return paths.aisquare_home() / "run" / "typing" / name


def touch_typing_lease(server: TmuxServer, pane_id: str) -> None:
    """Say someone is typing into ``pane_id`` now (``asq``'s ``_forward``). Never raises: a home
    that cannot be written costs the lease, and the input box is still read."""
    path = typing_lease(server, pane_id)
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


def human_typing(
    server: TmuxServer, pane_id: str, *, quiet: float, now: float | None = None
) -> bool:
    """Whether someone typed into ``pane_id`` in ``asq`` within the last ``quiet`` seconds."""
    try:
        touched = typing_lease(server, pane_id).stat().st_mtime
    except OSError:
        return False
    return (time.time() if now is None else now) - touched < quiet


def draft_guard(
    server: TmuxServer, pane_id: str, *, ours: str | None = None, quiet: float
) -> str | None:
    """Why typing into ``pane_id`` now would land in a human's draft, or ``None`` to go ahead.

    Two signals, either one enough. Claude Code's input box holds text that is not ``ours``
    (:func:`screen.composer_text`: the dim placeholder and suggestion are not a draft); and
    someone typed into the pane in ``asq`` within ``quiet`` seconds (:func:`human_typing`),
    which also covers a draft the box reader cannot see — a long one wrapped past the box,
    a binary that is not Claude Code. A human at ``tmux attach`` leaves no lease; the box
    reader covers them. A screen tmux will not show is no draft.
    """
    if human_typing(server, pane_id, quiet=quiet):
        return "someone is typing in its pane"
    composer = _composer(server, pane_id)
    if composer and not (ours is not None and holds(composer, ours)):
        return "a draft is in its input box"
    return None


def prompted_since(store: ContextStore, session_id: str, since: datetime) -> Callable[[], bool]:
    """The acknowledgement of a tell or a nudge typed at ``since``: the session's
    ``UserPromptSubmit`` stamped ``prompted_at`` past it (``store.mark_prompted``). Asked of
    ``store``, the one connection the caller holds for the wait."""

    def acked() -> bool:
        session = store.get_session(session_id)
        return (
            session is not None
            and session.prompted_at is not None
            and (session.prompted_at >= since)
        )

    return acked


def _ack_within(
    seconds: float,
    acked: Callable[[], bool] | None,
    *,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> bool:
    """Whether ``acked()`` says yes within ``seconds``; with no ``acked``, wait them out."""
    deadline = monotonic() + seconds
    while True:
        if acked is not None and acked():
            return True
        if monotonic() >= deadline:
            return False
        sleep(ACK_POLL)


# --- how one write into a pane ended (moved from services.fleet) ----------------------------


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
    wrote = pane_writes(
        (what, lambda: server.paste(pane_id, text)),
        ("Enter", lambda: server.send_keys(pane_id, "Enter")),
    )
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
