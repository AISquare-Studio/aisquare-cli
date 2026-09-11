"""Read what an agent has changed, without touching what it is changing.

The fleet gives an agent a worktree; something outside the pane eventually wants
to know what is in it — a review surface, a status line, the Office's Changes
tab. That question is answerable only by ``git``, and the two seams that already
run git here cannot answer it: ``fleet._git`` exists for ``git worktree`` and the
branch queries behind ``reap``, and ``ci_snapshot._git`` is the CI bed's own
plumbing. Neither is public, and a caller outside this package that wanted a diff
had no name to call.

So this module is the public, **read-only** answer, and every part of its shape
is a consequence of one fact: the tree being read belongs to an agent that is
probably editing it right now.

**It asks git not to take the index lock, and git obeys for some commands and
not others.** ``GIT_OPTIONAL_LOCKS=0`` is set on every call, and measured on git
2.50 it is honoured by ``status`` and NOT by ``diff``: a ``git diff`` against a
worktree still stats the files and may rewrite ``.git/index`` with the refreshed
stat cache. So the honest claim is narrower than "it never writes": the *content*
of the repository — its objects, refs, branches and working tree — is never
touched, and the one thing that can change is a cache git maintains for its own
speed, exactly as any ``git status`` the user runs would. An earlier version of
this docstring claimed the lock was never taken at all, which was simply not
true, and a safety claim that is not true is worse than no claim.

**It never mutates.** The commands are ``diff``, ``rev-parse``,
``symbolic-ref``, ``worktree list`` and ``merge-base``. There is no ``add``, no
``stash``, no ``-N``: the only ways to make untracked files appear in a diff
either write to the agent's index or cost a second full-tree walk, and
``untracked`` is reported as a count instead. A read route that wrote to an
agent's index would be a bug with somebody's uncommitted work in it.

**It is bounded twice.** A per-call ``timeout`` because a repository with
thousands of refs can take seconds, and a patch byte budget because a diff has
no natural size and the caller is usually a socket. Exceeding the budget is
reported (``truncated``) rather than hidden.

Failure is ``None`` or an empty result, never an exception: a caller asking
"what changed?" about a directory that is not a repository, or a repository that
cannot be read, deserves an answer rather than a traceback.
"""

from __future__ import annotations

import subprocess
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from aisquare.core.spawn import untraced_env

DEFAULT_TIMEOUT: Final = 5.0
"""Seconds for one git command. Generous for a diff, short enough to stay honest."""

DEFAULT_MAX_PATCH_BYTES: Final = 200_000
"""The patch budget. Matches what the Office contract carries over a socket."""

MAX_FILES: Final = 2_000
"""Rows in one answer. A refactor that touched more is a count, not a list."""

FileStatus = Literal["added", "modified", "deleted", "renamed"]

_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,255}$")
"""What may be passed to git as a ref.

Narrow on purpose, and the leading character is the whole point: git reads any
argument beginning with ``-`` as an OPTION, so a ``base`` of
``--output=/path/to/file`` made ``git diff`` write a file wherever the caller
named. That was reachable from every caller of :func:`read_diff`, and a browser
is two hops away from one. A ref cannot begin with ``-`` (git refuses it), so
refusing it here costs nothing real and closes the channel.

``..`` is excluded separately below: it is legal in a ref pattern but turns one
argument into a range, which is a different question than the caller asked.
"""


class UnsafeRef(ValueError):
    """A ref that git would read as an option, or as something other than a ref."""


def _checked_ref(ref: str) -> str:
    """``ref`` if it is unmistakably a ref, else raise.

    Validation rather than escaping: there is no quoting to get right when the
    argument list never reaches a shell, and the only real hazard is argv
    position. A name that passes this cannot be an option and cannot be a range.
    """
    text = str(ref)
    if not _SAFE_REF.match(text) or ".." in text:
        raise UnsafeRef(
            "a base must be a plain ref name (letters, digits, . _ / @ + -), "
            "may not begin with '-', and may not contain '..'"
        )
    return text

