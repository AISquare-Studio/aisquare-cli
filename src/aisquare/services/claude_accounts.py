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
writes there (on macOS, with the token in its Keychain item) —
:func:`sign_in_landed` — and nothing here ever writes those files. What the
accounts SHARE is the exception, and touches no login: a folder's trust entry
in ``.claude.json``, and (opt-in) ``settings.json`` as a link to the plain
claude's (:func:`share_for_launch`, ``core.claude_sharing``).

**Usage is best effort, on purpose.** The percentages come from the endpoint
Claude Code's own ``/usage`` reads (:data:`USAGE_URL`), called with the access
token the account's stored login holds (``core.stored_login``: the credentials
file, or on macOS the login Keychain). It is not a documented API: it
answered on 2026-09-09 with Claude Code 2.1.266 and its shape is recorded in
``tests/test_claude_accounts.py``, and the day it changes an account's row says
``usage unavailable`` with the reason while everything else on the page keeps
working. It is never called from a hook or on any path a session pays for.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from aisquare.core import agents as agent_core
from aisquare.core import claude_accounts as core
from aisquare.core import claude_sharing as sharing
from aisquare.core import paths
from aisquare.core.config import AccountsSettings, FleetSettings, load_config
from aisquare.core.harness import MODEL_FAMILIES
from aisquare.core.names import name_key
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
    HeadroomLevel,
    HeadroomSummary,
    ProjectInfo,
    StatuslineSession,
    StatuslineSite,
    UsageSample,
    UsageTrend,
    WeekReset,
)
from aisquare.services import agents as agents_service
from aisquare.services import install_route
from aisquare.services import settings as settings_service
from aisquare.statusline import FLEET_FILE as STATUSLINE_FLEET_FILE

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
    """Everything about one slot that can be read without leaving the machine.

    A token the Keychain would not show (locked, refused) is ``unreadable``: neither a
    missing one, which would offer a log-in that cannot help, nor one to use.
    """
    login = core.stored_login(account)
    creds = login.credentials
    return ClaudeAccountStatus(
        account=account,
        label=core.label(account),
        identity=core.identity(account),
        signed_in=core.signed_in(account),
        token_state="unreadable" if login.unreadable is not None else core.token_state(creds),
        subscription=core.subscription_label(creds),
        hooks_installed=_hooks_installed(account),
    )


def _hooks_installed(account: ClaudeAccount) -> bool:
    """Whether aisquare runs in this slot: the one shared answer, which never raises.

    So a slot reads as the doctor's row and ``agents status`` read it, and a
    settings.json that cannot be read is "not installed" rather than a crash.
    """
    return agents_service.claude_code_connected(account.config_dir)


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
    vanished = [slot for slot in rows if slot not in on_disk]
    if vanished:
        # One delete for all of them, so the order is renumbered once (the store
        # closes the gaps), and the rows read before it are stale: re-read rather
        # than hand back positions with a hole where a pruned slot was.
        store.delete_claude_accounts(vanished)
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


def _read_registry(
    project: ProjectInfo | None = None,
) -> tuple[list[ClaudeAccount], str | None, int | None]:
    """The arranged list, the reason the registry was not read, and ``project``'s default slot.

    One store open for both, behind one fail-open: the project-default rung
    used to open a second session of its own with no ``sqlite3.Error`` guard,
    so a store going unreadable between the two opens raised out of ``choose``
    and out of ``launch`` (review of #205, third round).
    """
    try:
        with store_session() as store:
            accounts = _arranged(store)
            raw = (
                store.project_setting(project.id, PROJECT_ACCOUNT_KEY)
                if project is not None
                else None
            )
    except sqlite3.Error as exc:
        return core.list_accounts(), f"accounts registry unreadable ({exc})", None
    return accounts, None, int(raw) if raw is not None and raw.isdigit() else None


def _read_arranged() -> tuple[list[ClaudeAccount], str | None]:
    """The arranged list, or the plain directories plus the reason the registry was not read."""
    accounts, note, _slot = _read_registry()
    return accounts, note


