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
from aisquare.core import paths, tls
from aisquare.core.config import AccountsSettings, FleetSettings, load_config, save_config
from aisquare.core.harness import MODEL_FAMILIES, model_family
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
    UsageCause,
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

Fetch = Callable[
    [str, Mapping[str, str], float],
    tuple[int, bytes] | tuple[int, bytes, Mapping[str, str]],
]
"""``(url, headers, timeout) -> (status, body[, headers])``: how :func:`usage` reaches the
network. The answer's headers are optional, so a fetch that has none to give (a test's
fake) still works; :func:`_http_get` gives them, for ``Retry-After``."""


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
    cannot be opened costs the ARRANGEMENT (order, aliases, disabled flags)
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

ChoiceSource = Literal["flag", "role binding", "project default", "headroom", "preferred"]


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

    ``account is None`` means nothing chose — no flag, no binding, no project
    default and no enabled, signed-in account to prefer — and the caller must
    leave the launch environment exactly as it found it. It is
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
        """``account 2`` for a flag; ``work · preferred (rank 1)`` when the order decided."""
        if self.account is None:
            return ""
        name = core.label(self.account)
        if self.source == "flag":
            return name
        if self.source == "preferred":
            return f"{name} · preferred (rank {self.account.position})"
        return f"{name} · {self.source}"


