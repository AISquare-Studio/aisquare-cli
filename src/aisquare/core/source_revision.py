"""Content identity for evidence: changing working files invalidates old checks."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path

from aisquare.core.paths import aisquare_home

#: Never source, whether or not a project remembered to gitignore them: a first
#: `pytest` run must not turn every evidence record stale by writing __pycache__.
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


def _gitlink(root: Path, name: str) -> str | None:
    """The commit a submodule entry records, or None when the path is not a gitlink."""
    try:
        entry = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-s", "-z", "--", name],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for record in entry.stdout.split(b"\0"):
        fields = record.split()
        if len(fields) >= 3 and fields[0] == b"160000":
            return fields[1].decode("ascii", "replace")
    return None


def source_root_for(project_root: Path, cwd: Path | None = None) -> Path:
    """Resolve the actual checkout, retaining a worktree or shared-hub worker.

    A board can cover multiple checkouts. Only use cwd when its board matches
    the requested project; an explicit command from elsewhere uses that
    project's registered directory instead.
    """
    from aisquare.core import orchestrator

    project_root = project_root.resolve()
    start = (cwd or Path.cwd()).resolve()
    if orchestrator.team_project(start).root.resolve() != project_root:
        return project_root
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0:
        return Path(result.stdout.strip()).resolve()
    if start.is_relative_to(project_root):
        return project_root
    for directory in (start, *start.parents):
        if any((directory / marker).exists() for marker in (".git", ".hg", ".aisquare")):
            return directory
    return start


def source_fingerprint(root: Path) -> str:
    """Hash the working CONTENTS of tracked and nonignored files.

    Content, not history: an empty commit, an amend or a branch switch that
    leaves every file byte-identical keeps the same fingerprint, because the
    evidence was about those bytes. Uncommitted and untracked work count.
    Git-ignored outputs, well-known generated directories and AI Square's home
    are excluded. Submodules contribute the commit they record, not their
    trees. Symlinks are hashed as links; no reads escape the project through
    them. A failed or incomplete read raises rather than certifying stale
    evidence.
    """
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"source directory does not exist: {root}")
    home = aisquare_home().resolve()
    if root.is_relative_to(home):
        raise ValueError("source directory must be outside AI Square's report/storage directory")
    digest = hashlib.sha256()
    try:
        probe = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError:
        probe = None
    if probe is not None and probe.returncode == 0:
        try:
            listing = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "ls-files",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                    "-z",
                ],
                capture_output=True,
                timeout=30,
                check=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            detail = getattr(exc, "stderr", b"") or b""
            raise ValueError(
                f"git could not list {root}: {detail.decode('utf-8', 'replace').strip() or exc}"
            ) from None
        names = sorted(
            {
                os.fsdecode(name)
                for name in listing.stdout.split(b"\0")
                if name and not _generated(os.fsdecode(name))
            }
        )
    else:
        names = []

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
        names.sort()
    for name in names:
        path = root / name
        if path.is_relative_to(home):
            continue
        if not path.parent.resolve().is_relative_to(root):
            raise ValueError(f"source path escapes through a directory link: {path}")
        digest.update(os.fsencode(name) + b"\0")
        if path.is_symlink():
            digest.update(b"link\0" + os.fsencode(os.readlink(path)))
        elif not path.exists():
            digest.update(b"deleted\0")
        elif path.is_dir():
            # A submodule: its recorded commit is its identity here. Descending into
            # it would double-count its files and an uninitialised one has no tree.
            digest.update(b"gitlink\0" + (_gitlink(root, name) or "unrecorded").encode())
        else:
            try:
                before = path.stat()
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError(f"source is not a regular file: {path}")
                digest.update(str(before.st_mode).encode() + b"\0")
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
            except OSError as exc:
                # Unreadable is unknown, and unknown must never certify evidence.
                raise ValueError(f"cannot read source file {path}: {exc}") from None
            after = path.stat()
            if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                raise ValueError(f"source changed while checking: {path}")
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()
