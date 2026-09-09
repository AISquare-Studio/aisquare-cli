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
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aisquare.core import claude_accounts as core
from aisquare.core import paths
from aisquare.core.spawn import untraced_env
from aisquare.core.tmux import TmuxServer, WindowInfo
from aisquare.core.version import __version__
from aisquare.models import (
    AccountsOverview,
    ClaudeAccount,
    ClaudeAccountStatus,
    ClaudeIdentity,
    ClaudeInstall,
    ClaudeUsage,
)
from aisquare.services import agents as agents_service

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
    """The install plus every slot, offline. Usage is a separate, slower question."""
    return AccountsOverview(
        claude=install(), accounts=[describe(account) for account in core.list_accounts()]
    )


def resolve(ref: str | int) -> ClaudeAccount:
    """A slot by number, or by the email it is signed in as."""
    text = str(ref).strip()
    if text.isdigit():
        account = core.find_account(int(text))
        if account is None:
            raise NoSuchAccount(f"no Claude account in slot {text} — see: aisquare accounts")
        return account
    wanted = text.lower()
    for account in core.list_accounts():
        identity = core.identity(account)
        if identity is not None and identity.email.lower() == wanted:
            return account
    raise NoSuchAccount(f"no Claude account is signed in as {text!r} — see: aisquare accounts")


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
    return core.remove_account(account)


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
    orphaned and the slot half-made. SIGINT is ignored here for the child's
    lifetime (main thread only — a signal handler cannot be set elsewhere) and
    the previous disposition is restored afterwards.
    """
    found = install()
    if not found.installed or found.binary is None:
        raise ClaudeNotInstalled(
            f"Claude Code is not installed — install it first: {core.INSTALL_COMMAND}"
        )
    previous: Any = None
    on_main_thread = threading.current_thread() is threading.main_thread()
    if on_main_thread:
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        completed = subprocess.run(  # argv, never a shell
            [found.binary, *args], env=session_env(account, env), check=False
        )
    finally:
        if on_main_thread:
            signal.signal(signal.SIGINT, previous)
    return int(completed.returncode)
