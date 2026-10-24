"""The content identity of a checkout, file by file: what a recorded check ran against.

A command report taken with ``--project`` (``services/command_reports.py``) holds this
identity before and after the command, so a later step can tell whether the source a
check passed on is still the source in the tree. Content, not history: Git HEAD is never
read, so an empty commit, an amend or a branch switch that leaves every byte alone keeps
the identity. Uncommitted and untracked nonignored work counts.

Kept per file so a change can be named (:func:`changed_paths`). No path is exempt: a
file that is new, changed or gone is a change, whoever wrote it, so a check's own output
belongs in a gitignored directory or outside the checkout (owner's decision on M6).

Every read that fails raises ``ValueError``: an identity that could not be taken is
unknown, and unknown must never pass for "unchanged".
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
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


#: Labels for the ignore rules kept outside the work tree. Under ``.git/``, where no
#: tracked or untracked path can be, so neither can be confused with a checkout file.
INFO_EXCLUDE = ".git/info/exclude"
EXCLUDES_FILE = ".git/core.excludesFile"


def _names(root: Path) -> list[str]:
    """Every source path under ``root``, relative, sorted.

    In a git work tree that is every tracked file, every untracked one git does not
    ignore, and every ``.gitignore`` git reads, ignored or not: an untracked
    ``tests/.gitignore`` that ignores itself and a ``conftest.py`` must not hide both
    (tester's X2 on #14). A ``.gitignore`` inside an ignored directory is never read by
    git, and is not listed."""
    probe = _git(root, "rev-parse", "--show-toplevel", timeout=10)
    if probe.returncode == 0:
        tracked = _git_ls(root, "--cached")
        untracked = _git_ls(root, "--others", "--exclude-standard")
        rules = [
            name
            for name in _git_ls(root, "--others", "--ignored", "--exclude-standard", "--directory")
            if Path(name).name == ".gitignore" and not name.endswith("/")
        ]
        kept = {name for name in [*untracked, *rules] if not _generated(name)}
        return sorted(set(tracked) | kept)
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


def _rule_files(root: Path) -> list[tuple[str, Path]]:
    """The ignore rules git applies to ``root`` from outside its work tree, as
    ``(label, path)``: ``info/exclude`` and ``core.excludesFile`` (or its default), each
    only where the file exists. Editing either changes what counts as source, so their
    content is part of the identity (tester's X1 on #14). Empty outside git."""
    probe = _git(root, "rev-parse", "--show-toplevel", timeout=10)
    if probe.returncode != 0:
        return []
    found: list[tuple[str, Path]] = []
    exclude = _git(root, "rev-parse", "--git-path", "info/exclude", timeout=10)
    if exclude.returncode == 0 and (text := os.fsdecode(exclude.stdout.strip())):
        found.append((INFO_EXCLUDE, root / text))
    configured = _git(root, "config", "--path", "--get", "core.excludesFile", timeout=10)
    if configured.returncode == 0 and (text := os.fsdecode(configured.stdout.strip())):
        found.append((EXCLUDES_FILE, root / text))
    elif configured.returncode == 1:  # unset: git's default
        config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        found.append((EXCLUDES_FILE, Path(config) / "git" / "ignore"))
    else:
        raise ValueError(f"git could not read core.excludesFile in {root}")
    return [(label, path) for label, path in found if path.is_file()]


def _change_stamp(status: os.stat_result) -> str:
    """When a file last changed in any way: its ctime, which neither a write that puts
    the old bytes back nor ``touch``/``utime`` can restore (tester's X5 on #14). Windows
    has no such time, and its mtime stands in."""
    return str(status.st_mtime_ns if sys.platform == "win32" else status.st_ctime_ns)


def _hash_file(path: Path, entry: hashlib._Hash) -> str:
    """Add ``path``'s mode and bytes to ``entry``; its :func:`_change_stamp`. ValueError when it
    cannot be read whole, or changed while it was read."""
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
    return _change_stamp(after)


def _hash_link(root: Path, path: Path, entry: hashlib._Hash) -> str:
    """Add a symlink's text and, when it points inside the checkout, what it points at:
    an untracked ``conftest.py`` linked to a file in an ignored directory is that file's
    content, not the link's (tester's X3 on #14). A target outside the checkout counts
    by its link text only. The stamp covers the link and every file hashed through it."""
    stamps = [_change_stamp(path.lstat())]
    entry.update(b"link\0" + os.fsencode(os.readlink(path)))
    target = path.resolve()
    if not target.is_relative_to(root) or not target.exists():
        return ":".join(stamps)
    if target.is_file():
        entry.update(b"\0target\0")
        stamps.append(_hash_file(target, entry))
    elif target.is_dir():
        for directory, dirs, files in os.walk(target, followlinks=False):
            dirs.sort()
            for filename in sorted(files):
                inner = Path(directory) / filename
                if inner.is_symlink():
                    continue  # one level: a link inside counts as the directory's listing
                entry.update(b"\0" + os.fsencode(str(inner.relative_to(target))) + b"\0")
                stamps.append(_hash_file(inner, entry))
    return hashlib.sha256(":".join(stamps).encode()).hexdigest()


def _iter_source_entries(root: Path) -> Iterator[tuple[str, str, str | None]]:
    """Yield ``(relative path, content hash, stamp)`` for every source file under
    ``root``, then for each ignore-rules file outside it (:func:`_rule_files`).

    The one place the working set is enumerated and hashed, so the per-file map and the
    combined fingerprint cannot disagree about what counts. A symlink is hashed with
    what it points at inside the checkout, never followed out of it. A submodule or
    nested repository adds its recorded commit AND its own content, so an edit, a new
    file or a local commit inside it changes the identity. The stamp (:func:`_change_stamp`)
    is None for a path with no file to time: deleted, or a directory entry.
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
        stamp: str | None = None
        if path.is_symlink():
            try:
                stamp = _hash_link(root, path, entry)
            except OSError as exc:
                raise ValueError(f"cannot read source link {path}: {exc}") from None
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
            stamp = _hash_file(path, entry)
        yield name, entry.hexdigest(), stamp
    for label, path in _rule_files(root):
        entry = hashlib.sha256(os.fsencode(label) + b"\0")
        stamp = _hash_file(path, entry)
        yield label, entry.hexdigest(), stamp


def source_state(root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """``root``'s per-file content hashes, and the stamp of each path that has one: what
    a report keeps before and after its command, so a change put back before the
    command ended still shows (tester's X5 on #14)."""
    files: dict[str, str] = {}
    stamps: dict[str, str] = {}
    for name, digest, stamp in _iter_source_entries(root):
        files[name] = digest
        if stamp is not None:
            stamps[name] = stamp
    return files, stamps


def source_file_hashes(root: Path) -> dict[str, str]:
    """The per-file content hash of every source file under ``root``."""
    return source_state(root)[0]


def moved_stamps(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    """The paths whose stamp moved between ``before`` and ``after``, sorted: each was
    written, replaced or re-moded in between, whatever bytes it holds now."""
    return sorted(name for name, stamp in before.items() if after.get(name, stamp) != stamp)


def fingerprint_of(files: Mapping[str, str]) -> str:
    """One deterministic identity for a per-file map."""
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(os.fsencode(name) + b"\0" + files[name].encode() + b"\0")
    return "sha256:" + digest.hexdigest()


def source_fingerprint(root: Path) -> str:
    """The combined identity of ``root``'s source: "did anything at all change"."""
    return fingerprint_of(source_file_hashes(root))


def changed_paths(recorded: Mapping[str, str], current: Mapping[str, str]) -> list[str]:
    """The paths that make ``current`` differ from ``recorded``, sorted: each recorded
    file that changed or vanished, and each one new since the record. A new file counts
    like any other change: an added ``conftest.py`` changes what a pytest check means, and
    the identity cannot tell a check's own write from an edit made while it ran
    (validator's M6 on #136; the owner chose this strict rule)."""
    changed = [name for name, digest in recorded.items() if current.get(name) != digest]
    changed += [name for name in current if name not in recorded]
    return sorted(changed)


def source_unchanged(recorded: Mapping[str, str], current: Mapping[str, str]) -> bool:
    """Whether ``current`` is ``recorded``, file for file."""
    return not changed_paths(recorded, current)
