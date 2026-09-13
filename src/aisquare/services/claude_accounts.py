"""Claude Code accounts as ``asq`` and ``aisquare accounts`` see them.

``core.claude_accounts`` owns the directories. This module answers the
questions around them — is Claude Code installed, who is signed in where, how
much of each account's limits is used — and runs the two flows that change the
machine: a sign-in (a real Claude Code session in the account's own directory,
watched until its login lands) and a removal.

**The sign-in is Claude Code's own.** There is no non-interactive login in
Claude Code (``claude setup-token`` mints a CI token, which is a different
thing), so signing an account in means starting ``claude`` with that account's
``CLAUDE_CONFIG_DIR`` and letting its first-run flow send the user to the
browser. The UI does that in a tmux window it renders and watches; the
terminal command does it as a foreground child and looks afterwards. Either
way the login "landed" when the directory holds what a signed-in Claude Code
writes there — :func:`sign_in_landed` — and nothing here ever writes those
files.

**Usage is best effort, on purpose.** The percentages come from the endpoint
Claude Code's own ``/usage`` reads (:data:`USAGE_URL`), called with the access
token the account's credentials file holds. It is not a documented API: it
answered on 2026-09-09 with Claude Code 2.1.266 and its shape is recorded in
``tests/test_claude_accounts.py``, and the day it changes an account's row says
``usage unavailable`` with the reason while everything else on the page keeps
working. It is never called from a hook or on any path a session pays for.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from aisquare.core import claude_accounts as core
from aisquare.core import paths
from aisquare.core.config import AccountsSettings, load_config
from aisquare.core.spawn import untraced_env
from aisquare.core.store import ContextStore, store_session
from aisquare.core.tmux import TmuxServer, WindowInfo
from aisquare.core.version import __version__
from aisquare.models import (
    AccountsOverview,
    ClaudeAccount,
    ClaudeAccountRecord,
    ClaudeAccountStatus,
    ClaudeIdentity,
    ClaudeInstall,
    ClaudeUsage,
    ProjectInfo,
    UsageSample,
    UsageTrend,
)
from aisquare.services import agents as agents_service
from aisquare.services import settings as settings_service

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
"""What Claude Code's ``/usage`` reads; ``anthropic-beta: oauth-2025-04-20`` is required."""
OAUTH_BETA = "oauth-2025-04-20"
USAGE_TIMEOUT_SECONDS = 10.0

SIGN_IN_SESSION = "asq-accounts"
"""The fleet server's tmux session that hosts sign-in windows (one window per attempt)."""

AGENT = "claude-code"
"""The agent name ``aisquare agents`` knows Claude Code by."""

Fetch = Callable[[str, Mapping[str, str], float], tuple[int, bytes]]
"""``(url, headers, timeout) -> (status, body)``: how :func:`usage` reaches the network."""


class AccountsError(RuntimeError):
    """A reason the caller shows; never a traceback."""


class NoSuchAccount(AccountsError):
    pass


class ClaudeNotInstalled(AccountsError):
    pass


def _now() -> datetime:
    return datetime.now(tz=UTC)


# --- what is on this machine ----------------------------------------------------------


def install() -> ClaudeInstall:
    """Whether ``claude`` is on PATH, and which version (read from the install layout)."""
    binary = shutil.which("claude")
    if binary is None:
        return ClaudeInstall(installed=False)
    # Lazy: diagnostics imports this module for its doctor line.
    from aisquare.services.diagnostics import claude_code_version

    return ClaudeInstall(installed=True, binary=binary, version=claude_code_version(binary))


def describe(account: ClaudeAccount) -> ClaudeAccountStatus:
    """Everything about one slot that can be read without leaving the machine."""
    creds = core.credentials(account)
    return ClaudeAccountStatus(
        account=account,
        label=core.label(account),
        identity=core.identity(account),
        signed_in=core.signed_in(account),
        token_state=core.token_state(creds),
        subscription=core.subscription_label(creds),
        hooks_installed=_hooks_installed(account),
    )


def _hooks_installed(account: ClaudeAccount) -> bool:
    from aisquare.core import agents as agent_core

    try:
        return agent_core.hooks_installed(AGENT, account.config_dir)
    except Exception:  # a settings.json we cannot parse is "not installed", not a crash
        return False


def overview() -> AccountsOverview:
    """The install plus every slot in priority order, offline. Usage is a separate, slower question.

    "Offline" still holds: the registry is a local SQLite read. A store that
    cannot be opened costs the ARRANGEMENT (order, aliases, the default badge)
    and never the listing — see :func:`list_accounts`.
    """
    return AccountsOverview(
        claude=install(), accounts=[describe(account) for account in list_accounts()]
    )


