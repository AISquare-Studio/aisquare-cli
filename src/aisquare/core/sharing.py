"""Opt-in anonymous usage sharing: what decides whether anything is sent, and where its state lives.

The switch is ``[sharing] enabled`` in ``config.toml`` — **off by default**. Beside the config,
``state.json`` keeps what the sharing itself needs under one key (:data:`STATE_KEY`): this
installation's random id, the salt its repository hashes are keyed with, whether the consent
dialog was answered, and what has been sent. The id and salt are minted at the first opt-in and
kept across off and on (docs/usage-telemetry.md); everything else is bookkeeping.

**Forced off.** ``DO_NOT_TRACK``, ``AISQUARE_NO_TELEMETRY`` and ``CI`` turn sharing off whatever
the switch says, and suppress the consent dialog (:func:`forced_off_reason`). A day on which it
was forced off while the switch was on is never sent later (:func:`withhold_today`).

**Never on a hot path.** The root callback calls :func:`start_background_ship` on every command;
with sharing off (the default, and any forced-off machine) that is an environment look and the
config's switch (``load_config`` reads the file and reuses its parse while the bytes are the
same). Turned on, it is a read of one JSON file and, once the
shipment is due (every :data:`SHIP_EVERY`), one detached ``aisquare metrics ship``: the network
is never touched by the process the user typed. Every function here fails open.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, Literal

from pydantic import BaseModel, Field

from aisquare.core import paths, state_file

STATE_KEY = "usage_sharing"
SHIP_EVERY = timedelta(hours=6)
"""How often a shipment is due once one has gone through."""
BACKOFF_FIRST = timedelta(minutes=5)
BACKOFF_MAX = SHIP_EVERY
"""A failed shipment is tried again after 5 minutes, then twice as long each time, up to 6 h."""
SPAWN_EVERY = timedelta(minutes=30)
"""Between two detached shippers started from commands, however many commands run meanwhile."""
KEEP_ACKED_DAYS = 60
"""Days whose acknowledged digest is kept; older ones are settled
(:attr:`SharingState.settled_through`)."""
MAX_BACKLOG_DAYS = 399
"""How far back an unsent day is still sent: the server takes dates back to today minus 400."""

ENV_DO_NOT_TRACK = "DO_NOT_TRACK"
ENV_NO_TELEMETRY = "AISQUARE_NO_TELEMETRY"
ENV_CI = "CI"


class SharingState(BaseModel):
    """What ``state.json`` keeps of the sharing (:data:`STATE_KEY`)."""

    install_id: str | None = None
    salt: str | None = None
    consent_asked_at: str | None = None
    consent_answer: Literal["share", "declined"] | None = None
    since: str | None = None
    """The local day sharing was turned on: the first day that is ever sent. A day before it
    was not agreed to and is never sent; a day since it is sent until the server has it."""
    acked: dict[str, str] = Field(default_factory=dict)
    """Local day → digest of the day as the server last accepted it (the last
    :data:`KEEP_ACKED_DAYS`): a day is sent again when its content changes, and today always is."""
    settled_through: str | None = None
    """Every day up to this one was acknowledged and is not looked at again: acknowledged days
    older than :data:`KEEP_ACKED_DAYS` are forgotten into it, which keeps this file small."""
    withheld: list[str] = Field(default_factory=list)
    """Local days on which something forced sharing off (:func:`forced_off_reason`) while it was
    on: a day in this list is never sent, whenever the force is lifted. (The last
    :data:`MAX_BACKLOG_DAYS`.)"""
    last_sent_at: str | None = None
    linked: bool | None = None
    next_due_at: str | None = None
    spawned_at: str | None = None
    failures: int = 0
    last_error: str | None = None


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() not in ("", "0", "false", "no", "off")


def forced_off_reason(environ: Mapping[str, str] | None = None) -> str | None:
    """Why sharing is off whatever the switch says, or ``None``: ``DO_NOT_TRACK=1`` (the
    cross-tool convention), ``AISQUARE_NO_TELEMETRY=1``, or a ``CI`` run."""
    env = os.environ if environ is None else environ
    if _truthy(env.get(ENV_DO_NOT_TRACK)):
        return f"{ENV_DO_NOT_TRACK} is set"
    if _truthy(env.get(ENV_NO_TELEMETRY)):
        return f"{ENV_NO_TELEMETRY} is set"
    if _truthy(env.get(ENV_CI)):
        return "CI is set (a CI run never shares)"
    return None


def switch_on() -> bool:
    """``[sharing] enabled``; ``False`` for a config that cannot be read (fail closed: sharing
    that cannot be read is not shared)."""
    try:
        from aisquare.core.config import load_config  # lazy: the root callback imports this

        return load_config(copy=False).sharing.enabled
    except Exception:
        return False


def enabled() -> bool:
    """Whether usage is shared now: the switch is on and nothing forces it off."""
    return forced_off_reason() is None and switch_on()


def read() -> SharingState:
    """The sharing's state; a fresh one for a file with none or one that cannot be read."""
    try:
        raw = state_file.read_state().get(STATE_KEY)
        return SharingState.model_validate(raw) if isinstance(raw, dict) else SharingState()
    except Exception:
        return SharingState()


