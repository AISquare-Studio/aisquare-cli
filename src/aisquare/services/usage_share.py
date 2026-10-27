"""Opt-in anonymous usage sharing: the batch, the shipment, the switch and the consent.

What is shared, and when, is ``docs/usage-telemetry.md`` (the wire contract and, in plain words,
what is and is never collected). This module is its client:

- :func:`build_batch` turns the local daily rollups (``services.usage_stats.daily_rollups``)
  into the contract's body: today, and every day of the last 31 not yet acknowledged, oldest
  first. Repositories are sent as ``HMAC-SHA256(per-install salt, remote or root)[:16]`` with
  four coarse traits and nothing else; commands and pages as counts of their NAMES. The one
  function builds what :func:`preview` shows and what :func:`ship` posts, so they cannot differ.
- :func:`ship` posts it, off every hot path (``aisquare metrics ship`` in a detached process, or
  a TUI worker): 5 s timeouts, the AISquare sign-in as a bearer when there is one and a retry
  once without it on a 401, a backoff on any failure, the days acknowledged only when the server
  says 200. A failed shipment loses nothing: the days are rebuilt from the local record and sent
  again, and the server upserts by (install, day).
- :func:`enable` and :func:`disable` flip the switch. Off purges what was kept of the shipments
  and sends the one ``opt_out`` ping, best effort; nothing is sent after it, and nothing at all
  while sharing is off or forced off (``core.sharing.forced_off_reason``).

The transport is a seam (:data:`Transport`): the tests give :func:`ship` a fake, and no test
reaches a server. The endpoint (``/api/v1/cli/metrics/`` of ``AISquare-Studio-BE``) is the
contract's.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import platform
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any

from aisquare.core import locking, repo_profile, sharing
from aisquare.core.sharing import SharingState
from aisquare.core.store import ContextStore
from aisquare.core.version import __version__
from aisquare.models import DailyRollup, SharingStatus, ShipReport
from aisquare.services import iam, install_route, usage_stats
from aisquare.services import settings as settings_service

SCHEMA_VERSION = 1
log = logging.getLogger("aisquare.usage_share")
ENDPOINT_PATH = "/api/v1/cli/metrics/"
WINDOW_DAYS = 31
"""A batch holds at most this many days: today and the 30 before."""
REJECTED_IN_A_ROW = 3
"""A shipment goes on past a batch the server refused (a 4xx that is not a throttle), but gives up
on the older ones for now after this many in a row: a server that refuses everything is not asked
14 times. The newest batch, with today, is still tried: the oldest days are the ones a server is
most likely to refuse, and they must not keep today from going."""
MAX_BATCHES = 14
"""Batches one shipment sends: 14 of 31 days reach back the 400 days the server takes."""
MAX_FEATURES = 200
MAX_REPOS = 25
MAX_BODY_BYTES = 250_000
"""The contract caps a body at 256 KB; a batch past this drops its oldest days (they go next)."""
TIMEOUT_S = 5.0
DISABLE_WAIT_S = 6.0
"""How long turning sharing off waits for a shipment in flight to finish (one POST, 5 s) before it
purges what was kept: the shipment re-checks the switch before every POST and stops."""
BY_TYPE_KEYS = frozenset({"pro", "max", "team", "enterprise", "api", "unknown"})
"""The plans a shipment names (contract v1.1); any other is counted as ``other``."""
PLACEHOLDER_ID = "00000000-0000-4000-8000-000000000000"
PLACEHOLDER_SALT = "00" * 32
"""A sample is hashed with this, so that two previews of one history agree."""

COVERAGE = (
    "Usage is counted for every repo you run Claude Code in, including ones you haven't added to "
    "aisquare; capture settings don't change this."
)
"""Said wherever sharing is switched on (the consent dialog, the Settings switch, ``metrics share
on``): the counts are not limited to the projects aisquare knows."""

_MODEL_ID = re.compile(r"^claude-[a-z0-9.-]{1,60}$")
_FEATURE = re.compile(r"^[a-z0-9_.-]{1,64}$")
_OS_VERSION = re.compile(r"^\d+(\.\d+)?$")
_WORD = re.compile(r"^[a-z0-9_]{1,16}$")
_PYTHON = re.compile(r"^\d+\.\d+\.\d+")
_SALT = re.compile(r"^[0-9a-f]{64}$")

Transport = Callable[[str, dict[str, Any], str | None, float], iam.HttpResult]
"""``(url, body, bearer token or None, timeout) -> result``: how a body reaches the server."""


def default_transport(
    url: str, body: dict[str, Any], token: str | None, timeout: float
) -> iam.HttpResult:
    return iam.post_json(url, body, token=token, timeout=timeout)


class SharingForcedOff(Exception):
    """Sharing cannot be turned on: ``DO_NOT_TRACK``, ``AISQUARE_NO_TELEMETRY`` or ``CI``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --- the batch ----------


