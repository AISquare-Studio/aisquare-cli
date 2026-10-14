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
  record under a home is that account's usage. There is no public usage
  endpoint and none is invented: nothing here leaves the machine, so a reading
  is as fresh as the account's last turn. A plan can have one window (a
  weekly ``primary`` and no ``secondary``) or two (five hours and a week).
- **A limit** has no hook of its own (Claude Code's ``StopFailure``): it is
  read from the end of the session's own rollout when a turn stops — a
  ``rate_limit_reached_type`` on its newest ``token_count``, or an ``error``
  event of this turn whose ``codex_error_info`` is ``usage_limit_reached`` or
  whose message says the usage limit was hit (``You've hit your usage limit…``,
  the text the 0.162.1 binary carries).
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aisquare.core import paths
from aisquare.core.claude_accounts import LimitNotice
from aisquare.models import ClaudeAccount, ClaudeIdentity, ClaudeUsage

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
NEWEST_ROLLOUTS = 5
"""How many of a home's newest rollouts are tried for a reading (an API-key session has none)."""

LIMIT_ERROR_INFO = "usage_limit_reached"
LIMIT_MESSAGE = "hit your usage limit"
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
    if data is None:
        return False
    if _api_key(data):
        return True
    tokens = data.get("tokens")
    if not isinstance(tokens, dict) or claims is None:
        return False
    return _has(tokens.get("refresh_token")) or _has(tokens.get("access_token"))


def plan(account: ClaudeAccount) -> str | None:
    """``plus``, ``pro``, ``team``… as the id token names it; ``None`` for an API key."""
    _data, claims = _login(account.config_dir)
    auth = claims.get(AUTH_CLAIMS) if claims is not None else None
    value = auth.get("chatgpt_plan_type") if isinstance(auth, dict) else None
    return value if _has(value) else None


# --- usage: the newest token_count a session recorded ------------------------------------------


def _records_from_end(path: Path) -> Iterator[dict[str, Any]]:
    """The rollout's JSON records, NEWEST FIRST, reading back from the end in growing chunks.

    A line cut by a chunk's start is skipped until a larger read holds it whole;
    nothing before :data:`MAX_SCAN_BYTES` from the end is read.
    """
    try:
        size = path.stat().st_size
        handle = path.open("rb")
    except OSError:
        return
    with handle:
        seen = 0  # complete lines already yielded, counted from the end
        span = TAIL_BYTES
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
            if start == 0 or span >= MAX_SCAN_BYTES:
                return
            span *= 2


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


def _rollouts(home: Path) -> list[Path]:
    """The home's rollouts, most recently WRITTEN first — a resumed session appends to its own."""
    root = home / "sessions"
    found: list[tuple[float, Path]] = []
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
                    found.append((entry.stat().st_mtime, Path(entry.path)))
            except OSError:
                continue
    return [path for _mtime, path in sorted(found, key=lambda pair: pair[0], reverse=True)]


def newest_rate_limits(home: Path) -> tuple[Mapping[str, Any], datetime | None] | None:
    """The newest ``rate_limits`` any session under ``home`` recorded, and when."""
    for rollout in _rollouts(home)[:NEWEST_ROLLOUTS]:
        for record in _records_from_end(rollout):
            limits = _rate_limits(record)
            if limits is not None:
                return limits, _stamp(record)
    return None


def usage(account: ClaudeAccount, *, now: datetime | None = None) -> ClaudeUsage:
    """The account's windows as its newest session recorded them — offline, no request.

    A window whose reset has passed since that reading has started again, and
    nobody has used this home since (the reading is the newest), so it reads 0 %
    with no reset known. ``fetched_at`` is when the session recorded it.
    """
    found = newest_rate_limits(account.config_dir)
    if found is None:
        return ClaudeUsage(
            available=False,
            reason="no Codex session on this account has recorded its limits yet",
        )
    limits, recorded_at = found
    moment = now or datetime.now(tz=UTC)
    session, week = _session_and_week(_windows(limits))
    if session is None and week is None:
        return ClaudeUsage(
            available=False, reason="the newest Codex session recorded no rate-limit window"
        )

    def read(window: _Window | None) -> tuple[float | None, datetime | None]:
        if window is None:
            return None, None
        if window.resets_at is not None and window.resets_at <= moment:
            return 0.0, None
        return window.percent, window.resets_at

    session_percent, session_resets = read(session)
    week_percent, week_resets = read(week)
    return ClaudeUsage(
        available=True,
        session_percent=session_percent,
        session_resets_at=session_resets,
        week_percent=week_percent,
        week_resets_at=week_resets,
        fetched_at=recorded_at,
    )


# --- a limit, from the end of the session's rollout --------------------------------------------


def _limit_error(record: Mapping[str, Any]) -> bool:
    if not _is_event(record, "error"):
        return False
    payload = _payload(record)
    info = payload.get("codex_error_info")
    message = payload.get("message")
    return info == LIMIT_ERROR_INFO or (
        isinstance(message, str) and LIMIT_MESSAGE in message.lower()
    )


def limit_from_transcript(path: Path, *, now: datetime | None = None) -> LimitNotice | None:
    """The usage limit the rollout's end records, or ``None`` when its newest turn hit none.

    Limited when the newest ``token_count`` names a ``rate_limit_reached_type``,
    or when an ``error`` event since the newest turn began (``task_started``)
    says the usage limit was reached. The window and its reset come from the
    newest ``rate_limits``: of the windows at 100 %, the one that lifts LAST
    (``session`` for a window under a day, ``weekly`` for longer); depleted
    credits are the ``credits`` window; ``usage`` with no reset when no window
    reads full. A recorded limit whose every full window has since reset — with
    no limit error in the newest turn — is over, and reads ``None``.
    """
    limits: Mapping[str, Any] | None = None
    errored = False
    turn_open = True  # still reading records of the newest turn
    for record in _records_from_end(path):
        if turn_open and _limit_error(record):
            errored = True
        if _is_event(record, "task_started"):
            turn_open = False
        if limits is None:
            limits = _rate_limits(record)
        if limits is not None and not turn_open:
            break
    reached = limits.get("rate_limit_reached_type") if limits is not None else None
    if not _has(reached) and not errored:
        return None
    moment = now or datetime.now(tz=UTC)
    windows = _windows(limits) if limits is not None else []
    full = [w for w in windows if w.percent is not None and w.percent >= 100]
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

    def sign_in_args(self) -> tuple[str, ...]:
        # Codex's own browser (or device-code) sign-in; it writes auth.json and exits.
        return ("login",)

    def plain_home(self, home: Path, plain: Mapping[str, str]) -> Path:
        value = plain.get(HOME_VAR, "").strip()
        return paths.expand_user(Path(value)) if value else home / ".codex"

    def identity(
        self, account: ClaudeAccount, *, env: Mapping[str, str] | None = None
    ) -> ClaudeIdentity | None:
        return identity(account, env=env)

    def signed_in(self, account: ClaudeAccount) -> bool:
        return signed_in(account)

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
