"""Where the captain runs and how it starts: its brain folder, its one server, one per home (T2).

The captain is a Claude Code session the fleet starts like any agent (contract,
seq 13121), with four things of its own:

- **The home board.** Its row lives on the project row for ``$AISQUARE_HOME``
  (:func:`state.home_project`), which the store keeps captured and never
  onboards, so the home never joins the owner's projects.
- **The brain folder.** It runs from ``$AISQUARE_HOME/captain/brain``, never a
  repository, so no project's CLAUDE.md or hooks brief it as a worker. Its
  window carries ``AISQUARE_HOME`` and ``AISQUARE_TEAM_HUB=<home>``: ``.aisquare``
  is itself a project-root marker, and without the hub a brain under
  ``~/.aisquare`` would resolve its board to ``$HOME``.
- **One server and no other tool.** ``--strict-mcp-config --mcp-config
  <captain/mcp.json>`` mounts only the Actions server, through the module entry
  (0.8 s to its first answer against 1.0 s through the CLI); ``--tools ""``
  leaves it no built-in tool; ``--allowedTools mcp__captain__*`` pre-approves
  its own. ``mcp.json`` is rewritten at every start, so no interpreter path is
  frozen into the launch spec a restart replays.
- **One per home**, enforced by the fleet (``fleet.CAPTAIN_ROLE``).

Every path handed to the window is ABSOLUTE (:func:`_home`): the window starts in
the brain folder, where a relative ``AISQUARE_HOME`` would name another folder.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aisquare.core import paths, transcripts
from aisquare.core.atomic import write_replacing
from aisquare.core.locking import lock_exclusive, unlock
from aisquare.core.store import store_session
from aisquare.core.tmux import TmuxError, TmuxServer
from aisquare.models import FleetAgent, TeamSession
from aisquare.services import fleet
from aisquare.services.captain import state as captain_state

PERSONA = "captain"
SERVER = "captain"
"""The MCP server's name in ``mcp.json``: Claude Code calls its tools ``mcp__captain__<tool>``."""

SAY_TIMEOUT_S = 180.0
_POLL_S = 1.0

# Indirection so a test runs a wait on a fake clock.
_now: Callable[[], datetime] = lambda: datetime.now(tz=UTC)  # noqa: E731
_sleep: Callable[[float], None] = time.sleep


_LOCK_HELD = {errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES}


class NoReply(Exception):
    """The captain did not answer, or could not be reached — the message says which.

    ``timed_out`` is False when waiting longer would not have helped: the captain is
    dead, the prompt was never typed, tmux refused the keys.
    """

    def __init__(self, message: str, *, timed_out: bool = True) -> None:
        super().__init__(message)
        self.timed_out = timed_out


@dataclass(frozen=True)
class Reply:
    """What the captain said back, and when its answering turn ended.

    ``text`` is ``None`` when the turn ended without text — it answered with tools
    alone. Never a placeholder in its place: under ``--json`` that read as the
    captain's own words, and the voice page (T3) would speak it.
    """

    text: str | None
    ended_at: datetime | None = None


def _home() -> Path:
    """The home, resolved — the spelling ``state.home_project`` keys the home board by."""
    return paths.aisquare_home().resolve()


def brain_dir() -> Path:
    """The captain's working directory: under the home, never a project."""
    return _home() / "captain" / "brain"


def mcp_config_path() -> Path:
    return _home() / "captain" / "mcp.json"


def write_mcp_config() -> Path:
    """Write the one-server MCP config the captain's session mounts; returns its path."""
    config = {
        "mcpServers": {
            SERVER: {
                "command": sys.executable,
                "args": ["-m", "aisquare.services.captain", "--stdio", "--close-after", "0"],
                # The window carries AISQUARE_HOME only when an account is chosen
                # (fleet.spawn), so the server is told its home here, whatever the
                # tmux server's environment says.
                "env": {"AISQUARE_HOME": str(_home())},
            }
        }
    }
    path = mcp_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_replacing(path, json.dumps(config, indent=2) + "\n")
    return path


