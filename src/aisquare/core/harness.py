"""The agent harness: role -> model x effort tiering with an availability-aware ladder.

Ported from a production Claude Code fleet design. The principles:

- **Model follows role; effort follows depth.** Each role has a model *ladder*
  ordered top-tier-first: the planner and validator want the strongest model
  (``fable``) and fall back to the next tier (``opus``, then ``sonnet``) when
  it is not available to the account; workers (coder/runner) run on ``sonnet``,
  which is the measured sweet spot for agentic work, not a economy compromise.
- **Availability is verified, never assumed.** ``claude --model`` on a known
  model the account cannot use *silently substitutes the default with only a
  startup warning* — exit codes and launch success prove nothing. The probe
  therefore asserts that the reply's ``modelUsage`` contains the requested
  alias's model family. Results are cached (disposable, under ``cache/``) so
  the paid probe runs at most once a day per alias.
- **Fail-open, like everything else in the orchestrator.** A probe error walks
  down the ladder; the last rung is accepted without proof; nothing here may
  ever block a launch or break a hook. ``AISQUARE_HARNESS_PROBE=0`` disables
  probing entirely (tests, offline machines) and resolves ladders
  optimistically at their head.
- **Roles stay free-form.** Profiles exist for the four first-class roles;
  any other role string keeps working exactly as before, untiered.

Env knobs (the orchestrator's configuration surface is env, not files):

- ``AISQUARE_MODEL_<ROLE>``   — pin a role's model outright (skips the ladder).
- ``AISQUARE_EFFORT``         — the session's BASE effort; per-role offsets apply
                                on top (default ``high``). Falls back to
                                ``CLAUDE_EFFORT``, which Claude Code exports, so
                                a session raised to xhigh raises what it spawns.
- ``AISQUARE_EFFORT_<ROLE>``  — pin one role's effort absolutely (no offset).
- ``AISQUARE_HARNESS_PROBE=0``— never spawn probe subprocesses.

Known limits, stated rather than hidden:

- **Capture is advisory.** A session's model is whatever its ``SessionStart``
  payload reported (the field is optional in the Claude Code contract, and
  absent entirely on some surfaces — MCP virtual sessions have no model at
  all). ``spawn`` does not persist what it resolved, so "no chip on the board"
  means *not reported*, not *wrong*: absence is never flagged. Tiering
  enforcement lives at launch (``spawn``), not in the store.
- **Mid-session switches are invisible.** An in-session ``/model`` change is
  not re-reported, so a chip reflects the model at session start.
- **``effort`` capture is opportunistic.** It is recorded when a payload
  carries ``effort.level`` and stays ``None`` otherwise; nothing depends on it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from aisquare.core import spawn
from aisquare.core.paths import aisquare_home, cache_dir

if TYPE_CHECKING:  # runtime import stays lazy: config imports harness back
    from aisquare.core.config import TeamSettings

_OFF_VALUES = {"0", "false", "no", "off"}
PROBE_TIMEOUT_SECONDS = 150
CACHE_TTL = timedelta(hours=24)


@dataclass(frozen=True)
class ProbeContext:
    """The exact executable and effective account used by a launch.

    The executable's resolved path and stat fingerprint invalidate availability
    on upgrades without executing wrappers during read-only status queries.
    """

    binary: str
    env: dict[str, str]


_PROBE_CONTEXT: ContextVar[ProbeContext | None] = ContextVar("agent_probe", default=None)
_RESOLUTION_SCOPE: ContextVar[str | None] = ContextVar("agent_resolution_scope", default=None)

#: alias → the family token that proves the alias actually resolved to it. Matched
#: as a substring so full ids (``claude-sonnet-5``), dated legacy ids
#: (``claude-3-5-sonnet-20241022``) and provider ids
#: (``us.anthropic.claude-sonnet-4-5-v1:0``) all resolve to the same family.
MODEL_FAMILIES: dict[str, str] = {
    "fable": "fable",
    "opus": "opus",
    "sonnet": "sonnet",
    "haiku": "haiku",
}


#: The effort scale, weakest first — the levels ``claude --effort`` accepts.
#: (An unknown value is silently ignored by the CLI, so values are validated here.)
EFFORT_SCALE: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

#: ``ultracode`` = xhigh plus automatic workflow orchestration. Claude Code itself
#: reports it as ``xhigh`` in ``CLAUDE_EFFORT``, so it ranks there and is passed
#: through verbatim for any role that lands at xhigh or above.
ULTRACODE = "ultracode"

#: The base when nothing else says otherwise — the documented default for most work.
DEFAULT_BASE_EFFORT = "high"


class RoleProfile(BaseModel):
    """How one first-class role runs: its model ladder, effort shape, and mission."""

    role: str
    ladder: list[str]
    """Model aliases, strongest first; resolution walks down on unavailability."""
    effort_offset: int = 0
    """Tiers above the session's base effort. The gate rides one tier higher than
    the work it checks — a validator you can out-power is not a gate — so the
    ordering survives whatever base the user picks."""
    mission: str
    """One line of intent for the spawn banner — why this role exists."""
    default_args: list[str] = []
    """Flags the role needs on the agent binary wherever it starts — ``launch``,
    ``team spawn``, a fleet window — applied only to the default agent (``claude``)
    and only when the operator has not already said otherwise (see
    :func:`role_defaults`). A role's tooling is the role's business: the
    ui-tester needs Claude in Chrome, and asking every operator to remember
    ``--chrome`` is how the role silently degrades on the second machine."""


#: The role -> model x effort matrix. Order within a ladder is the fallback order;
#: effort is relative to the session base (default ``high``), never hardcoded.
ROLE_PROFILES: dict[str, RoleProfile] = {
    "planner": RoleProfile(
        role="planner",
        ladder=["fable", "opus", "sonnet"],
        mission="turns intent into contract-carrying tasks and keeps the board coherent",
    ),
    "coder": RoleProfile(
        role="coder",
        ladder=["sonnet", "opus"],
        mission="claims ready tasks and implements them to their acceptance criteria",
    ),
    "runner": RoleProfile(
        role="runner",
        ladder=["sonnet", "opus"],
        mission="fresh-context verifier — tries to make reviewed work fail before it ships",
    ),
    "validator": RoleProfile(
        role="validator",
        ladder=["fable", "opus"],
        effort_offset=1,
        mission="final accountability gate, once, on the assembled deliverable",
    ),
    # The fleet's roles (docs/plans/fleet-tui.md §3.3). `manager` is the planner
    # with fleet authority and rides the planner's ladder; `tester` is the
    # fleet's name for `runner` and shares its shape; `reviewer` reads the PR.
    "manager": RoleProfile(
        role="manager",
        ladder=["fable", "opus", "sonnet"],
        mission="turns a goal into a fleet that ships it — plans, spawns, steers, reports",
    ),
    "tester": RoleProfile(
        role="tester",
        ladder=["sonnet", "opus"],
        mission="fresh-context verifier — tries to make reviewed work fail before it ships",
    ),
    "reviewer": RoleProfile(
        role="reviewer",
        ladder=["sonnet", "opus"],
        mission="reads the PR as the stranger who will maintain it — read-only findings",
    ),
    "ui-tester": RoleProfile(
        role="ui-tester",
        ladder=["sonnet", "opus"],
        mission="verifies user-facing work in a real browser — evidence, not eyeballing",
        default_args=["--chrome"],
    ),
}


class RoleDefaults(BaseModel):
    """The role's own flags this launch gets, and what it did not get, and why.

    ``notes`` exists because a WITHHELD flag is invisible otherwise: the
    operator sees a successful launch and a ui-tester that reopens every UI
    task as "not browser-verified" with nothing on screen explaining why. Same
    shape and same reason as ``explainability.SessionIdentity``, which carries
    the note for the launch it could not pin.
    """

    args: list[str] = []
    notes: list[str] = []


def _opt_out_of(flag: str) -> str:
    """The flag that means "no" to ``flag``.

    Generated rather than tabled: ``--no-<flag>`` is the convention this CLI
    already relies on everywhere (``--worktree``/``--no-worktree``,
    ``--probe``/``--no-probe``), so it covers the next ``default_args`` entry
    the day it is added instead of the day someone remembers a table.
    """
    return f"--no-{flag.removeprefix('--')}"


def _flag_present(args: Sequence[str], flag: str) -> bool:
    """Whether ``args`` already says ``flag``, in either spelling.

    ``--chrome`` and ``--chrome=1`` are the same flag to the agent, so both
    count as the operator having said it — mirroring
    ``explainability._flag_value``, which parses these same two shapes out of
    this same argv for ``--session-id``. Exact-token matching read
    ``--no-chrome=1`` as an unrelated word and appended ``--chrome`` beside it,
    defeating the documented opt-out.
    """
    return any(arg == flag or arg.startswith(f"{flag}=") for arg in args)


def role_defaults(role: str, *, binary: str, args: Sequence[str]) -> RoleDefaults:
    """The role's :attr:`RoleProfile.default_args` this launch still needs.

    Applied only when the agent is Claude Code (:func:`is_default_agent`) — a
    wrapper or another agent has its own flags and would reject Claude Code's;
    skipped for any flag already present in ``args`` (a binding, a fleet
    ``extra_args``, the operator's own line) and for any flag whose ``--no-``
    opt-out is present. So the fleet's ``aisquare launch ui-tester`` inside a
    tmux window and an operator's ``ais-cli-ais ui-tester --chrome`` both end
    with exactly one ``--chrome``, and ``--no-chrome`` anywhere wins.

    Only the BINARY gate produces a note. The other two withholdings are
    already on screen — the flag or its opt-out is in the command the operator
    typed — while this one happens inside a tmux window nobody watched, for a
    binding made in another shell.
    """
    profile = ROLE_PROFILES.get(base_role(role))
    if profile is None or not profile.default_args:
        return RoleDefaults()
    if not is_default_agent(binary):
        return RoleDefaults(
            notes=[
                f"{', '.join(profile.default_args)} withheld: "
                f"{os.path.basename(binary)!r} is not {DEFAULT_AGENT_BINARY}"
            ]
        )
    return RoleDefaults(
        args=[
            flag
            for flag in profile.default_args
            if not _flag_present(args, flag) and not _flag_present(args, _opt_out_of(flag))
        ]
    )


#: A numbered SEAT: a first-class role with a crew index glued on (``coder1``).
_SEAT_SUFFIX = re.compile(r"^(?P<base>[a-z][a-z_-]*)\d+$")


def base_role(role: str) -> str:
    """The first-class role a numbered seat belongs to — ``coder1`` → ``coder``.

    ``cli/launch.py`` accepts numbered seats (a crew runs several agents in one
    role and needs them apart on the board) and exports the seat VERBATIM as
    ``AISQUARE_ROLE``, so every lookup in this module is handed ``coder1``.
    Measured before this existed: ``role_cycle("coder1", …)`` returned ``[]``
    where ``"coder"`` returns seven lines, ``model_mismatch("coder1", …)``
    returned ``None`` so the board could never flag a seat as off-ladder, and
    ``resolve_model("coder1")`` picked no model and no effort offset — the seat
    launched on the session default. The number is an identity, not a new role.

    Anything that is not a seat of a role this module PROFILES comes back
    unchanged, so a declared role called ``bot7`` stays ``bot7`` and a typo is
    never silently promoted to a real role.
    """
    match = _SEAT_SUFFIX.match(role)
    if match is None:
        return role
    base = match.group("base")
    return base if base in ROLE_PROFILES else role


def normalize_effort(value: str | None) -> str | None:
    """A raw effort value placed on :data:`EFFORT_SCALE`; ``None`` if unusable.

    ``ultracode`` ranks as ``xhigh`` (that is how Claude Code reports it).
    """
    if value is None:
        return None
    candidate = value.strip().lower()
    if candidate == ULTRACODE:
        return "xhigh"
    return candidate if candidate in EFFORT_SCALE else None


def effective_env() -> Mapping[str, str]:
    context = _PROBE_CONTEXT.get()
    return context.env if context is not None else os.environ


def role_env_key(kind: str, role: str) -> str:
    return f"AISQUARE_{kind}_" + "".join(c if c.isalnum() else "_" for c in role.upper())


def role_effort_override(role: str, env: Mapping[str, str] | None = None) -> str | None:
    """The ``AISQUARE_EFFORT_<ROLE>`` pin for ``role`` — absolute, offset not applied."""
    return (effective_env() if env is None else env).get(
        role_env_key("EFFORT", role), ""
    ).strip() or None


def base_effort() -> tuple[str, str]:
    """The session's base effort and where it came from.

    Precedence: ``AISQUARE_EFFORT`` (the harness knob) > ``CLAUDE_EFFORT`` (what
    the launching Claude session is actually running at, exported by Claude Code)
    > :data:`DEFAULT_BASE_EFFORT`. So raising your own session to xhigh raises the
    fleet you spawn from it, without configuring anything.
    """
    for name, source in (("AISQUARE_EFFORT", "env"), ("CLAUDE_EFFORT", "inherited")):
        raw = effective_env().get(name, "").strip()
        if not raw:
            continue
        rung = normalize_effort(raw)
        if rung is not None:
            return rung, source
    return DEFAULT_BASE_EFFORT, "default"


def _apply_offset(base: str, offset: int, *, ultracode: bool) -> str:
    index = EFFORT_SCALE.index(base) + offset
    index = max(0, min(index, len(EFFORT_SCALE) - 1))
    level = EFFORT_SCALE[index]
    if ultracode and index >= EFFORT_SCALE.index("xhigh"):
        return ULTRACODE  # keep the workflow-orchestration half the user asked for
    return level


def resolve_effort(role: str, *, explicit: str | None = None) -> tuple[str, str]:
    """The effort for ``role`` and its source.

    An explicit value (``spawn --effort``) or a per-role pin is absolute — the
    user named that role's level. Otherwise the session base is shifted by the
    role's predefined offset, so the relative shape holds at any base.
    """
    for value, source in ((explicit, "explicit"), (role_effort_override(role), "pinned")):
        if value is None:
            continue
        raw = value.strip().lower()
        if raw == ULTRACODE:
            return ULTRACODE, source
        rung = normalize_effort(raw)
        if rung is not None:
            return rung, source
    base, base_source = base_effort()
    profile = ROLE_PROFILES.get(base_role(role))
    offset = profile.effort_offset if profile else 0
    wants_ultracode = effective_env().get("AISQUARE_EFFORT", "").strip().lower() == ULTRACODE
    return _apply_offset(base, offset, ultracode=wants_ultracode), base_source


def effort_warning(model: str, effort: str) -> str | None:
    """A caution when a model/effort pairing is a known budget trap.

    Sonnet at ``max`` costs more per task than Opus does for the same work, so a
    worker that lands there is spending flagship money on a mid-tier model.
    """
    if effort == "max" and MODEL_FAMILIES["sonnet"] in model:
        return f"{model} at max effort out-spends opus per task — consider --effort xhigh"
    return None


class ModelResolution(BaseModel):
    """Outcome of resolving a role's model down its ladder."""

    role: str
    model: str
    effort: str
    source: str
    """How the pick was made: pinned (env), probed, cached, optimistic, or last-rung."""
    effort_source: str = "default"
    """Where the effort came from: explicit, pinned, env, inherited, or default."""
    skipped: list[str] = []
    """Ladder rungs that were probed (or cached) unavailable, in order."""
    notes: list[str] = []
    """Native argument precedence or compatibility mappings worth reporting."""


class ProbeResult(BaseModel):
    """One availability probe of a model alias, cache-shaped."""

    alias: str
    available: bool
    conclusive: bool = True
    """False when the probe could not determine what ran (never cached, never trusted)."""
    resolved_id: str | None = None
    reason: str | None = None
    checked_at: datetime


def probing_enabled() -> bool:
    """Whether availability probes may spawn subprocesses (default: yes)."""
    return effective_env().get("AISQUARE_HARNESS_PROBE", "").strip().lower() not in _OFF_VALUES


def role_model_override(role: str, env: Mapping[str, str] | None = None) -> str | None:
    """The ``AISQUARE_MODEL_<ROLE>`` pin for ``role``, if set."""
    value = (effective_env() if env is None else env).get(role_env_key("MODEL", role), "").strip()
    return value or None


def executable_path(binary: str, env: Mapping[str, str]) -> str | None:
    path = env.get("PATH")
    return (
        shutil.which(binary) if path == os.environ.get("PATH") else shutil.which(binary, path=path)
    )


_SCOPE_CACHE: ContextVar[dict[str, str] | None] = ContextVar(
    "diagnostic_account_scopes", default=None
)
_PROBE_CACHE: ContextVar[dict[Path, dict[str, ProbeResult]] | None] = ContextVar(
    "diagnostic_probes", default=None
)


@contextlib.contextmanager
def probe_snapshot() -> Iterator[None]:
    scope_token = _SCOPE_CACHE.set({})
    cache_token = _PROBE_CACHE.set({})
    try:
        yield
    finally:
        _PROBE_CACHE.reset(cache_token)
        _SCOPE_CACHE.reset(scope_token)


@contextlib.contextmanager
def probe_context(context: ProbeContext) -> Iterator[None]:
    token = _PROBE_CONTEXT.set(context)
    scope_token = _RESOLUTION_SCOPE.set(None)
    try:
        # Only account, provider and executable selectors affect entitlement.
        selectors = [
            context.binary,
            context.env.get("PATH"),
            context.env.get("HOME"),
            context.env.get("CLAUDE_CONFIG_DIR"),
            _provider_identity(context.env),
        ]
        key = hashlib.sha256(json.dumps(selectors).encode()).hexdigest()
        cache = _SCOPE_CACHE.get()
        scope = cache.get(key) if cache is not None else None
        if scope is None:
            scope = account_scope()
            if cache is not None:
                cache[key] = scope
        _RESOLUTION_SCOPE.set(scope)
        yield
    finally:
        _RESOLUTION_SCOPE.reset(scope_token)
        _PROBE_CONTEXT.reset(token)


def account_scope() -> str:
    """A stable key for the Claude account/config this machine is currently using.

    Availability is an account fact, so a probe run under one login must never
    answer for another (this workspace routinely runs several config dirs).
    ``CLAUDE_CONFIG_DIR`` is the only account selector Claude Code exposes to
    us; unset means the default ``~/.claude``.
    """
    cached_scope = _RESOLUTION_SCOPE.get()
    if cached_scope is not None:
        return cached_scope
    context = _PROBE_CONTEXT.get()
    if context is not None:
        from aisquare.core import agents, claude_accounts

        resolved = executable_path(context.binary, context.env)
        executable = Path(resolved or context.binary)
        identity: list[object] = ["claude-code", str(executable.resolve()), resolved is not None]
        with contextlib.suppress(OSError):
            stat = executable.stat()
            identity += [stat.st_mtime_ns, stat.st_size]
        selected_account = claude_accounts.default_account(context.env)
        home = selected_account.config_dir
        identity.append(str(home.resolve()))
        account = claude_accounts.identity(selected_account, env=context.env)
        identity.append(account.model_dump() if account else None)
        credentials = (
            None
            if claude_accounts.keychain_platform()
            else claude_accounts.credentials(selected_account)
        )
        identity += [
            credentials.subscription_type if credentials else None,
            credentials.rate_limit_tier if credentials else None,
        ]
        # Login identity, provider routing and explicit credentials affect
        # entitlement. Hook edits, OAuth refreshes and parent-session IDs do not.
        settings = agents._read_settings(home / "settings.json")
        configured_env = settings.get("env", {})
        identity += [
            _provider_identity(configured_env if isinstance(configured_env, dict) else {}),
            settings.get("apiKeyHelper"),
            _provider_identity(context.env),
        ]
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:20]
    raw = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if not raw:
        return "default"
    resolved = str(Path(raw).expanduser())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]


