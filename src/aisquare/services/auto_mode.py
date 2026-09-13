"""Auto mode behind the explainability proxy (#150): measure, warn, detect.

**The failure.** Claude Code's ``auto`` permission mode asks a separate
CLASSIFIER model whether each tool call is safe — a non-streaming request
carrying a portion of the transcript plus the pending action. Behind the
explainability proxy that request fails once the session is large: the proxy
forwards non-streaming calls under fixed total timeouts and surfaces a timeout
as an anonymous 500, which Claude Code renders as

    claude-opus-5[1m] is temporarily unavailable (server error), so auto mode
    cannot determine the safety of Bash right now. …

while the session's own chat keeps working, because chat streams. "Large" is
not something a session grows into: on the machine that reported it every
fleet agent's FIRST request was already ~137k tokens (system prompt, tool
schemas including every MCP connector's, skills, memory — the session
BASELINE), so the manager and its coders were refused from their first shell
command. Probes at a ~98k baseline passed. The fix is in the SDK
(:data:`SDK_ISSUE`); this module is what the CLI can do meanwhile:

- :func:`measure_baseline` — the first-turn size of the machine's recent
  sessions, read from their transcripts (``core.transcripts``), and whether
  any of them was refused;
- :func:`doctor_check` — the ``explainability auto-mode`` doctor line, present
  only when a fleet role runs ``auto`` behind a configured proxy;
- :func:`spawn_note` — the same warning on ``fleet spawn``'s receipt, when the
  evidence says the agent about to start will be refused;
- :func:`record_refusals` — at a session's Stop hook: the refusal text in its
  transcript puts the row in ``attention`` with one board line, so a manager
  does not grind for two hours before anyone is told.

Nothing here dials the proxy. A probe that PROVES the cut would have to send a
classifier-sized request through it — a full-size Opus call per doctor run —
and the deterministic signal is the baseline, which decides whether a
classifier call fits before the first tool call is ever made.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from aisquare.core import orchestrator, paths, transcripts
from aisquare.core.config import FleetSettings
from aisquare.core.store import store_session
from aisquare.models import CheckStatus, DoctorCheck
from aisquare.services import explainability as explainability_service
from aisquare.services import team as team_service

#: The SDK-side report: the proxy's fixed total timeouts on non-streaming
#: forwards (120 s on the recorded path, 30 s on the Claude Code bypass paths),
#: the anonymous 500 they surface as, and the asks.
SDK_ISSUE = "AISquare-Explainability-SDK#1144"
SDK_ISSUE_URL = "https://github.com/AISquare-Studio/AISquare-Explainability-SDK/issues/1144"

#: Above this first-turn size a classifier call has been MEASURED to fail
#: behind the proxy: the smallest refused session started at ~137k tokens and
#: the largest probe that passed at ~98k (aisquare-cli#150). A round number
#: between the two, on the side that warns.
REFUSED_ABOVE_TOKENS = 100_000

#: How many recent sessions the baseline is read from. Enough to see a change
#: of config dir (a connector added, an account switched), few enough that
#: doctor stays quick — each is one bounded read of a transcript's head.
SAMPLE_SESSIONS = 12

#: Refusals in a transcript's tail before a Stop hook calls it blocked. One
#: can be a real transient 5xx ("Wait a moment and then try this action
#: again"); the sessions that reported this had 5 to 153.
REFUSAL_THRESHOLD = 3

CHECK_NAME = "explainability auto-mode"
EVENT_KIND = "auto_mode_blocked"
_META_PREFIX = "auto-mode-blocked:"


@dataclass(frozen=True)
class Sample:
    """One recent session: how big its first request was, and whether it was refused."""

    session_id: str
    role: str
    started_at: datetime
    tokens: int | None
    """The first assistant turn's input size, or ``None`` when the transcript has none yet."""
    refusals: int


@dataclass(frozen=True)
class Baseline:
    """What this machine's recent transcripts say a session starts at (newest first)."""

    samples: tuple[Sample, ...] = ()

    @property
    def measured(self) -> tuple[int, ...]:
        return tuple(sample.tokens for sample in self.samples if sample.tokens is not None)

    @property
    def latest(self) -> int | None:
        """The newest measured session — the closest predictor for the next spawn."""
        return self.measured[0] if self.measured else None

    @property
    def largest(self) -> int | None:
        return max(self.measured) if self.measured else None

    @property
    def median(self) -> int | None:
        return int(statistics.median(self.measured)) if self.measured else None

    @property
    def refused_sessions(self) -> int:
        return sum(1 for sample in self.samples if sample.refusals >= 1)

    @property
    def predicted(self) -> int | None:
        """The size the next session will start at, as far as the evidence says."""
        return self.latest

    def describe(self) -> str:
        """``137k (newest of 8 sessions; median 137k, largest 141k)`` — or why not."""
        if not self.measured:
            return "not measurable yet (no session transcript on disk)"
        head = f"{_k(self.latest)} (newest of {len(self.measured)} session"
        head += "s" if len(self.measured) != 1 else ""
        if len(self.measured) > 1:
            head += f"; median {_k(self.median)}, largest {_k(self.largest)}"
        return head + ")"


