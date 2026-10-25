"""The token ledger: what each agent session spent, counted from its own transcript.

One ruler, so that every before/after number a workstream reports is counted the same way
and can be compared and summed (``docs/plans/handoff-p1-token-diet.md`` §1.7). What it
counts is :mod:`aisquare.core.ledger`'s (requests, the baseline, context, wakes, deltas,
compactions, tool calls); where each sits in a transcript is the session's agent's own
reader's (``AgentTranscript.ledger``): Claude Code's transcript
(``agent_adapters.claude_ledger``), Codex's rollout (``agent_adapters.codex_ledger``), any
agent's by its adapter (#296's WS-0, made every agent's in #113's #17).

Read line by line, so a transcript of any size costs one line of memory; a line that does
not parse (the one the agent is writing) is skipped and counted. What comes out is counts,
never text: transcripts hold prompts.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from aisquare.core import harness
from aisquare.core.agent_adapters import recorded_adapter
from aisquare.core.ledger import BOARD_READ as BOARD_READ
from aisquare.core.ledger import DELTA_CLOSE as DELTA_CLOSE
from aisquare.core.ledger import DELTA_OPEN as DELTA_OPEN
from aisquare.core.ledger import WAKE_MARKER as WAKE_MARKER
from aisquare.core.ledger import SessionLedger as SessionLedger
from aisquare.core.ledger import is_board_read as is_board_read
from aisquare.core.store import store_session
from aisquare.models import TeamSession


def sessions(
    project_id: str | None, *, session: str | None = None, role: str | None = None
) -> list[TeamSession]:
    """The sessions ``metrics ledger`` reads, oldest first: the project's, or every
    project's for ``None``; narrowed to an id prefix and to a role, a numbered seat
    (``coder2``) counting as its role."""
    with store_session() as store:
        projects = (
            [project.id for project in store.list_projects(all=True)]
            if project_id is None
            else [project_id]
        )
        found = [row for pid in projects for row in store.team_sessions(pid)]
    if session:
        found = [row for row in found if row.id.startswith(session)]
    if role:
        found = [row for row in found if role in (row.role, harness.base_role(row.role))]
    return sorted(found, key=lambda row: row.started_at)


def ledger(
    sessions: Iterable[TeamSession],
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[SessionLedger]:
    """One :class:`SessionLedger` per session, from the entries stamped within
    ``[since, until]`` (an entry with no timestamp counts). A session whose transcript is
    unrecorded or gone is a row of zeros, ``transcript_found`` false."""
    return [_one(session, since=since, until=until) for session in sessions]


def total(rows: Iterable[SessionLedger]) -> SessionLedger:
    """Every row summed into one, ``session_id`` ``total``: the peak is the highest, the
    mean is weighted by requests, the baseline is not summed (it is per session)."""
    summed = SessionLedger(session_id="total", role="")
    weighted = 0
    for row in rows:
        summed.transcript_found = summed.transcript_found or row.transcript_found
        for field in _SUMMED:
            setattr(summed, field, getattr(summed, field) + getattr(row, field))
        summed.peak_context = max(summed.peak_context, row.peak_context)
        weighted += row.mean_context * row.requests
        summed.compaction_triggers = dict(
            Counter(summed.compaction_triggers) + Counter(row.compaction_triggers)
        )
        summed.tool_calls = dict(Counter(summed.tool_calls) + Counter(row.tool_calls))
    summed.mean_context = round(weighted / summed.requests) if summed.requests else 0
    return summed


_SUMMED = (
    "requests",
    "input_fresh",
    "input_cache_write",
    "input_cache_read",
    "output",
    "latest_context",
    "wakes",
    "no_action_wakes",
    "wake_input",
    "delta_blocks",
    "delta_chars",
    "delta_carried_chars",
    "compactions",
    "skipped_lines",
)


def _one(session: TeamSession, *, since: datetime | None, until: datetime | None) -> SessionLedger:
    """``session``'s row, read by its agent's own transcript reader (a session whose agent
    was never recorded is Claude Code's, ``recorded_adapter``). A transcript unrecorded or
    gone, or an agent this install does not know, is a row of zeros."""
    row = SessionLedger(session_id=session.id, role=session.role)
    path = Path(session.transcript_path) if session.transcript_path else None
    adapter = recorded_adapter(session.agent)
    reader = adapter.transcript_reader if adapter is not None else None
    if path is None or not path.is_file() or reader is None:
        return row
    counted = reader.ledger(path, since=since, until=until)
    return counted.model_copy(update={"session_id": session.id, "role": session.role})


def _deltas(entry: dict[str, Any]) -> list[str]:
    """The ``<aisquare-team-delta>`` blocks a Claude Code hook's attachment carries, tags
    included (``agent_adapters.claude_ledger``)."""
    from aisquare.core import ledger
    from aisquare.core.agent_adapters import claude_ledger

    return [block for text in claude_ledger._attachment_texts(entry)
            for block in ledger.delta_blocks(text)]  # fmt: skip
