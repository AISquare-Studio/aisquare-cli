"""Filesystem layout of the ``~/.aisquare`` home directory.

Layout:
    ~/.aisquare/
    ├── config.toml     # typed TOML configuration (see core.config)
    ├── credentials     # API keys / tokens
    ├── context.db      # SQLite store: context entries and projects (see core.store)
    ├── agents.json     # registry of detected and connected agents
    ├── claude-accounts/# one CLAUDE_CONFIG_DIR per managed account (core.claude_accounts)
    ├── cache/          # disposable cached data (incl. each managed account's TMPDIR)
    ├── explainability/ # session→Run join records (see services.explainability)
    └── log/            # capture and diagnostic logs

Set ``AISQUARE_HOME`` to relocate the whole tree (tests rely on this).
"""

from __future__ import annotations

import errno
import functools
import os
import stat
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

_T = TypeVar("_T")

HOME_ENV_VAR = "AISQUARE_HOME"
"""Environment variable that overrides the default ``~/.aisquare`` location."""


def restrict_to_owner(path: Path) -> bool:
    """Make ``path`` readable and writable by its owner only. True when enforced.

    ``chmod(0o600)`` is the whole story on POSIX, and *nothing* on Windows:
    the group/other bits have no NTFS equivalent, so ``os.chmod`` silently
    leaves the file readable by every other account on the machine. Since the
    two callers are an API key and a bearer token, "silently" is the problem.

    ONE icacls call, and each piece of it is load-bearing. ``/inheritance:r``
    removes only *inherited* entries and ``/grant:r`` replaces the grant only
    for the principal it names, so an **explicit** ``BUILTIN\\Users`` ACE —
    inherited from a widened parent at creation time, or set by hand — survives
    both and leaves the file readable by every account on the box. ``/remove``
    is what drops an explicit ACE, and it names the three broad principals by
    SID rather than by display name, which is localised.

    This used to be ``/reset`` followed by a second call, and the pair was not
    atomic. ``/reset`` discards the explicit entries by RESTORING INHERITANCE
    FROM THE PARENT — so between the two calls the file sat on the parent's
    DACL with the secret already written into it, readable by whoever that
    parent grants, for two ``CreateProcess`` calls' worth of time. Worse, a
    failure of the second call left the file *wider than before this function
    was called*, because the first had already thrown away the owner-only DACL
    a previous call had set: a regression reported as ``False`` rather than a
    no-op. One call cannot half-apply.

    The trade is stated rather than hidden: ``/remove`` drops the three ACEs it
    names, where ``/reset`` dropped every explicit ACE. A file carrying an
    explicit grant to some OTHER principal — a domain group, a service account,
    a second local user — keeps it. So the DACL the call left is READ BACK, in
    process, and any principal still granted anything other than this account,
    SYSTEM, Administrators and the owner placeholders makes the answer False:
    the file is not what the callers promise, and they say so (review of #65,
    R3). The read changes nothing, so it cannot widen what the call narrowed.
    ``tests/test_paths.py`` pins both halves: the fourth principal survives,
    and it is reported.

    The trustee is a SID from ``whoami``, not ``getpass.getuser()``. CPython
    returns the first set of ``LOGNAME``, ``USER``, ``LNAME``, ``USERNAME``
    before asking the OS, and the first three are set by MSYS2, Git Bash and
    anything sourcing a POSIX profile. With ``USER=alice`` and a Windows
    account of ``CORP\\a.smith``, ``icacls /grant:r alice:(R,W)`` fails with
    "No mapping between account names and security IDs was done" — which, in
    the old two-call shape, failed *after* the reset had stripped the DACL. A
    SID also sidesteps localised names and domain qualification.

    An ``Administrators`` entry can remain when the parent grants one, which is
    not worth chasing: an admin can take ownership regardless, exactly as root
    reads a 0600 file on POSIX.

    Returns False when the restriction could not be applied, so a caller can
    say so rather than implying a protection that is not there.
    """
    if sys.platform != "win32":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return True
    # Imported HERE, not at module scope: `subprocess` pulls `signal`,
    # `threading`, `select`, `contextlib` and `warnings`, and this module is a
    # leaf that almost everything imports on a cold start. That is measurable
    # cost on `aisquare --version` on BOTH platforms to buy something only
    # Windows uses. The repo already treats this as a rule worth a test —
    # tests/test_iam_single_reader.py and test_import_cost_of_the_integration.py.
    import subprocess

    sid = _current_user_sid()
    if sid is None:
        return False
    argv = [
        _system32("icacls.exe"),
        str(path),
        "/inheritance:r",
        # Users, Everyone, Authenticated Users — by SID, because the display
        # names are localised and would not match on a non-English Windows.
        "/remove",
        "*S-1-5-32-545",
        "*S-1-1-0",
        "*S-1-5-11",
        "/grant:r",
        f"*{sid}:(R,W)",
    ]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    sddl = _dacl_sddl(path)
    return sddl is not None and _grants_only_owner(sddl, sid)


