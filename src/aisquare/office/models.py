"""The Office value types: the wire contract, and the internal records around it.

Two populations live here, and the difference is load-bearing.

**Wire models** subclass :class:`OfficeModel` and correspond one-for-one to a
schema in the pinned contract artifact (``tests/fixtures/office-contract/``,
revision 1.4, manifest digest
``06770d7f8d792d19214b09a6ddf6c3785241dadf182e3cfc151e3f28975d09a0``). Where
this module and a schema disagree, **the schema wins**: it is the artifact P00
froze and every other consumer — the browser, the mock, P16's compatibility
tests — validates against it, not against this file.
``tests/office/test_foundation_models.py`` asserts the agreement field by field
rather than trusting this sentence.

**Internal records** are frozen dataclasses. They never reach a browser, so
they carry things a wire model may not — a lifecycle generation, an auth scope,
bounded raw evidence. None of them carries a credential, a filesystem path, a
tmux target or a live connection, and the tests pin that too.

Serialization is an explicit allow-list, never ``model_dump()``. Every wire
model declares ``__wire_required__`` and ``__wire_optional__``, which are
exactly the schema's ``required`` list and the rest of its ``properties``;
:meth:`OfficeModel.to_wire` emits the required keys always (so an explicit
``null`` stays explicit) and an optional key only when it is set. That
distinction is not cosmetic: ``ServiceResult.data`` must be present and null
when there is no data, while ``Cost.by_tool[].avg_ms`` is typed ``number`` with
no null member, so emitting it as null would be invalid. A blanket
``model_dump()`` gets one of those two wrong whichever way it is configured.

Timestamps are timezone-aware UTC. A naive datetime is rejected at validation
rather than silently interpreted as local time — the whole contract's freshness
and since-seconds reasoning depends on the offset being real.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, ClassVar, Generic, Literal, TypeVar

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    model_validator,
)

from aisquare.models import (
    FleetAgentStatus,
    ProjectInfo,
    TeamEvent,
    TeamSession,
    TeamTask,
    TurnMetric,
)

CONTRACT_REVISION = "1.4"
"""The revision this foundation is written against (``backend-v1.4.md`` §1)."""

CONTRACT_ARTIFACT_SHA256 = "06770d7f8d792d19214b09a6ddf6c3785241dadf182e3cfc151e3f28975d09a0"
"""``manifest.json``'s ``digest`` for the frozen pack P00 accepted.

Pinned as a constant and vendored under ``tests/fixtures/office-contract/``
deliberately: a test that fetched a moving branch would go green against a
contract nobody agreed to. This is also the value ``Capabilities``
``artifact_revision`` reports (``backend-v1.4.md`` §3.5).
"""

CONTRACT_ARTIFACT_FILE_COUNT = 36
"""Files covered by :data:`CONTRACT_ARTIFACT_SHA256`, per ``manifest.json``."""


# --------------------------------------------------------------------------
# Scalars
# --------------------------------------------------------------------------


def _require_aware(value: datetime) -> datetime:
    """Reject a naive datetime rather than guess which zone it meant."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("timestamp must be timezone-aware (UTC); got a naive datetime")
    return value


def _iso(value: datetime) -> str:
    """RFC3339 with an offset, matching the contract's ``Timestamp`` pattern."""
    return value.isoformat()


Timestamp = Annotated[
    datetime,
    AfterValidator(_require_aware),
    PlainSerializer(_iso, return_type=str, when_used="json"),
]
"""``snapshot.json#/$defs/Timestamp``: ISO 8601 carrying a timezone offset."""

Id = Annotated[str, StringConstraints(min_length=1)]
"""``snapshot.json#/$defs/Id``: opaque, stable, never empty."""

Role = Annotated[str, StringConstraints(min_length=1)]
"""Open set (``manager``, ``coder``, …): the board's roles are not ours to fix."""

TaskStatusName = Annotated[str, StringConstraints(min_length=1)]
"""Open set, for the same reason as :data:`Role`."""

PaneId = Annotated[str, StringConstraints(pattern=r"^%\d+$")]
"""A tmux pane id as the contract spells it (``%14``)."""

AbsolutePath = Annotated[str, StringConstraints(pattern=r"^/")]
"""``Project.root``. The one place the contract carries a path at all."""

PromptIdStr = Annotated[str, StringConstraints(min_length=1, max_length=128)]
IdempotencyKeyStr = Annotated[
    str, StringConstraints(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_.:-]{1,255}$")
]
RequestIdStr = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
]
CursorStr = Annotated[str, StringConstraints(max_length=512, pattern=r"^[A-Za-z0-9_-]{1,512}$")]
RemoteId = Annotated[str, StringConstraints(min_length=1, max_length=128)]

OfficeOrigin = Literal["fleet", "self"]
OfficeState = Literal["working", "waiting", "attention", "idle", "ended", "starting"]
OfficeActivity = Literal["planning", "building", "testing", "reviewing", "idle"]
QuestionKind = Literal["permission", "plan", "ask", "form", "continue", "question", "note"]
ModelFamily = Literal["fable", "opus", "sonnet", "haiku", "other"]
DetectedBy = Literal["hook", "frame", "both"]
Pause = Literal["none", "paused", "rate_limited", "compacting"]
SubState = Literal[
    "none",
    "tool",
    "subagents",
    "compacting",
    "retrying",
    "limited",
    "interrupted",
    "dialog",
    "auth",
]
PermissionMode = Literal[
    "default", "acceptEdits", "plan", "auto", "bypassPermissions", "dontAsk", "unknown"
]
PaneHealth = Literal["live", "dead", "gone", "unknown"]
EndedReason = Literal["exit", "clear", "logout", "crash", "killed", "pruned", "other"]
TestsLast = Literal["pass", "fail", "none"]
OptionConsequence = Literal[
    "none", "remembers", "session", "mode_auto", "mode_bypass", "mode_accept_edits"
]
HistoryKind = Literal["prompt", "answer", "tool", "note", "feedback", "state", "error"]
HistoryBy = Literal["agent", "user", "system"]
FileStatus = Literal["added", "modified", "deleted", "renamed"]
Delivered = Literal["typed", "board", "spawned", "none"]

ServiceStatus = Literal[
    "ok", "unconfigured", "unauthorized", "forbidden", "unsupported", "unavailable", "partial"
]
"""Availability of the service that answered, and nothing else.

A domain state — a run still ``processing``, an agent ``unjoined``, an insight
with no recorded injection — is a field of the typed model. Adding one here is
how a UI ends up unable to tell "the service is down" from "the answer is no".
"""

ServiceErrorCode = Literal[
    "service_unconfigured",
    "service_unavailable",
    "unauthorized",
    "forbidden",
    "unsupported_capability",
    "binding_required",
    "upstream_error",
    "upstream_invalid",
    "timeout",
    "rate_limited",
    "outcome_unknown",
    "internal",
]

OperationStatus = Literal["queued", "running", "succeeded", "failed", "outcome_unknown"]
"""``outcome_unknown`` is neither success nor failure, and is never auto-retried."""

OperationKind = Literal[
    "prompt.resolve",
    "prompt.resolve_all",
    "terminal.keys",
    "terminal.input",
    "agent.tell",
    "agent.interrupt",
    "agent.stop",
    "agent.spawn",
    "agent.respawn",
    "learning.teach",
]
"""Exactly the changed 1.4 mutations. A retained legacy route produces no Operation."""

LegacyActionKind = Literal[
    "agent.note",
    "agent.whip",
    "agent.gift",
    "agent.rename",
    "agent.pause",
    "agent.resume",
    "task.assign",
    "task.status",
    "task.create",
    "project.freeze",
]
"""The §4.3 mutations the contract deliberately kept unchanged.

They never appear in a wire ``Operation`` — :data:`OperationKind` is the wire
enum and stays exactly the schema's ten. They exist here because SHARED.md
still requires that *no mutation reaches the coordinator without a resolved
internal key*, so P06 must be able to reserve one, and a reservation needs a
finite kind. Keeping the two enums separate is what stops a legacy note from
being reported to a browser as an Operation it can poll.
"""

ActionKind = OperationKind | LegacyActionKind
"""What :class:`ActionSpec` may carry: a changed mutation or a retained one."""

ContractRevision = Literal["1.3", "1.4"]
FeatureId = Literal[
    "local.read",
    "local.actions",
    "terminal.read",
    "terminal.input",
    "prompt.resolve",
    "operations",
    "team_os.views",
    "platform.runs",
    "platform.reasoning",
    "platform.learning",
    "platform.teach",
]
FEATURE_IDS: tuple[FeatureId, ...] = (
    "local.read",
    "local.actions",
    "terminal.read",
    "terminal.input",
    "prompt.resolve",
    "operations",
    "team_os.views",
    "platform.runs",
    "platform.reasoning",
    "platform.learning",
    "platform.teach",
)
"""Every feature id, each of which must appear in ``Capabilities.features`` exactly
once — omitting one is a contract violation, not a way to say "off"."""

ServiceId = Literal["cli", "team_os", "explainability", "praxis"]
SERVICE_IDS: tuple[ServiceId, ...] = ("cli", "team_os", "explainability", "praxis")

ProviderObservation = Literal["hooks_and_pane", "pane_only", "unsupported"]

ApiErrorCode = Literal[
    "unauthorized",
    "no_such_agent",
    "no_such_project",
    "no_such_task",
    "not_fleet",
    "no_pane",
    "not_waiting",
    "fleet_unavailable",
    "team_disabled",
    "invalid",
    "timeout",
    "internal",
    "wrong_kind",
    "not_working",
    "too_large",
    "not_ended",
    "no_gui_answer",
    "mode_change",
    "contract_revision_required",
    "stale_prompt",
    "idempotency_conflict",
    "operation_not_found",
    "unsupported_capability",
    "target_not_assignable",
    "binding_required",
    "service_unconfigured",
    "service_unavailable",
    "outcome_unknown",
]
"""The local ``error.json`` body. Distinct from :class:`ServiceError`, which lives
*inside* a 200 ``ServiceResult``; ``backend-v1.4.md`` §7 forbids mixing them."""

