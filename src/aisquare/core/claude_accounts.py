"""Claude Code accounts the CLI owns: one config directory per numbered slot.

Claude Code keeps a login inside the directory ``CLAUDE_CONFIG_DIR`` names
(``~/.claude`` when the variable is unset), so a second account is a second
directory and a launch that points the variable at it. Operators have run that
by hand as shell aliases (``alias c2='CLAUDE_CONFIG_DIR=~/.claude-c2 claude'``)
for as long as parallel installs have existed; this module is the CLI owning
the same mechanism, so that ``asq`` can add, list, launch and remove accounts
without anyone writing an alias.

Layout (:func:`accounts_root` / :func:`tmp_root`, each under the accounts' ``dir_name``)::

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

**Under one of our slots, slot 1 is still the launching shell's.** An agent
launched on slot 2 runs with both variables naming slot 2, and so does every
hook it fires, the hand-over worker it detaches and any ``fleet spawn`` it
runs. There the variables are the slot's, not the plain claude's: a launch
onto a managed slot keeps the launching shell's own two beside them
(:data:`PLAIN_VARS`), and :func:`plain_environment` reads slot 1 from those.

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
organisation, ids) and ``.credentials.json`` (the OAuth tokens; on macOS the
login Keychain item Claude Code keeps them in, :func:`read_keychain`) are read to
describe an account; nothing here writes either. Claude Code refreshes its own
tokens — a token found expired is reported as such, never refreshed, because a
second writer racing the agent for that file is how a login gets corrupted. The
one write into ``.claude.json`` is ``core.claude_sharing``'s: a folder's trust,
copied from another account under Claude Code's own lock, and nothing else.

**Codex logins are slots too.** Every agent whose adapter has an accounts
slot (``core.agent_adapters.types.AgentAccounts``) keeps its managed homes the
same way, under its own directory (``~/.aisquare/codex-accounts/<n>``, whose
marker also names ``"agent": "codex"``), and the per-agent facts — the
variables, the plain home, the identity, the usage — are the adapter's. ONE
LADDER numbers the managed slots of every agent: a new slot takes the lowest
number no agent's directory uses, so a number names one account whatever the
agent, and ``--account 4`` can never mean two logins. Each agent's plain home
has a reserved number of its own: slot 1 is the plain ``claude``, slot 0 the
plain ``codex`` (listed once its home exists — Codex makes it on first run).
Claude Code's numbers, directories and variables are exactly what they were.

This is not the "accounts" cut that ``core.config.RoleLaunchProfile`` records
deleting. That one expanded a bare name into the OPERATOR's directory layout —
a convention the tool had no business knowing. These directories are the
tool's own, created here and nowhere else; a role bound with ``team bind
--env`` to some other layout keeps working untouched.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aisquare.core import claude_sharing, paths
from aisquare.core.harness import MODEL_FAMILIES, model_family
from aisquare.core.names import name_flaw, name_key
from aisquare.core.version import __version__
from aisquare.models import ClaudeAccount, ClaudeIdentity, ClaudeUsage, session_and_week

if TYPE_CHECKING:
    from aisquare.core.agent_adapters.types import AgentAccounts, UsageFetch

AGENT = "claude-code"
"""The adapter id of Claude Code, whose slots this module has always kept."""

DEFAULT_SLOT = 1
"""The slot that is the machine's plain ``claude`` — never a directory of ours."""

FIRST_MANAGED_SLOT = 2

MARKER = ".aisquare-account.json"
"""Written into every directory this module creates; a numbered directory without it is a stray."""

CONFIG_DIR_VAR = "CLAUDE_CONFIG_DIR"
TMPDIR_VAR = "CLAUDE_CODE_TMPDIR"
HOME_DIR = ".claude"
"""The config dir a plain ``claude`` uses under the user's home, ``CLAUDE_CONFIG_DIR`` unset."""
CLAUDE_JSON = ".claude.json"
"""Its account file: the login it records, its folder trust (``core.claude_sharing``)."""
CREDENTIALS = ".credentials.json"
"""Where it keeps the login's tokens off macOS's Keychain."""

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


# --- which agents keep slots ----------------------------------------------------------


def kind(agent: str) -> AgentAccounts:
    """The adapter's accounts slot for ``agent``; ``ValueError`` when it has none.

    Imported lazily: the adapters import this module for Claude Code's own.
    """
    from aisquare.core.agent_adapters import get_adapter

    accounts = get_adapter(agent).accounts
    if accounts is None:
        raise ValueError(f"{get_adapter(agent).label} has no account slots")
    return accounts


def account_agents() -> tuple[str, ...]:
    """Every agent with account slots, in registration order (Claude Code first)."""
    from aisquare.core.agent_adapters import adapters

    return tuple(adapter.id for adapter in adapters() if adapter.accounts is not None)


def agent_label(agent: str) -> str:
    """``Claude Code`` / ``Codex`` — the adapter's own name; the id for an unknown one."""
    from aisquare.core.agent_adapters import get_adapter

    try:
        return get_adapter(agent).label
    except ValueError:
        return agent


# --- where things are -------------------------------------------------------------


def accounts_root(agent: str = AGENT) -> Path:
    """Where ``agent``'s slots are: its accounts' ``dir_name`` under the aisquare home
    (``claude-accounts``, ``codex-accounts``)."""
    return paths.agent_accounts_dir(kind(agent).dir_name)


def tmp_root(agent: str = AGENT) -> Path:
    """Where the scratch dirs of ``agent``'s slots are, one per slot, for its accounts'
    ``extra_env`` (Claude Code's ``CLAUDE_CODE_TMPDIR``): disposable, under the cache."""
    return paths.cache_dir() / kind(agent).dir_name


def default_config_dir(env: Mapping[str, str] | None = None) -> Path:
    """What a plain ``claude`` from this environment uses: the variable, else ``~/.claude``.

    The variable as :func:`plain_environment` reads it (from ``env``, this process's
    when ``None``), so a process running under one of our slots answers the launching
    shell's directory, not the slot's.
    """
    value = plain_environment(env).get(CONFIG_DIR_VAR, "").strip()
    if value:
        return paths.expand_user(Path(value))  # a `~olduser/…` reads as written (sweep of #257)
    return home_config_dir()


def home_config_dir() -> Path:
    """``~/.claude``: the directory a plain ``claude`` uses with ``CLAUDE_CONFIG_DIR`` unset."""
    return _home() / HOME_DIR


def default_account(env: Mapping[str, str] | None = None, *, agent: str = AGENT) -> ClaudeAccount:
    """``agent``'s plain slot: whatever its binary uses in the shell (:func:`plain_home`)."""
    accounts = kind(agent)
    return ClaudeAccount(
        slot=accounts.plain_slot, config_dir=plain_home(agent, env), managed=False, agent=agent
    )


def plain_home(agent: str, env: Mapping[str, str] | None = None) -> Path:
    """The home a plain run of ``agent``'s binary from this environment uses.

    Read as :func:`default_config_dir` reads Claude Code's: from the shell's OWN
    variables (:func:`plain_environment`), so a process under one of our slots
    answers the launching shell's home, not the slot's.
    """
    return kind(agent).plain_home(_home(), plain_environment(env, agent=agent))


def launched_home(agent: str, env: Mapping[str, str]) -> Path:
    """The home ``agent``'s binary started with ``env`` itself reads: its variable there
    (``CODEX_HOME``), else its default under the user's home. Not :func:`plain_home`, which
    reads the shell's own variables back from under a slot."""
    accounts = kind(agent)
    return accounts.plain_home(_home(), {accounts.home_env: env.get(accounts.home_env, "")})


