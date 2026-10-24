"""Codex logins in numbered slots: what a ``CODEX_HOME`` holds, read offline.

``core.claude_accounts`` keeps the slot directories for every agent; this module
is Codex's answers to the accounts slot of its adapter
(``core.agent_adapters.types.AgentAccounts``). Measured on Codex 0.154.0 and
again on 0.162.1:

- **The login** is ``$CODEX_HOME/auth.json``, which ``codex login`` writes:
  ``auth_mode`` (``chatgpt``, or an API key), ``OPENAI_API_KEY``,
  ``tokens.{id_token, access_token, refresh_token, account_id}`` and
  ``last_refresh``. Who it is comes from the CLAIMS of the id token — its
  payload decoded as base64, never verified and never sent anywhere — ``email``,
  ``name`` and, under ``https://api.openai.com/auth``, ``chatgpt_plan_type`` and
  ``chatgpt_account_id``. No token value is ever returned, logged or printed;
  only whether one is there is asked. An API-key login reads ``API key``.
- **Usage** is what Codex's own sessions recorded: every session is a rollout,
  ``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl``, and its
  ``event_msg`` records of type ``token_count`` carry ``rate_limits`` —
  ``{limit_id, primary: {used_percent, window_minutes, resets_at}, secondary:
  {…} | null, credits, plan_type, rate_limit_reached_type}``. The newest such
  record under a home that carries a window is that account's usage: at the
  limit 0.162.1 records one more after it, ``limit_id`` ``premium`` with both
  windows ``null``, which only says the limit was reached. There is no public
  usage endpoint and none is invented: nothing here leaves the machine, so a
  reading is as fresh as the account's last turn. A plan can have one window (a
  weekly ``primary`` and no ``secondary``) or two (five hours and a week).
- **A limit** has no hook of its own (Claude Code's ``StopFailure``): it is
  read from the end of the session's own rollout — a ``rate_limit_reached_type``
  on its newest ``token_count``, or this turn's error saying so: an ``error``
  event whose ``codex_error_info`` is ``usage_limit_reached`` (0.154.0), or the
  turn's ``task_complete`` ``error`` whose ``codex_error_info`` is
  ``usage_limit_exceeded`` (0.162.1), or either's message (``You've hit your
  usage limit…``, the text the binary carries; ``You hit your spend cap set in
  your workspace…``, the server's own). On 0.162.1 a turn that fails, on its
  limit or any other error, fires no ``Stop`` at all, so a failed turn is read
  off the rollout's end too (:func:`failed_turn`), by the fleet.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aisquare.core import paths
from aisquare.core.claude_accounts import FailedTurn, LimitNotice, LoginUnreadable
from aisquare.core.codex_sharing import CodexFolderTrust
from aisquare.models import (
    SESSION_WINDOW,
    WEEK_WINDOW,
    ClaudeAccount,
    ClaudeIdentity,
    ClaudeUsage,
    UsageWindow,
)

AGENT = "codex"
HOME_VAR = "CODEX_HOME"
PLAIN_VARS = {HOME_VAR: "AISQUARE_PLAIN_CODEX_HOME"}
"""Where a launch onto a managed Codex slot keeps the shell's own ``CODEX_HOME``."""
PLAIN_SLOT = 0
"""The plain ``codex``'s reserved number: slot 1 is the plain ``claude``'s, and every
managed slot of either agent is 2 or more on one ladder."""
INSTALL_COMMAND = "npm install -g @openai/codex"
API_KEY_IDENTITY = "API key"
"""What an API-key login reads as: it has no email to show."""

AUTH_CLAIMS = "https://api.openai.com/auth"
DAY_MINUTES = 24 * 60
"""A window this long or longer is a WEEK-like window; shorter ones are session windows."""

TAIL_BYTES = 256 * 1024
"""How much of a rollout's end one read takes; doubled until a reading is found."""
MAX_SCAN_BYTES = 32 * 1024 * 1024
"""A rollout read no further back than this: a long session records a reading per turn."""
NEWEST_ROLLOUTS = 20
"""How many of a home's newest rollouts are tried for a reading. An API-key session has
none, nor has a session that only ran ``/clear`` or hit its limit at once: in SMOKE3 the
account's newest window was in the seventh rollout written last. Past the first reading,
a rollout written before it was recorded is not read (:func:`newest_rate_limits`)."""
HOME_SCAN_BYTES = 1024 * 1024
"""How much of each of those rollouts' end the home's search reads: a session records a
reading with each response, so its newest is near its end, and an account with none (an
API key, the limit's windowless reading alone) read twenty rollouts to
:data:`MAX_SCAN_BYTES` each on every ``usage()`` (review r5 of #113, finding 6)."""

