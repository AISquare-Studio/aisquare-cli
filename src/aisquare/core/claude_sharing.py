"""What several Claude Code accounts share: the folders the human trusted, and one settings.json.

Every account is its own ``CLAUDE_CONFIG_DIR`` (``core.claude_accounts``), and Claude Code
keeps two things there that the human means for every account at once.

**Folder trust.** The first time Claude Code starts in a folder it asks "Quick safety
check: Is this a project you created or one you trust?", and a yes is recorded in THAT
account's ``.claude.json`` as ``projects["<folder>"].hasTrustDialogAccepted``. A yes on
account 2 is nothing to account 3, whose agent then sits at the question until a human
answers it — and the highlighted answer is "No, exit", so the first prompt the fleet types
there ends the agent. ``.claude.json`` cannot be shared to fix that: the login
(``oauthAccount``, ``userID``) is in the same file. So the entry that covers a folder is
COPIED from the account that has it (:func:`grant_trust`), and nothing else is touched.

Measured with Claude Code 2.1.299 in a scratch ``CLAUDE_CONFIG_DIR`` (2026-10-14):

- the key is a REAL path: an entry under a symlinked spelling does not count, and a folder
  reached through a link is looked up by its resolved path;
- a yes in a git checkout records the CANONICAL repository root — the main checkout's, from
  a worktree as from a sub-folder — and a yes outside git records the folder itself;
- a folder in a repository is trusted by the canonical root's entry or by one on any folder
  from it up to its own git top level; outside git, by one on it or any ancestor. A folder
  ABOVE a repository's top level does not cover the repository (:func:`trust_scope`);
- ``{"hasTrustDialogAccepted": true}`` alone is an entry Claude Code honours;
- Claude Code saves the file under a ``<file>.lock`` directory (``proper-lockfile``),
  re-reading it under that lock, and replaces it by rename THROUGH a symlink — so an entry
  another process writes under the same lock survives the running session's next save.

**settings.json** (permissions, hooks, model, status line) holds no login, and Claude Code
writes it through a symlink and keeps the link (measured: ``/model`` on a linked file), as
``agents connect`` does (``core.agents``). So the managed accounts can share the plain
claude's by linking theirs to it (:func:`link_settings`) — opt-in, and never where either
file carries what decides a login (:func:`login_keys`): shared, that would sign every
account in as one.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from aisquare.core import atomic
from aisquare.models import ClaudeAccount

TRUST_KEY = "hasTrustDialogAccepted"

LOCK_STALE_SECONDS = 10.0
"""When Claude Code's ``<file>.lock`` is a dead process's: ``proper-lockfile``'s default,
which its config saves use (a live holder refreshes the lock every 5 s)."""
LOCK_WAIT_SECONDS = 2.0
"""How long :func:`grant_trust` waits for a live holder; a save holds it for milliseconds."""
_LOCK_POLL = 0.05

_sleep: Callable[[float], None] = time.sleep
_monotonic: Callable[[], float] = time.monotonic


class SharingError(Exception):
    """Why an account's file was left as it was, in words for a launch note."""


class NotStartedYet(SharingError):
    """The account has no ``.claude.json``: Claude Code never started there, so there is
    no file to add to — and creating one is Claude Code's first run, not ours."""


# --- folder trust ---------------------------------------------------------------------


@dataclass(frozen=True)
class TrustScope:
    """Which ``projects`` entries decide whether Claude Code asks in :attr:`folder`."""

    folder: Path
    """The folder as Claude Code sees it: resolved."""
    key: str
    """What a yes to the question there records: the canonical repository root, else the
    folder. ``accounts trust`` writes this one."""
    covering: tuple[str, ...]
    """Every entry that trusts the folder, :attr:`key` first: then the folder and its
    parents up to its git top level, or up to ``/`` outside git."""


def trust_scope(folder: Path) -> TrustScope:
    """The entries that trust ``folder`` for Claude Code (the module docstring's rules)."""
    real = Path(os.path.realpath(folder))
    top = _git_top(real)
    if top is None:
        return TrustScope(real, project_key(real), tuple(map(project_key, (real, *real.parents))))
    canonical = _canonical_root(top)
    walk = [real, *(parent for parent in real.parents if parent.is_relative_to(top))]
    covering = dict.fromkeys(map(project_key, [canonical, *walk]))
    return TrustScope(real, project_key(canonical), tuple(covering))


