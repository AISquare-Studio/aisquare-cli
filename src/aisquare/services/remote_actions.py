"""Agent actions from the phone: tell, stop, restart, switch (SPEC §3), and the request ledger.

The actions are ordinary write handlers (:data:`ACTION_ENDPOINTS`,
:func:`action_handlers`). The write dispatcher runs each one in a worker thread
behind ``allow_write``, the request ledger and the audit log, as it runs ``note``
or ``send-keys``. Acting on a live agent from a phone needs more than that, and
the rest is here:

* **A pin.** ``agent_id`` (required by stop, restart and switch, optional for a
  tell) must still be the newest row holding the label, and a card's
  ``needs_id`` must still be one of the agent's items. Both are checked before
  anything reaches the fleet. A screen that went stale therefore never stops,
  restarts or moves the replacement a manager started meanwhile, and never
  types into it either (409 ``stale``, with what is ``current``).
* **A confirmation.** Stop, restart and switch carry ``confirm=<label>``.
* **The dialog guard.** A stop types ``/exit`` and Enter, and restart and switch
  stop the agent the same way. With a dialog up, that Enter answers it: it can
  approve a Bash command or take a question's first option. So an open dialog
  refuses the action (409 ``dialog_open``), unless ``dismiss_dialog`` asks for one
  Escape (No) first. A ``prompt`` or ``interrupt`` tell does not type into a
  dialog either. ``auto`` is ``fleet tell`` unchanged, which types into any row
  that reads waiting, a stale dialog's included (SPEC §10 leaves that to main).
* **One action per agent at a time.** ``remote_server.remote_agent_lock`` is
  taken without waiting, and the needs card's quick answers take it too (409
  ``busy``).
* **The fleet's own refusals**, mapped as ``asq fleet`` maps them
  (:func:`fleet_refusal`). A 200 body is the CLI's ``--json`` payload plus
  ``project``; a tell's also says its ``mode``.
* **The trail.** The dispatcher audits what went through. A refusal that comes
  after something reached the agent, or may have, is audited too: one after an
  Escape went to its pane, and every fleet call that fails, which may have done
  part of its work first (a restart stops the agent before it starts the
  replacement). The line says what was sent and how it ended: ``refused=`` when
  the action stopped short of its own step, ``failed=`` when that step was
  tried. A refusal before any of that changed nothing, and only the ledger
  keeps it.

``agent/tell`` has three modes. ``auto`` is ``fleet tell``: it types into an agent
that reads as waiting, and files a board note for any other, which the agent reads
at its next prompt. ``prompt`` types now, into an agent idle at its input prompt.
After an Escape no Stop hook fires, so the row reads ``working`` for up to 30
minutes while the agent sits at its prompt, and ``auto`` would only file a note
the agent never wakes for. ``interrupt`` sends one Escape, then types once the
prompt is back.

:class:`ActionLedger` keeps, per device, how its recent write-gated requests
ended. A retried ``request_id`` gets the first answer instead of a second run: a
phone that slept through a 30 s restart retries it, and must not start a second
hand-over. ``GET api/actions/recent`` (:func:`action_routes`) and the stream's
``action`` frame show the ledger to the device that made the requests, and to
no other.

What the agent shows right now comes from needs-you (``needs_agent_now`` and its
predicates), always called through the ``remote_needs`` module, so a test can
stand in for it. ``remote_server`` imports this module inside functions only, so
neither is on the hook path (SPEC §0.2, §7.3). The fleet service is imported
where it is used.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple, TypedDict, TypeVar

from aisquare.services import remote_needs, remote_server
from aisquare.services.remote_server import RequestError

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import BaseRoute

    from aisquare.models import FleetAgent, ProjectInfo
    from aisquare.services.remote_needs import AgentNow
    from aisquare.services.remote_server import Device, RemoteKit, WriteHandler

log = logging.getLogger(__name__)

ACTION_ENDPOINTS: tuple[str, ...] = ("agent/tell", "agent/stop", "agent/restart", "agent/switch")
"""The action names ``POST api/{name}`` accepts beside ``remote_server.WRITE_ENDPOINTS``."""

TELL_TEXT_MAX = 8_000
"""The longest ``agent/tell``. It arrives as one bracketed paste, however many lines it has."""

TELL_MODES = ("auto", "prompt", "interrupt")
"""``agent/tell``'s ``mode``: ``fleet tell``, type at the idle prompt, or Escape and then type."""

ACTION_AUDIT_EXCERPT = 120
"""How many characters of a tell its audit line keeps (SPEC §2.7)."""

ACTION_NEEDS_ID_MAX = 64
"""The longest ``needs_id`` a body may carry (an id is ``ny_`` and 16 hex digits)."""

ACTION_FIELD_MAX = 200
"""The longest ``to`` (a slot, an alias or an email) and ``reason`` of ``agent/switch``."""

ACTION_POLL_SECONDS = 0.25
"""How often an action reads the agent again while it waits for its Escape to land."""

ACTION_LEDGER_SIZE = 50
"""Finished requests the ledger keeps per device; the oldest goes first."""