def _provider_identity(env: dict[str, object] | dict[str, str]) -> list[tuple[str, object]]:
    """Account/provider selectors, excluding per-process and tracing variables."""
    keys = {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "AWS_PROFILE",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_ACCESS_KEY_ID",
        "AWS_ROLE_ARN",
        "AWS_BEARER_TOKEN_BEDROCK",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "CLOUD_ML_REGION",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "ANTHROPIC_FOUNDRY_RESOURCE",
        "ANTHROPIC_FOUNDRY_BASE_URL",
        "ANTHROPIC_FOUNDRY_API_KEY",
    }
    return sorted((key, env[key]) for key in keys if key in env)


def _cache_path() -> Path:
    return cache_dir() / f"harness_models.{account_scope()}.json"


def _load_cache() -> dict[str, ProbeResult]:
    path = _cache_path()
    snapshot = _PROBE_CACHE.get()
    if snapshot is not None and path in snapshot:
        return dict(snapshot[path])
    results = _read_cache(path)
    if snapshot is not None:
        snapshot[path] = results
    return dict(results)


def _read_cache(path: Path) -> dict[str, ProbeResult]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    results: dict[str, ProbeResult] = {}
    for alias, item in raw.items():
        try:
            results[alias] = ProbeResult.model_validate(item)
        except ValueError:
            continue
    return results


