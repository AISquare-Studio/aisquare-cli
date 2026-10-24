"""``aisquare code``: the project's symbol index, as the CLI and the MCP tool ask it (#285).

The project is the one the working directory is in (``core.workspace.current_project``),
so a fleet coder in its worktree asks the project the worktree belongs to; the index is of
the project's root checkout. Services return data; the CLI prints.
"""

from __future__ import annotations

from pathlib import Path

from aisquare.core import code_index
from aisquare.core import snapshot as snapshot_core
from aisquare.core.config import CodeSettings, SnapshotSettings, load_config
from aisquare.core.workspace import current_project
from aisquare.models import ProjectInfo

INDEX_COMMAND = "aisquare code index"
EXTRA_HINT = 'pip install --upgrade "aisquare-cli[code]"'
"""What adds the TypeScript and JavaScript parsers: the CLI named first, as every extra's
hint names it (``services.explainability.INSTALL_HINT`` says why)."""


class CodeIndexError(RuntimeError):
    """No index to ask, or one being built: the message says what to run."""


def settings() -> CodeSettings:
    """``[code]``; the defaults when the config is unreadable (fail-open)."""
    try:
        return load_config().code
    except Exception:
        return CodeSettings()


def _snapshot_settings() -> SnapshotSettings:
    try:
        return load_config().snapshot
    except Exception:
        return SnapshotSettings()


def project_here(cwd: Path | None = None) -> ProjectInfo:
    return current_project(cwd)


def build_index(
    project: ProjectInfo | None = None, *, full: bool = False
) -> code_index.BuildReport:
    """Bring ``project``'s index up to date (``full``: from nothing). Raises
    :class:`CodeIndexError` when another process is building it."""
    target = project or project_here()
    try:
        return code_index.build(
            target.id,
            target.root,
            full=full,
            max_file_kb=settings().max_file_kb,
            ignore=_snapshot_settings().ignore,
            head=snapshot_core.head_sha(target.root),
        )
    except code_index.IndexBusy as exc:
        raise CodeIndexError(f"{exc} — try again in a moment") from None


def index_status(project: ProjectInfo | None = None) -> code_index.IndexStatus:
    target = project or project_here()
    return code_index.status(target.id, current_head=snapshot_core.head_sha(target.root))


def _indexed(project: ProjectInfo | None) -> ProjectInfo:
    target = project or project_here()
    if not code_index.exists(target.id):
        raise CodeIndexError(f"no code index for {target.root} yet — build it: {INDEX_COMMAND}")
    return target


def find_symbols(
    name: str, *, kind: str | None = None, limit: int = 20, project: ProjectInfo | None = None
) -> list[code_index.Symbol]:
    return code_index.find(_indexed(project).id, name, kind=kind, limit=limit)


def find_refs(
    name: str, *, limit: int = 50, project: ProjectInfo | None = None
) -> list[code_index.Ref]:
    return code_index.refs(_indexed(project).id, name, limit=limit)


def find_similar(
    target: str, *, limit: int = 10, project: ProjectInfo | None = None
) -> tuple[code_index.Symbol | None, list[code_index.Similar]]:
    return code_index.similar(_indexed(project).id, target, limit=limit)