# --- the registry: alias, priority order, default, disabled (#145) ------------------------------
#
# The directories under ~/.aisquare/claude-accounts say WHICH accounts exist and
# who is signed in (core.claude_accounts). What no directory can say is how the
# operator ARRANGED them — which one a launch should pick when nothing more
# specific says, in what order to try them, what to call them — and that lives
# in the ``claude_account`` table (core.store, v15). This section is the only
# code that reads or writes it, and :func:`choose` is the only place a launch's
# account is decided (pinned by tests/test_one_account_resolver.py).
#
# The two records are reconciled on every read, directories winning: a slot
# with no row gets one at the end of the order, a row whose directory is gone is
# dropped. There is no "migration" step because there is nothing to migrate —
# every existing machine is the "no rows yet" case, and the first read arranges
# its slots in slot order, which is exactly the order they were listed in before.

PROJECT_ACCOUNT_KEY = "claude_account"
"""The ``project_setting`` key holding a project's default account (a slot number)."""

ChoiceSource = Literal["flag", "role binding", "project default", "headroom", "machine default"]


class AccountsUnreadable(AccountsError):
    """The registry could not be read — the store is damaged or locked.

    Raised only by the WRITERS (``set_default`` and friends), where refusing is
    the honest answer. The readers fail open: they return the directories'
    view with a note, because a launch and a listing must survive a wedged
    ``context.db`` (tests/test_launch_survives_a_damaged_store.py).
    """


@dataclass(frozen=True)
class AccountChoice:
    """What :func:`choose` decided: an account and WHY, or no decision at all.

    ``account is None`` means nothing chose — no flag, no binding, no project or
    machine default — and the caller must leave the launch environment exactly
    as it found it. That is the pre-#145 behaviour, byte for byte, and it is the
    case every machine that never ran ``accounts default`` is in. It is
    deliberately distinct from "slot 1 was chosen": an EXPLICIT slot 1 restores
    this shell's own variables over a role binding's (``core.apply_launch_env``),
    which a silent default must never do to a binding someone wrote by hand.
    """

    account: ClaudeAccount | None
    source: ChoiceSource | None
    notes: list[str] = field(default_factory=list)
    """What was skipped or could not be read on the way down the ladder — for
    the launch line's dim notes, never a reason to refuse."""

    def describe(self) -> str:
        """``account 2`` for a flag; ``work · machine default`` when a default decided."""
        if self.account is None:
            return ""
        name = core.label(self.account)
        return name if self.source == "flag" else f"{name} · {self.source}"


def _arranged(store: ContextStore) -> list[ClaudeAccount]:
    """Every slot on disk, folded with its registry row, in priority order.

    Reconciles as it reads (see the section comment): rows are created for new
    slots and dropped for vanished ones, so the table can never describe an
    account that is not there.
    """
    on_disk = {account.slot: account for account in core.list_accounts()}
    rows: dict[int, ClaudeAccountRecord] = {
        record.slot: record for record in store.claude_accounts()
    }
    for slot in sorted(on_disk):
        if slot not in rows:
            rows[slot] = store.upsert_claude_account(slot, on_disk[slot].config_dir)
    if any(slot not in on_disk for slot in rows):
        for slot in list(rows):
            if slot not in on_disk:
                store.delete_claude_account(slot)
        # A delete renumbers the order (the store closes the gap), so the rows
        # read before it are stale: re-read rather than hand back positions with
        # a hole where the pruned slot was.
        rows = {record.slot: record for record in store.claude_accounts()}
    arranged = [
        on_disk[record.slot].model_copy(
            update={
                "alias": record.alias,
                "position": record.position,
                "is_default": record.is_default,
                "disabled": record.disabled,
            }
        )
        for record in sorted(rows.values(), key=lambda r: (r.position, r.slot))
    ]
    return arranged


def _read_arranged() -> tuple[list[ClaudeAccount], str | None]:
    """The arranged list, or the plain directories plus the reason the registry was not read."""
    try:
        with store_session() as store:
            return _arranged(store), None
    except sqlite3.Error as exc:
        return core.list_accounts(), f"accounts registry unreadable ({exc})"


def list_accounts() -> list[ClaudeAccount]:
    """The accounts in priority order, each carrying its alias, rank, default and disabled flags.

    Fails open to ``core.list_accounts()`` — slot order, no arrangement — when
    the store cannot be opened, so the Accounts page and ``accounts list`` keep
    working on a machine whose ``context.db`` is wedged; ``doctor`` is where
    that state is reported.
    """
    accounts, _note = _read_arranged()
    return accounts