def launch_args(home_root: Path) -> list[str]:
    """The captain's agent arguments: its environment (``launch -e``) and its only tools."""
    return [
        "-e",
        f"AISQUARE_HOME={_home()}",
        "-e",
        f"AISQUARE_TEAM_HUB={home_root}",
        "--strict-mcp-config",
        "--mcp-config",
        str(mcp_config_path()),
        "--tools",
        "",
        "--allowedTools",
        f"mcp__{SERVER}__*",
    ]


def find() -> FleetAgent | None:
    """The home's captain that can still answer, if there is one.

    Read through the fleet's own reconciliation, not the bare row: a captain that
    exited with nothing open to notice (no UI, no ``fleet ls``) kept a live row, and
    ``say`` refused it while the bare command attached to its dead pane. A dead pane
    is ended by the listing itself (``fleet.list_agents``); a VANISHED one — the
    server restarted — by a reap of the home, which ends a row only on tmux's own
    word that the pane is gone. Either way the next start replaces it.
    """
    home = captain_state.home_project()
    for status in fleet.list_agents(home):
        agent = status.agent
        if agent.role != fleet.CAPTAIN_ROLE or agent.ended_at is not None:
            continue
        if status.state == "exited":
            return None
        if status.state == "lost":
            fleet.reap(home)
            with store_session() as store:
                return store.fleet_agent_by_label(home.id, fleet.CAPTAIN_LABEL, live_only=True)
        return agent
    return None


def start(prompt: str | None = None, *, size: tuple[int, int] | None = None) -> fleet.SpawnReceipt:
    """Start the home's captain. The fleet refuses a second one (one per home)."""
    home = captain_state.home_project()
    brain_dir().mkdir(parents=True, exist_ok=True)
    write_mcp_config()
    return fleet.spawn(
        home,
        fleet.CAPTAIN_ROLE,
        label=fleet.CAPTAIN_LABEL,
        persona=PERSONA,
        cwd=brain_dir(),
        agent_args=launch_args(home.root),
        prompt=prompt,
        size=size,
    )


def say(text: str, *, timeout: float = SAY_TIMEOUT_S) -> Reply:
    """Deliver ``text`` to the captain and wait for its reply (contract 13121, item 5).

    - No live captain: it is started with ``text`` as its first prompt.
    - A captain waiting at its prompt: ``text`` is typed (one bracketed paste, one
      Enter).
    - A BUSY captain: waited for until its turn ends, then typed. Never a board
      note — nothing would prompt the captain to read one.

    The reply is the transcript's text since the prompt (:func:`transcripts.last_reply`),
    read once the captain's session reads ``waiting`` AFTER the text went in (a
    ``waiting`` from before it is the previous turn's end). Raises :class:`NoReply`
    past ``timeout`` seconds.
    """
    if not text.strip():
        raise ValueError("nothing to say")
    deadline = _now() + timedelta(seconds=timeout)
    with _one_at_a_time(deadline, timeout):
        agent = find()
        if agent is None:
            typed_at = _now()
            receipt = start(prompt=text)
            if not receipt.prompt_typed:
                why = "; ".join(receipt.notes) or "the fleet did not say why"
                raise NoReply(
                    f"the captain was started but the message never reached it ({why}) — "
                    "`aisquare captain` shows its pane",
                    timed_out=False,
                )
            agent = receipt.agent
        else:
            srv = _wait_until_ready(agent, deadline, timeout)
            typed_at = _now()
            _type(srv, agent, text)
        return _await_reply(agent, typed_at, deadline, timeout)


@contextlib.contextmanager
def _one_at_a_time(deadline: datetime, timeout: float) -> Iterator[None]:
    """Hold ``captain/say.lock`` from the lookup to the reply: one delivery at a time.

    Two says into one waiting captain both typed, and both read the one reply that
    came back. The wait for the lock is bounded by the say's own deadline; the OS
    drops it if the holder dies. A lock file, as ``core.state_file`` holds one.
    """
    lock_path = _home() / "captain" / "say.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        while True:
            try:
                lock_exclusive(fd)
                break
            except OSError as exc:
                if exc.errno not in _LOCK_HELD:
                    raise
                if _now() >= deadline:
                    raise NoReply(
                        f"another message to the captain is still waiting for its reply "
                        f"(waited {timeout:g}s) — nothing was typed"
                    ) from exc
                _sleep(_POLL_S)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                unlock(fd)
    finally:
        os.close(fd)


