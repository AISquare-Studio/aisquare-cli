"""asq's daily release check, and the agents its one-click update keeps.

The check asks PyPI what ``aisquare upgrade --check`` asks it
(:func:`~aisquare.services.lifecycle.latest_release`, the only lookup) and nothing more, at
most once a day: at asq's start when the last answer is :data:`CHECK_EVERY` old or older, and
again as each one comes due while asq runs. asq asks :func:`check_for_release` every
:data:`ASK_EVERY_SECONDS`, and :func:`due` decides whether that ask looks anything up. A
lookup that fails (offline, PyPI erring, a uv setting that holds releases back) is no
answer: nothing is recorded and the next hourly ask tries again. ``[updates] check = false``
turns the check off, and a checkout (an editable or local-source install) is never checked:
it moves with its source, not with PyPI.

What it found is kept in the store's ``ui_state``, beside what asq had open: when PyPI last
answered, the newest release it named, and the release a toast last announced. So a release
is announced once, and offered under *⚙ Settings* until the version that runs is that one or
newer (the plan's ``up_to_date``: a version ahead of PyPI's is offered nothing).

The update is ``aisquare upgrade``'s, handed the terminal as Doctor's Update hands it. At the
press, :func:`record_press` writes down every agent alive then, by row and pane; the asq that
reopens takes that record once (:func:`take_press`), and :func:`reattach_agents` restarts each one
whose pane is gone through ``fleet.restart``, which resumes its session when the transcript
is on disk. Agents outlive asq on the fleet's tmux server, so normally every one is still
there. Nothing else is ever restarted: an ordinary start reads no record.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aisquare.core.config import load_config
from aisquare.core.store import store_session
from aisquare.core.version import __version__
from aisquare.models import FleetAgent
from aisquare.services import fleet as fleet_service
from aisquare.services import install_route
from aisquare.services import lifecycle as lifecycle_service

CHECKED_KEY = "updates.checked_at"
"""``ui_state``: when PyPI last answered the check (ISO 8601, UTC). A failed lookup leaves it."""
LATEST_KEY = "updates.latest"
"""``ui_state``: the newest release that answer named."""
ANNOUNCED_KEY = "updates.announced"
"""``ui_state``: the release a toast last announced, so each is announced once."""
PRESSED_KEY = "updates.pressed"
"""``ui_state``: what "Restart & update" recorded at the press (:func:`record_press`)."""

CHECK_EVERY = timedelta(hours=24)
"""How old the last answer is before the check asks PyPI again."""
ASK_EVERY_SECONDS = 3600.0
"""How often asq asks :func:`check_for_release` while it runs: a failed lookup's retry, at
most hourly."""

_CHECKOUTS = frozenset({install_route.EDITABLE, install_route.LOCAL_SOURCE})


@dataclass(frozen=True)
class Offer:
    """A newer release, as the sidebar's ⬆ row offers it."""

    version: str
    """The newest release the check found."""
    current: str
    """The version that runs."""
    runnable: bool
    """Whether asq updates this install itself; otherwise its dialog says the command."""
    announce: bool
    """The first time this release was found: one toast says so."""


def lookup_latest() -> str | None:
    """The newest release for this install, or ``None`` when that could not be learned.

    The check's one way out of the process, and the seam its tests and the real-app captures
    replace: never a request of its own, only what ``aisquare upgrade --check`` asks.
    """
    return lifecycle_service.latest_release().version


def route_is_checked(route: install_route.InstallRoute) -> bool:
    """Whether an install that came by ``route`` is checked: every one but a checkout's."""
    return route.kind not in _CHECKOUTS


def check_enabled() -> bool:
    """``[updates] check``; on when the config cannot be read, as its default is."""
    try:
        return load_config().updates.check
    except Exception:
        return True


def due(checked_at: datetime | None, now: datetime) -> bool:
    """Whether the last answer, given at ``checked_at``, is old enough to ask again. One from
    the future is due: a clock set back must not hold the check off until it catches up."""
    if checked_at is None or checked_at > now:
        return True
    return now - checked_at >= CHECK_EVERY