def account_in_home(
    agent: str, env: Mapping[str, str], accounts: Sequence[ClaudeAccount]
) -> ClaudeAccount | None:
    """The one of ``accounts`` whose home ``agent``'s binary started with ``env`` reads
    (:func:`launched_home`): ``AgentAccounts.launched_account`` for an agent whose home is
    all its login and trust are kept in (Codex's)."""
    home = launched_home(agent, env)
    return next((one for one in accounts if _same_file(one.config_dir, home)), None)


def _same_file(a: Path, b: Path) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def _managed(slot: int, agent: str = AGENT) -> ClaudeAccount:
    """Slot ``slot`` of ``agent``'s, with a scratch dir where its accounts keep variables of
    their own beside the home (``extra_env``; Codex keeps its temporary files in its home)."""
    return ClaudeAccount(
        slot=slot,
        config_dir=accounts_root(agent) / str(slot),
        tmp_dir=tmp_root(agent) / str(slot) if kind(agent).extra_env else None,
        managed=True,
        agent=agent,
    )


def managed_accounts(agent: str = AGENT) -> list[ClaudeAccount]:
    """Every slot the CLI made for ``agent``, lowest first. Removed slots (renamed) are not slots.

    Asked with ``os.path.isdir`` and ``os.path.isfile``, which never raise: ``Path``'s
    raised PermissionError on 3.11 to 3.13 for a slot linked into a folder this user
    cannot enter, or one it may list but not enter, and every reader of the slots ended
    in a traceback (uninstall's plan among them). Such a slot is not one of these.
    """
    root = accounts_root(agent)
    if not os.path.isdir(root):
        return []
    found: list[ClaudeAccount] = []
    for child in root.iterdir():
        if not _SLOT_DIR.match(child.name) or not os.path.isdir(child):
            continue
        slot = int(child.name)
        if slot < FIRST_MANAGED_SLOT or not os.path.isfile(child / MARKER):
            continue
        found.append(_managed(slot, agent))
    return sorted(found, key=lambda account: account.slot)


def all_managed_accounts() -> list[ClaudeAccount]:
    """Every agent's managed slots, Claude Code's first, each agent's lowest first."""
    return [account for agent in account_agents() for account in managed_accounts(agent)]


def list_accounts() -> list[ClaudeAccount]:
    """Each agent's plain slot, then every managed slot of its in order, Claude Code's first.

    An agent's plain home is listed only once it exists: Codex makes ``~/.codex`` on its
    first run, and a machine that never ran it has no Codex login to show. Claude Code's
    plain slot is listed always, as it always was (``AgentAccounts.legacy_slots``).
    """
    found: list[ClaudeAccount] = []
    for agent in account_agents():
        plain = default_account(agent=agent)
        if kind(agent).legacy_slots or os.path.isdir(plain.config_dir):
            found.append(plain)
        found.extend(managed_accounts(agent))
    return found


def find_account(slot: int) -> ClaudeAccount | None:
    return next((account for account in list_accounts() if account.slot == slot), None)


def managed_slot(config_dir: Path | str, agent: str | None = AGENT) -> int | None:
    """The slot number when ``config_dir`` is one of ``agent``'s (any agent's for ``None``):
    a slot's directory, or the config dir its agent reads there (:func:`agent_config_dir`)."""
    path = Path(config_dir)
    for each in account_agents() if agent is None else (agent,):
        try:
            root = accounts_root(each).resolve()
            relative = path.resolve().relative_to(root)
        except (ValueError, OSError, RuntimeError):  # RuntimeError: a symlink loop, on 3.11/3.12
            continue
        parts = relative.parts
        if not parts or not _SLOT_DIR.match(parts[0]):
            continue
        if len(parts) > 1 and kind(each).config_dir(root / parts[0]) != root.joinpath(*parts):
            continue
        slot = int(parts[0])
        if slot >= FIRST_MANAGED_SLOT:
            return slot
    return None


def agent_config_dir(account: ClaudeAccount) -> Path:
    """The config dir ``account``'s agent reads in its home (``AgentAccounts.config_dir``):
    where its hooks, settings and login are. ``account.config_dir`` is the home the launch
    variable names, which is the config dir itself for Claude Code and Codex."""
    return kind(account.agent).config_dir(account.config_dir)


def label(account: ClaudeAccount) -> str:
    """What the board, the UI and a launch line call the slot.

    The alias when the operator gave it one; else ``plain claude`` for slot 1
    and ``account N`` for a managed slot. Slot 1 was called ``default`` until
    #145 gave "default" a meaning of its own — the account a launch picks when
    nothing more specific says — and a slot that is NOT the default could not
    keep wearing the word. "Plain claude" is what it is: whatever ``claude``
    already is in the shell ``asq`` was started from.
    """
    if account.alias:
        return account.alias
    plain = kind(account.agent)
    return plain.plain_label if account.slot == plain.plain_slot else f"account {account.slot}"


def default_label(slot: int) -> str:
    """What slot ``slot`` is called while it has no alias (:func:`label`)."""
    return "plain claude" if slot == DEFAULT_SLOT else f"account {slot}"


def slot_name(slot: int) -> str:
    """The built-in name of a slot known only by its number: what :func:`label` says unaliased.

    ``plain claude`` for 1, an agent's plain name for its reserved number
    (``plain codex`` for 0), ``account N`` otherwise — for the surfaces that hold
    a number and no account (``fleet ls``, the board's account chip).
    """
    if slot == DEFAULT_SLOT:
        return default_label(slot)
    for agent in account_agents():
        accounts = kind(agent)
        if slot == accounts.plain_slot:
            return accounts.plain_label
    return default_label(slot)


def slot_number(text: str) -> int | None:
    """The slot ``text`` names by its number, ``None`` when it names none that way: digits, or a
    minus and digits. Claude Code's plain slot is 1, Codex's 0 and the managed ladder runs from
    :data:`FIRST_MANAGED_SLOT` up, so an agent registered after them has a negative plain slot
    (``AgentAccounts.plain_slot``): ``--account -2`` names it, as ``--account 3`` slot 3."""
    stripped = text.strip()
    return int(stripped) if stripped.removeprefix("-").isdecimal() else None


def _labelled_slot(name: str) -> int | None:
    """The slot whose :func:`default_label` ``name`` reads as, whatever its case, spacing and
    variation selectors (:func:`core.names.name_key`): ``account 3`` U+FE00 prints as slot 3's.

    Only a label a slot prints: ``account 1`` is no slot's (slot 1 is ``plain claude``), nor
    is ``account 03`` (slot 3 is ``account 3``). Each was refused as that slot's name, which
    it is not (review of #259, round 21)."""
    words = name_key(name).split()
    if words == ["plain", "claude"]:
        return DEFAULT_SLOT
    if len(words) == 2 and words[0] == "account" and words[1].isdecimal():
        slot = int(words[1])
        if slot >= FIRST_MANAGED_SLOT and words == name_key(default_label(slot)).split():
            return slot
    return None


ALIAS_MAX = 64
"""The longest alias, in characters, after trimming."""
ALIAS_HINT = "any name up to 64 characters, not only digits, no @"
"""``normalise_alias``'s rules in words: ``accounts alias --help`` and asq's name field."""


