"""Pack a project's codebase with Repomix for fast, low-token LLM understanding.

Mirrors AISquare's server-side Repomix packing so artifacts stay consistent for
future sync:

- **full pack** — ``repomix --style xml`` (directory tree + every file's contents);
- **skeleton** — ``repomix --compress --style xml`` (signatures/imports, bodies dropped);
- **index** — a per-file map of char offsets + token counts, parsed from the pack.

Adaptive, like the server: use the full pack when it fits the token cap; else the
compressed pack; else mark the snapshot ``too_large`` and store no pack. Repomix
is a Node CLI — we shell out to ``repomix`` (or ``npx repomix``); if neither is
available the snapshot is skipped, not fatal.
"""

from __future__ import annotations

import importlib
import json
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from aisquare.core import paths
from aisquare.models import Snapshot


class IndexEntry(TypedDict):
    """One file's location in the pack: char offsets + token count, for slicing."""

    path: str
    start: int
    end: int
    token_count: int


MAX_TOKENS = 150_000  # mirrors REPO_PACK_MAX_TOKENS
_PACK_TIMEOUT = 600
_FILE_TAG = re.compile(r'<file path="([^"]+)">')
_TOTAL_TOKENS = re.compile(r"Total Tokens:\s*([\d,]+)", re.IGNORECASE)
_CLOSE_TAG = "</file>"


class RepomixUnavailableError(RuntimeError):
    """Neither a ``repomix`` binary nor ``npx`` is available to pack the repo."""


def snapshot_dir(project_id: str) -> Path:
    return paths.project_data_dir(project_id) / "snapshot"


def pack_path(project_id: str) -> Path:
    return snapshot_dir(project_id) / "pack.repomix.xml"


def skeleton_path(project_id: str) -> Path:
    return snapshot_dir(project_id) / "skeleton.repomix.xml"


def index_path(project_id: str) -> Path:
    return snapshot_dir(project_id) / "index.json"


def meta_path(project_id: str) -> Path:
    return snapshot_dir(project_id) / "snapshot.json"


def exists(project_id: str) -> bool:
    return meta_path(project_id).exists()


def load(project_id: str) -> Snapshot | None:
    path = meta_path(project_id)
    if not path.exists():
        return None
    return Snapshot.model_validate_json(path.read_text(encoding="utf-8"))


def head_sha(root: Path) -> str | None:
    """Best-effort current commit of ``root``."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return result.stdout.strip() or None


def _repomix_base() -> list[str]:
    """The argv prefix that runs repomix, as an already-resolved path.

    ``shutil.which`` has done the PATH lookup, so the resolved path is what
    gets run: on Windows these are ``.CMD`` shims, and ``CreateProcess`` does
    not apply ``PATHEXT``, so handing ``subprocess`` the bare name raises
    ``FileNotFoundError`` even though the tool is installed and on PATH.
    Passing the resolved path is also marginally better on POSIX — one PATH
    walk instead of two, and immune to PATH changing in between.
    """
    direct = shutil.which("repomix")
    if direct:
        return [direct]
    npx = shutil.which("npx")
    if npx:
        return [npx, "--yes", "repomix"]
    raise RepomixUnavailableError(
        "repomix not found — install Node.js then `npm install -g repomix`"
    )


def _run_repomix(root: Path, *, compress: bool) -> tuple[str, str]:
    """Run repomix in ``root``; return (pack text, repomix stdout)."""
    base = _repomix_base()
    with tempfile.TemporaryDirectory(prefix="aisquare-repomix-") as tmp:
        out = Path(tmp) / "pack.xml"
        argv = [*base, "--style", "xml", "-o", str(out)]
        if compress:
            argv.insert(len(base), "--compress")
        result = subprocess.run(
            argv,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PACK_TIMEOUT,
            check=True,
        )
        text = out.read_text(encoding="utf-8") if out.exists() else ""
    return text, result.stdout or ""


def _tiktoken_count(text: str) -> int | None:
    """Exact o200k_base token count if tiktoken is installed, else ``None``."""
    try:
        tiktoken = importlib.import_module("tiktoken")
    except ImportError:
        return None
    try:
        return len(tiktoken.get_encoding("o200k_base").encode(text))
    except Exception:
        return None


def _total_tokens(pack_text: str, repomix_stdout: str) -> int:
    exact = _tiktoken_count(pack_text)
    if exact is not None:
        return exact
    match = _TOTAL_TOKENS.search(repomix_stdout)  # repomix prints its own count
    if match:
        return int(match.group(1).replace(",", ""))
    return len(pack_text) // 4  # rough fallback when neither is available


def _slice_tokens(text: str) -> int:
    exact = _tiktoken_count(text)
    return exact if exact is not None else len(text) // 4


def _build_index(pack_text: str) -> list[IndexEntry]:
    """Map each ``<file path="...">`` block to its char offsets and token count."""
    matches = list(_FILE_TAG.finditer(pack_text))
    entries: list[IndexEntry] = []
    for position, match in enumerate(matches):
        start = match.start()
        search_end = (
            matches[position + 1].start() if position + 1 < len(matches) else len(pack_text)
        )
        close = pack_text.rfind(_CLOSE_TAG, match.end(), search_end)
        end = close + len(_CLOSE_TAG) if close != -1 else search_end
        entries.append(
            {
                "path": match.group(1),
                "start": start,
                "end": end,
                "token_count": _slice_tokens(pack_text[start:end]),
            }
        )
    return entries


def generate(project_id: str, root: Path, *, head: str | None = None) -> Snapshot:
    """Pack ``root`` and write the snapshot artifacts under the project's data dir.

    Raises :class:`RepomixUnavailableError` if repomix cannot be run.
    """
    directory = snapshot_dir(project_id)
    directory.mkdir(parents=True, exist_ok=True)
    meta = Snapshot(
        project_id=project_id,
        head_sha=head,
        generated_at=datetime.now(tz=UTC),
        pack_path=pack_path(project_id),
        skeleton_path=skeleton_path(project_id),
        index_path=index_path(project_id),
    )

    full_text, full_stdout = _run_repomix(root, compress=False)
    full_tokens = _total_tokens(full_text, full_stdout)

    if full_tokens <= MAX_TOKENS:
        _write_pack(meta, full_text, tokens=full_tokens, compressed=False)
        try:  # best-effort skeleton; the full pack still ships if this fails
            skeleton_text, skeleton_stdout = _run_repomix(root, compress=True)
            meta.skeleton_path.write_text(skeleton_text, encoding="utf-8")
            meta.skeleton_token_count = _total_tokens(skeleton_text, skeleton_stdout)
        except (subprocess.SubprocessError, OSError):
            pass
    else:
        compressed_text, compressed_stdout = _run_repomix(root, compress=True)
        compressed_tokens = _total_tokens(compressed_text, compressed_stdout)
        if compressed_tokens <= MAX_TOKENS:
            _write_pack(meta, compressed_text, tokens=compressed_tokens, compressed=True)
        else:
            meta.token_count = compressed_tokens
            meta.compressed = True
            meta.status = "too_large"

    meta_path(project_id).write_text(meta.model_dump_json(indent=2), encoding="utf-8")
    return meta


def _write_pack(meta: Snapshot, pack_text: str, *, tokens: int, compressed: bool) -> None:
    meta.pack_path.write_text(pack_text, encoding="utf-8")
    index = _build_index(pack_text)
    meta.index_path.write_text(json.dumps(index), encoding="utf-8")
    meta.token_count = tokens
    meta.compressed = compressed
    meta.file_count = len(index)
    meta.status = "ready"