_STATUS: Final[dict[str, FileStatus]] = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "C": "added",
    "T": "modified",
}


@dataclass(frozen=True, slots=True)
class DiffFile:
    """One changed path and its line counts.

    ``additions`` and ``deletions`` are ``0`` for a binary file, which is what
    ``--numstat`` reports as ``-``; the alternative is ``None`` everywhere and a
    caller that has to special-case a number.
    """

    path: str
    status: FileStatus
    additions: int
    deletions: int
    binary: bool = False


@dataclass(frozen=True, slots=True)
class WorktreeDiff:
    """What one worktree has changed against its base."""

    base: str
    """The ref compared against, as resolved — never the caller's guess."""
    files: tuple[DiffFile, ...]
    patch: str | None
    """The unified diff, or ``None`` when it exceeded the budget."""
    truncated: bool
    """True when ``patch`` was dropped or ``files`` was capped."""
    untracked: int = 0
    """Files git can see but is not tracking. Counted, never diffed — see above."""


def _new_path(raw: str) -> str:
    """The path a rename ENDED at, from numstat's two spellings of one.

    ``old => new`` for a plain rename and ``dir/{old => new}`` when a prefix is
    shared. A reader wants the file it can open now, and the status column is
    what says a rename happened.
    """
    if "=>" not in raw:
        return raw
    if "{" in raw and "}" in raw:
        head, _, rest = raw.partition("{")
        inner, _, tail = rest.partition("}")
        return f"{head}{inner.split('=>')[-1].strip()}{tail}".replace("//", "/")
    return raw.split("=>")[-1].strip()


def _git(root: Path, *args: str, timeout: float) -> str | None:
    """One git command in ``root``; stdout stripped, or ``None`` on any failure.

    The one spawn site in this module, and registered as such in
    ``core.spawn.SEAMS``. ``GIT_OPTIONAL_LOCKS=0`` is the load-bearing part: it
    keeps this read from contending with the agent's own git for the index lock.
    """
    env = untraced_env()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def is_repository(root: Path, *, timeout: float = DEFAULT_TIMEOUT) -> bool:
    """Whether ``root`` is inside a git work tree. Cheap preflight."""
    return _git(root, "rev-parse", "--is-inside-work-tree", timeout=timeout) == "true"


def default_branch(root: Path, *, timeout: float = DEFAULT_TIMEOUT) -> str:
    """The branch this repository's work merges into.

    The same three-step ladder ``fleet reap`` uses — origin's HEAD, then a local
    ``main``/``master``, then ``HEAD`` — so a caller here and the reaper cannot
    disagree about what "the base" means.
    """
    head = _git(root, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD", timeout=timeout)
    if head:
        return head
    for name in ("main", "master"):
        if _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}", timeout=timeout):
            return name
    return "HEAD"


def current_branch(root: Path, *, timeout: float = DEFAULT_TIMEOUT) -> str | None:
    """The checked-out branch, or ``None`` on a detached HEAD."""
    name = _git(root, "rev-parse", "--abbrev-ref", "HEAD", timeout=timeout)
    return None if not name or name == "HEAD" else name


def worktree_branches(root: Path, *, timeout: float = DEFAULT_TIMEOUT) -> dict[Path, str]:
    """Every linked worktree of this repository and the branch it is on.

    One call for a whole project, rather than one ``rev-parse`` per agent. A
    worktree on a detached HEAD has no ``branch`` line and is simply absent.
    """
    listing = _git(root, "worktree", "list", "--porcelain", timeout=timeout)
    if not listing:
        return {}
    out: dict[Path, str] = {}
    path: Path | None = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree ") :].strip())
        elif line.startswith("branch ") and path is not None:
            out[path] = line[len("branch ") :].strip().removeprefix("refs/heads/")
            path = None
        elif not line.strip():
            path = None
    return out