# Remote domain vocabularies (explainability.json, praxis.json, team-os.json).
RunJoinState = Literal["verified", "unjoined"]
RunState = Literal["queued", "running", "completed", "failed", "cancelled", "unknown"]
DetailState = Literal["processing", "ready", "not_available"]
PolicyStatus = Literal["passed", "failed", "skipped", "error", "unknown"]
InsightStatus = Literal["draft", "active", "retired", "unknown"]
TeachIntent = Literal["correction", "preference", "convention", "warning"]
TeachEvidenceKind = Literal["agent", "task", "run", "insight"]
ProvenanceRelation = Literal["derived_from", "supersedes", "supports", "contradicts", "references"]
ProvenanceNodeKind = Literal["insight", "run", "injection", "note"]
TeamOSSource = Literal["team_os"]
ViewName = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
]


# --------------------------------------------------------------------------
# The wire base
# --------------------------------------------------------------------------


def _wire_value(value: object) -> object:
    """One value, as the wire wants it — recursing through the allow-lists.

    Recursion matters: a nested model dumped by ``model_dump`` would emit its
    unset optional keys as ``null``, and several nested schemas (``Cost``'s
    ``by_tool`` rows, ``Stats``) type those keys without a null member, so the
    result would not validate. Going through :meth:`OfficeModel.to_wire` at
    every level keeps each model's own required/optional split.
    """
    if isinstance(value, OfficeModel):
        return value.to_wire()
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, (list, tuple)):
        return [_wire_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _wire_value(item) for key, item in value.items()}
    return value


class OfficeModel(BaseModel):
    """A strict, frozen, allow-list-serialized contract model.

    ``extra="forbid"`` because the schemas are strict: an undocumented property
    is rejected rather than ignored, so a security or semantic invariant can
    never be inferred from an unknown field (``backend-v1.4.md`` §5.6).

    ``frozen=True`` because these values are shared — one immutable
    :class:`Snapshot` is handed to every SSE subscriber at once — and because a
    projection that callers may edit in place is not a projection.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    __wire_required__: ClassVar[tuple[str, ...]] = ()
    """Wire keys always emitted, exactly the schema's ``required`` list.

    Emitted even when the value is ``None``, which is what makes an explicit
    null explicit: ``ServiceResult.data`` is always a present key.
    """

    __wire_optional__: ClassVar[tuple[str, ...]] = ()
    """Wire keys emitted only when set — the schema's remaining ``properties``."""

    __wire_alias__: ClassVar[Mapping[str, str]] = {}
    """Wire key → attribute name, for the keys Python cannot spell (``in``)."""

    def to_wire(self) -> dict[str, object]:
        """This model as the JSON object the contract describes.

        The only supported serialization. ``model_dump()`` is deliberately not
        wrapped or re-exported: later routes select fields explicitly, and a
        model that grows an internal attribute must not start leaking it
        because something called the generic dumper.
        """
        out: dict[str, object] = {}
        for name in self.__wire_required__:
            out[name] = _wire_value(getattr(self, self.__wire_alias__.get(name, name)))
        for name in self.__wire_optional__:
            value = getattr(self, self.__wire_alias__.get(name, name))
            if value is not None:
                out[name] = _wire_value(value)
        return out

    @classmethod
    def wire_fields(cls) -> frozenset[str]:
        """Every key :meth:`to_wire` may emit."""
        return frozenset(cls.__wire_required__) | frozenset(cls.__wire_optional__)


# --------------------------------------------------------------------------
# snapshot.json — the canonical local read
# --------------------------------------------------------------------------


class Project(OfficeModel):
    """``snapshot.json#/$defs/Project``."""

    __wire_required__ = ("id", "name", "root")
    __wire_optional__ = ("codename", "frozen")

    id: Id
    name: Annotated[str, StringConstraints(min_length=1)]
    root: AbsolutePath
    codename: str | None = None
    frozen: bool | None = None


class QuestionOption(OfficeModel):
    """One answerable choice, as the pane shows it.

    Never synthesised. An invented label is a button that types something the
    user did not read, which is why ``Question.options`` is absent rather than
    guessed whenever the back-end cannot capture the labels.
    """

    __wire_required__ = ("key", "label")
    __wire_optional__ = ("description", "consequence", "default")

    key: Annotated[str, StringConstraints(min_length=1)]
    label: str
    description: str | None = None
    consequence: OptionConsequence | None = None
    default: bool | None = None


class AskQuestion(OfficeModel):
    """One tab of an ``AskUserQuestion`` prompt."""

    __wire_required__ = ("header", "question", "options", "multi")

    header: Annotated[str, StringConstraints(max_length=12)]
    question: str
    options: Annotated[tuple[QuestionOption, ...], Field(min_length=2, max_length=5)]
    multi: bool


class Question(OfficeModel):
    """What an agent is waiting on. Present iff its state is waiting or attention."""

    __wire_required__ = ("kind", "text", "detected_by")
    __wire_optional__ = (
        "tool",
        "summary",
        "diff",
        "options",
        "questions",
        "plan_md",
        "plan_path",
        "deny_reason",
        "asked_user",
        "context",
        "raw",
        "prompt_id",
    )

    kind: QuestionKind
    text: Annotated[str, StringConstraints(max_length=400)]
    detected_by: DetectedBy
    tool: str | None = None
    summary: Annotated[str, StringConstraints(max_length=200)] | None = None
    diff: str | None = None
    options: Annotated[tuple[QuestionOption, ...], Field(max_length=8)] | None = None
    """Optional everywhere and pane-gated: a self session has no pane to read
    labels from, so its permission prompt carries none and the GUI must not
    offer answer controls for it."""
    questions: Annotated[tuple[AskQuestion, ...], Field(min_length=1, max_length=4)] | None = None
    plan_md: str | None = None
    plan_path: str | None = None
    deny_reason: bool | None = None
    asked_user: bool | None = None
    context: str | None = None
    raw: str | None = None
    prompt_id: PromptIdStr | None = None
    """Revision 1.4, additive: one observed prompt lifecycle. The same text after
    the pane cleared is a new lifecycle with a new id, so an answer written for
    the old one cannot land on the new one."""


class Turn(OfficeModel):
    """``snapshot.json#/$defs/Turn`` — where the agent is inside its current turn."""

    __wire_required__ = ("n",)
    __wire_optional__ = ("started_at", "tool", "tool_arg")

    n: Annotated[int, Field(ge=0)]
    started_at: Timestamp | None = None
    tool: str | None = None
    tool_arg: Annotated[str, StringConstraints(max_length=120)] | None = None


class Worktree(OfficeModel):
    """``snapshot.json#/$defs/Worktree``."""

    __wire_required__ = ("path", "branch", "dirty", "ahead")

    path: str
    branch: str
    dirty: Annotated[int, Field(ge=0)]
    ahead: Annotated[int, Field(ge=0)]


class Stats(OfficeModel):
    """Best-effort worktree statistics. Both fields optional; null is never guessed."""

    __wire_optional__ = ("files_changed", "tests_last")

    files_changed: Annotated[int, Field(ge=0)] | None = None
    tests_last: TestsLast | None = None


class TokenCounts(OfficeModel):
    """``snapshot.json#/$defs/TokenCounts``.

    ``in`` is a Python keyword, so the attribute is ``tokens_in`` and
    :attr:`OfficeModel.__wire_alias__` maps it back for serialization.
    """

    __wire_required__ = ("in", "out", "cache_write", "cache_read")
    __wire_alias__ = {"in": "tokens_in"}

    tokens_in: Annotated[int, Field(ge=0, alias="in")]
    out: Annotated[int, Field(ge=0)]
    cache_write: Annotated[int, Field(ge=0)]
    cache_read: Annotated[int, Field(ge=0)]


class ConcentrationFlag(OfficeModel):
    __wire_required__ = ("kind", "detail")

    kind: Literal["concentration"] = "concentration"
    detail: str


class OversizedResponseFlag(OfficeModel):
    __wire_required__ = ("kind", "detail", "tool", "tokens")

    kind: Literal["oversized_response"] = "oversized_response"
    detail: str
    tool: str
    tokens: Annotated[int, Field(ge=0)]


class SlowToolFlag(OfficeModel):
    __wire_required__ = ("kind", "detail", "tool", "avg_ms")

    kind: Literal["slow_tool"] = "slow_tool"
    detail: str
    tool: str
    avg_ms: Annotated[float, Field(ge=0)]


class RepeatedCallFlag(OfficeModel):
    __wire_required__ = ("kind", "detail", "tool", "count")

    kind: Literal["repeated_call"] = "repeated_call"
    detail: str
    tool: str
    count: Annotated[int, Field(ge=3)]


class LowCacheFlag(OfficeModel):
    __wire_required__ = ("kind", "detail", "cache_hit")

    kind: Literal["low_cache"] = "low_cache"
    detail: str
    cache_hit: Annotated[float, Field(ge=0, le=1)]


class BurnFlag(OfficeModel):
    __wire_required__ = ("kind", "detail", "rate_usd_per_min")

    kind: Literal["burn"] = "burn"
    detail: str
    rate_usd_per_min: Annotated[float, Field(ge=0)]


CostFlag = Annotated[
    ConcentrationFlag
    | OversizedResponseFlag
    | SlowToolFlag
    | RepeatedCallFlag
    | LowCacheFlag
    | BurnFlag,
    Field(discriminator="kind"),
]
"""``snapshot.json#/$defs/CostFlag`` — a cost outlier, discriminated by ``kind``."""


class CostByTool(OfficeModel):
    """One row of ``Cost.by_tool``. ``avg_ms`` has no null member in the schema,
    so it is omitted rather than emitted as null when unknown."""

    __wire_required__ = ("tool", "calls", "resp_tokens")
    __wire_optional__ = ("avg_ms",)

    tool: str
    calls: Annotated[int, Field(ge=0)]
    resp_tokens: Annotated[int, Field(ge=0)]
    avg_ms: Annotated[float, Field(ge=0)] | None = None