def resolve(ref: str | int) -> ClaudeAccount:
    """A slot by number, by alias, or by the email it is signed in as.

    The three spellings cannot collide: a slot is all digits, an email has an
    ``@``, and an alias starts with a letter and has no ``@``
    (``core.normalise_alias``). Lookups are case-insensitive.
    """
    text = str(ref).strip()
    accounts, note = _read_arranged()
    if text.isdigit():
        account = next((a for a in accounts if a.slot == int(text)), None)
        if account is None:
            raise NoSuchAccount(f"no Claude account in slot {text} — see: aisquare accounts")
        return account
    wanted = text.lower()
    if "@" not in wanted:
        by_alias = next((a for a in accounts if a.alias == wanted), None)
        if by_alias is not None:
            return by_alias
    for account in accounts:
        identity = core.identity(account)
        if identity is not None and identity.email.lower() == wanted:
            return account
    if "@" in wanted:
        raise NoSuchAccount(f"no Claude account is signed in as {text!r} — see: aisquare accounts")
    detail = f" ({note})" if note else ""
    raise NoSuchAccount(
        f"no Claude account is called {text!r} — not a slot, an alias or a signed-in email"
        f"{detail}; see: aisquare accounts"
    )


def slot_of(config_dir: str | Path) -> int | None:
    """Which slot a Claude config directory IS: a managed slot's number, 1 for the plain
    claude's directory, ``None`` for a directory the CLI does not own (a hand-made layout).

    A reader, not a decider: ``services.team.session_account`` records the
    directory a session runs under from its transcript path, and this turns it
    back into the slot a hand-over (#146) is leaving.
    """
    managed = core.managed_slot(config_dir)
    if managed is not None:
        return managed
    try:
        if Path(config_dir).resolve() == core.default_config_dir().resolve():
            return core.DEFAULT_SLOT
    except OSError:
        return None
    return None


def _role_binding_ref(role: str | None) -> tuple[str | None, str | None]:
    """The account reference ``role``'s binding names, or the reason the config was unreadable."""
    if role is None:
        return None, None
    try:
        return settings_service.role_account_bindings().get(role), None
    except Exception as exc:  # a broken config.toml costs the binding, never the launch
        return None, f"role bindings unreadable ({type(exc).__name__}: {exc})"


def choose(
    explicit: str | None = None,
    *,
    role: str | None = None,
    project: ProjectInfo | None = None,
    exclude: Iterable[int] = (),
    spread: bool | None = None,
    fetch: Fetch | None = None,
) -> AccountChoice:
    """THE account resolver: which Claude account a launch runs under, and why.

    The ladder, top rung wins::

        --account <ref>            the flag (a slot, alias or email)
        team bind <role> --account the role's binding
        accounts default --project the project's default
        accounts default           the machine default
        (nothing)                  the launch environment is left untouched

    ``launch``, ``fleet spawn`` and the Settings tab all ask here and nowhere
    else — tests/test_one_account_resolver.py pins that — because the whole
    point of a default is that every surface agrees on it.

    A rung that names an account which DOES NOT EXIST raises
    :class:`NoSuchAccount` naming the rung: a binding to a removed slot must
    stop the launch, not quietly run it somewhere else (the same rule
    ``resolve_binary`` applies to a missing executable). A rung that names a
    DISABLED account is skipped with a note and the ladder continues — disabled
    means "never pick this one for me", and the operator can still name it on
    the flag, which is why the flag rung does not check. A registry or config
    that cannot be read costs its rung and leaves a note, never the launch.

    One rung is optional (#146): with ``[accounts] pick = "headroom"`` — or
    ``spread=True``, which a hand-over passes — the machine default gives way to
    :func:`headroom_choice`, the account with room in its five-hour window,
    ``exclude`` naming the slot a hand-over is leaving. When no account's usage
    can be read, the machine default decides exactly as before.
    """
    if explicit is not None:
        return AccountChoice(resolve(explicit), "flag")
    notes: list[str] = []
    bound, note = _role_binding_ref(role)
    if note is not None:
        notes.append(note)
    if bound:
        try:
            account = resolve(bound)
        except NoSuchAccount as exc:
            raise NoSuchAccount(
                f"the role binding for {role!r} names account {bound!r}: {exc}"
            ) from exc
        if account.disabled:
            notes.append(f"{core.label(account)} (bound to {role}) is disabled — skipped")
        else:
            return AccountChoice(account, "role binding", notes)
    accounts, note = _read_arranged()
    if note is not None:
        notes.append(note)
        return AccountChoice(None, None, notes)
    if project is not None:
        slot = _project_default_slot(project)
        if slot is not None:
            preferred = next((a for a in accounts if a.slot == slot), None)
            if preferred is None:
                raise NoSuchAccount(
                    f"the default account of {project.root.name or project.id} is slot {slot}, "
                    "which no longer exists — see: aisquare accounts default --project"
                )
            if preferred.disabled:
                notes.append(f"{core.label(preferred)} (project default) is disabled — skipped")
            else:
                return AccountChoice(preferred, "project default", notes)
    by_headroom = spread if spread is not None else accounts_settings().pick == "headroom"
    if by_headroom:
        picked, more = headroom_choice(
            accounts, switch_at=accounts_settings().switch_at, exclude=exclude, fetch=fetch
        )
        notes.extend(more)
        if picked is not None:
            return AccountChoice(picked, "headroom", notes)
    skip = set(exclude)
    default = next((a for a in accounts if a.is_default), None)
    if default is not None and default.slot not in skip:
        if default.disabled:
            notes.append(f"{core.label(default)} (machine default) is disabled — skipped")
        else:
            return AccountChoice(default, "machine default", notes)
    return AccountChoice(None, None, notes)


