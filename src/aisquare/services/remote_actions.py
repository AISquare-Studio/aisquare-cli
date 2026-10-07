"""Agent actions from the phone: stop, restart, switch (SPEC §3), and the request ledger.

The actions are ordinary write handlers (:data:`ACTION_ENDPOINTS`,
:func:`action_handlers`). The write dispatcher runs each one in a worker thread
behind ``allow_write``, the request ledger and the audit log, as it runs ``note``
or ``send-keys``. Acting on a live agent from a phone needs more than that, and
the rest is here:

* **A pin.** ``agent_id`` must still be the newest row holding the label, and a
  card's ``needs_id`` must still be one of the agent's items. Both are checked
  before anything reaches the fleet. A screen that went stale therefore never
  stops, restarts or moves the replacement a manager started meanwhile (409
  ``stale``, with what is ``current``).
* **A confirmation.** Each action carries ``confirm=<label>``.
* **The dialog guard.** A stop types ``/exit`` and Enter, and restart and switch
  stop the agent the same way. With a dialog up, that Enter answers it: it can
  approve a Bash command or take a question's first option. So an open dialog
  refuses the action (409 ``dialog_open``), unless ``dismiss_dialog`` asks for one
  Escape (No) first.
* **One action per agent at a time.** ``remote_server.remote_agent_lock`` is
  taken without waiting, and the needs card's quick answers take it too (409
  ``busy``).
* **The fleet's own refusals**, mapped as ``asq fleet`` maps them
  (:func:`fleet_refusal`). A 200 body is the CLI's ``--json`` payload plus
  ``project``.

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
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, TypedDict, TypeVar

from aisquare.services import remote_needs, remote_server
from aisquare.services.remote_server import RequestError

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import BaseRoute

    from aisquare.models import FleetAgent, ProjectInfo
    from aisquare.services.remote_needs import AgentNow
    from aisquare.services.remote_server import Device, RemoteKit, WriteHandler

ACTION_ENDPOINTS: tuple[str, ...] = ("agent/stop", "agent/restart", "agent/switch")
"""The action names ``POST api/{name}`` accepts beside ``remote_server.WRITE_ENDPOINTS``."""

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


def action_required(body: dict[str, Any], key: str) -> str:
    """A string the action cannot go without; 400 ``invalid`` when it is missing or blank."""
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RequestError(400, "invalid", f"{key!r} is required")
    return value.strip()


def action_ref(body: dict[str, Any], key: str, *, limit: int | None = None) -> str | None:
    """An optional string: absent, null and blank all mean none; over ``limit`` is a 413."""
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RequestError(400, "invalid", f"{key!r} must be a string")
    if limit is not None and len(value) > limit:
        raise RequestError(413, "too_large", f"{key!r} is over {limit} characters")
    return value.strip() or None


def action_flag(body: dict[str, Any], key: str) -> bool:
    """An optional ``true`` or ``false``, and nothing else.

    ``"false"`` is a non-empty string, and read as true it would make ``force``
    kill an agent without its ``/exit``.
    """
    value = body.get(key)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise RequestError(400, "invalid", f"{key!r} must be true or false")
    return value


def action_pinned(body: dict[str, Any]) -> tuple[str, str]:
    """``agent``, ``confirm`` and ``agent_id``, which stop, restart and switch all require.

    ``confirm`` must repeat the label exactly. The page sends it from its confirm
    sheet, so a stray tap, or a body built by hand, does not stop an agent.
    """
    label = action_required(body, "agent")
    if body.get("confirm") != label:
        raise RequestError(400, "confirm_required", f"confirm by sending confirm={label}")
    return label, action_required(body, "agent_id")


def action_yes_no(flag: bool) -> str:
    return "yes" if flag else "no"


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


def action_stale(target: ProjectInfo, label: str, current: FleetAgent | None) -> RequestError:
    """409 ``stale``: the label is not the agent the phone saw, and ``current`` says who is."""
    if current is None:
        said = f"there is no agent {label!r} in {target.root.name or target.id} now"
    else:
        said = f"{label!r} is another agent now ({current.id})"
    return RequestError(
        409,
        "stale",
        f"{said} — nothing was done",
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


def action_snapshot(target: ProjectInfo, label: str, agent_id: str | None) -> AgentNow:
    """The agent as needs-you reads it now. With a pin, it must still be the pinned row.

    Every snapshot is checked, not only the first. The reads after an Escape go
    on for seconds, and the pane they send to must still belong to the agent the
    phone pinned.
    """
    snap = action_fleet_call(lambda: remote_needs.needs_agent_now(target, label))
    if agent_id is not None and snap.status is not None and snap.status.agent.id != agent_id:
        raise action_stale(target, label, snap.status.agent)
    return snap


def action_check_needs(
    target: ProjectInfo, label: str, agent_id: str | None, needs_id: str | None
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
    snap = action_snapshot(target, label, agent_id)
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
    agent_id: str | None,
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
        snap = action_snapshot(target, label, agent_id)
        if reached(snap):
            return snap
    return None


def action_dialog_guard(
    target: ProjectInfo,
    label: str,
    agent_id: str,
    snap: AgentNow | None,
    *,
    dismiss: bool,
    doing: str,
) -> bool:
    """Refuse to stop an agent that shows a dialog, or with ``dismiss``, press Escape first.

    ``doing`` names the action for the refusal ("stopping"). Returns whether a
    dialog was dismissed, which the audit line records. A false alarm costs a
    refusal with a sentence, or with ``dismiss`` an Escape to an agent about to be
    stopped anyway. It never costs an Enter into a dialog.
    """
    snap = snap if snap is not None else action_snapshot(target, label, agent_id)
    if not remote_needs.needs_dialog_open(snap):
        return False
    if not dismiss:
        raise RequestError(
            409,
            "dialog_open",
            f"{label} is showing a prompt; {doing} would answer it — "
            "send dismiss_dialog: true to press Esc (No) first",
        )
    action_press_escape(snap, label)
    closed = action_settle(
        target,
        label,
        agent_id,
        lambda again: not remote_needs.needs_dialog_open(again),
        remote_needs.DIALOG_SETTLE_SECONDS,
    )
    if closed is None:
        raise RequestError(
            409,
            "dialog_open",
            f"Escape was sent, but {label} still shows a prompt — nothing else was done",
        )
    return True


# --- the actions ---------------------------------------------------------------------------


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
    needs_id = action_ref(body, "needs_id", limit=ACTION_NEEDS_ID_MAX)
    target = action_project(body)
    with action_locked(target, label, agent_id) as row:
        snap = action_check_needs(target, label, agent_id, needs_id)
        guarded = not force and row.ended_at is None
        dismissed = guarded and action_dialog_guard(
            target, label, agent_id, snap, dismiss=dismiss, doing="stopping"
        )
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
    summary = (
        f"stop {label}@{target.id} agent={agent_id} force={action_yes_no(force)} "
        f"dismissed={action_yes_no(dismissed)} released={len(released)}"
    )
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
    needs_id = action_ref(body, "needs_id", limit=ACTION_NEEDS_ID_MAX)
    target = action_project(body)
    with action_locked(target, label, agent_id) as row:
        snap = action_check_needs(target, label, agent_id, needs_id)
        dismissed = row.ended_at is None and action_dialog_guard(
            target, label, agent_id, snap, dismiss=dismiss, doing="restarting"
        )
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
    needs_id = action_ref(body, "needs_id", limit=ACTION_NEEDS_ID_MAX)
    target = action_project(body)
    with action_locked(target, label, agent_id) as row:
        snap = action_check_needs(target, label, agent_id, needs_id)
        dismissed = row.ended_at is None and action_dialog_guard(
            target, label, agent_id, snap, dismiss=dismiss, doing="switching"
        )
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
    "ACTION_ENDPOINTS",
    "ACTION_FIELD_MAX",
    "ACTION_LEDGER_SIZE",
    "ACTION_LEDGER_TTL",
    "ACTION_NEEDS_ID_MAX",
    "ACTION_POLL_SECONDS",
    "ActionLedger",
    "LedgerEntry",
    "action_handlers",
    "action_restart",
    "action_routes",
    "action_stop",
    "action_switch",
    "fleet_refusal",
    "new_action_ledger",
]