TURN_END_BYTES = 64 * 1024
"""How much of a rollout's end :func:`failed_turn` reads: a failed turn's ``task_complete`` is
the rollout's last record, and a working row's rollout is read on every fleet listing."""

LIMIT_ERROR_INFOS = frozenset({"usage_limit_reached", "usage_limit_exceeded"})
"""A limit error's ``codex_error_info``: an ``error`` event's on 0.154.0, the turn's
``task_complete`` ``error``'s on 0.162.1 (measured at a team plan's spend cap)."""
LIMIT_MESSAGES = ("hit your usage limit", "spend cap")
"""What a limit error's message says (lowercased): ``You've hit your usage limit…``, the
binary's own, and ``You hit your spend cap set in your workspace…``, the server's (0.162.1)."""
CREDIT_LIMITS = frozenset({"workspace_owner_credits_depleted", "workspace_member_credits_depleted"})


def auth_path(home: Path) -> Path:
    return home / "auth.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _claims(token: Any) -> dict[str, Any] | None:
    """The payload of a JWT, decoded and NOT verified — identity claims for display only."""
    if not isinstance(token, str) or token.count(".") != 2:
        return None
    payload = token.split(".")[1]
    try:
        decoded: Any = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, binascii.Error):
        return None
    return decoded if isinstance(decoded, dict) else None


