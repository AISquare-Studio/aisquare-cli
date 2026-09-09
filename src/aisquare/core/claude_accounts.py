"""Claude Code accounts the CLI owns: one config directory per numbered slot.

Claude Code keeps a login inside the directory ``CLAUDE_CONFIG_DIR`` names
(``~/.claude`` when the variable is unset), so a second account is a second
directory and a launch that points the variable at it. Operators have run that
by hand as shell aliases (``alias c2='CLAUDE_CONFIG_DIR=~/.claude-c2 claude'``)
for as long as parallel installs have existed; this module is the CLI owning
the same mechanism, so that ``asq`` can add, list, launch and remove accounts
without anyone writing an alias.

Layout (``paths.claude_accounts_dir`` / ``paths.claude_accounts_tmp_dir``)::

    ~/.aisquare/claude-accounts/<n>/                    CLAUDE_CONFIG_DIR of slot n (n ≥ 2)
    ~/.aisquare/claude-accounts/<n>/.aisquare-account.json   the marker: slot, created_at
    ~/.aisquare/claude-accounts/<n>.removed-<stamp>/    a removed slot, kept but never listed
    ~/.aisquare/cache/claude-accounts/<n>/              CLAUDE_CODE_TMPDIR of slot n

**Slot 1 is never a directory of ours.** It is whatever a plain ``claude`` in
this shell is — ``CLAUDE_CONFIG_DIR`` when the environment sets it,
``~/.claude`` otherwise — and launching it changes nothing about the
environment. Pointing ``CLAUDE_CONFIG_DIR`` at ``~/.claude`` would NOT be a
no-op: Claude Code keeps the default install's ``.claude.json`` at
``~/.claude.json``, beside the directory, and moves it inside only when the
variable is set — so a "default" launched that way re-onboards into an empty
config and diverges from the one the user has.

**Both variables, always.** ``CLAUDE_CODE_TMPDIR`` goes with ``CLAUDE_CONFIG_DIR``
because the config dir alone leaves the account on the shared scratch
directory, where two parallel sessions collide (README, "Several accounts, one
team").

**The directories are the record; there is no registry file.** A slot is a
directory named by its number that carries our marker; the account's identity
is read from the files Claude Code itself writes there. A registry would be a
second copy of facts the filesystem already holds, and the two would drift the
first time someone moved a directory by hand.

**Read-only towards Claude Code's own files.** ``.claude.json`` (email,
organisation, ids) and ``.credentials.json`` (the OAuth tokens) are read to
describe an account; nothing here writes either. Claude Code refreshes its own
tokens — a token found expired is reported as such, never refreshed, because a
second writer racing the agent for that file is how a login gets corrupted.

This is not the "accounts" cut that ``core.config.RoleLaunchProfile`` records
deleting. That one expanded a bare name into the OPERATOR's directory layout —
a convention the tool had no business knowing. These directories are the
tool's own, created here and nowhere else; a role bound with ``team bind
--env`` to some other layout keeps working untouched.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aisquare.core import paths
from aisquare.core.version import __version__
from aisquare.models import ClaudeAccount, ClaudeIdentity

DEFAULT_SLOT = 1
"""The slot that is the machine's plain ``claude`` — never a directory of ours."""

FIRST_MANAGED_SLOT = 2

MARKER = ".aisquare-account.json"
"""Written into every directory this module creates; a numbered directory without it is a stray."""

CONFIG_DIR_VAR = "CLAUDE_CONFIG_DIR"
TMPDIR_VAR = "CLAUDE_CODE_TMPDIR"

INSTALL_COMMAND = "curl -fsSL https://claude.ai/install.sh | bash"
"""The native installer — the layout ``services.diagnostics.claude_code_version`` reads first."""
INSTALL_ALTERNATIVE = "npm install -g @anthropic-ai/claude-code"

_SLOT_DIR = re.compile(r"^\d+$")
_REMOVED_STAMP = "%Y%m%dT%H%M%SZ"


def _home() -> Path:
    """The user's home directory (indirection so tests can redirect it)."""
    return Path.home()


def _now() -> datetime:
    return datetime.now(tz=UTC)


# --- where things are -------------------------------------------------------------


def accounts_root() -> Path:
    return paths.claude_accounts_dir()


def tmp_root() -> Path:
    return paths.claude_accounts_tmp_dir()


def default_config_dir() -> Path:
    """What a plain ``claude`` from this environment uses: the variable, else ``~/.claude``."""
    env = os.environ.get(CONFIG_DIR_VAR, "").strip()
    if env:
        return Path(env).expanduser()
    return _home() / ".claude"