def counts(root: Path, *, base: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, int]:
    """``(dirty, ahead)`` — files changed against HEAD, commits not on the base.

    The two numbers a status line wants without paying for a whole diff.
    """
    status = _git(root, "status", "--porcelain", timeout=timeout)
    dirty = len([line for line in (status or "").splitlines() if line.strip()])
    ref = _checked_ref(base) if base else default_branch(root, timeout=timeout)
    counted = _git(root, "rev-list", "--count", f"{ref}..HEAD", timeout=timeout)
    try:
        ahead = int(counted or 0)
    except ValueError:
        ahead = 0
    return dirty, ahead


def read_diff(
    root: Path,
    *,
    base: str | None = None,
    max_patch_bytes: int = DEFAULT_MAX_PATCH_BYTES,
    max_files: int = MAX_FILES,
    timeout: float = DEFAULT_TIMEOUT,
) -> WorktreeDiff | None:
    """What this worktree has changed against ``base``. ``None`` when unreadable.

    The comparison is against the **merge base**, not against the tip of the base
    branch: a caller wants to see what this agent did, and diffing against a tip
    that has moved since the branch was cut would attribute everybody else's
    commits to it as deletions.

    ``base`` is resolved before use and reported back on the result, because a
    caller that guessed wrong should be able to see that it did.
    """
    if not is_repository(root, timeout=timeout):
        return None

    ref = _checked_ref(base) if base else default_branch(root, timeout=timeout)
    # The fork point. When there is no common ancestor — an unrelated history, or
    # a base that does not exist here — fall back to the ref itself rather than
    # answering with nothing: a diff against something is more useful than a
    # refusal, and `base` on the result says which it was.
    merge_base = _git(root, "merge-base", ref, "HEAD", timeout=timeout) or ref

    numstat = _git(root, "diff", "--numstat", merge_base, timeout=timeout)
    names = _git(root, "diff", "--name-status", merge_base, timeout=timeout)
    if numstat is None or names is None:
        return None

    # `--name-status` reports a rename as "R100\told\tnew" and `--numstat` as
    # "a\td\told => new" (or the brace form "sub/{a => b}"). Both are keyed on the
    # NEW path here, which is the one a reader can open. Getting this wrong made
    # every rename read as a "modified" file whose path was an arrow.
    status_by_path: dict[str, FileStatus] = {}
    for line in names.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        code = parts[0].strip()[:1]
        path = parts[-1].strip()
        status_by_path[path] = _STATUS.get(code, "modified")

    files: list[DiffFile] = []
    truncated = False
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, removed, path = parts[0], parts[1], _new_path(parts[-1].strip())
        if len(files) >= max_files:
            truncated = True
            break
        binary = added == "-" or removed == "-"
        files.append(
            DiffFile(
                path=path,
                status=status_by_path.get(path, "modified"),
                additions=0 if binary else int(added or 0),
                deletions=0 if binary else int(removed or 0),
                binary=binary,
            )
        )

    patch = _git(root, "diff", merge_base, timeout=timeout)
    if patch is not None and len(patch.encode("utf-8")) > max_patch_bytes:
        # Dropped whole rather than cut: half a unified diff is not a diff, and a
        # caller that tried to apply or colour it would be parsing a broken one.
        patch = None
        truncated = True

    untracked = _git(root, "ls-files", "--others", "--exclude-standard", timeout=timeout)

    return WorktreeDiff(
        base=ref,
        files=tuple(files),
        patch=patch or None,
        truncated=truncated,
        untracked=len([line for line in (untracked or "").splitlines() if line.strip()]),
    )


__all__ = [
    "DEFAULT_MAX_PATCH_BYTES",
    "DEFAULT_TIMEOUT",
    "MAX_FILES",
    "DiffFile",
    "UnsafeRef",
    "FileStatus",
    "WorktreeDiff",
    "counts",
    "current_branch",
    "default_branch",
    "is_repository",
    "read_diff",
    "worktree_branches",
]