def worktree_scope(repository: Path, tree: Path) -> TrustScope:
    """The entries that will trust ``tree``, a worktree of the repository ``repository`` is
    in, whether or not ``git worktree add`` has made it yet.

    Once it exists, :func:`trust_scope`'s: a worktree is its own top level, so the canonical
    root's entry, then its own. Before, the same two: asked of a path with no ``.git`` yet,
    :func:`trust_scope` walked up from it into the main checkout and counted every folder
    there up to the repository's top, which do not cover the tree once it is made (review
    of #259, round 22). Outside git, :func:`trust_scope`'s, as there is no tree to make."""
    if os.path.lexists(tree / ".git"):
        return trust_scope(tree)
    top = _git_top(Path(os.path.realpath(repository)))
    if top is None:
        return trust_scope(tree)
    real = Path(os.path.realpath(tree))
    canonical = project_key(_canonical_root(top))
    return TrustScope(real, canonical, tuple(dict.fromkeys((canonical, project_key(real)))))


def project_key(path: Path) -> str:
    """``path`` as Claude Code spells a ``projects`` key: with ``/`` on Windows too, where
    its path normaliser turns every ``\\`` into one."""
    text = str(path)
    return text.replace("\\", "/") if os.name == "nt" else text


def _git_top(folder: Path) -> Path | None:
    """The nearest folder from ``folder`` up holding ``.git`` — a directory in a checkout,
    a file in a worktree or a submodule — or ``None`` outside git."""
    for candidate in (folder, *folder.parents):
        if os.path.lexists(candidate / ".git"):
            return candidate
    return None


def _canonical_root(top: Path) -> Path:
    """The main checkout of the repository ``top`` is the top level of.

    ``top`` itself for a checkout; for a worktree, the folder holding the ``.git`` its
    ``gitdir``'s ``commondir`` names (``git worktree add`` writes both). A ``.git`` file
    with no ``commondir`` behind it is a submodule's, its own repository's root.
    """
    marker = top / ".git"
    if not marker.is_file():
        return top
    try:
        line = marker.read_text(encoding="utf-8").strip()
        if not line.startswith("gitdir:"):
            return top
        gitdir = top / line.removeprefix("gitdir:").strip()  # an absolute gitdir wins the join
        common = (gitdir / "commondir").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return top
    shared = Path(os.path.realpath(gitdir / common))
    return shared.parent if shared.name == ".git" else shared


def trusted_by(data: Mapping[str, Any] | None, scope: TrustScope) -> str | None:
    """The entry of a parsed ``.claude.json`` that trusts ``scope``'s folder, or ``None``."""
    projects = data.get("projects") if data is not None else None
    if not isinstance(projects, dict):
        return None
    for key in scope.covering:
        entry = projects.get(key)
        if isinstance(entry, dict) and entry.get(TRUST_KEY) is True:
            return key
    return None


def trusted_folders(data: Mapping[str, Any]) -> list[str]:
    """Every entry of a parsed ``.claude.json`` that records a yes, in the file's order."""
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return []
    return [
        key
        for key, entry in projects.items()
        if isinstance(key, str) and isinstance(entry, dict) and entry.get(TRUST_KEY) is True
    ]


def read_claude_json(path: Path) -> dict[str, Any]:
    """The ``.claude.json`` at ``path``, parsed — or :class:`SharingError` saying why not."""
    return _parse(_read(path), "its .claude.json")


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        raise NotStartedYet("Claude Code has never started there (no .claude.json)") from None
    except OSError as exc:
        raise SharingError(f"its .claude.json cannot be read ({exc.strerror or exc})") from exc


def _parse(raw: bytes, what: str) -> dict[str, Any]:
    try:
        data: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise SharingError(f"{what} is not valid JSON") from None
    if not isinstance(data, dict):
        raise SharingError(f"{what} is not a JSON object")
    return data


