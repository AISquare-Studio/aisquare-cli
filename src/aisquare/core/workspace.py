"""Resolve which project is active.

The active project is the one pinned by ``project switch`` (stored in
``state.json``) if there is one and it is still registered; otherwise it is
derived from the working directory by walking up to the nearest repository root.
A project's id is a stable hash of its resolved root path.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from aisquare.core import paths
from aisquare.core.ids import PROJECT_PREFIX
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


def git_common_root(start: Path) -> Path | None:
    """The principal repository root for ``start``, resolving git worktrees.

    A linked worktree's ``.git`` is a *file* pointing at the principal
    repository, so a plain marker walk stops inside the worktree and treats it
    as its own project. ``--git-common-dir`` names the shared directory
    instead, which is what makes every checkout of one repository resolve to a
    single project.

    Returns ``None`` when git is unavailable or ``start`` is not in a work
    tree, leaving callers on the marker-based fallback.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    common = result.stdout.strip()
    if not common:
        return None
    common_dir = Path(common)
    if not common_dir.is_absolute():
        common_dir = (start / common_dir).resolve()
    if common_dir.name != ".git" and ".git" in common_dir.parts:
        # A submodule's common dir is <super>/.git/modules/<name> (deeper when
        # nested) — git bookkeeping, not anyone's project root. Bail to the
        # marker walk, which lands on the submodule checkout itself.
        return None
    # <principal>/.git → <principal>; a bare repo's common dir is the repo itself.
    root = common_dir.parent if common_dir.name == ".git" else common_dir
    return root.resolve()


def _git_directory_common(gitdir: Path) -> Path | None:
    """The common directory of ``gitdir`` if git takes it for a git directory, else ``None``.

    git's ``is_git_directory``: a ``HEAD`` in ``gitdir``, and ``objects`` and ``refs`` in
    the common directory, which a ``commondir`` file names (a linked worktree's) or which
    is ``gitdir`` itself. An empty ``.git`` a vendored copy or an aborted clone left is
    none, and git's discovery walks on past it (review of #243, round 15, 5/5).
    """
    try:
        commondir = (gitdir / "commondir").read_text(encoding="utf-8", errors="replace")
        common = gitdir / commondir.strip()
    except OSError:
        common = gitdir
    if (gitdir / "HEAD").is_file() and (common / "objects").is_dir() and (common / "refs").is_dir():
        return common.resolve()
    return None


def _discovered_common_root(start: Path) -> Path | None:
    """What :func:`git_common_root` gives for ``start``, read from disk where git gave
    nothing (not installed, timed out).

    git's discovery, walked up from ``start``: a ``.git`` file names the git directory
    (``gitdir:``), and git fails on one that names none; a ``.git`` directory, else the
    directory itself (a bare repository), is one only if git takes it for one, else git
    walks on. The common directory found is mapped by :func:`git_common_root`'s rule.
    ``None`` wherever git's answer leaves the root to the marker walk: no repository, a
    ``.git`` file naming none (a worktree whose principal moved), a submodule.
    """
    for directory in (start, *start.parents):
        dotgit = directory / ".git"
        if dotgit.is_file():
            try:
                text = dotgit.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
            if not text.startswith("gitdir:"):
                return None
            common = _git_directory_common(directory / text[len("gitdir:") :].strip())
            if common is None:
                return None
        else:
            common = _git_directory_common(dotgit) or _git_directory_common(directory)
            if common is None:
                continue
        if common.name != ".git" and ".git" in common.parts:
            return None
        return common.parent if common.name == ".git" else common
    return None


def find_project_root(start: Path) -> Path:
    """Return the project root for ``start``, resolving git worktrees.

    Git is asked first so that every worktree of a repository resolves to the
    principal checkout — a feature branch in ``../wt-auth`` shares the parent
    repo's context pool instead of starting empty. Where git gives no answer, the
    repository is read from disk for the answer git would give
    (:func:`_discovered_common_root`): the marker walk alone stopped at the first
    marker, a worktree's own root or a subdirectory's ``.aisquare``, so a directory
    was one project while git answered and another while it did not (review of
    #243, round 12, 2/10 and 4/10). Falls back to the nearest ancestor carrying a
    project marker, then to ``start`` itself, so every directory resolves to
    *some* project.
    """
    start = start.resolve()
    common = git_common_root(start) or _discovered_common_root(start)
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
    """
    dotgit = root / ".git"
    try:
        if not dotgit.is_file():
            return None
        text = dotgit.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if not line.startswith("gitdir:"):
            continue
        gitdir = Path(line[len("gitdir:") :].strip())
        if not gitdir.is_absolute():
            gitdir = root / gitdir
        if gitdir.parent.name == "worktrees":
            common = gitdir.parent.parent
            principal = common.parent if common.name == ".git" else common
            return principal.resolve()
    principal_root = git_common_root(root)
    if principal_root is None or principal_root == root.resolve():
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
