"""What several Codex accounts share: the folders the human trusted, in each ``config.toml``.

Every Codex account is its own ``CODEX_HOME`` (``core.codex_accounts``), and Codex asks
"Trust this folder?" once per home: a yes on account 2 is nothing to account 3, whose agent
then sits at the question until a human answers it. The yes is a table in THAT home's
``config.toml``, which also holds the account's own settings, so the table that covers a
folder is COPIED from the account that has it (:func:`grant_trust`), appended, and nothing
else in the file is touched. The login is ``auth.json``, never this file.

Measured with Codex 0.162.1 in scratch homes (``CODEX_HOME``) on a loopback provider
(2026-10-19):

- it asks only in a git checkout, and a yes records the REPOSITORY ROOT, the main
  checkout's from a sub-folder or a worktree alike ("Trusting will apply to the repository
  root"), as ``[projects."<root>"]`` / ``trust_level = "trusted"``, appended after a blank
  line (Codex edits the file in place: every other table and comment is kept);
- a folder is decided by the entry on itself, else the one on its repository's canonical
  root, and by nothing else: not an entry on a folder above it (a parent, a folder above
  the repository), nor one on a worktree for the main checkout;
- ``trust_level = "untrusted"`` is a decision too: the folder's own entry saying it holds
  the start at another dialog ("Folder access … Open restricted"), whatever the root's
  says, and its own ``trusted`` wins over an ``untrusted`` root;
- the key is a REAL path: an entry under a symlinked spelling does not count, and a folder
  reached through a link is looked up by its resolved path;
- there is no lock: Codex reads the file, edits it and writes it back.

So the folder's scope is ``core.claude_sharing.trust_scope``'s ``folder`` and ``key``, in
Codex's spelling (:func:`scope`). An entry that says ``untrusted`` is the human's decision
on that account and is never overridden.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any

from aisquare.core import atomic
from aisquare.core.claude_sharing import SharingError, TrustScope
from aisquare.models import ClaudeAccount

CONFIG_FILE = "config.toml"
TRUSTED = "trusted"
UNTRUSTED = "untrusted"


def config_path(home: Path) -> Path:
    return home / CONFIG_FILE


def native_key(key: str) -> str:
    """``key`` (``core.claude_sharing.project_key``'s spelling) as Codex spells a
    ``projects`` key: the path as the platform writes it."""
    return str(Path(key))


def scope(base: TrustScope) -> TrustScope:
    """``base`` in Codex's rules: the entry on the folder itself, then the one a yes there
    records (the canonical repository root, else the folder)."""
    key = native_key(base.key)
    return TrustScope(base.folder, key, tuple(dict.fromkeys((str(base.folder), key))))


def read_trust(home: Path) -> dict[str, bool]:
    """``{key: trusted}`` for every ``projects`` table of the home's ``config.toml`` that
    says ``trusted`` or ``untrusted``, in the file's order; ``{}`` with no file (Codex
    runs without one, and makes it on its first yes). :class:`SharingError` when it
    cannot be read or is not TOML."""
    data = _parse(_read(config_path(home)))
    projects = _projects(data)
    found: dict[str, bool] = {}
    for key, entry in projects.items():
        level = entry.get("trust_level") if isinstance(entry, dict) else None
        if level in (TRUSTED, UNTRUSTED):
            found[key] = level == TRUSTED
    return found


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""
    except OSError as exc:
        raise SharingError(f"its {CONFIG_FILE} cannot be read ({exc.strerror or exc})") from exc


def _parse(raw: bytes) -> dict[str, Any]:
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise SharingError(f"its {CONFIG_FILE} is not valid TOML") from None


def _projects(data: dict[str, Any]) -> dict[str, Any]:
    projects = data.get("projects", {})
    if not isinstance(projects, dict):
        raise SharingError(f"its {CONFIG_FILE} has a 'projects' that is not a table")
    return projects


def grant_trust(home: Path, *keys: str) -> list[str]:
    """Record a yes for each of ``keys`` in the home's ``config.toml``; those written.

    A key already trusted is left out. Each is a ``[projects."<key>"]`` table of
    ``trust_level = "trusted"``, APPENDED after a blank line as Codex appends its own, so
    every byte before it is kept: never prepended, where the file's top-level settings
    would become the table's. The file is made when there is none. A key with a table of
    its own already (``untrusted``, the human's no, or no ``trust_level``) is refused, as
    a second table for it would make the file invalid; so is a file the tables cannot be
    appended to as such (``projects`` written inline). The result is parsed before it is
    written, and must be the file as it was plus these entries; one write, replaced by
    rename, through a symlink. :class:`SharingError` says why the file was left as it was.
    """
    target = Path(os.path.realpath(config_path(home)))
    raw = _read(target)
    before = _parse(raw)
    projects = _projects(before)
    wanted: list[str] = []
    for key in dict.fromkeys(keys):
        entry = projects.get(key)
        if entry is None:
            wanted.append(key)
        elif not isinstance(entry, dict):
            raise SharingError(f"its {CONFIG_FILE} has an entry for {key} that is not a table")
        elif entry.get("trust_level") != TRUSTED:
            said = entry.get("trust_level")
            raise SharingError(
                f"its {CONFIG_FILE} has a table for {key} already"
                + (f" (trust_level = {json.dumps(said)})" if isinstance(said, str) else "")
                + ' — set trust_level = "trusted" there if you trust it'
            )
    if not wanted:
        return []
    text = raw.decode("utf-8")
    if text and not text.endswith("\n"):
        text += "\n"
    for key in wanted:
        header = json.dumps(key, ensure_ascii=False)
        text += ("\n" if text else "") + f'[projects.{header}]\ntrust_level = "{TRUSTED}"\n'
    try:
        body = text.encode("utf-8")
        after = tomllib.loads(text)
    except (UnicodeError, tomllib.TOMLDecodeError):
        raise SharingError(f"its {CONFIG_FILE} cannot take a [projects] table at its end") from None
    expected = {
        **before,
        "projects": {**projects, **{key: {"trust_level": TRUSTED} for key in wanted}},
    }
    if after != expected:
        raise SharingError(f"its {CONFIG_FILE} would read otherwise with the entry appended")
    try:
        atomic.write_replacing(target, body)
    except OSError as exc:
        raise SharingError(f"its {CONFIG_FILE} could not be written ({exc})") from exc
    return wanted


class CodexFolderTrust:
    """Codex's answers to the accounts slot's ``trust`` (``agent_adapters.types.FolderTrust``):
    each account's ``config.toml``, by the rules above."""

    file_name = CONFIG_FILE

    def scope(self, base: TrustScope) -> TrustScope:
        return scope(base)

    def read(self, account: ClaudeAccount) -> dict[str, bool]:
        return read_trust(account.config_dir)

    def grant(self, account: ClaudeAccount, *keys: str) -> list[str]:
        return grant_trust(account.config_dir, *keys)