ACTION_LEDGER_TTL = timedelta(minutes=15)
"""How long a finished request can be replayed or shown. Far longer than a phone sleeps
through one restart, and short enough that a request id the page reuses much later
runs anew rather than getting a stale answer back."""


class LedgerEntry(TypedDict):
    """One finished request, as ``GET api/actions/recent`` and the ``action`` frame show it."""

    request_id: str
    endpoint: str
    status: int
    body: dict[str, object]
    at: str


class LedgerSeen(NamedTuple):
    """What the ledger knows of a request id it has (:meth:`ActionLedger.ledger_seen`)."""

    answer: tuple[int, dict[str, object]] | None
    """How the request ended, ``(status, body)``; ``None`` while it still runs."""


_Record = TypeVar("_Record")


def _ledger_now() -> datetime:
    return datetime.now(UTC)


def _ledger_drop_expired(
    book: dict[str, dict[str, tuple[_Record, datetime]]], now: datetime
) -> None:
    """Forget every record of ``book`` (device → request id → (record, when)) past the TTL."""
    for device_id in list(book):
        kept = {
            request_id: held
            for request_id, held in book[device_id].items()
            if now - held[1] < ACTION_LEDGER_TTL
        }
        if kept:
            book[device_id] = kept
        else:
            del book[device_id]


class ActionLedger:
    """Per device: how its recent write-gated requests ended, and which are still running.

    In memory only, one per app. A server that restarts forgets it, and a retry
    then runs again, as every retry did before there was a ledger. Per device it
    keeps at most :data:`ACTION_LEDGER_SIZE` finished requests younger than
    :data:`ACTION_LEDGER_TTL`, and the ids still running. A running id is
    forgotten after the TTL too, so a request whose ending was never recorded
    cannot answer ``in_progress`` for the life of the server. Every pass drops
    what expired for EVERY device: a phone that never comes back must not keep
    its last answers in memory until the server stops.

    The server calls it from the event loop (the dispatcher, ``kit_route``, each
    socket's tick) and tests call it from their own threads, so one lock guards
    each method.
    """

    def __init__(self, *, clock: Callable[[], datetime] = _ledger_now) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._finished: dict[str, dict[str, tuple[LedgerEntry, datetime]]] = {}
        """device id → request id → (its entry, when it ended); oldest first."""
        self._running: dict[str, dict[str, tuple[str, datetime]]] = {}
        """device id → request id → (its endpoint, when it began)."""

    def _ledger_forget_expired(self) -> datetime:
        now = self._clock()
        _ledger_drop_expired(self._finished, now)
        _ledger_drop_expired(self._running, now)
        return now

    def ledger_replay(
        self, device_id: str, request_id: str
    ) -> tuple[int, dict[str, object]] | None:
        """The stored ``(status, body)`` of a finished request; ``None``: not finished here."""
        with self._lock:
            self._ledger_forget_expired()
            held = self._finished.get(device_id, {}).get(request_id)
        return None if held is None else (held[0]["status"], held[0]["body"])

    def ledger_seen(self, device_id: str, request_id: str) -> LedgerSeen | None:
        """How this device's request with this id ended, or that it still runs; ``None`` when
        the ledger has no such request: never sent here, or forgotten.

        What the server asks before anything else of a write that carries an id, its
        write gate included: a retry is answered from here whatever the switch says now.
        """
        with self._lock:
            self._ledger_forget_expired()
            held = self._finished.get(device_id, {}).get(request_id)
            if held is not None:
                return LedgerSeen((held[0]["status"], held[0]["body"]))
            if request_id in self._running.get(device_id, {}):
                return LedgerSeen(None)
        return None

    def ledger_begin(self, device_id: str, request_id: str, endpoint: str) -> bool:
        """Mark a request as running; ``False`` while one with that id still is."""
        with self._lock:
            now = self._ledger_forget_expired()
            running = self._running.setdefault(device_id, {})
            if request_id in running:
                return False
            running[request_id] = (endpoint, now)
            return True

    def ledger_finish(
        self, device_id: str, request_id: str, status: int, body: dict[str, object]
    ) -> None:
        """Store how a request ended, refusals included, so a retry gets the same answer."""
        with self._lock:
            now = self._ledger_forget_expired()
            running = self._running.get(device_id, {})
            began = running.pop(request_id, None)
            if not running:
                self._running.pop(device_id, None)
            entry: LedgerEntry = {
                "request_id": request_id,
                "endpoint": "" if began is None else began[0],
                "status": status,
                "body": body,
                "at": now.isoformat(timespec="seconds"),
            }
            finished = self._finished.setdefault(device_id, {})
            finished.pop(request_id, None)  # a repeat ends up newest, not where it first was
            finished[request_id] = (entry, now)
            while len(finished) > ACTION_LEDGER_SIZE:
                del finished[next(iter(finished))]

    def ledger_recent(self, device_id: str) -> list[LedgerEntry]:
        """This device's finished requests, newest first."""
        with self._lock:
            self._ledger_forget_expired()
            held = self._finished.get(device_id, {})
            return [entry.copy() for entry, _ended in reversed(held.values())]


def new_action_ledger() -> ActionLedger:
    """The ledger a new app starts with."""
    return ActionLedger()


# --- the body ------------------------------------------------------------------------------


