"""Domain models shared by the CLI and service layers.

Storage is not implemented yet, but these models pin down the shapes the
services will accept and return, so the CLI layer can be wired against them
from day one.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

_SIGNAL_TEXT = re.compile(r"^([a-z0-9][a-z0-9._-]*): (\S+)(?: \(was (\S+)\))?$")
"""The anchored serialization of a signal event's text: ``name: value`` with
an optional `` (was prev)`` tail. Names and values are validated single
tokens at set time, so the decode is exact — never substring matching."""

Pool = Literal["user", "project"]
"""Where context lives: the global user pool or the current project pool."""


class ExportFormat(StrEnum):
    """Supported ``context export`` output formats."""

    md = "md"
    json = "json"


class RedactionLevel(StrEnum):
    """How aggressively captured data is scrubbed before storage."""

    off = "off"
    standard = "standard"
    strict = "strict"


class ContextEntry(BaseModel):
    """A single remembered fact, preference, or convention."""

    id: str
    pool: Pool
    project_id: str | None = None
    """Owning project for ``pool == "project"`` entries; ``None`` for the user pool."""
    text: str
    tags: list[str] = Field(default_factory=list)
    source: str = "manual"
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
    """Set when the entry is soft-deleted; tombstones survive so deletes can sync."""


class DataEnvelope(BaseModel):
    """Payload exchanged over the capture pipe between agents and aisquare."""

    kind: str
    scope: Pool
    payload: dict[str, Any]
    source: str
    ts: datetime


class AgentHookSite(BaseModel):
    """One config directory aisquare installed an agent's hooks into.

    Parallel agent installs (``CLAUDE_CONFIG_DIR=~/.claude2 claude``, run for
    separate rate limits) each keep their own ``settings.json``, so "is this
    agent connected?" has one answer *per directory*, not one per agent.
    """

    config_dir: Path
    hooks_installed: bool = False


class AgentInfo(BaseModel):
    """A coding agent aisquare knows how to integrate with."""

    name: str
    detected: bool = False
    config_paths: list[Path] = Field(default_factory=list)
    connected: bool = False
    sites: list[AgentHookSite] = Field(default_factory=list)
    """Every config dir this agent was connected in, with that dir's hook health."""


class ProjectInfo(BaseModel):
    """A project (workspace) tracked by aisquare."""

    id: str
    root: Path
    linked_repos: list[str] = Field(default_factory=list)


class InjectionRecord(BaseModel):
    """A record of the most recent context injection, surfaced by ``why``."""

    injected_at: datetime
    project_id: str | None = None
    user_count: int = 0
    project_count: int = 0
    entry_ids: list[str] = Field(default_factory=list)
    retrieved_chars: int = 0
    """Characters of CI-retrieved context injected this turn (the payload inside
    the frame, after the size cap); 0 when none was."""
    retrieved_items: list[str] = Field(default_factory=list)
    """The knowledge items the briefing carried, as ``item_id vN``, so ``why``
    can name what the agent was shown. Evidence ids stay on the metric row."""


class SetupReport(BaseModel):
    """What ``init`` did, for reporting back to the user."""

    home: Path
    already_initialized: bool
    project: ProjectInfo
    onboarded: int = 0
    notes: list[str] = Field(default_factory=list)


class ShippingStatus(BaseModel):
    """How the explainability client lane is doing, for ``status``.

    ``sent`` counts records handed to the SDK's durable inbox, which is where
    delivery guarantees live from that point on — not an acknowledgement from
    the gateway. ``dead`` is ours: records this CLI gave up on before the SDK
    ever saw them.
    """

    configured: bool
    queued: int
    sent: int
    dead: int
    reason: str


class StatusReport(BaseModel):
    """A snapshot of installation health for ``status``."""

    home: Path
    initialized: bool
    user_entries: int
    project_entries: int
    active_project: ProjectInfo
    project_count: int
    agents_detected: list[str] = Field(default_factory=list)
    agents_connected: list[str] = Field(default_factory=list)
    shipping: ShippingStatus | None = None
    """Present only once shipping is configured, or while something is still
    buffered from when it was. An install that declined the step reports
    exactly what it reported before — that is acceptance clause one."""