def _read_for_handover() -> tuple[list[ClaudeAccount] | None, str | None]:
    """:func:`_read_arranged` for a hand-over: ``None`` for the list, with why, when the slot
    directories could not even be listed (an ``OSError``), which raised out of the worker
    with nothing on the board (review of #259, round 15). One read for the hook's question
    (:func:`somewhere_to_hand_over`) and the worker's pick (:func:`choose_for_handover`),
    so the two agree."""
    try:
        return _read_arranged()
    except OSError as exc:
        return None, f"accounts unreadable ({exc})"


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
    ``@``, and an alias is never all digits and has no ``@``
    (``core.normalise_alias``). Lookups are case-insensitive, an alias's
    included: ``Work`` is stored as typed and ``--account work`` finds it.
    """
    accounts, note = _read_arranged()
    return _resolve_in(accounts, ref, note=note)


def _resolve_in(
    accounts: Sequence[ClaudeAccount], ref: str | int, *, note: str | None = None
) -> ClaudeAccount:
    """:func:`resolve` against an arranged list already in hand.

    ``choose`` reads the registry ONCE and resolves the flag and the binding
    against that read; before, every rung reopened the store and re-ran the
    reconcile upsert, two or three times per launch (review of #205, finding 12).
    """
    text = str(ref).strip()
    if text.isdigit():
        account = next((a for a in accounts if a.slot == int(text)), None)
        if account is None:
            raise NoSuchAccount(f"no Claude account in slot {text} — see: aisquare accounts")
        return account
    wanted = text.lower()
    if "@" not in wanted:
        by_alias = next((a for a in accounts if _same_alias(a.alias, text)), None)
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


def _same_alias(alias: str | None, typed: str) -> bool:
    """Whether ``typed`` names ``alias``: an alias keeps its case and its variation selectors,
    and is matched without either (``core.names.name_key``), so ``work`` U+FE00 is ``work``."""
    return alias is not None and name_key(alias) == name_key(typed.strip())


def slot_labels(store: ContextStore | None = None) -> dict[int, str]:
    """Slot → the label the Accounts page and the launch line use (the alias when one is set).

    For the surfaces that know an agent only by its slot — the agent header,
    ``fleet ls`` — so an aliased account reads ``work`` everywhere rather than
    ``account 2`` on two of them (review of #205, finding 10). Fails open to
    ``{}``: a label is a courtesy, and every caller falls back to ``account N``.
    ``store`` is a caller's own open handle — a hook rendering the board while it
    holds one must not open a second connection under its own pending write.
    """
    try:
        accounts = _arranged(store) if store is not None else list_accounts()
        return {account.slot: core.label(account) for account in accounts}
    except Exception:
        return {}


def slot_of(config_dir: str | Path) -> int | None:
    """Which slot a Claude config directory IS: a managed slot's number, 1 for the plain
    claude's directory, ``None`` for a directory the CLI does not own (a hand-made layout).

    A reader, not a decider: ``services.team.session_account`` records the
    directory a session runs under from its transcript path, and this turns it
    back into the slot a hand-over (#146) is leaving.

    The plain claude's directory is ``core.default_config_dir``, which knows a
    process that runs under one of OUR slots. Every hook of an agent on a
    managed slot does, with ``CLAUDE_CONFIG_DIR`` naming that slot, and read
    straight the variable made an aliased slot 1 ``.claude`` on the board
    those hooks render (review of #205, fifth round).
    """
    managed = core.managed_slot(config_dir)
    if managed is not None:
        return managed
    try:
        if Path(config_dir).resolve() == core.default_config_dir().resolve():
            return core.DEFAULT_SLOT
    except (OSError, RuntimeError):  # RuntimeError: a symlink loop, on 3.11 and 3.12
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
    family: str | None = None,
) -> AccountChoice:
    """THE account resolver: which Claude account a launch runs under, and why.

    The ladder, top rung wins::

        --account <ref>            the flag (a slot, alias or email)
        team bind <role> --account the role's binding
        accounts default --project the project's default
        accounts default           the machine default
        (nothing)                  the launch environment is left untouched

    ``launch``, ``fleet spawn`` and App settings all ask here and nowhere
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
    :func:`headroom_choice`, the account with room in both of its windows,
    ``exclude`` naming the slot a hand-over is leaving. When no account's usage
    can be read, the machine default decides exactly as before. A launch reads
    usage through the cache, a minute deep (:data:`PICK_MAX_AGE_SECONDS`); a
    hand-over (``spread=True``) reads it live.

    ``family`` is the launch's model family (``core.harness.launch_family``), for the
    headroom rung's per-family weeks and limit marks (#277).

    ``exclude`` is honoured on EVERY rung: a binding or a project default that
    names the account being left is skipped with a note and the ladder goes on,
    so a hand-over on a machine that arranged its accounts does not re-pick the
    account it is leaving (review of #205, finding 2). The registry and the
    ``[accounts]`` settings are read once per call (finding 12).
    """
    skip = frozenset(exclude)  # once: an iterator would be spent by its first reader
    accounts, registry_note, project_slot = _read_registry(project)
    if explicit is not None:
        return AccountChoice(_resolve_in(accounts, explicit, note=registry_note), "flag")
    notes: list[str] = []
    bound, note = _role_binding_ref(role)
    if note is not None:
        notes.append(note)
    if bound:
        try:
            account: ClaudeAccount | None = _resolve_in(accounts, bound, note=registry_note)
        except NoSuchAccount as exc:
            if registry_note is None:
                raise NoSuchAccount(
                    f"the role binding for {role!r} names account {bound!r}: {exc}"
                ) from exc
            # The fallback list carries no aliases, so an alias binding cannot be
            # checked while the registry is unreadable: that costs this rung and
            # leaves a note — never the launch, which is the bar
            # tests/test_launch_survives_a_damaged_store.py holds (review of
            # #205, second round).
            account = None
            notes.append(
                f"the role binding for {role!r} names account {bound!r}, which cannot be "
                f"resolved while the {registry_note} — skipped"
            )
        if account is None:
            pass
        elif account.disabled:
            notes.append(f"{core.label(account)} (bound to {role}) is disabled — skipped")
        elif account.slot in skip:
            notes.append(
                f"{core.label(account)} (bound to {role}) is the account being left — skipped"
            )
        else:
            return AccountChoice(account, "role binding", notes)
    if registry_note is not None:
        notes.append(registry_note)
        return AccountChoice(None, None, notes)
    if project is not None:
        slot = project_slot
        if slot is not None:
            preferred = next((a for a in accounts if a.slot == slot), None)
            if preferred is None:
                raise NoSuchAccount(
                    f"the default account of {project.root.name or project.id} is slot {slot}, "
                    "which no longer exists — see: aisquare accounts default --project"
                )
            if preferred.disabled:
                notes.append(f"{core.label(preferred)} (project default) is disabled — skipped")
            elif preferred.slot in skip:
                notes.append(
                    f"{core.label(preferred)} (project default) is the account being left — skipped"
                )
            else:
                return AccountChoice(preferred, "project default", notes)
    settings = accounts_settings()
    by_headroom = spread if spread is not None else settings.pick == "headroom"
    if by_headroom:
        picked, more = headroom_choice(
            accounts,
            switch_at=settings.switch_at,
            exclude=skip,
            fetch=fetch,
            max_age=PICK_MAX_AGE_SECONDS if spread is None else None,
            family=family,
            prefer=settings.prefer,
        )
        if spread is None:
            # The default rung since 0.8.1, so every machine walks it: one with no account
            # to pick from (an API key, no login yet) is not told so on every launch. A
            # hand-over that finds none still says it.
            more = [note for note in more if note != _NOTHING_TO_PICK]
        notes.extend(more)
        if picked is not None:
            return AccountChoice(picked, "headroom", notes)
    default = next((a for a in accounts if a.is_default), None)
    if default is not None:
        if default.disabled:
            notes.append(f"{core.label(default)} (machine default) is disabled — skipped")
        elif default.slot in skip:
            # Said like the two rungs above: skipped in silence, a hand-over that
            # found nothing was refused with no word that the last rung named the
            # account being left (review of #205, fourth round).
            notes.append(
                f"{core.label(default)} (machine default) is the account being left — skipped"
            )
        else:
            return AccountChoice(default, "machine default", notes)
    return AccountChoice(None, None, notes)


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
    if account is not None and account.disabled:
        # Accepted silently, the default was a rung `choose` skipped on every
        # launch, and only doctor said so (review of #205, third round).
        raise AccountsError(
            f"{core.label(account)} (slot {account.slot}) is disabled, so no launch would "
            f"pick it — enable it first: aisquare accounts enable {account.slot}"
        )
    try:
        with store_session() as store:
            _arranged(store)  # the row must exist before it can be the default
            if project is not None:
                if account is None:
                    # A delete, which needs no row: registering the directory first
                    # brought a forgotten project back (review of #168, round 2).
                    store.clear_project_setting(project.id, PROJECT_ACCOUNT_KEY)
                else:
                    # Choosing the account a project launches under is choosing the
                    # project (#139): it is listed from now on, as after a `context
                    # add --project` there. Clearing that choice adds nothing.
                    store.onboard_project(project)
                    store.set_project_setting(project.id, PROJECT_ACCOUNT_KEY, str(account.slot))
            else:
                store.set_claude_account_default(account.slot if account else None)
    except KeyError as exc:
        raise NoSuchAccount(_vanished(account)) from exc
    except sqlite3.Error as exc:
        raise AccountsUnreadable(f"the accounts registry cannot be written ({exc})") from exc
    return account


def _vanished(account: ClaudeAccount | None) -> str:
    """The store refused a slot the reconcile had just seen: removed underneath us."""
    slot = account.slot if account is not None else "?"
    return (
        f"no Claude account in slot {slot} any more — it was removed meanwhile; "
        "see: aisquare accounts"
    )


def project_default(project: ProjectInfo) -> ClaudeAccount | None:
    """The account ``project`` defaults to, or ``None`` when it has none (or it vanished).

    One guarded store open for the slot and the list it is looked up in
    (:func:`_read_registry`, as ``choose`` reads them): it took two, the second
    with a directory scan of its own (review of #205, fourth round). An
    unreadable store reads as no default, as it always did.
    """
    accounts, _note, slot = _read_registry(project)
    if slot is None:
        return None
    return next((a for a in accounts if a.slot == slot), None)


def machine_default() -> ClaudeAccount | None:
    return next((a for a in list_accounts() if a.is_default), None)


def set_alias(ref: str, alias: str | None) -> ClaudeAccount:
    """Name a slot (``None`` unnames it). The name must be free and well-formed.

    Free whatever its case: the store's unique index compares the stored
    text, and ``Work`` beside ``work`` would be two slots that ``--account
    work`` cannot tell apart, so another slot's name in any case is taken.
    """
    account = resolve(ref)
    normalised = core.normalise_alias(alias, slot=account.slot) if alias is not None else None
    try:
        with store_session() as store:
            arranged = _arranged(store)
            if normalised is not None and any(
                other.slot != account.slot and _same_alias(other.alias, normalised)
                for other in arranged
            ):
                raise AccountsError(
                    f"the alias {normalised!r} is already taken — see: aisquare accounts"
                )
            try:
                store.set_claude_account_alias(account.slot, normalised)
            except sqlite3.IntegrityError as exc:
                raise AccountsError(
                    f"the alias {normalised!r} is already taken — see: aisquare accounts"
                ) from exc
            except KeyError as exc:
                raise NoSuchAccount(_vanished(account)) from exc
            return next(a for a in _arranged(store) if a.slot == account.slot)
    except sqlite3.Error as exc:
        raise AccountsUnreadable(f"the accounts registry cannot be written ({exc})") from exc


def set_disabled(ref: str, disabled: bool) -> ClaudeAccount:
    """Take a slot out of (or back into) automatic selection. It stays usable by name."""
    account = resolve(ref)
    try:
        with store_session() as store:
            _arranged(store)
            try:
                store.set_claude_account_disabled(account.slot, disabled)
            except KeyError as exc:
                raise NoSuchAccount(_vanished(account)) from exc
            return next(a for a in _arranged(store) if a.slot == account.slot)
    except sqlite3.Error as exc:
        raise AccountsUnreadable(f"the accounts registry cannot be written ({exc})") from exc


def reorder(refs: Sequence[str]) -> list[ClaudeAccount]:
    """Put ``refs`` first, in that order; everything else keeps its relative order after them.

    One registry read for every reference, one store open for the write: each
    ``resolve`` used to reopen the store and rescan the directories, N times
    per ``↑`` (review of #205, third round).
    """
    accounts, note = _read_arranged()
    slots = [_resolve_in(accounts, ref, note=note).slot for ref in refs]
    return _write_order(slots)


def _write_order(slots: Sequence[int]) -> list[ClaudeAccount]:
    try:
        with store_session() as store:
            _arranged(store)
            store.order_claude_accounts(slots)
            return _arranged(store)
    except sqlite3.Error as exc:
        raise AccountsUnreadable(f"the accounts registry cannot be written ({exc})") from exc


Direction = Literal["up", "down", "top", "bottom"]


def move(ref: str, direction: Direction) -> list[ClaudeAccount]:
    """Move one slot a step (or all the way) in the priority order — one read, one write."""
    accounts, note = _read_arranged()
    account = _resolve_in(accounts, ref, note=note)
    order = [a.slot for a in accounts]
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
    return _write_order(order)


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
        # The readings too: keyed on the slot alone, they would rate the next
        # occupant's first window against this login's history (third round).
        store.delete_usage_samples(slot)


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
    """One window's percent USED and its reset; a non-null ``locked_reason`` reads 100 %: the
    endpoint says the window refuses requests, whatever its figure (#277)."""
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
    if raw.get("locked_reason") is not None:
        value = 100.0
    return value, when


_FAMILY_WEEK = "seven_day_"
"""The prefix of a per-family weekly window in the usage payload: ``seven_day_opus``…"""
_FAMILIES = frozenset(MODEL_FAMILIES.values())
"""The model families a per-family window or limit can name (``opus``, ``sonnet``…)."""
_SESSION_KINDS = frozenset({"session", "five_hour"})
_WEEK_KINDS = frozenset({"weekly", "week", "seven_day"})


def _fuller(a: float | None, b: float | None) -> float | None:
    return b if a is None else a if b is None else max(a, b)


def _family_weeks(payload: Mapping[str, Any]) -> tuple[dict[str, float], dict[str, datetime]]:
    """Every per-family week in ``payload``: each top-level ``seven_day_<family>`` that holds
    a window, keyed by ``<family>``, with a ``limits[]`` entry of that family's kind folded in
    (the fuller figure wins). A family week the endpoint sends as null is no reading."""
    percents: dict[str, float] = {}
    resets: dict[str, datetime] = {}
    for key, raw in payload.items():
        if not key.startswith(_FAMILY_WEEK) or not isinstance(raw, dict):
            continue
        family = key.removeprefix(_FAMILY_WEEK)
        percent, when = _window(raw)
        if percent is not None:
            percents[family] = percent
        if when is not None:
            resets[family] = when
    for kind, percent in _limits(payload):
        family = kind.removeprefix(_FAMILY_WEEK)
        if family in _FAMILIES:
            percents[family] = max(percents.get(family, percent), percent)
    return percents, resets


def _limits(payload: Mapping[str, Any]) -> list[tuple[str, float]]:
    """``limits[]`` as ``(kind, percent)``: an entry without a string kind or a number is
    skipped — the field is undocumented, and an unknown shape costs it, never the reading."""
    raw = payload.get("limits")
    found: list[tuple[str, float]] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        kind, percent = entry.get("kind"), entry.get("percent", entry.get("utilization"))
        if isinstance(kind, str) and isinstance(percent, int | float):
            found.append((kind.lower(), float(percent)))
    return found


def _limit_of(payload: Mapping[str, Any], kinds: frozenset[str]) -> float | None:
    return max((percent for kind, percent in _limits(payload) if kind in kinds), default=None)


def usage(
    account: ClaudeAccount, *, now: datetime | None = None, fetch: Fetch | None = None
) -> ClaudeUsage:
    """The account's five-hour and seven-day windows, or why they cannot be read.

    Every window the endpoint sends is read (#277): the per-family weeks
    (``seven_day_opus``, ``seven_day_sonnet``: :attr:`ClaudeUsage.family_week_percent`),
    the ``limits[]`` entries, folded into the window of their kind as the fuller figure,
    and a ``locked_reason``, which reads that window 100 % used. Keys it does not know
    are ignored.

    Reads the token where Claude Code keeps it (``core.stored_login``: the
    credentials file, or on macOS the login Keychain) and sends it to
    :data:`USAGE_URL` only; a token that has already expired is not sent at all,
    because Claude Code refreshes it on the account's next session and this
    module never does. A Keychain that would not show the token says why
    (locked, refused), and asks for no log-in. No reason ever carries the token.
    """
    login = core.stored_login(account)
    creds = login.credentials
    if creds is None:
        if login.unreadable is not None:
            reason = login.unreadable
        elif core.keychain_platform():
            reason = "no login in the Keychain or a credentials file — not signed in"
        else:
            reason = "no credentials file — not signed in"
        return ClaudeUsage(available=False, reason=reason)
    if creds.expired(now):
        return ClaudeUsage(
            available=False,
            reason="the stored token has expired — open a session on this account to refresh it",
            needs_login=True,
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
            needs_login=True,
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
    family_percent, family_resets = _family_weeks(payload)
    return ClaudeUsage(
        available=True,
        session_percent=_fuller(session_percent, _limit_of(payload, _SESSION_KINDS)),
        session_resets_at=session_resets,
        week_percent=_fuller(week_percent, _limit_of(payload, _WEEK_KINDS)),
        week_resets_at=week_resets,
        family_week_percent=family_percent,
        family_week_resets_at=family_resets,
        fetched_at=now or _now(),
    )


# --- usage over time, and picking by headroom (#146) ---------------------------------------------
#
# One reading says how full a window is; two say how fast it is filling. Every
# fetch made through `sample_usage` leaves a row in `claude_usage` (core.store,
# v16), and `usage_trend` turns this window's rows into "at this pace, N minutes
# to the limit". `headroom_choice` is the automatic pick `[accounts] pick =
# "headroom"` switches on: it reads every enabled, signed-in account ONCE,
# concurrently, and applies one rule to the fuller of each account's two
# windows (`headroom_percent`) — in priority order, the first account under
# `switch_at`; failing that, the one with the most room. Best effort at
# every step, because the endpoint is undocumented (§5): an account that does
# not answer is skipped with a note, and when none answers the caller's next
# rung (the machine default) decides exactly as it did before #146.

TREND_WINDOW = timedelta(minutes=60)
"""How far back `usage_trend` looks for the reading it rates against."""
TREND_MINIMUM = timedelta(minutes=2)
"""Two readings closer than this say nothing about a rate — noise, not a trend."""
HEADROOM_WORKERS = 4
"""How many usage fetches run at once when several accounts are read (one per account, capped)."""

RESET_JITTER = timedelta(seconds=60)
"""How far apart two ``resets_at`` readings may be and still name the SAME window.

Measured on the live endpoint (2026-09-13): one window's reset came back as
08:59:59.86, 09:00:00.26 and 08:59:59.62 on three calls seconds apart. Compared
for equality, every reading was a window of its own and no trend was ever
computed in production (review of #205, finding 3). A new window is hours away,
never seconds, so a minute tells them apart safely."""


def _same_window(a: datetime | None, b: datetime | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= RESET_JITTER


def accounts_settings() -> AccountsSettings:
    """``[accounts]`` — a default like every other one; an unreadable config costs the knobs."""
    try:
        return load_config().accounts
    except Exception:
        return AccountsSettings()


def somewhere_to_hand_over(leaving: int | None = None) -> bool:
    """Whether a hand-over of an agent on slot ``leaving`` has an account to go to: an
    enabled, signed-in one other than ``leaving``. Not knowing which slot the agent is on
    (``None``), two such accounts, so that one is not the agent's.

    Where the registry cannot be read, the answer comes from the slot directories it falls
    back to: which accounts are enabled is not known, but a disabled flag only takes an
    account away, so with no other signed-in slot there no hand-over can succeed, and a
    yes started a worker to say "not switched" at every limit (review of #259, round 15).
    With one, the hand-over is started (a no stopped the hook without a word, round 14), and
    :func:`choose_for_handover` picks nothing from that registry and says why, which the
    worker posts ("not switched — accounts registry unreadable …"). Where the slot
    directories cannot even be listed, nothing is known, and the answer is yes, for the
    worker to say so.

    ``on_limit = "switch"`` is the default since 0.8.1, so a machine with one account has it
    too. There every limit started a hand-over that could only refuse, posted "not
    switched" on the board each time, and held Remote's push of the limit 90 s for it
    (review of #259, round 13). ``choose`` drops its "nothing to pick" note for ``pick`` on
    the same grounds.
    """
    accounts, _note = _read_for_handover()
    if accounts is None:
        return True
    usable = [
        account.slot for account in accounts if not account.disabled and core.signed_in(account)
    ]
    if leaving is None:
        return len(usable) >= 2
    return any(slot != leaving for slot in usable)


def leaving_slot(account_slot: int | None, account: str | None) -> int | None:
    """The slot a hand-over moves an agent from, read as ``fleet.switch`` reads where it is:
    the row's (``FleetAgent.account_slot``, #145), else its session's config dir's
    (:func:`slot_of`); ``None`` when neither says.

    The StopFailure hook and Remote's needs scan both ask :func:`somewhere_to_hand_over`
    with it, so they agree. Remote asked with no slot, "two usable accounts", and the two
    disagreed where the agent's own account was no longer usable: the hook handed it over
    to the one left while Remote pushed the limit at once (review of #259, round 14).
    """
    if account_slot is not None:
        return account_slot
    return slot_of(account) if account else None


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
            _record_sample(store, account, result, now)
    return result


def _record_sample(
    store: ContextStore, account: ClaudeAccount, result: ClaudeUsage, now: datetime | None
) -> None:
    """The reading's row — and a limit mark for each family week it reads spent, which the
    row has no column for: a launch picks from the cache, and without the mark it would put
    an Opus agent on an account whose Opus week a live reading saw spent (#277)."""
    for family, percent in result.family_week_percent.items():
        resets = result.family_week_resets_at.get(family)
        if percent >= 100 and resets is not None:
            _write_limit(store, account.slot, family, resets, how="read", now=now)
    store.add_usage_sample(
        UsageSample(
            slot=account.slot,
            fetched_at=result.fetched_at or now or _now(),
            session_percent=result.session_percent,
            session_resets_at=result.session_resets_at,
            week_percent=result.week_percent,
            week_resets_at=result.week_resets_at,
            token_fingerprint=_token_fingerprint(account),
        )
    )


def _token_fingerprint(account: ClaudeAccount, now: datetime | None = None) -> str | None:
    """Which login a reading belongs to: the :func:`login_fingerprint` of the token
    :func:`usage` would send, or ``None`` when it would send none — no credentials file,
    or, given ``now``, a token already expired at ``now``.

    Read when the reading is recorded, a few milliseconds after the fetch sent it: a
    token swapped in between is a race no surface could act on.
    """
    creds = core.credentials(account)
    if creds is None or (now is not None and creds.expired(now)):
        return None
    return _login_digest(creds)


def usage_trend(
    slot: int,
    latest: ClaudeUsage,
    *,
    now: datetime | None = None,
    store: ContextStore | None = None,
) -> UsageTrend | None:
    """Where the five-hour window is heading, from this window's readings.

    Rates the latest reading against the OLDEST reading of the same window
    (a ``resets_at`` within :data:`RESET_JITTER` of the latest one's) within
    :data:`TREND_WINDOW`. Readings from before the
    window reset are not a trend — the percentage fell to zero at the reset —
    so they are excluded by the reset time rather than by age. ``None`` when
    there is no usable reading at all; a trend with ``per_hour=None`` when
    there is only the latest one, or the span is under :data:`TREND_MINIMUM`.
    """
    if not latest.available or latest.session_percent is None:
        return None
    moment = latest.fetched_at or now or _now()
    try:
        if store is not None:  # the caller's open handle (the page's tick, one open for all)
            samples = store.usage_samples(slot, since=moment - TREND_WINDOW)
        else:
            with store_session() as opened:
                samples = opened.usage_samples(slot, since=moment - TREND_WINDOW)
    except sqlite3.Error:
        samples = []
    same_window = [
        s
        for s in samples
        if s.session_percent is not None
        and _same_window(s.session_resets_at, latest.session_resets_at)
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
        # No limit to project: the window is not filling. A negative rate is a
        # window EMPTYING (another session ended, or just after a reset), which
        # "flat" beside a falling percentage misdescribed (third round).
        return "falling" if trend.per_hour < 0 else "flat"
    minutes = trend.minutes_to_limit
    if trend.resets_at is not None:
        # A window that resets before it fills is not going to fill.
        until_reset = (trend.resets_at - (_now())).total_seconds() / 60
        if 0 < until_reset < minutes:
            return "resets before the limit"
    if minutes < 60:
        return f"≈ {max(1, round(minutes))} min to the limit"
    return f"≈ {minutes / 60:.1f} h to the limit"


def read_usage(
    accounts: Sequence[ClaudeAccount],
    *,
    now: datetime | None = None,
    fetch: Fetch | None = None,
) -> dict[int, ClaudeUsage]:
    """Every account's reading, RECORDED, read concurrently: one round trip, not one per account.

    The one fetcher behind every usage surface — a hand-over's pick and
    ``doctor --live`` call it directly, everything else through
    :func:`cached_usage` (review of #205, finding 11): four accounts and a slow
    endpoint cost one ``USAGE_TIMEOUT_SECONDS``, not four. A reader that raises
    costs its own slot a reading, never the others'.

    The threads only fetch; every answered reading is then recorded in ONE
    store session, after the round. Recorded by each thread, a headroom pick —
    every launch under ``pick = "headroom"``, every hand-over — was up to four
    concurrent writers on ``context.db``'s one write lock, on the launch path
    (review of #205, fourth round). Best effort, as :func:`sample_usage` is: a
    store that cannot be opened costs the history, never the readings.
    """
    if not accounts:
        return {}
    readings: dict[int, ClaudeUsage] = {}
    with ThreadPoolExecutor(max_workers=min(HEADROOM_WORKERS, len(accounts))) as pool:
        futures = {
            pool.submit(usage, account, now=now, fetch=fetch): account.slot for account in accounts
        }
        for future, slot in futures.items():
            try:
                readings[slot] = future.result()
            except Exception as exc:  # one account's failure must not cost the rest
                readings[slot] = ClaudeUsage(
                    available=False, reason=f"usage read failed ({type(exc).__name__})"
                )
    answered = [account for account in accounts if readings[account.slot].available]
    if answered:
        with contextlib.suppress(sqlite3.Error), store_session() as store:
            for account in answered:
                _record_sample(store, account, readings[account.slot], now)
    return readings


# --- the shared five-minute cache -------------------------------------------------------------
#
# Every surface that only SHOWS usage — the Accounts page, the sidebar's dot,
# `accounts usage` — reads through `cached_usage`, so between them an account is
# asked at most once per USAGE_MAX_AGE_SECONDS. The cache is the `claude_usage`
# history every recording reader already writes, so it is shared across
# processes for free: a reading `accounts usage` took two minutes ago serves the
# page. Only answered readings are recorded there; a fetch that went out and
# failed (a rejected token, an unreachable endpoint) is remembered in-process.
# Both are keyed on the token: a recorded reading is served only to the token
# that took it (a fingerprint is kept with it, schema v28), so a refreshed token
# or another login is asked at once, and a slot that would send no token at all
# (signed out, expired) is told so rather than shown its last figure. Of the two,
# the NEWER wins: a refresh that found the token rejected is not undone by the
# success recorded before it. A failure that says nothing of the token (an
# unreachable endpoint, an HTTP 5xx) is the exception: it never hides a good
# reading still inside the window, which it does not contradict (review of #261,
# m4). Calls for the same slot in one process share one fetch: the second waits
# for the first and is served what it read. A launch's headroom pick reads
# through the cache too, a minute deep (PICK_MAX_AGE_SECONDS); a hand-over and
# `doctor --live` still read live (`read_usage`): one moves an agent off a full
# account, the other was asked to look now.

USAGE_MAX_AGE_SECONDS = 300.0
"""How old a recorded reading may be and still be served instead of a fetch: five minutes."""

_UNANSWERED: dict[tuple[str, str], ClaudeUsage] = {}
"""Fetches that went out and failed, by (config dir, fingerprint of the token sent): see above."""
_UNANSWERED_LOCK = threading.Lock()
_FETCH_LOCKS: dict[str, threading.Lock] = {}
"""One per config dir, held while that slot is fetched: see :func:`_fetching`."""
_FETCH_ROUNDS: dict[str, int] = {}
"""How many fetch rounds each config dir has had in this process: see :func:`_fetching`."""
_FETCH_LOCKS_LOCK = threading.Lock()


def _fetch_rounds(accounts: Sequence[ClaudeAccount]) -> dict[str, int]:
    """Each account's :data:`_FETCH_ROUNDS` count now, read BEFORE the cache is looked at."""
    with _FETCH_LOCKS_LOCK:
        return {
            str(account.config_dir): _FETCH_ROUNDS.get(str(account.config_dir), 0)
            for account in accounts
        }


@contextlib.contextmanager
def _fetching(accounts: Sequence[ClaudeAccount], seen: Mapping[str, int]) -> Iterator[bool]:
    """Hold each account's fetch lock while its usage is fetched; yields whether another call
    ended a round for any of them since ``seen`` (:func:`_fetch_rounds`) was read.

    The sidebar's read and the page's, or either and a launch's pick, asked for the same
    slot at the same moment and each sent a request (review of #261, m4). Now the second
    waits for the first, and when a round ended since it looked, it looks again and is
    served what that round read. Counted rounds, not "had to wait": a call can miss the
    cache just before another records and take the lock just after it is let go. Taken
    in one order, so two calls over overlapping slots cannot deadlock.
    """
    keys = sorted({str(account.config_dir) for account in accounts})
    with _FETCH_LOCKS_LOCK:
        locks = [_FETCH_LOCKS.setdefault(key, threading.Lock()) for key in keys]
    held: list[threading.Lock] = []
    try:
        for lock in locks:
            lock.acquire()
            held.append(lock)
        with _FETCH_LOCKS_LOCK:
            moved = any(_FETCH_ROUNDS.get(key, 0) != seen.get(key, 0) for key in keys)
        yield moved
    finally:
        if len(held) == len(locks):
            with _FETCH_LOCKS_LOCK:
                for key in keys:
                    _FETCH_ROUNDS[key] = _FETCH_ROUNDS.get(key, 0) + 1
        for lock in reversed(held):
            lock.release()


def _recorded_usage(
    accounts: Sequence[ClaudeAccount],
    tokens: Mapping[int, str],
    *,
    since: datetime,
    now: datetime,
) -> dict[int, ClaudeUsage]:
    """Each account's newest reading recorded at or after ``since`` WITH the token it holds
    now (``tokens``, by slot), in one store session.

    A reading another token took is not served: a slot signed into another login
    since would show the last one's figure. Nor is one of whose windows has reset
    since: the window it describes is over, and the new one starts near 0 %.
    """
    found: dict[int, ClaudeUsage] = {}
    try:
        with store_session() as store:
            for account in accounts:
                token = tokens.get(account.slot)
                if token is None:
                    continue
                samples = store.usage_samples(account.slot, since=since)
                taken = [sample for sample in samples if sample.token_fingerprint == token]
                if not taken:
                    continue
                newest = taken[-1]
                resets = (newest.session_resets_at, newest.week_resets_at)
                if any(when is not None and when <= now for when in resets):
                    continue
                found[account.slot] = ClaudeUsage(
                    available=True,
                    session_percent=newest.session_percent,
                    session_resets_at=newest.session_resets_at,
                    week_percent=newest.week_percent,
                    week_resets_at=newest.week_resets_at,
                    fetched_at=newest.fetched_at,
                )
    except sqlite3.Error:
        return {}
    return found


def _served(
    accounts: Sequence[ClaudeAccount],
    tokens: Mapping[int, str],
    *,
    since: datetime,
    now: datetime,
) -> dict[int, ClaudeUsage]:
    """What the cache answers for each account, from ``since`` on: the reading recorded with
    the token it holds now, or the failed fetch with that token, by :func:`_preferred`.
    """
    recorded = _recorded_usage(accounts, tokens, since=since, now=now)
    served: dict[int, ClaudeUsage] = {}
    for account in accounts:
        token = tokens.get(account.slot)
        if token is None:
            continue
        with _UNANSWERED_LOCK:
            failed = _UNANSWERED.get((str(account.config_dir), token))
        if failed is not None and (failed.fetched_at is None or failed.fetched_at < since):
            failed = None
        if (answer := _preferred(recorded.get(account.slot), failed)) is not None:
            served[account.slot] = answer
    return served


def _preferred(good: ClaudeUsage | None, failed: ClaudeUsage | None) -> ClaudeUsage | None:
    """Which of a slot's good reading and its failed try, both within the window, it is shown.

    The NEWER, except that a failure which says nothing of the token (``needs_login``
    False: an unreachable endpoint, an HTTP 5xx) never hides a good reading: it does not
    contradict the figure, so the figure is shown, marked with when the try failed
    (``last_failed_at``) for the line that says how old it is. A 401 does contradict it,
    and wins when newer. One rule for a read the cache serves and for a forced round's
    answer: applied to the plain read alone, a forced refresh that failed showed the
    failure, and the next plain read showed the figure with no word of it (review of
    #261, m4, reopened).
    """
    if failed is None or good is None:
        return good or failed
    if failed.fetched_at is None or (
        good.fetched_at is not None and failed.fetched_at <= good.fetched_at
    ):
        return good  # the latest try read it
    if failed.needs_login:
        return failed
    return good.model_copy(update={"last_failed_at": failed.fetched_at})


def cached_usage(
    accounts: Sequence[ClaudeAccount],
    *,
    max_age: float = USAGE_MAX_AGE_SECONDS,
    force: bool = False,
    now: datetime | None = None,
    fetch: Fetch | None = None,
) -> dict[int, ClaudeUsage]:
    """Every account's reading, fetched only when the last one is older than ``max_age`` seconds.

    Serves each account's newest result younger than ``max_age`` for the token
    it holds now: a reading recorded in ``claude_usage`` with that token, or a
    fetch with it that failed (a rejected token, an unreachable endpoint), so a
    failed fetch is not retried before ``max_age`` either. Whichever of the two
    is newer wins, but a failure that says nothing of the token never hides a
    good reading (:func:`_served`). The rest are fetched in ONE concurrent,
    recording round (:func:`read_usage`), and so is an account that holds no
    token to send — signed out, or expired — which :func:`usage` answers
    without a request. A slot another call in this process is fetching is
    waited for, and served what that call read (:func:`_fetching`).
    ``force=True`` fetches every account regardless — the page's refresh key —
    and asks the Keychain again too (``core.forget_keychain_reads``), so a row
    that said to unlock it reads again once someone has.
    Each reading's ``fetched_at`` says how old it is ("refreshed 3m ago"); a
    reading that could not be taken carries the moment that was found out.
    """
    if not accounts:
        return {}
    if force:
        core.forget_keychain_reads()
    moment = now or _now()
    if not force:
        # What a session's own status line saw (#287, 2c), before anything is fetched.
        ingest_statusline(accounts, max_age=max_age, now=moment)
    since = moment - timedelta(seconds=max_age)
    tokens = {
        account.slot: token
        for account in accounts
        if (token := _token_fingerprint(account, moment)) is not None
    }
    seen = _fetch_rounds(accounts)
    readings = {} if force else _served(accounts, tokens, since=since, now=moment)
    stale = [account for account in accounts if account.slot not in readings]
    if not stale:
        return {account.slot: readings[account.slot] for account in accounts}
    with _fetching(stale, seen) as moved:
        if moved and not force:  # another call fetched meanwhile: what it read serves
            readings.update(_served(stale, tokens, since=since, now=moment))
            stale = [account for account in stale if account.slot not in readings]
        fetched = read_usage(stale, now=now, fetch=fetch)
        for account in stale:
            reading = fetched[account.slot]
            token = tokens.get(account.slot)
            if not reading.available:
                reading = reading.model_copy(update={"fetched_at": moment})
            if token is not None:
                with _UNANSWERED_LOCK:
                    key = (str(account.config_dir), token)
                    if reading.available:
                        _UNANSWERED.pop(key, None)
                    else:
                        _UNANSWERED[key] = reading
            readings[account.slot] = reading
        # A try that failed, forced or due, against a good reading still in the window:
        # the same rule as a served read, so the row keeps its figure (_preferred).
        failed = [
            account
            for account in stale
            if not readings[account.slot].available and account.slot in tokens
        ]
        if failed:
            recorded = _recorded_usage(failed, tokens, since=since, now=moment)
            for account in failed:
                answer = _preferred(recorded.get(account.slot), readings[account.slot])
                readings[account.slot] = answer or readings[account.slot]
    return {account.slot: readings[account.slot] for account in accounts}


def cached_usage_with_trends(
    accounts: Sequence[ClaudeAccount],
    *,
    max_age: float = USAGE_MAX_AGE_SECONDS,
    force: bool = False,
    now: datetime | None = None,
    fetch: Fetch | None = None,
) -> dict[int, tuple[ClaudeUsage, UsageTrend | None]]:
    """The Accounts page's read: :func:`cached_usage`, and each reading's trend.

    Fetched only when a reading is older than ``max_age`` (``force``: always —
    the page's *Refresh usage*); the trends are read from the recorded history,
    which every fetch here still records, in ONE store session after the
    readings. A store that cannot be opened costs the trends, never the
    readings.
    """
    readings = cached_usage(accounts, max_age=max_age, force=force, now=now, fetch=fetch)
    try:
        with store_session() as store:
            return {
                slot: (reading, usage_trend(slot, reading, now=now, store=store))
                for slot, reading in readings.items()
            }
    except sqlite3.Error:
        return {slot: (reading, None) for slot, reading in readings.items()}


def usage_is_readable(status: ClaudeAccountStatus, reading: ClaudeUsage | None) -> bool:
    """Whether a row's usage is this CLI's to read: signed in, with a token it can read that
    has neither expired nor been refused (a 401).

    The rows a "refreshed …" line ages from and says a try failed on (the Accounts page's
    rule, ``views.accounts`` ``_paint_usage_age``): a row that needs a login, or whose token
    is missing or unreadable (a locked Keychain), says so itself, and would otherwise put
    "last try failed" on the line every time.
    """
    return (
        status.signed_in
        and status.token_state == "ok"
        and not (reading is not None and reading.needs_login)
    )


def describe_age(fetched_at: datetime, *, now: datetime | None = None) -> str:
    """``just now`` / ``3m ago`` / ``2h ago``: how old a reading is, for a "refreshed …" line."""
    minutes = max(0, int(((now or _now()) - fetched_at).total_seconds() // 60))
    if minutes == 0:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"
    return f"{minutes // 60}h ago"


def headroom_percent(reading: ClaudeUsage, family: str | None = None) -> float | None:
    """How full an account reads for a pick: the FULLER of its two windows, or ``None``.

    Given the agent's model ``family`` (``core.harness.model_family``), that family's
    week counts too (#277): an Opus limit refuses an Opus agent exactly as a weekly one
    does, and refuses no other family's.

    A weekly limit refuses requests exactly as a five-hour one does, and an
    account that has spent its week builds no five-hour usage, so its
    five-hour window reads near 0 % once that window rolls over. Ranked on the
    five-hour window alone it was the account with the most room: the
    automatic hand-over moved an agent that had just hit its weekly limit back
    onto an account that had hit its own, the first request was refused, and
    the hand-over cooldown then kept the agent parked until a reset days away
    (final review of #203, accounts F1). ``None`` when the five-hour window was
    not read, which the pick skips as it always has; the seven-day window only
    ever makes an account read fuller.
    """
    if not reading.available or reading.session_percent is None:
        return None
    used = reading.session_percent
    if reading.week_percent is not None:
        used = max(used, reading.week_percent)
    own = reading.family_week_percent.get(family) if family is not None else None
    return used if own is None else max(used, own)


def describe_headroom(
    account: ClaudeAccount, reading: ClaudeUsage, family: str | None = None
) -> str:
    """``account 2 40% used (60% left)`` — ``account 2 5% used (week 100% used, 0% left)`` when
    the week is the fuller window.

    Every figure says USED, and what is left is spelled out beside it: an agent
    read a bare ``40%`` as the room remaining and rationed an account that had
    plenty. The five-hour figure leads, as it does on the page; the weekly one
    is named only when it is what ranks the account, so a note never shows a
    low number beside an account the pick passed over without saying why.
    """
    name = core.label(account)
    session = reading.session_percent
    if session is None:
        return name
    left = percent_left(reading, family)
    spare = "" if left is None else f"{left:.0f}% left"
    week = reading.week_percent
    own = reading.family_week_percent.get(family) if family is not None else None
    if own is not None and own > session and (week is None or own > week):
        detail = f"{family} week {own:.0f}% used" + (f", {spare}" if spare else "")
        return f"{name} {session:.0f}% used ({detail})"
    if week is not None and week > session:
        detail = f"week {week:.0f}% used" + (f", {spare}" if spare else "")
        return f"{name} {session:.0f}% used ({detail})"
    return f"{name} {session:.0f}% used" + (f" ({spare})" if spare else "")


HEADROOM_GOOD_ABOVE = 50.0
"""Over this share LEFT across the fleet, the level is ``good``."""
HEADROOM_FAIR_FROM = 25.0
"""From this share left up to :data:`HEADROOM_GOOD_ABOVE`, ``fair``."""
HEADROOM_LOW_FROM = 5.0
"""From this share left up to :data:`HEADROOM_FAIR_FROM`, ``low``; over 0 below it, ``critical``."""


def percent_left(reading: ClaudeUsage, family: str | None = None) -> float | None:
    """The share of an account LEFT for a pick: ``100 - max(session used, week used)``, 0-100
    (and the ``family`` week's, given one: :func:`headroom_percent`)."""
    used = headroom_percent(reading, family)
    return None if used is None else min(100.0, max(0.0, 100.0 - used))


def headroom_level(left: float | None) -> HeadroomLevel:
    """The word for a fleet-wide share left (:func:`fleet_headroom` states the bands)."""
    if left is None:
        return "unknown"
    if left > HEADROOM_GOOD_ABOVE:
        return "good"
    if left >= HEADROOM_FAIR_FROM:
        return "fair"
    if left >= HEADROOM_LOW_FROM:
        return "low"
    return "critical" if left > 0 else "empty"


def fleet_headroom(
    accounts: Sequence[ClaudeAccount], readings: Mapping[int, ClaudeUsage]
) -> HeadroomSummary:
    """How much room the fleet has LEFT, from readings already taken. Pure: no disk, no network.

    The formula, as the help screen quotes it:

    - an account is **usable** when it is enabled and has a readable reading
      (signed in, token accepted, the five-hour window read);
    - its share **left** is ``100 - max(session % used, week % used)`` — the
      fuller window decides, as it does for a pick (:func:`headroom_percent`);
    - the fleet's **percent left** is the mean of ``left`` over the usable
      accounts, and **usable** is how many there are;
    - **needs login** counts the accounts whose token has expired or was
      rejected (``ClaudeUsage.needs_login``);
    - **session by slot** is each usable account's five-hour window, percent
      USED, as read — what the agent rows show beside an agent's account;
    - the **level**: ``good`` over 50 % left, ``fair`` 25-50 %, ``low`` 5-25 %,
      ``critical`` under 5 % (but above 0), ``empty`` at 0 % or with no usable
      account, ``unknown`` while no enabled account has been read yet;
    - the **week** (#287), over the usable accounts whose weekly window was read:
      ``week left by slot`` is ``100 - week % used``; ``week percent left`` their mean;
      ``weeks left`` the sum of ``(100 - week % used) / 100``, account-weeks (two
      accounts at 90 % and 20 % used have 0.9 of 2 left); ``week resets soonest`` the
      account whose week resets first, and when.

    ``accounts`` are the accounts to summarise (disabled ones are skipped);
    ``readings`` is slot → reading, and an account without one has not been
    read yet.
    """
    enabled = [account for account in accounts if not account.disabled]
    read = [readings[account.slot] for account in enabled if account.slot in readings]
    needs_login = sum(1 for reading in read if reading.needs_login)
    if not enabled:
        return HeadroomSummary(level="empty")
    if not read:
        return HeadroomSummary(level="unknown")
    left_by_slot: dict[int, float] = {}
    session_by_slot: dict[int, float] = {}
    week_left: dict[int, float] = {}
    resets: list[tuple[datetime, int]] = []
    taken: list[datetime] = []
    for account in enabled:
        reading = readings.get(account.slot)
        left = percent_left(reading) if reading is not None else None
        if reading is None or left is None or reading.session_percent is None:
            continue
        left_by_slot[account.slot] = left
        session_by_slot[account.slot] = reading.session_percent
        if reading.week_percent is not None:
            week_left[account.slot] = max(0.0, 100.0 - reading.week_percent)
            if reading.week_resets_at is not None:
                resets.append((reading.week_resets_at, account.slot))
        if reading.fetched_at is not None:
            taken.append(reading.fetched_at)
    if not left_by_slot:
        return HeadroomSummary(level="empty", needs_login=needs_login)
    mean = sum(left_by_slot.values()) / len(left_by_slot)
    soonest = min(resets, default=None)
    return HeadroomSummary(
        level=headroom_level(mean),
        percent_left=mean,
        usable=len(left_by_slot),
        needs_login=needs_login,
        left_by_slot=left_by_slot,
        session_by_slot=session_by_slot,
        fetched_at=min(taken, default=None),
        week_left_by_slot=week_left,
        week_percent_left=sum(week_left.values()) / len(week_left) if week_left else None,
        weeks_left=sum(week_left.values()) / 100 if week_left else None,
        week_resets_soonest=(
            WeekReset(slot=soonest[1], resets_at=soonest[0]) if soonest is not None else None
        ),
    )


def summary_sentence(
    summary: HeadroomSummary, labels: Mapping[int, str], *, now: datetime | None = None
) -> str | None:
    """The fleet in one line (#287): ``Fleet: 58% left now · week: 1.8 of 3 accounts left ·
    next weekly reset in 2d 4h (Thu 14:00) (account 3)``; ``None`` with no reading.

    The Accounts page's header and ``accounts usage``'s last line. "Now" is the fuller
    window's share left (:attr:`HeadroomSummary.percent_left`), the week part is
    account-weeks over the accounts whose week was read, the reset is the soonest
    (:func:`core.format_reset`, the one reset formatter). ``labels`` is slot → alias;
    ``now`` is this module's clock unless given.
    """
    if summary.percent_left is None:
        return None
    now = now or _now()
    parts = [f"Fleet: {round(summary.percent_left)}% left now"]
    if summary.weeks_left is not None:
        parts.append(
            f"week: {summary.weeks_left:.1f} of {len(summary.week_left_by_slot)} accounts left"
        )
    if summary.week_resets_soonest is not None:
        soonest = summary.week_resets_soonest
        name = labels.get(soonest.slot) or core.default_label(soonest.slot)
        reset = core.format_reset(soonest.resets_at, now=now)
        parts.append(f"next weekly reset {reset} ({name})")
    return " · ".join(parts)


def read_fleet_headroom(
    accounts: Sequence[ClaudeAccount] | None = None,
    *,
    max_age: float = USAGE_MAX_AGE_SECONDS,
    force: bool = False,
    now: datetime | None = None,
    fetch: Fetch | None = None,
) -> HeadroomSummary:
    """:func:`fleet_headroom` over the enabled, signed-in accounts, read via :func:`cached_usage`.

    ``accounts`` defaults to the arranged list; a slot that is not signed in is
    never fetched, and with none signed in the level is ``empty``.
    """
    candidates = [
        account
        for account in (list_accounts() if accounts is None else accounts)
        if not account.disabled and core.signed_in(account)
    ]
    readings = cached_usage(candidates, max_age=max_age, force=force, now=now, fetch=fetch)
    summary = fleet_headroom(candidates, readings)
    write_statusline_fleet(summary, candidates, now=now)
    return summary


# --- the Claude Code status line and the sessions it reports (#287, 2c) -----------------------
#
# `asq-statusline` (aisquare.statusline, stdlib only) keeps one record per session under
# cache/statusline/. Read here they are three things: the sessions on this machine
# (`statusline_sessions`, `accounts sessions`), readings the usage endpoint need not be
# asked for (`ingest_statusline`, which `cached_usage` runs first), and, the other way,
# `fleet.json`, the fleet's figure the status line prints (`write_statusline_fleet`).

STATUSLINE_KEEP = timedelta(hours=24)
"""A record older than this is deleted when the records are read."""


def statusline_dir() -> Path:
    """Where ``asq-statusline`` keeps its records (``aisquare.statusline.record_dir``)."""
    return paths.cache_dir() / "statusline"


def _statusline_slot(config_dir: str | None) -> int | None:
    """The account a record's ``CLAUDE_CONFIG_DIR`` names: slot 1 for none or the plain
    claude's own directory, a managed slot's number, else ``None`` (not ours)."""
    if not config_dir:
        return core.DEFAULT_SLOT
    if (slot := core.managed_slot(config_dir)) is not None:
        return slot
    with contextlib.suppress(OSError, RuntimeError):
        if Path(config_dir).resolve() == core.default_config_dir().resolve():
            return core.DEFAULT_SLOT
    return None


def _at(epoch: Any) -> datetime | None:
    if isinstance(epoch, bool) or not isinstance(epoch, int | float):
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _statusline_session(data: Any) -> StatuslineSession | None:
    """One record as a :class:`StatuslineSession`; ``None`` for one that is not a record."""
    if not isinstance(data, dict) or not isinstance(data.get("session_id"), str):
        return None
    updated = _at(data.get("updated_at"))
    if updated is None:
        return None

    def window(name: str) -> tuple[float | None, datetime | None]:
        value = data.get(name)
        if not isinstance(value, dict):
            return None, None
        used = value.get("used_percentage")
        percent = float(used) if isinstance(used, int | float) else None
        return percent, _at(value.get("resets_at"))

    def text(key: str) -> str | None:
        value = data.get(key)
        return value if isinstance(value, str) and value else None

    def number(key: str) -> float | None:
        value = data.get(key)
        return float(value) if isinstance(value, int | float) else None

    session_percent, session_resets = window("five_hour")
    week_percent, week_resets = window("seven_day")
    return StatuslineSession(
        session_id=data["session_id"],
        updated_at=updated,
        cwd=text("cwd"),
        model=text("model"),
        config_dir=text("config_dir"),
        slot=_statusline_slot(text("config_dir")),
        fleet_agent=text("fleet_agent"),
        session_percent=session_percent,
        session_resets_at=session_resets,
        week_percent=week_percent,
        week_resets_at=week_resets,
        context_percent=number("context_percent"),
        cost_usd=number("cost_usd"),
    )


def _read_statusline_records(now: datetime) -> list[StatuslineSession]:
    """Every record in :func:`statusline_dir`, newest first; one older than
    :data:`STATUSLINE_KEEP` is deleted, and a file that is not a record skipped."""
    found: list[StatuslineSession] = []
    try:
        files = list(statusline_dir().glob("*.json"))
    except OSError:
        return []
    for file in files:
        if file.name == STATUSLINE_FLEET_FILE:
            continue
        try:
            session = _statusline_session(json.loads(file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        if session is None:
            continue
        if now - session.updated_at > STATUSLINE_KEEP:
            with contextlib.suppress(OSError):
                file.unlink()
            continue
        found.append(session)
    return sorted(found, key=lambda session: session.updated_at, reverse=True)


def statusline_sessions(
    *, since: timedelta | None = None, now: datetime | None = None
) -> list[StatuslineSession]:
    """The Claude Code sessions that reported in the last ``since`` (all kept ones with
    ``None``), newest first, each named with its account and, for a fleet agent, its label.

    Every session carrying ``asq-statusline`` reports: hand-launched ones, and ones in a
    directory no project holds, under any config directory. Read-only but for deleting
    records older than a day.
    """
    moment = now or _now()
    sessions = [
        session
        for session in _read_statusline_records(moment)
        if since is None or moment - session.updated_at <= since
    ]
    names = {account.slot: core.label(account) for account in list_accounts()}
    agents = {session.fleet_agent for session in sessions if session.fleet_agent}
    labels: dict[str, str] = {}
    if agents:
        with contextlib.suppress(sqlite3.Error), store_session() as store:
            for agent_id in agents:
                if (row := store.get_fleet_agent(agent_id)) is not None:
                    labels[agent_id] = row.label
    return [
        session.model_copy(
            update={
                "account": names.get(session.slot) if session.slot is not None else None,
                "fleet_label": labels.get(session.fleet_agent or ""),
            }
        )
        for session in sessions
    ]


def ingest_statusline(
    accounts: Sequence[ClaudeAccount],
    *,
    max_age: float = USAGE_MAX_AGE_SECONDS,
    now: datetime | None = None,
) -> int:
    """Record, as a ``claude_usage`` sample, each account's newest status-line reading that
    is younger than ``max_age`` and newer than every sample the store holds for it; returns
    how many it recorded.

    A reading Claude Code took itself, on that account's own token, so
    :func:`cached_usage` serves it like one of its own fetches and the usage endpoint is
    asked less often. Sampled under the token the account holds NOW (``_token_fingerprint``),
    as a fetch would be: an account signed out has no sample to add. Best effort: an
    unreadable record or store records nothing.
    """
    moment = now or _now()
    newest: dict[int, StatuslineSession] = {}
    for session in _read_statusline_records(moment):
        # Both windows, or none: a sample stands for a whole reading, and one without its
        # week (a payload that left `seven_day` out) would be served as an account with
        # its week to spare, over the endpoint's reading of it spent (review of #362).
        if session.slot is None or session.session_percent is None or session.week_percent is None:
            continue
        if (moment - session.updated_at).total_seconds() > max_age:
            continue
        if session.slot not in newest:  # newest first
            newest[session.slot] = session
    recorded = 0
    if not newest:
        return recorded
    with contextlib.suppress(sqlite3.Error), store_session() as store:
        for account in accounts:
            reading = newest.get(account.slot)
            if reading is None:
                continue
            token = _token_fingerprint(account, moment)
            if token is None or store.usage_samples(account.slot, since=reading.updated_at):
                continue
            store.add_usage_sample(
                UsageSample(
                    slot=account.slot,
                    fetched_at=reading.updated_at,
                    session_percent=reading.session_percent,
                    session_resets_at=reading.session_resets_at,
                    week_percent=reading.week_percent,
                    week_resets_at=reading.week_resets_at,
                    token_fingerprint=token,
                )
            )
            recorded += 1
    return recorded


def statusline_sites(
    slots: Sequence[int] | None = None,
    *,
    action: Literal["status", "install", "uninstall"] = "status",
    wrap: bool = False,
) -> list[StatuslineSite]:
    """Each account's ``statusLine`` (every account, or those in ``slots``), after ``action``.

    ``install`` sets ``asq-statusline`` where none is set, and with ``wrap`` around a
    command the user set; a status line it did not write is otherwise left alone and
    reported ``foreign``. ``uninstall`` takes out only what it wrote, putting a wrapped
    command back as it was. A settings file two accounts share (``[accounts] share``
    links it) is acted on once. One account's failure is its row's ``error``; the rest go on.
    """
    chosen = [account for account in list_accounts() if slots is None or account.slot in slots]
    sites: list[StatuslineSite] = []
    seen: dict[str, int] = {}
    for account in chosen:
        path = account.config_dir / "settings.json"
        key = os.path.realpath(path)
        base = {"slot": account.slot, "account": core.label(account), "settings_path": str(path)}
        if key in seen:
            sites.append(StatuslineSite(**base, state="shared", theirs=f"slot {seen[key]}"))
            continue
        seen[key] = account.slot
        before = agent_core.statusline_entry(account.config_dir)
        try:
            if action == "install":
                after = agent_core.install_statusline(account.config_dir, wrap=wrap)
                statusline_dir().mkdir(parents=True, exist_ok=True)
            elif action == "uninstall":
                agent_core.remove_statusline(account.config_dir)
                after = agent_core.statusline_entry(account.config_dir)
            else:
                after = before
        except Exception as exc:  # one account's file; the rest still go
            sites.append(
                StatuslineSite(**base, state=before.kind, theirs=before.theirs, error=str(exc))
            )
            continue
        sites.append(
            StatuslineSite(
                **base,
                state=after.kind,
                theirs=after.theirs,
                changed=(after.kind, after.theirs) != (before.kind, before.theirs),
            )
        )
    return sites


def write_statusline_fleet(
    summary: HeadroomSummary, accounts: Sequence[ClaudeAccount], *, now: datetime | None = None
) -> None:
    """``fleet.json`` for ``asq-statusline``: the fleet's share left, how many accounts, and
    each account's name by its directory. Only where the status line has run or been
    installed (its directory exists), and best effort: the line goes without it."""
    directory = statusline_dir()
    if not directory.is_dir():
        return
    labels = {os.path.realpath(account.config_dir): core.label(account) for account in accounts}
    plain = next((a for a in accounts if a.slot == core.DEFAULT_SLOT), None)
    if plain is not None:
        labels[""] = core.label(plain)
    data = {
        "percent_left": summary.percent_left,
        "week_percent_left": summary.week_percent_left,
        "accounts": summary.usable,
        "labels": labels,
        "updated_at": (now or _now()).timestamp(),
    }
    with contextlib.suppress(OSError):
        temporary = directory / f".{STATUSLINE_FLEET_FILE}.{os.getpid()}.tmp"
        temporary.write_text(json.dumps(data), encoding="utf-8")
        os.replace(temporary, directory / STATUSLINE_FLEET_FILE)


# --- the refusal memory (#277) -------------------------------------------------------------------
#
# A 429 is an answer the usage endpoint may not give: a family week it does not send, a
# window it reads under the line while the account refuses. So every ``rate_limit``
# StopFailure leaves a mark in ``team_meta`` — ``account_limit:<slot>:<window>`` = the reset
# the message named — and :func:`headroom_choice` skips an account whose mark is still in
# the future: always for the windows every model shares (``session``, ``weekly``), and for a
# family's (``opus``, ``sonnet``) when the agent is of that family. No migration: a mark is
# a ``team_meta`` row, and one whose reset has passed is simply not read.

LIMIT_MARK_PREFIX = "account_limit:"
"""``team_meta`` key prefix of a limit mark: ``account_limit:<slot>:<window>``."""


@dataclass(frozen=True)
class LimitMark:
    """One remembered limit: the account refused (``how = "refused"``) or a live reading saw
    a family week spent (``"read"``) at ``at``, until ``until``."""

    slot: int
    window: str
    until: datetime
    at: datetime
    how: str = "refused"

    def blocks(self, family: str | None) -> bool:
        """Whether it shuts the account to an agent of ``family``: a window every model
        shares blocks everyone; a family's only that family (an unknown word is shared)."""
        return self.window not in _FAMILIES or self.window == family

    def describe(self, *, now: datetime) -> str:
        """``weekly limit until Mon 00:00 (in 2d 4h), refused 10:42``."""
        until = self.until.astimezone()
        at = self.at.astimezone().strftime("%H:%M")
        distance = core.format_reset(self.until, now=now, clock=False)
        return (
            f"{self.window} limit until {until.strftime('%a %H:%M')} ({distance}), {self.how} {at}"
        )


def _write_limit(
    store: ContextStore,
    slot: int,
    window: str,
    until: datetime,
    *,
    how: str,
    now: datetime | None = None,
) -> None:
    value = {"until": until.isoformat(), "at": (now or _now()).isoformat(), "how": how}
    store.set_meta(f"{LIMIT_MARK_PREFIX}{slot}:{window.lower()}", json.dumps(value))


def remember_limit(
    slot: int | None,
    window: str | None,
    until: datetime | None,
    *,
    now: datetime | None = None,
) -> None:
    """Remember that account ``slot`` refused with a ``window`` limit lifting at ``until``.

    Called on every ``rate_limit`` StopFailure (``services.hooks.turn_failed``). Nothing
    to remember without all three: a 429 that names no reset (an API key's throttle)
    leaves no mark, as it leaves no reset on the row. Best effort: a store that cannot be
    written costs the memory, never the hook.
    """
    if slot is None or not window or until is None:
        return
    with contextlib.suppress(sqlite3.Error), store_session() as store:
        _write_limit(store, slot, window, until, how="refused", now=now)


def limit_marks(*, now: datetime | None = None) -> dict[int, list[LimitMark]]:
    """Every limit mark still in force at ``now``, by slot; ``{}`` when the store cannot be
    read (a pick then reads usage as it did before #277)."""
    moment = now or _now()
    try:
        with store_session() as store:
            rows = store.list_meta(LIMIT_MARK_PREFIX)
    except sqlite3.Error:
        return {}
    marks: dict[int, list[LimitMark]] = {}
    for key, raw in rows.items():
        slot_text, _, window = key.removeprefix(LIMIT_MARK_PREFIX).partition(":")
        try:
            data = json.loads(raw)
            mark = LimitMark(
                slot=int(slot_text),
                window=window,
                until=datetime.fromisoformat(data["until"]),
                at=datetime.fromisoformat(data["at"]),
                how=str(data.get("how", "refused")),
            )
        except (ValueError, KeyError, TypeError):
            continue
        if mark.until > moment:
            marks.setdefault(mark.slot, []).append(mark)
    return marks


_NOTHING_TO_PICK = "headroom: no enabled, signed-in account to pick from"

PICK_MAX_AGE_SECONDS = 60.0
"""How old a reading a LAUNCH's headroom pick may use (:func:`choose`): one minute.

``pick = "headroom"`` is the default since 0.8.1, so every ``fleet spawn`` and
``aisquare claude`` walks that rung, and read live it sent one request per account each
time, a second after the sidebar had read them too (review of #261, m1). A minute is the
sidebar's own tick, so a burst of spawns, or a spawn beside the sidebar's or the page's
read, is served by one reading. And a five-hour window moves a point or two a minute
under the heaviest use, far less than the room under ``switch_at`` a pick looks for;
the cache's five minutes could start an agent on an account that crossed the line since.
A hand-over still reads live (:func:`choose_for_handover`)."""


def headroom_choice(
    accounts: Sequence[ClaudeAccount],
    *,
    switch_at: int,
    exclude: Iterable[int] = (),
    fetch: Fetch | None = None,
    now: datetime | None = None,
    least_bad: bool = True,
    max_age: float | None = None,
    family: str | None = None,
    prefer: str = "priority",
) -> tuple[ClaudeAccount | None, list[str]]:
    """The account with headroom, and the notes that say how it was picked.

    ``family`` is the agent's model family, when known: that family's week counts
    (:func:`headroom_percent`), and so does its limit mark. An account with a limit
    mark still in force (:func:`limit_marks`) is skipped with a note and never read.
    ``prefer = "reset-soonest"`` takes, among the accounts under ``switch_at``, the one
    whose week resets first, priority breaking ties (#277).

    ``least_bad=False`` withholds the "every account is over the line, take
    the one with the most room" answer: right for a spawn, it is what made two
    exhausted accounts hand an agent back and forth for a whole window when
    applied by the automatic hand-over (review of #205, second round).

    Candidates are the enabled, signed-in accounts in priority order, minus
    ``exclude`` (the account a hand-over is leaving). Their usage is read
    concurrently, so a pick costs one round trip, not one per account: every
    candidate now (:func:`read_usage`) by default — a hand-over is moving an
    agent off a full account — or, given ``max_age`` (a launch:
    :data:`PICK_MAX_AGE_SECONDS`), through :func:`cached_usage`, and then not at
    all for a single candidate, which is the answer whatever it reads (with
    ``least_bad``). The rule: the FIRST candidate under ``switch_at``
    percent of the fuller of its two windows (:func:`headroom_percent`: a
    spent week is no room, however empty the five hours read) — priority order
    is the operator's preference, and an account with room keeps it — else the
    candidate with the lowest usage, because "every account is nearly out"
    still has a least bad answer. An account whose usage cannot be read is
    skipped and named; ``None`` when nothing could be measured, so the
    caller's next rung decides.
    """
    skip = set(exclude)
    candidates = [
        account
        for account in accounts
        if not account.disabled and account.slot not in skip and core.signed_in(account)
    ]
    if not candidates:
        return None, [_NOTHING_TO_PICK]
    moment = now or _now()
    marks = limit_marks(now=moment)
    notes: list[str] = []
    open_ones: list[ClaudeAccount] = []
    for account in candidates:
        mark = next((m for m in marks.get(account.slot, []) if m.blocks(family)), None)
        if mark is None:
            open_ones.append(account)
        else:
            notes.append(f"headroom: {core.label(account)} skipped — {mark.describe(now=moment)}")
    if not open_ones:
        notes.append("headroom: every account is at a limit it named — the default decides")
        return None, notes
    candidates = open_ones
    if max_age is None:
        readings = read_usage(candidates, now=now, fetch=fetch)
    elif len(candidates) == 1 and least_bad:
        return candidates[0], [*notes, *_over_the_line(candidates[0], switch_at, now=now)]
    else:
        readings = cached_usage(candidates, max_age=max_age, now=now, fetch=fetch)
    measured: list[tuple[ClaudeAccount, float]] = []
    for account in candidates:
        reading = readings[account.slot]
        percent = headroom_percent(reading, family)
        if percent is not None:
            measured.append((account, percent))
        else:
            notes.append(
                f"headroom: {core.label(account)} skipped — {reading.reason or 'no reading'}"
            )
    if not measured:
        notes.append("headroom: no account's usage could be read — the default decides")
        return None, notes
    summary = " · ".join(describe_headroom(a, readings[a.slot], family) for a, _pct in measured)
    under_line = [a for a, pct in measured if pct < switch_at]
    if prefer == "reset-soonest" and len(under_line) > 1:
        never = datetime.max.replace(tzinfo=UTC)
        soonest = min(under_line, key=lambda a: readings[a.slot].week_resets_at or never)
        if soonest is not under_line[0]:
            notes.append(
                f"headroom: {summary} — {core.label(soonest)}'s week resets first of those "
                f"under the {switch_at}%-used line (prefer = reset-soonest)"
            )
            return soonest, notes
    under = under_line[0] if under_line else None
    if under is not None:
        notes.append(
            f"headroom: {summary} — {core.label(under)} is first under the {switch_at}%-used line"
        )
        return under, notes
    if not least_bad:
        notes.append(
            f"headroom: {summary} — every account is over the {switch_at}%-used line; "
            "nothing to switch to"
        )
        return None, notes
    least, pct = min(measured, key=lambda pair: pair[1])
    notes.append(
        f"headroom: {summary} — every account is over the {switch_at}%-used line; "
        f"{core.label(least)} has the most room ({max(0, 100 - pct):.0f}% left)"
    )
    return least, notes


def _over_the_line(account: ClaudeAccount, switch_at: int, *, now: datetime | None) -> list[str]:
    """For the one account a launch takes without asking: the note the pick of several gives
    when every account is over the line, when the cache already says so of this one — its
    newest reading recorded in the last :data:`USAGE_MAX_AGE_SECONDS` for the token it holds
    now. Nothing is fetched; no reading, no note (review of #261, N4)."""
    moment = now or _now()
    token = _token_fingerprint(account, moment)
    if token is None:
        return []
    since = moment - timedelta(seconds=USAGE_MAX_AGE_SECONDS)
    reading = _recorded_usage([account], {account.slot: token}, since=since, now=moment).get(
        account.slot
    )
    percent = headroom_percent(reading) if reading is not None else None
    if reading is None or percent is None or percent < switch_at:
        return []
    return [
        f"headroom: {describe_headroom(account, reading)} — every account is over the "
        f"{switch_at}%-used line; {core.label(account)} has the most room "
        f"({max(0, 100 - percent):.0f}% left)"
    ]


def choose_for_handover(
    explicit: str | None = None,
    *,
    role: str | None,
    project: ProjectInfo | None,
    exclude: Iterable[int],
    automatic: bool = False,
    fetch: Fetch | None = None,
    family: str | None = None,
) -> AccountChoice:
    """The target of a hand-over: HEADROOM FIRST; the ladder only when nothing can be measured.

    ``family`` is the agent's model family (its session's model), so an account whose
    week for that family is spent, or that refused it with that family's limit, is no
    target (#277).

    A switch exists to move an agent to the account with room, so the binding
    and project-default rungs — explicit choices for a *launch* — do not get to
    send it somewhere just as full; ``choose(spread=True)`` let them, because
    it consults them above the headroom rung (review of #205, second round).
    ``explicit`` (``--to``) is still the flag rung. ``automatic`` is the
    hand-over the hook starts: it takes only an account UNDER ``switch_at``
    (no least-bad answer, see :func:`headroom_choice`) and otherwise refuses,
    which leaves the agent parked with Claude Code's own wait intact; a manual
    switch keeps the least-bad answer and, when no usage can be read at all,
    falls back to the ladder minus the account being left — and minus an account
    with a limit mark still in force for the agent (#277): only ``--to`` names one.
    Either reads every candidate's usage live, never from the cache a launch uses: it is moving an
    agent off an account that just filled, and a figure even a minute old could
    send it to another that has filled since.

    A registry that cannot be read picks nothing, manual or automatic, and says
    why. Its fallback is the bare directories, where every slot reads as
    enabled, so headroom moved an agent onto a slot the operator had disabled.
    :func:`choose` does not pick from it by headroom or by a default either
    (review of the #205 fold, round 1). ``--to`` still names an account.
    """
    skip = frozenset(exclude)  # read three times below: never an iterator
    if explicit is not None:
        return choose(explicit, role=role, project=project, exclude=skip, fetch=fetch)
    accounts, registry_note = _read_for_handover()
    if registry_note is not None:
        return AccountChoice(None, None, [registry_note])
    assert accounts is not None  # listed, or there is a note
    notes: list[str] = []
    settings = accounts_settings()
    picked, more = headroom_choice(
        accounts,
        switch_at=settings.switch_at,
        exclude=skip,
        fetch=fetch,
        least_bad=not automatic,
        family=family,
        prefer=settings.prefer,
    )
    notes.extend(more)
    if picked is not None:
        return AccountChoice(picked, "headroom", notes)
    if automatic:
        return AccountChoice(None, None, notes)
    fallback = choose(
        None, role=role, project=project, exclude=skip, spread=False, fetch=fetch, family=family
    )
    # The ladder's rungs read no marks: on a hand-over they are not a choice someone made for
    # THIS move, and with every other account marked the machine default moved the agent
    # back onto the account that had just refused it (review of #304, reviewer-a, major 2).
    # Only `--to` (``explicit``, above) may name a marked account.
    if fallback.account is not None:
        moment = _now()
        mark = next(
            (m for m in limit_marks(now=moment).get(fallback.account.slot, []) if m.blocks(family)),
            None,
        )
        if mark is not None:
            said = f"{core.label(fallback.account)} ({fallback.source}) skipped — "
            return AccountChoice(
                None, None, [*notes, *fallback.notes, said + mark.describe(now=moment)]
            )
    return AccountChoice(fallback.account, fallback.source, [*notes, *fallback.notes])


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
      ``core.claude_accounts.default_config_dir``;
    - the two account variables are the plain claude's as this process knows
      them (``core.plain_environment``). A process under one of our slots (a
      hand-over worker, a manager on slot 2 spawning) has them naming that
      slot, and carried as they stood, ``launch --account 1`` in the window
      kept them and ran on the slot (review of #205, sixth round).
    """
    source = os.environ if environ is None else environ
    view = {
        paths.HOME_ENV_VAR: source.get(paths.HOME_ENV_VAR, ""),
        **core.plain_environment(source),
    }
    base = Path.cwd() if cwd is None else cwd
    to_set: dict[str, str] = {}
    for var in CARRIED_VARS:
        value = view.get(var, "").strip()
        if value:
            path = paths.expand_user(Path(value))
            # A `~olduser/…` naming a home this machine does not have travels as written,
            # as this shell holds it: expanding it raised, and joined to the cwd it named a
            # directory there for the window's claude to make (review of #257).
            to_set[var] = value if paths.names_no_home(path) else str((base / path).absolute())
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
    config: FleetSettings | None = None,
) -> WindowInfo:
    """Start a Claude Code session for ``account`` in the fleet's tmux server, detached.

    The UI renders the window in a pane and polls :func:`sign_in_landed`; the
    working directory is the home directory, exactly what a fresh terminal
    would give ``claude``; the environment is this process's view of what the
    slot means (:func:`carry_environment`), resolved against THIS process's
    working directory, not the window's.

    Opened as every window at the fleet's socket is (``fleet.open_window``): it starts the
    fleet's server when none runs, so the rows of a server that exited there forget its
    start first, and a socket path a running server lost refuses it with
    ``fleet.FleetError``. Started past both, after a ``kill-server`` every live row was held
    for good, and over a freed path the agents of the server that lost it were stranded
    (review of #259, round 3). Imported here, as ``fleet`` imports this module. ``config`` is
    the ``[fleet]`` section ``server`` was made for: that check asks the rows of its socket,
    and without it reads ``config.toml`` again (review of #259, round 5).
    """
    from aisquare.services import fleet as fleet_service

    command, env = sign_in_window_command(account, environ)
    return fleet_service.open_window(
        server,
        SIGN_IN_SESSION,
        name=f"account-{account.slot}",
        cwd=cwd if cwd is not None else Path.home(),
        command=command,
        env=env,
        config=config,
    )


class LoginUnreadable(AccountsError):
    """The stored login could not be read (a locked or refusing Keychain): a sign-in window
    opened over it could not tell the login it was opened over from a new one."""


def login_fingerprint(account: ClaudeAccount) -> str | None:
    """A digest of the stored login (its token and expiry), or ``None`` when none is stored.

    So a sign-in window opened to RE-authenticate an account can tell the login
    it was opened over from a fresh one: an expired or rejected token is still
    a signed-in directory, and :func:`sign_in_landed` alone would answer at the
    first poll, before the user had done anything. A digest, never the token. Read
    afresh (a login is what changes here), so never on the UI thread. A store that
    would not say raises :class:`LoginUnreadable` with its reason: ``None`` there
    would let the login already stored count as the new one at the first poll.
    """
    login = core.stored_login(account, fresh=True)
    if login.unreadable is not None:
        raise LoginUnreadable(login.unreadable)
    return _login_digest(login.credentials) if login.credentials is not None else None


def _login_digest(creds: core.ClaudeCredentials) -> str:
    """:func:`login_fingerprint` of credentials already read (the usage cache's key too)."""
    stamp = creds.expires_at.isoformat() if creds.expires_at is not None else ""
    return hashlib.sha256(f"{creds.access_token}\0{stamp}".encode()).hexdigest()


def sign_in_landed(account: ClaudeAccount, *, since: str | None = None) -> ClaudeIdentity | None:
    """Who the directory is now signed in as, once Claude Code has stored its login.

    ``since`` is :func:`login_fingerprint` as the window opened, for a re-auth:
    the login lands only once the stored one is a different login (a refreshed
    or new token), not the expired or rejected one the window was opened over.
    Read afresh, as :func:`login_fingerprint` is, and once. A store that would not
    say (a Keychain locked mid-window) is no landing: the window keeps waiting.
    """
    identity = core.identity(account)
    if identity is None:
        return None
    creds = core.stored_login(account, fresh=True).credentials
    if creds is None:  # none stored, or the Keychain would not say
        return None
    if since is not None and _login_digest(creds) == since:
        return None
    return identity


def complete_sign_in(account: ClaudeAccount) -> ClaudeAccountStatus:
    """Wire the newly signed-in directory like any other: aisquare's hooks into its settings."""
    agents_service.connect(AGENT, account.config_dir)
    return describe(account)


def abandon_sign_in(account: ClaudeAccount) -> bool:
    """A sign-in that never landed leaves no slot behind. Returns whether one was discarded.

    Asked afresh: a login Claude Code stored after the last look (the window's poll a
    second ago) is the slot's, and a slot deleted from under it leaves its Keychain item
    behind for whichever slot takes the number next.
    """
    if not account.managed or core.signed_in(account, fresh=True):
        return False
    core.discard_account(account)
    return True


class LandedNotWired(AccountsError):
    """A cancelled sign-in's login had landed after all, and aisquare's hooks did not install
    there: the slot is kept, signed in (:func:`settle_sign_in`; the cause is chained)."""


def settle_sign_in(account: ClaudeAccount) -> ClaudeAccountStatus | bool:
    """A fresh slot's sign-in, cancelled: kept and wired when its login landed after all
    (:func:`complete_sign_in`'s status), else discarded (:func:`abandon_sign_in`'s answer).

    Kept and not wired raises :class:`LandedNotWired`, so a caller does not read the
    failure as a sign-in that changed nothing: the page said "nothing changed" of a slot it
    had kept (review of #259, round 16)."""
    if sign_in_landed(account) is not None:
        try:
            return complete_sign_in(account)
        except Exception as exc:  # whatever wiring the hooks raised: the slot is kept anyway
            raise LandedNotWired(str(exc)) from exc
    return abandon_sign_in(account)


# --- removing -------------------------------------------------------------------------------


def remove(account: ClaudeAccount, *, notes: list[str] | None = None) -> Path:
    """Forget a managed slot: hooks out of its settings, the directory renamed beside itself.

    ``notes`` collects what happened to role bindings that named the slot by
    number (see :func:`_retarget_bindings`), for the caller to show.
    """
    if not account.managed:
        raise AccountsError(
            "slot 1 is the plain claude of this machine, not an account the CLI added — "
            "sign out of it inside Claude Code (/logout) instead"
        )
    identity = core.identity(account)  # read before the rename moves the directory
    # The alias too, from the registry itself: a caller holding a plain
    # directory record (``core.create_account`` hands one out) knows none.
    alias = account.alias or next(
        (a.alias for a in _read_arranged()[0] if a.slot == account.slot), None
    )
    # The directory is leaving either way; a settings.json we cannot parse is not a stop.
    with contextlib.suppress(Exception):
        agents_service.disconnect(AGENT, account.config_dir)
    moved = core.remove_account(account)
    # After the rename, never before: the number is free again the moment the
    # directory moves, and a default or alias left behind would be inherited by
    # whatever `add` puts in that slot next.
    forget_arrangement(account.slot)
    _retarget_bindings(account.slot, alias, identity.email if identity is not None else None, notes)
    return moved


def _retarget_bindings(
    slot: int, alias: str | None, email: str | None, notes: list[str] | None
) -> None:
    """Role bindings that named the removed slot by NUMBER or ALIAS now name its email, or nothing.

    A project default is a row of ours and ``forget_arrangement`` drops it; a
    role binding is a line in ``config.toml`` that stored the reference as
    typed, and a ``2`` left behind would be inherited by whoever the next
    ``add`` signs into slot 2 — the very hazard the registry guards the default
    against (review of #205, finding 8). The alias has the same hazard one
    step later: ``forget_arrangement`` frees it, and naming the next account
    ``work`` again is the ordinary thing to do (second round). The email keeps
    the operator's intent and dangles honestly: ``launch`` refuses with the
    rung named, ``doctor`` flags it, and it resolves again the day that person
    signs back in. With no email to name (a slot that never signed in), the
    binding's account is cleared instead. Both go through the one config
    writer, ``bind_role``.
    """
    try:
        bindings = settings_service.role_account_bindings()
    except Exception as exc:  # an unreadable config.toml is doctor's to report
        if notes is not None:
            notes.append(f"role bindings not checked ({type(exc).__name__}: {exc})")
        return
    for role, ref in sorted(bindings.items()):
        typed = ref.strip()
        if typed != str(slot) and not _same_alias(alias, typed):
            continue
        named = f"slot {slot}" if typed == str(slot) else f"alias {typed!r} (slot {slot})"
        try:
            if email:
                settings_service.bind_role(role, account=email)
                note = (
                    f"role {role} was bound to {named}; it now names {email} — refused at "
                    "launch until that account is signed in again, or re-bound"
                )
            else:
                settings_service.bind_role(role, clear_account=True)
                note = (
                    f"role {role} was bound to {named}, which had no login — the account "
                    "binding is cleared"
                )
        except Exception as exc:
            note = f"role {role} still names {named} — config.toml could not be written ({exc})"
        if notes is not None:
            notes.append(note)


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


# --- what the accounts share: folder trust and settings.json (core.claude_sharing) --------


@dataclass(frozen=True)
class TrustChange:
    """What sharing folder trust did to one account's ``.claude.json``."""

    account: ClaudeAccount
    outcome: Literal["trusted", "already", "skipped", "failed"]
    """``trusted``: entries were written (:attr:`folders`); ``already``: nothing was
    missing; ``skipped``: Claude Code never started there; ``failed``: :attr:`reason`."""
    folders: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class SettingsChange:
    """What sharing ``settings.json`` did to one managed account, or what it holds."""

    account: ClaudeAccount
    outcome: Literal["shared", "already", "kept", "unshared", "own", "none", "failed"]
    """``shared``: linked now; ``already``: was linked; ``kept``: left as its own, for
    :attr:`reason`; ``unshared``: its link became its own copy; ``own`` / ``none``: what
    it holds, untouched; ``failed``: :attr:`reason`."""
    backup: Path | None = None
    """The copy of its own settings.json a link replaced, beside it."""
    reason: str | None = None


SHARED_ENV = "AISQUARE_ACCOUNTS_SHARED"
"""The slot ``fleet spawn`` shared into (:func:`share_for_launch`), set on its window, so the
window's ``aisquare launch`` does not read the accounts' ``.claude.json`` files again for the
share just made: files of tens of MB on a long-used account (review of #259, round 21). The
launch takes it out of the agent's environment."""


class SharedNote(str):
    """A :func:`share_for_launch` note that says a share was made (a folder's trust
    copied, settings.json linked), not one that could not be: a receipt shows it as done,
    not as a warning. Still a ``str``, so a caller that prints the notes needs nothing."""

    __slots__ = ()


def share_for_launch(
    folder: Path | sharing.TrustScope | None = None,
    *,
    account: ClaudeAccount | None = None,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Share into the account a launch in ``folder`` runs on what ``[accounts]`` says to.

    Right before ``aisquare launch`` replaces itself with the agent, and before ``fleet
    spawn`` opens its window (so the receipt carries the note). The account is ``account``
    when the launch chose one, else the one whose ``.claude.json`` the launch's ``env``
    leads Claude Code to (default: this process's), and nothing is done for a directory
    that is no account of ours. ``folder`` defaults to the working directory; a spawn
    whose worktree is not made yet passes the scope it will have
    (:func:`core.claude_sharing.worktree_scope`).

    - ``share_trust``: when no entry trusts the folder there and another account's does,
      that entry is copied (:func:`core.claude_sharing.grant_trust`). Trust nobody gave is
      never written: Claude Code asks, and ``fleet ls`` says the agent waits for it.
    - ``share_settings``: a managed account's ``settings.json`` becomes a link to the plain
      claude's, its own kept beside it (:func:`share_settings`).

    The notes say what changed, or why it could not; a launch is never refused for it.
    """
    try:
        knobs = accounts_settings()
        if not (knobs.share_trust or knobs.share_settings):
            return []
        accounts = list_accounts()
        if len(accounts) < 2:
            return []  # one account: no trust to copy from, no plain claude's settings to link
        target = (
            next((one for one in accounts if one.slot == account.slot), account)
            if account is not None
            else _account_of_env(os.environ if env is None else env, accounts)
        )
        if target is None:
            return []
        notes: list[str] = []
        if knobs.share_trust:
            scope = (
                folder
                if isinstance(folder, sharing.TrustScope)
                else sharing.trust_scope(Path.cwd() if folder is None else folder)
            )
            notes += _share_trust_into(target, scope, accounts)
        if knobs.share_settings and target.managed:
            notes += _share_settings_into(target)
        return notes
    except Exception as exc:  # an observer of the launch: it costs the sharing, never the launch
        return [f"nothing was shared into this launch's account ({exc})"]


def _account_of_env(
    env: Mapping[str, str], accounts: Sequence[ClaudeAccount]
) -> ClaudeAccount | None:
    """The account whose ``.claude.json`` a Claude Code started with ``env`` reads, if ours."""
    config = env.get(core.CONFIG_DIR_VAR, "").strip()
    wanted = (
        paths.expand_user(Path(config)) / ".claude.json"
        if config
        else core.home_config_dir().with_name(".claude.json")
    )
    return next((one for one in accounts if _same_path(core.claude_json_path(one), wanted)), None)


def _same_path(a: Path, b: Path) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def _share_trust_into(
    target: ClaudeAccount, scope: sharing.TrustScope, accounts: Sequence[ClaudeAccount]
) -> list[str]:
    """Copy an entry of ``scope`` into ``target`` from the first account with one."""
    name = core.label(target)
    try:
        own = sharing.read_claude_json(core.claude_json_path(target))
    except sharing.NotStartedYet:
        return []  # its first run asks for a login before any folder
    except sharing.SharingError as exc:
        return [_trust_not_shared(name, scope, exc)]
    if sharing.trusted_by(own, scope) is not None:
        return []
    for other in accounts:
        if other.slot == target.slot:
            continue
        try:
            key = sharing.trusted_by(sharing.read_claude_json(core.claude_json_path(other)), scope)
        except sharing.SharingError:
            continue
        if key is None:
            continue
        try:
            sharing.grant_trust(core.claude_json_path(target), key)
        except sharing.SharingError as exc:
            return [_trust_not_shared(name, scope, exc)]
        return [SharedNote(f"{key} is trusted on {core.label(other)} — now on {name} too")]
    return []


def trust_command(folder: Path | None) -> str:
    """``aisquare accounts trust <folder>``, quoted for this shell (``install_route.command_line``):
    ``shlex.quote`` wrapped a Windows path in single quotes, which cmd.exe passes on as part
    of the path (review of #259, round 20). ``<dir>`` for a folder not known."""
    if folder is None:
        return "aisquare accounts trust <dir>"
    return install_route.command_line(["aisquare", "accounts", "trust", str(folder)])


def _trust_not_shared(name: str, scope: sharing.TrustScope, exc: Exception) -> str:
    return (
        f"folder trust could not be shared into {name}: {exc} — approve Claude Code's "
        f"question there once, or `{trust_command(scope.folder)}`"
    )


def trust_everywhere(folder: Path) -> tuple[sharing.TrustScope, list[TrustChange]]:
    """Trust ``folder`` on every account at once — ``aisquare accounts trust <dir>``.

    The entry written is the one Claude Code's own yes there would write (the canonical
    repository root, else the folder: :attr:`~core.claude_sharing.TrustScope.key`). An
    account any entry already trusts it on is left as it is. The human's own command, so
    ``[accounts] share_trust`` is not consulted.
    """
    scope = sharing.trust_scope(folder)
    changes: list[TrustChange] = []
    for account in list_accounts():
        path = core.claude_json_path(account)
        try:
            if sharing.trusted_by(sharing.read_claude_json(path), scope) is not None:
                changes.append(TrustChange(account, "already"))
                continue
            written = sharing.grant_trust(path, scope.key)
        except sharing.NotStartedYet as exc:
            changes.append(TrustChange(account, "skipped", reason=str(exc)))
            continue
        except sharing.SharingError as exc:
            changes.append(TrustChange(account, "failed", reason=str(exc)))
            continue
        changes.append(TrustChange(account, "trusted", folders=tuple(written)))
    return scope, changes


def sync_trust() -> tuple[list[TrustChange], int]:
    """Copy every folder any account trusts into every account — ``accounts trust --sync``.

    Folders that no longer exist are not copied; their count comes back beside the
    changes. An account that another entry already trusts a folder on is not given its
    entry too. One write per account.
    """
    accounts = list_accounts()
    read: dict[int, dict[str, Any] | sharing.SharingError] = {}
    every: dict[str, None] = {}
    for account in accounts:
        try:
            data = sharing.read_claude_json(core.claude_json_path(account))
        except sharing.SharingError as exc:
            read[account.slot] = exc
            continue
        read[account.slot] = data
        every.update(dict.fromkeys(sharing.trusted_folders(data)))
    folders = [key for key in every if os.path.isdir(key)]
    scopes = [(key, sharing.trust_scope(Path(key))) for key in folders]
    changes: list[TrustChange] = []
    for account in accounts:
        found = read[account.slot]
        if isinstance(found, sharing.SharingError):
            outcome: Literal["skipped", "failed"] = (
                "skipped" if isinstance(found, sharing.NotStartedYet) else "failed"
            )
            changes.append(TrustChange(account, outcome, reason=str(found)))
            continue
        missing = [key for key, scope in scopes if sharing.trusted_by(found, scope) is None]
        if not missing:
            changes.append(TrustChange(account, "already"))
            continue
        try:
            written = sharing.grant_trust(core.claude_json_path(account), *missing)
        except sharing.SharingError as exc:
            changes.append(TrustChange(account, "failed", reason=str(exc)))
            continue
        changes.append(TrustChange(account, "trusted", folders=tuple(written)))
    return changes, len(every) - len(folders)


def settings_source() -> Path:
    """The ``settings.json`` every managed account shares: the plain claude's."""
    return sharing.settings_path(core.default_account())


def settings_refusal(source: Path | None = None) -> str | None:
    """Why the plain claude's ``settings.json`` cannot be the shared one, or ``None``."""
    source = settings_source() if source is None else source
    if not os.path.isfile(source):
        return f"the plain claude has no settings.json yet ({source})"
    try:
        login = sharing.login_keys(
            sharing.read_settings(source, "the plain claude's settings.json")
        )
    except sharing.SharingError as exc:
        return str(exc)
    if login:
        return (
            f"the plain claude's settings.json sets {', '.join(login)}, which decide a "
            "login — shared, every account would sign in as one"
        )
    return None


def settings_overview() -> tuple[Path, str | None, list[SettingsChange]]:
    """The shared file, why it cannot be shared (if so), and what each managed account holds."""
    source = settings_source()
    held = [
        SettingsChange(account, sharing.settings_state(account, source))
        for account in list_accounts()
        if account.managed
    ]
    return source, settings_refusal(source), held


def share_settings(*, now: datetime | None = None) -> tuple[Path, list[SettingsChange]]:
    """Link every managed account's ``settings.json`` to the plain claude's —
    ``aisquare accounts settings --sync``. :class:`AccountsError` when that file cannot be
    the shared one (:func:`settings_refusal`)."""
    source = settings_source()
    refusal = settings_refusal(source)
    if refusal is not None:
        raise AccountsError(refusal)
    moment = now or _now()
    return source, [_link_settings(one, source, moment) for one in list_accounts() if one.managed]


def _link_settings(account: ClaudeAccount, source: Path, now: datetime) -> SettingsChange:
    if sharing.settings_state(account, source) == "shared":
        return SettingsChange(account, "already")
    try:
        backup = sharing.link_settings(account, source, now=now)
    except sharing.SharingError as exc:
        return SettingsChange(account, "kept", reason=str(exc))
    except OSError as exc:
        return SettingsChange(account, "failed", reason=exc.strerror or str(exc))
    return SettingsChange(account, "shared", backup=backup)


def _share_settings_into(target: ClaudeAccount) -> list[str]:
    """``share_settings`` at a launch: link ``target``'s, saying why not when it cannot."""
    source = settings_source()
    if sharing.settings_state(target, source) == "shared":
        return []
    name = core.label(target)
    refusal = settings_refusal(source)
    if refusal is not None:
        return [f"settings.json is not shared into {name}: {refusal}"]
    change = _link_settings(target, source, _now())
    if change.outcome == "shared":
        kept = f" (its own kept as {change.backup.name})" if change.backup is not None else ""
        return [SharedNote(f"{name} now reads the plain claude's settings.json{kept}")]
    return [f"settings.json is not shared into {name}: {change.reason}"]


def unshare_settings() -> tuple[Path, list[SettingsChange]]:
    """Give every linked managed account its own copy again — ``accounts settings --unshare``.

    Each keeps the settings it has now; none follows the plain claude's any more. With
    ``[accounts] share_settings`` still on, the next launch links it again.
    """
    source = settings_source()
    changes: list[SettingsChange] = []
    for account in list_accounts():
        if not account.managed:
            continue
        try:
            unlinked = sharing.unlink_settings(account, source)
        except OSError as exc:
            changes.append(SettingsChange(account, "failed", reason=exc.strerror or str(exc)))
            continue
        state = sharing.settings_state(account, source)
        changes.append(SettingsChange(account, "unshared" if unlinked else state))
    return source, changes