# An action reads its body as every write does, with the server's one set of readers: a
# copy of them here refused what the server's own read as absent (review of #243, round 3).
action_required = remote_server._required
action_ref = remote_server._optional_ref
action_flag = remote_server._remote_flag


def action_pinned(body: dict[str, Any]) -> tuple[str, str]:
    """``agent``, ``confirm`` and ``agent_id``, which stop, restart and switch all require.

    ``confirm`` must repeat the label exactly. The page sends it from its confirm
    sheet, so a stray tap, or a body built by hand, does not stop an agent.
    """
    label = action_required(body, "agent")
    if body.get("confirm") != label:
        raise RequestError(400, "confirm_required", f"confirm by sending confirm={label}")
    return label, action_required(body, "agent_id")


def action_tell_text(body: dict[str, Any]) -> str:
    """The tell's ``text``, kept literally: whitespace is content. Empty is a 400, longer
    than :data:`TELL_TEXT_MAX` a 413, and a control character other than tab, newline and
    carriage return a 400 (``remote_server.check_remote_text``). Typed text refuses the
    carriage return too, which is the Enter key there; inside the paste it is a line break.

    A tell goes into the pane as one bracketed paste, and tmux before 3.7 pastes the
    buffer's bytes as they are: an ``ESC [201~`` in the text ended the paste early, and
    what followed it arrived as keystrokes, a Ctrl-C past the double-press guard or the
    Ctrl-Z no key may send. tmux 3.7 escapes them, so the agent got a mangled message
    and the page no sign of it. Mode ``auto`` may file it as a board note instead, and
    it is refused all the same: one rule for every tell.
    """
    text = body.get("text")
    if not isinstance(text, str) or not text:
        raise RequestError(400, "invalid", "'text' is required: what to tell the agent")
    if len(text) > TELL_TEXT_MAX:
        raise RequestError(413, "too_large", f"'text' is over {TELL_TEXT_MAX} characters")
    remote_server.check_remote_text(text, pasted=True)
    return text


def action_tell_mode(body: dict[str, Any]) -> str:
    """The tell's ``mode``, ``auto`` when there is none (:data:`TELL_MODES`)."""
    mode = body.get("mode")
    if mode is None:
        return "auto"
    if not isinstance(mode, str) or mode not in TELL_MODES:
        raise RequestError(400, "invalid", "'mode' is auto, prompt or interrupt")
    return mode


def action_audit_excerpt(text: str) -> str:
    """How a tell began, for its audit line (SPEC §2.7).

    A tell is the one write that delivers free-form instructions, and one typed
    into a pane leaves no board event, so the trail keeps its start. A longer
    text is cut to :data:`ACTION_AUDIT_EXCERPT` characters, the last of them
    ``…``. Anything that would not print becomes ``?``: a newline would begin a
    forged line of its own. That is ``remote_server._audit_clean``, the trail's one
    scrub: a copy of it here would miss the next character class it learns.
    """
    return remote_server._audit_clean(text, ACTION_AUDIT_EXCERPT)


def action_yes_no(flag: bool) -> str:
    return "yes" if flag else "no"


def action_tell_summary(label: str, target: ProjectInfo, mode: str, text: str, how: str) -> str:
    """A tell's audit line: ``how`` it ended (``delivered=…`` and what stopped it), then the
    text's length and how it began, last, since an excerpt may hold anything printable."""
    excerpt = action_audit_excerpt(text)
    return f'tell {label}@{target.id} mode={mode} {how} text={len(text)}ch "{excerpt}"'


@contextlib.contextmanager
def action_audited(summary: Callable[[str], str]) -> Iterator[None]:
    """Audit a refusal raised inside as ``summary(<its error code>)``.

    For the steps after something reached the agent, or may have: an Escape
    went to its pane, or the fleet was asked to act and may have done part of it
    before it failed (``fleet stop`` types ``/exit`` before it can fail to kill
    the window). The dispatcher audits only what went through, and the trail is
    there for what a device did to a live agent, finished or not (review of
    #243, round 1, F10). Any other exception here is a 400 ``write_failed``,
    logged as the dispatcher logs one, and audited the same way.
    """
    try:
        yield
    except RequestError as exc:
        exc.audit = summary(exc.error)
        raise
    except Exception as exc:
        log.warning("remote: an action failed after it reached the agent: %s", exc)
        raise RequestError(400, "write_failed", str(exc), audit=summary("write_failed")) from exc


# --- the fleet's refusals ------------------------------------------------------------------

_Result = TypeVar("_Result")


def fleet_refusal(exc: Exception) -> RequestError:
    """The refusal ``asq fleet`` gives for ``exc`` (``cli/fleet.py:_fail_fleet``), as a write's.

    Most specific first, as there: the fleet's own errors are all ``FleetError``,
    so the order is the mapping. The message is the service's own sentence, such
    as its refusal of a row that a hand-over is already moving.
    ``TeamDisabledError`` comes from the board, and ``ValueError`` from an
    argument the service would not take.
    """
    from aisquare.services import fleet as fleet_service
    from aisquare.services import team as team_service

    message = str(exc)
    if isinstance(exc, fleet_service.FleetUnavailable):
        return RequestError(503, "fleet_unavailable", message)
    if isinstance(exc, fleet_service.NoSuchProject | remote_server.NoSuchProject):
        return RequestError(404, "not_found", message)
    if isinstance(exc, fleet_service.NoSuchAgent | remote_server.NoSuchAgent):
        return RequestError(404, "no_such_agent", message)
    if isinstance(exc, team_service.TeamDisabledError):
        return RequestError(409, "team_disabled", message)
    if isinstance(exc, ValueError):
        return RequestError(400, "invalid", message)
    return RequestError(409, "fleet_error", message)


