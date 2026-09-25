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
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aisquare.core import paths, transcripts
from aisquare.core.atomic import write_replacing
from aisquare.core.store import store_session
from aisquare.core.tmux import TmuxServer
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


class NoReply(Exception):
    """The captain did not answer, or could not be reached, in time — the message says which."""


@dataclass(frozen=True)
class Reply:
    """What the captain said back, and when its answering turn ended."""

    text: str
    ended_at: datetime | None = None


def brain_dir() -> Path:
    """The captain's working directory: under the home, never a project."""
    return captain_state.captain_dir() / "brain"


def mcp_config_path() -> Path:
    return captain_state.captain_dir() / "mcp.json"


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
                "env": {"AISQUARE_HOME": str(paths.aisquare_home())},
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
        f"AISQUARE_HOME={paths.aisquare_home()}",
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
    """The home's live captain row, if there is one."""
    home = captain_state.home_project()
    with store_session() as store:
        return store.fleet_agent_by_label(home.id, fleet.CAPTAIN_LABEL, live_only=True)


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
    agent = find()
    if agent is None:
        agent = start(prompt=text).agent
        typed_at = _now()
    else:
        srv = _wait_until_ready(agent, deadline, timeout)
        typed_at = _now()
        srv.paste(agent.pane_id, text)
        srv.send_keys(agent.pane_id, "Enter")
    return _await_reply(agent, typed_at, deadline, timeout)


def _wait_until_ready(agent: FleetAgent, deadline: datetime, timeout: float) -> TmuxServer:
    srv = fleet.server_for(agent.tmux_socket)
    while True:
        state = fleet.status_of(agent).state
        if state == "waiting" and fleet.pane_is_the_agent(srv, agent.pane_id):
            return srv
        if state in ("exited", "lost"):
            raise NoReply(
                f"the captain is {state} — `aisquare captain` starts it again; nothing was typed"
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
    while True:
        session = _session_of(agent)
        if session is not None and session.state == "waiting" and session.last_seen_at > typed_at:
            path = Path(session.transcript_path) if session.transcript_path else None
            text = transcripts.last_reply(path) if path is not None else None
            return Reply(
                text or "(the captain's turn ended without text — see its pane)",
                ended_at=session.last_seen_at,
            )
        if _now() >= deadline:
            raise NoReply(
                f"the captain did not answer within {timeout:g}s — its answer will be in its "
                "pane (`aisquare captain`)"
            )
        _sleep(_POLL_S)


def _session_of(agent: FleetAgent) -> TeamSession | None:
    if agent.session_id is None:
        return None
    with store_session() as store:
        return store.get_session(agent.session_id)