class Cost(OfficeModel):
    """One agent's token meter (``snapshot.json#/$defs/Cost``)."""

    __wire_required__ = (
        "session",
        "session_usd",
        "turn",
        "turn_usd",
        "calls",
        "rate_usd_per_min",
        "cache_hit",
        "by_tool",
        "share_today",
        "price_known",
        "flags",
        "updated_at",
    )

    session: TokenCounts
    session_usd: Annotated[float, Field(ge=0)]
    turn: TokenCounts
    turn_usd: Annotated[float, Field(ge=0)]
    calls: Annotated[int, Field(ge=0)]
    rate_usd_per_min: Annotated[float, Field(ge=0)]
    cache_hit: Annotated[float, Field(ge=0, le=1)]
    by_tool: Annotated[tuple[CostByTool, ...], Field(max_length=6)]
    share_today: Annotated[float, Field(ge=0, le=1)]
    price_known: bool
    """False means the price of at least one call is unknown. The USD figures are
    then a lower bound, and a client must not present them as complete."""
    flags: tuple[CostFlag, ...]
    updated_at: Timestamp


class CostByModel(OfficeModel):
    __wire_required__ = ("model", "family", "calls", "usd")

    model: str
    family: ModelFamily
    calls: Annotated[int, Field(ge=0)]
    usd: Annotated[float, Field(ge=0)]


class CostByProject(OfficeModel):
    __wire_required__ = ("project_id", "usd")

    project_id: str
    usd: Annotated[float, Field(ge=0)]


class CostByAgent(OfficeModel):
    __wire_required__ = ("agent_id", "usd", "share")

    agent_id: str
    usd: Annotated[float, Field(ge=0)]
    share: Annotated[float, Field(ge=0, le=1)]


class OfficeCost(OfficeModel):
    """The office-wide token meter (``snapshot.json#/$defs/OfficeCost``).

    ``budget_usd`` is nullable because no budget and a zero budget are different
    facts, and local transcript cost is never summed with gateway cost.
    """

    __wire_required__ = (
        "today_usd",
        "today",
        "by_model",
        "by_project",
        "by_agent",
        "rate_usd_per_min",
        "updated_at",
    )
    __wire_optional__ = ("budget_usd",)

    today_usd: Annotated[float, Field(ge=0)]
    today: TokenCounts
    by_model: tuple[CostByModel, ...]
    by_project: tuple[CostByProject, ...]
    by_agent: tuple[CostByAgent, ...]
    rate_usd_per_min: Annotated[float, Field(ge=0)]
    budget_usd: Annotated[float, Field(ge=0)] | None = None
    updated_at: Timestamp


class Agent(OfficeModel):
    """One session (``snapshot.json#/$defs/Agent``).

    The four v1.3 fields ``sub``, ``subagents``, ``permission_mode`` and
    ``health`` are required because each has a safe default the back-end can
    always emit — and ``"unknown"`` is that default, not a claim of evidence.
    Fields that *do* depend on evidence stay optional.
    """

    __wire_required__ = (
        "id",
        "label",
        "project_id",
        "project_now",
        "projects",
        "role",
        "origin",
        "state",
        "activity",
        "since_s",
        "morale",
        "model_family",
        "up_s",
        "output_tail",
        "last_seen_at",
        "sub",
        "subagents",
        "permission_mode",
        "health",
    )
    __wire_optional__ = (
        "doing",
        "question",
        "model",
        "tree",
        "pane_id",
        "task_id",
        "pause",
        "pane_alive",
        "exit_status",
        "turn",
        "last_error",
        "worktree",
        "stats",
        "cost",
        "sub_detail",
        "context_pct",
        "limited_until",
        "auto_resume",
        "ended_at",
        "ended_reason",
        "tty",
    )

    id: Id
    label: Annotated[str, StringConstraints(min_length=1)]
    project_id: Id
    project_now: Id
    projects: Annotated[tuple[Id, ...], Field(min_length=1)]
    role: Role
    origin: OfficeOrigin
    state: OfficeState
    activity: OfficeActivity
    since_s: Annotated[int, Field(ge=0)]
    """Seconds in the current waiting/attention state, else 0. Two derivations,
    deliberately: attention counts from the transition into attention and a
    re-notification does not reset it; waiting counts from the stop hook."""
    morale: Annotated[int, Field(ge=0, le=100)]
    model_family: ModelFamily
    up_s: Annotated[int, Field(ge=0)]
    output_tail: Annotated[tuple[str, ...], Field(max_length=6)]
    last_seen_at: Timestamp
    sub: SubState
    subagents: Annotated[int, Field(ge=0)]
    permission_mode: PermissionMode
    health: PaneHealth
    doing: str | None = None
    question: Question | None = None
    model: str | None = None
    tree: str | None = None
    pane_id: PaneId | None = None
    task_id: Id | None = None
    pause: Pause | None = None
    pane_alive: bool | None = None
    exit_status: int | None = None
    turn: Turn | None = None
    last_error: Annotated[str, StringConstraints(max_length=200)] | None = None
    worktree: Worktree | None = None
    stats: Stats | None = None
    cost: Cost | None = None
    sub_detail: str | None = None
    context_pct: Annotated[float, Field(ge=0, le=100)] | None = None
    limited_until: Timestamp | None = None
    auto_resume: bool | None = None
    ended_at: Timestamp | None = None
    ended_reason: EndedReason | None = None
    tty: str | None = None


class Task(OfficeModel):
    """``snapshot.json#/$defs/Task``."""

    __wire_required__ = ("id", "project_id", "title", "status", "needs")
    __wire_optional__ = ("claimed_by", "role")

    id: Id
    project_id: Id
    title: Annotated[str, StringConstraints(min_length=1)]
    status: TaskStatusName
    needs: tuple[Id, ...]
    claimed_by: Id | None = None
    role: str | None = None


class HistoryItem(OfficeModel):
    """``snapshot.json#/$defs/HistoryItem``."""

    __wire_required__ = ("at", "kind", "text")
    __wire_optional__ = ("by",)

    at: Timestamp
    kind: HistoryKind
    text: str
    by: HistoryBy | None = None


class DiffFile(OfficeModel):
    __wire_required__ = ("path", "status", "additions", "deletions")

    path: str
    status: FileStatus
    additions: Annotated[int, Field(ge=0)]
    deletions: Annotated[int, Field(ge=0)]


class Diff(OfficeModel):
    """``snapshot.json#/$defs/Diff`` — what a fleet agent changed since its base."""

    __wire_required__ = ("agent_id", "base", "files", "truncated")
    __wire_optional__ = ("patch",)

    agent_id: str
    base: str
    files: tuple[DiffFile, ...]
    truncated: bool
    patch: str | None = None


class Snapshot(OfficeModel):
    """The whole office at one instant: the body of ``GET /api/snapshot`` and the
    first message on the stream.

    ``seq`` is the board event sequence and doubles as the public SSE id, so
    several real events may share one id. Reconnect always starts with a
    complete snapshot rather than durable replay.
    """

    __wire_required__ = ("v", "seq", "taken_at", "stale", "projects", "agents", "queue", "tasks")
    __wire_optional__ = ("cost",)

    v: Literal[1] = 1
    seq: Annotated[int, Field(ge=0)]
    taken_at: Timestamp
    stale: bool
    """True when the last poll failed and this is the previous frame."""
    projects: tuple[Project, ...]
    agents: tuple[Agent, ...]
    queue: tuple[Id, ...]
    """Agent ids waiting for the user, front first: attention (longest first),
    then waiting (longest first). An ended row is never here."""
    tasks: tuple[Task, ...]
    cost: OfficeCost | None = None


class Plan(OfficeModel):
    """``plan.json`` — one project's whole board, plus the latest plan note."""

    __wire_required__ = ("project_id", "tasks")
    __wire_optional__ = ("note",)

    project_id: Id
    tasks: tuple[Task, ...]
    note: str | None = None


class AgentHistory(OfficeModel):
    """``history.json``."""

    __wire_required__ = ("items",)

    items: tuple[HistoryItem, ...]


class AgentOutput(OfficeModel):
    """``output.json`` — ANSI-stripped pane lines, or a self session's board notes."""

    __wire_required__ = ("lines", "source")

    lines: Annotated[tuple[str, ...], Field(max_length=200)]
    source: Literal["pane", "board"]


# --------------------------------------------------------------------------
# receipt.json / error.json
# --------------------------------------------------------------------------


class Receipt(OfficeModel):
    """What an action actually did (``receipt.json``).

    ``closed`` is a statement about the *pane*, never a claim that the agent
    accepted the answer: true means the dialog was observed to leave within one
    second, false that it was observed still there, and null that it was not
    observed either way — the honest answer for a board delivery or a self
    session.
    """

    __wire_required__ = ("delivered", "detail")
    __wire_optional__ = ("agent_id", "keys", "at", "closed")

    delivered: Delivered
    detail: Annotated[str, StringConstraints(min_length=1)]
    agent_id: Id | None = None
    keys: Annotated[tuple[str, ...], Field(max_length=64)] | None = None
    """Exactly the tmux key names sent, in order — the plan the back-end owns."""
    at: Timestamp | None = None
    closed: bool | None = None


class ApiError(OfficeModel):
    """The body of a non-2xx local ``/api`` response (``error.json``).

    Never to be confused with :class:`ServiceError`, which lives inside a *200*
    ``ServiceResult``. A local authorisation failure is never smuggled into a
    200, and an upstream failure never becomes a local 5xx.
    """

    __wire_required__ = ("error",)
    __wire_optional__ = ("detail",)

    error: ApiErrorCode
    detail: str | None = None


# --------------------------------------------------------------------------
# service.json — the 1.4 envelopes
# --------------------------------------------------------------------------

T = TypeVar("T")