def action_fleet_call(call: Callable[[], _Result]) -> _Result:
    """``call()``, with a refusal from the fleet (or needs-you) answered as
    :func:`fleet_refusal` maps it."""
    from aisquare.services import fleet as fleet_service
    from aisquare.services import team as team_service

    try:
        return call()
    except (
        fleet_service.FleetError,
        team_service.TeamDisabledError,
        remote_server.NoSuchAgent,
        remote_server.NoSuchProject,
        ValueError,
    ) as exc:
        raise fleet_refusal(exc) from exc


# --- which agent: the project, the pin, the lock ---------------------------------------------


def action_project(body: dict[str, Any]) -> ProjectInfo:
    """The body's ``project`` (an id prefix, a name or a codename), or the current project."""
    from aisquare.services import fleet as fleet_service

    ref = action_ref(body, "project")
    return action_fleet_call(lambda: fleet_service.resolve_project(ref))


def action_newest_row(target: ProjectInfo, label: str) -> FleetAgent | None:
    """The newest row holding ``label``, live or ended: the one a pinned ``agent_id`` names."""
    from aisquare.core.store import store_session

    with store_session() as store:
        return store.fleet_agent_by_label(target.id, label, live_only=False)


def action_stale(
    target: ProjectInfo, label: str, current: FleetAgent | None, *, escaped: bool = False
) -> RequestError:
    """409 ``stale``: the label is not the agent the phone saw, and ``current`` says who is.

    ``escaped``: an Escape already went to the pinned agent's pane, and the
    sentence must not say that nothing was done.
    """
    if current is None:
        said = f"there is no agent {label!r} in {target.root.name or target.id} now"
    else:
        said = f"{label!r} is another agent now ({current.id})"
    done = "Escape was sent, nothing else was done" if escaped else "nothing was done"
    return RequestError(
        409,
        "stale",
        f"{said} — {done}",
        current={"agent_id": None if current is None else current.id},
    )


def action_gone(target: ProjectInfo, label: str, agent_id: str | None) -> RequestError:
    """The refusal for a label no row holds: 409 ``stale`` when pinned, else 404."""
    if agent_id is not None:
        return action_stale(target, label, None)
    return RequestError(
        404,
        "no_such_agent",
        f"no agent {label!r} in {target.root.name or target.id} — "
        "`aisquare fleet ls --all` shows every row",
    )


@contextlib.contextmanager
def action_locked(target: ProjectInfo, label: str, agent_id: str | None) -> Iterator[FleetAgent]:
    """Hold the agent's one lock (409 ``busy`` while another action has it), and pin it.

    The label must name a row before a lock is made for it. The lock registry is
    process-wide and never shrinks, and a label is whatever a body says.

    The pin is checked under the lock, against the row read there. A second
    request that passed an earlier read while the first one replaced the row must
    not act on the replacement. ``fleet.switch`` takes no ``agent_id``, so for a
    switch this check is the only pin there is.
    """
    if action_newest_row(target, label) is None:
        raise action_gone(target, label, agent_id)
    lock = remote_server.remote_agent_lock(target.id, label)
    if not lock.acquire(blocking=False):
        raise RequestError(409, "busy", f"another action on {label} is still running")
    try:
        row = action_newest_row(target, label)
        if row is None:
            raise action_gone(target, label, agent_id)
        if agent_id is not None and row.id != agent_id:
            raise action_stale(target, label, row)
        yield row
    finally:
        lock.release()


# --- what the agent shows now ------------------------------------------------------------------


def action_snapshot(
    target: ProjectInfo, label: str, pin: str, *, escaped: bool = False
) -> AgentNow:
    """The agent as needs-you reads it now, which must still be the row ``pin`` names.

    The pin is the row read under the lock: the body's ``agent_id`` when it had
    one, else whoever held the label then. Every snapshot is checked, not only
    the first. The reads after an Escape go on for seconds, and the pane the
    paste goes to must belong to the agent the Escape went to.
    """
    snap = action_fleet_call(lambda: remote_needs.needs_agent_now(target, label))
    if snap.status is not None and snap.status.agent.id != pin:
        raise action_stale(target, label, snap.status.agent, escaped=escaped)
    return snap