def _save_cache(cache: dict[str, ProbeResult]) -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {alias: item.model_dump(mode="json") for alias, item in cache.items()}
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        snapshot = _PROBE_CACHE.get()
        if snapshot is not None:
            snapshot[path] = dict(cache)
        _prune_probe_scopes()
    except OSError:
        pass  # the cache is disposable; failing to write it must not surface


def cached_probe(alias: str) -> ProbeResult | None:
    """The fresh cached probe for ``alias``, or ``None`` (missing or stale)."""
    result = _load_cache().get(alias)
    if result is None:
        return None
    checked_at = result.checked_at
    if checked_at.tzinfo is None:
        # A hand-edited cache file can carry a naive stamp; the subtraction
        # below would TypeError and block the launch. Read it as UTC — the
        # worst case is one wasted re-probe, which the disposable cache can
        # always afford.
        checked_at = checked_at.replace(tzinfo=UTC)
    if datetime.now(tz=UTC) - checked_at > CACHE_TTL:
        return None
    return result


def _probe_env() -> dict[str, str]:
    """A minimal environment for the probe child.

    The probe must not become a team member or inherit model overrides: role
    and delta knobs are dropped, the orchestrator is switched off for the
    child, and the model-selection vars that would defeat the probe's whole
    purpose (they can silently redirect the alias) are stripped. Credentials
    and PATH are inherited — the probe has to authenticate as this account.

    It must not inherit a tracing IDENTITY either. This starts a real
    ``claude -p``, so a probe run from inside a traced session would post a
    Run wearing that session's agent name — junk data attributed to a teammate
    who never asked a question. Stripped here rather than left to the proxy's
    junk-run suppression: the traffic is ours not to send in the first place.
    """
    keep = {"AISQUARE_HOME"}  # a relocated tree must stay relocated in the child
    context = _PROBE_CONTEXT.get()
    ambient = spawn.untraced_env(context.env if context else None)
    env = {k: v for k, v in ambient.items() if k in keep or not k.startswith("AISQUARE_")}
    for name in (
        "ANTHROPIC_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL",
        "ANTHROPIC_DEFAULT_FABLE_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    ):
        env.pop(name, None)
    env["AISQUARE_TEAM"] = "0"  # never register the probe as a teammate
    env["CLAUDE_CODE_DISABLE_ADVISOR_TOOL"] = "1"  # a 1-token probe needs no advisor
    return env


def probe_model(alias: str) -> ProbeResult:
    """Ask Claude Code whether ``alias`` genuinely resolves for this account.

    Success requires the probe reply's ``modelUsage`` to contain a model id in
    the alias's family — a plain exit 0 is NOT proof, because an unavailable
    (but known) model is silently substituted with the session default. When
    the reply carries no ``modelUsage`` at all the result is *inconclusive*
    (``available=None`` semantics via ``conclusive=False``), never a demotion:
    an output-format change must not silently downgrade every role.

    The child is isolated: it runs from the aisquare home (never the caller's
    checkout, whose ``SessionStart`` hooks and MCP servers would otherwise
    execute), with settings and MCP config suppressed, one turn, and a
    stripped environment.
    """
    now = datetime.now(tz=UTC)
    family = MODEL_FAMILIES.get(alias)
    home = aisquare_home()
    with contextlib.suppress(OSError):
        home.mkdir(parents=True, exist_ok=True)
    context = _PROBE_CONTEXT.get()
    argv = [
        context.binary if context is not None else "claude",
        "-p",
        "reply with exactly: ok",
        "--model",
        alias,
        "--output-format",
        "json",
        "--settings",
        "{}",
        "--strict-mcp-config",
        "--max-turns",
        "1",
    ]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
            cwd=str(home) if home.is_dir() else None,
            env=_probe_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ProbeResult(
            alias=alias,
            available=False,
            conclusive=False,
            reason=f"probe failed: {exc.__class__.__name__}",
            checked_at=now,
        )
    if completed.returncode != 0:
        # Inconclusive on purpose: an unentitled-but-known model exits 0 and is
        # silently substituted, so a nonzero exit means something else went
        # wrong (outage, expired auth, rate limit, a CLI that rejects one of the
        # isolation flags). Never cache that as unavailability.
        return ProbeResult(
            alias=alias,
            available=False,
            conclusive=False,
            reason="probe did not complete (nonzero exit)",
            checked_at=now,
        )
    try:
        reply = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return ProbeResult(
            alias=alias,
            available=False,
            conclusive=False,
            reason="unparseable probe reply",
            checked_at=now,
        )
    usage = reply.get("modelUsage")
    if not isinstance(usage, dict):
        # No modelUsage in the reply: we cannot tell what ran. Inconclusive —
        # the ladder treats this as "no evidence", not as unavailable.
        return ProbeResult(
            alias=alias,
            available=False,
            conclusive=False,
            reason="reply carried no modelUsage — cannot verify which model ran",
            checked_at=now,
        )
    used = list(usage.keys())
    if family is None:
        # Unknown alias/full id: launching worked, which is all we can assert.
        return ProbeResult(alias=alias, available=True, resolved_id=None, checked_at=now)
    resolved = next((m for m in used if family in m), None)
    if resolved is None:
        return ProbeResult(
            alias=alias,
            available=False,
            reason=f"silently substituted (ran on {', '.join(used) or 'unknown'})",
            checked_at=now,
        )
    return ProbeResult(alias=alias, available=True, resolved_id=resolved, checked_at=now)


_PROBE_NOTICE: ContextVar[Callable[[], None] | None] = ContextVar("probe_notice", default=None)


@contextlib.contextmanager
def probe_notice(notice: Callable[[], None]) -> Iterator[None]:
    token = _PROBE_NOTICE.set(notice)
    try:
        yield
    finally:
        _PROBE_NOTICE.reset(token)


def _probe_and_cache(alias: str) -> ProbeResult:
    notice = _PROBE_NOTICE.get()
    if notice is not None:
        _PROBE_NOTICE.set(None)
        notice()
    result = probe_model(alias)
    if not result.conclusive:
        return result  # never cache "we could not tell" — retry next time
    cache = _load_cache()
    cache[alias] = result
    _save_cache(cache)
    return result


def clear_probe_cache() -> None:
    """Refresh this account; prune expired scopes without spending other logins' probes."""
    directory = cache_dir()
    snapshot = _PROBE_CACHE.get()
    if snapshot is not None:
        snapshot.pop(_cache_path(), None)
    for path in (_cache_path(), directory / "harness_models.json"):
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
    _prune_probe_scopes()


def _prune_probe_scopes() -> None:
    """Normal probe writes collect expired scopes left by upgrades or account changes."""
    directory = cache_dir()
    cutoff = (datetime.now(tz=UTC) - CACHE_TTL).timestamp()
    with contextlib.suppress(OSError):
        for path in directory.glob("harness_models.*.json"):
            with contextlib.suppress(OSError):
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)