class ServiceError(OfficeModel):
    """Why a service read produced no data (``service.json#/$defs/ServiceError``).

    ``detail`` is one sanitised, bounded sentence: never a stack trace, a
    credential, a filesystem path, a raw upstream body or a key-bearing URL.
    The adapters do the sanitising; this type only guarantees the bound.
    """

    __wire_required__ = ("code", "detail", "retryable")

    code: ServiceErrorCode
    detail: Annotated[str, StringConstraints(max_length=500)]
    retryable: bool
    """Whether an identical *read* may be repeated. An uncertain mutation
    outcome is never retryable — see :class:`Operation`."""


class PageCoverage(OfficeModel):
    """The fixed coverage object. Every field is nullable because an upstream
    that establishes none of them must not be made to look precise."""

    __wire_required__ = (
        "reported_total",
        "total_is_exact",
        "reachable_scope_count",
        "failed_scope_count",
        "omitted_scope_count",
    )

    reported_total: Annotated[int, Field(ge=0, le=1_000_000)] | None
    """What the upstream itself reported. Null when it reported none — never a
    count of whatever happened to be fetched."""
    total_is_exact: bool | None
    reachable_scope_count: Annotated[int, Field(ge=0, le=10_000)] | None
    failed_scope_count: Annotated[int, Field(ge=0, le=10_000)] | None
    omitted_scope_count: Annotated[int, Field(ge=0, le=10_000)] | None


class Page(OfficeModel, Generic[T]):
    """``Page<T>`` — bounded, honest pagination.

    ``partial`` means a scope *failed*, so the page is known incomplete. That is
    not the same as having a ``next_cursor``, which only means more rows follow.
    """

    __wire_required__ = ("items", "next_cursor", "partial", "coverage")

    items: Annotated[tuple[T, ...], Field(max_length=200)]
    next_cursor: CursorStr | None
    """Opaque and bound to the query, profile and workspace that produced it. A
    cursor can continue a page; it can never widen or change its scope."""
    partial: bool
    coverage: PageCoverage | None


class ServiceResult(OfficeModel, Generic[T]):
    """Every read that leaves the local process (``service.json``).

    Four invariants the JSON Schema cannot state are enforced here, because the
    whole point of the envelope is that a client can trust them:

    1. ``ok`` carries non-null data and a null error.
    2. ``error`` is null exactly when the status is ``ok``.
    3. ``unauthorized``/``forbidden``/``unconfigured`` carry null data and
       ``stale`` false — losing authorisation **drops** the cached body, so a
       revoked binding can never keep painting a workspace's data.
    4. ``stale`` is only possible with ``ok`` or ``partial``, and null data
       means a null ``observed_at``.
    """

    __wire_required__ = ("status", "data", "stale", "observed_at", "last_success_at", "error")

    status: ServiceStatus
    data: T | None
    stale: bool = False
    observed_at: Timestamp | None = None
    last_success_at: Timestamp | None = None
    error: ServiceError | None = None

    @model_validator(mode="after")
    def _check_envelope(self) -> ServiceResult[T]:
        if self.status == "ok":
            if self.data is None:
                raise ValueError("ServiceResult status 'ok' requires non-null data")
            if self.error is not None:
                raise ValueError("ServiceResult status 'ok' requires a null error")
        elif self.error is None:
            raise ValueError(f"ServiceResult status {self.status!r} requires an error")
        if self.status in ("unauthorized", "forbidden", "unconfigured"):
            if self.data is not None:
                raise ValueError(
                    f"ServiceResult status {self.status!r} must drop cached data, not display it"
                )
            if self.stale:
                raise ValueError(f"ServiceResult status {self.status!r} cannot be stale")
        if self.stale and self.status not in ("ok", "partial"):
            raise ValueError(f"ServiceResult status {self.status!r} cannot carry stale data")
        if self.data is None and self.observed_at is not None:
            raise ValueError("ServiceResult with null data must have a null observed_at")
        return self


def service_ok(
    data: T, *, observed_at: datetime, last_success_at: datetime | None = None
) -> ServiceResult[T]:
    """A successful read. ``last_success_at`` defaults to this observation."""
    return ServiceResult[T](
        status="ok",
        data=data,
        stale=False,
        observed_at=observed_at,
        last_success_at=last_success_at if last_success_at is not None else observed_at,
        error=None,
    )


def service_failed(
    status: ServiceStatus,
    error: ServiceError,
    *,
    last_success_at: datetime | None = None,
) -> ServiceResult[T]:
    """A read that produced no data. Never carries a cached body: an unavailable
    service may legitimately show stale data, but this helper is the honest
    no-data path and the caller opts into staleness explicitly."""
    if status == "ok":
        raise ValueError("service_failed cannot build an 'ok' result")
    return ServiceResult[T](
        status=status,
        data=None,
        stale=False,
        observed_at=None,
        last_success_at=last_success_at,
        error=error,
    )


# --------------------------------------------------------------------------
# operation.json / capabilities.json / services.json
# --------------------------------------------------------------------------


class OperationTarget(OfficeModel):
    """Only local identifiers the server resolved itself.

    Never a pane id, a tmux target, a path, a workspace key or any part of the
    validated body: the browser learns nothing from a target it did not already
    name. :class:`ActionSpec` reuses this type so a resolved target cannot
    smuggle anything else into the coordinator either.
    """

    __wire_optional__ = ("project_id", "agent_id")

    project_id: Id | None = None
    agent_id: Id | None = None


class Operation(OfficeModel):
    """Work the back-end owns past the request (``operation.json``).

    200 means a :class:`Receipt`; 202 means this. ``outcome_unknown`` carries a
    null receipt on purpose — inventing one would claim a delivery nobody
    observed — and is never retried automatically.
    """

    __wire_required__ = (
        "operation_id",
        "kind",
        "target",
        "status",
        "submitted_at",
        "updated_at",
        "receipt",
        "error",
    )

    operation_id: RemoteId
    """Opaque and server-generated. Never the client's ``Idempotency-Key``."""
    kind: OperationKind
    target: OperationTarget
    status: OperationStatus
    submitted_at: Timestamp
    updated_at: Timestamp
    receipt: Receipt | None = None
    error: ServiceError | None = None

    @model_validator(mode="after")
    def _check_status(self) -> Operation:
        if self.receipt is not None and self.status != "succeeded":
            raise ValueError(f"only a succeeded Operation carries a receipt; got {self.status!r}")
        if self.error is not None and self.status not in ("failed", "outcome_unknown"):
            raise ValueError(f"an Operation with status {self.status!r} carries no error")
        return self


class Feature(OfficeModel):
    """One capability, reported whether or not it is on.

    A back-end that cannot do something reports ``enabled: false`` with a
    reason; omitting the entry is a contract violation, not a way to say "off".
    """

    __wire_required__ = ("id", "enabled", "reason")

    id: FeatureId
    enabled: bool
    reason: Annotated[str, StringConstraints(max_length=200)] | None = None

    @model_validator(mode="after")
    def _check_reason(self) -> Feature:
        if self.enabled and self.reason is not None:
            raise ValueError("an enabled Feature has a null reason")
        if not self.enabled and self.reason is None:
            raise ValueError("a disabled Feature must say why")
        return self


class Provider(OfficeModel):
    """What evidence the back-end really has for one agent provider.

    ``pane_only`` is an honest statement about evidence, not a fault: a Question
    from such a provider is detected by frame and may lag.
    """

    __wire_required__ = ("id", "observation", "structured_input", "reason")

    id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    observation: ProviderObservation
    structured_input: bool
    """False means the GUI must send the user to the terminal."""
    reason: Annotated[str, StringConstraints(max_length=200)] | None = None

    @model_validator(mode="after")
    def _check_reason(self) -> Provider:
        if (
            self.observation == "hooks_and_pane"
            and self.structured_input
            and self.reason is not None
        ):
            raise ValueError("a fully capable Provider has a null reason")
        return self


class Service(OfficeModel):
    """How one service answered when last checked."""

    __wire_required__ = ("id", "status", "detail", "checked_at")

    id: ServiceId
    status: ServiceStatus
    detail: Annotated[str, StringConstraints(max_length=200)] | None = None
    """One bounded sentence. Never an endpoint carrying a key, or a path."""
    checked_at: Timestamp | None = None
    """Null when the service was never reached — which is not the same as
    reaching it and finding it down."""


class Capabilities(OfficeModel):
    """``GET /api/capabilities``: what this back-end speaks, wires and observes."""

    __wire_required__ = (
        "contract_revision",
        "supported_contract_revisions",
        "artifact_revision",
        "features",
        "providers",
        "services",
    )

    contract_revision: ContractRevision
    supported_contract_revisions: Annotated[
        tuple[ContractRevision, ...], Field(min_length=1, max_length=8)
    ]
    artifact_revision: Annotated[str, StringConstraints(max_length=128)] | None = None
    """``manifest.json``'s digest, or null when not built from a pinned pack."""
    features: Annotated[tuple[Feature, ...], Field(min_length=11, max_length=32)]
    providers: Annotated[tuple[Provider, ...], Field(max_length=16)]
    services: Annotated[tuple[Service, ...], Field(min_length=4, max_length=4)]

    @model_validator(mode="after")
    def _check_inventories(self) -> Capabilities:
        feature_ids = [feature.id for feature in self.features]
        if sorted(feature_ids) != sorted(FEATURE_IDS):
            raise ValueError("Capabilities.features must list every FeatureId exactly once")
        service_ids = [service.id for service in self.services]
        if sorted(service_ids) != sorted(SERVICE_IDS):
            raise ValueError("Capabilities.services must list every ServiceId exactly once")
        provider_ids = [provider.id for provider in self.providers]
        if len(set(provider_ids)) != len(provider_ids):
            raise ValueError("Capabilities.providers must list each provider once")
        if self.contract_revision not in self.supported_contract_revisions:
            raise ValueError("contract_revision must appear in supported_contract_revisions")
        return self


class ServicesDocument(OfficeModel):
    """``GET /api/services`` — the same four entries, probed fresh."""

    __wire_required__ = ("services", "checked_at")

    services: Annotated[tuple[Service, ...], Field(min_length=4, max_length=4)]
    checked_at: Timestamp

    @model_validator(mode="after")
    def _check_services(self) -> ServicesDocument:
        if sorted(service.id for service in self.services) != sorted(SERVICE_IDS):
            raise ValueError("ServicesDocument.services must list every ServiceId exactly once")
        return self