class CheckStatus(StrEnum):
    """Severity of a ``doctor`` check: healthy, degraded-but-usable, or broken."""

    ok = "ok"
    warn = "warn"
    fail = "fail"


class DoctorCheck(BaseModel):
    """One diagnostic check result from ``doctor``."""

    name: str
    status: CheckStatus
    detail: str
    fix: str | None = None  # actionable guidance when not ok


class PromptRecord(BaseModel):
    """A captured user prompt — how the user asked their agent, for replay."""

    id: str
    project_id: str | None = None
    text: str
    source: str = "claude-code"
    created_at: datetime


# --- Collective Intelligence: the vocabularies both layers share --------------
#
# The wire models in ``services.ci_contract`` and the per-turn ``metric`` row are
# spelled from ONE set of names, defined here because ``models`` may not import
# ``services``. Every value below that the server owns is fixed by hook contract
# v2 (``tests/fixtures/ci_contract/v2/schemas``); changing one is a contract
# revision, not an edit.

HookTrigger = Literal["session_start", "prompt_submit", "agent_request"]
"""Why the CLI called the server: the two hook events, plus ``agent_request``
for the MCP recall tool — which travels on the pull route, not the hook."""

BriefingStatus = Literal["served", "empty", "degraded", "unavailable"]
"""The server's own verdict on a call. ``unavailable`` is never a baseline."""

HookAction = Literal["inject", "noop"]
"""What the server asks the CLI to do — the only two actions v2 has."""

CacheStatus = Literal["hit", "miss", "bypass"]
"""The server's cache report for one briefing (the client keeps no response cache)."""

RunKind = Literal["live", "replay"]
"""Whether a turn was a developer's live session or a replay of one. Recorded
locally from the first v2 build; not yet a wire field (seam doc J12)."""

DeliverySource = Literal["descriptor", "override"]
"""Which document decided how a turn was delivered: the descriptor the server
published, or the staging override (``services/ci_override.py``) standing in for
it while the server's descriptor still says ``direct_api``. Rows the two produce
are never summed; a row from an override measures nothing."""


class ClientReason(StrEnum):
    """Why there is no usable server decision on a turn, or ``none``.

    A separate axis from the server's ``status``, recorded beside it on every
    row. The values fall into three groups that aggregates must never mix:

    - **baseline** — the client never asked, because the experiment is off or
      not configured. These rows ARE the control data.
    - **by design** — the experiment is on and the client chose not to call:
      the descriptor did not list the trigger, or there was nothing to send.
    - **failures** — the client tried and did not get a usable answer. Treated
      like the server's ``unavailable``: excluded by reason code, never counted
      as "CI had nothing to say".

    ``none`` means the server answered and this build understood it.
    """

    none = "none"

    # baseline
    disabled = "disabled"
    not_configured = "not_configured"
    no_run = "no_run"

    # by design
    trigger_not_in_descriptor = "trigger_not_in_descriptor"
    no_prompt = "no_prompt"
    no_session = "no_session"

    # failures
    descriptor_unavailable = "descriptor_unavailable"
    transport_error = "transport_error"
    deadline_exceeded = "deadline_exceeded"
    http_error = "http_error"
    malformed_body = "malformed_body"
    contract_mismatch = "contract_mismatch"
    schema_mismatch = "schema_mismatch"


BASELINE_REASONS = frozenset(
    {ClientReason.disabled, ClientReason.not_configured, ClientReason.no_run}
)
"""The client never asked. The stretch before an endpoint is live looks like this."""

BY_DESIGN_REASONS = frozenset(
    {ClientReason.trigger_not_in_descriptor, ClientReason.no_prompt, ClientReason.no_session}
)
"""The experiment was on and the client deliberately made no call."""

FAILURE_REASONS = (
    frozenset(ClientReason) - BASELINE_REASONS - BY_DESIGN_REASONS - {ClientReason.none}
)
"""The client tried. Never baseline, never "nothing to add"."""