def resolve_model(
    role: str,
    *,
    probe: bool | None = None,
    refresh: bool = False,
    effort: str | None = None,
    context: ProbeContext | None = None,
) -> ModelResolution | None:
    """Resolve the model for ``role`` down its ladder; ``None`` for untiered roles.

    ``probe`` overrides the env default (``AISQUARE_HARNESS_PROBE``);
    ``refresh`` ignores cached verdicts so a newly granted entitlement is seen
    immediately. The last
    rung of a ladder is always accepted without proof — resolution never comes
    back empty-handed, and a launch is never blocked.
    """
    if context is not None:
        with probe_context(context):
            return resolve_model(role, probe=probe, refresh=refresh, effort=effort)
    if refresh:
        clear_probe_cache()
    profile = ROLE_PROFILES.get(base_role(role))
    level, effort_source = resolve_effort(role, explicit=effort)
    pinned = role_model_override(role)
    if pinned is not None:
        # An explicit pin works for any role, profiled or not.
        return ModelResolution(
            role=role,
            model=pinned,
            effort=level,
            effort_source=effort_source,
            source="pinned",
        )
    if profile is None:
        return None
    may_probe = probing_enabled() if probe is None else probe
    skipped: list[str] = []
    for index, alias in enumerate(profile.ladder):
        last_rung = index == len(profile.ladder) - 1
        if last_rung:
            return ModelResolution(
                role=role,
                model=alias,
                effort=level,
                effort_source=effort_source,
                source="last-rung" if skipped else "first-rung",
                skipped=skipped,
            )
        cached = None if refresh else cached_probe(alias)
        if cached is not None:
            if cached.available:
                return ModelResolution(
                    role=role,
                    model=alias,
                    effort=level,
                    effort_source=effort_source,
                    source="cached",
                    skipped=skipped,
                )
            skipped.append(alias)
            continue
        if not may_probe:
            return ModelResolution(
                role=role,
                model=alias,
                effort=level,
                effort_source=effort_source,
                source="optimistic",
                skipped=skipped,
            )
        result = _probe_and_cache(alias)
        if result.available:
            return ModelResolution(
                role=role,
                model=alias,
                effort=level,
                effort_source=effort_source,
                source="probed",
                skipped=skipped,
            )
        if not result.conclusive:
            # We could not tell what ran (outage, expired auth, an output-format
            # or CLI change). "No evidence" must never demote a role: keep this
            # rung and say the pick is unverified.
            return ModelResolution(
                role=role,
                model=alias,
                effort=level,
                effort_source=effort_source,
                source="unverified",
                skipped=skipped,
            )
        skipped.append(alias)
    return None  # unreachable: the last rung always returns