# --------------------------------------------------------------------------
# request.json — the validated bodies of the changed 1.4 mutations
# --------------------------------------------------------------------------


class ResolveRequest(OfficeModel):
    """``POST /api/queue/{id}/resolve`` at revision 1.4.

    The one normative structured resolve body; the WS ``answer`` frame carries
    the same shape so there is no second vocabulary. The v1 ``{answer}`` body is
    historical and a 1.4 call carrying it is refused, not reinterpreted.
    """

    __wire_required__ = ("prompt_id",)
    __wire_optional__ = ("option", "reason", "comment", "selections", "other", "text", "confirm")

    prompt_id: PromptIdStr
    option: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    reason: Annotated[str, StringConstraints(max_length=2000)] | None = None
    comment: Annotated[str, StringConstraints(max_length=2000)] | None = None
    selections: (
        Annotated[
            tuple[
                Annotated[tuple[Annotated[int, Field(ge=0, le=4)], ...], Field(max_length=5)], ...
            ],
            Field(max_length=4),
        ]
        | None
    ) = None
    other: Mapping[str, Annotated[str, StringConstraints(max_length=2000)]] | None = None
    text: Annotated[str, StringConstraints(max_length=8000)] | None = None
    confirm: bool | None = None
    """Required true when the chosen option would switch the agent into auto or
    bypass mode; without it the back-end answers ``mode_change`` and sends
    nothing."""


class ResolveAllRequest(OfficeModel):
    """``POST /api/queue/resolve-all``. Permission-only, and never allow-remember:
    a bulk action must not leave a durable rule behind."""

    __wire_required__ = ("kind", "option", "prompt_ids")
    __wire_optional__ = ("project_id",)

    kind: Literal["permission"] = "permission"
    option: Literal["allow", "deny"]
    prompt_ids: Annotated[tuple[PromptIdStr, ...], Field(min_length=1, max_length=50)]
    """Exactly the prompts the client saw. The sweep is never widened to one it
    did not name; a stale id is skipped and reported."""
    project_id: Id | None = None


class TellRequest(OfficeModel):
    __wire_required__ = ("text",)

    text: Annotated[str, StringConstraints(min_length=1, max_length=8000)]


class KeysRequest(OfficeModel):
    """tmux key names. The server resolves which pane they reach from the agent
    id; the browser never names a pane or a tmux target."""

    __wire_required__ = ("keys",)

    keys: Annotated[
        tuple[Annotated[str, StringConstraints(min_length=1, max_length=32)], ...],
        Field(min_length=1, max_length=32),
    ]


class InterruptRequest(OfficeModel):
    """One operation, two explicit strengths: soft ``Escape`` by default, ``C-c``
    when ``hard`` is true."""

    __wire_optional__ = ("hard",)

    hard: bool | None = None


class StopRequest(OfficeModel):
    """Deliberately empty, and deliberately strict: an undocumented property is
    rejected rather than ignored."""


class SpawnRequest(OfficeModel):
    __wire_required__ = ("role",)
    __wire_optional__ = ("label", "task_id", "prompt")

    role: Role
    label: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    task_id: Id | None = None
    prompt: Annotated[str, StringConstraints(max_length=8000)] | None = None


class RespawnRequest(OfficeModel):
    __wire_optional__ = ("prompt",)

    prompt: Annotated[str, StringConstraints(max_length=8000)] | None = None


# --------------------------------------------------------------------------
# team-os.json
# --------------------------------------------------------------------------


class TeamOSMeta(OfficeModel):
    """The local peer's own description. ``available: false`` with status
    ``unavailable`` is the normal stopped-peer case, never a fleet outage."""

    __wire_required__ = ("source", "available", "views", "display_name", "peer_revision")

    source: TeamOSSource = "team_os"
    available: bool
    views: Annotated[tuple[ViewName, ...], Field(max_length=32)]
    """The read views this peer supports and this back-end allows. Empty when the
    peer is down — an empty allow-list, not an error."""
    display_name: Annotated[str, StringConstraints(max_length=120)] | None = None
    peer_revision: Annotated[str, StringConstraints(max_length=64)] | None = None


class TeamOSRosterEntry(OfficeModel):
    """One peer identity. ``peer_local_id`` is stable inside Team OS and is never
    an Office fleet agent id or session id; the namespaces are not joined."""

    __wire_required__ = ("source", "peer_local_id", "label", "summary")

    source: TeamOSSource = "team_os"
    peer_local_id: RemoteId
    label: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    summary: Annotated[str, StringConstraints(max_length=400)] | None = None


class TeamOSViewSection(OfficeModel):
    __wire_required__ = ("title", "text")

    title: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    text: Annotated[str, StringConstraints(max_length=8000)]
    """Plain text. Never HTML, never a filesystem read, never a rendered template
    the peer supplied for the browser to execute."""


class TeamOSView(OfficeModel):
    __wire_required__ = ("source", "name", "title", "sections")

    source: TeamOSSource = "team_os"
    name: ViewName
    title: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    sections: Annotated[tuple[TeamOSViewSection, ...], Field(max_length=32)]


# --------------------------------------------------------------------------
# project-context.json
# --------------------------------------------------------------------------


class ContextSection(OfficeModel):
    __wire_required__ = ("title", "text")

    title: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    text: Annotated[str, StringConstraints(max_length=8000)]


class CliContext(OfficeModel):
    __wire_required__ = ("source", "project_id", "sections")

    source: Literal["cli"] = "cli"
    project_id: Id
    sections: Annotated[tuple[ContextSection, ...], Field(max_length=32)]


class ProjectContext(OfficeModel):
    """The CLI's project context plus, only when explicitly associated, a peer
    view. The two are separately labelled sources and are never merged: a null
    ``team_os`` never removes or alters anything under ``cli``."""

    __wire_required__ = ("project_id", "cli", "team_os")

    project_id: Id
    cli: CliContext
    team_os: TeamOSView | None = None


# --------------------------------------------------------------------------
# explainability.json
# --------------------------------------------------------------------------


class RunSummary(OfficeModel):
    """One platform run, normalised.

    ``join`` is ``verified`` only through the correlation the sidecar recorded;
    it is never upgraded by a name match, a timestamp guess or a shared prefix.
    """

    __wire_required__ = (
        "run_id",
        "workspace_id",
        "studio_id",
        "stable_agent_id",
        "local_agent_id",
        "join",
        "state",
        "upstream_state_label",
        "started_at",
        "updated_at",
        "platform_usd",
        "duration_s",
    )

    run_id: RemoteId
    workspace_id: RemoteId
    studio_id: Annotated[str, StringConstraints(max_length=128)] | None
    stable_agent_id: Annotated[str, StringConstraints(max_length=128)] | None
    local_agent_id: Id | None
    """The Office session id, present only when ``join`` is ``verified``."""
    join: RunJoinState
    state: RunState
    upstream_state_label: Annotated[str, StringConstraints(max_length=64)] | None
    """What the platform called this state, shown as-is beside the normalised one."""
    started_at: Timestamp | None
    updated_at: Timestamp | None
    platform_usd: Annotated[float, Field(ge=0)] | None
    """Null (not priced) and 0 (measured as zero) are different values and stay
    different. Never summed with local transcript cost — a separate source."""
    duration_s: Annotated[int, Field(ge=0, le=31_536_000)] | None

    @model_validator(mode="after")
    def _check_join(self) -> RunSummary:
        if self.join == "unjoined" and self.local_agent_id is not None:
            raise ValueError("an unjoined RunSummary carries no local_agent_id")
        return self


class RunDetail(OfficeModel):
    __wire_required__ = ("run", "summary_text", "available_details")

    run: RunSummary
    summary_text: Annotated[str, StringConstraints(max_length=4000)] | None
    """The bounded human summary, or null. Never a serialised trace."""
    available_details: Annotated[tuple[Literal["policies", "reasoning"], ...], Field(max_length=8)]
    """Which detail views this run actually has, so the UI does not offer a tab
    that will answer ``not_available``."""


class PolicyRow(OfficeModel):
    __wire_required__ = ("policy_id", "status", "summary")

    policy_id: RemoteId
    status: PolicyStatus
    summary: Annotated[str, StringConstraints(max_length=1000)] | None


class PolicySummary(OfficeModel):
    """``processing`` is a domain state, deliberately here rather than in
    :data:`ServiceStatus`: a run whose policies are still processing is a
    *successful* read of a run that is not finished."""

    __wire_required__ = ("state", "rows")

    state: DetailState
    rows: Annotated[tuple[PolicyRow, ...], Field(max_length=100)]

    @model_validator(mode="after")
    def _check_rows(self) -> PolicySummary:
        if self.state != "ready" and self.rows:
            raise ValueError("PolicySummary rows are empty unless the state is ready")
        return self


class ReasoningSection(OfficeModel):
    __wire_required__ = ("title", "text")

    title: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    text: Annotated[str, StringConstraints(max_length=8000)]


class ReasoningSummary(OfficeModel):
    __wire_required__ = ("state", "summary", "sections")

    state: DetailState
    summary: Annotated[str, StringConstraints(max_length=4000)] | None
    sections: Annotated[tuple[ReasoningSection, ...], Field(max_length=32)]

    @model_validator(mode="after")
    def _check_sections(self) -> ReasoningSummary:
        if self.state != "ready" and self.sections:
            raise ValueError("ReasoningSummary sections are empty unless the state is ready")
        return self


# --------------------------------------------------------------------------
# praxis.json
# --------------------------------------------------------------------------