class TurnMetric(BaseModel):
    """One turn, as the CI test bed measures it.

    Opened by ``UserPromptSubmit`` and closed by ``Stop``, so a row exists for
    every turn whether or not CI was consulted — which is what makes the
    period before the endpoint goes live a usable baseline rather than a gap.
    ``session_start`` and ``agent_request`` rows are closed at creation: they
    are calls, not turns, and must never be picked up by a later ``Stop``.

    The join keys the server ledger pairs on — ``run_id``, ``session_id``,
    ``trace_id``, ``query_id`` — are all here, so a bad retrieval can be walked
    from this row to the server log line that produced it.
    """

    trace_id: str
    project_id: str
    session_id: str | None = None
    """The raw agent session id, as the board uses it. The wire form is
    ``ses_`` + this; ``services.ci_contract.wire_session_id`` is the one place
    that conversion lives."""
    started_at: datetime
    ended_at: datetime | None = None
    wall_ms: int | None = None

    run_id: str | None = None
    run_kind: RunKind | None = None
    opaque_config_id: str | None = None
    """From the descriptor; recorded, never interpreted. It is the only handle
    the client ever holds on which configuration served it."""
    delivery_source: DeliverySource | None = None
    """``descriptor`` when the server's delivery list ruled this turn, ``override``
    when the staging override stood in for it; ``None`` when no descriptor was
    in hand. Loud on purpose: a consulted row under an override must never be
    mistaken for one the descriptor allowed."""
    trigger: HookTrigger | None = None

    client_reason: ClientReason = ClientReason.disabled
    """Why there is no server decision, or ``none``. Recorded beside the
    server's ``status`` because neither can carry the other: a timed-out call
    and a server answering ``empty`` both inject nothing, and without this
    column an endpoint failing every request reads as a clean baseline."""
    status: BriefingStatus | None = None
    action: HookAction | None = None

    query_id: str | None = None
    briefing_id: str | None = None
    config_fingerprint: str | None = None
    input_checkpoint: str | None = None
    resolved_scope_version: int | None = None

    round_trip_ms: int | None = None
    server_ms: int | None = None
    deadline_breached: bool | None = None
    token_count: int | None = None
    items_count: int | None = None
    cache_status: CacheStatus | None = None
    error_codes: list[str] = Field(default_factory=list)
    """``errors[].code`` from the response, verbatim. The catalog is the server's."""

    rendered_chars: int | None = None
    """Size of ``briefing.rendered_context`` as received, before the cap."""
    injected_chars: int | None = None
    """Payload characters actually put in front of the agent — the body inside
    the frame after the cap, never the frame's own boilerplate."""
    frame_version: str | None = None
    instruction_version: str | None = None
    redaction_level: RedactionLevel | None = None
    snapshot_ref: str | None = None
    snapshot_untracked_excluded: bool | None = None
    """``git stash create`` does not capture untracked files. Recorded rather
    than pretended: a replay from this snapshot is missing them."""

    tokens_in: int | None = None
    tokens_out: int | None = None
    tool_calls: int | None = None
    """Token and tool counts stay ``None`` until they come from real evidence
    (Explainability spans) — hook payloads do not carry them, and a fabricated
    number is worse than a null because it survives into a comparison."""


class MetricsSummary(BaseModel):
    """An aggregate over recorded turns, computed on read.

    The three reason groups are reported separately and never summed into one
    "degraded" figure: a baseline run with the experiment off must not read as
    a run where every call failed, and a run where every call failed must not
    read as one where the server had nothing to say.
    """

    turns: int
    project_id: str | None = None
    consulted: int = 0
    """Rows where the server answered and was understood (``client_reason == none``)."""
    baseline: int = 0
    """Rows where the client never asked: off, unconfigured, no run."""
    skipped: int = 0
    """Rows where the experiment was on and the client chose not to call."""
    failed: int = 0
    """Rows where the client tried and got no usable answer."""
    injected_turns: int = 0
    deadline_breaches: int = 0
    override_turns: int = 0
    """Rows delivered under the staging override (``delivery_source override``).
    They measure nothing and are kept out of the round-trip figures below;
    counted here so their presence is never invisible."""
    by_delivery_source: dict[str, int] = Field(default_factory=dict)
    by_reason: dict[str, int] = Field(default_factory=dict)
    by_status: dict[str, int] = Field(default_factory=dict)
    by_action: dict[str, int] = Field(default_factory=dict)
    by_trigger: dict[str, int] = Field(default_factory=dict)
    median_wall_ms: int | None = None
    median_round_trip_ms: int | None = None
    """Over consulted rows only. A row that never made a round trip has no
    round trip, and counting its zero makes a 300 ms endpoint read as instant."""
    p95_round_trip_ms: int | None = None
    turns_with_tokens: int = 0
    """How many rows carry token data. Reported so a zero reads as "not
    measured yet" rather than as "no tokens were used"."""