def default_account() -> ClaudeAccount:
    return ClaudeAccount(slot=DEFAULT_SLOT, config_dir=default_config_dir(), managed=False)


def _managed(slot: int) -> ClaudeAccount:
    return ClaudeAccount(
        slot=slot,
        config_dir=accounts_root() / str(slot),
        tmp_dir=tmp_root() / str(slot),
        managed=True,
    )


def managed_accounts() -> list[ClaudeAccount]:
    """Every slot the CLI created, lowest first. Removed slots (renamed) are not slots."""
    root = accounts_root()
    if not root.is_dir():
        return []
    found: list[ClaudeAccount] = []
    for child in root.iterdir():
        if not child.is_dir() or not _SLOT_DIR.match(child.name):
            continue
        slot = int(child.name)
        if slot < FIRST_MANAGED_SLOT or not (child / MARKER).is_file():
            continue
        found.append(_managed(slot))
    return sorted(found, key=lambda account: account.slot)


def list_accounts() -> list[ClaudeAccount]:
    """The default first, then every managed slot in order."""
    return [default_account(), *managed_accounts()]


def find_account(slot: int) -> ClaudeAccount | None:
    return next((account for account in list_accounts() if account.slot == slot), None)


def managed_slot(config_dir: Path | str) -> int | None:
    """The slot number when ``config_dir`` is one of ours, else ``None``."""
    path = Path(config_dir)
    try:
        relative = path.resolve().relative_to(accounts_root().resolve())
    except (ValueError, OSError):
        return None
    parts = relative.parts
    if len(parts) != 1 or not _SLOT_DIR.match(parts[0]):
        return None
    slot = int(parts[0])
    return slot if slot >= FIRST_MANAGED_SLOT else None


def label(account: ClaudeAccount) -> str:
    """``default`` for slot 1, ``account N`` otherwise — what the board and the UI call it."""
    return "default" if account.slot == DEFAULT_SLOT else f"account {account.slot}"


# --- creating and removing ----------------------------------------------------------


def next_free_slot() -> int:
    """The lowest number from :data:`FIRST_MANAGED_SLOT` up that no current slot uses.

    A removed slot's directory was renamed, so its number is free again; the
    next account added takes the lowest gap rather than growing forever.
    """
    taken = {account.slot for account in managed_accounts()}
    slot = FIRST_MANAGED_SLOT
    while slot in taken or (accounts_root() / str(slot)).exists():
        slot += 1
    return slot


def create_account() -> ClaudeAccount:
    """Make the next slot's two directories (0700) and its marker; nothing is signed in yet."""
    account = _managed(next_free_slot())
    paths.ensure_home()
    account.config_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    if account.tmp_dir is not None:
        account.tmp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = {
        "slot": account.slot,
        "created_at": _now().isoformat(),
        "created_by": f"aisquare-cli {__version__}",
    }
    (account.config_dir / MARKER).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    return account


def remove_account(account: ClaudeAccount) -> Path:
    """Retire a managed slot: its directory is RENAMED beside itself, its scratch dir deleted.

    ``<n>.removed-<UTC stamp>`` keeps the login, the settings and the transcripts
    exactly as they were, so a removal is reversible by hand (move it back) and
    costs nothing if it was a mistake; ``aisquare accounts`` never lists it and
    the number is free for the next add. The scratch directory is cache and
    goes. Refuses the default slot, which was never ours to move.
    """
    if not account.managed:
        raise ValueError("the default account is not a directory of ours to remove")
    stamp = _now().strftime(_REMOVED_STAMP)
    target = account.config_dir.with_name(f"{account.slot}.removed-{stamp}")
    counter = 1
    while target.exists():
        counter += 1
        target = account.config_dir.with_name(f"{account.slot}.removed-{stamp}-{counter}")
    account.config_dir.rename(target)
    if account.tmp_dir is not None:
        shutil.rmtree(account.tmp_dir, ignore_errors=True)
    return target


def discard_account(account: ClaudeAccount) -> None:
    """Delete a managed slot that never signed in — an abandoned add leaves no trace."""
    if not account.managed:
        raise ValueError("the default account is not a directory of ours to discard")
    shutil.rmtree(account.config_dir, ignore_errors=True)
    if account.tmp_dir is not None:
        shutil.rmtree(account.tmp_dir, ignore_errors=True)


# --- what Claude Code wrote there ---------------------------------------------------


def claude_json_path(account: ClaudeAccount) -> Path:
    """Where this account's ``.claude.json`` is.

    Inside the directory whenever ``CLAUDE_CONFIG_DIR`` names it — every managed
    slot, and a default that the environment redirects — and at ``~/.claude.json``
    for the plain default, which is where Claude Code keeps it.
    """
    if account.managed or os.environ.get(CONFIG_DIR_VAR, "").strip():
        return account.config_dir / ".claude.json"
    return _home() / ".claude.json"


