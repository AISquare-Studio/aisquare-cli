"""A per-project symbol index: where a name is defined, who uses it, what looks like it (#285).

Agents found code by grepping and reading whole files, and one that missed an existing
helper wrote another. This is phase A of the code-intelligence program (#91): a table of
definitions and references the agent can ask before it greps, and before it writes a
helper (``aisquare code similar``).

**Its own database.** ``~/.aisquare/projects/<id>/code/index.db`` (SQLite, WAL), apart
from ``context.db``: it is a cache of the checkout, rebuilt from it at will, so it takes
no migration and a damaged one is deleted and built again. Tables: ``file`` (path,
sha256, size, mtime_ns, lang, parser), ``symbol`` (name, qualname, kind, path, line,
end_line, signature, parent, arity), ``ref`` (name, path, line), ``meta`` (HEAD, parser
versions, built_at), and ``symbol_fts`` over each name split into its snake_case and
camelCase sub-tokens.

**Files** are git's (``git ls-files -co --exclude-standard``), less the snapshot's
ignore list (``core.snapshot.DEFAULT_IGNORE``, nested checkouts, ``[snapshot]
ignore``), binaries, and files over ``[code] max_file_kb``. Outside git, a walk of the
root with the same list.

**Parsers.** Python through the standard library's ``ast``: no dependency. TypeScript
and JavaScript through tree-sitter and each grammar's own ``tags.scm`` (definition and
reference captures, as Aider reads them), from the optional extra
``aisquare-cli[code]``; its wheels carry the compiled grammars and the queries, so
nothing is downloaded. Any other source file is counted as not indexed, by language,
and the status says so.

**Incremental.** A file whose size and mtime are unchanged is skipped; one whose hash is
unchanged gets its new mtime; anything else is parsed again; a file gone from the list is
dropped. A parser version that moved rebuilds everything. One builder per project at a
time, under a lock file beside the database.

Nothing here leaves the machine.
"""

from __future__ import annotations

import ast
import contextlib
import fnmatch
import hashlib
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from aisquare.core import locking, paths
from aisquare.core import snapshot as snapshot_core
from aisquare.core.spawn import git_env

SCHEMA_VERSION = 1
"""The database's own layout. A file built by another layout is deleted and rebuilt: it is a
cache of the checkout, never the only copy of anything."""
DEFAULT_MAX_FILE_KB = 1024
_BUILD_WAIT_S = 0.5
_GIT_TIMEOUT = 60.0
_BINARY_SNIFF = 8192