def grant_trust(path: Path, *keys: str) -> list[str]:
    """Record a yes for each of ``keys`` in the ``.claude.json`` at ``path``; those written.

    A key whose entry already says yes is left out. Only ``projects[key].hasTrustDialogAccepted``
    changes: the file is read under Claude Code's own lock (:func:`_config_lock`), those
    values set, and the rest written back the way Claude Code writes it — two-space JSON,
    no newline at the end — so every other key keeps its bytes. One write, replaced by
    rename, through a symlink as Claude Code replaces it. A file that is missing,
    unreadable or not a JSON object is left as it is, with :class:`SharingError` saying
    why: never created here, and never overwritten with what this could make of it.
    """
    with _config_lock(path):
        target = Path(os.path.realpath(path))
        raw = _read(target)
        data = _parse(raw, "its .claude.json")
        projects = data.setdefault("projects", {})
        if not isinstance(projects, dict):
            raise SharingError("its .claude.json has a 'projects' that is not an object")
        written: list[str] = []
        for key in dict.fromkeys(keys):
            entry = projects.get(key)
            if entry is None:
                projects[key] = {TRUST_KEY: True}
            elif not isinstance(entry, dict):
                raise SharingError(f"its .claude.json has an entry for {key} that is not an object")
            elif entry.get(TRUST_KEY) is True:
                continue
            else:
                entry[TRUST_KEY] = True
            written.append(key)
        if not written:
            return []
        body = _LONE_SURROGATE.sub(_escaped, json.dumps(data, indent=2, ensure_ascii=False))
        if raw.endswith(b"\n"):  # hand-edited: kept as found
            body += "\n"
        try:
            atomic.write_replacing(target, body.encode("utf-8"))
        except (OSError, UnicodeError) as exc:
            raise SharingError(f"its .claude.json could not be written ({exc})") from exc
    return written


_LONE_SURROGATE = re.compile("[\ud800-\udfff]")
"""Half of a surrogate pair, alone: what ``json.loads`` makes of the ``"\\ud83d"`` Node's
``JSON.stringify`` writes for a string cut between the two halves (a history entry cut
mid-emoji). UTF-8 cannot hold it, so it is written back as the escape it was read from —
only ever inside a string, so the JSON stays valid (review of #259, round 21)."""


def _escaped(match: re.Match[str]) -> str:
    return f"\\u{ord(match.group()):04x}"


@contextlib.contextmanager
def _config_lock(path: Path) -> Iterator[None]:
    """Claude Code's own lock on its config: the directory ``<path>.lock`` (``proper-lockfile``).

    Taken as Claude Code takes it, so a session saving at the same moment waits for this
    write and then re-reads the file with the entry in it, rather than both writing what
    each read. A lock older than :data:`LOCK_STALE_SECONDS` is a dead process's and is
    taken over, as ``proper-lockfile`` does; a live one is waited for, up to
    :data:`LOCK_WAIT_SECONDS`. Named after the file's resolved path, as ``proper-lockfile``
    names it by default (``realpath: true``): a linked ``.claude.json`` is locked beside
    the file it points at, under the one name Claude Code locks it by.
    """
    real = Path(os.path.realpath(path))
    lock = real.with_name(real.name + ".lock")
    deadline = _monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            os.mkdir(lock)
            break
        except FileExistsError:
            if _stale(lock) and _removed(lock):
                continue
            if _monotonic() >= deadline:
                raise SharingError(
                    "a running Claude Code held its .claude.json lock past "
                    f"{LOCK_WAIT_SECONDS:.0f} s"
                ) from None
            _sleep(_LOCK_POLL)
        except OSError as exc:
            raise SharingError(
                f"its .claude.json cannot be locked ({exc.strerror or exc})"
            ) from exc
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            os.rmdir(lock)


def _stale(lock: Path) -> bool:
    try:
        return time.time() - lock.stat().st_mtime > LOCK_STALE_SECONDS
    except OSError:  # released meanwhile, or not ours to ask: not a dead holder's
        return False


def _removed(lock: Path) -> bool:
    """Take a dead holder's lock away; whether it is gone (it may not be empty, or ours)."""
    try:
        os.rmdir(lock)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