def model_mismatch(role: str, model: str | None, *, agent: str | None = None) -> str | None:
    """A warning when a session's captured model does not fit its role's tiering.

    ``None`` when there is nothing to say: untiered role, no capture, or a
    model that belongs to one of the ladder's families.

    Deliberately env-blind: this judges *other* sessions (from the shared
    store, in other processes), so reading this process's
    ``AISQUARE_MODEL_<ROLE>`` would both mis-exempt their sessions and let a
    planted env var silence the board's only tiering signal. A pin that lands
    inside the role's ladder is unflagged anyway; one outside it is worth
    saying out loud.
    """
    if model is None or agent not in (None, "claude-code"):
        return None
    profile = ROLE_PROFILES.get(base_role(role))
    if profile is None:
        return None
    families = [MODEL_FAMILIES[alias] for alias in profile.ladder if alias in MODEL_FAMILIES]
    if not families:
        return None
    if any(family in model for family in families):
        return None
    ladder = "→".join(profile.ladder)
    return f"model {model} outside the {role} ladder ({ladder})"


#: Model ids are ``claude-<family>-<version>``-shaped; anything else is untrusted
#: text from a hook payload and never reaches another session's context.
_MODEL_ID_MAX = 48


def clean_model_id(value: str | None) -> str | None:
    """A self-reported model id, or ``None`` if it isn't plausibly one.

    The value arrives from a hook payload — any process that can run
    ``aisquare hook session-start`` can set it — and is rendered into every
    teammate's injected context, so it is validated rather than escaped:
    a single line, bounded length, and only the characters real model ids use.
    """
    if value is None:
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > _MODEL_ID_MAX:
        return None
    if not all(c.isalnum() or c in "-._[]:" for c in candidate):
        return None
    return candidate


def clean_effort(value: str | None) -> str | None:
    """An effort level, restricted to the levels the harness knows.

    The allowed set is EFFORT_SCALE plus ``ultracode`` — the harness itself
    launches sessions at ``max`` and ``ultracode``, so rejecting them here
    silently dropped the self-report of exactly the sessions it dialled up.
    """
    if value is None:
        return None
    candidate = value.strip().lower()
    return candidate if candidate in {*EFFORT_SCALE, ULTRACODE} else None


def interfering_env() -> list[str]:
    """Env vars set right now that override model selection behind the harness's back."""
    suspects = (
        "ANTHROPIC_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL",
        "ANTHROPIC_DEFAULT_FABLE_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        # A redirected endpoint can answer a probe with any modelUsage it likes,
        # so it undermines availability evidence just as much as a model pin.
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
    )
    return [name for name in suspects if os.environ.get(name, "").strip()]


