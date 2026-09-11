"""Native, local command reports. Commands run once; retrieval only reads saved bytes.

This is a wrapper for finite, non-interactive commands, not a PTY or a shell.
No command flags/environment are rewritten. Binary stdout/stderr are retained
separately with an explicit per-stream cap. Only known progress/hint lines are
removed from recognised formats; unexpected output and failure details survive.
"""

from __future__ import annotations

import json
import math
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any, BinaryIO, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from aisquare.core import orchestrator
from aisquare.core.paths import aisquare_home
from aisquare.core.source_revision import source_fingerprint, source_root_for
from aisquare.core.store import store_session

DEFAULT_STREAM_LIMIT = 1024 * 1024
MAX_STREAM_LIMIT = 16 * 1024 * 1024
RETAIN_REPORTS = 64
RETAIN_DAYS = 14
_REPORT_ID = re.compile(r"^[0-9a-f]{32}$")
_OFF = {"0", "false", "no", "off"}
# Strip complete terminal sequences before parsing; raw files never go through this.
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
_PROGRESS = re.compile(r"^(?:[^\r\n]*test[^\r\n]*\.py\s+)?[.sfxXFE]+\s*\[\s*\d+%\]\s*$")
#: A progress line is short; the optional path prefix above backtracks
#: quadratically, so anything longer is never even offered to it.
_PROGRESS_MAX = 512
_PYTEST_BANNER = re.compile(r"^=+ .+ =+$")
#: Report metadata is read back with this bound; argv is capped so the whole
#: indented document always fits.
_REPORT_JSON_LIMIT = 262_144
_ARGV_LIMIT = 32_768
_PYTEST_END = re.compile(
    r"(?:^=+\s*|^)(?:\d+ "
    r"(?:passed|failed|error|errors|skipped|deselected|xfailed|xpassed|warnings?)"
    r"(?:, | in |$))"
)


class StreamRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observed_bytes: int = Field(ge=0)
    retained_bytes: int = Field(ge=0)
    truncated: bool


class CommandReport(BaseModel):
    """Factual metadata; no persona text, code, hooks or credentials are loaded."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    argv: list[str]
    cwd: str
    started_at: datetime
    finished_at: datetime
    returncode: int
    signal: int | None = None
    launch_error: str | None = None
    #: The signal the OPERATOR sent the wrapper while the command ran, forwarded to
    #: the command's process group. Distinct from ``signal`` (how the child died):
    #: a child that swallowed the interrupt and exited 0 still shows the run was
    #: interrupted rather than looking like a clean pass.
    interrupted_by: int | None = None
    session_id: str | None = None
    task_id: str | None = None
    project_id: str | None = None
    pipeline_id: str | None = None
    completed: Literal[True] = True
    source_root: str | None = None
    source_fingerprint_before: str | None = None
    source_fingerprint_after: str | None = None
    source_capture_error: str | None = None
    stdout: StreamRecord
    stderr: StreamRecord
    compaction_enabled: bool
    format: str
    display_stdout_bytes: int = Field(ge=0)
    display_stderr_bytes: int = Field(ge=0)
    per_stream_limit: int

    @property
    def exit_code(self) -> int:
        """Shell's conventional status; returncode preserves actual signal identity."""
        return 128 + self.signal if self.signal is not None else self.returncode

    def metrics(self) -> dict[str, int | str]:
        retained = self.stdout.retained_bytes + self.stderr.retained_bytes
        displayed = self.display_stdout_bytes + self.display_stderr_bytes
        return {
            "observed_bytes": self.stdout.observed_bytes + self.stderr.observed_bytes,
            "retained_bytes": retained,
            "display_body_bytes": displayed,
            "compaction_removed_bytes": max(0, retained - displayed),
            "estimated_retained_tokens": math.ceil(retained / 4),
            "estimated_display_body_tokens": math.ceil(displayed / 4),
            "token_estimate_method": "UTF-8 byte count / 4, rounded up; not provider usage",
            "scope": "saved report bodies only; excludes receipts, prompts and other agent tools",
        }


def reports_dir() -> Path:
    return aisquare_home() / "reports"


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"Refusing report directory that is not a real directory: {path}")
    path.chmod(0o700)


def _directory(report_id: str) -> Path:
    if not _REPORT_ID.fullmatch(report_id):
        raise ValueError("Use the complete 32-character report ID.")
    root = reports_dir()
    path = root / report_id
    if root.is_symlink() or path.is_symlink():
        raise ValueError("Report directories cannot be symbolic links.")
    return path


