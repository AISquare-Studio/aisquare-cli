"""Content identity for evidence: changing working files invalidates old checks."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from collections.abc import Iterator, Mapping
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


def _git_ls(root: Path, *flags: str) -> list[str]:
    """``git ls-files`` under ``root`` with ``flags``; ValueError if git cannot answer."""
    try:
        listing = subprocess.run(
            ["git", "-C", str(root), "ls-files", *flags, "-z"],
            capture_output=True,
            timeout=30,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        detail = getattr(exc, "stderr", b"") or b""
        raise ValueError(
            f"git could not list {root}: {detail.decode('utf-8', 'replace').strip() or exc}"
        ) from None
    return [os.fsdecode(name) for name in listing.stdout.split(b"\0") if name]


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


def _iter_source_entries(root: Path) -> Iterator[tuple[str, str]]:
    """Yield ``(relative path, per-file content hash)`` for every source file.

    The one place the working set is enumerated and hashed; both
    :func:`source_fingerprint` (a single combined identity) and
    :func:`source_file_hashes` (the per-file map finding 14 needs) are built from
    it, so the two can never disagree about what counts as source.

    Content, not history: an empty commit, an amend or a branch switch that
    leaves every file byte-identical yields the same hashes. Uncommitted and
    untracked work count. Git-ignored outputs, well-known generated directories
    and AI Square's home are excluded. Submodules contribute the commit they
    record plus, when checked out, their own content. Symlinks are hashed as
    links; no reads escape the project through them. A failed or incomplete read
    raises rather than certifying stale evidence.
    """
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"source directory does not exist: {root}")
    home = aisquare_home().resolve()
    if root.is_relative_to(home):
        raise ValueError("source directory must be outside AI Square's report/storage directory")
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
        # Tracked files count whatever they are named: a repo that commits source
        # under a path component like ``venv`` or ``node_modules`` must still be
        # fingerprinted. The generated-directory filter is about UNTRACKED build
        # output, so it applies only to the ``--others`` set.
        tracked = _git_ls(root, "--cached")
        untracked = _git_ls(root, "--others", "--exclude-standard")
        names = sorted(set(tracked) | {name for name in untracked if not _generated(name)})
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
        # A per-file digest fed the SAME bytes, in the same order, that the
        # combined fingerprint used to fold in for this entry.
        entry = hashlib.sha256()
        entry.update(os.fsencode(name) + b"\0")
        if path.is_symlink():
            entry.update(b"link\0" + os.fsencode(os.readlink(path)))
        elif not path.exists():
            entry.update(b"deleted\0")
        elif path.is_dir():
            # A gitlink: a submodule or a nested repo, one entry in the parent's
            # index. Its recorded commit is part of its identity, but that alone
            # misses edits, new files and local commits INSIDE it — so fold in the
            # nested checkout's own content fingerprint too. This does not
            # double-count: the parent's ls-files never lists the nested files.
            # An uninitialised submodule is an empty dir with no repo, so it
            # contributes only the recorded gitlink.
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
            except OSError as exc:
                # Unreadable is unknown, and unknown must never certify evidence.
                raise ValueError(f"cannot read source file {path}: {exc}") from None
            after = path.stat()
            if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                raise ValueError(f"source changed while checking: {path}")
        entry.update(b"\0")
        yield name, entry.hexdigest()


def fingerprint_of(files: Mapping[str, str]) -> str:
    """Combine a per-file map into one content identity, deterministically."""
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(os.fsencode(name) + b"\0" + files[name].encode() + b"\0")
    return "sha256:" + digest.hexdigest()


def source_file_hashes(root: Path) -> dict[str, str]:
    """The per-file content hash of every source file under ``root``.

    Finding 14: an evidence gate that compares only the SINGLE combined
    fingerprint of the whole tree cannot tell a NEW file a check wrote (a
    ``pytest --junitxml`` report, a non-git project's ``.coverage``) from an edit
    to source — every run differs, so such a check can never be recorded as a
    pass. This map lets a caller compare file by file: a path present only in the
    later map is a new output and is ignored; a path whose hash changed, or that
    has vanished, is a real source change. See :func:`source_unchanged`.
    """
    return dict(_iter_source_entries(root))


def source_unchanged(recorded: Mapping[str, str], current: Mapping[str, str]) -> bool:
    """Whether every file recorded at check time is byte-identical now.

    A file that appears only in ``current`` is new since the check — the per-run
    output a command wrote into the tree — and does not invalidate the record. A
    recorded file that is now missing or different does (finding 14). "New output
    file" and "mutated source" are distinguishable only per file: at the whole-
    tree level both merely make the combined hash differ.
    """
    return all(current.get(name) == digest for name, digest in recorded.items())


def source_fingerprint(root: Path) -> str:
    """One content identity for the working set — the combine of the per-file map.

    Kept as the quick "did anything at all change" signal and the value stored on
    manual evidence; per-file comparisons go through :func:`source_file_hashes`
    and :func:`source_unchanged`.
    """
    return fingerprint_of(source_file_hashes(root))