def normalise_alias(raw: str, *, slot: int | None = None) -> str:
    """``slot``'s alias as stored — trimmed, its case kept — or ``ValueError`` saying why not.

    Free text, because a name is the operator's to choose ("anything I want"):
    ``Work laptop`` is as good as ``work``. Two rules keep it from shadowing the
    other spellings ``--account`` takes, since a reference that could be read
    two ways is a launch that could land on two accounts: it is never all
    digits (a slot number) and has no ``@`` (an email). Nor is it ANOTHER slot's
    :func:`default_label` — ``account 3`` on slot 2, ``Plain Claude`` on any slot
    but 1 — which the board, a headroom note and ``fleet switch`` print for that
    slot, so two accounts would go by one name and a manager's ``--to "account
    3"`` would land on slot 2. Whether that slot exists yet or not: a removed
    slot's number is the next one ``accounts add`` gives out. Without ``slot``,
    no slot's label is taken. Nothing :func:`core.names.name_flaw` refuses in a
    project's nickname too — a control character, a lone surrogate, a character
    that prints nothing or turns the text round (a zero-width space, a
    right-to-left override) — so it stays one line on every surface that prints
    it and never prints the same as another; any emoji is a name. The case is
    kept as typed and compared without it (``services.claude_accounts``
    refuses ``WORK`` beside ``work``, and resolves either spelling).
    """
    alias = raw.strip()
    if not alias:
        raise ValueError("an alias cannot be empty")
    if len(alias) > ALIAS_MAX:
        raise ValueError(f"{raw!r} is {len(alias)} characters — an alias is at most {ALIAS_MAX}")
    if name_key(alias).isdigit() or slot_number(name_key(alias)) is not None:
        raise ValueError(
            f"{raw!r} reads as a slot number — an alias cannot be only digits, signed or not"
        )
    if "@" in alias:
        raise ValueError(f"{raw!r} reads as an email — an alias cannot contain '@'")
    flaw = name_flaw(alias)
    if flaw == "control":
        raise ValueError(f"{raw!r} has a control character — an alias is one line of text")
    if flaw == "undecodable":
        raise ValueError(
            f"{raw!r} has bytes the terminal's encoding could not decode — an alias is text"
        )
    if flaw == "invisible":
        raise ValueError(
            f"{raw!r} has an invisible formatting character — an alias prints as it is typed"
        )
    labelled = _labelled_slot(alias)
    if labelled is not None and labelled != slot:
        raise ValueError(
            f"{raw!r} is what slot {labelled} is called without an alias — "
            "an alias cannot be another account's name"
        )
    return alias


# --- creating and removing ----------------------------------------------------------


def next_free_slot() -> int:
    """The lowest number from :data:`FIRST_MANAGED_SLOT` up that no agent's slot uses.

    A removed slot's directory was renamed, so its number is free again; the
    next account added takes the lowest gap rather than growing forever. One
    ladder for every agent: a Codex slot never takes a number a Claude slot
    has, so ``--account 3`` names one login.
    """
    taken = {account.slot for account in all_managed_accounts()}
    roots = [accounts_root(agent) for agent in account_agents()]
    slot = FIRST_MANAGED_SLOT
    while slot in taken or any((root / str(slot)).exists() for root in roots):
        slot += 1
    return slot


def create_account(agent: str = AGENT) -> ClaudeAccount:
    """Make the next slot's directories (0700) and its marker; nothing is signed in yet."""
    account = _managed(next_free_slot(), agent)
    paths.ensure_home()
    account.config_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    if account.tmp_dir is not None:
        account.tmp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker: dict[str, Any] = {
        "slot": account.slot,
        "created_at": _now().isoformat(),
        "created_by": f"aisquare-cli {__version__}",
    }
    if not kind(agent).legacy_slots:
        marker["agent"] = agent  # Claude Code's slots' markers have always gone without
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


def claude_json_path(
    account: ClaudeAccount,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Where this account's ``.claude.json`` is.

    Inside the directory whenever ``CLAUDE_CONFIG_DIR`` names it — every managed
    slot, and a default that the environment redirects — and at ``~/.claude.json``
    for the plain default, which is where Claude Code keeps it.
    """
    if (
        account.managed
        or CONFIG_DIR_VAR in plain_environment(env)
        or account.config_dir != home_config_dir()
    ):
        return account.config_dir / CLAUDE_JSON
    return _home() / CLAUDE_JSON


def credentials_path(account: ClaudeAccount) -> Path:
    return account.config_dir / CREDENTIALS


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def identity(
    account: ClaudeAccount,
    *,
    env: Mapping[str, str] | None = None,
) -> ClaudeIdentity | None:
    """The signed-in account, or ``None`` when the directory has no login recorded: its
    agent's accounts slot's answer (``AgentAccounts.identity``)."""
    return kind(account.agent).identity(account, env=env)


def read_identity(path: Path) -> ClaudeIdentity | None:
    """Read stable account identity without using or fingerprinting rotating tokens."""
    return identity_in(_read_json(path))


def identity_in(data: Mapping[str, Any] | None) -> ClaudeIdentity | None:
    """The signed-in account a parsed ``.claude.json`` records, or ``None``: for a reader
    that has parsed the file for something else, which can be tens of MB."""
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


def credentials(account: ClaudeAccount, *, fresh: bool = False) -> ClaudeCredentials | None:
    """The stored OAuth credentials, or ``None`` when none are stored or they cannot be read.

    :func:`stored_login`'s credentials, for a reader that needs no reason when there are
    none. ``fresh`` reads the Keychain again rather than take a read from the last
    :data:`KEYCHAIN_MEMO_SECONDS`.
    """
    return stored_login(account, fresh=fresh).credentials


def credentials_in(data: Mapping[str, Any] | None) -> ClaudeCredentials | None:
    """The OAuth credentials a parsed credentials document holds (``.credentials.json``, or
    the Keychain item, which holds the same JSON), or ``None`` when it holds no usable token."""
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
        # Milliseconds since the epoch, as Claude Code writes it. A number the
        # clock cannot represent (``1e300``) is a damaged file, and a damaged
        # credentials file is not one to lift a token out of.
        try:
            expires_at = datetime.fromtimestamp(raw_expiry / 1000, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    subscription = oauth.get("subscriptionType")
    tier = oauth.get("rateLimitTier")
    return ClaudeCredentials(
        access_token=token,
        expires_at=expires_at,
        subscription_type=subscription if isinstance(subscription, str) else None,
        rate_limit_tier=tier if isinstance(tier, str) else None,
    )


@dataclass(frozen=True)
class StoredLogin:
    """What the store Claude Code keeps an account's login in answered.

    ``credentials`` is ``None`` both when no login is stored and when the store would not
    say; ``unreadable`` tells the two apart, and says why in a line the Accounts page and
    ``accounts usage`` show as they stand: a locked or refusing Keychain is not a login to
    do again, so it is not a missing token. It never carries the token.
    """

    credentials: ClaudeCredentials | None
    unreadable: str | None = None


class LoginUnreadable(Exception):
    """The store an account's login is kept in would not say (a locked or refusing
    Keychain): raised by an accounts slot's ``login_fingerprint``
    (``agent_adapters.types.AgentAccounts``), with the reason."""


def login_digest(creds: ClaudeCredentials) -> str:
    """A digest of a Claude Code login (its token and expiry), never the token: the accounts
    slot's ``login_fingerprint``, and the usage cache's key."""
    stamp = creds.expires_at.isoformat() if creds.expires_at is not None else ""
    return hashlib.sha256(f"{creds.access_token}\0{stamp}".encode()).hexdigest()


def stored_login(account: ClaudeAccount, *, fresh: bool = False) -> StoredLogin:
    """The ONE credential source: the account's login where Claude Code keeps it.

    On macOS that is the login Keychain, in the item :func:`keychain_service` names for
    the account (:func:`read_keychain`); a Keychain with no such item is not the end of
    it, since some setups write ``.credentials.json`` there too (Claude Code falls back
    to the file when it cannot write the Keychain), so the file is read then
    (:func:`plaintext_credentials_path`), as on Linux, where Claude Code writes it (0600)
    into the config dir and nothing else. ``fresh`` is :func:`credentials`'.

    Another agent's account keeps no Claude Code login: none is read for it, from the
    Keychain or a file (its adapter's accounts slot reads its own).
    """
    if not _claude_codes(account):
        return StoredLogin(None)
    if keychain_platform():
        read = read_keychain(keychain_service(account), keychain_account(), fresh=fresh)
        if read.found or read.unreadable is not None:
            return StoredLogin(read.credentials, read.unreadable)
        return StoredLogin(credentials_in(_read_json(plaintext_credentials_path(account))))
    return StoredLogin(credentials_in(_read_json(credentials_path(account))))


def _claude_codes(account: ClaudeAccount) -> bool:
    """Whether ``account``'s agent keeps its slots as Claude Code does (its accounts slot is
    :class:`ClaudeCodeAccounts`); ``False`` for any other, one this install does not know
    included."""
    try:
        return isinstance(kind(account.agent), ClaudeCodeAccounts)
    except ValueError:
        return False


def plaintext_credentials_path(
    account: ClaudeAccount, environ: Mapping[str, str] | None = None
) -> Path:
    """Where a Claude Code on macOS writes the file it falls back to without a Keychain item.

    Its ``wx()``: the directory ``CLAUDE_SECURESTORAGE_CONFIG_DIR`` names when set (an empty
    one is ``~/.claude``), NFC, as the item is keyed on it (:func:`keychain_service`); else
    the account's config dir, :func:`credentials_path`. On Linux aisquare reads the config
    dir's file whatever that variable says, as it always has.
    """
    import unicodedata

    source = os.environ if environ is None else environ
    directory = source.get(SECURE_STORAGE_VAR)
    if directory is None:
        return credentials_path(account)
    base = Path(unicodedata.normalize("NFC", directory)) if directory else home_config_dir()
    return base / CREDENTIALS


def keychain_platform() -> bool:
    """Whether this platform keeps Claude Code's credentials in the Keychain (macOS)."""
    return sys.platform == "darwin"