#: Extensions the index reads, by language. A source file outside these is counted as
#: not indexed, under :data:`_OTHER_SOURCE`'s name for it.
_PYTHON = (".py", ".pyi")
_TREE_SITTER: dict[str, str] = {
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
}
_OTHER_SOURCE: dict[str, str] = {
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cc": "c++",
    ".cpp": "c++",
    ".hpp": "c++",
    ".cs": "c#",
    ".swift": "swift",
    ".scala": "scala",
    ".lua": "lua",
    ".sh": "shell",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS file (
    path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL, lang TEXT NOT NULL, parser TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS symbol (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, qualname TEXT NOT NULL, kind TEXT NOT NULL,
    path TEXT NOT NULL, line INTEGER NOT NULL, end_line INTEGER NOT NULL,
    signature TEXT NOT NULL, parent TEXT, arity INTEGER
);
CREATE INDEX IF NOT EXISTS symbol_name ON symbol (name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS symbol_path ON symbol (path);
CREATE TABLE IF NOT EXISTS ref (name TEXT NOT NULL, path TEXT NOT NULL, line INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ref_name ON ref (name);
CREATE INDEX IF NOT EXISTS ref_path ON ref (path);
CREATE VIRTUAL TABLE IF NOT EXISTS symbol_fts USING fts5 (tokens, content='');
"""


# --- results ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Symbol:
    """A definition: a function, method, class (or, in TypeScript, interface or module)."""

    name: str
    qualname: str
    kind: str
    path: str
    line: int
    end_line: int
    signature: str
    parent: str | None = None
    arity: int | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "qualname": self.qualname,
            "kind": self.kind,
            "path": self.path,
            "line": self.line,
            "end_line": self.end_line,
            "signature": self.signature,
            "parent": self.parent,
            "arity": self.arity,
        }


@dataclass(frozen=True)
class Ref:
    """A use of a name: a call, an attribute read, a type annotation."""

    name: str
    path: str
    line: int

    def as_json(self) -> dict[str, Any]:
        return {"name": self.name, "path": self.path, "line": self.line}


@dataclass(frozen=True)
class Similar:
    """A definition that looks like the one asked about, and why."""

    symbol: Symbol
    score: float
    shared: tuple[str, ...]
    """The sub-tokens the two names share."""

    def as_json(self) -> dict[str, Any]:
        return {**self.symbol.as_json(), "score": round(self.score, 3), "shared": list(self.shared)}


@dataclass
class BuildReport:
    """What one :func:`build` did."""

    full: bool
    files: int = 0
    parsed: int = 0
    unchanged: int = 0
    removed: int = 0
    skipped_large: int = 0
    skipped_binary: int = 0
    symbols: int = 0
    refs: int = 0
    seconds: float = 0.0
    not_indexed: dict[str, int] = field(default_factory=dict)
    """Source files no installed parser reads, by language."""
    failed: dict[str, str] = field(default_factory=dict)
    """Files a parser refused (a syntax error), with its words: their old rows are dropped."""

    def as_json(self) -> dict[str, Any]:
        return {
            "full": self.full,
            "files": self.files,
            "parsed": self.parsed,
            "unchanged": self.unchanged,
            "removed": self.removed,
            "skipped_large": self.skipped_large,
            "skipped_binary": self.skipped_binary,
            "symbols": self.symbols,
            "refs": self.refs,
            "seconds": round(self.seconds, 2),
            "not_indexed": self.not_indexed,
            "failed": self.failed,
        }


@dataclass(frozen=True)
class IndexStatus:
    """``aisquare code index --status``: what the index holds and how old it is."""

    exists: bool
    path: Path
    files: int = 0
    symbols: int = 0
    refs: int = 0
    built_at: str | None = None
    head: str | None = None
    current_head: str | None = None
    parsers: dict[str, int] = field(default_factory=dict)
    """Indexed files per parser."""
    not_indexed: dict[str, int] = field(default_factory=dict)

    @property
    def stale(self) -> bool:
        """Built at another commit than the checkout's (uncommitted edits are not seen here)."""
        return self.exists and self.head != self.current_head

    def as_json(self) -> dict[str, Any]:
        return {
            "exists": self.exists,
            "path": str(self.path),
            "files": self.files,
            "symbols": self.symbols,
            "refs": self.refs,
            "built_at": self.built_at,
            "head": self.head,
            "current_head": self.current_head,
            "stale": self.stale,
            "parsers": self.parsers,
            "not_indexed": self.not_indexed,
        }


class IndexBusy(RuntimeError):
    """Another process is building this project's index."""


# --- where ------------------------------------------------------------------------------


def code_dir(project_id: str) -> Path:
    return paths.project_data_dir(project_id) / "code"


def index_path(project_id: str) -> Path:
    return code_dir(project_id) / "index.db"


def exists(project_id: str) -> bool:
    return index_path(project_id).is_file()


@contextlib.contextmanager
def _connect(project_id: str, *, create: bool) -> Iterator[sqlite3.Connection]:
    path = index_path(project_id)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.is_file():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(path, timeout=5.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        if create:
            conn.executescript(_SCHEMA)
        yield conn
    finally:
        conn.close()


# --- names --------------------------------------------------------------------------------

_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z0-9])|[A-Z]?[a-z0-9]+|[A-Z]+")


def subtokens(name: str) -> list[str]:
    """``spawnAgent`` → ``spawn agent``; ``_verify_cap`` → ``verify cap``; ``HTTPServer``
    → ``http server``: lower-case, in order, repeats kept."""
    return [
        part.lower() for chunk in re.split(r"[^A-Za-z0-9]+", name) for part in _CAMEL.findall(chunk)
    ]


# --- files ----------------------------------------------------------------------------------


def _git_files(root: Path) -> list[str] | None:
    """Tracked and untracked-but-not-ignored files under ``root``, or ``None`` outside git."""
    try:
        completed = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard", "-z"],
            cwd=root,
            capture_output=True,
            timeout=_GIT_TIMEOUT,
            check=False,
            env=git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return [
        item for item in completed.stdout.decode("utf-8", "surrogateescape").split("\0") if item
    ]


def _walk_files(root: Path) -> list[str]:
    """Every file under ``root`` outside git, never entering a directory the snapshot's
    defaults leave out (``node_modules``, ``.venv``…): pruned as walked, not filtered after."""
    stems = [p.removeprefix("**/").removesuffix("/**") for p in snapshot_core.DEFAULT_IGNORE]
    found: list[str] = []
    for current, dirs, files in os.walk(root, onerror=lambda _exc: None):
        dirs[:] = [
            name for name in dirs if not any(fnmatch.fnmatchcase(name, stem) for stem in stems)
        ]
        here = Path(current)
        found += [(here / name).relative_to(root).as_posix() for name in files]
    return found


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """A repomix-style glob (``**/node_modules/**``, ``docs/generated/**``, ``*.snap``) as a
    regex over a POSIX relative path: ``**`` spans directories, ``*`` stays inside one."""
    out = ""
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out += "(?:.*/)?"
            index += 3
        elif pattern.startswith("**", index):
            out += ".*"
            index += 2
        elif pattern[index] == "*":
            out += "[^/]*"
            index += 1
        elif pattern[index] == "?":
            out += "[^/]"
            index += 1
        else:
            out += re.escape(pattern[index])
            index += 1
    if "/" not in pattern:
        out = "(?:.*/)?" + out  # a bare name matches at any depth, as repomix's do
    return re.compile(out + r"\Z")


def listed_files(root: Path, ignore: Sequence[str] = ()) -> list[str]:
    """The files the index reads: git's list (else a walk), less the snapshot's ignore list."""
    listed = _git_files(root)
    if listed is None:
        listed = _walk_files(root)
    patterns = [_glob_regex(p) for p in snapshot_core.ignore_patterns(root, ignore)]
    return sorted(path for path in listed if not any(p.match(path) for p in patterns))


def _language(path: str) -> str | None:
    suffix = os.path.splitext(path)[1].lower()
    if suffix in _PYTHON:
        return "python"
    return _TREE_SITTER.get(suffix) or _OTHER_SOURCE.get(suffix)


# --- parsers --------------------------------------------------------------------------------

Parsed = tuple[list[Symbol], list[Ref]]


class ParseError(ValueError):
    """The parser refused the file (a syntax error)."""


def python_symbols(path: str, source: str) -> Parsed:
    """Definitions and references in one Python file, through ``ast``."""
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError) as exc:
        raise ParseError(f"{type(exc).__name__}: {exc}") from None
    symbols: list[Symbol] = []
    refs: list[Ref] = []

    # A stack, not recursion: a generated 1,200-term expression parses, and a recursive
    # walk of it raised RecursionError (review of #327).
    stack: list[tuple[ast.AST, list[ast.AST]]] = [(tree, [])]
    while stack:
        node, scope = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                symbols.append(_python_symbol(path, child, scope))
                stack.append((child, [*scope, child]))
                continue
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                refs.append(Ref(child.id, path, child.lineno))
            elif isinstance(child, ast.Attribute) and isinstance(child.ctx, ast.Load):
                refs.append(Ref(child.attr, path, child.lineno))
            stack.append((child, scope))
    return symbols, refs


def _python_symbol(
    path: str, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, scope: list[ast.AST]
) -> Symbol:
    names = [getattr(outer, "name", "") for outer in scope]
    parent = names[-1] if names else None
    in_class = bool(scope) and isinstance(scope[-1], ast.ClassDef)
    if isinstance(node, ast.ClassDef):
        bases = ", ".join(ast.unparse(base) for base in node.bases)
        signature = f"class {node.name}({bases})" if bases else f"class {node.name}"
        kind, arity = "class", None
    else:
        args = node.args
        positional = [*args.posonlyargs, *args.args]
        if in_class and positional and positional[0].arg in ("self", "cls"):
            positional = positional[1:]
        arity = len(positional) + len(args.kwonlyargs)
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
        signature = f"{prefix} {node.name}({ast.unparse(args)}){returns}"
        kind = "method" if in_class else "function"
    return Symbol(
        name=node.name,
        qualname=".".join([*names, node.name]),
        kind=kind,
        path=path,
        line=node.lineno,
        end_line=node.end_lineno or node.lineno,
        signature=signature[:240],
        parent=parent,
        arity=arity,
    )


class _TreeSitter:
    """The optional extra's parsers: TypeScript, TSX and JavaScript, with their tags queries.

    ``None`` from :func:`load` when ``aisquare-cli[code]`` is not installed."""

    def __init__(self) -> None:
        from importlib.resources import files

        import tree_sitter
        import tree_sitter_javascript
        import tree_sitter_typescript

        js_tags = (files(tree_sitter_javascript) / "queries" / "tags.scm").read_text()
        ts_tags = (
            js_tags + "\n" + (files(tree_sitter_typescript) / "queries" / "tags.scm").read_text()
        )
        grammars = {
            "javascript": (tree_sitter_javascript.language(), js_tags),
            "typescript": (tree_sitter_typescript.language_typescript(), ts_tags),
            "tsx": (tree_sitter_typescript.language_tsx(), ts_tags),
        }
        self._ts = tree_sitter
        self._parsers: dict[str, tuple[Any, Any]] = {}
        for lang, (pointer, tags) in grammars.items():
            language = tree_sitter.Language(pointer)
            self._parsers[lang] = (tree_sitter.Parser(language), tree_sitter.Query(language, tags))
        self.version = "tree-sitter-" + "+".join(
            metadata.version(dist)
            for dist in ("tree-sitter", "tree-sitter-typescript", "tree-sitter-javascript")
        )

    def symbols(self, lang: str, path: str, source: bytes) -> Parsed:
        parser, query = self._parsers[lang]
        tree = parser.parse(source)
        symbols: list[Symbol] = []
        refs: list[Ref] = []
        seen: set[tuple[str, int, str]] = set()
        for _pattern, captures in self._ts.QueryCursor(query).matches(tree.root_node):
            names = captures.get("name") or []
            tags = [key for key in captures if key.startswith(("definition.", "reference."))]
            if not names or not tags:
                continue
            node, tag = names[0], tags[0]
            name = (node.text or b"").decode("utf-8", "replace")
            line = node.start_point[0] + 1
            if (name, line, tag) in seen:
                continue
            seen.add((name, line, tag))
            if tag.startswith("reference."):
                refs.append(Ref(name, path, line))
                continue
            whole = captures[tag][0]
            parent = _ts_parent(whole)
            params = whole.child_by_field_name("parameters")
            if params is None:
                value = whole.child_by_field_name("value")
                params = value.child_by_field_name("parameters") if value is not None else None
            kind = tag.removeprefix("definition.")
            first = whole.text.decode("utf-8", "replace").splitlines()[0] if whole.text else name
            symbols.append(
                Symbol(
                    name=name,
                    qualname=f"{parent}.{name}" if parent else name,
                    kind=kind,
                    path=path,
                    line=whole.start_point[0] + 1,
                    end_line=whole.end_point[0] + 1,
                    signature=first.strip()[:240],
                    parent=parent,
                    arity=params.named_child_count if params is not None else None,
                )
            )
        return symbols, refs


def _ts_parent(node: Any) -> str | None:
    """The class or interface a tree-sitter definition sits in, by name."""
    current = node.parent
    while current is not None:
        if current.type in (
            "class_declaration",
            "class",
            "abstract_class_declaration",
            "interface_declaration",
        ):
            name = current.child_by_field_name("name")
            if name is not None and name.text:
                return str(name.text.decode("utf-8", "replace"))
        current = current.parent
    return None


_tree_sitter_cache: list[_TreeSitter | None] = []


def tree_sitter() -> _TreeSitter | None:
    """The extra's parsers, built once per process; ``None`` without ``aisquare-cli[code]``."""
    if not _tree_sitter_cache:
        try:
            _tree_sitter_cache.append(_TreeSitter())
        except Exception:  # not installed, or a grammar this build cannot load
            _tree_sitter_cache.append(None)
    return _tree_sitter_cache[0]


def parser_versions() -> dict[str, str]:
    """What each language is parsed with; a change rebuilds the index."""
    versions = {"python": f"ast-{sys.version_info.major}.{sys.version_info.minor}"}
    extra = tree_sitter()
    if extra is not None:
        for lang in ("typescript", "tsx", "javascript"):
            versions[lang] = extra.version
    return versions


# --- build ------------------------------------------------------------------------------------


def build(
    project_id: str,
    root: Path,
    *,
    full: bool = False,
    max_file_kb: int = DEFAULT_MAX_FILE_KB,
    ignore: Sequence[str] = (),
    head: str | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> BuildReport:
    """Bring the index of ``root`` up to date (or rebuild it with ``full``), under the lock.

    Raises :class:`IndexBusy` when another process holds the lock.
    """
    code_dir(project_id).mkdir(parents=True, exist_ok=True)
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(
                locking.holding(code_dir(project_id) / "build.lock", wait_s=_BUILD_WAIT_S)
            )
        except TimeoutError:
            raise IndexBusy("another process is building this project's code index") from None
        return _build(
            project_id,
            root,
            full=full,
            max_file_kb=max_file_kb,
            ignore=ignore,
            head=head,
            clock=clock,
        )
    raise AssertionError("unreachable")  # pragma: no cover - ExitStack swallows nothing


def _build(
    project_id: str,
    root: Path,
    *,
    full: bool,
    max_file_kb: int,
    ignore: Sequence[str],
    head: str | None,
    clock: Callable[[], float],
) -> BuildReport:
    started = clock()
    versions = parser_versions()
    _reset_if_foreign(project_id)
    with _connect(project_id, create=True) as conn:
        known_versions = _meta(conn, "parsers")
        if known_versions != _encode(versions) or _meta(conn, "schema") != str(SCHEMA_VERSION):
            full = True
        report = BuildReport(full=full)
        if full:
            conn.executescript(
                "DELETE FROM file; DELETE FROM symbol; DELETE FROM ref; "
                "INSERT INTO symbol_fts (symbol_fts) VALUES ('delete-all');"
            )
        known = {
            row[0]: (row[1], row[2], row[3], row[4])
            for row in conn.execute("SELECT path, sha256, size, mtime_ns, parser FROM file")
        }
        listed = listed_files(root, ignore)
        extra = tree_sitter()
        keep: set[str] = set()
        limit = max_file_kb * 1024
        for rel in listed:
            lang = _language(rel)
            if lang is None:
                continue
            parser = versions.get(lang)
            if parser is None:
                report.not_indexed[lang] = report.not_indexed.get(lang, 0) + 1
                continue
            full_path = root / rel
            try:
                stat = full_path.stat()
            except OSError:
                continue
            if stat.st_size > limit:
                report.skipped_large += 1
                continue
            report.files += 1
            keep.add(rel)
            before = known.get(rel)
            if (
                before is not None
                and before[1] == stat.st_size
                and before[2] == stat.st_mtime_ns
                and before[3] == parser
            ):
                report.unchanged += 1
                continue
            try:
                data = full_path.read_bytes()
            except OSError:
                keep.discard(rel)
                report.files -= 1
                continue
            if b"\0" in data[:_BINARY_SNIFF]:
                keep.discard(rel)
                report.files -= 1
                report.skipped_binary += 1
                continue
            digest = hashlib.sha256(data).hexdigest()
            if before is not None and before[0] == digest and before[3] == parser:
                conn.execute(
                    "UPDATE file SET size = ?, mtime_ns = ? WHERE path = ?",
                    (stat.st_size, stat.st_mtime_ns, rel),
                )
                report.unchanged += 1
                continue
            _drop_file_rows(conn, rel)
            try:
                if lang == "python":
                    symbols, refs = python_symbols(rel, data.decode("utf-8", "replace"))
                else:
                    assert extra is not None  # versions names it only when it loaded
                    symbols, refs = extra.symbols(lang, rel, data)
            except ParseError as exc:
                report.failed[rel] = str(exc)
                symbols, refs = [], []
            except Exception as exc:  # one file's failure is that file's, never the build's
                report.failed[rel] = f"{type(exc).__name__}: {exc}"
                symbols, refs = [], []
            conn.execute(
                "INSERT OR REPLACE INTO file (path, sha256, size, mtime_ns, lang, parser) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (rel, digest, stat.st_size, stat.st_mtime_ns, lang, parser),
            )
            _insert(conn, symbols, refs)
            report.parsed += 1
        for gone in set(known) - keep:
            _drop_file_rows(conn, gone)
            conn.execute("DELETE FROM file WHERE path = ?", (gone,))
            report.removed += 1
        report.symbols = conn.execute("SELECT COUNT(*) FROM symbol").fetchone()[0]
        report.refs = conn.execute("SELECT COUNT(*) FROM ref").fetchone()[0]
        _set_meta(conn, "schema", str(SCHEMA_VERSION))
        _set_meta(conn, "parsers", _encode(versions))
        _set_meta(conn, "head", head or "")
        _set_meta(conn, "built_at", datetime.now(tz=UTC).isoformat(timespec="seconds"))
        _set_meta(conn, "not_indexed", _encode({k: str(v) for k, v in report.not_indexed.items()}))
        conn.commit()
    report.seconds = clock() - started
    return report


def _reset_if_foreign(project_id: str) -> None:
    """Delete an index this build cannot read (another layout, or not a database): a cache."""
    path = index_path(project_id)
    if not path.is_file():
        return
    try:
        with _connect(project_id, create=False) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
            if "meta" not in tables:
                raise sqlite3.DatabaseError("no meta table")
            schema = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
            if schema is not None and schema[0] != str(SCHEMA_VERSION):
                raise sqlite3.DatabaseError("another layout")
    except sqlite3.DatabaseError:
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                path.with_name(path.name + suffix).unlink()


def _drop_file_rows(conn: sqlite3.Connection, path: str) -> None:
    rows = conn.execute("SELECT id, name FROM symbol WHERE path = ?", (path,)).fetchall()
    for row_id, name in rows:
        conn.execute(
            "INSERT INTO symbol_fts (symbol_fts, rowid, tokens) VALUES ('delete', ?, ?)",
            (row_id, " ".join(subtokens(name))),
        )
    conn.execute("DELETE FROM symbol WHERE path = ?", (path,))
    conn.execute("DELETE FROM ref WHERE path = ?", (path,))


def _insert(conn: sqlite3.Connection, symbols: Iterable[Symbol], refs: Iterable[Ref]) -> None:
    for s in symbols:
        cursor = conn.execute(
            "INSERT INTO symbol (name, qualname, kind, path, line, end_line, signature, "
            "parent, arity) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                s.name,
                s.qualname,
                s.kind,
                s.path,
                s.line,
                s.end_line,
                s.signature,
                s.parent,
                s.arity,
            ),
        )
        conn.execute(
            "INSERT INTO symbol_fts (rowid, tokens) VALUES (?, ?)",
            (cursor.lastrowid, " ".join(subtokens(s.name))),
        )
    conn.executemany(
        "INSERT INTO ref (name, path, line) VALUES (?, ?, ?)",
        [(r.name, r.path, r.line) for r in refs],
    )


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row is not None else None


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _encode(mapping: dict[str, str]) -> str:
    return ";".join(f"{key}={value}" for key, value in sorted(mapping.items()))


def _decode(text: str | None) -> dict[str, str]:
    if not text:
        return {}
    return dict(item.split("=", 1) for item in text.split(";") if "=" in item)


# --- status ---------------------------------------------------------------------------------


def status(project_id: str, *, current_head: str | None = None) -> IndexStatus:
    """What the index holds; ``exists=False`` when there is none (or it cannot be read)."""
    path = index_path(project_id)
    try:
        with _connect(project_id, create=False) as conn:
            parsers = {
                parser: count
                for parser, count in conn.execute(
                    "SELECT parser, COUNT(*) FROM file GROUP BY parser"
                )
            }
            return IndexStatus(
                exists=True,
                path=path,
                files=conn.execute("SELECT COUNT(*) FROM file").fetchone()[0],
                symbols=conn.execute("SELECT COUNT(*) FROM symbol").fetchone()[0],
                refs=conn.execute("SELECT COUNT(*) FROM ref").fetchone()[0],
                built_at=_meta(conn, "built_at"),
                head=_meta(conn, "head") or None,
                current_head=current_head,
                parsers=parsers,
                not_indexed={k: int(v) for k, v in _decode(_meta(conn, "not_indexed")).items()},
            )
    except (FileNotFoundError, sqlite3.DatabaseError):
        return IndexStatus(exists=False, path=path, current_head=current_head)


# --- queries ----------------------------------------------------------------------------------

_SYMBOL_COLUMNS = "name, qualname, kind, path, line, end_line, signature, parent, arity"


def _symbol(row: Sequence[Any]) -> Symbol:
    return Symbol(*row)


def find(project_id: str, name: str, *, kind: str | None = None, limit: int = 20) -> list[Symbol]:
    """Definitions of ``name``: exact, else case-insensitive, else prefix, else by sub-token.

    The first tier that finds anything is the answer, in path order, up to ``limit``:
    asked for ``spawn``, the ``spawn`` functions, not every ``spawn_*`` beside them.
    ``name`` may be qualified (``FleetApp.spawn``). The sub-token tier wants every part
    (``agent_spawn`` finds ``spawn_agent`` and ``spawnAgent``).
    """
    found: list[Symbol] = []
    seen: set[tuple[str, int, str]] = set()
    kinds = "" if kind is None else " AND kind = :kind"
    column = "qualname" if "." in name else "name"
    params = {"name": name, "kind": kind, "prefix": _like(name) + "%", "limit": limit}
    tiers = [
        f"SELECT {_SYMBOL_COLUMNS} FROM symbol WHERE {column} = :name{kinds} "
        "ORDER BY path, line LIMIT :limit",
        f"SELECT {_SYMBOL_COLUMNS} FROM symbol WHERE {column} = :name COLLATE NOCASE{kinds} "
        "ORDER BY path, line LIMIT :limit",
        f"SELECT {_SYMBOL_COLUMNS} FROM symbol WHERE {column} LIKE :prefix ESCAPE '\\'{kinds} "
        "ORDER BY length(name), path, line LIMIT :limit",
    ]
    tokens = subtokens(name.rsplit(".", 1)[-1])
    with _connect(project_id, create=False) as conn:
        for sql in tiers:
            _take(found, seen, (_symbol(row) for row in conn.execute(sql, params)), limit)
            if found:
                return found
        if tokens:
            match = " ".join(f'"{token}"' for token in tokens)
            rows = conn.execute(
                f"SELECT {', '.join('s.' + c.strip() for c in _SYMBOL_COLUMNS.split(','))} "
                "FROM symbol_fts JOIN symbol s ON s.id = symbol_fts.rowid "
                f"WHERE symbol_fts MATCH ?{' AND s.kind = ?' if kind else ''} "
                "ORDER BY rank LIMIT ?",
                (match, *([kind] if kind else []), limit),
            )
            _take(found, seen, (_symbol(row) for row in rows), limit)
    return found


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _take(
    into: list[Symbol], seen: set[tuple[str, int, str]], more: Iterable[Symbol], limit: int
) -> None:
    for symbol in more:
        key = (symbol.path, symbol.line, symbol.name)
        if key in seen:
            continue
        seen.add(key)
        into.append(symbol)
        if len(into) >= limit:
            return


def refs(project_id: str, name: str, *, limit: int = 50) -> list[Ref]:
    """Every use of ``name`` (its last segment when qualified), in path and line order."""
    bare = name.rsplit(".", 1)[-1]
    with _connect(project_id, create=False) as conn:
        rows = conn.execute(
            "SELECT DISTINCT name, path, line FROM ref WHERE name = ? ORDER BY path, line LIMIT ?",
            (bare, limit),
        )
        return [Ref(*row) for row in rows]


def similar(
    project_id: str, target: str, *, limit: int = 10
) -> tuple[Symbol | None, list[Similar]]:
    """Definitions whose names share sub-tokens with ``target`` — a name, or ``path:line``.

    The DRY check before a new helper: ``similar parse_duration`` before writing one.
    Scored by the share of sub-tokens in common (Jaccard), plus a little for the same
    arity; the target itself is left out. The first item is the definition ``target``
    named, when it named one.
    """
    with _connect(project_id, create=False) as conn:
        anchor = _anchor(conn, target)
        name = anchor.name if anchor is not None else target.rsplit(".", 1)[-1]
        mine = set(subtokens(name))
        if not mine:
            return anchor, []
        match = " OR ".join(f'"{token}"' for token in sorted(mine))
        rows = conn.execute(
            f"SELECT {', '.join('s.' + c.strip() for c in _SYMBOL_COLUMNS.split(','))} "
            "FROM symbol_fts JOIN symbol s ON s.id = symbol_fts.rowid "
            "WHERE symbol_fts MATCH ? LIMIT 2000",
            (match,),
        ).fetchall()
    scored: list[Similar] = []
    for row in rows:
        candidate = _symbol(row)
        if anchor is not None and (candidate.path, candidate.line) == (anchor.path, anchor.line):
            continue
        theirs = set(subtokens(candidate.name))
        shared = mine & theirs
        if not shared:
            continue
        score = len(shared) / len(mine | theirs)
        if anchor is not None and anchor.arity is not None and anchor.arity == candidate.arity:
            score += 0.1
        scored.append(Similar(candidate, score, tuple(sorted(shared))))
    scored.sort(key=lambda s: (-s.score, s.symbol.path, s.symbol.line))
    return anchor, scored[:limit]


def _anchor(conn: sqlite3.Connection, target: str) -> Symbol | None:
    """The definition ``target`` names: ``path:line`` (the innermost one spanning it), or a name."""
    where = re.fullmatch(r"(.+):(\d+)", target)
    if where is not None:
        row = conn.execute(
            f"SELECT {_SYMBOL_COLUMNS} FROM symbol WHERE path = ? AND line <= ? AND end_line >= ? "
            "ORDER BY line DESC LIMIT 1",
            (where.group(1), int(where.group(2)), int(where.group(2))),
        ).fetchone()
        return _symbol(row) if row is not None else None
    column = "qualname" if "." in target else "name"
    row = conn.execute(
        f"SELECT {_SYMBOL_COLUMNS} FROM symbol WHERE {column} = ? ORDER BY path, line LIMIT 1",
        (target,),
    ).fetchone()
    return _symbol(row) if row is not None else None
