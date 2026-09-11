"""Content identity for evidence: changing working files invalidates old checks."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path

from aisquare.core.paths import aisquare_home

_GENERATED = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


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
    """Hash HEAD and the working contents of tracked/nonignored files.

    Includes uncommitted and untracked work, not just HEAD. Git-ignored build
    outputs and AI Square's home are excluded. In a non-git project common
    generated directories are excluded. Symlinks are hashed as links; no reads
    escape the project through them. A failed/incomplete read raises rather
    than certifying stale evidence.
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
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        digest.update(head.stdout if head.returncode == 0 else b"unborn")
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
        names = sorted({os.fsdecode(name) for name in listing.stdout.split(b"\0") if name})
    else:
        names = []
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in _GENERATED)
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
            # A gitlink (submodule): include its own source identity, too.
            digest.update(b"directory\0" + source_fingerprint(path).encode())
        else:
            before = path.stat()
            if not stat.S_ISREG(before.st_mode):
                raise ValueError(f"source is not a regular file: {path}")
            digest.update(str(before.st_mode).encode() + b"\0")
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            after = path.stat()
            if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                raise ValueError(f"source changed while checking: {path}")
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()