# --- the macOS Keychain -------------------------------------------------------------------
#
# Claude Code on macOS keeps each config dir's login in one generic-password item of the
# login Keychain, as the same JSON `.credentials.json` holds elsewhere. Read from its
# bundle (2.1.300, chunks 0jv9mjsf and v2e77mt7; the same since 2.1.296):
#
#   service  "Claude Code" + OAUTH_FILE_SUFFIX ("" in a release build) + "-credentials"
#            + ("" when CLAUDE_SECURESTORAGE_CONFIG_DIR is set but empty, or is unset and
#               CLAUDE_CONFIG_DIR is unset or empty;
#               else "-" + sha256(dir.normalize("NFC")).hex[:8], where dir is
#               CLAUDE_SECURESTORAGE_CONFIG_DIR when set, else the RAW CLAUDE_CONFIG_DIR:
#               not resolved, no trailing slash stripped, no ~ expanded)
#   account  $USER, else the login name; "claude-code-user" when that is not
#            /^[a-zA-Z0-9._-]+$/
#
# and it writes the item through `security add-generic-password -U -a … -s … -X <hex>`,
# so `/usr/bin/security` is the item's own creator, and reading through it asks nothing.

KEYCHAIN_TOOL = "/usr/bin/security"
"""The program that reads the item: never a shell, always this path, never one on PATH."""
KEYCHAIN_TIMEOUT_SECONDS = 5.0
"""How long a read may take: a locked Keychain in a desktop session asks to be unlocked."""
KEYCHAIN_MEMO_SECONDS = 10.0
"""How long a read that answered (the item, or none) stands for the next readers in this
process: the Accounts page reads each slot every two seconds, and one usage round read each
token three times over (the cache's key, the request, the reading's record). A read the
Keychain refused (locked, an unlock cancelled, no answer) stands until someone asks again
(:func:`forget_keychain_reads`): asked by itself, a locked Keychain in a desktop session
puts its unlock dialog up for every read, which every tick of asq's would repeat."""
SECURE_STORAGE_VAR = "CLAUDE_SECURESTORAGE_CONFIG_DIR"
"""Claude Code's override of the directory its secure storage is keyed on: it wins."""
KEYCHAIN_ACCOUNT_FALLBACK = "claude-code-user"
KEYCHAIN_LOCKED = "the login Keychain is locked — unlock it (or log in to the Mac) and refresh"
KEYCHAIN_REFUSED = (
    "macOS refused aisquare access to the Claude Code item — allow it in Keychain Access"
)
"""Said for errSecAuthFailed (51): the Deny button on the item's access prompt."""

_KEYCHAIN_SERVICE = "Claude Code"
_KEYCHAIN_OAUTH_SUFFIX = ""
"""``OAUTH_FILE_SUFFIX``: empty in a release build (``-local-oauth``, ``-custom-oauth`` else)."""
_KEYCHAIN_CREDENTIALS_SUFFIX = "-credentials"
_KEYCHAIN_ACCOUNT_NAME = re.compile(r"[a-zA-Z0-9._-]+")
# `security` exits with the low byte of the OSStatus it got.
_ITEM_NOT_FOUND = 44  # errSecItemNotFound (-25300)
_NO_KEYCHAIN = frozenset({37, 50})  # errSecNoDefaultKeychain, errSecNoSuchKeychain
_LOCKED = frozenset(
    {
        36,  # errSecInteractionNotAllowed (-25308): locked, and no dialog to unlock it with
        128,  # errSecUserCanceled (-128): the unlock dialog cancelled (/usr/bin/security made
        # the item, so reading it asks nothing else)
    }
)
_REFUSED = frozenset({51})  # errSecAuthFailed (-25293): Deny on the item's access prompt
_HEX = re.compile(r"(?:[0-9a-fA-F]{2})+")


def keychain_account(environ: Mapping[str, str] | None = None) -> str:
    """The item's account, as Claude Code names it: ``$USER``, else this user's login name."""
    source = os.environ if environ is None else environ
    name = source.get("USER") or _login_name()
    if name is None or not _KEYCHAIN_ACCOUNT_NAME.fullmatch(name):
        return KEYCHAIN_ACCOUNT_FALLBACK
    return name


def _login_name() -> str | None:
    """This user's login name as ``os.userInfo().username`` reads it (the effective user's
    passwd entry), or ``None`` without one. Never asked on Windows, which has no passwd."""
    if sys.platform == "win32":
        return None
    import pwd

    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except (KeyError, OSError):
        return None