def action_check_needs(
    target: ProjectInfo, label: str, pin: str, needs_id: str | None
) -> AgentNow | None:
    """With a card's ``needs_id``: the agent now, but only while that item is still its own.

    Otherwise the answer is 409 ``stale``, with the agent's current items as
    ``current``. This keeps a card's Switch from moving a replacement that a
    manager already started, since the card's item went with the agent it was
    about. It also keeps a second tap on a card's Restart from starting a third
    agent.
    """
    if needs_id is None:
        return None
    snap = action_snapshot(target, label, pin)
    if not remote_needs.needs_item_current(snap, needs_id):
        raise RequestError(
            409,
            "stale",
            f"{label} no longer shows what that card was about — nothing was done",
            current=[item.needs_item_json() for item in snap.items],
        )
    return snap


def action_state(snap: AgentNow) -> str:
    """The agent's derived state for a refusal to name; ``ended`` once its row has."""
    return "ended" if snap.status is None else snap.status.state


def action_pane_agent(snap: AgentNow, label: str) -> FleetAgent:
    """The row whose pane gets the keys; 409 ``not_agent`` when no pane runs the agent."""
    if snap.status is None or not snap.pane_is_agent:
        raise RequestError(
            409,
            "not_agent",
            f"{label}'s pane is not running the agent (it reads {action_state(snap)}) "
            "— nothing was sent",
        )
    return snap.status.agent


def action_press_escape(snap: AgentNow, label: str) -> None:
    """ONE Escape into the agent's pane. Never two: a second one on an idle prompt opens
    Claude Code's Rewind selector."""
    from aisquare.core.tmux import TmuxError
    from aisquare.services import fleet as fleet_service

    agent = action_pane_agent(snap, label)
    try:
        fleet_service.server_for(agent.tmux_socket).send_keys(agent.pane_id, "Escape")
    except TmuxError as exc:
        raise RequestError(
            409,
            "fleet_error",
            f"tmux could not send Escape to {label}'s pane ({exc}) — nothing was done",
        ) from exc