@dataclass(frozen=True)
class Batch:
    """The body a shipment posts, and the digest of each day in it (what is acknowledged)."""

    body: dict[str, Any]
    digests: dict[str, str]

    @property
    def days(self) -> list[dict[str, Any]]:
        days: list[dict[str, Any]] = self.body["days"]
        return days


def repo_hash(salt: str, key: str) -> str:
    """The first 16 hex of ``HMAC-SHA256(salt, key)``: how a repository is named in a batch. The
    salt is this installation's own, so the same repository hashes differently for each user."""
    return hmac.new(bytes.fromhex(salt), key.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def install_method() -> str:
    """``brew``, ``pipx``, ``uv``, ``pip``, ``script`` (the one-line installer's uv tool) or
    ``unknown`` (a checkout)."""
    try:
        kind = install_route.detect().kind
    except Exception:
        return "unknown"
    return {
        install_route.HOMEBREW: "brew",
        install_route.PIPX: "pipx",
        install_route.UVX: "uv",
        install_route.UV_TOOL: "script",
        install_route.VENV: "pip",
        install_route.SYSTEM: "pip",
    }.get(kind, "unknown")


def client_info() -> dict[str, str]:
    """The installation's make and model: version, platform, architecture, interpreter. Each is
    matched against the shape it has: a kernel release that carries a host or a person's name
    (``6.8-alice-laptop``) is ``unknown``, not sent."""
    release = ".".join(platform.release().split(".")[:2])
    system, machine = platform.system().lower(), platform.machine().lower()
    python = _PYTHON.match(platform.python_version())
    return {
        "cli_version": __version__,
        "os": system if _WORD.match(system) else "unknown",
        "os_version": release if _OS_VERSION.match(release) else "unknown",
        "arch": machine if _WORD.match(machine) else "unknown",
        "python": python.group(0) if python else "unknown",
        "install_method": install_method(),
    }


def _language(name: str | None) -> str:
    """A repository's main language if it is one the profile can name, else ``unknown``."""
    return name if name in repo_profile.KNOWN_LANGUAGES else "unknown"


def _tally(counts: dict[str, int]) -> dict[str, int]:
    """``counts`` keyed by plan, in the closed set of :data:`BY_TYPE_KEYS`: any other key is a
    free text from a file and is summed under ``other``."""
    out: dict[str, int] = {}
    for kind, number in counts.items():
        key = kind if kind in BY_TYPE_KEYS else "other"
        out[key] = out.get(key, 0) + number
    return dict(sorted(out.items()))


def _models(models: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Per-model token counts keyed by a Claude model id (``^claude-[a-z0-9.-]{1,60}$``); a
    model that is anything else is summed under ``other`` (contract v1.1)."""
    out: dict[str, dict[str, int]] = {}
    for name, counts in models.items():
        key = name if _MODEL_ID.match(name) else "other"
        kinds = out.setdefault(key, {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0})
        for kind, number in counts.model_dump().items():
            kinds[kind] += number
    return dict(sorted(out.items()))


def day_body(rollup: DailyRollup, salt: str, *, signed_in: bool) -> dict[str, Any]:
    """One local day in the contract's shape. Repositories become hashes; of everything the
    rollup holds that names something of the user's, nothing remains."""
    features = sorted(
        ((name, n) for name, n in rollup.features.items() if _FEATURE.match(name)),
        key=lambda item: (-item[1], item[0]),
    )
    kept = dict(sorted(features[:MAX_FEATURES]))
    return {
        "date": rollup.date,
        "tz_offset_minutes": rollup.tz_offset_minutes,
        "active": True,
        "signed_in": signed_in,
        "tokens": rollup.tokens.model_dump(),
        "cost_usd_list": round(rollup.cost_usd_list, 4),
        "sessions": rollup.sessions,
        "turns": rollup.turns,
        "models": _models(rollup.models),
        "accounts": {
            "active": rollup.accounts.active,
            "registered": rollup.accounts.registered,
            "by_type": _tally(rollup.accounts.by_type),
            "top_account_share": rollup.accounts.top_account_share,
        },
        "repos_active": rollup.repos_active,
        "repos": [
            {
                "repo_hash": repo_hash(salt, repo.key),
                "tokens_total": repo.tokens_total,
                "sessions": repo.sessions,
                "turns": repo.turns,
                "traits": {
                    "host": repo.traits.host,
                    "language": _language(repo.traits.language),
                    "size_bucket": repo.traits.size_bucket,
                    "monorepo": repo.traits.monorepo,
                },
            }
            for repo in rollup.repos[:MAX_REPOS]
        ],
        "features": kept,
        "fleet": {
            "agents_spawned": dict(sorted(rollup.fleet.agents_spawned.items())),
            "tasks_done": rollup.fleet.tasks_done,
        },
    }


def _digest(day: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(day, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _sent_at_stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_today(moment: datetime, tz: tzinfo | None) -> date:
    return moment.astimezone(tz).date()


def build_batches(
    *,
    now: datetime | None = None,
    tz: tzinfo | None = None,
    store: ContextStore | None = None,
    state: SharingState | None = None,
    identity: tuple[str, str] | None = None,
    sample: bool | None = None,
) -> list[Batch]:
    """Everything that is to be sent, in batches of at most 31 days and :data:`MAX_BODY_BYTES`,
    oldest first: each day since sharing was turned on that the server does not have as it
    stands now, and today (which is sent every time, as it grows). The last batch ends with
    today. Empty when there is nothing to send.

    ``sample`` builds what the batches WOULD be had sharing been turned on a month ago: for
    showing a person what is sent before they agree to it (it is the default unless a shipment
    would send exactly this: see :func:`sample_reason`). A real shipment is never a sample.

    ``identity`` is ``(install id, salt)``; without it the installation's own is read, and one
    sharing never minted gets placeholders (nothing is stored by building a batch)."""
    moment = now or datetime.now(UTC)
    held = state if state is not None else sharing.read()
    if sample is None:
        sample = _sample_reason(held) is not None
    if identity is None:
        identity = _held_identity(held) or (PLACEHOLDER_ID, PLACEHOLDER_SALT)
    install_id, salt = identity
    today = _local_today(moment, tz)
    first = _first_day(held, today, sample=sample)
    settled = held.settled_through or ""
    withheld = frozenset(held.withheld)
    rollups = usage_stats.daily_rollups(first, today, now=moment, tz=tz, store=store)
    signed_in = iam.signed_in_quietly() is not None
    wanted: list[tuple[str, dict[str, Any], str]] = []
    for rollup in rollups:
        if rollup.date <= settled or rollup.date in withheld:
            continue
        body = day_body(rollup, salt, signed_in=signed_in)
        digest = _digest(body)
        if rollup.date == today.isoformat() or held.acked.get(rollup.date) != digest[:12]:
            wanted.append((rollup.date, body, digest))
    return _chunks(moment, install_id, wanted)


def _first_day(held: SharingState, today: date, *, sample: bool) -> date:
    """The first day a shipment looks at: the day sharing was turned on, or at most 399 days
    back (the server takes dates back to today minus 400); a sample, 30 days back."""
    if sample:
        return today - timedelta(days=WINDOW_DAYS - 1)
    oldest = today - timedelta(days=sharing.MAX_BACKLOG_DAYS)
    try:
        since = date.fromisoformat(held.since) if held.since else today
    except ValueError:
        since = today
    return max(since, oldest)


def _chunks(
    moment: datetime, install_id: str, wanted: list[tuple[str, dict[str, Any], str]]
) -> list[Batch]:
    """``wanted`` (date, day, digest), oldest first, cut into bodies of at most
    :data:`WINDOW_DAYS` days that fit :data:`MAX_BODY_BYTES`."""
    batches: list[Batch] = []
    days: list[dict[str, Any]] = []
    digests: dict[str, str] = {}
    size = len(json.dumps(_batch(moment, install_id, [])))
    for date_text, body, digest in wanted:
        cost = len(json.dumps(body)) + 2
        if days and (len(days) >= WINDOW_DAYS or size + cost > MAX_BODY_BYTES):
            batches.append(Batch(_batch(moment, install_id, days), digests))
            days, digests, size = [], {}, len(json.dumps(_batch(moment, install_id, [])))
        days.append(body)
        digests[date_text] = digest
        size += cost
    if days:
        batches.append(Batch(_batch(moment, install_id, days), digests))
    return batches


def build_batch(**kwargs: Any) -> Batch:
    """The body the next POST would carry: the first of :func:`build_batches`, or a body with no
    days when there is nothing to send."""
    batches = build_batches(**kwargs)
    if batches:
        return batches[0]
    moment = kwargs.get("now") or datetime.now(UTC)
    held = kwargs.get("state") or sharing.read()
    identity = kwargs.get("identity") or _held_identity(held) or (PLACEHOLDER_ID, "")
    install_id = identity[0]
    return Batch(_batch(moment, install_id, []), {})


def _batch(moment: datetime, install_id: str, days: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "install_id": install_id,
        "sent_at": _sent_at_stamp(moment),
        "opt_out": False,
        "client": client_info(),
        "days": list(days),
    }


def _sample_reason(held: SharingState) -> str | None:
    if not sharing.switch_on():
        return "sharing is off"
    forced = sharing.forced_off_reason()
    if forced is not None:
        return f"sharing is off: {forced}"
    if _held_identity(held) is None:
        return "this installation has no id yet, as when sharing is switched on by hand"
    return None


def sample_reason() -> str | None:
    """Why :func:`preview` shows a SAMPLE (the last 31 days with placeholder ids) and not the body
    of the next POST, in words; ``None`` when it IS that body: sharing is on, nothing forces it
    off, and the installation has its id and salt. Building a body stores nothing, so an
    installation sharing was switched on for by hand (``config set sharing.enabled true``) that
    has no id yet is a sample until its first shipment mints one."""
    return _sample_reason(sharing.read())


def is_sample() -> bool:
    """Whether :func:`preview` is a SAMPLE (see :func:`sample_reason`)."""
    return sample_reason() is not None


def preview(*, sweep: bool = True, **kwargs: Any) -> str:
    """The body the next POST would carry, as the JSON it is: with sharing on, exactly it (built
    by the same steps as a shipment, the transcripts not read for ten minutes read first); with
    it off, a sample of the last 31 days (nothing before the day it is turned on is ever sent)."""
    if sweep:
        _sweep_first(kwargs.get("now") or datetime.now(UTC))
    return json.dumps(build_batch(**kwargs).body, indent=2)


# --- identity ----------


def _valid_id(text: str | None) -> bool:
    if not text:
        return False
    try:
        return uuid.UUID(text).version == 4
    except ValueError:
        return False


def _valid_salt(text: str | None) -> bool:
    return bool(text) and _SALT.match(text or "") is not None


def _held_identity(state: SharingState) -> tuple[str, str] | None:
    """The installation's ``(install id, salt)`` from its state, if both are what they should be."""
    if state.install_id and state.salt and _valid_id(state.install_id) and _valid_salt(state.salt):
        return state.install_id, state.salt
    return None


def ensure_identity() -> tuple[str, str]:
    """This installation's ``(install id, salt)``, minted at the first opt-in and kept after. A
    part of the state file that is not what it should be (a damaged or hand-edited id or salt)
    is minted again, quietly: a salt that is not 64 hex digits would otherwise fail every
    repository hash."""

    def mint(state: SharingState) -> None:
        if not _valid_id(state.install_id):
            state.install_id = str(uuid.uuid4())
        if not _valid_salt(state.salt):
            state.salt = os.urandom(32).hex()

    state = sharing.update(mint)
    assert state.install_id is not None and state.salt is not None
    return state.install_id, state.salt


def reset_identity(*, now: datetime | None = None) -> tuple[str, str]:
    """A new install id and salt: what was sent before is no longer this installation's."""

    moment = now or datetime.now(UTC)

    def fresh(state: SharingState) -> None:
        state.install_id = str(uuid.uuid4())
        state.salt = os.urandom(32).hex()
        state.acked = {}
        state.settled_through = None
        state.since = moment.astimezone().date().isoformat()  # not what went before
        state.linked = None
        state.last_sent_at = None
        state.withheld = []

    state = sharing.update(fresh)
    assert state.install_id is not None and state.salt is not None
    return state.install_id, state.salt


# --- the shipment ----------


def endpoint() -> str:
    """Where batches go: the sign-in's API plus the contract's path."""
    return f"{iam.resolve_api_url()}{ENDPOINT_PATH}"


def _bearer(api_url: str) -> str | None:
    """The sign-in's token when there is one for THIS api: a session from another deployment is
    never sent here."""
    try:
        session = iam.current_session(api_url)
    except iam.IamError:
        return None
    if session is None or (session.source == "file" and session.api_url != api_url):
        return None
    return session.token


def _still_on() -> bool:
    """Whether sharing is on and nothing forces it off, asked again before a POST and before
    anything is recorded: turning it off stops a shipment already under way."""
    return sharing.forced_off_reason() is None and sharing.switch_on()


def _sweep_first(moment: datetime) -> None:
    """Read the transcripts nothing has read for ten minutes, so that what is previewed and what
    is shipped are built from the same record: one entry point for both."""
    with contextlib.suppress(Exception):
        usage_stats.maybe_sweep(now=moment)


def ship(
    *,
    now: datetime | None = None,
    tz: tzinfo | None = None,
    store: ContextStore | None = None,
    transport: Transport | None = None,
    force: bool = False,
    sweep: bool = True,
) -> ShipReport:
    """Post what is waiting, unless sharing is off or forced off, a shipment is not due (``force``
    ignores that), another one is running, or there is nothing to send. Never raises, and never
    makes a sound about failing: the days stay unsent and go next time, however long the server
    takes to answer (the report says what happened, for ``--json`` and the tests; nothing else
    reads it aloud). Turning sharing off stops it: the switch is looked at again before every
    POST. ``sweep`` reads the transcripts nothing has read for ten minutes first, as
    :func:`preview` does."""
    reason = sharing.forced_off_reason()
    if reason is not None:
        if sharing.switch_on():  # opted in, and forced off today: this day is never sent
            sharing.withhold_today(now, tz)
        return ShipReport(skipped=f"sharing is off: {reason}")
    if not sharing.switch_on():
        return ShipReport(skipped="sharing is off")
    moment = now or datetime.now(UTC)
    try:
        with locking.holding(sharing.lock_path(), wait_s=0):
            return _ship(moment, tz, store, transport or default_transport, force, sweep)
    except TimeoutError:
        return ShipReport(skipped="another shipment is running")
    except Exception as exc:  # a shipment may fail; it may not raise into whoever asked
        log.debug("usage shipment failed: %r", exc)
        return ShipReport(error=f"{type(exc).__name__}: {exc}"[:200])


def _accepted(result: iam.HttpResult) -> tuple[int, bool] | None:
    """``(accepted_days, linked)`` of a reply that is the contract's 200, else ``None``.

    Only that is "the server has it": a 200 with any other body (an empty object, an error with
    a 200, a gateway's page, ``accepted_days`` of the wrong type or 0) is not an answer of the
    endpoint, and acknowledging it would drop the days for good."""
    body = result.body
    if result.status != 200 or not isinstance(body, dict):
        return None
    days, linked = body.get("accepted_days"), body.get("linked")
    if type(days) is not int or days < 1 or type(linked) is not bool:
        return None
    return days, linked


def _ship(
    moment: datetime,
    tz: tzinfo | None,
    store: ContextStore | None,
    post: Transport,
    force: bool,
    sweep: bool,
) -> ShipReport:
    state = sharing.read()
    if not force and not sharing.due(state, moment):
        return ShipReport(skipped=f"not due until {state.next_due_at}")
    if sweep:
        _sweep_first(moment)
    identity = ensure_identity()
    state = _begin(moment, tz)
    batches = build_batches(now=moment, tz=tz, store=store, state=state, identity=identity)
    if not batches:
        _schedule(moment)
        return ShipReport(skipped="nothing to send")
    try:
        url = endpoint()
    except iam.IamError as exc:
        return _failed(moment, str(exc))
    token = _bearer(iam.resolve_api_url())
    report = ShipReport()
    refused_in_a_row = 0
    held_from: str | None = None  # the oldest day the server refused: nothing from it is settled
    first_refusal: str | None = None
    pending = batches[:MAX_BATCHES]  # oldest first
    while pending:
        batch = pending.pop(0)
        if not _still_on():  # turned off since the last POST: nothing more goes
            report.skipped = "sharing was turned off"
            return report
        try:
            result = post(url, batch.body, token, TIMEOUT_S)
            if result.status == 401 and token is not None:
                # A token the server will not take must not cost the shipment: it is accepted
                # anonymously, and the rest of this shipment goes the same way. The days stay
                # unlinked until the user signs in again.
                token, report.retried_anonymously = None, True
                result = post(url, batch.body, None, TIMEOUT_S)
        except iam.IamError as exc:
            return _failed(moment, str(exc), report=report)
        answer = _accepted(result)
        if answer is not None:
            if not _still_on():  # it went off while this was in flight: nothing is recorded
                report.skipped = "sharing was turned off"
                return report
            accepted, linked = answer
            _acknowledge(moment, batch, linked, held_from)
            report.sent, report.linked = True, linked
            report.days += accepted
            refused_in_a_row = 0
            continue
        what = (
            "HTTP 200: not the contract's reply"
            if result.status == 200
            else f"HTTP {result.status}"
        )
        if _refused(result):
            # The server looked at this batch and said no, for good: that is about these days,
            # not about the server. The newer batches (and today) still go; these stay unsent
            # and are asked again next time.
            first_refusal = first_refusal or f"{what}{_detail(result)}"
            report.rejected_days += len(batch.digests)
            oldest = min(batch.digests)
            held_from = oldest if held_from is None else min(held_from, oldest)
            refused_in_a_row += 1
            if refused_in_a_row == REJECTED_IN_A_ROW:
                del pending[:-1]  # the rest wait for next time; the newest, with today, still goes
            continue
        wait = result.retry_after() if result.status == 429 else None
        return _failed(moment, f"{what}{_detail(result)}", retry_after=wait, report=report)
    if first_refusal is not None:
        if not report.sent:
            return _failed(moment, first_refusal, report=report)
        _note_refusal(first_refusal)  # a debug line in the state; the shipment itself went
    return report


def _refused(result: iam.HttpResult) -> bool:
    """Whether the server answered a batch with a refusal that is about the batch: a 4xx that is
    not a throttle (429) or a timeout (408, 425), which are the server's moment, not the days'."""
    return 400 <= result.status < 500 and result.status not in (408, 425, 429)


def _note_refusal(error: str) -> None:
    def record(state: SharingState) -> None:
        state.last_error = f"{error} (some days refused, sent again next time)"[:200]

    if _still_on():
        with contextlib.suppress(Exception):
            sharing.update(record)


def _begin(moment: datetime, tz: tzinfo | None) -> SharingState:
    """The state, with the first day shared set if a switch turned on by hand left it unset."""

    def first(state: SharingState) -> None:
        if state.since is None:
            state.since = _local_today(moment, tz).isoformat()

    if _still_on():
        with contextlib.suppress(Exception):
            return sharing.update(first)
    return sharing.read()


def _detail(result: iam.HttpResult) -> str:
    body = result.body
    if isinstance(body, dict) and body.get("errors") is not None:
        return f": {json.dumps(body['errors'])[:160]}"
    return ""


def _acknowledge(
    moment: datetime, batch: Batch, linked: bool, held_from: str | None = None
) -> None:
    """Record that the server has ``batch``. Days older than the keep window are settled (and
    forgotten), except from ``held_from`` on, the oldest day this shipment's server refused: a
    day it has not taken is never settled."""

    def record(state: SharingState) -> None:
        state.acked.update({day: digest[:12] for day, digest in batch.digests.items()})
        cutoff = (
            moment.astimezone(UTC).date() - timedelta(days=sharing.KEEP_ACKED_DAYS)
        ).isoformat()
        old = [
            day for day in state.acked if day < cutoff and (held_from is None or day < held_from)
        ]
        if old:  # settled: the server has them, and nothing looks at them again
            state.settled_through = max([state.settled_through or "", *old])
            for day in old:
                del state.acked[day]
        state.last_sent_at = moment.isoformat(timespec="seconds")
        state.linked = linked
        state.failures = 0
        state.last_error = None
        state.next_due_at = (moment + sharing.SHIP_EVERY).isoformat(timespec="seconds")

    if _still_on():  # off since: what turning it off purged is not written back
        with contextlib.suppress(Exception):
            sharing.update(record)


def _schedule(moment: datetime) -> None:
    def record(state: SharingState) -> None:
        state.next_due_at = (moment + sharing.SHIP_EVERY).isoformat(timespec="seconds")

    if _still_on():
        with contextlib.suppress(Exception):
            sharing.update(record)


def _failed(
    moment: datetime,
    error: str,
    *,
    retry_after: int | None = None,
    report: ShipReport | None = None,
) -> ShipReport:
    """A shipment that did not go (or not all of it): tried again after a backoff that grows to
    six hours and stays there, for as long as it takes. Logged at debug level and nowhere else."""
    log.debug("usage shipment not sent: %s", error)

    def record(state: SharingState) -> None:
        state.failures += 1
        state.last_error = error[:200]
        delay = sharing.backoff(state.failures)
        if retry_after is not None:
            delay = max(delay, timedelta(seconds=retry_after))
        state.next_due_at = (moment + delay).isoformat(timespec="seconds")

    if _still_on():
        with contextlib.suppress(Exception):
            sharing.update(record)
    kept = report or ShipReport()
    kept.error = error[:200]
    return kept


# --- the switch ----------


def enable(*, now: datetime | None = None) -> SharingStatus:
    """Turn sharing on: mint the identity (once), set the switch, record the consent, and make a
    shipment due. Raises :class:`SharingForcedOff` when something forces it off.

    Turning it on when it IS on changes nothing about what is to be sent: the first day shared,
    the days acknowledged and the schedule stay as they are (a script or a person that says
    *on* again must not throw away the days still waiting for a server). They start afresh only
    on a change from off to on."""
    reason = sharing.forced_off_reason()
    if reason is not None:
        raise SharingForcedOff(reason)
    moment = now or datetime.now(UTC)
    ensure_identity()
    was_on = sharing.switch_on()
    settings_service.set_value("sharing.enabled", "true")

    def record(state: SharingState) -> None:
        if state.consent_answer is None:
            state.consent_asked_at = moment.isoformat(timespec="seconds")
            state.consent_answer = "share"
        if was_on and state.since is not None:
            return  # already on, from a day already agreed: keep since, acked and the schedule
        # Only what happens from now on is shared: a day before this one was not agreed to.
        state.since = moment.astimezone().date().isoformat()
        state.acked = {}
        state.settled_through = None
        state.failures = 0
        state.last_error = None
        state.next_due_at = None
        state.spawned_at = None

    sharing.update(record)
    return status()


def disable(*, now: datetime | None = None, transport: Transport | None = None) -> ShipReport:
    """Turn sharing off: at once, and for good until it is turned on again.

    The switch goes first, so a shipment already under way re-reads it before its next POST and
    stops. What was kept of the shipments is then purged under the shipper's lock, waited for up
    to :data:`DISABLE_WAIT_S` (one POST is 5 s at most): the shipment in flight finishes before the
    purge, and nothing it records can land after it. If anything was ever sent, or a shipment was
    in flight, one ``opt_out`` ping says so, best effort; nothing is sent after it. Nothing is
    sent at all while something forces sharing off. An explicit *off* is also the answer to the
    one-time question: it is not asked afterwards."""
    moment = now or datetime.now(UTC)
    settings_service.set_value("sharing.enabled", "false")
    in_flight = False
    with contextlib.ExitStack() as held:
        try:
            held.enter_context(locking.holding(sharing.lock_path(), wait_s=0))
        except TimeoutError:
            in_flight = True  # a shipment is running: wait for its POST, then purge
            with contextlib.suppress(TimeoutError, OSError):
                held.enter_context(locking.holding(sharing.lock_path(), wait_s=DISABLE_WAIT_S))
        except OSError:
            pass  # no lock file to take: nothing can be in flight
        state = sharing.read()

        def purge(old: SharingState) -> None:
            old.acked = {}
            old.settled_through = None
            old.since = None
            old.next_due_at = None
            old.spawned_at = None
            old.failures = 0
            old.last_error = None
            old.linked = None
            old.last_sent_at = None
            old.withheld = []
            if old.consent_answer is None:
                old.consent_asked_at = moment.isoformat(timespec="seconds")
                old.consent_answer = "declined"

        with contextlib.suppress(Exception):
            sharing.update(purge)
    if (state.last_sent_at is None and not in_flight) or state.install_id is None:
        return ShipReport(skipped="nothing had been sent", opt_out=False)
    if sharing.forced_off_reason() is not None:
        return ShipReport(skipped="sharing is forced off: nothing is sent", opt_out=False)
    body = {
        "schema_version": SCHEMA_VERSION,
        "install_id": state.install_id,
        "sent_at": _sent_at_stamp(moment),
        "opt_out": True,
        "client": client_info(),
        "days": [],
    }
    post = transport or default_transport
    try:
        result = post(endpoint(), body, None, TIMEOUT_S)
    except Exception as exc:  # best effort, and silent: the switch is off either way
        log.debug("usage opt-out notice not sent: %r", exc)
        return ShipReport(error=f"{type(exc).__name__}: {exc}"[:200], opt_out=True)
    if result.status == 200:  # the notice has no days: only the status says it was taken
        return ShipReport(sent=True, opt_out=True)
    log.debug("usage opt-out notice not sent: HTTP %s", result.status)
    return ShipReport(error=f"HTTP {result.status}", opt_out=True)


def status() -> SharingStatus:
    """Where sharing stands: the switch, what forces it off, the last shipment that WENT, the
    sign-in. Failures are in ``debug`` and nowhere a person reads (sharing fails silently)."""
    state = sharing.read()
    reason = sharing.forced_off_reason()
    switch = sharing.switch_on()
    try:
        where: str | None = endpoint()
    except iam.IamError:
        where = None
    return SharingStatus(
        enabled=switch and reason is None,
        switch=switch,
        forced_off=reason,
        consent_asked=state.consent_answer is not None,
        consent_answer=state.consent_answer,
        install_id=state.install_id,
        signed_in=iam.signed_in_quietly() is not None,
        linked=state.linked,
        last_sent_at=sharing.parse_time(state.last_sent_at),
        days_acknowledged=len(state.acked),
        debug={
            "since": state.since,
            "next_due_at": state.next_due_at,
            "failures": state.failures,
            "last_error": state.last_error,
            "endpoint": where,
        },
    )


# --- the consent ----------


def consent_due() -> bool:
    """Whether the TUI should ask now: it has not been answered, sharing is not already on, and
    nothing forces it off (a forced-off machine is never asked)."""
    if sharing.forced_off_reason() is not None or sharing.switch_on():
        return False
    return sharing.read().consent_answer is None


def record_consent(answer: str, *, now: datetime | None = None) -> SharingStatus:
    """Record the answer of the one-time dialog: ``share`` turns sharing on, ``declined`` leaves
    it off and the dialog is not shown again."""
    moment = now or datetime.now(UTC)
    if answer == "share":
        return enable(now=moment)

    def record(state: SharingState) -> None:
        state.consent_asked_at = moment.isoformat(timespec="seconds")
        state.consent_answer = "declined"

    sharing.update(record)
    return status()