def keychain_service(account: ClaudeAccount, environ: Mapping[str, str] | None = None) -> str:
    """The Keychain item a Claude Code launched on ``account`` keeps its login in.

    Hashed from the ``CLAUDE_CONFIG_DIR`` a launch on the account is given, byte for byte:
    a managed slot's is :func:`launch_env`'s; the plain claude's is this shell's own as
    :func:`plain_environment` reads it, unexpanded, which is none for ``~/.claude`` (the
    item without a suffix). ``CLAUDE_SECURESTORAGE_CONFIG_DIR``, which every launch from
    this environment inherits, wins over both, as it does in Claude Code.
    """
    source = os.environ if environ is None else environ
    directory = source.get(SECURE_STORAGE_VAR)
    if directory is None:
        if account.managed:
            directory = launch_env(account)[CONFIG_DIR_VAR]
        else:
            directory = plain_environment(source).get(CONFIG_DIR_VAR, "")
    suffix = f"-{_js_sha256(directory)[:8]}" if directory else ""
    return f"{_KEYCHAIN_SERVICE}{_KEYCHAIN_OAUTH_SUFFIX}{_KEYCHAIN_CREDENTIALS_SUFFIX}{suffix}"


def _js_sha256(text: str) -> str:
    """``createHash("sha256").update(text.normalize("NFC")).digest("hex")`` for ``text`` read
    from the environment: Node decodes it as UTF-8, each byte it cannot as U+FFFD, where
    Python keeps that byte as a lone surrogate (which no macOS path can hold anyway)."""
    import hashlib
    import unicodedata

    try:
        text = text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    except UnicodeEncodeError:
        text = text.encode("utf-8", "replace").decode("utf-8")
    return hashlib.sha256(unicodedata.normalize("NFC", text).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class KeychainRead:
    """What one read of a Keychain item answered: never the item's text, which also holds
    the refresh token and every MCP server's, only the credentials lifted out of it."""

    found: bool
    """The item exists (it may still hold no usable login: a ``/logout`` keeps the item)."""
    credentials: ClaudeCredentials | None = None
    unreadable: str | None = None
    """Why the Keychain would not say: locked, refused, timed out (:func:`read_keychain`)."""


_KEYCHAIN_READS: dict[tuple[str, str], tuple[float, KeychainRead]] = {}
"""The last read of each (service, account), with when it was taken (``time.monotonic``)."""
_KEYCHAIN_READS_LOCK = threading.Lock()
_KEYCHAIN_GENERATION = [0]
"""Bumped by each :func:`forget_keychain_reads`: a read that began before one is not kept,
or a refusal from before an unlock would stand after the refresh that asked again."""


def _monotonic() -> float:
    return time.monotonic()


def forget_keychain_reads() -> None:
    """Drop every remembered Keychain read, so the next asks the Keychain: a forced refresh,
    after someone unlocked the Keychain or allowed aisquare in at the row's word."""
    with _KEYCHAIN_READS_LOCK:
        _KEYCHAIN_READS.clear()
        _KEYCHAIN_GENERATION[0] += 1


def read_keychain(service: str, account: str, *, fresh: bool = False) -> KeychainRead:
    """The item ``service``/``account`` of the login Keychain, read through ``security``.

    ``security find-generic-password -a <account> -w -s <service>``, run as an argument
    list (never a shell) under :data:`KEYCHAIN_TIMEOUT_SECONDS`, prints the item's text,
    the same JSON as ``.credentials.json``. Nothing it prints is logged, kept or put into
    a reason or an exception; the credentials lifted out of it are kept in this process
    for :data:`KEYCHAIN_MEMO_SECONDS` (a refusal: until :func:`forget_keychain_reads`)
    unless ``fresh``. A read is a subprocess, which callers run off the UI thread.

    No item (exit 44), or no login Keychain at all (37, 50), is ``found=False``; a locked
    Keychain, one that refused, a timeout and anything else is ``unreadable``, in words.
    """
    key = (service, account)
    with _KEYCHAIN_READS_LOCK:
        remembered = None if fresh else _KEYCHAIN_READS.get(key)
        generation = _KEYCHAIN_GENERATION[0]
    if remembered is not None:
        taken, read = remembered
        if read.unreadable is not None or 0 <= _monotonic() - taken < KEYCHAIN_MEMO_SECONDS:
            return read
    read = _ask_keychain(service, account)
    with _KEYCHAIN_READS_LOCK:
        if _KEYCHAIN_GENERATION[0] == generation:
            _KEYCHAIN_READS[key] = (_monotonic(), read)
    return read


def _ask_keychain(service: str, account: str) -> KeychainRead:
    import subprocess

    try:
        done = subprocess.run(
            [KEYCHAIN_TOOL, "find-generic-password", "-a", account, "-w", "-s", service],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:  # carries what it printed so far: dropped here
        return KeychainRead(
            found=False,
            unreadable=f"could not read the Keychain (no answer in {KEYCHAIN_TIMEOUT_SECONDS:g} s)",
        )
    except OSError as exc:
        why = exc.strerror or "not run"
        return KeychainRead(
            found=False, unreadable=f"could not read the Keychain ({KEYCHAIN_TOOL}: {why})"
        )
    if done.returncode == 0:
        text = done.stdout.decode("utf-8", "replace").strip()
        if not text:
            return KeychainRead(found=False)
        return KeychainRead(found=True, credentials=credentials_in(_item_json(text)))
    if done.returncode == _ITEM_NOT_FOUND or done.returncode in _NO_KEYCHAIN:
        return KeychainRead(found=False)
    return KeychainRead(found=False, unreadable=_keychain_problem(done.returncode, done.stderr))


def _item_json(text: str) -> dict[str, Any] | None:
    """The item's JSON: as ``security -w`` printed it, or from the hex it prints instead
    for a password with a byte it will not print as it stands."""
    for candidate in (text, _unhex(text)):
        if candidate is None:
            continue
        try:
            loaded: Any = json.loads(candidate)
        except ValueError:
            continue
        return loaded if isinstance(loaded, dict) else None
    return None


def _unhex(text: str) -> str | None:
    if not _HEX.fullmatch(text):
        return None
    try:
        return bytes.fromhex(text).decode("utf-8")
    except ValueError:
        return None


def _keychain_problem(code: int, stderr: bytes) -> str:
    """Why ``security`` exited ``code``, in the row's words. By the exit code first, which
    is the OSStatus's low byte, then by what it said, as Claude Code classifies it."""
    said = stderr.decode("utf-8", "replace").lower()
    if code in _LOCKED or any(
        words in said
        for words in ("interaction is not allowed", "interactionnotallowed", "canceled")
    ):
        return KEYCHAIN_LOCKED
    if code in _REFUSED or any(words in said for words in ("name or passphrase", "authfailed")):
        return KEYCHAIN_REFUSED
    if "locked" in said or "unlock" in said:
        return KEYCHAIN_LOCKED
    return f"could not read the Keychain (security exited {code})"


def token_state(creds: ClaudeCredentials | None, now: datetime | None = None) -> str:
    if creds is None:
        return "missing"
    return "expired" if creds.expired(now) else "ok"


def signed_in(account: ClaudeAccount, *, fresh: bool = False) -> bool:
    """A login is recorded AND its credentials are where this platform keeps them.

    Identity alone is not enough: ``/logout`` deletes the credentials (the file on Linux,
    the item's login on macOS), and a stale ``oauthAccount`` left behind would read as a
    working login that Claude Code itself would refuse. A Keychain that would not say
    (:attr:`StoredLogin.unreadable`: locked, refused) is not a logout, so the recorded
    identity answers then, and the row says why its usage cannot be read rather than
    offer a log-in that would not help. ``fresh`` is :func:`credentials`'. Another agent's
    is its accounts slot's answer (``AgentAccounts.signed_in``).
    """
    return kind(account.agent).signed_in(account, fresh=fresh)


def _claude_signed_in(account: ClaudeAccount, *, fresh: bool = False) -> bool:
    """:func:`signed_in` for a Claude Code slot (``ClaudeCodeAccounts.signed_in``)."""
    if read_identity(claude_json_path(account)) is None:
        return False
    login = stored_login(account, fresh=fresh)
    return login.credentials is not None or login.unreadable is not None


def subscription_label(creds: ClaudeCredentials | None) -> str | None:
    """``max 5x`` from ``default_claude_max_5x``; the plain subscription type otherwise."""
    if creds is None:
        return None
    tier = creds.rate_limit_tier or ""
    match = re.search(r"claude_([a-z]+)_(\d+x)$", tier)
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return creds.subscription_type


# --- what a usage-limit error says (#146) ----------------------------------------------------

# The time is matched by a BOUNDED pattern (an optional weekday, a clock time,
# am/pm — the shape ``_RESET_TIME`` reads) and nothing anchors the end: the
# rendered line can carry a period, a second sentence or more lines after the
# zone ("… (America/Toronto).\nUpgrade for more usage."), and an anchor made the
# whole match fail, which recorded the limit with no window and no reset
# (review of #205, finding 7).
_LIMIT_MESSAGE = re.compile(
    r"hit your (?P<window>[A-Za-z]+) limit"
    r"(?:\s*·\s*resets\s+(?P<when>(?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+)?"
    r"\d{1,2}(?::\d{2})?\s*(?:am|pm)))?"
    r"(?:\s*\((?P<zone>[A-Za-z_]+(?:/[A-Za-z_+\-0-9]+)*)\))?",
    re.IGNORECASE,
)
_RESET_TIME = re.compile(
    r"^(?:(?P<day>mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+)?(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?"
    r"\s*(?P<ampm>am|pm)$",
    re.IGNORECASE,
)
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class LimitNotice:
    """What a Claude Code usage-limit error told us: which window, and when it lifts."""

    window: str
    """``session``, ``weekly``, ``opus``, ``sonnet``… — lowercased, as the message named it."""
    resets_at: datetime | None
    """The reset as an aware UTC datetime, or ``None`` when the message named no time
    (or one this parser could not read — the raw text stays on the board event)."""


@dataclass(frozen=True)
class FailedTurn:
    """A turn the session's transcript records as ended on an error the agent fired no hook
    for (``AgentAccounts.failed_turn``): the agent's id for the turn, the error as a
    ``turn_failed`` event names it (``rate_limit`` for a usage limit, as Claude Code's
    ``StopFailure`` does; its window and reset are ``limit_from_transcript``'s), its
    text, and when the transcript recorded its end."""

    turn_id: str | None
    error: str
    message: str
    at: datetime | None = field(default=None, compare=False)


def parse_limit_notice(text: str | None, *, now: datetime | None = None) -> LimitNotice | None:
    """Read ``You've hit your session limit · resets 12:30am (America/Toronto)``, or ``None``.

    That is the rendered text Claude Code shows — and hands a ``StopFailure``
    hook as ``last_assistant_message`` — when a subscription's rolling allowance
    runs out (measured in this machine's own transcripts, 2026-09-13; the errors
    reference documents the same four shapes: session, weekly, Opus, Sonnet,
    the weekly one with a weekday, ``resets Mon 12:00am``). The time is a clock
    time in the zone named in parentheses, or the local zone when none is; it
    is resolved to the next such moment at or after ``now``, on the named
    weekday when there is one. A ``rate_limit`` that is not a usage limit —
    ``Request rejected (429)`` from an API key, the server's own throttle —
    does not match, and the caller treats it as a limit with no reset time.
    """
    if not text:
        return None
    match = _LIMIT_MESSAGE.search(text)
    if match is None:
        return None
    window = match.group("window").lower()
    when = (match.group("when") or "").strip()
    if not when:
        return LimitNotice(window, None)
    resets_at = _resolve_reset(when, match.group("zone"), now or _now())
    return LimitNotice(window, resets_at)


def _resolve_reset(when: str, zone_name: str | None, now: datetime) -> datetime | None:
    clock = _RESET_TIME.match(when.strip())
    if clock is None:
        return None
    raw_hour = int(clock.group("hour"))
    minute = int(clock.group("minute") or 0)
    if raw_hour > 12 or minute > 59:  # `13:00pm` is no clock time; `12:30am` is 00:30
        return None
    hour = raw_hour % 12
    if clock.group("ampm").lower() == "pm":
        hour += 12
    try:
        zone: tzinfo = ZoneInfo(zone_name) if zone_name else _local_zone(now)
    except (ZoneInfoNotFoundError, ValueError):
        zone = _local_zone(now)
    local_now = now.astimezone(zone)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    day = clock.group("day")
    if day is not None:
        # `resets Mon 12:00am`: the next Monday at that time — today if it is
        # Monday and the time is still ahead, else up to a week out.
        wanted = _WEEKDAYS.index(day.lower()[:3])
        ahead = (wanted - candidate.weekday()) % 7
        candidate += timedelta(days=ahead)
        if candidate < local_now:
            candidate += timedelta(days=7)
    elif candidate < local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


_LOCALTIME = Path("/etc/localtime")
"""Where Linux and macOS keep the machine's zone: a TZif file, or a link to one."""


def _local_zone(now: datetime) -> tzinfo:
    """The local zone WITH its rules, so a moment days away gets the offset in force then.

    ``now.astimezone().tzinfo`` is a fixed offset — the one in force NOW — and a
    weekly reset read against it on the far side of a DST change came out an
    hour off, and with it the board's ``⏳ limited`` line, ``format_reset`` and
    the grace ``_derive`` gives a parked row (review of #205, fourth round). The
    zone is ``TZ``'s when the process has one, since that is what the clock
    itself honours, else ``/etc/localtime``'s. Only a zone neither can load —
    a POSIX rule string in ``TZ``, a machine without the file (Windows) — falls
    back to the offset in force now.
    """
    raw = os.environ.get("TZ")
    key = raw.strip().removeprefix(":") if raw is not None else None
    try:
        if key is None:
            if _LOCALTIME.is_file():
                with _LOCALTIME.open("rb") as handle:
                    return ZoneInfo.from_file(handle, key="localtime")
        elif Path(key).is_absolute():  # `TZ=:/usr/share/zoneinfo/America/Toronto`
            with Path(key).open("rb") as handle:
                return ZoneInfo.from_file(handle, key=key)
        elif key:  # an empty TZ is UTC, which the offset below already is
            return ZoneInfo(key)
    except (OSError, ValueError, ZoneInfoNotFoundError):
        pass
    return now.astimezone().tzinfo or UTC


def format_reset(when: datetime | None, *, now: datetime | None = None, clock: bool = True) -> str:
    """When a rate-limit window lifts, as a distance AND a clock time: ``in 3h 10m (18:00)``.

    The ONE formatter for every surface that shows a reset — ``accounts usage``
    / ``list --usage``, the Accounts page, the board's ``limited`` line, the
    agent's ``limit resets …`` detail and doctor's parked-agents line — because
    copies drifted (#152): two printed a bare ``HH:MM``, which for the seven-day
    window can be six days away and read as tonight. It lives in core so the
    services can call it too (review of #205, finding 9).

    The rules, each chosen so nobody has to do calendar arithmetic:

    - under an hour: ``in 12m`` — the clock time adds nothing;
    - later the same LOCAL day: ``in 3h 10m (18:00)``;
    - another day: ``in 2d 4h (Tue 02:00)`` — the weekday is what tells a
      weekly reset from tonight's, and it is never a bare ``HH:MM`` again;
    - already past (the endpoint's reading is a little stale): ``now``.

    ``now`` is the clock to measure against; production reads the wall clock,
    tests pass one so the midnight boundary can be pinned. Returns ``""`` for
    ``None`` so callers can append it unconditionally. ``clock=False`` is the
    distance alone, ``in 3h 10m``, for a reader whose clock is not this
    machine's: the Remote's phone, which tells the time itself.
    """
    if when is None:
        return ""
    moment = now if now is not None else datetime.now(tz=UTC)
    # In UTC on purpose: two aware datetimes that share one tzinfo subtract by
    # their WALL CLOCKS (Python ignores the offsets then), so a reset across a
    # DST change would read an hour long; the endpoint's UTC stamps never hit
    # this, a caller's zoned pair would (review of #205, second round).
    remaining = when.astimezone(UTC) - moment.astimezone(UTC)
    if remaining <= timedelta(0):
        return "now"
    total_minutes = int(remaining.total_seconds() // 60)
    days, rest = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    # The clock time is shown to the nearest MINUTE. Measured against the live
    # endpoint (2026-09-13): the same window's ``resets_at`` came back as
    # 08:59:59.86, 09:00:00.26 and 08:59:59.62 on three calls seconds apart —
    # it jitters across the second boundary — so a truncated ``%H:%M`` flickered
    # between 04:59 and 05:00 from one refresh to the next. Rounding says what
    # a person means by the time of a reset, and the distance still moves.
    local_when = (when.astimezone() + timedelta(seconds=30)).replace(second=0, microsecond=0)
    # Each instant in the local zone AS OF THAT INSTANT. ``astimezone()`` with no
    # argument attaches the fixed offset in force at the value it converts, and
    # reusing the reset's offset for ``now`` compared the two dates an hour apart
    # across a DST change — a next-day reset printed as a bare clock time, the
    # very drift #152 removed (review of #205, second round).
    local_now = moment.astimezone()
    if remaining < timedelta(hours=1):
        return f"in {max(minutes, 1)}m"
    if days == 0:
        distance = f"{hours}h" if minutes == 0 else f"{hours}h {minutes:02d}m"
    else:
        distance = f"{days}d" if hours == 0 else f"{days}d {hours}h"
    if not clock:
        return f"in {distance}"
    if local_when.date() == local_now.date():
        return f"in {distance} ({local_when:%H:%M})"
    return f"in {distance} ({local_when:%a %H:%M})"


def format_age(when: datetime | None, *, now: datetime | None = None) -> str:
    """`` · as of 3h ago`` for a reading taken a while back; ``""`` when recent or unknown.

    The one formatter for a reading's age, as :func:`format_reset` is for a reset:
    a Codex account's usage is what its newest session recorded, as fresh as that
    account's last turn, and ``accounts usage`` and the Accounts page both say so.
    Under ten minutes it adds nothing.
    """
    if when is None:
        return ""
    moment = now if now is not None else datetime.now(tz=UTC)
    minutes = int((moment.astimezone(UTC) - when.astimezone(UTC)).total_seconds() // 60)
    if minutes < 10:
        return ""
    if minutes < 120:
        return f" · as of {minutes}m ago"
    if minutes < 48 * 60:
        return f" · as of {minutes // 60}h ago"
    return f" · as of {minutes // (24 * 60)}d ago"


# --- launching ------------------------------------------------------------------------


LAUNCH_VARS = (CONFIG_DIR_VAR, TMPDIR_VAR)
"""The two variables an account IS, for a launch."""

PLAIN_VARS = {
    CONFIG_DIR_VAR: "AISQUARE_PLAIN_CLAUDE_CONFIG_DIR",
    TMPDIR_VAR: "AISQUARE_PLAIN_CLAUDE_CODE_TMPDIR",
}
"""Where a launch onto a managed slot keeps the launching shell's own two variables.

The launch overwrites both with the slot's, and nothing started under the slot
could otherwise tell which directory the plain claude is. A variable the shell
did not have gets no copy.
"""


def _launch_vars(agent: str) -> tuple[tuple[str, ...], Mapping[str, str]]:
    """The variables an account of ``agent`` IS (its home's, then its accounts'
    ``extra_env``: Claude Code's :data:`LAUNCH_VARS`), and where a launch keeps the shell's
    own (``plain_vars``: Claude Code's :data:`PLAIN_VARS`)."""
    accounts = kind(agent)
    return (accounts.home_env, *accounts.extra_env), accounts.plain_vars


def plain_environment(
    environ: Mapping[str, str] | None = None, *, agent: str = AGENT
) -> dict[str, str]:
    """The plain claude's two variables as ``environ`` (default: this process's) has them.

    ``agent`` asks the same of another agent's (``CODEX_HOME`` for Codex), read
    by the same rule against that agent's own slots and copies.

    Each one ``environ`` sets to a non-blank value, as a plain ``claude`` started
    from it would read them, except in an environment that runs under one of
    OUR slots: ``CLAUDE_CONFIG_DIR`` names the slot there, and both variables
    are the slot's. The plain claude's are then the :data:`PLAIN_VARS` copies
    the launch kept, or none when the launching shell had none (``~/.claude``).
    Read as they stood, every hook of an agent on slot 2 took slot 2 for slot 1.
    The automatic hand-over then read slot 2's usage under slot 1's name and
    relaunched the limited agent on slot 2 as ``--account 1`` (review of #205,
    sixth round).
    """
    source = os.environ if environ is None else environ
    launch_vars, plain_vars = _launch_vars(agent)
    own = {var: source[var] for var in launch_vars if source.get(var, "").strip()}
    config_dir = own.get(launch_vars[0])
    if (
        config_dir is None
        or managed_slot(paths.expand_user(Path(config_dir.strip())), agent) is None
    ):
        return own
    return {var: source[kept] for var, kept in plain_vars.items() if source.get(kept, "").strip()}


def launch_env(account: ClaudeAccount) -> dict[str, str]:
    """The two variables that make a launch run under ``account`` — none for the default.

    The default slot is "whatever this shell's ``claude`` is", so it ADDS
    nothing: see the module docstring for why ``CLAUDE_CONFIG_DIR=~/.claude``
    would not be the same thing. A launch that may already carry another
    account's variables (a role binding) wants :func:`apply_launch_env`.
    """
    if not account.managed:
        return {}
    accounts = kind(account.agent)
    env = {accounts.home_env: str(account.config_dir)}
    if account.tmp_dir is not None:
        # Each of its scratch variables names the slot's scratch dir; Codex has none, and
        # keeps its temporary files under its home.
        env.update(dict.fromkeys(accounts.extra_env, str(account.tmp_dir)))
    return env


def apply_launch_env(
    env: dict[str, str], account: ClaudeAccount, *, shell: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Make ``env`` launch under ``account``, in place, and return it.

    A managed slot sets both variables, and keeps the shell's own beside them
    under :data:`PLAIN_VARS`. The default slot RESTORES the shell's own: each
    variable becomes what the launching shell had, and one the shell did not
    have is removed — so ``--account 1`` means "the plain claude of this shell"
    even when the role's binding had pointed the launch at another directory.
    Without the restore a launch announced as the default ran on whatever the
    binding said, which is the one thing the flag exists to override. "The
    shell's own" is :func:`plain_environment`'s reading, so a shell that itself
    runs under one of our slots restores the plain claude, never that slot.
    ``shell`` defaults to this process's environment.
    """
    launch_vars, plain_vars = _launch_vars(account.agent)
    plain = plain_environment(shell, agent=account.agent)
    wanted: dict[str, str | None]
    if account.managed:
        env.update(launch_env(account))
        wanted = {kept: plain.get(var) for var, kept in plain_vars.items()}
    else:
        wanted = {var: plain.get(var) for var in launch_vars}
        wanted.update(dict.fromkeys(plain_vars.values()))  # restored in place: no copy to go stale
    for var, value in wanted.items():
        if value is None:
            env.pop(var, None)
        else:
            env[var] = value
    return env


# --- Claude Code's accounts slot (core.agent_adapters.types.AgentAccounts) ---------------

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
"""What Claude Code's ``/usage`` reads; ``anthropic-beta: oauth-2025-04-20`` is required."""
OAUTH_BETA = "oauth-2025-04-20"
USAGE_TIMEOUT_SECONDS = 10.0


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
MODEL_FAMILY_NAMES = frozenset(MODEL_FAMILIES.values())
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
        if not key.startswith(_FAMILY_WEEK) or family not in MODEL_FAMILY_NAMES:
            continue
        percent, when = _window(raw)
        if percent is not None:
            percents[family] = percent
        if when is not None:
            resets[family] = when
    for limit in _limits(payload):
        named = limit.kind.removeprefix(_FAMILY_WEEK)
        scoped = limit.family or (named if named in MODEL_FAMILY_NAMES else None)
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


def endpoint_usage(
    account: ClaudeAccount, *, now: datetime | None, fetch: UsageFetch
) -> ClaudeUsage:
    """The account's five-hour and seven-day windows, or why they cannot be read.

    Reads the token where Claude Code keeps it (:func:`stored_login`: the
    credentials file, or on macOS the login Keychain) and sends it to
    :data:`USAGE_URL` only, through ``fetch`` (the service's HTTP GET: core opens
    no socket); a token that has already expired is not sent at all, because
    Claude Code refreshes it on the account's next session and this module never
    does. A Keychain that would not show the token says why (locked, refused),
    and asks for no log-in. No reason ever carries the token. ``fetched_at`` is
    left for the caller to stamp.

    Every window the endpoint sends is read (#277): the per-family weeks
    (``seven_day_opus``, ``seven_day_sonnet``: :attr:`ClaudeUsage.family_week_percent`),
    the ``limits[]`` entries, folded into the window of their kind as the fuller figure,
    and a ``locked_reason``, which reads that window 100 % used. Keys it does not know
    are ignored.
    """
    login = stored_login(account)
    creds = login.credentials
    if creds is None:
        if login.unreadable is not None:
            reason = login.unreadable
        elif keychain_platform():
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
        status, body = fetch(USAGE_URL, headers, USAGE_TIMEOUT_SECONDS)
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
        # Both windows, each read or not: one the endpoint answered without went unread.
        windows=session_and_week(
            _fuller(session_percent, _limit_of(payload, _SESSION_KINDS)),
            session_resets,
            _fuller(week_percent, _limit_of(payload, _WEEK_KINDS)),
            week_resets,
            every=True,
        ),
        family_week_percent=family_percent,
        family_week_resets_at=family_resets,
        fetched_at=now,
    )


class ClaudeFolderTrust:
    """Claude Code's folder trust: ``projects["<folder>"].hasTrustDialogAccepted`` in each
    account's ``.claude.json``, under its own lock (``core.claude_sharing``)."""

    file_name = CLAUDE_JSON

    def scope(self, base: claude_sharing.TrustScope) -> claude_sharing.TrustScope:
        return base  # core.claude_sharing's rules are Claude Code's

    def read(self, account: ClaudeAccount) -> dict[str, bool]:
        data = claude_sharing.read_claude_json(claude_json_path(account))
        return dict.fromkeys(claude_sharing.trusted_folders(data), True)

    def grant(self, account: ClaudeAccount, *keys: str) -> list[str]:
        return claude_sharing.grant_trust(claude_json_path(account), *keys)


class ClaudeCodeAccounts:
    """Claude Code's answers to the accounts slot: the functions above, as they were."""

    agent = AGENT
    home_env = CONFIG_DIR_VAR
    extra_env: tuple[str, ...] = (TMPDIR_VAR,)
    plain_vars: Mapping[str, str] = PLAIN_VARS
    dir_name = "claude-accounts"
    plain_slot = DEFAULT_SLOT
    plain_label = "plain claude"
    install_command = INSTALL_COMMAND
    network_usage = True
    legacy_slots = True
    noun = "Claude"
    relogin = "/login"
    sign_out = "inside Claude Code (/logout)"
    shares_settings = True

    def sign_in_args(self) -> tuple[str, ...]:
        # No non-interactive login: a first session in the slot runs Claude Code's own.
        return ()

    def plain_home(self, home: Path, plain: Mapping[str, str]) -> Path:
        value = plain.get(CONFIG_DIR_VAR, "").strip()
        return paths.expand_user(Path(value)) if value else home / HOME_DIR

    def config_dir(self, slot_dir: Path) -> Path:
        return slot_dir  # CLAUDE_CONFIG_DIR names the config dir itself

    def launched_account(
        self, env: Mapping[str, str], accounts: Sequence[ClaudeAccount]
    ) -> ClaudeAccount | None:
        """By the ``.claude.json`` a ``claude`` started with ``env`` reads: inside the dir
        ``CLAUDE_CONFIG_DIR`` names, else beside ``~/.claude`` (:func:`claude_json_path`)."""
        config = env.get(CONFIG_DIR_VAR, "").strip()
        wanted = (
            paths.expand_user(Path(config)) / CLAUDE_JSON
            if config
            else home_config_dir().with_name(CLAUDE_JSON)
        )
        return next((one for one in accounts if _same_file(claude_json_path(one), wanted)), None)

    def identity(
        self, account: ClaudeAccount, *, env: Mapping[str, str] | None = None
    ) -> ClaudeIdentity | None:
        return read_identity(claude_json_path(account, env=env))

    def signed_in(self, account: ClaudeAccount, *, fresh: bool = False) -> bool:
        return _claude_signed_in(account, fresh=fresh)

    def subscription(self, account: ClaudeAccount) -> str | None:
        return subscription_label(credentials(account))

    def token_state(self, account: ClaudeAccount) -> str:
        login = stored_login(account)
        return "unreadable" if login.unreadable is not None else token_state(login.credentials)

    def usage(
        self, account: ClaudeAccount, *, now: datetime | None, fetch: UsageFetch
    ) -> ClaudeUsage:
        return endpoint_usage(account, now=now, fetch=fetch)

    def limit_from_transcript(
        self, path: Path, *, now: datetime | None = None
    ) -> LimitNotice | None:
        # Claude Code reports a limit itself, through StopFailure (``parse_limit_notice``).
        return None

    def failed_turn(self, path: Path) -> FailedTurn | None:
        # And every other failed turn, through the same hook.
        return None

    def login_fingerprint(self, account: ClaudeAccount) -> str | None:
        login = stored_login(account, fresh=True)
        if login.unreadable is not None:
            raise LoginUnreadable(login.unreadable)
        return login_digest(login.credentials) if login.credentials is not None else None

    trust = ClaudeFolderTrust()