def credentials_path(account: ClaudeAccount) -> Path:
    return account.config_dir / ".credentials.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def identity(account: ClaudeAccount) -> ClaudeIdentity | None:
    """The signed-in account, or ``None`` when the directory has no login recorded."""
    data = _read_json(claude_json_path(account))
    if data is None:
        return None
    oauth = data.get("oauthAccount")
    if not isinstance(oauth, dict):
        return None
    email = oauth.get("emailAddress")
    if not isinstance(email, str) or not email:
        return None
    organization = oauth.get("organizationName")
    uuid = oauth.get("accountUuid")
    return ClaudeIdentity(
        email=email,
        organization=organization if isinstance(organization, str) and organization else None,
        account_uuid=uuid if isinstance(uuid, str) and uuid else None,
    )


@dataclass(frozen=True)
class ClaudeCredentials:
    """The OAuth material Claude Code stores — the token never leaves this process.

    Deliberately not a pydantic model and not in ``models``: those are dumped
    under ``--json`` and logged, and this carries a bearer token. ``repr`` shows
    everything but the token.
    """

    access_token: str
    expires_at: datetime | None
    subscription_type: str | None
    rate_limit_tier: str | None

    def __repr__(self) -> str:
        return (
            f"ClaudeCredentials(access_token=<redacted>, expires_at={self.expires_at!r}, "
            f"subscription_type={self.subscription_type!r}, "
            f"rate_limit_tier={self.rate_limit_tier!r})"
        )

    def expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or _now()) >= self.expires_at


def credentials(account: ClaudeAccount) -> ClaudeCredentials | None:
    """The stored OAuth credentials, or ``None`` when the file is absent or not ours to read.

    On Linux Claude Code writes ``.credentials.json`` (0600) into the config dir;
    on macOS it uses the Keychain and the file does not exist — see
    :func:`keychain_platform`.
    """
    data = _read_json(credentials_path(account))
    if data is None:
        return None
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token:
        return None
    raw_expiry = oauth.get("expiresAt")
    expires_at: datetime | None = None
    if isinstance(raw_expiry, int | float) and raw_expiry > 0:
        # Milliseconds since the epoch, as Claude Code writes it.
        expires_at = datetime.fromtimestamp(raw_expiry / 1000, tz=UTC)
    subscription = oauth.get("subscriptionType")
    tier = oauth.get("rateLimitTier")
    return ClaudeCredentials(
        access_token=token,
        expires_at=expires_at,
        subscription_type=subscription if isinstance(subscription, str) else None,
        rate_limit_tier=tier if isinstance(tier, str) else None,
    )


def keychain_platform() -> bool:
    """Whether this platform keeps Claude Code's credentials out of the config dir (macOS)."""
    return sys.platform == "darwin"


def token_state(creds: ClaudeCredentials | None, now: datetime | None = None) -> str:
    if creds is None:
        return "missing"
    return "expired" if creds.expired(now) else "ok"


def signed_in(account: ClaudeAccount) -> bool:
    """A login is recorded AND its credentials are where this platform keeps them.

    Identity alone is not enough on Linux: ``/logout`` deletes the credentials
    file, and a stale ``oauthAccount`` left behind would read as a working
    login that Claude Code itself would refuse. On macOS the credentials are in
    the Keychain, which is not ours to read, so the identity is the answer.
    """
    if identity(account) is None:
        return False
    return keychain_platform() or credentials(account) is not None


def subscription_label(creds: ClaudeCredentials | None) -> str | None:
    """``max 5x`` from ``default_claude_max_5x``; the plain subscription type otherwise."""
    if creds is None:
        return None
    tier = creds.rate_limit_tier or ""
    match = re.search(r"claude_([a-z]+)_(\d+x)$", tier)
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return creds.subscription_type


# --- launching ------------------------------------------------------------------------


def launch_env(account: ClaudeAccount) -> dict[str, str]:
    """The two variables that make a launch run under ``account`` — none for the default.

    The default slot is "whatever this shell's ``claude`` is", so it sets
    nothing: see the module docstring for why ``CLAUDE_CONFIG_DIR=~/.claude``
    would not be the same thing.
    """
    if not account.managed:
        return {}
    env = {CONFIG_DIR_VAR: str(account.config_dir)}
    if account.tmp_dir is not None:
        env[TMPDIR_VAR] = str(account.tmp_dir)
    return env