# --- role work cycles -----------------------------------------------------------
#
# The standing briefing each role receives at session start. These carry the
# harness discipline: tasks are dispatch contracts, verification is adversarial
# and evidence-grounded, nobody guesses, and escalation is explicit. They are
# always-injected context, so every line has to earn its tokens.


#: What each role does INSTEAD when the human asks for work another role owns,
#: as a ``(trigger, instead)`` pair. Only the reader's own pair is emitted — a
#: role never reads the other roles' triggers, so adding a role adds one entry
#: here and rewrites no shared sentence. ``instead`` names a COMMAND with
#: ``{sid}`` where the session id goes, because "don't" without a runnable
#: alternative is what lost to a direct "fix it" in the incident this fixes.
#: ``tester`` shares ``runner``'s by construction (the two cycles must stay
#: byte-identical up to the label — see the manager-loop test that pins it).
_LANE: dict[str, tuple[str, str]] = {
    "planner": (
        "asked to fix or build something yourself",
        'add the tasks (`aisquare task add "<title>" --role coder --detail "<contract>" '
        "--as {sid}`), say how many, and tell the human to prompt each coder tab with "
        '"check the board"',
    ),
    "coder": (
        "asked to verify, review or plan your own work",
        "do your task; verification is the runner's (`aisquare task review <id> --as {sid}`), "
        'planning is the planner\'s (`aisquare note "…" --to planner --as {sid}`)',
    ),
    "runner": (
        "asked to edit or fix the code",
        '`aisquare task reopen <id> --reason "<what failed>" --as {sid}` — the coder fixes, '
        "not you",
    ),
    "validator": (
        "asked to edit or fix the code",
        "findings in your GATE note — the coder fixes, not you",
    ),
    "reviewer": (
        "asked to edit or fix the code",
        "findings on the PR and one board note — the coder fixes, not you",
    ),
    "manager": (
        "asked to write code or fix something yourself",
        "spawn a coder for it (`aisquare fleet spawn coder --task <id> --as {sid}`)",
    ),
    "ui-tester": (
        "asked to edit or fix the code",
        '`aisquare task reopen <id> --reason "<what failed + screenshot path>" --as {sid}` — '
        "the coder fixes, not you",
    ),
}
_LANE["tester"] = _LANE["runner"]


def _lane_rule(role: str, sid: str, *, merge_said: bool) -> list[str]:
    """The closing paragraph of a first-class role's cycle: stay in the role.

    A standing note that only says what a role does loses to a direct
    instruction that asks for something else — measured 2026-09-10, when a
    planner told "get it fixed in the same PR" edited four files and pushed
    while two coders sat on an empty task list. What holds is naming the
    trigger AND the substitute action, and the substitute has to be a command
    that runs: every ``--as`` is pre-filled here exactly as the core cycle
    pre-fills its own. The human can still override — that is theirs to do —
    but never by accident.

    Three lines, because this block is always-injected context and lands last
    in it (the first thing truncated on a busy board). ``merge_said`` skips the
    "Never merge" the role's own cycle already states, so no role reads the
    same prohibition twice. A role with no entry gets no paragraph — never a
    ``KeyError``, which the session-start hook would swallow together with the
    whole team block.
    """
    lane = _LANE.get(role)
    if lane is None:
        return []
    trigger, instead = lane
    closing = (
        "Read-only investigation is always fine; say in one line what you routed and to "
        "whom, and if the human insists, say once which role owns it and offer them the command."
    )
    if not merge_said:
        closing += " Never merge."
    return [
        f"Stay in your lane ({role}). When you are {trigger}, do not do it here.",
        f"Instead: {instead.format(sid=sid)}.",
        closing,
    ]


def role_cycle(role: str, session_short_id: str) -> list[str]:
    """The standing work cycle injected for ``role`` (empty for unknown roles).

    Keyed on :func:`base_role`, so a numbered seat (``coder1``) is briefed as
    the role it is a seat of — the number is an identity, not a new role. Every
    first-class cycle ends with :func:`_lane_rule`; an unknown role has no
    cycle and therefore no lane either. The role is normalised once, here.
    """
    role = base_role(role)
    core = _role_cycle_core(role, session_short_id)
    if not core:
        return []
    merge_said = any("merge" in line.lower() for line in core)
    return [*core, *_lane_rule(role, session_short_id, merge_said=merge_said)]