def _project_default_slot(project: ProjectInfo) -> int | None:
    with store_session() as store:
        raw = store.project_setting(project.id, PROJECT_ACCOUNT_KEY)
    return int(raw) if raw is not None and raw.isdigit() else None


# --- arranging: the writers ---------------------------------------------------------------------
#
# Every writer resolves its reference FIRST (a bad name is a usage error, not a
# half-applied change), then opens the store once. They raise AccountsError
# subclasses with a sentence — never a traceback — because the CLI and the page
# both show the message as-is.


def set_default(ref: str | None, *, project: ProjectInfo | None = None) -> ClaudeAccount | None:
    """Make ``ref`` the machine default — or ``project``'s — and return it; ``None`` clears.

    A project default is stored as the SLOT NUMBER, resolved now: the project
    table is ours, and a number cannot go stale the way a typed alias could.
    ``remove`` clears every project default that names the slot it removes.
    """
    account = resolve(ref) if ref is not None else None
    try:
        with store_session() as store:
            _arranged(store)  # the row must exist before it can be the default
            if project is not None:
                store.ensure_project(project)
                if account is None:
                    store.clear_project_setting(project.id, PROJECT_ACCOUNT_KEY)
                else:
                    store.set_project_setting(project.id, PROJECT_ACCOUNT_KEY, str(account.slot))
            else:
                store.set_claude_account_default(account.slot if account else None)
    except sqlite3.Error as exc:
        raise AccountsUnreadable(f"the accounts registry cannot be written ({exc})") from exc
    return account


def project_default(project: ProjectInfo) -> ClaudeAccount | None:
    """The account ``project`` defaults to, or ``None`` when it has none (or it vanished)."""
    slot = _project_default_slot(project)
    if slot is None:
        return None
    return next((a for a in list_accounts() if a.slot == slot), None)


def machine_default() -> ClaudeAccount | None:
    return next((a for a in list_accounts() if a.is_default), None)


def set_alias(ref: str, alias: str | None) -> ClaudeAccount:
    """Name a slot (``None`` unnames it). The name must be free and well-formed."""
    account = resolve(ref)
    normalised = core.normalise_alias(alias) if alias is not None else None
    try:
        with store_session() as store:
            _arranged(store)
            try:
                store.set_claude_account_alias(account.slot, normalised)
            except sqlite3.IntegrityError as exc:
                raise AccountsError(
                    f"the alias {normalised!r} is already taken — see: aisquare accounts"
                ) from exc
            return next(a for a in _arranged(store) if a.slot == account.slot)
    except sqlite3.Error as exc:
        raise AccountsUnreadable(f"the accounts registry cannot be written ({exc})") from exc


def set_disabled(ref: str, disabled: bool) -> ClaudeAccount:
    """Take a slot out of (or back into) automatic selection. It stays usable by name."""
    account = resolve(ref)
    try:
        with store_session() as store:
            _arranged(store)
            store.set_claude_account_disabled(account.slot, disabled)
            return next(a for a in _arranged(store) if a.slot == account.slot)
    except sqlite3.Error as exc:
        raise AccountsUnreadable(f"the accounts registry cannot be written ({exc})") from exc


def reorder(refs: Sequence[str]) -> list[ClaudeAccount]:
    """Put ``refs`` first, in that order; everything else keeps its relative order after them."""
    slots = [resolve(ref).slot for ref in refs]
    try:
        with store_session() as store:
            _arranged(store)
            store.order_claude_accounts(slots)
            return _arranged(store)
    except sqlite3.Error as exc:
        raise AccountsUnreadable(f"the accounts registry cannot be written ({exc})") from exc


Direction = Literal["up", "down", "top", "bottom"]


def move(ref: str, direction: Direction) -> list[ClaudeAccount]:
    """Move one slot a step (or all the way) in the priority order."""
    account = resolve(ref)
    order = [a.slot for a in list_accounts()]
    index = order.index(account.slot)
    order.pop(index)
    if direction == "up":
        order.insert(max(index - 1, 0), account.slot)
    elif direction == "down":
        order.insert(min(index + 1, len(order)), account.slot)
    elif direction == "top":
        order.insert(0, account.slot)
    else:
        order.append(account.slot)
    return reorder([str(slot) for slot in order])