def _has(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _login(home: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """``auth.json`` and its id token's claims (``None`` each when absent or unreadable)."""
    data = _read_json(auth_path(home))
    if data is None:
        return None, None
    tokens = data.get("tokens")
    claims = _claims(tokens.get("id_token")) if isinstance(tokens, dict) else None
    return data, claims


def _api_key(data: Mapping[str, Any]) -> bool:
    return _has(data.get("OPENAI_API_KEY"))


def identity(
    account: ClaudeAccount, *, env: Mapping[str, str] | None = None
) -> ClaudeIdentity | None:
    """Who the home is signed in as, from the id token's claims; ``None`` with no login."""
    data, claims = _login(account.config_dir)
    if data is None:
        return None
    if claims is not None and _has(claims.get("email")):
        auth = claims.get(AUTH_CLAIMS)
        auth = auth if isinstance(auth, dict) else {}
        account_id = auth.get("chatgpt_account_id")
        return ClaudeIdentity(
            email=str(claims["email"]),
            account_uuid=account_id if _has(account_id) else None,
        )
    if _api_key(data) or str(data.get("auth_mode", "")).lower().replace("_", "") == "apikey":
        return ClaudeIdentity(email=API_KEY_IDENTITY)
    return None


def signed_in(account: ClaudeAccount) -> bool:
    """A login Codex can use: an API key, or ChatGPT tokens beside readable claims."""
    data, claims = _login(account.config_dir)
    return data is not None and _usable(data, claims)


def _usable(data: Mapping[str, Any], claims: Mapping[str, Any] | None) -> bool:
    if _api_key(data):
        return True
    tokens = data.get("tokens")
    if not isinstance(tokens, dict) or claims is None:
        return False
    return _has(tokens.get("refresh_token")) or _has(tokens.get("access_token"))


def login_fingerprint(account: ClaudeAccount) -> str | None:
    """A digest of ``auth.json`` as it stands while it holds a login Codex can use
    (:func:`signed_in`), else ``None``: of the file's bytes, never a value read out of it.

    What a *Log in* opened over a login still on disk waits to see change: ``codex login``
    writes the file anew with new tokens, and the stale ones are a signed-in home. A
    file rewritten with the same bytes is the same login; any other write of it (a new
    login, or Codex's own refresh of its tokens) is a new one. One read of the file, so
    the digest is of the login it judged usable. An ``auth.json`` there that cannot be read
    raises ``LoginUnreadable``: ``None`` would let the login it holds land as new."""
    try:
        raw = auth_path(account.config_dir).read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LoginUnreadable(f"its auth.json cannot be read ({exc.strerror or exc})") from exc
    try:
        data: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None  # no login Codex can use: whatever it writes next is new
    if not isinstance(data, dict):
        return None
    tokens = data.get("tokens")
    tokens = tokens if isinstance(tokens, dict) else {}
    if not _usable(data, _claims(tokens.get("id_token"))):
        return None
    return hashlib.sha256(raw).hexdigest()


def plan(account: ClaudeAccount) -> str | None:
    """``plus``, ``pro``, ``team``… as the id token names it; ``None`` for an API key."""
    _data, claims = _login(account.config_dir)
    auth = claims.get(AUTH_CLAIMS) if claims is not None else None
    value = auth.get("chatgpt_plan_type") if isinstance(auth, dict) else None
    return value if _has(value) else None


# --- usage: the newest token_count a session recorded ------------------------------------------


def _records_from_end(path: Path, *, limit: int = MAX_SCAN_BYTES) -> Iterator[dict[str, Any]]:
    """The rollout's JSON records, NEWEST FIRST, reading back from the end in growing chunks.

    A line cut by a chunk's start is skipped until a larger read holds it whole;
    nothing before ``limit`` bytes from the end is read.
    """
    try:
        size = path.stat().st_size
        handle = path.open("rb")
    except OSError:
        return
    with handle:
        seen = 0  # complete lines already yielded, counted from the end
        span = min(TAIL_BYTES, limit)
        while True:
            start = max(size - span, 0)
            try:
                handle.seek(start)
                chunk = handle.read(size - start)
            except OSError:
                return
            lines = chunk.split(b"\n")
            if start > 0:
                lines = lines[1:]  # cut by the chunk's start
            complete = [line for line in lines if line.strip()]
            for line in reversed(complete[: len(complete) - seen]):
                try:
                    record: Any = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    yield record
            seen = len(complete)
            if start == 0 or span >= limit:
                return
            span = min(span * 2, limit)


def _payload(record: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else {}


def _is_event(record: Mapping[str, Any], kind: str) -> bool:
    return record.get("type") == "event_msg" and _payload(record).get("type") == kind


def _rate_limits(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if not _is_event(record, "token_count"):
        return None
    limits = _payload(record).get("rate_limits")
    return limits if isinstance(limits, dict) else None


def _stamp(record: Mapping[str, Any]) -> datetime | None:
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _epoch(value: Any) -> datetime | None:
    if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


class _Window:
    def __init__(self, raw: Mapping[str, Any], order: int) -> None:
        percent = raw.get("used_percent")
        minutes = raw.get("window_minutes")
        self.percent = float(percent) if isinstance(percent, int | float) else None
        self.minutes = minutes if isinstance(minutes, int) and minutes > 0 else None
        self.resets_at = _epoch(raw.get("resets_at"))
        self.order = order

    @property
    def weekly(self) -> bool:
        return self.minutes is not None and self.minutes >= DAY_MINUTES


def _windows(limits: Mapping[str, Any]) -> list[_Window]:
    found = [
        _Window(raw, order)
        for order, raw in enumerate((limits.get("primary"), limits.get("secondary")))
        if isinstance(raw, dict)
    ]
    return [window for window in found if window.percent is not None]


def _session_and_week(windows: list[_Window]) -> tuple[_Window | None, _Window | None]:
    """Which window is the session's and which the week's, by LENGTH, not by position.

    A plan with one weekly window records it as ``primary`` (measured: a
    business plan on 0.154.0), so ``primary`` is not always the five hours.
    One window goes by its length; two go shorter-first, ties in recorded order.
    """
    if not windows:
        return None, None
    if len(windows) == 1:
        only = windows[0]
        return (None, only) if only.weekly else (only, None)
    first, second = sorted(windows, key=lambda w: (w.minutes or 0, w.order))[:2]
    return first, second


def _full(limits: Mapping[str, Any] | None) -> list[_Window]:
    """The reading's windows at 100 %."""
    windows = _windows(limits) if limits is not None else []
    return [w for w in windows if w.percent is not None and w.percent >= 100]


def _later(when: datetime | None, than: datetime | None) -> bool:
    """``when`` is after ``than``, a reading with no time being older than any."""
    return when is not None and (than is None or when > than)


def _rollouts(home: Path) -> list[tuple[float, Path, int]]:
    """The home's rollouts with their mtimes, most recently WRITTEN first — a resumed
    session appends to its own — and their sizes."""
    root = home / "sessions"
    found: list[tuple[float, Path, int]] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.name.startswith("rollout-") and entry.name.endswith(".jsonl"):
                    stat = entry.stat()
                    found.append((stat.st_mtime, Path(entry.path), stat.st_size))
            except OSError:
                continue
    return sorted(found, key=lambda item: item[0], reverse=True)


def _home_of(rollout: Path) -> Path | None:
    """The ``CODEX_HOME`` a rollout was written in (``<home>/sessions/YYYY/MM/DD/``)."""
    parents = rollout.parents
    return parents[4] if len(parents) > 4 and parents[3].name == "sessions" else None


def _newest_windowed(rollout: Path) -> tuple[Mapping[str, Any], datetime | None] | None:
    """The newest ``rate_limits`` in the rollout that carries a window, and when.

    Not simply the newest: at the limit Codex 0.162.1 records a second reading after the
    account's, ``limit_id`` ``premium`` with both windows ``null`` and only its credits
    and ``rate_limit_reached_type`` (measured at a team plan's spend cap), so the newest
    reading of a full account said nothing of its windows (SMOKE3).
    """
    for record in _records_from_end(rollout, limit=HOME_SCAN_BYTES):
        limits = _rate_limits(record)
        if limits is not None and _windows(limits):
            return limits, _stamp(record)
    return None


_Reading = tuple[Mapping[str, Any], datetime | None]

#: The newest reading found under each home, by the home, with the rollouts it was found
#: among (their paths, mtimes and sizes): read again only once one of them is written.
_READINGS: dict[str, tuple[tuple[tuple[float, str, int], ...], _Reading | None]] = {}


def newest_rate_limits(home: Path) -> _Reading | None:
    """The newest ``rate_limits`` carrying a window any session under ``home`` recorded,
    and when (:func:`_newest_windowed`).

    Newest by when it was recorded, among the :data:`NEWEST_ROLLOUTS` rollouts written
    last: the one written last may have recorded only the limit's windowless reading
    since its own last window, older than another session's. Each is read back
    :data:`HOME_SCAN_BYTES` at most, and none again until one of them is written: the
    Accounts page, Welcome's look and the hand-over's headroom ask on every ``usage()``,
    and the app's tick through a failed turn's limit (review r5 of #113, finding 6).
    """
    newest = _rollouts(home)[:NEWEST_ROLLOUTS]
    seen = tuple((mtime, str(rollout), size) for mtime, rollout, size in newest)
    cached = _READINGS.get(str(home))
    if cached is not None and cached[0] == seen:
        return cached[1]
    best: _Reading | None = None
    for mtime, rollout, _ in newest:
        if best is not None and best[1] is not None and mtime < best[1].timestamp():
            break  # written last before that reading was recorded: nothing in it is newer
        found = _newest_windowed(rollout)
        if found is not None and (best is None or _later(found[1], best[1])):
            best = found
    _READINGS[str(home)] = (seen, best)
    return best


def usage(account: ClaudeAccount, *, now: datetime | None = None) -> ClaudeUsage:
    """The account's windows as its sessions last recorded them (:func:`newest_rate_limits`)
    — offline, no request.

    A window whose reset has passed since that reading has started again, and
    nobody has used this home since (the reading is the newest), so it reads 0 %
    with no reset known. ``fetched_at`` is when the session recorded it.
    """
    found = newest_rate_limits(account.config_dir)
    if found is None:
        return ClaudeUsage(
            available=False,
            reason="no Codex session on this account has recorded a rate-limit window yet",
        )
    limits, recorded_at = found
    moment = now or datetime.now(tz=UTC)
    session, week = _session_and_week(_windows(limits))

    def read(window: _Window | None) -> tuple[float | None, datetime | None]:
        if window is None:
            return None, None
        if window.resets_at is not None and window.resets_at <= moment:
            return 0.0, None
        return window.percent, window.resets_at

    windows = []
    for name, window in ((SESSION_WINDOW, session), (WEEK_WINDOW, week)):
        if window is not None:  # the windows its plan HAS: a business plan's week alone
            percent, resets = read(window)
            windows.append(
                UsageWindow(
                    name=name, minutes=window.minutes, used_percent=percent, resets_at=resets
                )
            )
    return ClaudeUsage(available=True, windows=tuple(windows), fetched_at=recorded_at)


# --- a limit, from the end of the session's rollout --------------------------------------------


def _turn_error(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The error a record says its turn ended on: an ``error`` event's payload (0.154.0),
    or its ``task_complete``'s ``error`` (0.162.1); else ``None``."""
    if _is_event(record, "error"):
        return _payload(record)
    if _is_event(record, "task_complete"):
        error = _payload(record).get("error")
        return error if isinstance(error, dict) else None
    return None


def _error_kind(error: Mapping[str, Any]) -> str | None:
    """The error's ``codex_error_info``: a name, or an object of one name and its details."""
    info = error.get("codex_error_info")
    if isinstance(info, dict) and len(info) == 1:
        info = next(iter(info))
    return info if _has(info) else None


def _limit_error(record: Mapping[str, Any]) -> bool:
    error = _turn_error(record)
    if error is None:
        return False
    message = error.get("message")
    return _error_kind(error) in LIMIT_ERROR_INFOS or (
        isinstance(message, str) and any(said in message.lower() for said in LIMIT_MESSAGES)
    )


def limit_from_transcript(path: Path, *, now: datetime | None = None) -> LimitNotice | None:
    """The usage limit the rollout's end records, or ``None`` when its newest turn hit none.

    Limited when the newest ``token_count`` names a ``rate_limit_reached_type``,
    or when the newest turn (since its ``task_started``) ended on the limit's error
    (:func:`_limit_error`). The window and its reset come from the newest reading that
    carries windows (:func:`_newest_windowed`): of its windows at 100 %, the one that
    lifts LAST (``session`` for a window under a day, ``weekly`` for longer). Where none
    of the rollout's own reads full, a newer reading of the same home's (another of the
    account's sessions, :func:`newest_rate_limits`) is asked too: the session that hit
    the limit may not have recorded the window's last climb. Depleted credits are the
    ``credits`` window; ``usage`` with no reset when no window reads full. A recorded
    limit whose every full window has since reset — with no limit error in the newest
    turn — is over, and reads ``None``.
    """
    reached: Any = None
    newest = False  # the newest reading was seen; it alone says whether one was reached
    windowed: tuple[Mapping[str, Any], datetime | None] | None = None
    errored = False
    turn_open = True  # still reading records of the newest turn
    for record in _records_from_end(path):
        if turn_open and _limit_error(record):
            errored = True
        if _is_event(record, "task_started"):
            turn_open = False
        limits = _rate_limits(record)
        if limits is not None and not newest:
            newest = True
            reached = limits.get("rate_limit_reached_type")
        if limits is not None and windowed is None and _windows(limits):
            windowed = (limits, _stamp(record))
        if not turn_open and newest and (windowed is not None or not (_has(reached) or errored)):
            break
    if not _has(reached) and not errored:
        return None
    moment = now or datetime.now(tz=UTC)
    full = _full(windowed[0]) if windowed is not None else []
    home = _home_of(path) if not full else None
    account = newest_rate_limits(home) if home is not None else None
    if account is not None and _later(account[1], windowed[1] if windowed else None):
        full = _full(account[0])
    timed = [w for w in full if w.resets_at is not None]
    if timed:
        last = max(timed, key=lambda w: w.resets_at or moment)
        if (last.resets_at or moment) <= moment and not errored:
            return None
        return LimitNotice("weekly" if last.weekly else "session", last.resets_at)
    if reached in CREDIT_LIMITS:
        return LimitNotice("credits", None)
    if full:
        return LimitNotice("weekly" if full[0].weekly else "session", None)
    return LimitNotice("usage", None)


def failed_turn(path: Path) -> FailedTurn | None:
    """The rollout's newest turn when it ended on an error, else ``None``.

    Measured on 0.162.1, a loopback provider answering a turn with an error and the TUI
    under tmux: the turn records its ``task_complete`` with an ``error``
    (``{message, codex_error_info}``) and fires no ``Stop`` hook and no ``notify``
    program — only its ``UserPromptSubmit`` had fired — for the usage limit
    (``usage_limit_exceeded``, the live SMOKE3 limit's shape) and a plain ``400``
    (``other``) alike. The rollout's end is where it shows. Only its last
    :data:`TURN_END_BYTES` are read: the failed turn's ``task_complete`` is its last record,
    and a turn still running (its ``task_started`` newer) is no failure. A usage limit
    reads as ``rate_limit``, as Claude Code's ``StopFailure`` names one (its window and
    reset: :func:`limit_from_transcript`); any other error as its ``codex_error_info``.
    """
    for record in _records_from_end(path, limit=TURN_END_BYTES):
        if _is_event(record, "task_started") or _is_event(record, "turn_aborted"):
            return None
        if not _is_event(record, "task_complete"):
            continue
        error = _turn_error(record)
        if error is None:
            return None
        turn = _payload(record).get("turn_id")
        said = error.get("message")
        message = said.strip() if isinstance(said, str) and said.strip() else "the turn failed"
        kind = "rate_limit" if _limit_error(record) else _error_kind(error) or "unknown"
        return FailedTurn(turn if _has(turn) else None, kind, message, _stamp(record))
    return None


def approvals_reviewer(path: Path) -> str | None:
    """Who answers the session's approval requests, as its rollout last recorded it:
    ``user`` (the human, in the pane) or ``auto_review`` (Codex's automatic reviewer,
    ``--approve-for-me``), or ``None`` when the rollout cannot be read or names none.

    The newest of the ``turn_context`` each turn opens with and a ``thread_settings_applied``
    event (a ``/permissions`` change) decides: measured on 0.162.1, the PermissionRequest
    hook's payload is the same for both reviewers and only the rollout tells them apart.
    """
    for record in _records_from_end(path):
        if record.get("type") == "turn_context":
            settings: Mapping[str, Any] = _payload(record)
        elif _is_event(record, "thread_settings_applied"):
            applied = _payload(record).get("thread_settings")
            settings = applied if isinstance(applied, dict) else {}
        else:
            continue
        reviewer = settings.get("approvals_reviewer")
        return reviewer if _has(reviewer) else None
    return None


class CodexAccounts:
    """Codex's answers to the accounts slot (``core.agent_adapters.types.AgentAccounts``)."""

    agent = AGENT
    home_env = HOME_VAR
    extra_env: tuple[str, ...] = ()
    plain_vars: Mapping[str, str] = PLAIN_VARS
    dir_name = "codex-accounts"
    plain_slot = PLAIN_SLOT
    plain_label = "plain codex"
    install_command = INSTALL_COMMAND
    network_usage = False
    legacy_slots = False
    noun = "Codex"
    relogin = ""
    sign_out = "with `codex logout`"
    shares_settings = False

    def sign_in_args(self) -> tuple[str, ...]:
        # Codex's own browser (or device-code) sign-in; it writes auth.json and exits.
        return ("login",)

    def plain_home(self, home: Path, plain: Mapping[str, str]) -> Path:
        value = plain.get(HOME_VAR, "").strip()
        return paths.expand_user(Path(value)) if value else home / ".codex"

    def config_dir(self, slot_dir: Path) -> Path:
        return slot_dir  # CODEX_HOME names the config dir itself

    def launched_account(
        self, env: Mapping[str, str], accounts: Sequence[ClaudeAccount]
    ) -> ClaudeAccount | None:
        from aisquare.core.claude_accounts import account_in_home

        return account_in_home(AGENT, env, accounts)

    def identity(
        self, account: ClaudeAccount, *, env: Mapping[str, str] | None = None
    ) -> ClaudeIdentity | None:
        return identity(account, env=env)

    def signed_in(self, account: ClaudeAccount, *, fresh: bool = False) -> bool:
        return signed_in(account)  # auth.json, read afresh every time

    def subscription(self, account: ClaudeAccount) -> str | None:
        return plan(account)

    def token_state(self, account: ClaudeAccount) -> str:
        # Codex refreshes its own tokens; present is all that is asked, never their values.
        return "ok" if signed_in(account) else "missing"

    def usage(
        self, account: ClaudeAccount, *, now: datetime | None, fetch: object = None
    ) -> ClaudeUsage:
        return usage(account, now=now)

    def limit_from_transcript(
        self, path: Path, *, now: datetime | None = None
    ) -> LimitNotice | None:
        return limit_from_transcript(path, now=now)

    def failed_turn(self, path: Path) -> FailedTurn | None:
        return failed_turn(path)

    def login_fingerprint(self, account: ClaudeAccount) -> str | None:
        return login_fingerprint(account)

    trust = CodexFolderTrust()
