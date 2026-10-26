"""What a repository looks like from outside, coarsely: where it is hosted, what language
most of it is in, how big it is, whether it is a monorepo.

The Usage & stats page groups tokens by repository, and the opt-in sharing path may send the
same four traits (never the name, the path or the remote) beside a salted hash. Everything
here is read from git alone, once per repository a day (``services.usage_stats`` caches it):

- the **remote** is ``origin``'s URL, normalised to ``host/org/repo`` — no scheme, no
  credentials embedded in it, no ``.git``, lower case — so ``git@github.com:Org/Repo.git``
  and ``https://tok@github.com/org/repo`` are one repository;
- the **host** is that remote's host as one of ``github``/``gitlab``/``bitbucket``/``other``,
  or ``none`` with no remote;
- the **language** is the one with the most tracked files by extension (``git ls-files``,
  capped at :data:`MAX_FILES`; documents, data and images are no language);
- the **size bucket** is the tracked-file count: ``xs`` up to 50, ``s`` up to 500, ``m`` up to
  5 000, ``l`` up to 50 000, ``xl`` above;
- a **monorepo** has a workspace file at its root (``pnpm-workspace.yaml``, ``lerna.json``,
  ``nx.json``, ``turbo.json``, ``rush.json``) or package manifests in at least two
  directories below it.

Reading never raises: a path that is no repository, a git that is missing or slow, an output
too long, all read as the repository with no remote, no language and the smallest bucket.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from pathlib import Path, PurePosixPath

from aisquare.core.spawn import git_env
from aisquare.models import RepoTraits

#: The most tracked files read; a repository beyond it is ``xl`` whatever the rest says.
MAX_FILES = 200_000
_GIT_TIMEOUT_S = 20

#: Upper bounds of the size buckets, in tracked files; anything above the last is ``xl``.
_SIZE_BUCKETS: tuple[tuple[int, str], ...] = ((50, "xs"), (500, "s"), (5_000, "m"), (50_000, "l"))

_LANGUAGES = {
    ".py": "python", ".pyi": "python",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".scala": "scala", ".rb": "ruby", ".php": "php", ".cs": "csharp", ".fs": "fsharp",
    ".swift": "swift", ".m": "objective-c", ".mm": "objective-c", ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".ex": "elixir", ".exs": "elixir", ".erl": "erlang", ".hs": "haskell", ".clj": "clojure",
    ".lua": "lua", ".dart": "dart", ".r": "r", ".jl": "julia", ".sh": "shell", ".bash": "shell",
    ".zsh": "shell", ".ps1": "powershell", ".sql": "sql", ".vue": "vue", ".svelte": "svelte",
    ".tf": "terraform", ".zig": "zig", ".nim": "nim", ".pl": "perl",
}  # fmt: skip
"""Extension → language. What is not here (``.md``, ``.json``, ``.yaml``, images, lock files)
is documentation, data or an asset, and says nothing of what the code is written in."""

_WORKSPACE_FILES = frozenset(
    {"pnpm-workspace.yaml", "lerna.json", "nx.json", "turbo.json", "rush.json"}
)
_MANIFESTS = frozenset(
    {"package.json", "pyproject.toml", "setup.py", "Cargo.toml", "go.mod", "pom.xml",
     "build.gradle", "build.gradle.kts", "Gemfile", "composer.json", "mix.exs", "Package.swift"}
)  # fmt: skip
#: Directories whose manifests are someone else's code, not a package of this repository.
_VENDORED = frozenset({"node_modules", "vendor", "third_party", "third-party", "examples", "docs"})

_REMOTE_SCP = re.compile(r"^(?:[^@/\s]+@)?(?P<host>[^:/\s]+):(?P<path>(?!//).+)$")


def normalise_remote(url: str | None) -> str | None:
    """``url`` as ``host/org/repo``, or ``None`` when it is empty or names no host.

    Handles ``https://[user[:token]@]host[:port]/org/repo[.git]``, ``ssh://git@host/org/repo``
    and the scp form ``git@host:org/repo.git``; a local path (``/srv/repo.git``,
    ``file://…``) names no host and reads as ``None``.
    """
    text = (url or "").strip()
    if not text:
        return None
    if "://" in text:
        scheme, _, rest = text.partition("://")
        if scheme.lower() == "file":
            return None
        authority, _, path = rest.partition("/")
        host = authority.rsplit("@", 1)[-1].partition(":")[0]
    else:
        match = _REMOTE_SCP.match(text)
        if match is None:
            return None
        host, path = match.group("host"), match.group("path")
    path = path.strip("/").removesuffix(".git").strip("/")
    host = host.strip().lower()
    if not host or not path:
        return None
    return f"{host}/{path}".lower()


def host_of(remote: str | None) -> str:
    """The hosting service a normalised ``remote`` is on: ``github``, ``gitlab``,
    ``bitbucket``, ``other``, or ``none`` without one."""
    if not remote:
        return "none"
    host = remote.partition("/")[0]
    for name in ("github", "gitlab", "bitbucket"):
        if host == f"{name}.com" or host.startswith(f"{name}.") or host.endswith(f".{name}.com"):
            return name
    return "other"


def size_bucket(files: int) -> str:
    """The bucket a tracked-file count falls in."""
    for bound, name in _SIZE_BUCKETS:
        if files <= bound:
            return name
    return "xl"


def main_language(extensions: Counter[str]) -> str | None:
    """The language with the most files among ``extensions`` (lower-case, with the dot)."""
    by_language: Counter[str] = Counter()
    for extension, count in extensions.items():
        language = _LANGUAGES.get(extension)
        if language is not None:
            by_language[language] += count
    if not by_language:
        return None
    best = max(by_language.values())
    return min(language for language, count in by_language.items() if count == best)


def is_monorepo(paths: list[str]) -> bool:
    """Whether the tracked ``paths`` hold a workspace file at the root or the manifests of
    two packages below it."""
    packages: set[str] = set()
    for raw in paths:
        path = PurePosixPath(raw)
        if len(path.parts) == 1 and path.name in _WORKSPACE_FILES:
            return True
        if path.name in _MANIFESTS and len(path.parts) > 1:
            directory = path.parent
            if not any(part in _VENDORED for part in directory.parts):
                packages.add(str(directory))
    return len(packages) >= 2


def git_output(root: Path, *args: str) -> bytes | None:
    """``git -C root args`` standard output, or ``None`` when git fails, is missing or is slow."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
            env=git_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def read_remote(root: Path) -> str | None:
    """The normalised URL of ``origin``, else of the first remote there is."""
    for args in (("config", "--get", "remote.origin.url"), ("remote", "-v")):
        out = git_output(root, *args)
        if not out:
            continue
        text = out.decode("utf-8", errors="replace").strip()
        if args[0] == "remote":
            fields = text.split()
            text = fields[1] if len(fields) > 1 else ""
        remote = normalise_remote(text.splitlines()[0] if text else "")
        if remote is not None:
            return remote
    return None


def read_traits(root: Path, remote: str | None) -> tuple[RepoTraits, int]:
    """The repository's :class:`~aisquare.models.RepoTraits` and its tracked-file count."""
    traits = RepoTraits(host=host_of(remote))
    out = git_output(root, "ls-files", "-z")
    if out is None:
        return traits, 0
    names = [name for name in out.decode("utf-8", errors="replace").split("\0") if name]
    total = len(names)
    names = names[:MAX_FILES]
    extensions: Counter[str] = Counter(PurePosixPath(name).suffix.lower() for name in names)
    traits = RepoTraits(
        host=traits.host,
        language=main_language(extensions),
        size_bucket=size_bucket(total),
        monorepo=is_monorepo(names),
    )
    return traits, total