class InsightSummary(OfficeModel):
    __wire_required__ = (
        "insight_id",
        "studio_id",
        "stable_agent_id",
        "status",
        "text",
        "summary",
        "created_at",
        "updated_at",
        "source_reference",
    )

    insight_id: RemoteId
    studio_id: RemoteId
    stable_agent_id: Annotated[str, StringConstraints(max_length=128)] | None
    status: InsightStatus
    text: Annotated[str, StringConstraints(max_length=8000)]
    summary: Annotated[str, StringConstraints(max_length=1000)] | None
    created_at: Timestamp | None
    updated_at: Timestamp | None
    source_reference: Annotated[str, StringConstraints(max_length=200)] | None
    """A bounded opaque reference. Never a URL carrying a key, a filesystem path
    or a raw upstream record."""


class ContextPreview(OfficeModel):
    """What *would* be assembled — a candidate, not an injection.

    There is deliberately no ``run_id`` property and none may be added: the
    preview route rejects a ``run_id`` parameter precisely so an adapter cannot
    forward the current session's run and make a candidate read like an
    injection that happened. A preview creates no audit record.
    """

    __wire_required__ = (
        "candidate_only",
        "context_text",
        "insight_ids",
        "token_count",
        "studio_id",
        "stable_agent_id",
    )

    candidate_only: Literal[True] = True
    context_text: Annotated[str, StringConstraints(max_length=32000)]
    insight_ids: Annotated[tuple[RemoteId, ...], Field(max_length=100)]
    token_count: Annotated[int, Field(ge=0, le=10_000_000)] | None
    """Null when the platform reported none. Zero is a measured zero."""
    studio_id: RemoteId
    stable_agent_id: Annotated[str, StringConstraints(max_length=128)] | None


class InjectionRecord(OfficeModel):
    """What the platform *served*, and when.

    There is deliberately no consumed/applied/used field: serving context is
    observable, a model consuming it is not, and the contract will not let a UI
    claim the second from the first.
    """

    __wire_required__ = (
        "run_id",
        "studio_id",
        "stable_agent_id",
        "bundle_id",
        "insight_ids",
        "served_at",
    )

    run_id: RemoteId
    studio_id: RemoteId
    stable_agent_id: Annotated[str, StringConstraints(max_length=128)] | None
    bundle_id: Annotated[str, StringConstraints(max_length=128)] | None
    insight_ids: Annotated[tuple[RemoteId, ...], Field(max_length=100)]
    served_at: Timestamp | None


class ProvenanceNode(OfficeModel):
    __wire_required__ = ("ref", "kind", "label")

    ref: RemoteId
    kind: ProvenanceNodeKind
    label: Annotated[str, StringConstraints(min_length=1, max_length=200)]


class ProvenanceEdge(OfficeModel):
    __wire_required__ = ("from_ref", "to_ref", "relation")

    from_ref: RemoteId
    to_ref: RemoteId
    relation: ProvenanceRelation


class ProvenanceSummary(OfficeModel):
    """Enough to explain the one selected lesson. Not a graph endpoint: the
    browser cannot ask for a traversal depth, a node set or a query."""

    __wire_required__ = ("insight_id", "nodes", "edges", "text")

    insight_id: RemoteId
    nodes: Annotated[tuple[ProvenanceNode, ...], Field(max_length=100)]
    edges: Annotated[tuple[ProvenanceEdge, ...], Field(max_length=200)]
    text: Annotated[str, StringConstraints(max_length=4000)] | None


class TeachEvidence(OfficeModel):
    """Allowed local references only. There is no path, url, file or raw kind, so
    the browser cannot make the back-end read or forward something arbitrary."""

    __wire_required__ = ("kind", "id")

    kind: TeachEvidenceKind
    id: RemoteId


class TeachRequest(OfficeModel):
    """``POST /api/projects/{id}/learning/teach``.

    The browser picks text, a finite intent and local evidence. It never selects
    the platform actor, the credential, the endpoint or the target scope — all
    resolved server-side from the configured binding.
    """

    __wire_required__ = ("text", "intent")
    __wire_optional__ = ("evidence",)

    text: Annotated[str, StringConstraints(min_length=1, max_length=8000)]
    intent: TeachIntent
    evidence: Annotated[tuple[TeachEvidence, ...], Field(max_length=20)] | None = None


# --------------------------------------------------------------------------
# event.json — the 27 stream events
# --------------------------------------------------------------------------


class AgentEnteredEvent(OfficeModel):
    """A session appeared. Always the first event for that id."""

    __wire_required__ = ("kind", "agent")

    kind: Literal["agent.entered"] = "agent.entered"
    agent: Agent


class AgentLeftEvent(OfficeModel):
    """A session is gone. Always the last event for that id."""

    __wire_required__ = ("kind", "agent_id", "reason")

    kind: Literal["agent.left"] = "agent.left"
    agent_id: Id
    reason: Literal["ended", "pruned"]


class AgentStateEvent(OfficeModel):
    __wire_required__ = ("kind", "agent_id", "state", "since_s")
    __wire_optional__ = ("question",)

    kind: Literal["agent.state"] = "agent.state"
    agent_id: Id
    state: OfficeState
    since_s: Annotated[int, Field(ge=0)]
    question: Question | None = None


class AgentActivityEvent(OfficeModel):
    __wire_required__ = ("kind", "agent_id", "activity")
    __wire_optional__ = ("doing",)

    kind: Literal["agent.activity"] = "agent.activity"
    agent_id: Id
    activity: OfficeActivity
    doing: str | None = None


class AgentProjectEvent(OfficeModel):
    __wire_required__ = ("kind", "agent_id", "project_now", "projects")

    kind: Literal["agent.project"] = "agent.project"
    agent_id: Id
    project_now: Id
    projects: Annotated[tuple[Id, ...], Field(min_length=1)]


class AgentMoraleEvent(OfficeModel):
    """``morale`` is the new absolute value, so replaying the event is idempotent."""

    __wire_required__ = ("kind", "agent_id", "morale", "delta")
    __wire_optional__ = ("reason",)

    kind: Literal["agent.morale"] = "agent.morale"
    agent_id: Id
    morale: Annotated[int, Field(ge=0, le=100)]
    delta: int
    reason: str | None = None


class AgentOutputEvent(OfficeModel):
    __wire_required__ = ("kind", "agent_id", "output_tail")

    kind: Literal["agent.output"] = "agent.output"
    agent_id: Id
    output_tail: Annotated[tuple[str, ...], Field(max_length=6)]


class QueueChangedEvent(OfficeModel):
    __wire_required__ = ("kind", "queue")

    kind: Literal["queue.changed"] = "queue.changed"
    queue: tuple[Id, ...]


class TaskChangedEvent(OfficeModel):
    __wire_required__ = ("kind", "task")

    kind: Literal["task.changed"] = "task.changed"
    task: Task


class EventNoteEvent(OfficeModel):
    __wire_required__ = ("kind", "seq", "text", "note_kind", "at")
    __wire_optional__ = ("agent_id", "to_role")

    kind: Literal["event.note"] = "event.note"
    seq: Annotated[int, Field(ge=0)]
    text: Annotated[str, StringConstraints(min_length=1)]
    note_kind: Annotated[str, StringConstraints(min_length=1)]
    at: Timestamp
    agent_id: Id | None = None
    to_role: str | None = None


class OfficeStaleEvent(OfficeModel):
    __wire_required__ = ("kind", "stale")
    __wire_optional__ = ("detail",)

    kind: Literal["office.stale"] = "office.stale"
    stale: bool
    detail: str | None = None


class AgentPauseEvent(OfficeModel):
    """v1.1, deprecated in v1.3: mirrors the project-level hiring freeze. It never
    means the process was suspended; new clients read ``project.frozen``."""

    __wire_required__ = ("kind", "agent_id", "pause")
    __wire_optional__ = ("detail",)

    kind: Literal["agent.pause"] = "agent.pause"
    agent_id: Id
    pause: Pause
    detail: str | None = None


class AgentTurnEvent(OfficeModel):
    __wire_required__ = ("kind", "agent_id", "turn")

    kind: Literal["agent.turn"] = "agent.turn"
    agent_id: Id
    turn: Turn


class AgentErrorEvent(OfficeModel):
    __wire_required__ = ("kind", "agent_id", "last_error")

    kind: Literal["agent.error"] = "agent.error"
    agent_id: Id
    last_error: str


class AgentPaneEvent(OfficeModel):
    __wire_required__ = ("kind", "agent_id", "pane_alive")
    __wire_optional__ = ("exit_status",)

    kind: Literal["agent.pane"] = "agent.pane"
    agent_id: Id
    pane_alive: bool
    exit_status: int | None = None


class AgentStatsEvent(OfficeModel):
    """``stats`` is required *and* nullable: null means not known, and is never
    guessed — so the key is always present."""

    __wire_required__ = ("kind", "agent_id", "stats")

    kind: Literal["agent.stats"] = "agent.stats"
    agent_id: Id
    stats: Stats | None = None


class AgentHistoryEvent(OfficeModel):
    __wire_required__ = ("kind", "agent_id", "item")

    kind: Literal["agent.history"] = "agent.history"
    agent_id: Id
    item: HistoryItem


class QueueAnsweredEvent(OfficeModel):
    """The user answered somewhere, so the GUI clears its prompt UI even when the
    answer came from the terminal app."""

    __wire_required__ = ("kind", "agent_id", "by")
    __wire_optional__ = ("answer",)

    kind: Literal["queue.answered"] = "queue.answered"
    agent_id: Id
    by: Literal["gui", "terminal"]
    answer: str | None = None


class AgentCostEvent(OfficeModel):
    __wire_required__ = ("kind", "agent_id", "cost")

    kind: Literal["agent.cost"] = "agent.cost"
    agent_id: Id
    cost: Cost


class OfficeCostEvent(OfficeModel):
    __wire_required__ = ("kind", "cost")

    kind: Literal["office.cost"] = "office.cost"
    cost: OfficeCost


class CostFlagEvent(OfficeModel):
    """Emitted once when a cost outlier first holds; it is dropped from
    ``Cost.flags`` when it stops holding, without a second event."""

    __wire_required__ = ("kind", "agent_id", "flag")

    kind: Literal["cost.flag"] = "cost.flag"
    agent_id: Id
    flag: CostFlag