def _role_cycle_core(role: str, session_short_id: str) -> list[str]:
    """The role-specific half of the cycle for an already-normalised ``role``;
    see :func:`role_cycle`."""
    sid = session_short_id
    if role == "planner":
        return [
            "Your standing cycle (planner): turn intent into contract-carrying tasks —",
            '`aisquare task add "<title>" --role coder|runner --detail "<contract>"` where',
            "the contract states: objective · why it matters / who consumes it · what is",
            "already known or ruled out · acceptance criteria the runner can execute ·",
            "boundaries (what NOT to touch). Re-emitting is safe. Record choices:",
            f'`aisquare note "…" --kind decision --as {sid}`. A task reopened twice is',
            "yours again: re-spec or split it instead of letting it bounce. Anything a user",
            'SEES is a task titled "UI: …" whose acceptance criteria are browser steps — URL,',
            "login, action, expected text/pixels/request — so the ui-tester can run them.",
        ]
    if role == "coder":
        return [
            f"Your standing cycle (coder): `aisquare task next --role coder --claim --as {sid}`;",
            "if nothing is available, tell the user and stop. Read the task's contract and",
            "any reopen feedback first — if the contract is missing or ambiguous, don't",
            f'guess: `aisquare task block <id> --reason "needs spec: …" --as {sid}` and note',
            "it to the planner. Otherwise do the work, self-check against the acceptance",
            "criteria, then `aisquare task review <id> "
            f'--note "how to verify + evidence" --as {sid}`,',
            "and pick up the next one.",
        ]
    if role in ("runner", "tester"):
        # `tester` is the fleet's name for `runner` (docs/plans/fleet-tui.md §3.3):
        # one cycle, two labels, so the two can never drift apart.
        return [
            f"Your standing cycle ({role}): `aisquare task next --status review`; if nothing,",
            "tell the user and stop. You are the adversarial verifier: run the FULL check the",
            "acceptance criteria name — not a smoke test — and try to make the change fail",
            "(edge inputs, a counterexample) before you trust it. Verdicts cite evidence you",
            "produced this session: `aisquare task done <id> "
            f'--note "verified: <evidence>" --as {sid}`,',
            f'or `aisquare task reopen <id> --reason "<what failed + repro>" --as {sid}`.',
            "Criteria missing? Reopen as underspecified — never rubber-stamp. A task titled",
            '"UI: …" belongs to the ui-tester when one is on the board (`aisquare board`) and',
            "NOT marked (stale) — a stale row is a crashed or hung tester, not an available one;",
            "otherwise run its non-browser checks and reopen it —",
            f'`aisquare task reopen <id> --reason "UI not browser-verified" --as {sid}` —',
            "never done. Repeat.",
        ]
    if role == "validator":
        return [
            "Your standing cycle (validator): you gate the assembled deliverable ONCE,",
            "before handoff — not per-task (that is the runner's lane). Read the whole",
            "artifact fresh; restate its acceptance criteria; spot-verify the most",
            "load-bearing claims against reality; hunt internal contradictions and the",
            "stranger question (could someone with no context act on this tomorrow?).",
            'Verdict as a note: `aisquare note "GATE: PASS|PASS-WITH-FIXES|FAIL — …"'
            f" --kind result --as {sid}`,",
            "with findings severity-ordered (critical|major|minor|nit) and evidence per finding.",
        ]
    if role == "manager":
        # docs/plans/fleet-tui.md §7.1 — the planner's contract cycle plus the fleet
        # verbs and the loop protocol. Sub-agents are reached ONLY through the board
        # and `fleet spawn|tell` (§3.3, §7.6): native agent teams are off in fleet
        # launches, and a manager that codes is a coder nobody is steering.
        return [
            "Your standing cycle (manager): you run this project's fleet; you never do its",
            "work. Intake: ask until the acceptance criteria are executable. Contracts:",
            '`aisquare task add "<title>" --role coder --detail "<objective · why · known ·',
            'acceptance · boundaries>"`, `--needs` for ordering. Help comes only from',
            f"`aisquare fleet spawn coder --label coder-<purpose> --task <id> --as {sid}` —",
            "one per parallelisable task, within the agent cap; `fleet spawn tester` once work",
            'reaches review — `fleet spawn ui-tester --prompt "<branch or worktree to check +',
            'the URL to open>"` for tasks titled "UI: …", which it verifies in a real browser',
            "and cannot otherwise place, since nothing moves it into the coder's tree —",
            "`fleet spawn reviewer` once a PR exists, `fleet spawn validator`",
            "once every task is done. Board updates reach you every turn: reopen with reasons,",
            '`aisquare fleet tell <label> "…"` to steer, re-spec or split what bounces. Spawn',
            "nothing while the `fleet-paused` signal is set. When the validator's gate is PASS:",
            f'`aisquare note "READY: <PRs + evidence>" --kind result --as {sid}` and stop.',
            "Never write code. Never merge. Blocked twice on one task? Ask the human:",
            f'`aisquare note "…" --kind question --as {sid}`. Labels are unique and descriptive',
            "(coder-auth, not coder-2).",
        ]
    if role == "ui-tester":
        return [
            f"Your standing cycle (ui-tester): `aisquare task next --status review --as {sid}`;",
            'take tasks titled "UI: …" (leave the rest to the runner). That returns only the',
            "HEAD of the review pool, so if it hands you a non-UI task, find your `UI:` tasks",
            "on the board (`aisquare board`) and act on them by id; if there are none, tell the",
            "user and stop. Verify in a REAL browser with whatever this window has, in this",
            "order: Claude in Chrome (present when the window was started with `--chrome` and",
            "the extension is connected), the Chrome DevTools MCP, Playwright MCP. Check which",
            "of them answer BEFORE you start. Do the acceptance steps as written — URL, login,",
            "action — and MEASURE: screenshots, computed sizes, console errors, network",
            "responses; never pass a visual requirement by reading code. You get no worktree of",
            "your own, so SAY WHICH BUILD you measured — branch or commit, and the URL — in",
            "your verdict; if the task names neither, reopen it as underspecified instead of",
            "screenshotting whatever the root happens to hold. Verdict with evidence:",
            '`aisquare task done <id> --note "verified in <tool> on <branch/commit> at <url>:',
            f'<evidence>" --as {sid}`, or `aisquare task reopen <id> --reason "<what failed> +',
            f"<screenshot path>\" --as {sid}`. No browser tool answers? Run the task's",
            'non-browser checks, then reopen it with "UI not browser-verified in this window" —',
            "never done. You are ASKED to be read-only and nothing here enforces it (no",
            "allowed-tools list is written or passed): never edit, never push. Repeat.",
        ]
    if role == "reviewer":
        # §3.3 — reads the PR as the stranger who will maintain it; findings on the
        # PR through `gh pr review`; read-only (the fleet launches it --restricted).
        return [
            "Your standing cycle (reviewer): `aisquare task next --status review`; if nothing,",
            "tell the user and stop. Read the PR as the stranger who will maintain it —",
            "`gh pr diff`, the task's contract, the tests. You are READ-ONLY: never edit, never",
            "push, never merge. Findings go on the PR (`gh pr review --comment` or",
            "`--request-changes`), severity-ordered (critical|major|minor|nit), each with",
            'evidence, then one summary note: `aisquare note "REVIEW <pr>: …" --kind result',
            f"--as {sid}`. A PR that touches the frontend with no ui-tester evidence on its",
            "task (`aisquare task show <id>`) gets request-changes, not approval. Approve only",
            "what you would merge yourself. Repeat.",
        ]
    return []


# ─── Which BINARY runs a role's agent ────────────────────────────────────────
#
# Orthogonal to the model ladder above: that decides WHAT the agent runs on,
# this decides WHICH executable runs it. People hold several parallel installs
# — `claude`, `claude2`, a wrapper script — and want a role pinned to one
# without retyping a flag on every spawn (#52).

#: The executable used when nothing else says otherwise.
DEFAULT_AGENT_BINARY = "claude"


def is_default_agent(binary: str) -> bool:
    """Whether ``binary`` is Claude Code, so Claude Code's flags apply to it.

    ONE predicate, because two places decide it: the role's ``default_args``
    here and ``--session-id`` pinning in ``explainability.accepts_session_id``,
    which used to carry its own ``"claude"`` literal 55 lines away in the same
    launch.

    Matched on the basename with the Windows shims included and a trailing
    separator tolerated — ``os.path.basename("claude/")`` is ``""``, so a
    binding typed with a slash silently lost the role's flags.

    **A wrapper script literally named ``claude`` is accepted**, deliberately:
    ``~/bin/claude`` and ``/opt/wrap/claude`` are the shape operators actually
    use to add an account or a flag to Claude Code, from here a wrapper is
    indistinguishable from the real thing, and the cost of being wrong is one
    unknown flag on a program the operator named after Claude Code. Anything
    else — ``claude2``, ``claude-next``, ``aider`` — gets nothing.
    """
    from aisquare.core.agent_adapters.types import executable_name

    return executable_name(binary) == DEFAULT_AGENT_BINARY