def forget_arrangement(slot: int) -> None:
    """Drop a removed slot's row and every project default that named it.

    Best effort, after the directory is already gone: a registry that cannot
    be written leaves rows the next read prunes anyway (``_arranged``), and a
    project default pointing at a missing slot is reported by ``choose`` and
    ``doctor`` rather than silently honoured.
    """
    with contextlib.suppress(sqlite3.Error), store_session() as store:
        store.delete_claude_account(slot)
        for project_id, value in store.project_settings(PROJECT_ACCOUNT_KEY).items():
            if value == str(slot):
                store.clear_project_setting(project_id, PROJECT_ACCOUNT_KEY)


# --- usage ------------------------------------------------------------------------------


def _http_get(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    """One GET; an HTTP error status is an answer, not an exception."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), bytes(response.read())
    except urllib.error.HTTPError as exc:
        return int(exc.code), bytes(exc.read() or b"")


def _window(raw: Any) -> tuple[float | None, datetime | None]:
    if not isinstance(raw, dict):
        return None, None
    percent = raw.get("utilization")
    resets = raw.get("resets_at")
    when: datetime | None = None
    if isinstance(resets, str) and resets:
        try:
            when = datetime.fromisoformat(resets)
        except ValueError:
            when = None
    value = float(percent) if isinstance(percent, int | float) else None
    return value, when


def usage(
    account: ClaudeAccount, *, now: datetime | None = None, fetch: Fetch | None = None
) -> ClaudeUsage:
    """The account's five-hour and seven-day windows, or why they cannot be read.

    Reads the token from the credentials file and sends it to :data:`USAGE_URL`
    only; a token that has already expired is not sent at all, because Claude
    Code refreshes it on the account's next session and this module never does.
    No reason ever carries the token.
    """
    creds = core.credentials(account)
    if creds is None:
        if core.keychain_platform():
            reason = "credentials are in the macOS Keychain, which the CLI does not read"
        else:
            reason = "no credentials file — not signed in"
        return ClaudeUsage(available=False, reason=reason)
    if creds.expired(now):
        return ClaudeUsage(
            available=False,
            reason="the stored token has expired — open a session on this account to refresh it",
        )
    headers = {
        "Authorization": f"Bearer {creds.access_token}",
        "anthropic-beta": OAUTH_BETA,
        "Accept": "application/json",
        "User-Agent": f"aisquare-cli/{__version__}",
    }
    try:
        status, body = (fetch or _http_get)(USAGE_URL, headers, USAGE_TIMEOUT_SECONDS)
    except OSError as exc:  # URLError, a refused connection, a timeout
        return ClaudeUsage(
            available=False, reason=f"could not reach the usage endpoint ({type(exc).__name__})"
        )
    if status == 401:
        return ClaudeUsage(
            available=False,
            reason="the token was rejected — open a session on this account to refresh it",
        )
    if status != 200:
        return ClaudeUsage(available=False, reason=f"HTTP {status} from the usage endpoint")
    try:
        payload: Any = json.loads(body)
    except ValueError:
        return ClaudeUsage(available=False, reason="the usage endpoint did not answer with JSON")
    if not isinstance(payload, dict):
        return ClaudeUsage(
            available=False, reason="the usage endpoint answered in an unknown shape"
        )
    session_percent, session_resets = _window(payload.get("five_hour"))
    week_percent, week_resets = _window(payload.get("seven_day"))
    if session_percent is None and week_percent is None:
        return ClaudeUsage(
            available=False, reason="the usage endpoint answered without the two windows"
        )
    return ClaudeUsage(
        available=True,
        session_percent=session_percent,
        session_resets_at=session_resets,
        week_percent=week_percent,
        week_resets_at=week_resets,
        fetched_at=now or _now(),
    )


# --- usage over time, and picking by headroom (#146) ---------------------------------------------
#
# One reading says how full a window is; two say how fast it is filling. Every
# fetch made through `sample_usage` leaves a row in `claude_usage` (core.store,
# v16), and `usage_trend` turns this window's rows into "at this pace, N minutes
# to the limit". `headroom_choice` is the automatic pick `[accounts] pick =
# "headroom"` switches on: it reads every enabled, signed-in account ONCE,
# concurrently, and applies one rule — in priority order, the first account
# under `switch_at`; failing that, the one with the most room. Best effort at
# every step, because the endpoint is undocumented (§5): an account that does
# not answer is skipped with a note, and when none answers the caller's next
# rung (the machine default) decides exactly as it did before #146.

TREND_WINDOW = timedelta(minutes=60)
"""How far back `usage_trend` looks for the reading it rates against."""
TREND_MINIMUM = timedelta(minutes=2)
"""Two readings closer than this say nothing about a rate — noise, not a trend."""
HEADROOM_WORKERS = 4
"""How many usage fetches run at once in a headroom pick (one per account, capped)."""


def accounts_settings() -> AccountsSettings:
    """``[accounts]`` — a default like every other one; an unreadable config costs the knobs."""
    try:
        return load_config().accounts
    except Exception:
        return AccountsSettings()


def sample_usage(
    account: ClaudeAccount, *, now: datetime | None = None, fetch: Fetch | None = None
) -> ClaudeUsage:
    """:func:`usage`, and a row in ``claude_usage`` when it answered.

    The row is the only side effect, and it is best effort: a store that cannot
    be opened costs the trend line, never the reading the caller asked for.
    """
    result = usage(account, now=now, fetch=fetch)
    if result.available:
        with contextlib.suppress(sqlite3.Error), store_session() as store:
            store.add_usage_sample(
                UsageSample(
                    slot=account.slot,
                    fetched_at=result.fetched_at or now or _now(),
                    session_percent=result.session_percent,
                    session_resets_at=result.session_resets_at,
                    week_percent=result.week_percent,
                    week_resets_at=result.week_resets_at,
                )
            )
    return result


def usage_trend(
    slot: int, latest: ClaudeUsage, *, now: datetime | None = None
) -> UsageTrend | None:
    """Where the five-hour window is heading, from this window's readings.

    Rates the latest reading against the OLDEST reading of the same window
    (same ``resets_at``) within :data:`TREND_WINDOW`. Readings from before the
    window reset are not a trend — the percentage fell to zero at the reset —
    so they are excluded by the reset time rather than by age. ``None`` when
    there is no usable reading at all; a trend with ``per_hour=None`` when
    there is only the latest one, or the span is under :data:`TREND_MINIMUM`.
    """
    if not latest.available or latest.session_percent is None:
        return None
    moment = latest.fetched_at or now or _now()
    try:
        with store_session() as store:
            samples = store.usage_samples(slot, since=moment - TREND_WINDOW)
    except sqlite3.Error:
        samples = []
    same_window = [
        s
        for s in samples
        if s.session_percent is not None
        and s.session_resets_at == latest.session_resets_at
        and s.fetched_at < moment
    ]
    trend = UsageTrend(percent=latest.session_percent, resets_at=latest.session_resets_at)
    if not same_window:
        return trend
    oldest = same_window[0]
    span = moment - oldest.fetched_at
    if span < TREND_MINIMUM or oldest.session_percent is None:
        return trend
    hours = span.total_seconds() / 3600
    per_hour = (latest.session_percent - oldest.session_percent) / hours
    minutes_to_limit: float | None = None
    if per_hour > 0:
        minutes_to_limit = (100.0 - latest.session_percent) / per_hour * 60
    return trend.model_copy(
        update={
            "per_hour": per_hour,
            "minutes_to_limit": minutes_to_limit,
            "span_minutes": span.total_seconds() / 60,
        }
    )


def describe_trend(trend: UsageTrend | None) -> str:
    """``≈ 40 min to the limit`` / ``≈ 2 h to the limit`` / ``flat`` — or ``""``."""
    if trend is None or trend.per_hour is None:
        return ""
    if trend.minutes_to_limit is None:
        return "flat"
    minutes = trend.minutes_to_limit
    if trend.resets_at is not None:
        # A window that resets before it fills is not going to fill.
        until_reset = (trend.resets_at - (_now())).total_seconds() / 60
        if 0 < until_reset < minutes:
            return "resets before the limit"
    if minutes < 60:
        return f"≈ {max(1, round(minutes))} min to the limit"
    return f"≈ {minutes / 60:.1f} h to the limit"


def headroom_choice(
    accounts: Sequence[ClaudeAccount],
    *,
    switch_at: int,
    exclude: Iterable[int] = (),
    fetch: Fetch | None = None,
    now: datetime | None = None,
) -> tuple[ClaudeAccount | None, list[str]]:
    """The account with headroom, and the notes that say how it was picked.

    Candidates are the enabled, signed-in accounts in priority order, minus
    ``exclude`` (the account a hand-over is leaving). Their usage is read
    concurrently through :func:`sample_usage`, so a pick costs one round trip,
    not one per account. The rule: the FIRST candidate under ``switch_at``
    percent of its five-hour window — priority order is the operator's
    preference, and an account with room keeps it — else the candidate with
    the lowest usage, because "every account is nearly out" still has a least
    bad answer. An account whose usage cannot be read is skipped and named;
    ``None`` when nothing could be measured, so the caller's next rung decides.
    """
    skip = set(exclude)
    candidates = [
        account
        for account in accounts
        if not account.disabled and account.slot not in skip and core.signed_in(account)
    ]
    if not candidates:
        return None, ["headroom: no enabled, signed-in account to pick from"]
    readings: dict[int, ClaudeUsage] = {}
    with ThreadPoolExecutor(max_workers=min(HEADROOM_WORKERS, len(candidates))) as pool:
        futures = {
            pool.submit(sample_usage, account, now=now, fetch=fetch): account.slot
            for account in candidates
        }
        for future, slot in futures.items():
            try:
                readings[slot] = future.result()
            except Exception as exc:  # one account's failure must not cost the pick
                readings[slot] = ClaudeUsage(
                    available=False, reason=f"usage read failed ({type(exc).__name__})"
                )
    measured: list[tuple[ClaudeAccount, float]] = []
    notes: list[str] = []
    for account in candidates:
        reading = readings[account.slot]
        if reading.available and reading.session_percent is not None:
            measured.append((account, reading.session_percent))
        else:
            notes.append(
                f"headroom: {core.label(account)} skipped — {reading.reason or 'no reading'}"
            )
    if not measured:
        notes.append("headroom: no account's usage could be read — the default decides")
        return None, notes
    summary = " · ".join(f"{core.label(a)} {pct:.0f}%" for a, pct in measured)
    under = next((a for a, pct in measured if pct < switch_at), None)
    if under is not None:
        notes.append(f"headroom: {summary} — {core.label(under)} is first under {switch_at}%")
        return under, notes
    least, pct = min(measured, key=lambda pair: pair[1])
    notes.append(
        f"headroom: {summary} — every account is over {switch_at}%; "
        f"{core.label(least)} has the most room ({100 - pct:.0f}% left)"
    )
    return least, notes


# --- signing in -------------------------------------------------------------------------


def begin_sign_in(slot: int | None) -> ClaudeAccount:
    """The account a sign-in is about: a fresh slot when ``slot`` is ``None``.

    Refuses without Claude Code, before making a directory nothing could use.
    """
    if not install().installed:
        raise ClaudeNotInstalled(
            f"Claude Code is not installed — install it first: {core.INSTALL_COMMAND}"
        )
    if slot is None:
        return core.create_account()
    account = core.find_account(slot)
    if account is None:
        raise NoSuchAccount(f"no Claude account in slot {slot} — see: aisquare accounts")
    return account


def sign_in_command(account: ClaudeAccount) -> list[str]:
    """What a sign-in window runs: our own ``accounts run``, so one place sets the environment."""
    return [sys.executable, "-m", "aisquare", "accounts", "run", str(account.slot)]


CARRIED_VARS = (paths.HOME_ENV_VAR, core.CONFIG_DIR_VAR, core.TMPDIR_VAR)
"""What decides which directory a slot IS: the aisquare home, and the default's two variables."""


def carry_environment(
    command: Sequence[str],
    environ: Mapping[str, str] | None = None,
    *,
    cwd: Path | None = None,
) -> tuple[list[str], dict[str, str]]:
    """``command`` and the variables to set so a tmux window sees THIS process's slots.

    A tmux window inherits the environment of whoever started the private
    server, which may be another shell entirely: a different ``CLAUDE_CONFIG_DIR``
    would make ``accounts run 1`` (or ``launch --account 1``) open a login other
    than the one this process calls the default, and a different
    ``AISQUARE_HOME`` would resolve a managed slot under the wrong home. So the
    three variables travel with the window as this process sees them:

    - a variable this process HAS is set on the window (``tmux -e``), as an
      absolute path resolved against this process's working directory — the
      window starts somewhere else (the home directory, an agent's worktree),
      and ``CLAUDE_CONFIG_DIR=./profile`` must keep naming the directory the
      page is reading rather than a sibling of the new cwd;
    - a variable this process does NOT have is unset for the child through
      ``env -u``, because ``-e`` can only set and the server's retained value
      must not leak in as ours. A blank value counts as unset, as it does for
      ``core.claude_accounts.default_config_dir``.
    """
    source = os.environ if environ is None else environ
    base = Path.cwd() if cwd is None else cwd
    to_set: dict[str, str] = {}
    for var in CARRIED_VARS:
        value = source.get(var, "").strip()
        if value:
            to_set[var] = str((base / Path(value).expanduser()).absolute())
    to_unset = [var for var in CARRIED_VARS if var not in to_set]
    argv = list(command)
    if to_unset:
        env_binary = shutil.which("env") or "/usr/bin/env"
        argv = [env_binary, *(flag for var in to_unset for flag in ("-u", var)), *argv]
    return argv, to_set


def sign_in_window_command(
    account: ClaudeAccount,
    environ: Mapping[str, str] | None = None,
    *,
    cwd: Path | None = None,
) -> tuple[list[str], dict[str, str]]:
    """The sign-in window's command and variables: ``accounts run <slot>``, this process's view."""
    return carry_environment(sign_in_command(account), environ, cwd=cwd)


def open_sign_in_window(
    account: ClaudeAccount,
    server: TmuxServer,
    *,
    cwd: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> WindowInfo:
    """Start a Claude Code session for ``account`` in the fleet's tmux server, detached.

    The UI renders the window in a pane and polls :func:`sign_in_landed`; the
    working directory is the home directory, exactly what a fresh terminal
    would give ``claude``; the environment is this process's view of what the
    slot means (:func:`carry_environment`), resolved against THIS process's
    working directory, not the window's.
    """
    command, env = sign_in_window_command(account, environ)
    return server.spawn_window(
        SIGN_IN_SESSION,
        name=f"account-{account.slot}",
        cwd=cwd if cwd is not None else Path.home(),
        command=command,
        env=env,
    )


def sign_in_landed(account: ClaudeAccount) -> ClaudeIdentity | None:
    """Who the directory is now signed in as, once Claude Code has written both files."""
    if not core.signed_in(account):
        return None
    return core.identity(account)


def complete_sign_in(account: ClaudeAccount) -> ClaudeAccountStatus:
    """Wire the newly signed-in directory like any other: aisquare's hooks into its settings."""
    agents_service.connect(AGENT, account.config_dir)
    return describe(account)


def abandon_sign_in(account: ClaudeAccount) -> bool:
    """A sign-in that never landed leaves no slot behind. Returns whether one was discarded."""
    if not account.managed or core.signed_in(account):
        return False
    core.discard_account(account)
    return True


# --- removing -------------------------------------------------------------------------------


def remove(account: ClaudeAccount) -> Path:
    """Forget a managed slot: hooks out of its settings, the directory renamed beside itself."""
    if not account.managed:
        raise AccountsError(
            "slot 1 is the plain claude of this machine, not an account the CLI added — "
            "sign out of it inside Claude Code (/logout) instead"
        )
    # The directory is leaving either way; a settings.json we cannot parse is not a stop.
    with contextlib.suppress(Exception):
        agents_service.disconnect(AGENT, account.config_dir)
    moved = core.remove_account(account)
    # After the rename, never before: the number is free again the moment the
    # directory moves, and a default or alias left behind would be inherited by
    # whatever `add` puts in that slot next.
    forget_arrangement(account.slot)
    return moved


# --- running ------------------------------------------------------------------------------------


def session_env(account: ClaudeAccount, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment a plain session on ``account`` runs with.

    The tracing identity is dropped (``core.spawn.untraced_env``): a session
    started from the Accounts page or from ``accounts run`` is not a board role
    and must not file its work under whichever traced session happened to
    start it.
    """
    env = untraced_env(base)
    return core.apply_launch_env(env, account, shell=base)


def _leave_it_to_the_child(signum: int, frame: Any) -> None:
    """The parent's SIGINT handler while Claude Code runs: the keypress was for the child."""


def run_session(
    account: ClaudeAccount,
    args: Sequence[str] = (),
    *,
    env: Mapping[str, str] | None = None,
) -> int:
    """Run Claude Code on ``account`` in the foreground and wait; the exit status comes back.

    This is the terminal's sign-in: ``aisquare accounts add`` starts the session
    here, the user signs in inside it and leaves it, and the caller then asks
    :func:`sign_in_landed`. Registered in ``core.spawn.SEAMS`` as excluded.

    Ctrl-C belongs to Claude Code while it runs: the terminal delivers SIGINT
    to the whole foreground group, and a first Ctrl-C only interrupts Claude
    Code's current turn, so a parent that died on it would leave the session
    orphaned and the slot half-made. For the child's lifetime this process
    answers SIGINT with a handler that does nothing — a Python-level handler,
    NOT ``SIG_IGN``: an ignored disposition is inherited across ``exec`` and
    would have made Claude Code itself deaf to the signal, where a handler is
    reset to the default in the child. Main thread only (a signal handler
    cannot be set elsewhere), and the previous disposition is restored after.
    """
    found = install()
    if not found.installed or found.binary is None:
        raise ClaudeNotInstalled(
            f"Claude Code is not installed — install it first: {core.INSTALL_COMMAND}"
        )
    previous: Any = None
    on_main_thread = threading.current_thread() is threading.main_thread()
    if on_main_thread:
        previous = signal.signal(signal.SIGINT, _leave_it_to_the_child)
    try:
        completed = subprocess.run(  # argv, never a shell
            [found.binary, *args], env=session_env(account, env), check=False
        )
    finally:
        if on_main_thread:
            signal.signal(signal.SIGINT, previous)
    return int(completed.returncode)