def _read_file(path: Path, limit: int) -> bytes:
    """Bound reads and refuse links/devices, including a symlink swapped before open."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("Report content must be a regular file.")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Saved report exceeds its size limit.")
    return data


def load_report(report_id: str) -> CommandReport:
    report = CommandReport.model_validate_json(
        _read_file(_directory(report_id) / "report.json", _REPORT_JSON_LIMIT)
    )
    if report.id != report_id:
        raise ValueError("Saved report ID does not match its directory.")
    return report


def read_stream(report_id: str, stream: Literal["stdout", "stderr"], *, raw: bool = False) -> bytes:
    report = load_report(report_id)
    path = _directory(report_id) / (stream + (".bin" if raw else ".display"))
    data = _read_file(path, MAX_STREAM_LIMIT)
    expected = (
        getattr(report, stream).retained_bytes
        if raw
        else getattr(report, f"display_{stream}_bytes")
    )
    if len(data) != expected:
        raise ValueError("Saved report is incomplete or changed; byte counts do not match.")
    return data


def list_reports() -> list[CommandReport]:
    root = reports_dir()
    if root.is_symlink():
        raise ValueError("Report directory cannot be a symbolic link.")
    if not root.exists():
        return []
    result: list[CommandReport] = []
    for path in root.iterdir():
        if _REPORT_ID.fullmatch(path.name):
            try:
                result.append(load_report(path.name))
            except (OSError, ValueError):
                # A damaged entry does not hide other completed reports.
                continue
    return sorted(result, key=lambda item: item.finished_at, reverse=True)


def protected_report_ids() -> set[str]:
    """Reports that back recorded requirement evidence; retention must keep them.

    Imported lazily: work_briefs imports this module, and a store problem while
    reading evidence must not stop a command from running, so an unreadable
    board protects nothing rather than raising here.
    """
    try:
        from aisquare.services.work_briefs import referenced_report_ids

        return referenced_report_ids()
    except Exception:
        return set()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _prune_orphaned_pending(root: Path) -> list[str]:
    """A wrapper killed with SIGKILL leaves ``.pending-*`` behind forever otherwise.

    An entry is orphaned only when the wrapper pid it recorded is gone AND it is
    older than an hour; a live long-running command keeps its directory.
    """
    removed: list[str] = []
    for path in root.glob(".pending-*"):
        if path.is_symlink() or not path.is_dir():
            continue
        try:
            age = time.time() - path.stat().st_mtime
            pid_text = (path / "wrapper.pid").read_text(encoding="ascii").strip()
            alive = _pid_alive(int(pid_text))
        except (OSError, ValueError):
            # No readable pid record means we cannot prove the owner is gone: a
            # directory another runner is still filling must never be removed.
            continue
        if not alive and age > 3600:
            with suppress(OSError):
                shutil.rmtree(path)
                removed.append(path.name)
    return removed


def prune_reports(
    *, keep: int = RETAIN_REPORTS, days: int = RETAIN_DAYS, protect: Collection[str] = ()
) -> list[str]:
    """Remove completed owned records only, never unfinished concurrent runs.

    ``protect`` names reports that back recorded requirement evidence: deleting
    one would flip a VERIFIED brief to NOT VERIFIED, so those never count
    against ``keep`` and never expire here.
    """
    if keep < 0 or days < 0:
        raise ValueError("Retention values must be non-negative.")
    cutoff = time.time() - days * 86400
    protected = set(protect)
    removed: list[str] = []
    kept = 0
    for report in list_reports():
        if report.id in protected:
            continue
        if kept >= keep or report.finished_at.timestamp() < cutoff:
            path = _directory(report.id)
            try:
                shutil.rmtree(path)
            except FileNotFoundError:
                continue  # another runner pruned the same old completed report
            removed.append(report.id)
        else:
            kept += 1
    root = reports_dir()
    if root.is_dir() and not root.is_symlink():
        removed.extend(_prune_orphaned_pending(root))
    return removed


def safe_text(data: bytes) -> str:
    """Terminal-safe presentation; literal escaped controls, original bytes saved."""
    text = data.decode("utf-8", errors="backslashreplace")
    return "".join(
        ch if ch in "\n\t" or (ord(ch) >= 32 and not 127 <= ord(ch) <= 159) else f"\\x{ord(ch):02x}"
        for ch in text
    )


def compact_stdout(argv: Sequence[str], data: bytes, *, truncated: bool) -> tuple[str, bytes]:
    """Conservatively remove recognised noise. No summary invents a result."""
    if not data or truncated or b"\x00" in data:
        return "passthrough", data
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "passthrough", data
    # Do not interpret unusual terminal effects, carriage-return animations,
    # ANSI colour, JSON or machine formats. Raw passthrough is safer.
    if _ANSI.search(text) or any(ord(ch) < 32 and ch not in "\n\t" for ch in text):
        return "passthrough", data
    name = Path(argv[0]).name
    is_pytest = name in {"pytest", "py.test"} or (
        name.startswith("python") and list(argv[1:3]) == ["-m", "pytest"]
    )
    lines = text.splitlines(keepends=True)
    if (
        is_pytest
        and not _pytest_capture_disabled(argv[1:])
        and any(_PYTEST_END.search(line.strip()) for line in lines)
    ):
        compacted = "".join(_pytest_without_progress(lines))
        output = compacted.encode()
        if len(output) < len(data):
            return "pytest", output
    # Only ordinary git status: every changed path and status remains. We omit
    # Git's repeated usage hints, not branch/divergence/conflict information.
    if name == "git" and len(argv) >= 2 and argv[1] == "status":
        if any(arg in {"-z", "--short", "-s"} or arg.startswith("--porcelain") for arg in argv[2:]):
            return "passthrough", data
        headings = {
            "Changes to be committed:",
            "Changes not staged for commit:",
            "Untracked files:",
        }
        if any(line.rstrip() in headings for line in lines):
            hints = re.compile(
                r'^  \(use "git (?:add|restore|reset|rm|checkout) [^\r\n]*" to [^\r\n]*\)\n?$'
            )
            output = "".join(line for line in lines if not hints.fullmatch(line)).encode()
            if len(output) < len(data):
                return "git-status", output
    return "passthrough", data


def _pytest_capture_disabled(args: Sequence[str]) -> bool:
    """``-s`` alone or folded into a short-option cluster (``-sv``, ``-xqs``)."""
    for arg in args:
        if arg == "--capture" or arg.startswith("--capture="):
            return True
        if arg.startswith("-") and not arg.startswith("--") and "s" in arg[1:]:
            return True
    return False


def _pytest_without_progress(lines: Sequence[str]) -> Iterator[str]:
    """Drop progress lines from the collection zone only.

    Pytest prints ``tests/x.py ..F [ 40%]`` lines between its opening banner and
    the first section banner (FAILURES, ERRORS, warnings summary, …). A test's
    captured stdout inside a FAILURES section may contain look-alike lines, and
    those are part of the evidence, so once a banner is seen nothing is dropped.
    """
    in_zone = True
    for index, line in enumerate(lines):
        stripped = line.strip()
        if index > 0 and _PYTEST_BANNER.match(stripped) and "test session starts" not in stripped:
            in_zone = False
        if in_zone and len(stripped) <= _PROGRESS_MAX and _PROGRESS.fullmatch(stripped):
            continue
        yield line


class _Interrupts:
    """What the operator sent while the command ran, and the escalation ladder."""

    def __init__(self) -> None:
        self.first: int | None = None
        self.count = 0


@contextmanager
def _forward_signals(child: subprocess.Popen[bytes], seen: _Interrupts) -> Iterator[None]:
    """Forward SIGINT/SIGTERM/SIGHUP to the command's process group, escalating.

    A child that ignores SIGINT would otherwise hold the operator's terminal
    until they SIGKILL the wrapper, orphaning the child with the pipes open. The
    second interrupt sends SIGTERM to the group, the third SIGKILL; the report
    is still written, with what was sent recorded.
    """
    previous: dict[int, Callable[[int, FrameType | None], Any] | int | None] = {}
    if threading.current_thread() is threading.main_thread():

        def forward(number: int, frame: object) -> None:
            seen.count += 1
            if seen.first is None:
                seen.first = number
            escalated = (
                number if seen.count == 1 else signal.SIGTERM if seen.count == 2 else signal.SIGKILL
            )
            with suppress(ProcessLookupError):
                os.killpg(child.pid, escalated)

        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[number] = signal.signal(number, forward)
    try:
        yield
    finally:
        for old_number, handler in previous.items():
            signal.signal(old_number, handler)


@contextmanager
def _defer_interrupts() -> Iterator[None]:
    """The command already ran: finish publishing its report, THEN honour Ctrl-C.

    Discarding a report because the operator interrupted the source scan or the
    compaction step would lose the record of a command that did execute.
    """
    pending: list[int] = []
    previous: dict[int, Callable[[int, FrameType | None], Any] | int | None] = {}
    if threading.current_thread() is threading.main_thread():

        def defer(number: int, frame: object) -> None:
            pending.append(number)

        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(number, defer)
    try:
        yield
    finally:
        for old_number, handler in previous.items():
            signal.signal(old_number, handler)
        if pending:
            raise KeyboardInterrupt if pending[0] == signal.SIGINT else SystemExit(128 + pending[0])


def _capture(
    child: subprocess.Popen[bytes], directory: Path, limit: int, seen: _Interrupts
) -> dict[str, StreamRecord]:
    counts = {"stdout": 0, "stderr": 0}
    kept = {"stdout": 0, "stderr": 0}
    handles: dict[str, BinaryIO] = {}
    try:
        with selectors.DefaultSelector() as selector:
            for name, pipe in (("stdout", child.stdout), ("stderr", child.stderr)):
                assert pipe is not None
                fd = os.open(directory / f"{name}.bin", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                handles[name] = os.fdopen(fd, "wb")
                selector.register(pipe, selectors.EVENT_READ, data=name)
            with _forward_signals(child, seen):
                while selector.get_map():
                    for key, _ in selector.select():
                        pipe = cast(BinaryIO, key.fileobj)
                        data = os.read(pipe.fileno(), 65536)
                        if not data:
                            selector.unregister(pipe)
                            pipe.close()
                            continue
                        name = str(key.data)
                        counts[name] += len(data)
                        retained = data[: max(0, limit - kept[name])]
                        handles[name].write(retained)
                        kept[name] += len(retained)
                child.wait()
    except BaseException:
        # A failed storage write must not leave an invisible child running.
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        child.wait()
        raise
    finally:
        for handle in handles.values():
            handle.close()
        for pipe in (child.stdout, child.stderr):
            if pipe is not None:
                pipe.close()
    return {
        name: StreamRecord(
            observed_bytes=counts[name],
            retained_bytes=kept[name],
            truncated=counts[name] > kept[name],
        )
        for name in counts
    }


def _write_file(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _source_before(project_id: str | None, cwd: Path) -> tuple[Path | None, str | None, str | None]:
    """Opt-in proof with --project; a capture failure never prevents execution."""
    if project_id is None:
        return None, None, None
    root: Path | None = None
    try:
        with store_session() as store:
            project = store.get_project(project_id)
        if project is None:
            raise ValueError("project ID is not registered")
        root = project.root.resolve()
        if not cwd.is_relative_to(root) and orchestrator.team_project(cwd).root.resolve() != root:
            raise ValueError("command working directory is outside the named project's board")
        root = source_root_for(root, cwd)
        return root, source_fingerprint(root), None
    except Exception as exc:  # source capture is an observer, never an execution gate
        # No usable "before" means no proof either way; do not spend an "after" scan.
        return None, None, f"Before command: {exc}"[:2048]


def _source_after(root: Path | None) -> tuple[str | None, str | None]:
    if root is None:
        return None, None
    try:
        return source_fingerprint(root), None
    except Exception as exc:  # unknown proof is safer than cancelling the actual command
        return None, f"After command: {exc}"[:2048]


def run_command(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    compact: bool = True,
    session_id: str | None = None,
    task_id: str | None = None,
    project_id: str | None = None,
    max_output_bytes: int = DEFAULT_STREAM_LIMIT,
) -> CommandReport:
    """Execute argv exactly once (no shell), publish original files and metadata.

    Completed reports expire after 14 days / newest 64. Original streams retain
    their first 1 MiB by default, not an unbounded log; truncation is explicit.
    Retrieval cannot rerun the command. Signals are forwarded to the command's
    process group, and its negative signal returncode is retained in metadata.
    """
    if not argv or not argv[0] or any("\x00" in arg for arg in argv):
        raise ValueError("Provide a command and arguments without NUL characters.")
    if not 1 <= max_output_bytes <= MAX_STREAM_LIMIT:
        raise ValueError(f"Output limit must be between 1 and {MAX_STREAM_LIMIT} bytes per stream.")
    # Measured the way the report is WRITTEN (indented), against the same bound.
    if len(json.dumps(list(argv), indent=2).encode()) > _ARGV_LIMIT:
        raise ValueError("Command arguments exceed the report's 32 KiB metadata limit.")
    pipeline_id = os.environ.get("AISQUARE_PIPELINE_ID") or None
    for value in (session_id, task_id, project_id, pipeline_id):
        if value is not None and (len(value) > 256 or any(ord(ch) < 32 for ch in value)):
            raise ValueError(
                "Correlation identifiers must be at most 256 characters without controls."
            )
    directory_root = reports_dir()
    _private_directory(directory_root)
    prune_reports(keep=RETAIN_REPORTS - 1, protect=protected_report_ids())
    report_id = uuid.uuid4().hex
    pending = directory_root / f".pending-{report_id}"
    pending.mkdir(mode=0o700)
    _write_file(pending / "wrapper.pid", f"{os.getpid()}\n".encode("ascii"))
    seen = _Interrupts()
    started = datetime.now(UTC)
    working_dir = (cwd or Path.cwd()).resolve()
    launch_error = None
    source_root, source_before, source_error = _source_before(project_id, working_dir)
    try:
        try:
            child = subprocess.Popen(
                list(argv),
                cwd=working_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            launch_error = str(exc)[:4096]
            returncode = 127 if isinstance(exc, FileNotFoundError) else 126
            error_data = f"Unable to execute {argv[0]}: {exc}\n".encode()
            _write_file(pending / "stdout.bin", b"")
            _write_file(pending / "stderr.bin", error_data[:max_output_bytes])
            records = {
                "stdout": StreamRecord(observed_bytes=0, retained_bytes=0, truncated=False),
                "stderr": StreamRecord(
                    observed_bytes=len(error_data),
                    retained_bytes=min(len(error_data), max_output_bytes),
                    truncated=len(error_data) > max_output_bytes,
                ),
            }
        else:
            records = _capture(child, pending, max_output_bytes, seen)
            returncode = child.returncode
        with _defer_interrupts():
            return _publish(
                pending,
                CommandReport(
                    id=report_id,
                    argv=list(argv),
                    cwd=str(working_dir),
                    started_at=started,
                    finished_at=started,  # replaced when the report is finalized
                    returncode=returncode,
                    signal=-returncode if returncode < 0 else None,
                    launch_error=launch_error,
                    interrupted_by=seen.first,
                    session_id=session_id,
                    task_id=task_id,
                    project_id=project_id,
                    pipeline_id=pipeline_id,
                    source_root=str(source_root) if source_root is not None else None,
                    source_fingerprint_before=source_before,
                    source_capture_error=source_error,
                    stdout=records["stdout"],
                    stderr=records["stderr"],
                    compaction_enabled=False,
                    format="raw",
                    display_stdout_bytes=0,
                    display_stderr_bytes=0,
                    per_stream_limit=max_output_bytes,
                ),
                source_root=source_root,
                compact=compact,
            )
    except BaseException:
        shutil.rmtree(pending, ignore_errors=True)
        raise


def _publish(
    pending: Path, draft: CommandReport, *, source_root: Path | None, compact: bool
) -> CommandReport:
    """Everything after the child exited: after-scan, compaction, files, rename."""
    source_after, after_error = _source_after(source_root)
    enabled = compact and os.environ.get("AISQUARE_REPORTS", "").lower() not in _OFF
    original = _read_file(pending / "stdout.bin", MAX_STREAM_LIMIT)
    format_name, displayed = (
        compact_stdout(draft.argv, original, truncated=draft.stdout.truncated)
        if enabled
        else ("raw", original)
    )
    stderr = _read_file(pending / "stderr.bin", MAX_STREAM_LIMIT)
    _write_file(pending / "stdout.display", displayed)
    _write_file(pending / "stderr.display", stderr)
    report = draft.model_copy(
        update={
            "finished_at": datetime.now(UTC),
            "source_fingerprint_after": source_after,
            "source_capture_error": draft.source_capture_error or after_error,
            "compaction_enabled": enabled,
            "format": format_name,
            "display_stdout_bytes": len(displayed),
            "display_stderr_bytes": len(stderr),
        }
    )
    payload = report.model_dump_json(indent=2).encode()
    if len(payload) > _REPORT_JSON_LIMIT:
        raise ValueError("Report metadata exceeds its size limit.")
    _write_file(pending / "report.json", payload)
    (pending / "wrapper.pid").unlink(missing_ok=True)
    pending.rename(_directory(report.id))
    return report