#: Per-role override, e.g. AISQUARE_BIN_CODER=claude2. Role names are upper-cased
#: and non-alphanumerics become underscores, so `code-reviewer` reads
#: AISQUARE_BIN_CODE_REVIEWER.
_BIN_ENV_PREFIX = "AISQUARE_BIN_"

#: Applies to every role that has no more specific answer.
_BIN_ENV_GLOBAL = "AISQUARE_AGENT_BIN"


def _bin_env_var(role: str) -> str:
    return role_env_key("BIN", role)


class BinaryResolution(BaseModel):
    """Which executable a role launches on, and why that one.

    ``source`` is carried because a matrix that shows the answer without its
    provenance sends the reader hunting through four places to find who won.
    """

    binary: str
    source: str  # flag | env | env:global | config | default


def _team_settings() -> tuple[TeamSettings | None, str | None]:
    """The ``team`` config section, plus a reason if it could not be read.

    The fail-open is scoped deliberately tight: it covers READING the file — a
    broken config must cost a mapping, never a launch — and nothing else.
    Attribute access on the loaded object happens at the call site, outside the
    guard, so a genuine code bug raises instead of quietly resolving to the
    default. A resolver that silently answers "default" when it is broken is
    indistinguishable from one that is working.

    **The reason is RETURNED, never printed here.** No silent fail-soft is a
    standing rule, but ``core`` has no business writing to a terminal, so the
    caller surfaces it (``resolve_profile`` carries it out on the profile and
    the CLI echoes it once). The message names the exception CLASS and text and
    never a config VALUE — a config that fails to parse is exactly where a
    half-written secret is most likely to be sitting.
    """
    try:
        from aisquare.core.config import load_config

        return load_config().team, None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def resolve_binary(role: str, *, override: str | None = None) -> BinaryResolution:
    """Flag > per-role env > global env > the role's profile > default.

    Deliberately does NOT check PATH: resolution answers "what was asked for",
    and the caller reports "it is not there" separately. Fusing them would turn
    a missing binary into a silent fall-back to the default, which runs the
    WRONG AGENT under the right role name — worse than not launching.
    """
    if override:
        return BinaryResolution(binary=override, source="flag")
    per_role = os.environ.get(_bin_env_var(role))
    if per_role:
        return BinaryResolution(binary=per_role, source="env")
    everywhere = os.environ.get(_BIN_ENV_GLOBAL)
    if everywhere:
        return BinaryResolution(binary=everywhere, source="env:global")
    # The reason is dropped HERE on purpose: `resolve_profile` reads the same
    # config and carries it out, and every caller of this function calls that
    # one too — surfacing it in both would print the same warning twice.
    team, _ = _team_settings()
    profile = team.profiles.get(role) if team is not None else None
    if profile is not None and profile.bin:
        return BinaryResolution(binary=profile.bin, source="config")
    return BinaryResolution(binary=DEFAULT_AGENT_BINARY, source="default")


# ─── The LAUNCH PROFILE: env and args a role runs with ───────────────────────
#
# The third axis, and deliberately the DUMBEST one. The ladder decides what
# model a role runs on and ``resolve_binary`` decides which executable runs it;
# this carries whatever else the operator wants on the command, verbatim.
#
# It is deliberately ignorant. An earlier cut of this understood "accounts" and
# expanded a name like ``claude2`` into ``~/.claude2`` plus ``~/.cache/claude2``
# — which is one operator's directory convention baked into a tool that has no
# business knowing it. Anyone laid out differently could not use the feature at
# all, and the one person it fit would have it break the day they reorganised.
#
# So there is no notion of an account here, and no inferred path. The operator
# states the env and the args; we expand ``~`` and ``$VAR`` so a value reads
# exactly like the shell line it replaces, and pass it through. The common case
# — parallel agent installs reached through aliases —
#
#     alias claude2='CLAUDE_CONFIG_DIR="$HOME/.claude2" \
#                    CLAUDE_CODE_TMPDIR="$HOME/.cache/claude2" command claude'
#
# is then just two env entries the operator writes once per role, and any other
# knob (a proxy, a region, a wrapper's own vars) works the same way without
# this file learning a thing about it.


class LaunchProfile(BaseModel):
    """What a role adds to its launch: env, extra args, and where each came from."""

    env: dict[str, str] = {}
    args: list[str] = []
    #: Per-env-key provenance — ``flag`` or ``config``. Carried for the same
    #: reason the other two axes carry it: an answer without its source sends
    #: the reader hunting for who won.
    env_sources: dict[str, str] = {}
    #: Why the configured bindings could not be read, when they could not be.
    #: A launch still proceeds unbound — but silently proceeding would put a
    #: seat on the DEFAULT install while reporting success, so the caller MUST
    #: surface this. See the no-silent-fail-soft rule.
    notice: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.env and not self.args


def expand_value(value: str) -> str:
    """``~`` and ``$VAR`` expansion, so config reads like the shell it replaces.

    Order matters: ``$VAR`` first, because ``$HOME/.claude2`` must become a
    real path before ``~`` expansion would have nothing to do. An undefined
    ``$VAR`` is left verbatim by ``expandvars`` rather than becoming empty —
    a silently blank CLAUDE_CONFIG_DIR would start a fresh unauthenticated
    profile, which is exactly the failure this whole area exists to avoid.
    """
    return os.path.expanduser(os.path.expandvars(value))


def parse_env_pairs(pairs: Sequence[str]) -> dict[str, str]:
    """``["K=V", ...]`` -> ``{"K": "V"}``.

    Splits on the FIRST ``=`` only, so a value may contain more of them. A pair
    with no ``=`` is a usage error the caller reports; returning it silently
    dropped would leave the operator convinced they had set something.
    """
    parsed: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ValueError(f"expected KEY=VALUE, got {pair!r}")
        parsed[key] = value
    return parsed


def resolve_profile(
    role: str,
    *,
    env_overrides: dict[str, str] | None = None,
    extra_args: Sequence[str] | None = None,
) -> LaunchProfile:
    """Merge the role's configured profile with this launch's overrides.

    Env merges PER KEY — a flag can override one variable without discarding
    the rest of the role's profile, which is what makes a one-off tweak cheap.
    Args APPEND rather than replace, because the configured ones are the role's
    standing shape and the caller's are additions to it.
    """
    env: dict[str, str] = {}
    sources: dict[str, str] = {}
    args: list[str] = []
    team, unreadable = _team_settings()
    profile = (team.profiles or {}).get(role) if team is not None else None
    if profile is not None:
        for key, value in (profile.env or {}).items():
            env[key] = expand_value(value)
            sources[key] = "config"
        args.extend(profile.args or [])
    for key, value in (env_overrides or {}).items():
        env[key] = expand_value(value)
        sources[key] = "flag"
    args.extend(extra_args or [])
    return LaunchProfile(env=env, args=args, env_sources=sources, notice=unreadable)