def _system32(program: str) -> str:
    """``program`` in ``%SystemRoot%\\System32``, by its full path.

    Run by bare name, ``CreateProcess`` looks in the application's directory
    and the CURRENT directory before System32. An ``icacls.exe`` or
    ``whoami.exe`` planted in a project would run instead, and a fake
    ``whoami`` could turn the owner's grant into one for Everyone (review of
    #65, R8).
    """
    return str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / program)


def _current_user_sid() -> str | None:
    """This account's SID from ``whoami``, or ``None`` when it cannot be read.

    ``tests/winacl.py`` reads SIDs the same way and for the same reason — a
    name-based check would be the same vacuous pass one level down.
    """
    import subprocess  # Windows-only; see restrict_to_owner

    try:
        return _whoami_sid()
    except (OSError, subprocess.TimeoutExpired):
        return None


@functools.cache
def _whoami_sid() -> str:
    """The SID ``whoami`` names for this account; raises when it names none.

    Cached: the account cannot change under a running process, and every
    restriction otherwise cost a second subprocess (review of #65, R8). A
    failure RAISES rather than returning ``None``, because an exception is
    never cached, so a ``whoami`` that timed out once is asked again next time.
    """
    import subprocess  # Windows-only; see restrict_to_owner

    result = subprocess.run(
        [_system32("whoami.exe"), "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        raise OSError(f"whoami exited {result.returncode}: {result.stderr.strip()}")
    # '"domain\\user","S-1-5-21-..."'
    sid = result.stdout.strip().split(",")[-1].strip().strip('"')
    if not sid:
        raise OSError(f"whoami named no SID: {result.stdout.strip()!r}")
    return sid


#: Trustees an owner-only file may still grant, as SDDL writes them (an
#: abbreviation, or the SID on a host that spells it out): SYSTEM,
#: Administrators, OWNER RIGHTS and CREATOR OWNER. An administrator can take
#: ownership of any file regardless, the same deal 0600 offers against root.
_PRIVILEGED_TRUSTEES = frozenset(
    {"SY", "S-1-5-18", "BA", "S-1-5-32-544", "OW", "S-1-3-4", "CO", "S-1-3-0"}
)

#: SDDL abbreviates two ACCOUNT SIDs by their RID, so this account can come
#: back as one of these instead of spelled out: the built-in Administrator (a
#: GitHub runner's login) and Guest.
_ACCOUNT_ABBREVIATIONS = {"LA": "-500", "LG": "-501"}

#: ACE types that deny. Every other type in a DACL grants something.
_DENYING_ACES = frozenset({"D", "OD", "XD"})


def _grants_only_owner(sddl: str, sid: str) -> bool:
    """Whether the DACL in ``sddl`` grants nobody but ``sid`` and the privileged trustees.

    Each ACE is ``(type;flags;rights;object;inherited object;trustee)``. A
    deny ACE narrows and is skipped. An ACE this cannot read (a conditional
    one carries a nested expression) counts as a grant to someone else: a
    restriction this cannot vouch for is not reported as one. A NULL DACL
    grants everyone everything.
    """
    if "NO_ACCESS_CONTROL" in sddl:
        return False
    for ace in sddl.split("(")[1:]:
        fields = ace.split(")", 1)[0].split(";")
        if len(fields) != 6:
            return False
        kind, trustee = fields[0], fields[5]
        if kind in _DENYING_ACES or trustee in _PRIVILEGED_TRUSTEES or trustee == sid:
            continue
        suffix = _ACCOUNT_ABBREVIATIONS.get(trustee)
        if suffix is None or not sid.endswith(suffix):
            return False
    return True


def _dacl_sddl(path: Path) -> str | None:
    """``path``'s DACL as SDDL, read in process; ``None`` when it cannot be read.

    Through ``advapi32`` rather than ``icacls``: ``icacls <path>`` prints
    display names, which are localised and contain spaces, and a read that
    costs no subprocess keeps the restriction at one.
    """
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_security = advapi32.GetNamedSecurityInfoW
    get_security.argtypes = [
        wintypes.LPCWSTR,  # pObjectName
        ctypes.c_int,  # ObjectType: SE_FILE_OBJECT
        wintypes.DWORD,  # SecurityInfo
        ctypes.POINTER(ctypes.c_void_p),  # ppsidOwner
        ctypes.POINTER(ctypes.c_void_p),  # ppsidGroup
        ctypes.POINTER(ctypes.c_void_p),  # ppDacl
        ctypes.POINTER(ctypes.c_void_p),  # ppSacl
        ctypes.POINTER(ctypes.c_void_p),  # ppSecurityDescriptor
    ]
    get_security.restype = wintypes.DWORD
    to_sddl = advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
    to_sddl.argtypes = [
        ctypes.c_void_p,  # SecurityDescriptor
        wintypes.DWORD,  # RequestedStringSDRevision
        wintypes.DWORD,  # SecurityInformation
        ctypes.POINTER(wintypes.LPWSTR),  # StringSecurityDescriptor
        ctypes.POINTER(wintypes.ULONG),  # StringSecurityDescriptorLen
    ]
    to_sddl.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    se_file_object, dacl_security_information, sddl_revision_1 = 1, 0x4, 1
    descriptor = ctypes.c_void_p()
    status = get_security(
        str(path),
        se_file_object,
        dacl_security_information,
        None,
        None,
        None,
        None,
        ctypes.byref(descriptor),
    )
    if status != 0:
        return None
    try:
        text = wintypes.LPWSTR()
        if not to_sddl(
            descriptor, sddl_revision_1, dacl_security_information, ctypes.byref(text), None
        ):
            return None
        try:
            return text.value
        finally:
            local_free(ctypes.cast(text, ctypes.c_void_p))
    finally:
        local_free(descriptor)


def aisquare_home() -> Path:
    """Return the aisquare home directory (without creating it).

    The path is taken verbatim and nothing constrains it to a native disk. This
    is the one place that choice is made, so its consequence is recorded here
    rather than only beside the code that suffers it.

    ``core.config.save_config`` publishes changes with the durable-replace
    recipe — sibling temp, fsync, ``os.replace`` over the target, fsync the
    parent. Two of its properties come from the FILESYSTEM, not from our code:
    the replace is atomic, so a concurrent reader sees the whole old file or the
    whole new one and never a partial document; and the fsyncs cost about what a
    local disk costs, measured at +2.15 ms median for the directory flush.

    Both were measured on a native disk, where the default ``~/.aisquare``
    lives. NEITHER IS MEASURED FOR A WINDOWS-BACKED MOUNT (WSL 9p/DrvFs,
    ``/mnt/c/...``), where a rename becomes a Windows operation and an fsync is
    a round trip to the host. ``AISQUARE_HOME=/mnt/c/...``, or a Windows-side
    HOME, puts config.toml exactly there.

    What is NOT in doubt on such a mount: the temp file is created in the
    TARGET'S OWN DIRECTORY, so temp and target always share a filesystem and the
    precondition ``os.replace`` needs holds by construction. The open question is
    narrower than "does this work on 9p" — it is whether that filesystem's
    rename carries the same whole-old-or-whole-new guarantee, and what an fsync
    costs there.

    Until that is measured, treat a Windows-backed AISQUARE_HOME as unsupported
    FOR THE ATOMICITY GUARANTEE SPECIFICALLY. Everything still functions; what
    has not been ruled out is a torn read during a concurrent write, which on a
    native disk has been.

    ``aisquare doctor`` now reports which filesystem this path is on, so an
    operator does not have to know any of the above to find out they are in the
    unmeasured case — see ``services.diagnostics._check_home_filesystem``. It
    reports rather than refuses: being on a translated filesystem is unmeasured,
    not known-broken.
    """
    override = os.environ.get(HOME_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".aisquare"


def cache_dir() -> Path:
    """Directory for disposable cached data."""
    return aisquare_home() / "cache"


def ci_cache_dir() -> Path:
    """Directory for the CI test bed's cached delivery descriptors.

    Descriptors only — the client keeps no cache of hook responses. The server
    caches and reports what it did in ``briefing.cache``; a second cache here
    would make a cached turn's timing describe a network call it never made.
    """
    return cache_dir() / "ci"


def ci_descriptor_path(run_id: str) -> Path:
    """Where the delivery descriptor for ``run_id`` is cached until it expires.

    The run id comes from the environment, so it is treated as a filename
    the way any outside string is: anything outside the id alphabet becomes
    ``_`` and the name is bounded, so no value can name a path outside the
    cache directory.
    """
    safe = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in run_id)[:96]
    return ci_cache_dir() / f"descriptor-{safe or 'unknown'}.json"


def log_dir() -> Path:
    """Directory for capture and diagnostic logs."""
    return aisquare_home() / "log"


def config_path() -> Path:
    """Path of the TOML config file."""
    return aisquare_home() / "config.toml"


def db_path() -> Path:
    """Path of the SQLite database holding context entries and projects."""
    return aisquare_home() / "context.db"


def last_injection_path() -> Path:
    """Path of the record describing the most recent context injection."""
    return cache_dir() / "last_injection.json"


def state_path() -> Path:
    """Path of the small runtime-state file (e.g. the pinned active project)."""
    return aisquare_home() / "state.json"


def project_data_dir(project_id: str) -> Path:
    """Per-project data directory (codebase snapshots, future sync artifacts)."""
    return aisquare_home() / "projects" / project_id


def credentials_path() -> Path:
    """Path of the credentials file (API keys, tokens)."""
    return aisquare_home() / "credentials"


def claude_accounts_dir() -> Path:
    """Where the CLI keeps the Claude Code config directories it owns, one per slot."""
    return aisquare_home() / "claude-accounts"


def claude_accounts_tmp_dir() -> Path:
    """Where each managed account's ``CLAUDE_CODE_TMPDIR`` lives — disposable, under cache."""
    return cache_dir() / "claude-accounts"


def agents_registry_path() -> Path:
    """Path of the agents registry (detected and connected agents)."""
    return aisquare_home() / "agents.json"


def explainability_dir() -> Path:
    """Directory for explainability artefacts written by the launcher."""
    return aisquare_home() / "explainability"


def explainability_joins_path() -> Path:
    """Path of the session→Run join log (JSON Lines, append-only).

    Deliberately NOT under ``cache/``: this is the only local copy of the key
    that joins board rows to gateway Runs, and ``cache/`` is documented as
    disposable.
    """
    return explainability_dir() / "joins.jsonl"


def truncation_marker_path() -> Path:
    """Records that ``context.db`` was found emptied and rebuilt.

    Deliberately NOT under ``cache/``: that directory is documented as
    disposable, and this is the only surviving evidence that a board's history
    was lost. It exists because the fact is knowable for exactly one line —
    ``open_store`` sees a zero-length file, and one statement later the schema is
    back and nothing can tell an emptied store from a new machine.

    Cleared by the operator, not by us. A warning that clears itself is one
    nobody has to answer.
    """
    return aisquare_home() / "store-was-truncated"


def ensure_home() -> Path:
    """Create the aisquare home layout if missing and return its root."""
    home = aisquare_home()
    for directory in (home, cache_dir(), log_dir()):
        directory.mkdir(parents=True, exist_ok=True)
    return home


# --- Windows file contention ---------------------------------------------------
#
# Here rather than in `core.config` because it is a FILESYSTEM fact, not a
# config one, and three modules want it: `config.save_config`/`load_config`,
# `credentials.load_all`, and `services.explainability.store_api_key`. All
# three already import this module, and `paths` imports nothing from
# `aisquare`, so it is the one place none of them has to reach sideways for.


#: Windows error codes meaning "someone else has this file open right now":
#: ERROR_ACCESS_DENIED and ERROR_SHARING_VIOLATION.
_WINDOWS_BUSY = frozenset({5, 32})


def _is_contention(exc: PermissionError) -> bool:
    """Whether ``exc`` is Windows saying "busy" rather than "you may not".

    The two sides report it DIFFERENTLY, which is worth writing down because
    matching only the obvious one silently disables half the retry:

    * ``os.replace`` raises through the Win32 layer and carries ``winerror``
      5 or 32.
    * ``Path.open`` raises through the C runtime, which sets ``errno`` 13 and
      leaves ``winerror`` as **None** — measured, 122 of 122 racing reads.

    A genuine "you may not read this" is indistinguishable from the second
    form, so it is retried too and then raised unchanged. That costs ~0.9s on a
    path that was going to fail anyway, and buys the reader case being covered
    at all.
    """
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        return winerror in _WINDOWS_BUSY
    return exc.errno == errno.EACCES


#: Backoff for :func:`despite_windows_contention`. Bounded on purpose — these
#: are operator commands, so a slow failure is nearly as bad as a wrong one.
#: Ten tries over ~0.9s clears the contention that actually occurs (both the
#: read and the rename hold the file for microseconds) without turning a
#: genuine permission problem into a hang.
_BUSY_ATTEMPTS = 10
_BUSY_BACKOFF_SECONDS = 0.02


def despite_windows_contention(action: Callable[[], _T]) -> _T:
    """Run ``action``, retrying while Windows reports the file as busy.

    On POSIX this is a plain call: a rename over an existing name always
    succeeds, and a reader that already has the file open keeps its own inode,
    so neither side can observe the other.

    NTFS shares no such guarantee, and BOTH sides of this module hit it:

    * ``MoveFileEx`` refuses to replace a file that any other handle has open —
      including one opened purely for reading — so a second session merely
      READING the config failed a write with a bare ``Access is denied``.
    * and for the width of that rename, opening the destination fails too, so
      the reader takes an ``Access is denied`` of its own.

    Measured directly, not inferred: a replace over a target held open for read
    raises WinError 5, and a as-fast-as-possible read/write storm produces both
    directions. The second one is the more expensive of the two, because
    ``cli/launch.py`` treats an unreadable config as "launch untraced" by
    design — so on Windows a config write racing a launch silently cost
    tracing, with nothing raised anywhere to say so.

    Every window here is microseconds wide, which is what makes retrying the
    right remedy rather than a papering-over. The last failure is re-raised
    unchanged once the attempts run out, so a genuine permission problem still
    surfaces as itself rather than as a timeout, and the caller's
    symlink-aware wrapping still applies.
    """
    if sys.platform != "win32":
        return action()
    for attempt in range(_BUSY_ATTEMPTS):
        try:
            return action()
        except PermissionError as exc:
            if not _is_contention(exc):
                raise
            if attempt == _BUSY_ATTEMPTS - 1:
                raise
            time.sleep(_BUSY_BACKOFF_SECONDS * (attempt + 1))
    raise AssertionError("unreachable: the loop either returns or raises")