class AgentSubEvent(OfficeModel):
    """What the pane shows *inside* the state. The event that most often fires
    while the board sequence is unchanged, because it comes from pane facts."""

    __wire_required__ = ("kind", "agent_id", "sub", "subagents")
    __wire_optional__ = ("sub_detail", "context_pct", "limited_until", "auto_resume")

    kind: Literal["agent.sub"] = "agent.sub"
    agent_id: Id
    sub: SubState
    subagents: Annotated[int, Field(ge=0)]
    sub_detail: str | None = None
    context_pct: float | None = None
    limited_until: Timestamp | None = None
    auto_resume: bool | None = None


class AgentModeEvent(OfficeModel):
    __wire_required__ = ("kind", "agent_id", "permission_mode")

    kind: Literal["agent.mode"] = "agent.mode"
    agent_id: Id
    permission_mode: PermissionMode


class AgentHealthEvent(OfficeModel):
    __wire_required__ = ("kind", "agent_id", "health")
    __wire_optional__ = ("exit_status",)

    kind: Literal["agent.health"] = "agent.health"
    agent_id: Id
    health: PaneHealth
    exit_status: int | None = None


class AgentEndedEvent(OfficeModel):
    """Replaces ``agent.left`` for the retention window: the row stays in the
    snapshot for ten minutes so the office can show it leaving and offer
    Respawn. ``agent.left`` follows when the deadline passes."""

    __wire_required__ = ("kind", "agent_id", "ended_reason", "output_tail")
    __wire_optional__ = ("exit_status",)

    kind: Literal["agent.ended"] = "agent.ended"
    agent_id: Id
    ended_reason: EndedReason
    output_tail: tuple[str, ...]
    exit_status: int | None = None


class ProjectFrozenEvent(OfficeModel):
    """The project-level hiring freeze went on or off. Agents already working keep
    working; this is not process suspension."""

    __wire_required__ = ("kind", "project_id", "frozen")

    kind: Literal["project.frozen"] = "project.frozen"
    project_id: Id
    frozen: bool


class PromptChangedEvent(OfficeModel):
    """The pane's prompt appeared, changed or went away; ``question`` is null when
    it went away, so the key is always present."""

    __wire_required__ = ("kind", "agent_id", "question")

    kind: Literal["prompt.changed"] = "prompt.changed"
    agent_id: Id
    question: Question | None = None


Event = Annotated[
    AgentEnteredEvent
    | AgentLeftEvent
    | AgentStateEvent
    | AgentActivityEvent
    | AgentProjectEvent
    | AgentMoraleEvent
    | AgentOutputEvent
    | QueueChangedEvent
    | TaskChangedEvent
    | EventNoteEvent
    | OfficeStaleEvent
    | AgentPauseEvent
    | AgentTurnEvent
    | AgentErrorEvent
    | AgentPaneEvent
    | AgentStatsEvent
    | AgentHistoryEvent
    | QueueAnsweredEvent
    | AgentCostEvent
    | OfficeCostEvent
    | CostFlagEvent
    | AgentSubEvent
    | AgentModeEvent
    | AgentHealthEvent
    | AgentEndedEvent
    | ProjectFrozenEvent
    | PromptChangedEvent,
    Field(discriminator="kind"),
]
"""One ``event:`` message on the stream, discriminated by ``kind``."""

EVENT_MODELS: tuple[type[OfficeModel], ...] = (
    AgentEnteredEvent,
    AgentLeftEvent,
    AgentStateEvent,
    AgentActivityEvent,
    AgentProjectEvent,
    AgentMoraleEvent,
    AgentOutputEvent,
    QueueChangedEvent,
    TaskChangedEvent,
    EventNoteEvent,
    OfficeStaleEvent,
    AgentPauseEvent,
    AgentTurnEvent,
    AgentErrorEvent,
    AgentPaneEvent,
    AgentStatsEvent,
    AgentHistoryEvent,
    QueueAnsweredEvent,
    AgentCostEvent,
    OfficeCostEvent,
    CostFlagEvent,
    AgentSubEvent,
    AgentModeEvent,
    AgentHealthEvent,
    AgentEndedEvent,
    ProjectFrozenEvent,
    PromptChangedEvent,
)
"""Every event variant, so a test can prove the inventory matches the schema."""


# --------------------------------------------------------------------------
# Internal records — frozen, never serialized to a browser
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TerminalPaneFacts:
    """Bounded pane facts, as ``display-message`` knows them at one instant.

    Deliberately narrower than :class:`aisquare.core.tmux.PaneFacts`: no socket,
    no title, no ``current_command``. A pane's title and command line are
    arbitrary text from the process, and this value travels toward the browser
    inside a frame.
    """

    width: int
    height: int
    cursor_x: int
    cursor_y: int
    cursor_visible: bool
    alternate_on: bool
    history_size: int
    dead: bool
    dead_status: int | None
    in_mode: bool


@dataclass(frozen=True, slots=True)
class TerminalTarget:
    """A server-resolved terminal target: opaque local identity, nothing else.

    Never a browser model. There is no pane id, socket, session name or tmux
    argument here — P08 resolves those itself — so no browser-supplied value can
    become a tmux target. ``generation`` fences a recycled pane: a capture for
    an old generation is refused rather than answered from the new one.
    """

    agent_id: str
    session_id: str | None = None
    generation: int = 0


@dataclass(frozen=True, slots=True)
class TerminalFrame:
    """One captured frame.

    ``requested_scrollback`` and ``scrollback`` are both kept because tmux
    clamps a request deeper than the pane's history to the top of history:
    collapsing them loses the difference between "you asked for 5000" and "you
    got 900", which is exactly what a scrollback UI needs to stop paging.
    """

    target: TerminalTarget
    lines: tuple[str, ...]
    requested_scrollback: int
    scrollback: int
    """The EFFECTIVE offset, as :class:`aisquare.core.tmux.Capture` reports it."""
    captured_at: datetime
    source: Literal["pane", "board", "none"]
    facts: TerminalPaneFacts | None = None
    history_size: int | None = None
    stale: bool = False
    error: ServiceError | None = None


@dataclass(frozen=True, slots=True)
class PromptEvidence:
    """What the back-end actually observed about one prompt lifecycle.

    ``generation`` plus ``prompt_id`` are the fence: the same text appearing
    after the pane cleared is a new lifecycle with a new id, and an answer
    prepared against the old one is refused with ``stale_prompt`` and zero
    keystrokes. ``raw`` is bounded pane text kept as evidence; it is never a
    transcript and never reaches a browser unredacted.
    """

    agent_id: str
    provider: str
    prompt_id: str
    generation: int
    kind: QuestionKind
    detected_by: DetectedBy
    observed_at: datetime
    options: tuple[QuestionOption, ...] = ()
    questions: tuple[AskQuestion, ...] = ()
    selection: tuple[int, ...] | None = None
    raw: str | None = None
    stale: bool = False


@dataclass(frozen=True, slots=True)
class HookFact:
    """One bounded scalar fact a hook reported, with when it was seen."""

    agent_id: str
    name: str
    value: str | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class PaneObservation:
    """What one poll saw of one pane. Bounded lines, never a full scrollback."""

    agent_id: str
    alive: bool
    health: PaneHealth
    observed_at: datetime
    lines: tuple[str, ...] = ()
    facts: TerminalPaneFacts | None = None
    exit_status: int | None = None


@dataclass(frozen=True, slots=True)
class LocalObservationBatch:
    """One bounded read of everything local, as values.

    Deliberately free of live database connections and mutable service clients:
    it is produced on a worker thread and consumed by a pure projector, so
    anything live in here would be a connection crossing a thread boundary and a
    projection that could change under its own reader.

    The CLI models are carried as-is rather than re-spelled — they are the
    public shapes ``core.store`` already returns, and P04 maps them into the
    Office wire models. That mapping is where the contract's bounds apply; this
    record is evidence, not a response.
    """

    collected_at: datetime
    board_seq: int = 0
    projects: tuple[ProjectInfo, ...] = ()
    sessions: tuple[TeamSession, ...] = ()
    tasks: tuple[TeamTask, ...] = ()
    events: tuple[TeamEvent, ...] = ()
    fleet: tuple[FleetAgentStatus, ...] = ()
    turns: tuple[TurnMetric, ...] = ()
    panes: tuple[PaneObservation, ...] = ()
    prompts: tuple[PromptEvidence, ...] = ()
    hook_facts: tuple[HookFact, ...] = ()
    partial: bool = False
    """True when a source failed and this batch is known incomplete — the reason
    a snapshot may be marked stale rather than silently thinned."""


@dataclass(frozen=True, slots=True)
class Projection:
    """One immutable :class:`Snapshot` plus the indexes a diff needs.

    The snapshot is the whole public surface; the indexes exist because
    ``diff()`` must handle an unchanged board sequence — pane-derived state
    moves without the store moving — and because ``since_s`` counts from a
    transition, not from a last-seen column.

    Contains no Starlette object, no connection and no clock: ``project()`` is
    referentially transparent in its inputs.
    """

    snapshot: Snapshot
    generation: int = 0
    """Internal only. The public SSE id stays ``Snapshot.seq``."""
    prompts: Mapping[str, PromptEvidence] = field(default_factory=dict)
    state_since: Mapping[str, datetime] = field(default_factory=dict)
    """Agent id → when it entered its current state. A re-notification must not
    bump this, or a 600-second wait reports as 0."""