def _type(srv: TmuxServer, agent: FleetAgent, text: str) -> None:
    """One bracketed paste, one Enter — a tmux refusal is said, as ``fleet.tell`` says it."""
    try:
        srv.paste(agent.pane_id, text)
    except TmuxError as exc:
        raise NoReply(
            f"tmux could not type into the captain's pane ({exc}); nothing reached it — "
            "`aisquare captain` shows its pane",
            timed_out=False,
        ) from exc
    try:
        srv.send_keys(agent.pane_id, "Enter")
    except TmuxError as exc:
        raise NoReply(
            f"the text is in the captain's input but tmux could not press Enter ({exc}) — "
            "`aisquare captain` to send it; do not say it again",
            timed_out=False,
        ) from exc


def _wait_until_ready(agent: FleetAgent, deadline: datetime, timeout: float) -> TmuxServer:
    srv = fleet.server_for(agent.tmux_socket)
    while True:
        state = fleet.status_of(agent).state
        if state == "waiting" and fleet.pane_is_the_agent(srv, agent.pane_id):
            return srv
        if state in ("exited", "lost"):
            raise NoReply(
                f"the captain is {state} — `aisquare captain` starts it again; nothing was typed",
                timed_out=False,
            )
        if _now() >= deadline:
            raise NoReply(
                f"the captain stayed {state} for {timeout:g}s, so nothing was typed — "
                "`aisquare captain` shows what it is doing"
            )
        _sleep(_POLL_S)


def _await_reply(
    agent: FleetAgent, typed_at: datetime, deadline: datetime, timeout: float
) -> Reply:
    """Wait for the turn that answers the text typed at ``typed_at``, and read its reply.

    Answered means all three: the session reads ``waiting``, it was seen after the
    text went in, and its transcript's last prompt was stamped at or after it. The
    first two alone took a hook that bumps ``last_seen_at`` on a ``waiting`` row — a
    SessionEnd, a quiet notice — for the answer, and handed back the previous turn's
    reply. The row is re-read on every poll: after a ``/clear`` it is bound to the
    new session, which is the one that answers. A captain that dies, or parks on its
    usage limit, is said at once rather than waited out.
    """
    while True:
        row, session = _bound(agent)
        if row is None or row.ended_at is not None:
            raise NoReply("the captain exited before it answered", timed_out=False)
        if (
            session is not None
            and session.ended_at is None
            and session.state == "waiting"
            and session.last_seen_at > typed_at
            and session.transcript_path
        ):
            text = transcripts.last_reply(Path(session.transcript_path), since=typed_at)
            if text is not None:
                return Reply(text or None, ended_at=session.last_seen_at)
        state = fleet.status_of(row).state
        if state in ("exited", "lost"):
            raise NoReply(
                f"the captain is {state} and did not answer — `aisquare captain` starts it again",
                timed_out=False,
            )
        if state == "limited":
            raise NoReply(
                "the captain hit its usage limit before it answered — its answer comes "
                "after the reset, in its pane (`aisquare captain`)",
                timed_out=False,
            )
        if _now() >= deadline:
            raise NoReply(
                f"the captain did not answer within {timeout:g}s — its answer will be in its "
                "pane (`aisquare captain`)"
            )
        _sleep(_POLL_S)


def _bound(agent: FleetAgent) -> tuple[FleetAgent | None, TeamSession | None]:
    """The captain's row as it is NOW, and the session bound to it."""
    with store_session() as store:
        row = store.get_fleet_agent(agent.id)
        session = store.get_session(row.session_id) if row and row.session_id else None
    return row, session