def _k(tokens: int | None) -> str:
    return "?" if tokens is None else f"{tokens / 1000:.0f}k"


def measure_baseline(limit: int = SAMPLE_SESSIONS) -> Baseline:
    """Read the first-turn size (and refusals) of the newest ``limit`` sessions with a transcript.

    Creates nothing: without a database there are no sessions to read, and the
    answer is an empty baseline. A store that cannot be read is the same
    answer — the database line reports that.
    """
    if not paths.db_path().exists():
        return Baseline()
    try:
        with store_session() as store:
            sessions = [
                session
                for project in store.list_projects(all=True)
                for session in store.team_sessions(project.id)
                if session.transcript_path
            ]
    except Exception:
        return Baseline()
    sessions.sort(key=lambda session: session.started_at, reverse=True)
    samples: list[Sample] = []
    for session in sessions:
        path = Path(session.transcript_path or "")
        if not path.is_file():
            continue
        samples.append(
            Sample(
                session_id=session.id,
                role=session.role,
                started_at=session.started_at,
                tokens=transcripts.first_turn_tokens(path),
                refusals=transcripts.refusal_count(path),
            )
        )
        if len(samples) >= limit:
            break
    return Baseline(tuple(samples))


def auto_roles(config: FleetSettings | None = None) -> list[str]:
    """The fleet roles whose effective permission mode is ``auto`` — the classifier's users."""
    from aisquare.services import fleet as fleet_service  # lazy: fleet imports this module

    active = config or fleet_service.settings()
    roles = list(fleet_service.FLEET_ROLES) + [
        role for role in active.roles if role not in fleet_service.FLEET_ROLES
    ]
    return [
        role
        for role in roles
        if fleet_service.role_settings(role, active).permission_mode == "auto"
    ]


def exposed(baseline: Baseline) -> bool:
    """Whether the evidence says the next auto-mode session behind the proxy will be refused."""
    if baseline.refused_sessions:
        return True
    predicted = baseline.predicted
    return predicted is None or predicted >= REFUSED_ABOVE_TOKENS


def _remedy(roles: list[str]) -> str:
    role = roles[0] if roles else "coder"
    return (
        f"Until the proxy fix ({SDK_ISSUE_URL}), one of: a non-classifier mode for the fleet "
        f"roles — aisquare config set fleet.roles.{role}.permission_mode acceptEdits (per "
        "spawn: aisquare fleet spawn <role> --permission-mode acceptEdits); a lighter Claude "
        "config dir for the fleet's account (fewer MCP connectors — their schemas are most of "
        "the baseline); or run agents untraced: aisquare explainability disable"
    )


def doctor_check() -> DoctorCheck | None:
    """The ``explainability auto-mode`` line — only when a fleet role runs ``auto`` behind a proxy.

    Offline: config, the store, and the head of a few transcripts. ``None``
    when tracing is off or no role uses the classifier, because then there is
    nothing this line could warn about and doctor's output is read less for
    every row that says "n/a".
    """
    try:
        if not explainability_service.tracing_configured():
            return None
        roles = auto_roles()
    except Exception:  # a config that will not load is the config line's report
        return None
    if not roles:
        return None
    baseline = measure_baseline()
    who = ", ".join(roles)
    if baseline.refused_sessions:
        detail = (
            f"{who} run in auto mode behind the explainability proxy, and "
            f"{baseline.refused_sessions} of the last {len(baseline.samples)} sessions were "
            f"refused there ('cannot determine the safety of Bash'): the classifier's "
            f"non-streaming request fails at this machine's session baseline of "
            f"{baseline.describe()} ({SDK_ISSUE})"
        )
        return DoctorCheck(
            name=CHECK_NAME, status=CheckStatus.warn, detail=detail, fix=_remedy(roles)
        )
    if baseline.predicted is None:
        detail = (
            f"{who} run in auto mode behind the explainability proxy; the session baseline is "
            f"{baseline.describe()} — above ~{_k(REFUSED_ABOVE_TOKENS)} tokens the proxy has "
            f"been measured to fail the classifier's non-streaming request ({SDK_ISSUE})"
        )
        return DoctorCheck(
            name=CHECK_NAME, status=CheckStatus.warn, detail=detail, fix=_remedy(roles)
        )
    if baseline.predicted >= REFUSED_ABOVE_TOKENS:
        detail = (
            f"{who} run in auto mode behind the explainability proxy, and this machine's session "
            f"baseline is {baseline.describe()} — above the ~{_k(REFUSED_ABOVE_TOKENS)} tokens "
            f"at which the proxy has been measured to fail the classifier's non-streaming "
            f"request, so tool calls are refused from the first one ({SDK_ISSUE})"
        )
        return DoctorCheck(
            name=CHECK_NAME, status=CheckStatus.warn, detail=detail, fix=_remedy(roles)
        )
    detail = (
        f"{who} run in auto mode behind the explainability proxy; session baseline "
        f"{baseline.describe()}, under the ~{_k(REFUSED_ABOVE_TOKENS)} tokens at which the proxy "
        f"has been measured to fail the classifier call ({SDK_ISSUE}) — a session that grows "
        "past it is refused until /compact"
    )
    return DoctorCheck(name=CHECK_NAME, status=CheckStatus.ok, detail=detail)