@dataclass(frozen=True, slots=True)
class ActionContext:
    """Who is acting, under which revision, on which request.

    The server derives every field. A browser never selects an auth scope: the
    journal's uniqueness is ``(auth_scope, idempotency_key)``, so a
    browser-chosen scope would be a browser-chosen way past another session's
    reservation.
    """

    auth_scope: str
    received_at: datetime
    contract_revision: ContractRevision | None = None
    """What the caller sent in ``X-Office-Contract``. None means it sent none,
    which a changed 1.4 route refuses before any side effect."""
    request_id: str | None = None
    idempotency_key: str | None = None
    """The client's key for an HTTP mutation, or the transport-namespaced key
    derived from a WS ``request_id``. Never part of an Operation's identity."""


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """A finite operation kind, a resolved target and a validated body.

    None of the three can carry anything executable: ``kind`` is a closed
    vocabulary, ``target`` is :class:`OperationTarget` (local ids only, no pane
    or path), and ``body`` must be a strict :class:`OfficeModel` — never a raw
    dict, a string to evaluate, or a callback the browser supplied.
    """

    kind: ActionKind
    target: OperationTarget
    body: OfficeModel | None = None

    def __post_init__(self) -> None:
        if self.body is not None and not isinstance(self.body, OfficeModel):
            raise TypeError("ActionSpec.body must be a validated OfficeModel, not raw input")


@dataclass(frozen=True, slots=True)
class OperationReservation:
    """The outcome of reserving a journal record *before* any side effect.

    ``conflict`` carries no Operation: the key was reused with a different kind,
    target or body, so there is no result to return and the caller answers 409
    ``idempotency_conflict`` having executed nothing.
    """

    disposition: Literal["new", "existing", "conflict"]
    operation: Operation | None = None

    def __post_init__(self) -> None:
        if self.disposition == "conflict":
            if self.operation is not None:
                raise ValueError("a conflicting reservation returns no Operation")
        elif self.operation is None:
            raise ValueError(f"a {self.disposition!r} reservation must carry its Operation")


ObservationCategory = Literal[
    "project", "session", "task", "fleet", "pane", "prompt", "cost", "activity"
]
"""The bounded derived categories the sidecar stores facts under."""

CheckpointConsumer = Literal["activity", "cost"]
"""The two incremental consumers. Their cursors are independent namespaces."""


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    """One incremental consumer's position in one source.

    Keyed by ``(consumer, source_id)`` with the source identity resolved by the
    server. ``state`` is bounded and versioned and holds no transcript, prompt
    text, key, credential or path — a cursor is a position, not a copy.
    """

    consumer: CheckpointConsumer
    source_id: str
    state_version: int
    state: Mapping[str, str | int | float | bool | None]
    generation: int
    observed_at: datetime
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.state_version <= 0:
            raise ValueError("CheckpointRecord.state_version must be positive")
        if not self.source_id:
            raise ValueError("CheckpointRecord.source_id must not be empty")


@dataclass(frozen=True, slots=True)
class ObservationUpdate:
    """One derived fact, committed in the same transaction as a cursor move.

    That atomicity is the point: a cursor that advances without its derived rows
    loses the work, and rows written without the cursor are counted twice.
    """

    category: ObservationCategory
    key: str
    payload: Mapping[str, str | int | float | bool | None]
    generation: int
    observed_at: datetime
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("ObservationUpdate.key must not be empty")


@dataclass(frozen=True, slots=True)
class PlatformBinding:
    """The resolved platform scope for one project — and no credential.

    The workspace key is *not* a field here and never becomes one: credentials
    are references resolved at request time by the existing CLI mechanism. The
    normalised base URL is not here either; it belongs to the resolved profile
    configuration. ``revision`` changes whenever the profile, workspace, studio,
    agent or endpoint changes, so cached data and operation records pinned to an
    old revision cannot cross a binding change.
    """

    binding_id: str
    revision: int
    project_id: str
    profile_name: str
    workspace_id: str
    studio_id: str | None = None
    agent_uid: str | None = None


@dataclass(frozen=True, slots=True)
class PlatformScope:
    """The server-selected scope one remote read runs under."""

    binding_id: str
    revision: int
    workspace_id: str
    studio_id: str | None = None
    agent_uid: str | None = None


@dataclass(frozen=True, slots=True)
class PlatformQuery:
    """Bounded, allow-listed filters and pagination for a remote read.

    Never an endpoint, a path or raw upstream JSON. ``limit`` and ``cursor``
    follow the contract's pagination bounds; ``filters`` is a small map of
    allow-listed scalar keys that P13/P14 narrow further for their own routes.
    """

    limit: int | None = None
    cursor: str | None = None
    filters: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.limit is not None and not 1 <= self.limit <= 100:
            raise ValueError("PlatformQuery.limit must be between 1 and 100")
        if len(self.filters) > 8:
            raise ValueError("PlatformQuery.filters is bounded to 8 entries")
        for key, value in self.filters.items():
            if not key or len(key) > 32 or len(value) > 128:
                raise ValueError(f"PlatformQuery filter {key!r} is out of bounds")


@dataclass(frozen=True, slots=True)
class RunJoin:
    """Whether a local session is joined to a remote run, and on what evidence.

    ``ambiguous`` exists so a back-end never has to choose between lying and
    dropping the row: more than one candidate matched, which is not a join.
    Only ``joined`` may become ``verified`` on the wire.
    """

    state: Literal["joined", "unjoined", "ambiguous"]
    run_id: str | None = None
    evidence: str | None = None
    scope: PlatformScope | None = None
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.state != "joined" and self.run_id is not None:
            raise ValueError("only a joined RunJoin names a run")

    @property
    def wire_join(self) -> RunJoinState:
        """How this join is reported in a :class:`RunSummary`.

        ``ambiguous`` reports ``unjoined``: the schema has two members on
        purpose, and an unproven link must never be shown as verified.
        """
        return "verified" if self.state == "joined" else "unjoined"


@dataclass(frozen=True, slots=True)
class TeachAck:
    """The platform's acknowledgement that it ingested a lesson.

    Acceptance is **not** lesson creation, and no response may claim it is.

    Internal on purpose: the pinned 1.4 artifact defines no ``TeachAck`` schema
    — ``backend-v1.4.md`` §4.2 has the teach route answer a ``Receipt`` or an
    ``Operation`` — so this value is what P14 hands P06's executor, never a body
    written to a browser. See the P01 handoff note.
    """

    disposition: Literal["accepted", "duplicate", "rejected", "unknown"]
    reference_id: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.detail is not None and len(self.detail) > 500:
            raise ValueError("TeachAck.detail is bounded to 500 characters")


@dataclass(frozen=True, slots=True)
class TransportResponse:
    """A bounded, decoded platform response. Exactly these five members.

    No credential, no authorization header, no unrestricted URL and no unbounded
    body: ``allowed_headers`` is an allow-list the transport applied, and
    ``json_body`` is decoded under byte, depth and length limits. P13/P14 map
    this into a :class:`ServiceResult`; it is never serialized to a browser.
    """

    status_code: int
    allowed_headers: Mapping[str, str]
    json_body: object
    received_at: datetime
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class TransportResult:
    """Exactly one of ``response`` or ``error``.

    Keeping the HTTP status inside ``response`` rather than collapsing it here
    is what lets P13/P14 tell a domain state (a 200 whose body says
    ``processing``) from an availability failure.
    """

    response: TransportResponse | None = None
    error: ServiceError | None = None

    def __post_init__(self) -> None:
        if (self.response is None) == (self.error is None):
            raise ValueError("TransportResult carries exactly one of response or error")


# Generic aliases the adapters return, named once so nobody re-spells them.
RunPage = Page[RunSummary]
InsightPage = Page[InsightSummary]
InjectionPage = Page[InjectionRecord]
RosterPage = Page[TeamOSRosterEntry]

__all__ = [
    "CONTRACT_ARTIFACT_FILE_COUNT",
    "CONTRACT_ARTIFACT_SHA256",
    "CONTRACT_REVISION",
    "EVENT_MODELS",
    "FEATURE_IDS",
    "SERVICE_IDS",
    "ActionContext",
    "ActionKind",
    "ActionSpec",
    "Agent",
    "AgentHistory",
    "AgentOutput",
    "ApiError",
    "ApiErrorCode",
    "AskQuestion",
    "Capabilities",
    "CheckpointConsumer",
    "CheckpointRecord",
    "CliContext",
    "ContextPreview",
    "ContextSection",
    "ContractRevision",
    "Cost",
    "CostFlag",
    "Diff",
    "DiffFile",
    "Event",
    "Feature",
    "FeatureId",
    "HistoryItem",
    "HookFact",
    "Id",
    "InjectionPage",
    "InjectionRecord",
    "InsightPage",
    "InsightSummary",
    "InterruptRequest",
    "KeysRequest",
    "LegacyActionKind",
    "LocalObservationBatch",
    "ObservationCategory",
    "ObservationUpdate",
    "OfficeCost",
    "OfficeModel",
    "Operation",
    "OperationKind",
    "OperationReservation",
    "OperationStatus",
    "OperationTarget",
    "Page",
    "PageCoverage",
    "PaneObservation",
    "Plan",
    "PlatformBinding",
    "PlatformQuery",
    "PlatformScope",
    "PolicyRow",
    "PolicySummary",
    "Project",
    "ProjectContext",
    "Projection",
    "PromptEvidence",
    "ProvenanceEdge",
    "ProvenanceNode",
    "ProvenanceSummary",
    "Provider",
    "Question",
    "QuestionOption",
    "ReasoningSection",
    "ReasoningSummary",
    "Receipt",
    "ResolveAllRequest",
    "ResolveRequest",
    "RespawnRequest",
    "RosterPage",
    "RunDetail",
    "RunJoin",
    "RunPage",
    "RunSummary",
    "Service",
    "ServiceError",
    "ServiceErrorCode",
    "ServiceId",
    "ServiceResult",
    "ServiceStatus",
    "ServicesDocument",
    "Snapshot",
    "SpawnRequest",
    "Stats",
    "StopRequest",
    "Task",
    "TeachAck",
    "TeachEvidence",
    "TeachRequest",
    "TeamOSMeta",
    "TeamOSRosterEntry",
    "TeamOSView",
    "TeamOSViewSection",
    "TellRequest",
    "TerminalFrame",
    "TerminalPaneFacts",
    "TerminalTarget",
    "Timestamp",
    "TokenCounts",
    "TransportResponse",
    "TransportResult",
    "Turn",
    "Worktree",
    "service_failed",
    "service_ok",
]