def action_settle(
    target: ProjectInfo,
    label: str,
    pin: str,
    reached: Callable[[AgentNow], bool],
    seconds: float,
) -> AgentNow | None:
    """Read the agent every :data:`ACTION_POLL_SECONDS`, for up to ``seconds``, until it has
    ``reached`` what an Escape was sent for. Returns that snapshot, or ``None``.

    The first read comes one poll after the Escape, never at once. Until Claude
    Code redraws, the pane still shows what it showed before.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(ACTION_POLL_SECONDS)
        snap = action_snapshot(target, label, pin, escaped=True)
        if reached(snap):
            return snap
    return None


def action_dialog_guard(
    target: ProjectInfo,
    label: str,
    pin: str,
    snap: AgentNow | None,
    *,
    dismiss: bool,
    doing: str,
    audit_start: str,
) -> bool:
    """Refuse to stop an agent that shows a dialog, or with ``dismiss``, press Escape first.

    ``doing`` names the action for the refusal ("stopping"). Returns whether a
    dialog was dismissed, which the audit line records. A false alarm costs a
    refusal with a sentence, or with ``dismiss`` an Escape to an agent about to be
    stopped anyway. It never costs an Enter into a dialog.

    A pending tool counts as a dialog here (:func:`action_may_answer`): in a
    permission prompt's first seconds nothing else tells it from a tool at work,
    and the ``/exit`` and Enter of a stop would answer it "1. Yes". For a tool
    that is running, the Escape stops it, which a stop was about to do anyway.

    The Escape answers a permission prompt "No", so a refusal after it is audited,
    as ``audit_start`` and then ``dismissed=yes refused=<error>``.
    """
    snap = snap if snap is not None else action_snapshot(target, label, pin)
    if not action_may_answer(snap):
        return False
    dialog = remote_needs.needs_dialog_open(snap)
    if not dismiss:
        if dialog:
            message = (
                f"{label} is showing a prompt; {doing} would answer it — "
                "send dismiss_dialog: true to press Esc (No) first"
            )
        else:
            message = (
                f"{label} has a tool pending, and a prompt for it may have just opened; "
                f"{doing} could answer it — send dismiss_dialog: true to press Esc (No) "
                "first, which also stops a running tool"
            )
        raise RequestError(409, "dialog_open", message)
    action_press_escape(snap, label)
    with action_audited(lambda error: f"{audit_start} dismissed=yes refused={error}"):
        closed = action_settle(
            target,
            label,
            pin,
            lambda again: not action_may_answer(again),
            remote_needs.DIALOG_SETTLE_SECONDS,
        )
        if closed is None:
            still = "still shows a prompt" if dialog else "still has its tool pending"
            raise RequestError(
                409,
                "dialog_open",
                f"Escape was sent, but {label} {still} — nothing else was done",
            )
    return True


def action_may_answer(snap: AgentNow) -> bool:
    """Whether an Enter typed into the agent's pane now may answer a dialog.

    One needs-you sees (:func:`remote_needs.needs_dialog_open`), or any tool use
    still waiting for its result (:func:`remote_needs.needs_tool_pending`), which
    is what a permission prompt is until its pane has been quiet for 5 s and its
    notification has come at 6 s.
    """
    return remote_needs.needs_dialog_open(snap) or remote_needs.needs_tool_pending(snap)


# --- typing into the agent's prompt ------------------------------------------------------------


def action_interrupt_wait() -> float:
    """How long ``interrupt`` waits, after its Escape, for the input prompt to come back.

    ``needs_at_input_prompt`` holds only for a QUIET pane, one with no output for
    ``fleet.ACTIVITY_WINDOW``. The interrupt's own redraw is output: the spinner
    stops, and Claude Code prints that it was interrupted. So the prompt cannot
    read as reached until that long after the Escape, and the settle time
    (``DIALOG_SETTLE_SECONDS``) counts from then. A wait of the settle time
    alone would always have ended in ``still_busy``.
    """
    from aisquare.services import fleet as fleet_service

    return fleet_service.ACTIVITY_WINDOW.total_seconds() + remote_needs.DIALOG_SETTLE_SECONDS


def action_busy_sentence(snap: AgentNow, label: str) -> str:
    """Why ``prompt`` will not type: what the agent is doing, and what to do instead.

    A working agent can be interrupted. One parked on its usage limit cannot be
    told anything: a message fails on the same limit until the reset, and a
    switch to another account is what moves it. Any other row (waiting, or
    attention that an Escape already answered, with a pane still redrawing) is
    a moment early.
    """
    state = action_state(snap)
    if state == "working":
        return f"{label} is working — use Interrupt & tell"
    if state == "limited":
        return (
            f"{label} hit its usage limit, and a message will not get past it — use Switch account"
        )
    return (
        f"{label} is not idle at its prompt yet — try again in a few seconds, "
        "or use Interrupt & tell"
    )


def action_paste(snap: AgentNow, label: str, text: str, *, interrupted: bool) -> tuple[bool, str]:
    """Type ``text`` into the agent's pane the way ``fleet tell`` does: one bracketed paste,
    then Enter. Returns ``(delivered, how)``.

    A paste, not keystrokes: Claude Code takes every line of it as one message,
    where each typed newline would submit one. The paste is a single tmux call,
    so a paste that fails typed nothing, and is a 409. An Enter that fails leaves
    the text unsent at the prompt. That is a 200 with ``delivered: false`` that
    says so, because text reached a live agent: the audit line records it, and the
    page keeps the card.
    """
    from aisquare.core.tmux import TmuxError
    from aisquare.services import fleet as fleet_service

    agent = action_pane_agent(snap, label)
    server = fleet_service.server_for(agent.tmux_socket)
    first = "interrupted it with Escape, then " if interrupted else ""
    try:
        server.paste(agent.pane_id, text)
    except TmuxError as exc:
        sent = "Escape was sent, but " if interrupted else ""
        raise RequestError(
            409,
            "fleet_error",
            f"{sent}tmux could not type into {label}'s pane ({exc}) — nothing was typed",
        ) from exc
    try:
        server.send_keys(agent.pane_id, "Enter")
    except TmuxError as exc:
        return False, (
            f"{first}pasted it at its prompt, but tmux could not press Enter ({exc}) — "
            "press Enter on the pad to send it"
        )
    return True, f"{first}typed into its pane at its prompt"


def action_type_now(
    target: ProjectInfo,
    label: str,
    text: str,
    snap: AgentNow | None,
    *,
    pin: str,
    interrupt: bool,
    trail: Callable[[str], str],
) -> tuple[bool, str]:
    """``prompt`` and ``interrupt``: type ``text`` at the agent's input prompt, now.

    Both refuse an open dialog, which the Enter would answer, and a pane that is
    not running the agent. ``prompt`` types only at an idle prompt, and otherwise
    says what the agent is doing. ``interrupt`` sends one Escape and waits for the
    prompt to come back. If it does not, nothing is typed. By then the Escape has
    cut the agent's turn short, so a refusal after it is audited: ``trail`` words
    the tell's line from how it ended.
    """
    snap = snap if snap is not None else action_snapshot(target, label, pin)
    if remote_needs.needs_dialog_open(snap):
        raise RequestError(
            409,
            "dialog_open",
            f"{label} is showing a prompt; typing now would answer it — "
            "answer it or dismiss it first",
        )
    action_pane_agent(snap, label)  # refused here, before the interrupt's Escape
    if not interrupt:
        if not remote_needs.needs_at_input_prompt(snap):
            raise RequestError(409, "agent_busy", action_busy_sentence(snap, label))
        return action_paste(snap, label, text, interrupted=False)
    action_press_escape(snap, label)
    with action_audited(lambda error: trail(f"delivered=no escape=sent refused={error}")):
        reached = action_settle(
            target, label, pin, remote_needs.needs_at_input_prompt, action_interrupt_wait()
        )
        if reached is None:
            raise RequestError(
                409,
                "still_busy",
                f"Escape was sent; {label} has not stopped yet — nothing was typed",
            )
        snap = reached
    with action_audited(lambda error: trail(f"delivered=no escape=sent failed={error}")):
        return action_paste(snap, label, text, interrupted=True)


# --- the actions ---------------------------------------------------------------------------


def action_tell(body: dict[str, Any]) -> tuple[dict[str, object], str]:
    """``POST api/agent/tell``: say ``text`` to one agent, in ``auto``, ``prompt`` or
    ``interrupt`` mode.

    ``auto`` is ``fleet tell``, unchanged. The other two modes type now
    (:func:`action_type_now`). ``agent_id`` and a card's ``needs_id`` are
    optional here: with either one, the tell reaches only the agent it names.
    Without one it still reaches only the row that held the label when the lock
    was taken, so an interrupt cannot send its Escape to one agent and its text
    to the replacement. The audit line keeps how the text began, since a tell
    typed into a pane leaves no trace on the board.
    """
    from aisquare.services import fleet as fleet_service

    label = action_required(body, "agent")
    text = action_tell_text(body)
    mode = action_tell_mode(body)
    agent_id = action_ref(body, "agent_id", guard=True)
    needs_id = action_ref(body, "needs_id", limit=ACTION_NEEDS_ID_MAX, guard=True)
    target = action_project(body)
    trail = functools.partial(action_tell_summary, label, target, mode, text)
    with action_locked(target, label, agent_id) as row:
        snap = action_check_needs(target, label, row.id, needs_id)
        if mode == "auto":
            # fleet tell pastes before it presses Enter, and files a note when that
            # fails, so it can fail with the text already in the pane.
            with action_audited(lambda error: trail(f"delivered=no failed={error}")):
                told = action_fleet_call(
                    lambda: fleet_service.tell(target, label, text, sender=None)
                )
            delivered, how = told.delivered, told.how
        else:
            delivered, how = action_type_now(
                target,
                label,
                text,
                snap,
                pin=row.id,
                interrupt=mode == "interrupt",
                trail=trail,
            )
    result: dict[str, object] = {
        "label": label,
        "delivered": delivered,
        "how": how,
        "mode": mode,
        "project": target.id,
    }
    return result, trail(f"delivered={action_yes_no(delivered)}")


def action_stop(body: dict[str, Any]) -> tuple[dict[str, object], str]:
    """``POST api/agent/stop``: ``fleet stop`` of the pinned row, ``/exit`` first unless ``force``.

    Behind the dialog guard, except with ``force``, which kills the window
    without typing anything, and for a row that has already ended, which shows no
    dialog (the stop removes the window it left). The grace is the service's
    own 5 s. A stop whose claims could not be released still answers 200 with
    ``release_failed``: the agent is stopped, and the page shows the warning.
    """
    from aisquare.services import fleet as fleet_service

    label, agent_id = action_pinned(body)
    force = action_flag(body, "force")
    dismiss = action_flag(body, "dismiss_dialog")
    needs_id = action_ref(body, "needs_id", limit=ACTION_NEEDS_ID_MAX, guard=True)
    target = action_project(body)
    audit_start = f"stop {label}@{target.id} agent={agent_id} force={action_yes_no(force)}"
    with action_locked(target, label, agent_id) as row:
        snap = action_check_needs(target, label, row.id, needs_id)
        guarded = not force and row.ended_at is None
        dismissed = guarded and action_dialog_guard(
            target,
            label,
            row.id,
            snap,
            dismiss=dismiss,
            doing="stopping",
            audit_start=audit_start,
        )
        # The /exit may already be typed when the stop fails (a kill tmux refused).
        with action_audited(
            lambda error: f"{audit_start} dismissed={action_yes_no(dismissed)} failed={error}"
        ):
            receipt = action_fleet_call(
                lambda: fleet_service.stop(target, label, force=force, agent_id=agent_id)
            )
    released = [task.id for task in receipt.released]
    result: dict[str, object] = {
        "agent": receipt.agent.model_dump(mode="json"),
        "claims_released": released,
        "release_failed": receipt.release_failed,
        "project": target.id,
    }
    summary = f"{audit_start} dismissed={action_yes_no(dismissed)} released={len(released)}"
    return result, summary


def action_restart(body: dict[str, Any]) -> tuple[dict[str, object], str]:
    """``POST api/agent/restart``: ``fleet restart`` of the pinned row, asked by the human.

    It works on an exited, a lost or a running agent. A running one is handed
    over, so its claims wait for the replacement. While the row has not ended,
    the action is behind the dialog guard, because a running agent is first
    stopped with ``/exit``. The service gets ``agent_id`` as well, and refuses a
    row that was replaced after the pin was checked.
    """
    from aisquare.services import fleet as fleet_service

    label, agent_id = action_pinned(body)
    fresh = action_flag(body, "fresh")
    dismiss = action_flag(body, "dismiss_dialog")
    needs_id = action_ref(body, "needs_id", limit=ACTION_NEEDS_ID_MAX, guard=True)
    target = action_project(body)
    # A refused restart's line names the row it acted on: it has no started= to go by.
    audit_start = f"restart {label}@{target.id} agent={agent_id} fresh={action_yes_no(fresh)}"
    with action_locked(target, label, agent_id) as row:
        snap = action_check_needs(target, label, row.id, needs_id)
        dismissed = row.ended_at is None and action_dialog_guard(
            target,
            label,
            row.id,
            snap,
            dismiss=dismiss,
            doing="restarting",
            audit_start=audit_start,
        )
        # A running agent is stopped before its replacement starts, and a start that
        # fails then leaves it stopped.
        with action_audited(
            lambda error: f"{audit_start} dismissed={action_yes_no(dismissed)} failed={error}"
        ):
            receipt = action_fleet_call(
                lambda: fleet_service.restart(
                    target, label, fresh=fresh, spawned_by="user", agent_id=agent_id
                )
            )
    result: dict[str, object] = {
        "replaced": receipt.replaced.model_dump(mode="json"),
        "started": receipt.started.model_dump(mode="json"),
        "resumed": receipt.resumed,
        "prompt_typed": receipt.prompt_typed,
        "was_running": receipt.was_running,
        "tmux_session": receipt.tmux_session,
        "notes": list(receipt.notes),
        "project": target.id,
    }
    summary = (
        f"restart {label}@{target.id} fresh={action_yes_no(fresh)} "
        f"dismissed={action_yes_no(dismissed)} resumed={action_yes_no(receipt.resumed)} "
        f"started={receipt.started.id}"
    )
    return result, summary


def action_switch(body: dict[str, Any]) -> tuple[dict[str, object], str]:
    """``POST api/agent/switch``: move the pinned agent to another Claude account to un-park it.

    A usage limit parks an agent as ``limited`` until the reset. Short of waiting,
    the one way out is a hand-over to another account: the ``aisquare fleet switch
    <label>`` the manager is told to run. Headroom picks the account unless
    ``to`` names one. ``fleet.switch`` takes no ``agent_id``, so the pin checked
    under the lock is all that keeps a phone from moving a replacement that the
    automatic hand-over or the manager already started. There is no ``force``
    (SPEC §9.3).
    """
    from aisquare.services import fleet as fleet_service

    label, agent_id = action_pinned(body)
    to = action_ref(body, "to", limit=ACTION_FIELD_MAX)
    fresh = action_flag(body, "fresh")
    reason = action_ref(body, "reason", limit=ACTION_FIELD_MAX)
    dismiss = action_flag(body, "dismiss_dialog")
    needs_id = action_ref(body, "needs_id", limit=ACTION_NEEDS_ID_MAX, guard=True)
    target = action_project(body)
    # As a restart's: the row it acted on, and nothing a body typed (``to``, ``reason``).
    audit_start = f"switch {label}@{target.id} agent={agent_id}"
    with action_locked(target, label, agent_id) as row:
        snap = action_check_needs(target, label, row.id, needs_id)
        dismissed = row.ended_at is None and action_dialog_guard(
            target,
            label,
            row.id,
            snap,
            dismiss=dismiss,
            doing="switching",
            audit_start=audit_start,
        )
        # The hand-over stops the agent before it starts it on the other account.
        with action_audited(
            lambda error: f"{audit_start} dismissed={action_yes_no(dismissed)} failed={error}"
        ):
            receipt = action_fleet_call(
                lambda: fleet_service.switch(
                    target, label, to=to, fresh=fresh, reason=reason, spawned_by="user"
                )
            )
    result: dict[str, object] = {
        "stopped": receipt.stopped.model_dump(mode="json"),
        "started": receipt.started.model_dump(mode="json"),
        "from_slot": receipt.from_slot,
        "to_slot": receipt.to_slot,
        "resumed": receipt.resumed,
        "prompt_typed": receipt.prompt_typed,
        "tmux_session": receipt.tmux_session,
        "notes": list(receipt.notes),
        "project": target.id,
    }
    from_slot = "-" if receipt.from_slot is None else str(receipt.from_slot)
    summary = (
        f"switch {label}@{target.id} slot={from_slot}->{receipt.to_slot} "
        f"dismissed={action_yes_no(dismissed)} resumed={action_yes_no(receipt.resumed)} "
        f"started={receipt.started.id}"
    )
    return result, summary


def action_handlers() -> dict[str, WriteHandler]:
    """The write handlers behind :data:`ACTION_ENDPOINTS`, in its order."""
    return {
        "agent/tell": action_tell,
        "agent/stop": action_stop,
        "agent/restart": action_restart,
        "agent/switch": action_switch,
    }


def action_routes(kit: RemoteKit) -> list[BaseRoute]:
    """``GET api/actions/recent``: this device's finished requests, newest first.

    What a phone that slept through a long action reads when it wakes, to learn
    how the action ended without sending it again. Only the asking device's own
    requests: what another phone asked for is not this one's business.
    """
    from starlette.responses import JSONResponse

    async def ledger_recent_endpoint(
        request: Request, device: Device, body: dict[str, Any]
    ) -> Response:
        return JSONResponse({"actions": kit.ledger.ledger_recent(device.id)})

    return [
        kit.kit_route(
            "/api/actions/recent", ledger_recent_endpoint, methods=["GET"], write_gated=False
        )
    ]


__all__ = [
    "ACTION_AUDIT_EXCERPT",
    "ACTION_ENDPOINTS",
    "ACTION_FIELD_MAX",
    "ACTION_LEDGER_SIZE",
    "ACTION_LEDGER_TTL",
    "ACTION_NEEDS_ID_MAX",
    "ACTION_POLL_SECONDS",
    "TELL_MODES",
    "TELL_TEXT_MAX",
    "ActionLedger",
    "LedgerEntry",
    "LedgerSeen",
    "action_handlers",
    "action_restart",
    "action_routes",
    "action_stop",
    "action_switch",
    "action_tell",
    "fleet_refusal",
    "new_action_ledger",
]