# --- the question on screen -------------------------------------------------------------

_SCREEN_ESCAPES = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\|$)"  # OSC: the dialog's "Security guide" is a link
    r"|\x1b\[[0-?]*[ -/]*[@-~]"  # CSI, SGR included
)
_TRUST_QUESTION = re.compile(r"\btrust (?:this folder|the files in this folder)\b", re.IGNORECASE)
_TRUST_EXIT = re.compile(r"(?:\u276f ?|> ?)?(?:\d+\. ?)?No, exit")
"""The dialog's "No, exit" option, a row of its own: its pointer when highlighted, its
number on the 1.x builds."""
_INPUT_BOX = re.compile(r"[\u276f>](?! ?(?:\d+\. ?)?(?:Yes|No)\b).*")
"""An input box's row: its pointer, with nothing, a placeholder or what is typed after it —
never one of the dialog's options."""
_BORDERS = "│┃║╭╮╰╯┌┐└┘─━═ "
"""What a box draws around a row: stripped, so a bordered dialog's option is a row of its
own, and a row of nothing else is no row."""


def shows_trust_prompt(lines: Iterable[str]) -> bool:
    """Whether a captured screen is, or may be, Claude Code's folder-trust question.

    2.1.299 draws "Yes, I trust this folder" under a highlighted "No, exit", over its key
    hint; the 1.x builds asked "Do you trust the files in this folder?" over a numbered
    "2. No, exit", in a rounded box. Read fail-closed, since a screen read as not the
    question is typed into, and Enter there picks "No, exit": the question's words on the
    screen and "No, exit" an option of its own (box borders aside), with no input box under
    it. At work, the screen ends on its input box (:data:`_INPUT_BOX`), and the dialog
    takes the input box's place, so the words above one are a reviewer reading this module
    or the captured screen the tests pin (review of #259, round 14). Anything else that
    holds both reads as the question — a dialog in a box, a key hint wrapped in a narrow
    pane, a row the build adds under it — rather than a fixed count of rows at the foot,
    which each of those pushed "No, exit" out of (round 15). A Claude Code string, not a
    contract: pinned by a test with the captured screen.
    """
    rows = [
        row
        for line in lines
        if (row := " ".join(_SCREEN_ESCAPES.sub("", line).split()).strip(_BORDERS))
    ]
    exits = [at for at, row in enumerate(rows) if _TRUST_EXIT.fullmatch(row)]
    if not exits or not _TRUST_QUESTION.search(" ".join(rows)):
        return False
    return not any(_INPUT_BOX.fullmatch(row) for row in rows[exits[-1] + 1 :])


# --- one settings.json ------------------------------------------------------------------

SETTINGS_FILE = "settings.json"

LOGIN_SETTINGS = (
    "apiKeyHelper",
    "forceLoginMethod",
    "forceLoginOrgUUID",
    "awsAuthRefresh",
    "awsCredentialExport",
)
"""Top-level settings that decide which login or credentials a session uses."""

LOGIN_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_TMPDIR",
)
"""Variables a settings file's ``env`` may set that replace the account's own login (a key,
a token, a cloud provider) or the account itself (the two variables an account IS)."""

SettingsState = Literal["shared", "own", "none"]


def settings_path(account: ClaudeAccount) -> Path:
    return account.config_dir / SETTINGS_FILE


def login_keys(data: Mapping[str, Any]) -> list[str]:
    """The settings in a parsed settings file that decide a login, as ``env.X`` or ``key``."""
    found = [key for key in LOGIN_SETTINGS if key in data]
    env = data.get("env")
    if isinstance(env, dict):
        found += [f"env.{name}" for name in LOGIN_ENV if name in env]
    return found