def _arranged(store: ContextStore) -> list[ClaudeAccount]:
    """Every slot on disk, folded with its registry row, in priority order.

    Reconciles as it reads (see the section comment): rows are created for new
    slots and dropped for vanished ones, so the table can never describe an
    account that is not there.
    """
    # A machine default an older build set becomes rank 1, once (#8 on the board).
    store.promote_retired_claude_account_default()
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
    """The accounts in priority order, each carrying its alias, rank and disabled flag.

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
    max_age: float | None = None,
) -> AccountChoice:
    """THE account resolver: which Claude account a launch runs under, and why.

    The ladder, top rung wins::

        --account <ref>            the flag (a slot, alias or email)
        team bind <role> --account the role's binding
        accounts default --project the project's default
        accounts order / move      the preferred account: the first enabled,
                                   signed-in one in priority order (rank 1) —
                                   unless the role's binding sets
                                   CLAUDE_CONFIG_DIR by hand, which keeps it
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
    ``spread=True``, which a hand-over passes — the preferred account gives way to
    :func:`headroom_choice`, the account with room in both of its windows,
    ``exclude`` naming the slot a hand-over is leaving. When no account's usage
    can be read, the preferred account decides exactly as before. Like the preferred
    account, a launch's headroom pick leaves a binding that sets
    ``CLAUDE_CONFIG_DIR`` by hand alone; a hand-over's still moves it. A launch reads
    usage through the cache, a minute deep (:data:`PICK_MAX_AGE_SECONDS`); a
    hand-over (``spread=True``) reads it live.

    ``family`` is the launch's model family (``core.harness.launch_family``), for the
    headroom rung's per-family weeks and limit marks (#277). ``max_age`` is how deep a
    launch's headroom rung reads the cache, :data:`PICK_MAX_AGE_SECONDS` when unsaid: a
    form that only SHOWS the pick reads the shared five minutes (:func:`launch_default`).

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
    # The rungs below are SILENT defaults: neither may restore this shell's variables over
    # a `team bind <role> --env CLAUDE_CONFIG_DIR=…` someone wrote by hand, which is what
    # applying an account does (core.apply_launch_env). That binding launched on its own
    # directory before rank 1 was the default, and still does (review of #406, M1), whether
    # rank 1 or a headroom pick would have chosen (#9). A flag, an account binding or a
    # project default still win over it, and so does a hand-over, whose whole point is to
    # move the agent elsewhere: any ``spread`` is one (choose_for_handover reads headroom
    # itself, then falls back here with ``spread=False``).
    hand_written = _binding_names_a_directory(role)
    if hand_written and spread is None:
        return AccountChoice(None, None, notes)
    settings = accounts_settings()
    by_headroom = spread if spread is not None else settings.pick == "headroom"
    if by_headroom:
        # A hand-over reads live; a launch through the cache, a minute deep unless told.
        depth = PICK_MAX_AGE_SECONDS if max_age is None else max_age
        picked, more = headroom_choice(
            accounts,
            switch_at=settings.switch_at,
            exclude=skip,
            fetch=fetch,
            max_age=depth if spread is None else None,
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
    if hand_written:  # a hand-over that found no room: rank 1 is still a silent default
        return AccountChoice(None, None, notes)
    preferred = preferred_account(accounts, skip, notes)
    if preferred is not None:
        return AccountChoice(preferred, "preferred", notes)
    return AccountChoice(None, None, notes)


@dataclass(frozen=True)
class LaunchDefault:
    """What a launch with no ``--account`` runs under, for a form to show before it launches
    (the Spawn dialog's Account field, #12 on the board): :func:`choose`'s answer, and why
    it chose nothing when that is a binding's directory rather than this shell's."""

    choice: AccountChoice
    bound_directory: bool = False
    """Nothing was chosen because the role's binding sets ``CLAUDE_CONFIG_DIR`` by hand,
    which the launch keeps (#9): it runs on that directory, not on this shell's."""


def launch_default(
    role: str | None, *, project: ProjectInfo | None = None, family: str | None = None
) -> LaunchDefault:
    """:func:`choose` with no flag, as ``fleet spawn`` asks it for ``role`` in ``project``.

    For showing, not launching: its headroom rung reads the shared cache five minutes deep
    (:data:`USAGE_MAX_AGE_SECONDS`, the status line's depth), so a form that shows it sends
    no request the status line would not. The launch itself reads a minute deep and can
    pick another account when a window moved since. Raises :class:`NoSuchAccount` as the
    launch would.
    """
    choice = choose(None, role=role, project=project, family=family, max_age=USAGE_MAX_AGE_SECONDS)
    return LaunchDefault(choice, choice.account is None and _binding_names_a_directory(role))


def _binding_names_a_directory(role: str | None) -> bool:
    """Whether ``role``'s binding sets Claude Code's directory itself (``env`` or ``env_files``).

    An unreadable config names nothing: the binding is not applied either, so the
    preferred rung is all that is left to decide (``harness.resolve_profile``).
    """
    if role is None:
        return False
    try:
        profile = load_config().team.profiles.get(role)
    except Exception:  # a broken config.toml costs the binding, never the launch
        return False
    if profile is None:
        return False
    return any(var in profile.env or var in profile.env_files for var in core.LAUNCH_VARS)


def preferred_account(
    accounts: Sequence[ClaudeAccount], skip: Iterable[int] = (), notes: list[str] | None = None
) -> ClaudeAccount | None:
    """The machine default: the first enabled, signed-in account in priority order.

    There is no separate default to pick (#8 on the board): rank 1 IS the
    default, and an account that cannot run a launch — disabled, or with no
    login — hands the place to the next. One in ``skip`` (the account a
    hand-over is leaving) is passed over with a note, said as the rungs above
    say it (review of #205, fourth round).
    """
    for account in accounts:
        if account.disabled or not core.signed_in(account):
            continue
        if account.slot in skip:
            if notes is not None:
                notes.append(
                    f"{core.label(account)} (preferred, rank {account.position}) is the "
                    "account being left — skipped"
                )
            continue
        return account
    return None


# --- arranging: the writers ---------------------------------------------------------------------
#
# Every writer resolves its reference FIRST (a bad name is a usage error, not a
# half-applied change), then opens the store once. They raise AccountsError
# subclasses with a sentence — never a traceback — because the CLI and the page
# both show the message as-is.


def set_default(ref: str | None, *, project: ProjectInfo | None = None) -> ClaudeAccount | None:
    """Make ``ref`` ``project``'s default and return it (``None`` clears) — or, with no
    project, move ``ref`` to rank 1, which is what the machine default is.

    A project default is stored as the SLOT NUMBER, resolved now: the project
    table is ours, and a number cannot go stale the way a typed alias could.
    ``remove`` clears every project default that names the slot it removes. The
    machine's cannot be cleared: some account is always first in the order.
    """
    if project is None and ref is None:
        raise AccountsError(
            "the machine default is rank 1 of the priority order and cannot be cleared — "
            "reorder instead: aisquare accounts move <slot|alias|email> top"
        )
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
            # The row must exist before it can be the default.
            arranged = {each.slot for each in _arranged(store)}
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
            elif account is not None:
                # order_claude_accounts ignores an unknown slot, so a slot removed since
                # resolve() would print "moved to rank 1" over an order nothing changed.
                if account.slot not in arranged:
                    raise KeyError(account.slot)
                store.order_claude_accounts([account.slot])
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
    """What a launch runs under when nothing more specific says: :func:`preferred_account`."""
    accounts, note = _read_arranged()
    return None if note is not None else preferred_account(accounts)


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
        store.set_ui_state(_BACKOFF_KEY.format(slot=slot), None)  # and its wait (F1 of #400)
        store.set_ui_state(_FAMILY_KEY.format(slot=slot), None)  # and its family weeks


# --- usage ------------------------------------------------------------------------------


def _http_get(
    url: str, headers: Mapping[str, str], timeout: float
) -> tuple[int, bytes, Mapping[str, str]]:
    """One GET; an HTTP error status is an answer, not an exception. The answer's headers
    come back too: a 429's ``Retry-After`` says how long to wait (F1 of #400)."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with tls.urlopen(request, timeout=timeout) as response:
            return int(response.status), bytes(response.read()), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        sent = dict(exc.headers.items()) if exc.headers is not None else {}
        return int(exc.code), bytes(exc.read() or b""), sent


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


def _retry_after(headers: Mapping[str, str], now: datetime) -> float | None:
    """The seconds an answer's ``Retry-After`` asks to wait — a number, or an HTTP date —
    clamped to [1 s, :data:`RETRY_AFTER_MAX_SECONDS`]; ``None`` when it sent none we read."""
    raw = next((value for key, value in headers.items() if key.lower() == "retry-after"), None)
    if raw is None or not raw.strip():
        return None
    seconds: float | None
    try:
        seconds = float(raw.strip())
    except ValueError:
        from email.utils import parsedate_to_datetime

        try:
            when = parsedate_to_datetime(raw.strip())
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - now).total_seconds()
    if seconds != seconds:  # NaN
        return None
    return min(RETRY_AFTER_MAX_SECONDS, max(1.0, seconds))


_FAMILY_WEEK = "seven_day_"
"""The prefix of a per-family weekly window in the usage payload: ``seven_day_opus``…"""
_FAMILIES = frozenset(MODEL_FAMILIES.values())
"""The model families a per-family window or limit can name (``opus``, ``sonnet``…)."""
_SESSION_KINDS = frozenset({"session", "five_hour"})
_WEEK_KINDS = frozenset({"weekly", "weekly_all", "week", "seven_day"})
_SCOPED_KIND = "weekly_scoped"
"""The ``limits[]`` kind of a per-family week (P0-W0, Claude Code 2.1.309): its
``scope.model.display_name`` names the family ("Fable", "Opus")."""


def _fuller(a: float | None, b: float | None) -> float | None:
    return b if a is None else a if b is None else max(a, b)


@dataclass(frozen=True)
class _Limit:
    """One ``limits[]`` entry: its kind, percent USED, the model family a ``weekly_scoped``
    entry is scoped to (``None`` otherwise, or when the name is no family), and its reset."""

    kind: str
    percent: float
    family: str | None = None
    resets_at: datetime | None = None


def _family_weeks(payload: Mapping[str, Any]) -> tuple[dict[str, float], dict[str, datetime]]:
    """Every per-family week in ``payload``, keyed by family, the fuller figure winning.

    Two places can say it. ``limits[]`` entries of kind ``weekly_scoped``, scoped to a
    model by ``scope.model.display_name``, are where the endpoint really sends it (P0-W0:
    every ``seven_day_<family>`` key was null). A top-level ``seven_day_<family>`` window
    is read too, but only for a known family: ``seven_day_breakdown`` and its like are
    ``seven_day_*`` dicts that are no window (#277, P0-W3c). A ``limits[]`` kind named
    after a family (``opus``, ``seven_day_opus``) counts as that family's week as well.
    """
    percents: dict[str, float] = {}
    resets: dict[str, datetime] = {}
    for key, raw in payload.items():
        family = key.removeprefix(_FAMILY_WEEK)
        if not key.startswith(_FAMILY_WEEK) or family not in _FAMILIES:
            continue
        percent, when = _window(raw)
        if percent is not None:
            percents[family] = percent
        if when is not None:
            resets[family] = when
    for limit in _limits(payload):
        named = limit.kind.removeprefix(_FAMILY_WEEK)
        scoped = limit.family or (named if named in _FAMILIES else None)
        if scoped is None:
            continue
        percents[scoped] = max(percents.get(scoped, limit.percent), limit.percent)
        if limit.resets_at is not None and scoped not in resets:
            resets[scoped] = limit.resets_at
    return percents, resets


def _limits(payload: Mapping[str, Any]) -> list[_Limit]:
    """``limits[]`` as :class:`_Limit`: an entry without a string kind or a number is
    skipped — the field is undocumented, and an unknown shape costs it, never the reading."""
    raw = payload.get("limits")
    found: list[_Limit] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        kind, percent = entry.get("kind"), entry.get("percent", entry.get("utilization"))
        if not isinstance(kind, str) or not isinstance(percent, int | float):
            continue
        family = _scoped_family(entry) if kind.lower() == _SCOPED_KIND else None
        _percent, when = _window({"resets_at": entry.get("resets_at")})
        found.append(_Limit(kind.lower(), float(percent), family, when))
    return found


def _scoped_family(entry: Mapping[str, Any]) -> str | None:
    """The family a ``weekly_scoped`` entry's ``scope.model.display_name`` names, or ``None``."""
    scope = entry.get("scope")
    model = scope.get("model") if isinstance(scope, dict) else None
    name = model.get("display_name") if isinstance(model, dict) else None
    return model_family(name) if isinstance(name, str) else None


def _limit_of(payload: Mapping[str, Any], kinds: frozenset[str]) -> float | None:
    return max((limit.percent for limit in _limits(payload) if limit.kind in kinds), default=None)


# --- why there is no reading, and what to do about it --------------------------------------------
#
# Every reading with nothing to show carries a cause (`UsageCause`) and the fix for it,
# stamped in ONE place, `unreadable_usage`; the fixes are the one table `USAGE_FIXES`. The
# Accounts page shows the two on hover over its '!', and `accounts usage` beside the
# reason: neither works out the cause or the fix from the reason's words.

_LOG_IN = "press Log in (in a terminal: aisquare accounts run {slot}, then /login)"
_REFRESH_TOKEN = (
    "press Log in, or open a session on this account (aisquare accounts run {slot}): "
    "Claude Code refreshes the token"
)
_THEN_REFRESH = "then ↻ Refresh usage (or --refresh)"

USAGE_FIXES: dict[UsageCause, str | None] = {
    UsageCause.not_signed_in: _LOG_IN,
    UsageCause.token_missing: _LOG_IN,
    UsageCause.token_expired: _REFRESH_TOKEN,
    UsageCause.token_rejected: _REFRESH_TOKEN,
    UsageCause.keychain_locked: (
        f"unlock the login keychain (Keychain Access, or log out and back in), {_THEN_REFRESH}"
    ),
    UsageCause.keychain_refused: f"allow aisquare in Keychain Access, {_THEN_REFRESH}",
    UsageCause.keychain_timeout: (
        f"answer the Keychain's prompt if one is waiting, {_THEN_REFRESH}"
    ),
    # A Keychain that would not say is not asked again by itself (core.read_keychain).
    UsageCause.keychain_failed: "↻ Refresh usage (or --refresh) asks the Keychain again",
    UsageCause.rate_limited: "none needed, reads resume {at}by themselves",
    UsageCause.unreachable: "check the network or HTTPS_PROXY",
    UsageCause.tls_bundle: tls.BUNDLE_FIX,  # the same words as doctor's tls row
    UsageCause.tls_cert: tls.CERT_HINT,
    UsageCause.http_error: None,
    UsageCause.bad_answer: None,
    UsageCause.read_failed: None,
}
"""What to do about each cause, ``{slot}`` and ``{at}`` filled in by :func:`usage_fix`; ``None``
where nothing done on this machine helps, which :data:`NO_FIX` says."""

TLS_BUNDLE_REASON = "TLS: the CA bundle could not be loaded"
"""Why there is no reading when ``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` will not load (#403)."""
TLS_CERT_REASON = "TLS certificate could not be verified"
"""Why there is no reading when the endpoint's certificate did not verify (#403)."""

NO_FIX = "fix: none from here, the next read tries again by itself"
"""What :func:`fix_line` says for a cause with no fix (:data:`USAGE_FIXES`)."""


def usage_fix(
    cause: UsageCause, *, slot: int | None = None, until: datetime | None = None
) -> str | None:
    """What to do about ``cause`` on slot ``slot`` (:data:`USAGE_FIXES`): ``until`` is a rate
    limit's end, ``reads resume at 14:32 by themselves``."""
    fix = USAGE_FIXES[cause]
    if fix is None:
        return None
    at = f"at {until.astimezone().strftime('%H:%M')} " if until is not None else ""
    return fix.format(slot="<slot>" if slot is None else slot, at=at)


def unreadable_usage(
    cause: UsageCause, reason: str, *, slot: int | None = None, **fields: Any
) -> ClaudeUsage:
    """A reading with nothing to show: ``reason`` says what is wrong, and ``cause`` and its
    fix (:func:`usage_fix`) are stamped here, the one place they are."""
    until = fields.get("rate_limited_until")
    return ClaudeUsage(
        available=False,
        reason=reason,
        cause=cause,
        fix=usage_fix(cause, slot=slot, until=until),
        **fields,
    )


def fix_line(reading: ClaudeUsage) -> str:
    """``fix: press Log in …``, or :data:`NO_FIX` for a reading whose cause has none."""
    return f"fix: {reading.fix}" if reading.fix else NO_FIX


def kept_fix_line(reading: ClaudeUsage, *, slot: int | None = None) -> str | None:
    """``fix: point SSL_CERT_FILE …``: the fix for the failed try a kept reading stands in
    for (:attr:`~ClaudeUsage.last_failed_cause`, :data:`USAGE_FIXES`), or :data:`NO_FIX`;
    ``None`` for a live reading, or a kept one recorded before the cause was."""
    cause = reading.last_failed_cause
    if not reading.available or reading.last_failed_reason is None or cause is None:
        return None
    fix = usage_fix(cause, slot=slot, until=reading.rate_limited_until)
    return f"fix: {fix}" if fix else NO_FIX


def explain_usage(reading: ClaudeUsage) -> str:
    """``the stored token has expired; fix: press Log in …``: the reason and its fix in one
    line, for a surface with no hover (``accounts list --usage``, ``accounts usage``)."""
    return f"{reading.reason or 'unavailable'}; {fix_line(reading)}"


_EXPIRED = "the stored token has expired"
_REJECTED = "the token was rejected (HTTP 401)"
_KEYCHAIN_CAUSES: dict[core.KeychainProblem, tuple[UsageCause, str | None]] = {
    "locked": (UsageCause.keychain_locked, core.KEYCHAIN_LOCKED_REASON),
    "refused": (UsageCause.keychain_refused, core.KEYCHAIN_REFUSED_REASON),
    "timeout": (UsageCause.keychain_timeout, None),
    "failed": (UsageCause.keychain_failed, None),
}
"""A Keychain's kind of refusal as a reading's cause, and its reason when the core's words
carry a fix of their own (``None``: its words stand)."""


def _no_login_reason() -> str:
    if core.keychain_platform():
        return "no login in the Keychain or a credentials file — not signed in"
    return "no credentials file — not signed in"


def usage_problem(status: ClaudeAccountStatus, reading: ClaudeUsage | None) -> ClaudeUsage | None:
    """Why a row has no usage to show, worst first — or ``None`` when it shows a reading
    (a kept one too: F1 of #400) or one is still owed.

    No login, then a token gone or expired, all read from ``status`` without a request;
    then the reading's own cause (a 401, a locked Keychain, the endpoint). The page's
    '!' and its tooltip, and ``accounts usage``, are drawn from it alone.
    """
    slot = status.account.slot
    if status.identity is None:
        return unreadable_usage(UsageCause.not_signed_in, "not signed in", slot=slot)
    if not status.signed_in:
        return unreadable_usage(UsageCause.token_missing, _no_login_reason(), slot=slot)
    if status.token_state == "expired":
        return unreadable_usage(UsageCause.token_expired, _EXPIRED, slot=slot, needs_login=True)
    if reading is not None and not reading.available:
        return reading
    return None


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
    slot = account.slot
    login = core.stored_login(account)
    creds = login.credentials
    if creds is None:
        if login.unreadable is None:
            return unreadable_usage(UsageCause.token_missing, _no_login_reason(), slot=slot)
        cause, said = _KEYCHAIN_CAUSES[login.problem or "failed"]
        return unreadable_usage(cause, said or login.unreadable, slot=slot)
    if creds.expired(now):
        return unreadable_usage(UsageCause.token_expired, _EXPIRED, slot=slot, needs_login=True)
    headers = {
        "Authorization": f"Bearer {creds.access_token}",
        "anthropic-beta": OAUTH_BETA,
        "Accept": "application/json",
        "User-Agent": f"aisquare-cli/{__version__}",
    }
    try:
        answer = (fetch or _http_get)(USAGE_URL, headers, USAGE_TIMEOUT_SECONDS)
    except OSError as exc:  # URLError, a refused connection, a timeout
        # #403's two TLS failures, each with its own cause: the fix is tls.BUNDLE_FIX /
        # tls.CERT_HINT (USAGE_FIXES), shown beside the reason, not inside it.
        if isinstance(exc, tls.CABundleError):
            return unreadable_usage(UsageCause.tls_bundle, TLS_BUNDLE_REASON, slot=slot)
        if tls.cert_failure(exc):
            return unreadable_usage(UsageCause.tls_cert, TLS_CERT_REASON, slot=slot)
        return unreadable_usage(
            UsageCause.unreachable,
            f"could not reach the usage endpoint ({type(exc).__name__})",
            slot=slot,
        )
    status, body = answer[0], answer[1]
    answered: Mapping[str, str] = answer[2] if len(answer) == 3 else {}
    wait = _retry_after(answered, now or _now())
    if status == 429 or (status == 503 and wait is not None):
        # Not a login problem: the wait is recorded by `read_usage` (F1 of #400).
        moment = now or _now()
        return unreadable_usage(
            UsageCause.rate_limited,
            f"HTTP {status} from the usage endpoint — rate-limited",
            slot=slot,
            rate_limited=True,
            rate_limited_until=moment + timedelta(seconds=wait) if wait is not None else None,
        )
    if status == 401:
        return unreadable_usage(UsageCause.token_rejected, _REJECTED, slot=slot, needs_login=True)
    if status != 200:
        return unreadable_usage(
            UsageCause.http_error, f"HTTP {status} from the usage endpoint", slot=slot
        )
    try:
        payload: Any = json.loads(body)
    except ValueError:
        return unreadable_usage(
            UsageCause.bad_answer, "the usage endpoint did not answer with JSON", slot=slot
        )
    if not isinstance(payload, dict):
        return unreadable_usage(
            UsageCause.bad_answer, "the usage endpoint answered in an unknown shape", slot=slot
        )
    session_percent, session_resets = _window(payload.get("five_hour"))
    week_percent, week_resets = _window(payload.get("seven_day"))
    if session_percent is None and week_percent is None:
        return unreadable_usage(
            UsageCause.bad_answer, "the usage endpoint answered without the two windows", slot=slot
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
# rung (the preferred account, rank 1) decides exactly as it did before #146.

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
    # Through `read_usage`, so a slot the endpoint asked to wait is not asked (review of
    # #408, N5): it records the answered reading the same way.
    return read_usage([account], now=now, fetch=fetch)[account.slot]


_FAMILY_KEY = "usage-family:{slot}"
"""The ``ui_state`` row that holds a slot's newest reading's per-family weeks, which the usage
history has no columns for (:func:`_record_sample`, :func:`_with_family_weeks`)."""


def _record_sample(
    store: ContextStore, account: ClaudeAccount, result: ClaudeUsage, now: datetime | None
) -> None:
    """The reading's row — and a limit mark for each family week it reads spent, which the
    row has no column for: a launch picks from the cache, and without the mark it would put
    an Opus agent on an account whose Opus week a live reading saw spent (#277).

    The family weeks themselves go in one ``ui_state`` row per slot, stamped with the
    sample they came with (:data:`_FAMILY_KEY`, no schema step). Served from the store, a
    reading had none, so ``accounts usage`` without ``--refresh``, nearly every read while
    asq runs, showed no family week, a spent one included (tester's pass on #401, (a))."""
    for family, percent in result.family_week_percent.items():
        resets = result.family_week_resets_at.get(family)
        if percent >= 100 and resets is not None:
            _write_limit(store, account.slot, family, resets, how="read", now=now)
    sample = UsageSample(
        slot=account.slot,
        fetched_at=result.fetched_at or now or _now(),
        session_percent=result.session_percent,
        session_resets_at=result.session_resets_at,
        week_percent=result.week_percent,
        week_resets_at=result.week_resets_at,
        token_fingerprint=_token_fingerprint(account),
    )
    store.add_usage_sample(sample)
    families = None
    if result.family_week_percent or result.family_week_resets_at:
        families = json.dumps(
            {
                "fetched_at": sample.fetched_at.isoformat(),
                "token": sample.token_fingerprint,
                "percent": result.family_week_percent,
                "resets": {
                    family: when.isoformat()
                    for family, when in result.family_week_resets_at.items()
                },
            }
        )
    store.set_ui_state(_FAMILY_KEY.format(slot=account.slot), families)


def _with_family_weeks(
    store: ContextStore, slot: int, sample: UsageSample, reading: ClaudeUsage, now: datetime
) -> ClaudeUsage:
    """``reading`` (built from ``sample``) with the family weeks recorded with that very
    sample (:data:`_FAMILY_KEY`); as it was when the row is another sample's, or unreadable.

    A family week whose reset has passed by ``now`` comes back without its figure, its reset
    time kept, as :func:`_without_reset_windows` empties a kept reading's: given back whole,
    a pick read the old figure for the cache's window after the reset, skipped the account
    as "its opus week is spent" and ranked it on it (review of #401, m2)."""
    raw = store.ui_state(_FAMILY_KEY.format(slot=slot))
    if not raw:
        return reading
    try:
        data = json.loads(raw)
        if (
            datetime.fromisoformat(data["fetched_at"]) != sample.fetched_at
            or data.get("token") != sample.token_fingerprint
        ):
            return reading
        percent = {str(family): float(value) for family, value in data["percent"].items()}
        resets = {
            str(family): datetime.fromisoformat(when) for family, when in data["resets"].items()
        }
    except (ValueError, TypeError, KeyError, AttributeError):
        return reading
    over = {family for family, when in resets.items() if when <= now}
    percent = {family: used for family, used in percent.items() if family not in over}
    return reading.model_copy(
        update={"family_week_percent": percent, "family_week_resets_at": resets}
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


# --- backing off a rate-limited read (F1 of #400) ----------------------------------------------
#
# A 429 (or a 503 that says when to come back) is the endpoint asking this
# account's reads to stop for a while. They used to keep coming: the wait was
# dropped with the headers, failures were remembered only in the process that
# saw them, and `--refresh` skipped even that, so a script, a manager's loop or
# a few terminals kept the account rate-limited. Now the wait is recorded per
# slot in the store (a `ui_state` row, so no schema step), and `read_usage` —
# the one fetcher every surface reads through — sends nothing for a slot before
# it ends, whichever process asks, forced or not. The wait is the endpoint's
# `Retry-After`, or, when it gave none, an exponential default that doubles
# with each 429 in a row and starts over after a good read. A gated read is
# answered with the reason and the moment the wait ends; the cache then shows
# the last good reading beside it (`cached_usage`), and a headroom pick decides
# by it only while it is fresh (`headroom_choice`).

RETRY_AFTER_MAX_SECONDS = 6 * 3600.0
"""The longest ``Retry-After`` honoured: a header asking for days is a broken header."""
USAGE_BACKOFF_FIRST_SECONDS = 60.0
"""The wait after a 429 that named none: one minute, doubled for each 429 in a row."""
USAGE_BACKOFF_MAX_SECONDS = 1800.0
"""Where the doubling stops: half an hour."""
_BACKOFF_KEY = "usage-backoff:{slot}"
"""The ``ui_state`` row a slot's wait is recorded in."""


@dataclass(frozen=True)
class UsageBackoff:
    """One slot's recorded wait: no usage request before ``until``.

    ``attempts`` counts the 429s in a row that led here (the exponential default's
    exponent); ``since`` is when the latest came back. ``token`` is whose wait it is
    (:func:`_login_key`): a wait is the login's, not the slot's, so a slot signed into
    another login since is not held to it (review of #408, N3), and a token refresh or a
    *Log in again* to the same account is (m1). ``raw`` is the row as read, for the
    compare-and-set that clears or replaces it (N2).
    """

    slot: int
    until: datetime
    attempts: int
    since: datetime
    token: str | None = None
    raw: str | None = field(default=None, compare=False)

    def active(self, now: datetime) -> bool:
        return self.until > now

    def holds(self, token: str | None) -> bool:
        """Whether this wait is the login ``token`` (:func:`_login_key`) names."""
        return self.token == token

    @property
    def reason(self) -> str:
        return backoff_reason(self.until)

    def reading(self, now: datetime) -> ClaudeUsage:
        """What a read of this slot answers while the wait runs: nothing, and why."""
        # Stamped with when the 429 came back, not ``now``: a gated read sends nothing, and
        # "last try failed just now" said a try was made (review of #408, N1).
        return unreadable_usage(
            UsageCause.rate_limited,
            self.reason,
            slot=self.slot,
            rate_limited=True,
            rate_limited_until=self.until,
            fetched_at=min(self.since, now),
        )

    def dump(self) -> str:
        return json.dumps(
            {
                "until": self.until.isoformat(),
                "attempts": self.attempts,
                "since": self.since.isoformat(),
                "token": self.token,
            }
        )

    @classmethod
    def load(cls, slot: int, raw: str | None) -> UsageBackoff | None:
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return cls(
                slot=slot,
                until=datetime.fromisoformat(data["until"]),
                attempts=int(data.get("attempts", 1)),
                since=datetime.fromisoformat(data["since"]),
                token=data.get("token"),
                raw=raw,
            )
        except (ValueError, TypeError, KeyError):
            return None  # a row this version cannot read waits for nothing


def _login_key(account: ClaudeAccount, now: datetime) -> str | None:
    """Whose wait a slot is held to: the account signed into it, by the uuid and email its
    ``.claude.json`` records, which a token refresh and a *Log in again* to the same account
    keep; the token's fingerprint when the file names no login.

    Keyed to the token alone, every OAuth refresh and every *Log in again* lifted the wait,
    restarted the doubling and sent a request at once, while the row said logging in again
    does not clear it (review of #408, m1). Another account signed into the slot is still
    not held to the last one's wait (N3).
    """
    who = core.identity(account)
    if who is not None:
        return f"login:{who.account_uuid or ''}:{who.email}"
    return _token_fingerprint(account, now)


def rate_limited_until_text(until: datetime | None) -> str:
    """``rate-limited until 14:32`` (local time), or ``rate-limited`` with no time known."""
    if until is None:
        return "rate-limited"
    return f"rate-limited until {until.astimezone().strftime('%H:%M')}"


def backoff_reason(until: datetime) -> str:
    """``rate-limited until 14:32 — logging in again does not clear it``."""
    return f"{rate_limited_until_text(until)} — logging in again does not clear it"


def _next_backoff(
    slot: int,
    reading: ClaudeUsage,
    before: UsageBackoff | None,
    now: datetime,
    token: str | None = None,
) -> UsageBackoff:
    """The wait a rate-limited answer starts: its ``Retry-After`` (already turned into
    ``rate_limited_until`` by :func:`usage`), else the doubling default. A 429 long after
    the last wait ended, or for another login, starts the doubling over."""
    attempts = 1
    if (
        before is not None
        and before.holds(token)
        and before.until > now - timedelta(seconds=USAGE_BACKOFF_MAX_SECONDS)
    ):
        attempts = before.attempts + 1
    until = reading.rate_limited_until
    if until is None or until <= now:
        seconds = min(
            USAGE_BACKOFF_MAX_SECONDS, USAGE_BACKOFF_FIRST_SECONDS * 2 ** min(attempts - 1, 16)
        )
        until = now + timedelta(seconds=seconds)
    return UsageBackoff(slot=slot, until=until, attempts=attempts, since=now, token=token)


def _record_backoff(store: ContextStore, held: UsageBackoff, seen: UsageBackoff | None) -> None:
    """Write ``held`` unless the row changed since ``seen`` was read and now holds a longer
    wait for the same login: a stale write never shortens a newer wait (review of #408, N2).

    A row this version cannot read (another version's shape, a truncated write) is replaced:
    compared as absent, it was never, and that slot was asked on every read (n1). Replaced as
    it was just read, never blindly: a valid, longer wait another process wrote meanwhile
    stands (review of #401, n1)."""
    key = _BACKOFF_KEY.format(slot=held.slot)
    expected = seen.raw if seen is not None else None
    if store.replace_ui_state(key, expected, held.dump()):
        return
    raw = store.ui_state(key)
    current = UsageBackoff.load(held.slot, raw)
    if current is None:
        store.replace_ui_state(key, raw, held.dump())
        return
    if current.holds(held.token) and current.until >= held.until:
        return  # another process recorded a wait at least as long: keep it
    store.replace_ui_state(key, current.raw, held.dump())


def _read_backoffs(store: ContextStore, slots: Iterable[int]) -> dict[int, UsageBackoff]:
    found: dict[int, UsageBackoff] = {}
    for slot in slots:
        held = UsageBackoff.load(slot, store.ui_state(_BACKOFF_KEY.format(slot=slot)))
        if held is not None:
            found[slot] = held
    return found


def usage_backoffs(
    accounts: Sequence[ClaudeAccount], *, now: datetime | None = None
) -> dict[int, UsageBackoff]:
    """The waits still running for ``accounts``, by slot: doctor's line, and a test's look.
    A store that cannot be opened reads as none.

    Whose login a slot holds is read only for a slot with a wait recorded: on macOS that read
    can be the Keychain, once per slot on every doctor run (review of #408, n2)."""
    moment = now or _now()
    try:
        with store_session() as store:
            held = _read_backoffs(store, [account.slot for account in accounts])
    except sqlite3.Error:
        return {}
    logins = {
        account.slot: _login_key(account, moment) for account in accounts if account.slot in held
    }
    return {
        slot: backoff
        for slot, backoff in held.items()
        if backoff.active(moment) and backoff.holds(logins.get(slot))
    }


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

    A slot the endpoint asked to wait (:class:`UsageBackoff`, F1 of #400) is not
    asked before the wait ends — whoever reads, forced or not — and is answered
    with the reason and ``rate_limited_until``. A rate-limited answer records the
    wait, a good one clears it, in that same store session.
    """
    if not accounts:
        return {}
    moment = now or _now()
    readings: dict[int, ClaudeUsage] = {}
    backoffs: dict[int, UsageBackoff] = {}
    with contextlib.suppress(sqlite3.Error), store_session() as store:
        backoffs = _read_backoffs(store, [account.slot for account in accounts])
    logins: dict[int, str | None] = {}

    def _login_of(account: ClaudeAccount) -> str | None:
        """:func:`_login_key`, read once per slot, and only for a slot a wait concerns. A name
        of its own: the call-graph guards resolve a call by its name, and ``login`` is the
        accounts command's (``tests/test_config_writes_stay_in_the_cli.py``)."""
        if account.slot not in logins:
            logins[account.slot] = _login_key(account, moment)
        return logins[account.slot]

    asked: list[ClaudeAccount] = []
    for account in accounts:
        held = backoffs.get(account.slot)
        if held is not None and held.active(moment) and held.holds(_login_of(account)):
            readings[account.slot] = held.reading(moment)
        else:
            asked.append(account)
    if asked:
        with ThreadPoolExecutor(max_workers=min(HEADROOM_WORKERS, len(asked))) as pool:
            futures = {
                pool.submit(usage, account, now=now, fetch=fetch): account.slot for account in asked
            }
            for future, slot in futures.items():
                try:
                    readings[slot] = future.result()
                except Exception as exc:  # one account's failure must not cost the rest
                    readings[slot] = unreadable_usage(
                        UsageCause.read_failed,
                        f"usage read failed ({type(exc).__name__})",
                        slot=slot,
                    )
    answered = [account for account in asked if readings[account.slot].available]
    cleared = [account.slot for account in answered if account.slot in backoffs]
    started = {
        account.slot: _next_backoff(
            account.slot,
            readings[account.slot],
            backoffs.get(account.slot),
            moment,
            _login_of(account),
        )
        for account in asked
        if readings[account.slot].rate_limited
    }
    for slot, held in started.items():
        readings[slot] = held.reading(moment)
    if answered or started or cleared:
        with contextlib.suppress(sqlite3.Error), store_session() as store:
            for account in answered:
                _record_sample(store, account, readings[account.slot], now)
            for slot in cleared:  # only the row this round read: never a newer wait (N2)
                store.replace_ui_state(_BACKOFF_KEY.format(slot=slot), backoffs[slot].raw, None)
            for slot, held in started.items():
                _record_backoff(store, held, backoffs.get(slot))
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
# account, the other was asked to look now. The move `[accounts] before_limit` makes,
# of an agent that hit no limit, picks from the cache like a launch (F2 of #400).

USAGE_MAX_AGE_SECONDS = 300.0
"""How old a recorded reading may be and still be served instead of a fetch: five minutes."""
LAST_GOOD_LOOKBACK = timedelta(days=7)
"""How far back a failed read looks for the last good reading to show: the history's
whole retention (``core.store._USAGE_RETENTION``)."""

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
    current: bool = True,
) -> dict[int, ClaudeUsage]:
    """Each account's newest reading recorded at or after ``since`` WITH the token it holds
    now (``tokens``, by slot), in one store session.

    A reading another token took is not served: a slot signed into another login
    since would show the last one's figure. Nor, while ``current``, is one of whose
    windows has reset since: the window it describes is over, and the new one starts
    near 0 %. ``current=False`` is the last good reading a failed read shows, aged
    (F1 of #400): an old figure, said to be old, beats ``—``.
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
                if current and any(when is not None and when <= now for when in resets):
                    continue
                found[account.slot] = _with_family_weeks(
                    store,
                    account.slot,
                    newest,
                    ClaudeUsage(
                        available=True,
                        session_percent=newest.session_percent,
                        session_resets_at=newest.session_resets_at,
                        week_percent=newest.week_percent,
                        week_resets_at=newest.week_resets_at,
                        fetched_at=newest.fetched_at,
                    ),
                    now,
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

    A remembered wait that has ended is not served: after a short ``Retry-After`` the row
    said "rate-limited until" a time already past, and nothing was asked until the five
    minutes were up (review of #408, n5).
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
        if failed is not None and failed.rate_limited and _wait_over(failed, now):
            failed = None
        if (answer := _preferred(recorded.get(account.slot), failed)) is not None:
            served[account.slot] = answer
    return served


def _wait_over(reading: ClaudeUsage, now: datetime) -> bool:
    """Whether the wait a rate-limited ``reading`` names has ended by ``now``."""
    return reading.rate_limited_until is not None and reading.rate_limited_until <= now


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

    A wait (``rate_limited``) marks the figure whatever order the two came in: a gated read
    is stamped with the moment its 429 came back, so a reading another process recorded
    just after that was served as live for the whole wait, with no age and no wait, and the
    fleet's dot counted it (review of #408, MAJOR-1).
    """
    if failed is None or good is None:
        return good or failed
    if not failed.rate_limited and (
        failed.fetched_at is None
        or (good.fetched_at is not None and failed.fetched_at <= good.fetched_at)
    ):
        return good  # the latest try read it
    if failed.needs_login:
        return failed
    return good.model_copy(
        update={
            "last_failed_at": failed.fetched_at,
            "last_failed_reason": failed.reason,
            "last_failed_cause": failed.cause,
            "rate_limited": failed.rate_limited,
            "rate_limited_until": failed.rate_limited_until,
        }
    )


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
        return _with_last_good(accounts, readings, tokens, moment)
    with _fetching(stale, seen) as moved:
        if moved and not force:  # another call fetched meanwhile: what it read serves
            readings.update(_served(stale, tokens, since=since, now=moment))
            stale = [account for account in stale if account.slot not in readings]
        fetched = read_usage(stale, now=now, fetch=fetch)
        for account in stale:
            reading = fetched[account.slot]
            token = tokens.get(account.slot)
            if not reading.available and not reading.rate_limited:
                # A wait keeps the moment its 429 came back: nothing was tried now (N1).
                reading = reading.model_copy(update={"fetched_at": moment})
            if token is not None:
                with _UNANSWERED_LOCK:
                    key = (str(account.config_dir), token)
                    if reading.available:
                        _UNANSWERED.pop(key, None)
                    else:
                        _UNANSWERED[key] = reading
            readings[account.slot] = reading
    return _with_last_good(accounts, readings, tokens, moment)


def is_kept(reading: ClaudeUsage) -> bool:
    """Whether ``reading`` is not a live one: a slot the endpoint asked to wait, or the last
    good figure a failed read is showing (``last_failed_reason``). Shown, aged; never
    picked by or counted in the fleet's dot unless fresh (review of #408, MAJ-1)."""
    return reading.kept


def _has_reset(reading: ClaudeUsage, now: datetime) -> bool:
    """Whether any window of ``reading`` has reset by ``now``, a family week included (review
    of #401, m2): the figure is over."""
    resets = (
        reading.session_resets_at,
        reading.week_resets_at,
        *reading.family_week_resets_at.values(),
    )
    return any(when is not None and when <= now for when in resets)


def _without_reset_windows(reading: ClaudeUsage, now: datetime) -> ClaudeUsage:
    """A kept reading with each window that has reset since emptied: its percentage
    describes a window that is over, so it is shown as reset (``reset 1h ago``, the reset
    time kept), never as the old figure with ``resets now`` (review of #408, MAJ-1)."""
    update: dict[str, Any] = {}
    for window in ("session", "week"):
        when = getattr(reading, f"{window}_resets_at")
        if when is not None and when <= now:
            update[f"{window}_percent"] = None
    over = {
        family
        for family, when in reading.family_week_resets_at.items()
        if when <= now and family in reading.family_week_percent
    }
    if over:  # a family week that has reset since: its figure goes the same way
        update["family_week_percent"] = {
            family: percent
            for family, percent in reading.family_week_percent.items()
            if family not in over
        }
    return reading.model_copy(update=update) if update else reading


def _with_last_good(
    accounts: Sequence[ClaudeAccount],
    readings: Mapping[int, ClaudeUsage],
    tokens: Mapping[int, str],
    moment: datetime,
) -> dict[int, ClaudeUsage]:
    """``readings`` in ``accounts``' order, each failed one replaced by the last good reading
    on record for the token the slot holds, marked with the failure (:func:`_preferred`).

    A try that failed — forced, due, gated by a back-off, or served from this process's
    memory of one — keeps the row's figure at whatever age (F1 of #400): the reading is
    shown with how old it is and why the newer try read nothing; ``—`` only when nothing
    was ever read for this token. A failure that is about the token (``needs_login``) is
    shown as it is: the figure belongs to a login that no longer reads.
    """
    shown = dict(readings)
    failed = [
        account
        for account in accounts
        if not readings[account.slot].available
        and not readings[account.slot].needs_login
        and account.slot in tokens
    ]
    if failed:
        recorded = _recorded_usage(
            failed, tokens, since=moment - LAST_GOOD_LOOKBACK, now=moment, current=False
        )
        for account in failed:
            answer = _preferred(recorded.get(account.slot), shown[account.slot])
            if answer is not None and answer.available:
                answer = _without_reset_windows(answer, moment)
            shown[account.slot] = answer or shown[account.slot]
    return {account.slot: shown[account.slot] for account in accounts}


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

    A kept reading (:func:`is_kept`) has no trend: its pace, "≈ N min to the limit", was
    worked out for a moment that can be hours ago (review of #408, n4).
    """
    readings = cached_usage(accounts, max_age=max_age, force=force, now=now, fetch=fetch)
    try:
        with store_session() as store:
            return {
                slot: (
                    reading,
                    None if is_kept(reading) else usage_trend(slot, reading, now=now, store=store),
                )
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
    """``just now`` / ``3m ago`` / ``2h ago`` / ``4d ago``: how old a reading is, for a
    "refreshed …" line. Days from a day on: a kept reading can be a week old, and a full
    account's ``▮▮▮▮▮ 100% · 100h`` did not fit its 16-wide cell (review of #408, n6)."""
    minutes = max(0, int(((now or _now()) - fetched_at).total_seconds() // 60))
    if minutes == 0:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"
    if minutes < 24 * 60:
        return f"{minutes // 60}h ago"
    return f"{minutes // (24 * 60)}d ago"


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
    accounts: Sequence[ClaudeAccount],
    readings: Mapping[int, ClaudeUsage],
    *,
    now: datetime | None = None,
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
    read yet. A kept reading (:func:`is_kept`: the last good figure a failed read
    shows) counts only while it is under :data:`USAGE_MAX_AGE_SECONDS` old at
    ``now``; an older one is not the fleet's room (review of #408, MAJ-1). Its
    windows that have reset are already empty (``_without_reset_windows``).
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
    oldest = (now or _now()) - timedelta(seconds=USAGE_MAX_AGE_SECONDS)
    for account in enabled:
        reading = readings.get(account.slot)
        left = percent_left(reading) if reading is not None else None
        if reading is None or left is None or reading.session_percent is None:
            continue
        if is_kept(reading) and (reading.fetched_at is None or reading.fetched_at < oldest):
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
    summary = fleet_headroom(candidates, readings, now=now)
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
                note=after.note,
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
A hand-over after a limit still reads live (:func:`choose_for_handover`); one before the
limit (``[accounts] before_limit``) reads this deep, as a launch does."""


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

    An account the usage endpoint asked to wait (F1 of #400) is not asked
    again, by a launch or a hand-over: the pick decides by its newest reading
    recorded in the last :data:`USAGE_MAX_AGE_SECONDS`, and says so, or skips
    it with a note naming the wait when that reading is older or missing. The
    cache's older figure, shown on the page with its age, is never picked by.
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
    readings = dict(readings)
    # A kept reading — the last good figure a failed read shows (F1 of #400), or a slot the
    # endpoint asked to wait — is never taken for a live one: the pick decides by it only
    # while it is inside the pick's own window and neither window has reset since, and
    # otherwise skips it with a note that says how old it is (review of #408, MAJ-1).
    # A hand-over reads live, so its failure arrives bare (no kept figure on it): the same
    # rule applies to the reading on record behind it.
    kept = [
        account
        for account in candidates
        if is_kept(readings[account.slot])
        or not (readings[account.slot].available or readings[account.slot].needs_login)
    ]
    if kept:
        moment = now or _now()
        window = timedelta(seconds=max_age if max_age is not None else USAGE_MAX_AGE_SECONDS)
        tokens = {
            account.slot: token
            for account in kept
            if (token := _token_fingerprint(account, moment)) is not None
        }
        on_record = _recorded_usage(
            kept, tokens, since=moment - LAST_GOOD_LOOKBACK, now=moment, current=False
        )
        for account in kept:
            reading = readings[account.slot]
            why = (
                rate_limited_until_text(reading.rate_limited_until)
                if reading.rate_limited
                else reading.last_failed_reason or reading.reason or "the last read failed"
            )
            last = on_record.get(account.slot)
            taken = last.fetched_at if last is not None else None
            if (
                last is not None
                and taken is not None
                and taken >= moment - window
                and not (_has_reset(last, moment))
            ):
                readings[account.slot] = last
                notes.append(
                    f"headroom: {core.label(account)} {why} — picked by its reading from "
                    f"{describe_age(taken, now=moment)}"
                )
                continue
            if taken is None and not is_kept(reading):
                continue  # a bare failure with nothing on record: its own reason says it all
            if taken is None:
                age = "no reading on record to pick by"
            elif last is not None and _has_reset(last, moment):
                age = (
                    f"its last reading is from {describe_age(taken, now=moment)}, "
                    "and a window has reset since"
                )
            else:
                age = (
                    f"its last reading is from {describe_age(taken, now=moment)}, "
                    "too old to pick by"
                )
            readings[account.slot] = ClaudeUsage(
                available=False,
                reason=f"{why}; {age}",
                cause=reading.cause,
                fix=reading.fix,
                rate_limited=reading.rate_limited,
                rate_limited_until=reading.rate_limited_until,
            )
    measured: list[tuple[ClaudeAccount, float]] = []
    for account in candidates:
        reading = readings[account.slot]
        percent = headroom_percent(reading, family)
        own = reading.family_week_percent.get(family) if family is not None else None
        if own is not None and own >= 100:
            # Spent for this agent's family: it refuses the first request, so it is no
            # answer at all, not even the least bad one a switch by hand takes (P0-W3c).
            notes.append(f"headroom: {core.label(account)} skipped — its {family} week is spent")
        elif percent is not None:
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
    max_age: float | None = None,
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

    ``max_age`` reads the candidates through the cache instead, that deep: the move
    ``[accounts] before_limit`` makes (F2 of #400), of an agent that hit no limit, whose
    hook read them a moment ago (:func:`headroom_choice`).
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
        max_age=max_age,
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


# --- before the limit (F2 of #400) ---------------------------------------------------------
#
# ``[accounts] before_limit`` moves (or, ``warn``, names) a fleet agent whose account reads
# over ``switch_at``, at a turn end or an idle notice (``services.hooks.check_before_limit``).
# Nothing here polls, and the hooks fetch nothing: they decide from what is ON RECORD
# (``on_record=True``: the shared cache's readings, five minutes deep, no request), and only
# the detached worker reads through the cache, which may ask the endpoint (the agent's account
# five minutes deep, the accounts it could go to a launch's minute deep), never during a
# back-off. A kept reading (:func:`is_kept`) is no live one, and moves nobody.


@dataclass(frozen=True)
class LineReading:
    """An account's live reading against ``switch_at``, for an agent of ``family``."""

    account: ClaudeAccount
    reading: ClaudeUsage
    used: float
    """Percent used of its fuller window (:func:`headroom_percent`)."""
    over: bool
    """At or over the line: the account counts as full."""
    lifts_at: datetime | None
    """When every window over the line has reset, and the account is under it again; ``None``
    when one of them named no reset, or none is over."""

    def lifts_within(self, minutes: int, *, now: datetime | None = None) -> bool:
        """Whether the account is under the line again within ``minutes``: a wait that is
        cheaper than a cold start elsewhere, as after a limit (``wait_if_reset_within_minutes``)."""
        if self.lifts_at is None:
            return False
        return self.lifts_at - (now or _now()) <= timedelta(minutes=minutes)


def has_passed(when: datetime, *, now: datetime | None = None) -> bool:
    """Whether ``when`` — a window's reset — is past, by the clock readings are taken on."""
    return when <= (now or _now())


def _on_record(
    accounts: Sequence[ClaudeAccount], *, max_age: float, now: datetime | None = None
) -> dict[int, ClaudeUsage]:
    """What :func:`cached_usage` would serve ``accounts`` without a fetch: each one's reading
    recorded in the last ``max_age`` seconds for the token it holds now. Nothing is asked."""
    moment = now or _now()
    tokens = {
        account.slot: token
        for account in accounts
        if (token := _token_fingerprint(account, moment)) is not None
    }
    return _served(accounts, tokens, since=moment - timedelta(seconds=max_age), now=moment)


def line_reading(
    slot: int,
    *,
    switch_at: int,
    family: str | None = None,
    on_record: bool = False,
    fetch: Fetch | None = None,
    now: datetime | None = None,
) -> LineReading | None:
    """Account ``slot`` against ``switch_at`` for an agent of ``family``, by a LIVE reading;
    ``None`` when there is none: the account cannot be read, or the reading is a kept one (the
    last good figure of a failed read, or a back-off's), which moves no agent — and with
    ``on_record``, when nothing is on record. A registry that cannot be read is no answer.

    ``on_record`` reads what the cache holds and asks nothing: a hook's read. Otherwise it is
    read through :func:`cached_usage`, :data:`USAGE_MAX_AGE_SECONDS` deep: the worker's.
    """
    accounts, note = _read_for_handover()
    if accounts is None or note is not None:
        return None
    account = next((a for a in accounts if a.slot == slot), None)
    if account is None or not core.signed_in(account):
        return None
    if on_record:
        reading = _on_record([account], max_age=USAGE_MAX_AGE_SECONDS, now=now).get(slot)
    else:
        reading = cached_usage([account], fetch=fetch, now=now)[slot]
    if reading is None or not reading.available or is_kept(reading):
        return None
    used = headroom_percent(reading, family)
    if used is None:
        return None
    windows = [
        (reading.session_percent, reading.session_resets_at),
        (reading.week_percent, reading.week_resets_at),
    ]
    if family is not None:
        windows.append(
            (reading.family_week_percent.get(family), reading.family_week_resets_at.get(family))
        )
    over = [when for percent, when in windows if percent is not None and percent >= switch_at]
    resets = [when for when in over if when is not None]
    lifts_at = max(resets) if resets and len(resets) == len(over) else None
    return LineReading(account, reading, used, used >= switch_at, lifts_at)


def room_on_record(
    leaving: int, *, switch_at: int, family: str | None = None, now: datetime | None = None
) -> bool:
    """Whether an account other than ``leaving`` may take an agent of ``family``, by what is on
    record alone (nothing is asked): ``False`` only when every candidate the automatic pick
    would weigh (:func:`choose_for_handover`) is known not to — at a limit mark for the family,
    its family week spent, or its live reading on record at or over ``switch_at`` — or the
    registry cannot be read, when that pick takes nothing. A candidate with no live reading on
    record may: only a read can say, and that is the worker's.

    A hook's question (F2 of #400): with every other account over the line, a turn end then
    reads none of them, however long that lasts, where a pick a minute deep re-read each one a
    minute at every turn end.
    """
    accounts, note = _read_for_handover()
    if accounts is None or note is not None:
        return False
    moment = now or _now()
    marks = limit_marks(now=moment)
    candidates = [
        account
        for account in accounts
        if not account.disabled
        and account.slot != leaving
        and core.signed_in(account)
        and not any(mark.blocks(family) for mark in marks.get(account.slot, []))
    ]
    readings = _on_record(candidates, max_age=USAGE_MAX_AGE_SECONDS, now=moment)
    for account in candidates:
        reading = readings.get(account.slot)
        if reading is None or not reading.available or is_kept(reading):
            return True
        own = reading.family_week_percent.get(family) if family is not None else None
        if own is not None and own >= 100:
            continue
        used = headroom_percent(reading, family)
        if used is None or used < switch_at:
            return True
    return False


def account_pin(role: str | None, *, named: bool) -> str | None:
    """Why an agent of ``role`` stays on its account whatever its usage reads, or ``None``.

    ``named`` is its launch's ``--account`` (``LaunchSpec.account_pinned``). Then the role's
    binding to an account (``team bind <role> --account``), and a binding that sets
    ``CLAUDE_CONFIG_DIR`` by hand, which no default overrides (#9). A hand-over after a limit
    still moves each of them; ``[accounts] before_limit``, which acts on none (F2 of #400),
    asks here. A config that cannot be read pins nothing, as it binds nothing.
    """
    if named:
        return "named with --account"
    bound, _note = _role_binding_ref(role)
    if bound:
        return f"bound to account {bound}"
    if _binding_names_a_directory(role):
        return "its binding sets CLAUDE_CONFIG_DIR"
    return None


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
        if not (knobs.share_trust or knobs.share_settings or knobs.share):
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
        notes += _share_parts_into(target)
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


# --- [accounts] share: memory, skills, agents, commands, CLAUDE.md, plugins (P2-W7) -------


@dataclass(frozen=True)
class PartChange:
    """What ``accounts share`` did to one part on one account, or what it holds there."""

    account: ClaudeAccount
    part: sharing.SharePart
    outcome: Literal[
        "linked",
        "already",
        "unlinked",
        "restored",
        "refused",
        "failed",
        "shared",
        "own",
        "none",
        "elsewhere",
        "merged",
    ]
    detail: str | None = None
    """The kept copy's name, the reason a part was refused, or what a memory merge noted."""

    def as_json(self) -> dict[str, object]:
        return {
            "slot": self.account.slot,
            "account": core.label(self.account),
            "part": self.part,
            "outcome": self.outcome,
            "detail": self.detail,
        }


def shared_memory_root() -> Path:
    """Where ``memory`` keeps each project's auto memory: under the aisquare home, outside
    every ``.claude`` path on purpose (anthropics/claude-code#98044: since 2.1.280 every
    auto-memory write prompts when it lands under a linked ``.claude`` directory)."""
    return paths.aisquare_home() / "claude-shared" / "memory"


def memory_settings_args(cwd: Path) -> list[str]:
    """``--settings`` pointing Claude Code's auto memory at the shared directory for the
    project in ``cwd``, when ``[accounts] share`` names ``memory``; else nothing.

    ``autoMemoryDirectory`` is read from any settings scope, ``--settings`` included
    (Claude Code, Memory). Every launch gets it, the plain claude's too, so all of them
    read and write one directory per project."""
    try:
        if "memory" not in accounts_settings().share:
            return []
    except Exception:  # an unreadable config shares nothing
        return []
    directory = shared_memory_root() / sharing.memory_key(cwd)
    return ["--settings", json.dumps({"autoMemoryDirectory": str(directory)})]


def share_status() -> list[PartChange]:
    """What every account holds for every part: the linked parts per managed slot, and
    ``memory`` as on (``shared``) or off (``none``) for the machine."""
    knobs = accounts_settings()
    source = core.default_account().config_dir
    held: list[PartChange] = []
    for account in list_accounts():
        for part in sharing.SHARE_PARTS:
            if part == "memory":
                held.append(
                    PartChange(account, part, "shared" if "memory" in knobs.share else "none")
                )
            elif account.managed:
                held.append(
                    PartChange(account, part, sharing.part_state(account.config_dir, part, source))
                )
    return held


def enable_share(
    parts: Sequence[sharing.SharePart], *, now: datetime | None = None
) -> list[PartChange]:
    """Turn ``parts`` on: write them into ``[accounts] share`` and apply them now.

    Linked parts link every managed slot's entry (its own kept beside it); one linked
    anywhere else is refused and left as it is. ``memory`` merges every account's
    ``projects/<key>/memory/`` into the shared directory the first time it is turned on
    (nothing overwritten; a file that differs is kept as ``<name>.slot<N>.md``).
    Idempotent: a part already on changes nothing.
    """
    config = load_config()
    first_memory = "memory" in parts and "memory" not in config.accounts.share
    config.accounts.share = list(dict.fromkeys([*config.accounts.share, *parts]))
    save_config(config)
    moment = now or _now()
    source = core.default_account().config_dir
    changes: list[PartChange] = []
    for part in parts:
        if part == "memory":
            changes += _merge_memory() if first_memory else []
            continue
        for account in list_accounts():
            if account.managed:
                changes.append(_link_part(account, part, source, moment))
    return changes


def disable_share(parts: Sequence[sharing.SharePart]) -> list[PartChange]:
    """Turn ``parts`` off: out of ``[accounts] share``, and every linked entry back to the
    slot's own copy (the one :func:`enable_share` kept), exactly as it was. ``memory``
    stops being passed to launches; the shared directory stays where it is."""
    config = load_config()
    config.accounts.share = [part for part in config.accounts.share if part not in parts]
    save_config(config)
    source = core.default_account().config_dir
    changes: list[PartChange] = []
    for part in parts:
        if part == "memory":
            continue
        for account in list_accounts():
            if not account.managed:
                continue
            was = sharing.part_state(account.config_dir, part, source)
            if was != "shared":  # nothing of ours to undo: say what it holds
                changes.append(PartChange(account, part, was))
                continue
            try:
                restored = sharing.unlink_part(account.config_dir, part, source)
            except OSError as exc:
                changes.append(PartChange(account, part, "failed", exc.strerror or str(exc)))
                continue
            if restored is not None:
                changes.append(PartChange(account, part, "restored", restored.name))
            else:
                changes.append(PartChange(account, part, "unlinked"))
    return changes


def _link_part(
    account: ClaudeAccount, part: sharing.SharePart, source: Path, now: datetime
) -> PartChange:
    if sharing.part_state(account.config_dir, part, source) == "shared":
        return PartChange(account, part, "already")
    try:
        kept = sharing.link_part(account.config_dir, part, source, now=now)
    except sharing.SharingError as exc:
        return PartChange(account, part, "refused", str(exc))
    except OSError as exc:
        return PartChange(account, part, "failed", exc.strerror or str(exc))
    return PartChange(account, part, "linked", kept.name if kept is not None else None)


def _merge_memory() -> list[PartChange]:
    """Every account's per-project memory into the shared root, once (:func:`enable_share`)."""
    root = shared_memory_root()
    changes: list[PartChange] = []
    accounts = list_accounts()
    keys = sorted(
        {
            memory.parent.name
            for account in accounts
            for memory in (account.config_dir / "projects").glob("*/memory")
            if memory.is_dir()
        }
    )
    for key in keys:
        sources = [
            (f"slot{account.slot}", account.config_dir / "projects" / key / "memory")
            for account in accounts
        ]
        by_label = {f"slot{account.slot}": account for account in accounts}
        for label, note in sharing.merge_memory(sources, root / key):
            changes.append(PartChange(by_label[label], "memory", "merged", f"{key}: {note}"))
    return changes


def _share_parts_into(target: ClaudeAccount) -> list[str]:
    """``[accounts] share`` at a launch: link the parts ``target`` does not have yet (a slot
    made after they were turned on), saying what changed or why it could not."""
    knobs = accounts_settings()
    linked = [part for part in knobs.share if part in sharing.LINKED_PARTS]
    if not linked or not target.managed:
        return []
    source = core.default_account().config_dir
    notes: list[str] = []
    for part in linked:
        change = _link_part(target, part, source, _now())
        name = core.label(target)
        if change.outcome == "linked":
            kept = f" (its own kept as {change.detail})" if change.detail else ""
            notes.append(SharedNote(f"{name} now shares the plain claude's {part}{kept}"))
        elif change.outcome in ("refused", "failed"):
            notes.append(f"{part} is not shared into {name}: {change.detail}")
    return notes