TaskStatus = Literal["todo", "doing", "review", "blocked", "done", "dropped"]
"""Lifecycle of a shared team task: todo → doing → review → done (or parked)."""


class TeamSession(BaseModel):
    """One live agent session on the orchestrator (id = the agent's session id)."""

    id: str
    project_id: str
    role: str = "unassigned"
    label: str | None = None
    focus: str | None = None
    """What this session says it is working on right now."""
    started_at: datetime
    last_seen_at: datetime
    ended_at: datetime | None = None
    cursor: int = 0
    """Highest team-event ``seq`` already shown to this session (delta position)."""
    state: str = "working"
    """Live activity: working (mid-turn), waiting (wants input) or attention."""
    transcript_path: str | None = None
    """The session's Claude Code transcript (JSONL), from hook payloads."""
    account: str | None = None
    """The agent config dir this session runs under (parallel-account installs)."""
    model: str | None = None
    """The model id the session reported at start (optional in hook payloads)."""
    effort: str | None = None
    """The effort level the session reported at start (optional in hook payloads)."""


class TeamTask(BaseModel):
    """A shared task on the orchestrator, idempotent on ``(project_id, key)``."""

    id: str
    project_id: str
    key: str
    """Idempotency key: re-adding the same key returns the existing task."""
    title: str
    detail: str | None = None
    status: TaskStatus = "todo"
    role: str | None = None
    """Suggested owner role (planner/coder/runner/...), advisory only."""
    needs: list[str] = Field(default_factory=list)
    """Task ids this one depends on; ``task next`` only hands out ready tasks."""
    claimed_by: str | None = None
    claim_expires_at: datetime | None = None
    """Claim lease; an expired lease makes the task claimable again."""
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime


class TeamEvent(BaseModel):
    """One update on the team pipe: who did what, addressed to whom.

    ``seq`` is the gapless stream cursor (SQLite rowid); deltas are "events with
    ``seq`` past my cursor, authored by others". Events render as
    :class:`DataEnvelope` payloads so the team stream stays pipe-shaped.
    """

    seq: int = 0
    id: str
    project_id: str
    session_id: str | None = None
    kind: str = "note"
    text: str
    task_id: str | None = None
    to_role: str | None = None
    created_at: datetime

    def as_envelope(self) -> DataEnvelope:
        """This event as a capture-pipe envelope (``kind`` namespaced ``team.*``).

        ``signal`` events additionally carry structured ``name``/``value``/
        ``prev``/``set_by`` payload fields (#23) so consumers key on fields,
        never on free text — decoded from the event's own anchored text
        format, which token validation at set time makes unambiguous.
        """
        payload: dict[str, object | None] = {
            "seq": self.seq,
            "id": self.id,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "text": self.text,
            "task_id": self.task_id,
            "to_role": self.to_role,
        }
        if self.kind == "signal":
            decoded = _SIGNAL_TEXT.match(self.text)
            if decoded:
                payload.update(
                    {
                        "name": decoded.group(1),
                        "value": decoded.group(2),
                        "prev": decoded.group(3),
                        "set_by": self.session_id,
                    }
                )
        return DataEnvelope(
            kind=f"team.{self.kind}",
            scope="project",
            payload=payload,
            source=self.session_id or "cli",
            ts=self.created_at,
        )


class Snapshot(BaseModel):
    """A packed snapshot of a project's codebase (Repomix full pack + skeleton)."""

    project_id: str
    head_sha: str | None = None
    generated_at: datetime
    pack_path: Path
    skeleton_path: Path
    index_path: Path
    token_count: int = 0
    skeleton_token_count: int = 0
    file_count: int = 0
    compressed: bool = False
    status: str = "ready"


class OnboardReport(BaseModel):
    """Outcome of ``project onboard``: seeded facts and the codebase snapshot."""

    seeded: list[ContextEntry] = Field(default_factory=list)
    snapshot: Snapshot | None = None


class AgentConnection(BaseModel):
    """Outcome of ``agents connect``: hook install + context ingested."""

    name: str
    hooks_installed: bool = False
    imported: int = 0