def _instant(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def check_for_release(now: datetime | None = None) -> Offer | None:
    """The release the ⬆ row offers, or ``None``; PyPI is asked first when an answer is due.

    Nothing at all for a checkout, or with ``[updates] check`` off. A lookup that fails is
    not recorded, and the release an earlier answer named is offered meanwhile. Writes the
    store, a little: the answer, and the release announced.
    """
    if not check_enabled():
        return None
    route = install_route.detect()
    if not route_is_checked(route):
        return None
    now = now or datetime.now(UTC)
    with store_session() as store:
        checked_at = _instant(store.ui_state(CHECKED_KEY))
        latest = store.ui_state(LATEST_KEY)
    if due(checked_at, now):
        found = lookup_latest()
        if found is not None:
            latest = found
            with store_session() as store:
                store.set_ui_state(CHECKED_KEY, now.isoformat())
                store.set_ui_state(LATEST_KEY, found)
    if latest is None or lifecycle_service.newer_release(latest, __version__) is not True:
        return None
    with store_session() as store:
        announce = store.ui_state(ANNOUNCED_KEY) != latest
        if announce:
            store.set_ui_state(ANNOUNCED_KEY, latest)
    return Offer(
        version=latest,
        current=__version__,
        runnable=install_route.not_automated(route) is None,
        announce=announce,
    )


# --- the press, and the reopen ----------------------------------------------------------------


@dataclass(frozen=True)
class Kept:
    """One agent alive at the press: its row, its pane and its label."""

    agent_id: str
    pane_id: str
    label: str


@dataclass(frozen=True)
class Press:
    """What "Restart & update" recorded: the version that ran, and the agents alive then."""

    previous: str
    agents: tuple[Kept, ...]


def record_press(agents: Iterable[FleetAgent]) -> Press:
    """Write down ``agents``, the ones alive at the press, for the asq that reopens."""
    press = Press(
        previous=__version__,
        agents=tuple(Kept(agent.id, agent.pane_id, agent.label) for agent in agents),
    )
    payload = {
        "previous": press.previous,
        "agents": [
            {"id": kept.agent_id, "pane": kept.pane_id, "label": kept.label}
            for kept in press.agents
        ],
    }
    with store_session() as store:
        store.set_ui_state(PRESSED_KEY, json.dumps(payload))
    return press


def take_press() -> Press | None:
    """The press :func:`record_press` wrote, forgotten as it is read: one reopen acts on it.

    ``None`` when there is none, or when what is there cannot be read as one.
    """
    with store_session() as store:
        raw = store.ui_state(PRESSED_KEY)
        if raw is not None:
            store.set_ui_state(PRESSED_KEY, None)
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
        return Press(
            previous=str(payload["previous"]),
            agents=tuple(
                Kept(str(row["id"]), str(row["pane"]), str(row["label"]))
                for row in payload["agents"]
            ),
        )
    except (ValueError, KeyError, TypeError):
        return None


@dataclass(frozen=True)
class Reattached:
    """What the reopen found of the agents the press recorded, and what it did."""

    previous: str
    """The version that ran at the press."""
    version: str
    """The version that runs now."""
    kept: tuple[str, ...] = ()
    """The labels whose agent still runs."""
    restarted: tuple[tuple[str, str], ...] = ()
    """``(label, why it was not running)`` for each one restarted."""
    failed: tuple[tuple[str, str], ...] = ()
    """``(label, why it was not restarted)``: its row stays as the listing shows it."""

    @property
    def updated(self) -> bool:
        """Whether the version that runs is not the one that ran at the press."""
        return not install_route.same_version(self.version, self.previous)

    def toast(self) -> str:
        """``✓ aisquare 0.8.3 — 2 agents reattached, 1 restarted (coder-1: pane gone)``."""
        head = (
            f"✓ aisquare {self.version}"
            if self.updated
            else f"aisquare {self.version} is still installed: the update did not move it"
        )
        count = len(self.kept)
        parts = [f"{count} agent{'' if count == 1 else 's'} reattached"]
        if self.restarted:
            why = ", ".join(f"{label}: {reason}" for label, reason in self.restarted)
            parts.append(f"{len(self.restarted)} restarted ({why})")
        if self.failed:
            why = ", ".join(f"{label}: {reason}" for label, reason in self.failed)
            parts.append(f"not restarted ({why})")
        return f"{head} — {', '.join(parts)}"


def reattach_agents(press: Press) -> Reattached:
    """Each agent ``press`` recorded: still running, or restarted on its session.

    Running is what the listing reads as having a process (``fleet.PROCESS_STATES``,
    ``unknown`` among them: a server that could not be asked is no pane shown gone, and a
    restart would stop whatever runs there). Any other is restarted as the agent view's
    Restart does it, pinned to the recorded row (``agent_id``): ``fleet.restart`` resumes
    the session when its transcript is on disk. A restart that is refused, or a row that is
    gone, is named and its row left as the listing shows it.
    """
    kept: list[str] = []
    restarted: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []
    for recorded in press.agents:
        with store_session() as store:
            agent = store.get_fleet_agent(recorded.agent_id)
        if agent is None:
            failed.append((recorded.label, "its row is gone"))
            continue
        status = fleet_service.status_of(agent)
        if status.state in fleet_service.PROCESS_STATES:
            kept.append(agent.label)
            continue
        why = status.detail or status.state
        try:
            fleet_service.restart(fleet_service.project_of(agent), agent.label, agent_id=agent.id)
        except Exception as exc:  # a refusal is an answer to name, never a crash
            failed.append((agent.label, f"{why}; {exc}"))
        else:
            restarted.append((agent.label, why))
    return Reattached(
        previous=press.previous,
        version=__version__,
        kept=tuple(kept),
        restarted=tuple(restarted),
        failed=tuple(failed),
    )


def reattach_after_update() -> Reattached | None:
    """What the asq that ``upgrade --one-click`` reopened does first: take the press's record
    and :func:`reattach_agents` its agents. ``None`` when there is no record to take."""
    press = take_press()
    return None if press is None else reattach_agents(press)
