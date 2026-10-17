"""Resolve which project is active.

The active project is the one pinned by ``project switch`` (stored in
``state.json``) if there is one and it is still registered; otherwise it is
derived from the working directory by walking up to the nearest repository root.
A project's id is a stable hash of its resolved root path.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import subprocess
from pathlib import Path

from aisquare.core import paths
from aisquare.core.ids import PROJECT_PREFIX
from aisquare.core.spawn import git_env
from aisquare.core.state_file import StateUnwritableError, read_state, update_state
from aisquare.core.store import ContextStore
from aisquare.models import ProjectInfo

# Directory markers that identify a project root, nearest-first.
ROOT_MARKERS = (".git", ".hg", ".aisquare")
_PIN_KEY = "active_project_id"


# Entries that only ever exist inside OUR home directory, never inside a
# hand-made ``<project>/.aisquare`` opt-in marker (nothing in the codebase
# creates that one — a user makes it by hand, the way they would ``.git``).
_HOME_LAYOUT = ("config.toml", "context.db", "agents.json")


def _is_aisquare_home(candidate: Path) -> bool:
    """Whether ``candidate`` is an aisquare *home*, not a project marker.

    Matched structurally rather than by path, because the home that matters is
    not always the configured one: the test suite redirects ``AISQUARE_HOME``
    into a temp tree while the developer's real ``~/.aisquare`` is still
    sitting above ``tempfile.gettempdir()``, and only a structural check sees
    both for what they are.
    """
    if not candidate.is_dir():
        return False
    try:
        if candidate.resolve() == paths.aisquare_home().resolve():
            return True
    except OSError:  # pragma: no cover - unresolvable path is not our home
        pass
    return any((candidate / entry).exists() for entry in _HOME_LAYOUT)


def marks_project_root(directory: Path) -> bool:
    """Whether ``directory`` carries a project marker.

    ``.aisquare`` is overloaded: ``<project>/.aisquare`` is the opt-in project
    marker, but ``~/.aisquare`` is *our own state directory*. Counting the
    latter makes the home directory a project root, so every markerless
    directory beneath it silently resolves to ``$HOME`` and shares one context
    pool — and ``serve --stdio``'s "refusing to activate: not a project root"
    guard, which exists precisely because Claude Desktop launches from ``$HOME``,
    never fires. It bites hardest on Windows, where ``tempfile`` lives under
    ``%USERPROFILE%``, but it is not a Windows bug: a POSIX user running
    ``aisquare`` from a markerless ``~/scratch`` hits exactly the same thing.
    """
    for marker in ROOT_MARKERS:
        candidate = directory / marker
        if not candidate.exists():
            continue
        if marker == ".aisquare" and _is_aisquare_home(candidate):
            continue
        return True
    return False


#: What Windows says of a path through a symlink loop (``ERROR_CANT_RESOLVE_FILENAME``),
#: where POSIX says ``ELOOP``; 3.11's and 3.12's ``resolve`` read both as one.
_WINERROR_CANT_RESOLVE_FILENAME = 1921


def _resolved(path: Path) -> Path:
    """``path.resolve()``, a symlink loop on the way raised as ``OSError`` (``ELOOP``) on every
    Python, as ``stat`` raises it. A path that is missing still resolves: ``directory_project``
    is asked of deleted roots.

    3.11 and 3.12 raise ``RuntimeError`` for one (from 3.13 ``resolve`` hands back the path
    as far as it got), and the readers of a stored root catch ``OSError`` for one that cannot
    be asked. One project root that became a loop turned each phone write that reaches a
    board into a 500, and a stale Reply's 409 into one; caught at one caller alone
    (``fleet.restart_board``, round 39, 2/9), the others still let it through (review of
    #259, round 40, 3/6).

    So the loop is told by a ``stat`` of what ``resolve`` gave, as 3.11's and 3.12's own
    ``resolve`` tells it, not by what ``resolve`` raises. Mapped from its ``RuntimeError``
    alone, a loop raised nothing from 3.13, which CI runs: ``find_project_root`` walked from
    it to the nearest ancestor with a marker, and a 0.8.0 row was filed under that board, a
    phone could reach it, and a note named it (round 41, 1/6)."""
    try:
        resolved = path.resolve()
    except RuntimeError as exc:
        raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), str(path)) from exc
    try:
        os.stat(resolved)
    except OSError as exc:
        if exc.errno == errno.ELOOP or (
            getattr(exc, "winerror", 0) == _WINERROR_CANT_RESOLVE_FILENAME
        ):
            raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), str(path)) from exc
    return resolved


#: What ``stat`` says of a path that is gone: nothing there, a file where a directory on its
#: path was, or a link that can never reach one.
_GONE = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})


def _is_dir(root: Path) -> bool:
    """Whether ``root`` is a directory; one that cannot be asked is taken to be one.

    ``os.stat``, not ``Path.is_dir``: that raised for ``EACCES`` up to Python 3.13, and from
    3.14 it is ``os.path.isdir``, which says False for any ``OSError``. On 3.14 a root under
    a folder this user may not search, or on a mount that answered ``EIO``, was called
    missing, and ``prune --missing`` dropped it (review of #259, round 19)."""
    try:
        return stat.S_ISDIR(os.stat(root).st_mode)
    except OSError as exc:
        return exc.errno not in _GONE


def git_common_root(start: Path) -> Path | None:
    """The principal repository root for ``start``, resolving git worktrees.

    A linked worktree's ``.git`` is a *file* pointing at the principal
    repository, so a plain marker walk stops inside the worktree and treats it
    as its own project. ``--git-common-dir`` names the shared directory
    instead, which is what makes every checkout of one repository resolve to a
    single project.

    Returns ``None`` when git is unavailable or ``start`` is not in a work
    tree, leaving callers on the marker-based fallback. Without ``--no-optional-locks``:
    ``rev-parse`` takes no optional lock, and git before 2.15 refuses the flag, which here
    read as "not a work tree", so every linked worktree, a fleet agent's included, got a
    board of its own, with no warning (review of #259, round 26, 11/11). Every hook asks
    this.

    A git before 2.5 has no linked worktrees and no ``--git-common-dir``, so the marker walk
    is right for it, and it is left there. Before 1.8.5 it does not know ``-C`` and exits
    129; from 1.8.5 to 2.4, ``rev-parse`` echoes the option it does not know and exits 0,
    and ``<start>/--git-common-dir`` was every project's root (review of #259, round 28,
    6/10).

    From 2.5 to 2.12, a relative answer is relative to the top level of the work tree, not
    to ``start`` (2.13 made it ``start``'s): from ``repo/api`` it is ``.git``, and
    ``repo/api`` was taken for the root, recorded on every row spawned there, and a session
    in ``repo/api`` read another board than one at ``repo`` (review of #259, round 29, 4/8).
    So the same call asks ``--show-cdup``, the way from ``start`` up to that top level, and
    a relative answer that names nothing from ``start`` is read from there.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--git-common-dir", "--show-cdup"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
            env=git_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    # The common dir, then the way up, blank at the top level; none where there is no work
    # tree (a bare repository, or inside `.git`).
    lines = result.stdout.splitlines()
    common = lines[0].strip() if lines else ""
    up = lines[1].strip() if len(lines) > 1 else ""
    if not common or common == "--git-common-dir":  # the option echoed: git 1.8.5 to 2.4
        return None
    common_dir = Path(common)
    if not common_dir.is_absolute():
        here = start / common_dir
        common_dir = _resolved(here if here.exists() or not up else start / up / common_dir)
    if common_dir.name != ".git" and ".git" in common_dir.parts:
        # A submodule's common dir is <super>/.git/modules/<name> (deeper when
        # nested) — git bookkeeping, not anyone's project root. Bail to the
        # marker walk, which lands on the submodule checkout itself.
        return None
    # <principal>/.git → <principal>; a bare repo's common dir is the repo itself.
    root = common_dir.parent if common_dir.name == ".git" else common_dir
    return _resolved(root)


def find_project_root(start: Path) -> Path:
    """Return the project root for ``start``, resolving git worktrees.

    Git is asked first so that every worktree of a repository resolves to the
    principal checkout — a feature branch in ``../wt-auth`` shares the parent
    repo's context pool instead of starting empty. Falls back to the nearest
    ancestor carrying a project marker, then to ``start`` itself, so every
    directory resolves to *some* project. A symlink loop on the way to ``start`` raises
    ``OSError`` (:func:`_resolved`).

    A ``start`` that is not a directory (:func:`_is_dir`) is its own root, with no ``git``
    and no walk: a deleted project folder is asked of, as a stored root (``fleet``'s
    restarts and pauses, Remote's Board and Tasks tabs and ``done #N``). Walked from, a
    deleted ``~/code/app`` under a dotfiles ``~/.git`` resolved to ``~``: a restart moved its
    agent onto ``~``'s board, or refused it the task on ``app``'s, and the phone read and
    closed ``~``'s tasks for ``app``. Guarded in ``fleet.restart_board`` alone (round 41,
    3/6), the holds and the restart read different boards (review of #259, round 42, 3/11).
    """
    start = _resolved(start)
    if not _is_dir(start):
        return start
    common = git_common_root(start)
    if common is not None:
        return common
    for directory in (start, *start.parents):
        if marks_project_root(directory):
            return directory
    return start


def worktree_principal(root: Path) -> Path | None:
    """The principal repository a LINKED worktree at ``root`` belongs to, else ``None``.

    Only a root whose ``.git`` is a FILE can be a linked worktree — a main
    checkout has a ``.git`` directory and a plain directory has none — so that
    is the precondition, and it is what keeps a subdirectory registered as its
    own project (git would happily name the repo enclosing it) from being
    mistaken for one. The file is read first: ``gitdir: <common>/worktrees/<n>``
    is the shape git writes, needs no subprocess, still resolves after the
    principal has been deleted, and covers a bare principal
    (``repo.git/worktrees/x``). Any other content is put to git itself, whose
    ``--git-common-dir`` differs from ``--git-dir`` exactly for a worktree.

    A ``.git`` file whose way to the principal loops names none, as one that cannot be read:
    raised (``RuntimeError`` on 3.11 and 3.12), it ended ``project prune`` in a traceback
    (review of #259, round 40, 3/6).
    """
    dotgit = root / ".git"
    try:
        if not dotgit.is_file():
            return None
        text = dotgit.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        return _principal_named(root, text)
    except OSError:
        return None


def _principal_named(root: Path, text: str) -> Path | None:
    """The principal repository the ``.git`` file of the worktree at ``root`` names (``text``
    is its content), else ``None`` (:func:`worktree_principal`)."""
    for line in text.splitlines():
        if not line.startswith("gitdir:"):
            continue
        gitdir = Path(line[len("gitdir:") :].strip())
        if not gitdir.is_absolute():
            gitdir = root / gitdir
        if gitdir.parent.name == "worktrees":
            common = gitdir.parent.parent
            principal = common.parent if common.name == ".git" else common
            return _resolved(principal)
    principal_root = git_common_root(root)
    if principal_root is None or principal_root == _resolved(root):
        return None
    return principal_root


def project_id_for(root: Path) -> str:
    """Derive a stable project id from a resolved root path."""
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()
    return PROJECT_PREFIX + digest[:24]


def current_project(cwd: Path | None = None) -> ProjectInfo:
    """Describe the project containing ``cwd`` (default: the process cwd)."""
    root = find_project_root(cwd or Path.cwd())
    return ProjectInfo(id=project_id_for(root), root=root, linked_repos=[])


def pinned_project_id() -> str | None:
    """Return the project id pinned by ``project switch``, or ``None``.

    A ``state.json`` that is not a JSON object pins nothing (``core.state_file``):
    the project is then the working directory's, as with no pin at all — it
    used to raise ``AttributeError`` from here. One that EXISTS BUT CANNOT BE
    READ still raises its ``OSError``: read as "no pin", a permission error would
    silently point every project-scoped command at whatever directory the user
    happens to stand in.
    """
    value = read_state(strict=True).get(_PIN_KEY)
    return value if isinstance(value, str) else None


def pin_project(project_id: str | None) -> None:
    """Pin (or, with ``None``, unpin) the active project in ``state.json``.

    Unpinning when nothing is pinned is a no-op: a file that is not a JSON
    object already pins nothing, and removing an absent key is not a failed
    write. Otherwise ``core.state_file.StateUnwritableError`` says why the file
    could not be updated — it is not a JSON object and is left as it is, because
    the other keys in it (the board's theme, the fleet UI's navigator width) are
    the user's; its lock could not be taken; it could not be read or written.

    That includes the unpin's own look at the file first: its strict read
    raises the ``OSError`` of a file that cannot be read, and a caller that
    catches the refusal — ``project forget``'s repin, after a purge that has
    already committed — let that one escape as a traceback, leaving the data
    directory behind (review of the #167 fold, F6).
    """
    if project_id is None:
        try:
            pinned = pinned_project_id()
        except OSError as exc:
            raise StateUnwritableError(f"{paths.state_path()} could not be read: {exc}") from exc
        if pinned is None:
            return
    update_state(_PIN_KEY, project_id)


def active_project(store: ContextStore, cwd: Path | None = None) -> ProjectInfo:
    """The pinned project if one is set and still registered, else cwd-resolved.

    When the resolved project is already registered, its stored record (with any
    linked repos) is returned rather than a bare cwd-derived one.
    """
    pinned = pinned_project_id()
    if pinned is not None:
        found = store.get_project(pinned)
        if found is not None:
            return found
    resolved = current_project(cwd)
    stored = store.get_project(resolved.id)
    return stored if stored is not None else resolved