def update(change: Any) -> SharingState:
    """Apply ``change(state)`` (mutating it) under the state file's lock and write it back.

    Raises what the state file raises (``StateUnwritableError``): a caller that must not fail
    catches it."""
    holder: dict[str, SharingState] = {}

    def apply(old: object | None) -> object | None:
        current = SharingState.model_validate(old) if isinstance(old, dict) else SharingState()
        change(current)
        holder["state"] = current
        return current.model_dump(mode="json", exclude_none=True)

    state_file.modify_state(STATE_KEY, apply)
    return holder["state"]


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def due(state: SharingState, now: datetime) -> bool:
    """Whether a shipment is due: none has been scheduled yet, or its time has come."""
    at = parse_time(state.next_due_at)
    return at is None or now >= at


def backoff(failures: int) -> timedelta:
    """How long after the ``failures``-th failure in a row the next try waits."""
    # The exponent stops at 10 (5 min x 1024 is past the cap): an unbounded one overflowed a
    # timedelta after ~40 failures in a row, and a server that stays away for a week is 28.
    delay: timedelta = BACKOFF_FIRST * (2 ** min(10, max(0, failures - 1)))
    return min(BACKOFF_MAX, delay)


def lock_path() -> Any:
    return paths.aisquare_home() / "usage-share.lock"


def withhold_today(now: datetime | None = None, tz: tzinfo | None = None) -> bool:
    """Mark the local day as one on which sharing was forced off, if the switch is on: that day
    is never shipped, even if the force is lifted before midnight. Costs a state write once a
    day, and only for someone who turned sharing on and then runs with ``DO_NOT_TRACK`` /
    ``AISQUARE_NO_TELEMETRY`` / ``CI``. Never raises; ``True`` when it wrote."""
    try:
        today = (now or datetime.now(UTC)).astimezone(tz).date()
        stamp = today.isoformat()
        if stamp in read().withheld:
            return False
        oldest = (today - timedelta(days=MAX_BACKLOG_DAYS)).isoformat()

        def mark(state: SharingState) -> None:
            if stamp not in state.withheld:
                state.withheld = [d for d in state.withheld if d >= oldest] + [stamp]

        update(mark)
        return True
    except Exception:
        return False


def start_background_ship(now: datetime | None = None) -> bool:
    """Start a detached ``aisquare metrics ship`` if sharing is on and a shipment is due.

    Called by the root callback of every command that is a feature (not a hook): so it costs a
    few microseconds when sharing is off, one small file read when it is on, and one process start
    once in :data:`SHIP_EVERY`. The shipper is marked started BEFORE it is, so that the commands
    that follow in the next half hour start none. Never raises; ``True`` when one was started."""
    try:
        if forced_off_reason() is not None:
            if switch_on():  # opted in, and running with sharing forced off: this day is not sent
                withhold_today(now)
            return False
        if not switch_on():
            return False
        moment = now or datetime.now(UTC)
        state = read()
        spawned = parse_time(state.spawned_at)
        if not due(state, moment) or (spawned is not None and moment - spawned < SPAWN_EVERY):
            return False
        update(lambda s: setattr(s, "spawned_at", moment.isoformat(timespec="seconds")))
        _spawn()
        return True
    except Exception:
        return False


def _spawn() -> None:
    """The detached shipper: its own session, no terminal, without any agent's tracing identity."""
    import subprocess  # lazy: only a command that finds a shipment due gets here

    from aisquare.core import selfcli, spawn

    subprocess.Popen(
        selfcli.argv_for(["metrics", "ship"]),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=sys.platform != "win32",
        env=spawn.untraced_env(),
    )
