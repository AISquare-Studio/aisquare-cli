"""The content identity of a checkout, file by file: what a recorded check ran against.

A command report taken with ``--project`` (``services/command_reports.py``) holds this
identity before and after the command, so a later step can tell whether the source a
check passed on is still the source in the tree. Content, not history: Git HEAD is never
read, so an empty commit, an amend or a branch switch that leaves every byte alone keeps
the identity. Uncommitted and untracked nonignored work counts.

Kept per file because "a new file the check wrote" and "an edited source file" look the
same at the whole-tree level: both change one combined hash. Per file, a path that only
appears afterwards (``pytest --junitxml=report.xml``) is new output and is ignored, while
a pre-existing file that changed or vanished is a real change (:func:`source_unchanged`).

Every read that fails raises ``ValueError``: an identity that could not be taken is
unknown, and unknown must never pass for "unchanged".
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path

from aisquare.core.paths import aisquare_home
from aisquare.core.spawn import git_env

#: Build output, whether or not a project remembered to gitignore it: a first ``pytest``
#: run must not change the identity by writing ``__pycache__``. Applied to UNTRACKED files
#: only — a repository that commits source under a path named ``venv`` or
#: ``node_modules`` still has that source counted.
GENERATED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".hypothesis",
        ".tox",
        ".nox",
    }
)


def _generated(name: str) -> bool:
    return any(part in GENERATED_DIRECTORIES for part in Path(name).parts)


def _git(root: Path, *args: str, timeout: float = 30) -> subprocess.CompletedProcess[bytes]:
    """``git -C root args`` in :func:`git_env`; ValueError when git cannot be run at all.

    The one process this module starts. The caller reads the return code, since a
    non-zero one means different things to different callers ("not a work tree" is an
    answer; "could not list" is a failure)."""
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            timeout=timeout,
            check=False,
            env=git_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"git could not run in {root}: {exc}") from None


def _git_ls(root: Path, *flags: str) -> list[str]:
    """``git ls-files`` under ``root`` with ``flags``; ValueError if git cannot answer."""
    listing = _git(root, "ls-files", *flags, "-z")
    if listing.returncode != 0:
        detail = listing.stderr.decode("utf-8", "replace").strip()
        raise ValueError(f"git could not list {root}: {detail or listing.returncode}")
    return [os.fsdecode(name) for name in listing.stdout.split(b"\0") if name]


def _gitlink(root: Path, name: str) -> str | None:
    """The commit a submodule entry records, or None when the path is not a gitlink."""
    try:
        entry = _git(root, "ls-files", "-s", "-z", "--", name, timeout=10)
    except ValueError:
        return None
    for record in entry.stdout.split(b"\0"):
        fields = record.split()
        if len(fields) >= 3 and fields[0] == b"160000":
            return fields[1].decode("ascii", "replace")
    return None


def _toplevel(start: Path) -> Path | None:
    """The work tree ``start`` is in — a linked worktree's own, not its principal's."""
    try:
        result = _git(start, "rev-parse", "--show-toplevel", timeout=10)
    except ValueError:
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.decode("utf-8", "replace").strip()
    return Path(text).resolve() if text else None


def git_common_dir(path: Path) -> Path | None:
    """The repository ``path`` belongs to, as git's common directory: the same for a
    checkout and every worktree of it, different for an unrelated repository. None
    when ``path`` is in no git repository or git cannot answer."""
    try:
        result = _git(path, "rev-parse", "--git-common-dir", timeout=10)
    except ValueError:
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.decode("utf-8", "replace").strip()
    if not text:
        return None
    common = Path(text)
    return (common if common.is_absolute() else path / common).resolve()


def source_root_for(project_root: Path, cwd: Path | None = None) -> Path:
    """The checkout to fingerprint for a command run in ``cwd`` under ``project_root``.

    A board covers every worktree of one repository, and each worktree is fingerprinted
    as its own files: a coder's check in ``../wt-auth`` is about ``wt-auth``, not the
    principal checkout. ``cwd`` is used only when its board is ``project_root``'s; a
    command from elsewhere is about the project's registered directory.
    """
    from aisquare.core import orchestrator

    project_root = project_root.resolve()
    start = (cwd or Path.cwd()).resolve()
    if orchestrator.team_project(start).root.resolve() != project_root:
        return project_root
    top = _toplevel(start)
    if top is not None:
        return top
    if start.is_relative_to(project_root):
        return project_root
    for directory in (start, *start.parents):
        if any((directory / marker).exists() for marker in (".git", ".hg", ".aisquare")):
            return directory
    return start


def _refuse_home(root: Path) -> None:
    """A root that is, or holds, AI Square's home or the user's home is not a checkout.

    ``.aisquare`` marks a project root, so a non-git directory under ``~`` resolves up to
    ``~`` itself, and fingerprinting it would hash the whole home tree."""
    home = aisquare_home().resolve()
    if root.is_relative_to(home):
        raise ValueError("source directory must be outside AI Square's home")
    if home.is_relative_to(root):
        raise ValueError(
            "source directory contains AI Square's home; refusing to walk the whole tree"
        )
    try:
        user_home = Path.home().resolve()
    except (OSError, RuntimeError):
        return
    if user_home.is_relative_to(root):
        raise ValueError("source directory is the home directory; refusing to walk it")


def _names(root: Path) -> list[str]:
    """Every source path under ``root``, relative, sorted."""
    probe = _git(root, "rev-parse", "--show-toplevel", timeout=10)
    if probe.returncode == 0:
        tracked = _git_ls(root, "--cached")
        untracked = _git_ls(root, "--others", "--exclude-standard")
        return sorted(set(tracked) | {name for name in untracked if not _generated(name)})
    names: list[str] = []

    def unreadable(error: OSError) -> None:
        raise ValueError(f"cannot list source directory: {error}") from None

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=unreadable):
        dirs[:] = sorted(d for d in dirs if d not in GENERATED_DIRECTORIES)
        for dirname in dirs:
            link = Path(directory) / dirname
            if link.is_symlink():
                names.append(str(link.relative_to(root)))
        for filename in files:
            names.append(str((Path(directory) / filename).relative_to(root)))
    return sorted(names)


def _iter_source_entries(root: Path) -> Iterator[tuple[str, str]]:
    """Yield ``(relative path, content hash)`` for every source file under ``root``.

    The one place the working set is enumerated and hashed, so the per-file map and the
    combined fingerprint cannot disagree about what counts. Symlinks are hashed as
    links, never followed out of the checkout. A submodule or nested repository adds its
    recorded commit AND its own content, so an edit, a new file or a local commit inside
    it changes the identity.
    """
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"source directory does not exist: {root}")
    _refuse_home(root)
    home = aisquare_home().resolve()
    for name in _names(root):
        path = root / name
        if path.is_relative_to(home):
            continue
        if not path.parent.resolve().is_relative_to(root):
            raise ValueError(f"source path escapes through a directory link: {path}")
        entry = hashlib.sha256()
        entry.update(os.fsencode(name) + b"\0")
        if path.is_symlink():
            entry.update(b"link\0" + os.fsencode(os.readlink(path)))
        elif not path.exists():
            entry.update(b"deleted\0")
        elif path.is_dir():
            # A gitlink (submodule) or an untracked nested repository: one entry in the
            # parent's listing, whose files the parent never lists. An uninitialised
            # submodule is an empty directory and contributes only its recorded commit.
            entry.update(b"gitlink\0" + (_gitlink(root, name) or "unrecorded").encode())
            if (path / ".git").exists():
                entry.update(b"nested\0" + source_fingerprint(path).encode())
        else:
            try:
                before = path.stat()
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError(f"source is not a regular file: {path}")
                entry.update(str(before.st_mode).encode() + b"\0")
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        entry.update(chunk)
                after = path.stat()
            except OSError as exc:
                raise ValueError(f"cannot read source file {path}: {exc}") from None
            if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                raise ValueError(f"source changed while checking: {path}")
        yield name, entry.hexdigest()


def source_file_hashes(root: Path) -> dict[str, str]:
    """The per-file content hash of every source file under ``root``."""
    return dict(_iter_source_entries(root))


def fingerprint_of(files: Mapping[str, str]) -> str:
    """One deterministic identity for a per-file map."""
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(os.fsencode(name) + b"\0" + files[name].encode() + b"\0")
    return "sha256:" + digest.hexdigest()


def source_fingerprint(root: Path) -> str:
    """The combined identity of ``root``'s source: "did anything at all change"."""
    return fingerprint_of(source_file_hashes(root))


def source_unchanged(recorded: Mapping[str, str], current: Mapping[str, str]) -> bool:
    """Whether every file in ``recorded`` is byte-identical in ``current``.

    A path only in ``current`` is new since the record (a per-run output file) and does
    not count. A recorded path that changed or vanished does."""
    return all(current.get(name) == digest for name, digest in recorded.items())