def read_settings(path: Path, what: str) -> dict[str, Any]:
    """The settings file at ``path`` parsed — :class:`SharingError` naming it as ``what``."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SharingError(f"{what} cannot be read ({exc.strerror or exc})") from exc
    return _parse(raw, what)


def settings_state(account: ClaudeAccount, source: Path) -> SettingsState:
    """``shared`` when the account's settings.json is a link to ``source``, ``own`` when it is
    any other file or link, ``none`` when it has none."""
    path = settings_path(account)
    if os.path.islink(path) and os.path.realpath(path) == os.path.realpath(source):
        return "shared"
    return "own" if os.path.lexists(path) else "none"


def link_settings(account: ClaudeAccount, source: Path, *, now: datetime) -> Path | None:
    """Make ``account``'s settings.json a link to ``source``; the copy of its own it kept.

    The account's own file is copied beside it first, as ``settings.json.own-<UTC stamp>``
    (:func:`_keep_beside`: ``-2``, ``-3`` and on within one second, never over a copy kept
    before), then the link is renamed over it in one step: a session starting meanwhile
    reads the old file or the shared one, never none. Refused (:class:`SharingError`) when its own
    file sets what decides its login (:func:`login_keys`), since the link would drop it.
    An own file that is not JSON is copied and replaced: Claude Code reads nothing from it.
    ``None`` when there was nothing to keep. The caller checks ``source`` itself.
    """
    target = settings_path(account)
    backup: Path | None = None
    if os.path.isfile(target):
        try:
            own: dict[str, Any] | None = read_settings(target, "its own settings.json")
        except SharingError:
            own = None
        login = login_keys(own) if own is not None else []
        if login:
            raise SharingError(
                f"its own settings.json sets {', '.join(login)}, which decide its login"
            )
        backup = _keep_beside(target, f"{SETTINGS_FILE}.own-{now:%Y%m%dT%H%M%SZ}")
    temporary = target.with_name(f".{SETTINGS_FILE}.link-{os.getpid()}")
    with contextlib.suppress(FileNotFoundError):
        temporary.unlink()
    os.symlink(source, temporary)
    try:
        os.replace(temporary, target)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    return backup


def _keep_beside(path: Path, name: str) -> Path:
    """Copy ``path`` beside itself as ``name``, or ``name-2``, ``name-3`` and on when one is
    there, as a removed slot is kept (``claude_accounts.remove_account``); the copy.

    Created exclusively, so a copy kept before is never written over. Named to the second
    and copied over whatever held the name, a sync, an unshare and a sync again within one
    second replaced the account's own settings with the shared ones it was given back
    (review of #259, round 32, 8/9)."""
    counter = 1
    while True:
        kept = path.with_name(name if counter == 1 else f"{name}-{counter}")
        try:
            with path.open("rb") as own, kept.open("xb") as copy:
                shutil.copyfileobj(own, copy)
        except FileExistsError:
            counter += 1
            continue
        shutil.copystat(path, kept)
        return kept


def unlink_settings(account: ClaudeAccount, source: Path) -> bool:
    """Give ``account`` its own copy of the shared settings.json again; whether it had a link.

    The link is replaced by a file holding what the shared one holds now, so the account
    keeps the same settings and stops following the plain claude's.
    """
    if settings_state(account, source) != "shared":
        return False
    atomic.write_replacing(settings_path(account), Path(source).read_bytes())
    return True


# --- skills, agents, commands, CLAUDE.md, plugins: one copy, linked (P2-W7, #198) ---------

SharePart = Literal["memory", "skills", "agents", "commands", "claude_md", "plugins"]
SHARE_PARTS: tuple[SharePart, ...] = (
    "memory",
    "skills",
    "agents",
    "commands",
    "claude_md",
    "plugins",
)
"""What ``[accounts] share`` can name. ``plugins`` is experimental: MCP auth and the plugin
store are per directory, so a linked ``plugins/`` can carry a login one slot made."""

LINKED_PARTS: dict[SharePart, tuple[str, bool]] = {
    "skills": ("skills", True),
    "agents": ("agents", True),
    "commands": ("commands", True),
    "claude_md": ("CLAUDE.md", False),
    "plugins": ("plugins", True),
}
"""A linked part's entry in a config directory, and whether it is a directory."""

PartState = Literal["shared", "own", "none", "elsewhere"]

#: ``os.symlink`` and, on Windows where that needs a privilege, a junction for a directory:
#: injected by tests, the junction path through CPython's own ``_winapi.CreateJunction``.
_platform: Callable[[], str] = lambda: os.name  # noqa: E731


def part_entry(config_dir: Path, part: SharePart) -> Path:
    return config_dir / LINKED_PARTS[part][0]


def part_state(config_dir: Path, part: SharePart, source_dir: Path) -> PartState:
    """``shared`` when the entry links to the plain claude's, ``elsewhere`` when it links to
    anything else, ``own`` when it is a real file or directory, ``none`` when absent."""
    entry = part_entry(config_dir, part)
    if _is_link(entry):
        same = os.path.realpath(entry) == os.path.realpath(part_entry(source_dir, part))
        return "shared" if same else "elsewhere"
    return "own" if os.path.lexists(entry) else "none"


def _is_link(path: Path) -> bool:
    """A symlink, or on Windows a junction (which ``islink`` does not see)."""
    if os.path.islink(path):
        return True
    is_junction = getattr(os.path, "isjunction", None)  # 3.12+
    return bool(is_junction is not None and is_junction(path))


def link_part(config_dir: Path, part: SharePart, source_dir: Path, *, now: datetime) -> Path | None:
    """Make ``config_dir``'s entry for ``part`` the plain claude's; the copy of its own kept.

    Move-then-link: the slot's own file or directory is RENAMED beside itself as
    ``<entry>.own-<UTC stamp>`` (never written over: ``-2``, ``-3`` on), then the link
    is made. The plain claude's entry is created empty when it has none (a directory),
    so the slot follows whatever is added there later; a ``CLAUDE.md`` it has none of is
    refused instead, since an empty one would hide the slot's own. Already shared: a
    no-op, ``None``. An entry linked anywhere else is the operator's and is refused
    (:class:`SharingError`). Windows: a junction for a directory, and a ``CLAUDE.md`` is
    COPIED (no link without a privilege): it stops following the plain claude's.
    """
    entry = part_entry(config_dir, part)
    source = part_entry(source_dir, part)
    state = part_state(config_dir, part, source_dir)
    if state == "shared":
        return None
    if state == "elsewhere":
        raise SharingError(f"its {entry.name} links to {os.readlink(entry)} — not ours to replace")
    is_dir = LINKED_PARTS[part][1]
    if is_dir:
        source.mkdir(parents=True, exist_ok=True)
        if state == "own" and entry.is_dir():
            # What the slot has and the plain claude lacks goes there first, never over an
            # entry it has: linked to an empty directory, a slot's own skills vanished from
            # it (review of #360). Its own stays kept beside it as well.
            _seed(entry, source)
    elif not source.is_file():
        raise SharingError(f"the plain claude has no {source.name} to share")
    kept: Path | None = None
    if state == "own":
        kept = _free_beside(entry, f"{entry.name}.own-{now:%Y%m%dT%H%M%SZ}")
        os.replace(entry, kept)
    try:
        _make_link(source, entry, is_dir=is_dir)
    except OSError:
        if kept is not None:  # put it back: the slot keeps what it had
            os.replace(kept, entry)
        raise
    return kept


def _seed(own: Path, shared: Path) -> None:
    """Copy each entry of ``own`` that ``shared`` has no entry of that name for."""
    for item in sorted(own.iterdir()):
        target = shared / item.name
        if target.exists() or target.is_symlink():
            continue
        if item.is_dir() and not item.is_symlink():
            shutil.copytree(item, target, symlinks=True)
        else:
            shutil.copy2(item, target, follow_symlinks=False)


def unlink_part(config_dir: Path, part: SharePart, source_dir: Path) -> Path | None:
    """Undo :func:`link_part`: remove the link and put the newest kept copy back; the copy
    restored, or ``None`` when the slot had none of its own (or was not linked)."""
    entry = part_entry(config_dir, part)
    if part_state(config_dir, part, source_dir) != "shared":
        return None
    _remove_link(entry)
    kept = sorted(
        entry.parent.glob(f"{entry.name}.own-*"), key=lambda p: (p.stat().st_mtime, p.name)
    )
    if not kept:
        return None
    os.replace(kept[-1], entry)
    return entry


def _free_beside(path: Path, name: str) -> Path:
    counter = 1
    while True:
        candidate = path.with_name(name if counter == 1 else f"{name}-{counter}")
        if not os.path.lexists(candidate):
            return candidate
        counter += 1


def _make_link(source: Path, entry: Path, *, is_dir: bool) -> None:
    try:
        os.symlink(source, entry, target_is_directory=is_dir)
        return
    except OSError:
        if _platform() != "nt":
            raise
    if is_dir:
        _junction(source, entry)
    else:
        shutil.copy2(source, entry)


def _junction(source: Path, entry: Path) -> None:
    """A directory junction (Windows): no privilege needed, unlike a symlink."""
    import importlib

    # CPython's own Windows module, by name: it exists nowhere else, so the type checker
    # on another platform sees no such attribute.
    winapi: Any = importlib.import_module("_winapi")
    winapi.CreateJunction(str(source), str(entry))


def _remove_link(entry: Path) -> None:
    if os.path.isdir(entry) and not os.path.islink(entry):
        os.rmdir(entry)  # a junction: rmdir removes the link, never the target's contents
    else:
        os.unlink(entry)


# --- memory: one directory per project, outside every .claude path (P2-W7) -----------------

MEMORY_INDEX = "MEMORY.md"


def memory_key(cwd: Path) -> str:
    """The project key Claude Code files a folder's transcripts and memory under:
    ``projects/<key>/``, the folder's path with every character but a letter or a digit a
    ``-`` (measured: ``/private/tmp/x.y`` is ``-private-tmp-x-y``)."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def merge_memory(sources: Iterable[tuple[str, Path]], target: Path) -> list[tuple[str, str]]:
    """Merge each ``(label, memory dir)`` into ``target``: nothing is ever overwritten.

    A file ``target`` lacks is copied; one with the same bytes is skipped; one that
    differs is kept as ``<stem>.<label><suffix>`` beside it. ``MEMORY.md`` (the index
    Claude Code reads first) is merged by line, each line once, in first-seen order.
    Returns ``(label, note)`` for each file kept under another name.
    """
    notes: list[tuple[str, str]] = []
    target.mkdir(parents=True, exist_ok=True)
    for label, source in sources:
        if not source.is_dir():
            continue
        for item in sorted(source.rglob("*")):
            if not item.is_file():
                continue
            relative = item.relative_to(source)
            dest = target / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            if relative.as_posix() == MEMORY_INDEX:
                _merge_lines(item, dest)
                continue
            if not dest.exists():
                shutil.copy2(item, dest)
            elif dest.read_bytes() != item.read_bytes():
                kept = _kept_name(dest, label, item.read_bytes())
                if kept is not None:
                    shutil.copy2(item, kept)
                    notes.append((label, f"{relative} differed: kept as {kept.name}"))
    return notes


def _kept_name(dest: Path, label: str, body: bytes) -> Path | None:
    """Where a differing ``dest`` from ``label`` is kept: ``<stem>.<label><suffix>``, then
    ``<stem>.<label>-2<suffix>`` and on, so a note stays a ``.md`` Claude Code reads (review
    of #360: ``note.slotb.md-2``); ``None`` when one of them already holds these bytes."""
    counter = 1
    while True:
        tag = label if counter == 1 else f"{label}-{counter}"
        candidate = dest.with_name(f"{dest.stem}.{tag}{dest.suffix}")
        if not candidate.exists():
            return candidate
        if candidate.read_bytes() == body:
            return None
        counter += 1


def _merge_lines(source: Path, dest: Path) -> None:
    """``source``'s lines ``dest`` lacks, after it, each once; blank lines are the index's
    layout and are kept, not deduplicated away (review of #360)."""
    have = dest.read_text(encoding="utf-8").splitlines() if dest.exists() else []
    seen = {line for line in have if line.strip()}
    added: list[str] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if line.strip():
            if line in seen:
                continue
            seen.add(line)
        added.append(line)
    while added and not added[0].strip():
        added.pop(0)
    while added and not added[-1].strip():
        added.pop()
    if added or not dest.exists():
        lines = have + added
        dest.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
