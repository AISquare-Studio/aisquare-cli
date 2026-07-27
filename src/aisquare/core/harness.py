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
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from aisquare.core.paths import aisquare_home

_OFF_VALUES = {"0", "false", "no", "off"}
PROBE_TIMEOUT_SECONDS = 150
CACHE_TTL = timedelta(hours=24)

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
}


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


def role_effort_override(role: str) -> str | None:
    """The ``AISQUARE_EFFORT_<ROLE>`` pin for ``role`` — absolute, offset not applied."""
    key = "AISQUARE_EFFORT_" + "".join(c if c.isalnum() else "_" for c in role.upper())
    return os.environ.get(key, "").strip() or None


def base_effort() -> tuple[str, str]:
    """The session's base effort and where it came from.

    Precedence: ``AISQUARE_EFFORT`` (the harness knob) > ``CLAUDE_EFFORT`` (what
    the launching Claude session is actually running at, exported by Claude Code)
    > :data:`DEFAULT_BASE_EFFORT`. So raising your own session to xhigh raises the
    fleet you spawn from it, without configuring anything.
    """
    for name, source in (("AISQUARE_EFFORT", "env"), ("CLAUDE_EFFORT", "inherited")):
        raw = os.environ.get(name, "").strip()
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
    profile = ROLE_PROFILES.get(role)
    offset = profile.effort_offset if profile else 0
    wants_ultracode = os.environ.get("AISQUARE_EFFORT", "").strip().lower() == ULTRACODE
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
    return os.environ.get("AISQUARE_HARNESS_PROBE", "").strip().lower() not in _OFF_VALUES


def role_model_override(role: str) -> str | None:
    """The ``AISQUARE_MODEL_<ROLE>`` pin for ``role``, if set."""
    key = "AISQUARE_MODEL_" + "".join(c if c.isalnum() else "_" for c in role.upper())
    value = os.environ.get(key, "").strip()
    return value or None


def account_scope() -> str:
    """A stable key for the Claude account/config this machine is currently using.

    Availability is an account fact, so a probe run under one login must never
    answer for another (this workspace routinely runs several config dirs).
    ``CLAUDE_CONFIG_DIR`` is the only account selector Claude Code exposes to
    us; unset means the default ``~/.claude``.
    """
    raw = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if not raw:
        return "default"
    resolved = str(Path(raw).expanduser())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]


def _cache_path() -> Path:
    return aisquare_home() / "cache" / f"harness_models.{account_scope()}.json"


def _load_cache() -> dict[str, ProbeResult]:
    try:
        raw = json.loads(_cache_path().read_text(encoding="utf-8"))
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
    except OSError:
        pass  # the cache is disposable; failing to write it must not surface


def cached_probe(alias: str) -> ProbeResult | None:
    """The fresh cached probe for ``alias``, or ``None`` (missing or stale)."""
    result = _load_cache().get(alias)
    if result is None:
        return None
    if datetime.now(tz=UTC) - result.checked_at > CACHE_TTL:
        return None
    return result


def _probe_env() -> dict[str, str]:
    """A minimal environment for the probe child.

    The probe must not become a team member or inherit model overrides: role
    and delta knobs are dropped, the orchestrator is switched off for the
    child, and the model-selection vars that would defeat the probe's whole
    purpose (they can silently redirect the alias) are stripped. Credentials
    and PATH are inherited — the probe has to authenticate as this account.
    """
    keep = {"AISQUARE_HOME"}  # a relocated tree must stay relocated in the child
    env = {k: v for k, v in os.environ.items() if k in keep or not k.startswith("AISQUARE_")}
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
    argv = [
        "claude",
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


def _probe_and_cache(alias: str) -> ProbeResult:
    result = probe_model(alias)
    if not result.conclusive:
        return result  # never cache "we could not tell" — retry next time
    cache = _load_cache()
    cache[alias] = result
    _save_cache(cache)
    return result


def clear_probe_cache() -> None:
    """Forget every cached availability verdict (``spawn --refresh``)."""
    with contextlib.suppress(OSError):
        _cache_path().unlink(missing_ok=True)


def resolve_model(
    role: str,
    *,
    probe: bool | None = None,
    refresh: bool = False,
    effort: str | None = None,
) -> ModelResolution | None:
    """Resolve the model for ``role`` down its ladder; ``None`` for untiered roles.

    ``probe`` overrides the env default (``AISQUARE_HARNESS_PROBE``);
    ``refresh`` ignores cached verdicts so a newly granted entitlement is seen
    immediately. The last
    rung of a ladder is always accepted without proof — resolution never comes
    back empty-handed, and a launch is never blocked.
    """
    profile = ROLE_PROFILES.get(role)
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


def model_mismatch(role: str, model: str | None) -> str | None:
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
    if model is None:
        return None
    profile = ROLE_PROFILES.get(role)
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
    """An effort level, restricted to the levels the harness knows."""
    if value is None:
        return None
    candidate = value.strip().lower()
    return candidate if candidate in {"low", "medium", "high", "xhigh"} else None


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


def role_cycle(role: str, session_short_id: str) -> list[str]:
    """The standing work cycle injected for ``role`` (empty for unknown roles)."""
    sid = session_short_id
    if role == "planner":
        return [
            "Your standing cycle (planner): turn intent into contract-carrying tasks —",
            '`aisquare task add "<title>" --role coder|runner --detail "<contract>"` where',
            "the contract states: objective · why it matters / who consumes it · what is",
            "already known or ruled out · acceptance criteria the runner can execute ·",
            "boundaries (what NOT to touch). Re-emitting is safe. Record choices:",
            f'`aisquare note "…" --kind decision --as {sid}`. A task reopened twice is',
            "yours again: re-spec or split it instead of letting it bounce.",
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
    if role == "runner":
        return [
            "Your standing cycle (runner): `aisquare task next --status review`; if nothing,",
            "tell the user and stop. You are the adversarial verifier: run the FULL check the",
            "acceptance criteria name — not a smoke test — and try to make the change fail",
            "(edge inputs, a counterexample) before you trust it. Verdicts cite evidence you",
            "produced this session: `aisquare task done <id> "
            f'--note "verified: <evidence>" --as {sid}`,',
            f'or `aisquare task reopen <id> --reason "<what failed + repro>" --as {sid}`.',
            "Criteria missing? Reopen as underspecified — never rubber-stamp. Repeat.",
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
    return []