def spawn_note(mode: str | None) -> str | None:
    """A receipt note for a spawn in ``auto`` mode that the evidence says will be refused.

    Silent unless the mode is ``auto``, tracing is configured, AND a recent
    session was refused or the measured baseline is above the line. A machine
    with no transcript yet gets no note — the doctor line carries that case —
    so a fresh install is not warned on every spawn about a size nobody has
    measured. Never raises: a warning is not a reason not to spawn.
    """
    if mode != "auto":
        return None
    try:
        if not explainability_service.tracing_configured():
            return None
        baseline = measure_baseline()
    except Exception:
        return None
    if not baseline.measured or not exposed(baseline):
        return None
    if baseline.refused_sessions:
        why = (
            f"{baseline.refused_sessions} of the last {len(baseline.samples)} sessions were "
            "refused ('cannot determine the safety of Bash')"
        )
    else:
        why = (
            f"the session baseline here is {baseline.describe()}, above the "
            f"~{_k(REFUSED_ABOVE_TOKENS)} tokens the proxy has been measured to fail at"
        )
    return (
        f"auto mode behind the explainability proxy: {why} — expect its tool calls to be refused "
        f"({SDK_ISSUE}); pass --permission-mode acceptEdits, or see `aisquare doctor` "
        f"({CHECK_NAME})"
    )


def record_refusals(session_id: str) -> int:
    """At a Stop: count the refusals in the session's transcript tail; say so once.

    On the first Stop at which :data:`REFUSAL_THRESHOLD` is reached the row goes
    to ``attention`` (the bell, 🔔 on the row — the agent cannot act and a human
    must change something) and ONE ``auto_mode_blocked`` line lands on the board
    naming the agent, the count and the way out. Never twice for a session: the
    refusals stay in the transcript, and a feed that repeats them every turn is
    a feed nobody reads. Never raises — this runs inside the agent's hook, where
    a failure may cost nothing but the notice.
    """
    try:
        if not orchestrator.team_enabled():
            return 0
        with store_session() as store:
            session = store.get_session(session_id)
            if session is None or not session.transcript_path:
                return 0
            count = transcripts.refusal_count(Path(session.transcript_path))
            if count < REFUSAL_THRESHOLD:
                return count
            key = _META_PREFIX + session.id
            if store.get_meta(key) is not None:
                return count
            store.set_meta(key, datetime.now(tz=UTC).isoformat())
            agent = store.fleet_agent_for_session(session.project_id, session.id)
            label = agent.label if agent is not None else (session.label or session.id[:8])
            role = agent.role if agent is not None else session.role
            store.mark_attention(session.id)
            team_service._emit(
                store,
                session.project_id,
                EVENT_KIND,
                _blocked_text(label, role, count, fleet=agent is not None),
                session_id=session.id,
            )
            return count
    except Exception:
        return 0


def _blocked_text(label: str, role: str, count: int, *, fleet: bool) -> str:
    head = (
        f"{label}: auto mode is refusing its tool calls behind the explainability proxy "
        f"({count} refusals: 'cannot determine the safety of Bash') — the classifier's "
        f"non-streaming request fails at this session's size ({SDK_ISSUE})"
    )
    if fleet:
        return (
            head + f" · set a non-classifier mode (aisquare config set fleet.roles.{role}."
            f"permission_mode acceptEdits) and `aisquare fleet restart {label}` (its session "
            "resumes), or run it untraced"
        )
    return head + " · restart it with --permission-mode acceptEdits, or untraced"
