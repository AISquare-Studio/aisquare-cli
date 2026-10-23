"""Command reports: ``asq exec`` runs a command once and records how it ended.

A report is the record that a command ran, in which directory, on which source, and how
it ended, so a later step can cite it instead of running the command again. It holds the
exit status (and the signal, when the command died of one), what the operator sent while
it ran, whether both streams were kept whole, any launch error, and — with ``--project``
— the checkout's per-file source identity before and after (``core/source_revision``).

This wraps finite, non-interactive commands; it is not a shell and not a terminal. The
argv is passed unchanged, standard input is inherited, and both output streams go
through pipes into the report's own files. Only two recognised kinds of standard output
are shortened for display (:func:`compact_stdout`); the originals are always kept.

A report directory appears under ``reports/`` only once its files and metadata are all
written: a run in progress lives in ``.pending-<id>`` and is renamed at the end.
"""

from __future__ import annotations

import json
import math
import os
import re
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
from typing import IO, Any, BinaryIO, Literal

from pydantic import BaseModel, ConfigDict, Field

from aisquare.core import orchestrator
from aisquare.core.insights import RUN_KEY_ENV_VAR
from aisquare.core.paths import aisquare_home
from aisquare.core.source_revision import fingerprint_of, source_file_hashes, source_root_for
from aisquare.core.store import store_session

DEFAULT_STREAM_LIMIT = 1024 * 1024
MAX_STREAM_LIMIT = 16 * 1024 * 1024
RETAIN_REPORTS = 64
RETAIN_DAYS = 14
_POSIX = os.name == "posix"
_REPORT_ID = re.compile(r"^[0-9a-f]{32}$")
_OFF = {"0", "false", "no", "off"}
#: report.json is read back with this bound; argv is capped so the document always fits.
_REPORT_JSON_LIMIT = 262_144
_ARGV_LIMIT = 32_768
#: source.json holds the two per-file maps. A checkout too large for this keeps only the
#: combined fingerprints, and its report says so (``source_files_saved``).
_SOURCE_JSON_LIMIT = 32 * 1024 * 1024
# Complete terminal sequences; their presence makes output pass through unchanged.
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
_PROGRESS = re.compile(r"^(?:[^\r\n]*test[^\r\n]*\.py\s+)?[.sfxXFE]+\s*\[\s*\d+%\]\s*$")
#: A progress line is short; the optional path prefix above backtracks quadratically, so
#: a longer line is never offered to it.
_PROGRESS_MAX = 512
_PYTEST_BANNER = re.compile(r"^=+ .+ =+$")
_PYTEST_END = re.compile(
    r"(?:^=+\s*|^)(?:\d+ "
    r"(?:passed|failed|error|errors|skipped|deselected|xfailed|xpassed|warnings?)"
    r"(?:, | in |$))"
)
_GIT_HINT = re.compile(
    r'^  \(use "git (?:add|restore|reset|rm|checkout) [^\r\n]*" to [^\r\n]*\)\n?$'
)
_GIT_HEADINGS = frozenset(
    {"Changes to be committed:", "Changes not staged for commit:", "Untracked files:"}
)


class StreamRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observed_bytes: int = Field(ge=0)
    retained_bytes: int = Field(ge=0)
    truncated: bool


class CommandReport(BaseModel):
    """What one ``asq exec`` run did. Facts only: no display text, code or credentials."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    argv: list[str]
    cwd: str
    started_at: datetime
    finished_at: datetime
    #: The child's return code as ``subprocess`` reports it: negative for a signal.
    returncode: int
    #: The signal the command died of, or None.
    signal: int | None = None
    #: Why the command could not start; its returncode is then 127 or 126.
    launch_error: str | None = None
    #: The first signal the OPERATOR sent while the command ran. A command that caught
    #: the interrupt and still exited 0 was interrupted all the same.
    interrupted_by: int | None = None
    #: The command exited but something it started kept a pipe open, so capture stopped
    #: after a bounded drain: output written after that was not kept.
    output_pipes_held_open: bool = False
    session_id: str | None = None
    task_id: str | None = None
    project_id: str | None = None
    #: The Explainability run key in the environment, when one was set.
    pipeline_id: str | None = None
    #: The checkout fingerprinted with ``--project``; None without it, or when the
    #: capture failed before a root was known.
    source_root: str | None = None
    source_fingerprint_before: str | None = None
    source_fingerprint_after: str | None = None
    #: Whether the per-file maps behind both fingerprints are in ``source.json``.
    source_files_saved: bool = False
    #: Set whenever an identity could not be taken: the proof is then unknown.
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
        """The shell's convention: 128 + N for a death by signal N."""
        return 128 + self.signal if self.signal is not None else self.returncode

    @property
    def output_complete(self) -> bool:
        """Both streams kept whole: nothing past the cap, nothing after a held-open pipe."""
        return not (self.stdout.truncated or self.stderr.truncated or self.output_pipes_held_open)

    @property
    def source_known(self) -> bool:
        """Both identities were taken. False without ``--project``."""
        return (
            self.source_capture_error is None
            and self.source_fingerprint_before is not None
            and self.source_fingerprint_after is not None
        )

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


class SourceFiles(BaseModel):
    """The per-file identities behind a report's two fingerprints."""

    model_config = ConfigDict(extra="forbid")
    before: dict[str, str]
    after: dict[str, str] | None = None


def reports_dir() -> Path:
    return aisquare_home() / "reports"


# --------------------------------------------------------------------------- storage


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
    """A bounded read that refuses links and devices, including a link swapped in late."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("Report content must be a regular file.")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Saved report exceeds its size limit.")
    return data


def _create(path: Path) -> BinaryIO:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    return os.fdopen(os.open(path, flags, 0o600), "wb")


def _write_file(path: Path, data: bytes) -> None:
    with _create(path) as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def load_report(report_id: str) -> CommandReport:
    report = CommandReport.model_validate_json(
        _read_file(_directory(report_id) / "report.json", _REPORT_JSON_LIMIT)
    )
    if report.id != report_id:
        raise ValueError("Saved report ID does not match its directory.")
    return report


def load_source_files(report_id: str) -> SourceFiles | None:
    """The per-file maps of a ``--project`` report, or None when none were saved."""
    if not load_report(report_id).source_files_saved:
        return None
    return SourceFiles.model_validate_json(
        _read_file(_directory(report_id) / "source.json", _SOURCE_JSON_LIMIT)
    )


def read_stream(report_id: str, stream: Literal["stdout", "stderr"], *, raw: bool = False) -> bytes:
    """A saved stream's bytes: the original with ``raw``, else what was displayed."""
    report = load_report(report_id)
    data = _read_file(
        _directory(report_id) / (stream + (".bin" if raw else ".display")), MAX_STREAM_LIMIT
    )
    record: StreamRecord = getattr(report, stream)
    expected = record.retained_bytes if raw else getattr(report, f"display_{stream}_bytes")
    if len(data) != expected:
        raise ValueError("Saved report is incomplete or changed; byte counts do not match.")
    return data


def list_reports() -> list[CommandReport]:
    """Completed reports, newest first. A damaged one does not hide the others."""
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
                continue
    return sorted(result, key=lambda item: item.finished_at, reverse=True)


# --------------------------------------------------------------------------- retention


def _no_protected_reports() -> Collection[str]:
    return frozenset()


#: Names the reports that back recorded evidence, which retention must never delete.
#: Nothing records evidence yet; the evidence store replaces this with its own reader.
protected_reports_provider: Callable[[], Collection[str]] = _no_protected_reports


def protected_report_ids() -> set[str] | None:
    """The reports retention must keep, or None when that could not be determined.

    None is "unknown", never "nothing protected": deleting a report that backs evidence
    turns a proven requirement unproven for good, so :func:`prune_reports` deletes no
    report at all on None. Never raises: a command must still run when the evidence
    store cannot be read.
    """
    try:
        return set(protected_reports_provider())
    except Exception:
        return None


def _pid_alive(pid: int) -> bool | None:
    """Whether ``pid`` runs; None where that cannot be asked.

    On Windows ``os.kill`` terminates the process whatever the signal number, so a
    liveness probe there would kill the wrapper it asks about."""
    if not _POSIX:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _prune_orphaned_pending(root: Path) -> list[str]:
    """Remove the ``.pending-*`` of a wrapper that was killed outright.

    Only when the wrapper pid it recorded is gone and it is over an hour old. A run whose
    wrapper is alive is never removed, however long it takes. Where the pid cannot be
    asked (Windows), a directory over a day old is taken as abandoned.
    """
    removed: list[str] = []
    for path in root.glob(".pending-*"):
        if path.is_symlink() or not path.is_dir():
            continue
        try:
            age = time.time() - path.stat().st_mtime
            alive = _pid_alive(int((path / "wrapper.pid").read_text(encoding="ascii").strip()))
        except (OSError, ValueError):
            # No readable pid: nothing proves the owner is gone.
            continue
        if (alive is False and age > 3600) or (alive is None and age > 86400):
            with suppress(OSError):
                shutil.rmtree(path)
                removed.append(path.name)
    return removed


def prune_reports(
    *, keep: int = RETAIN_REPORTS, days: int = RETAIN_DAYS, protect: Collection[str] | None
) -> list[str]:
    """Apply retention to completed reports; return the IDs removed.

    ``protect`` names reports that back recorded evidence: they never expire and never
    count toward ``keep``. ``None`` means protection is unknown, and then no report is
    deleted at all — only orphaned pending directories, which are never evidence.

    A ``--project`` report that is not (yet) protected is an evidence candidate: it is
    kept for the whole ``days`` window whatever the count, so a burst of newer runs
    cannot delete it before its evidence is recorded. It still expires by age.
    """
    if keep < 0 or days < 0:
        raise ValueError("Retention values must be non-negative.")
    removed: list[str] = []
    if protect is not None:
        cutoff = time.time() - days * 86400
        protected = set(protect)
        kept = 0
        for report in list_reports():
            if report.id in protected:
                continue
            expired = report.finished_at.timestamp() < cutoff
            if not expired and report.project_id is not None:
                continue
            if not expired and kept < keep:
                kept += 1
                continue
            try:
                shutil.rmtree(_directory(report.id))
            except FileNotFoundError:
                continue  # another run pruned it first
            removed.append(report.id)
    root = reports_dir()
    if root.is_dir() and not root.is_symlink():
        removed.extend(_prune_orphaned_pending(root))
    return removed


# --------------------------------------------------------------------------- display


def safe_text(data: bytes) -> str:
    """Terminal-safe text: controls escaped literally. The saved bytes are untouched."""
    text = data.decode("utf-8", errors="backslashreplace")
    return "".join(
        ch if ch in "\n\t" or (ord(ch) >= 32 and not 127 <= ord(ch) <= 159) else f"\\x{ord(ch):02x}"
        for ch in text
    )


def compact_stdout(argv: Sequence[str], data: bytes, *, truncated: bool) -> tuple[str, bytes]:
    """``(format, display bytes)``: two recognised kinds of noise removed, else unchanged.

    Only git status's repeated usage hints, and pytest's percentage progress lines
    before the first section banner when a final count is present. Anything truncated,
    binary, coloured, or carrying control characters passes through, as does every
    other command. Nothing is summarised and no line is invented.
    """
    if not data or truncated or b"\x00" in data:
        return "passthrough", data
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "passthrough", data
    if _ANSI.search(text) or any(ord(ch) < 32 and ch not in "\n\t" for ch in text):
        return "passthrough", data
    name = Path(argv[0]).name.lower().removesuffix(".exe")
    lines = text.splitlines(keepends=True)
    is_pytest = name in {"pytest", "py.test"} or (
        name.startswith("python") and list(argv[1:3]) == ["-m", "pytest"]
    )
    if (
        is_pytest
        and not _pytest_capture_changed(argv[1:])
        and any(_PYTEST_END.search(line.strip()) for line in lines)
    ):
        output = "".join(_pytest_without_progress(lines)).encode()
        if len(output) < len(data):
            return "pytest", output
    if name == "git" and len(argv) >= 2 and argv[1] == "status":
        if _git_machine_format(argv[2:]):
            return "passthrough", data
        if any(line.rstrip() in _GIT_HEADINGS for line in lines):
            output = "".join(line for line in lines if not _GIT_HINT.fullmatch(line)).encode()
            if len(output) < len(data):
                return "git-status", output
    return "passthrough", data


def _git_machine_format(args: Sequence[str]) -> bool:
    """``--porcelain``, ``--short``, ``-z`` and friends, including in a cluster (``-sb``)."""
    for arg in args:
        if arg in {"--short", "--null"} or arg.startswith("--porcelain"):
            return True
        if arg.startswith("-") and not arg.startswith("--") and ({"s", "z"} & set(arg[1:])):
            return True
    return False


def _pytest_capture_changed(args: Sequence[str]) -> bool:
    """``-s`` alone or in a cluster (``-sv``, ``-xqs``), or any ``--capture``."""
    for arg in args:
        if arg == "--capture" or arg.startswith("--capture="):
            return True
        if arg.startswith("-") and not arg.startswith("--") and "s" in arg[1:]:
            return True
    return False


def _pytest_without_progress(lines: Sequence[str]) -> Iterator[str]:
    """Drop progress lines up to the first section banner only.

    After a banner (FAILURES, ERRORS, warnings summary, …) a look-alike line is a test's
    captured output, which is evidence, so nothing past it is dropped.
    """
    in_zone = True
    for index, line in enumerate(lines):
        stripped = line.strip()
        if index > 0 and _PYTEST_BANNER.match(stripped) and "test session starts" not in stripped:
            in_zone = False
        if in_zone and len(stripped) <= _PROGRESS_MAX and _PROGRESS.fullmatch(stripped):
            continue
        yield line


# --------------------------------------------------------------------------- running


class _Interrupts:
    """What the operator sent while the command ran."""

    def __init__(self) -> None:
        self.first: int | None = None
        self.count = 0


def _escalate(child: subprocess.Popen[bytes], number: int, count: int) -> None:
    """Pass the ``count``-th interrupt on: as sent, then SIGTERM, then SIGKILL.

    On POSIX to the command's whole process group, which is the session ``run_command``
    started it in. Windows has no process groups to signal: the child shares the console
    and already received the Ctrl-C itself, so the first is not repeated, and the second
    and third terminate the child.
    """
    if _POSIX:
        sent = number if count == 1 else signal.SIGTERM if count == 2 else signal.SIGKILL
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(child.pid, sent)
    elif count >= 2:
        with suppress(OSError):
            child.kill()


def _interrupt_signals() -> list[int]:
    names = ("SIGINT", "SIGTERM", "SIGHUP") if _POSIX else ("SIGINT", "SIGBREAK")
    return [getattr(signal, name) for name in names if hasattr(signal, name)]


@contextmanager
def _forward_signals(child: subprocess.Popen[bytes], seen: _Interrupts) -> Iterator[None]:
    """While the command runs, an interrupt goes to it, escalating (:func:`_escalate`).

    A command that ignores Ctrl-C would otherwise hold the terminal until the wrapper
    itself is killed, orphaning the command. Python delivers signals to the main thread
    only, so from any other thread nothing is installed.
    """
    previous: dict[int, Callable[[int, FrameType | None], Any] | int | None] = {}
    if threading.current_thread() is threading.main_thread():

        def forward(number: int, frame: FrameType | None) -> None:
            seen.count += 1
            if seen.first is None:
                seen.first = number
            _escalate(child, number, seen.count)

        for number in _interrupt_signals():
            previous[number] = signal.signal(number, forward)
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


class InterruptedAfterSave(Exception):
    """An interrupt arrived while a report was being saved; the report IS saved.

    Raised once publishing is done, so the caller can still name the report, which an
    interrupt raised from inside publishing would have lost."""

    def __init__(self, report: CommandReport, number: int) -> None:
        super().__init__(f"interrupted by signal {number} after report {report.id} was saved")
        self.report = report
        self.number = number


@contextmanager
def _defer_interrupts() -> Iterator[list[int]]:
    """The command already ran: hold any interrupt until its report is published.

    Yields the signals that arrived meanwhile, for the caller to honour once it has the
    saved report in hand (:class:`InterruptedAfterSave`)."""
    pending: list[int] = []
    previous: dict[int, Callable[[int, FrameType | None], Any] | int | None] = {}
    if threading.current_thread() is threading.main_thread():

        def defer(number: int, frame: FrameType | None) -> None:
            pending.append(number)

        for number in _interrupt_signals():
            previous[number] = signal.signal(number, defer)
    try:
        yield pending
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


#: How often the wait re-checks for the command's exit; a signal handler runs in between.
_POLL_WHILE_RUNNING = 0.1

#: After the command exits, how long a pipe something else still holds open is drained
#: before capture stops. Renewed while bytes keep arriving, so real trailing output is
#: kept; it ends only after this much silence. Without it, ``asq exec -- sh -c 'setsid
#: sleep 600 & echo started'`` blocked for the sleeper's whole lifetime.
_DRAIN_AFTER_EXIT = 2.0

#: However much keeps arriving, capture stops this long after the command exited. The
#: silence window alone is renewed by every byte, so a leftover process that writes a
#: line every second (a server's heartbeat, a watcher) would hold ``asq exec`` forever.
_DRAIN_MAX = 10.0


class _Reader:
    """Copies one pipe into its file, keeping the first ``limit`` bytes.

    A thread per pipe rather than ``select``: Windows cannot select on a pipe. The thread
    owns its pipe and closes it at EOF. Once :meth:`stop` is called it keeps reading, so a
    leftover writer never blocks on a full pipe, but writes nothing more: the report is
    already being sealed.

    It HAS a thread rather than being one: a subclass's attributes share a namespace with
    ``threading.Thread``'s private ones, and 3.13's ``Thread._handle`` is exactly the name
    a file handle gets, which made every run raise ``TypeError`` at ``start()``.
    """

    def __init__(self, pipe: IO[bytes], sink: BinaryIO, limit: int) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._pipe = pipe
        self._sink = sink
        self._limit = limit
        self._lock = threading.Lock()
        self._stopped = False
        self.observed = 0
        self.kept = 0
        self.last_data = time.monotonic()
        self.error: OSError | None = None

    def start(self) -> None:
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def _run(self) -> None:
        fd = self._pipe.fileno()
        try:
            while data := os.read(fd, 65536):
                with self._lock:
                    if self._stopped:
                        continue
                    self.last_data = time.monotonic()
                    self.observed += len(data)
                    retained = data[: max(0, self._limit - self.kept)]
                    if retained:
                        self._sink.write(retained)
                        self.kept += len(retained)
        except OSError as exc:
            with self._lock:
                if not self._stopped:
                    self.error = exc
        finally:
            with suppress(OSError):
                self._pipe.close()

    def stop(self) -> StreamRecord:
        with self._lock:
            self._stopped = True
            self._sink.close()
            return StreamRecord(
                observed_bytes=self.observed,
                retained_bytes=self.kept,
                truncated=self.observed > self.kept,
            )


def _capture(
    child: subprocess.Popen[bytes], directory: Path, limit: int, seen: _Interrupts
) -> tuple[dict[str, StreamRecord], bool]:
    """Stream both pipes to disk; return the records and whether a pipe was held open.

    Waits on the CHILD, not on pipe EOF: a grandchild that inherited a pipe keeps it open
    after the command exits. Once the command has exited, what is still arriving is
    drained until :data:`_DRAIN_AFTER_EXIT` of silence, or :data:`_DRAIN_MAX` after the
    exit at the latest, and then capture stops.
    """
    readers: dict[str, _Reader] = {}
    try:
        for name, pipe in (("stdout", child.stdout), ("stderr", child.stderr)):
            assert pipe is not None
            readers[name] = _Reader(pipe, _create(directory / f"{name}.bin"), limit)
        for reader in readers.values():
            reader.start()
        with _forward_signals(child, seen):
            while True:
                try:
                    child.wait(timeout=_POLL_WHILE_RUNNING)
                    break
                except subprocess.TimeoutExpired:
                    continue
            exited = time.monotonic()
            while any(reader.is_alive() for reader in readers.values()):
                now = time.monotonic()
                last = max(exited, *(reader.last_data for reader in readers.values()))
                remaining = min(_DRAIN_AFTER_EXIT - (now - last), _DRAIN_MAX - (now - exited))
                if remaining <= 0:
                    break
                for reader in readers.values():
                    reader.join(timeout=min(0.05, remaining))
    except BaseException:
        # A failed storage write must not leave an invisible command running.
        _kill_tree(child)
        for reader in readers.values():
            reader.stop()
        raise
    held_open = any(reader.is_alive() for reader in readers.values())
    records = {name: reader.stop() for name, reader in readers.items()}
    for reader in readers.values():
        if reader.error is not None:
            raise reader.error
    return records, held_open


def _kill_tree(child: subprocess.Popen[bytes]) -> None:
    if _POSIX:
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(child.pid, signal.SIGKILL)
    else:
        with suppress(OSError):
            child.kill()
    child.wait()


def _source_before(
    project_id: str | None, cwd: Path
) -> tuple[Path | None, dict[str, str] | None, str | None]:
    """``(root, per-file map, error)`` before the command. Never stops the command."""
    if project_id is None:
        return None, None, None
    try:
        with store_session() as store:
            project = store.get_project(project_id)
        if project is None:
            raise ValueError("project ID is not registered")
        root = project.root.resolve()
        if not cwd.is_relative_to(root) and orchestrator.team_project(cwd).root.resolve() != root:
            raise ValueError("command working directory is outside the named project's board")
        root = source_root_for(root, cwd)
        return root, source_file_hashes(root), None
    except Exception as exc:  # an observer of the command, never a gate on it
        return None, None, f"Before command: {exc}"[:2048]


def _source_after(root: Path | None) -> tuple[dict[str, str] | None, str | None]:
    if root is None:
        return None, None
    try:
        return source_file_hashes(root), None
    except Exception as exc:  # unknown proof, never a cancelled report
        return None, f"After command: {exc}"[:2048]


def _check_request(argv: Sequence[str], max_output_bytes: int, ids: Sequence[str | None]) -> None:
    if not argv or not argv[0] or any("\x00" in arg for arg in argv):
        raise ValueError("Provide a command and arguments without NUL characters.")
    if not 1 <= max_output_bytes <= MAX_STREAM_LIMIT:
        raise ValueError(f"Output limit must be between 1 and {MAX_STREAM_LIMIT} bytes per stream.")
    # Measured the way report.json writes it (indented), against the same bound.
    if len(json.dumps(list(argv), indent=2).encode()) > _ARGV_LIMIT:
        raise ValueError("Command arguments exceed the report's 32 KiB metadata limit.")
    for value in ids:
        if value is not None and (len(value) > 256 or any(ord(ch) < 32 for ch in value)):
            raise ValueError(
                "Correlation identifiers must be at most 256 characters without controls."
            )


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
    """Run ``argv`` exactly once, with no shell, and save its report.

    A request that fails validation runs nothing. A command that cannot start is a saved
    failure with 127 (not found) or 126. With ``project_id``, the checkout's source
    identity is taken before and after; a failure there leaves the proof unknown and
    the command still runs.
    """
    pipeline_id = os.environ.get(RUN_KEY_ENV_VAR) or None
    _check_request(argv, max_output_bytes, (session_id, task_id, project_id, pipeline_id))
    root_dir = reports_dir()
    _private_directory(root_dir)
    prune_reports(keep=RETAIN_REPORTS - 1, protect=protected_report_ids())
    report_id = uuid.uuid4().hex
    pending = root_dir / f".pending-{report_id}"
    pending.mkdir(mode=0o700)
    try:
        _write_file(pending / "wrapper.pid", f"{os.getpid()}\n".encode("ascii"))
        working_dir = (cwd or Path.cwd()).resolve()
        source_root, files_before, source_error = _source_before(project_id, working_dir)
        seen = _Interrupts()
        started = datetime.now(UTC)
        launch_error = None
        try:
            child = subprocess.Popen(
                list(argv),
                cwd=working_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=_POSIX,
            )
        except OSError as exc:
            launch_error = str(exc)[:4096]
            returncode = 127 if isinstance(exc, FileNotFoundError) else 126
            message = f"Unable to execute {argv[0]}: {exc}\n".encode()
            _write_file(pending / "stdout.bin", b"")
            _write_file(pending / "stderr.bin", message[:max_output_bytes])
            records = {
                "stdout": StreamRecord(observed_bytes=0, retained_bytes=0, truncated=False),
                "stderr": StreamRecord(
                    observed_bytes=len(message),
                    retained_bytes=min(len(message), max_output_bytes),
                    truncated=len(message) > max_output_bytes,
                ),
            }
            held_open = False
        else:
            records, held_open = _capture(child, pending, max_output_bytes, seen)
            returncode = child.returncode
        with _defer_interrupts() as deferred:
            draft = CommandReport(
                id=report_id,
                argv=list(argv),
                cwd=str(working_dir),
                started_at=started,
                finished_at=started,
                returncode=returncode,
                signal=-returncode if returncode < 0 else None,
                launch_error=launch_error,
                interrupted_by=seen.first,
                output_pipes_held_open=held_open,
                session_id=session_id,
                task_id=task_id,
                project_id=project_id,
                pipeline_id=pipeline_id,
                source_root=str(source_root) if source_root is not None else None,
                source_fingerprint_before=(
                    fingerprint_of(files_before) if files_before is not None else None
                ),
                source_capture_error=source_error,
                stdout=records["stdout"],
                stderr=records["stderr"],
                compaction_enabled=False,
                format="raw",
                display_stdout_bytes=0,
                display_stderr_bytes=0,
                per_stream_limit=max_output_bytes,
            )
            report = _publish(pending, draft, source_root, files_before, compact=compact)
    except BaseException:
        shutil.rmtree(pending, ignore_errors=True)
        raise
    if deferred:
        raise InterruptedAfterSave(report, deferred[0])
    return report


def _publish(
    pending: Path,
    draft: CommandReport,
    source_root: Path | None,
    files_before: dict[str, str] | None,
    *,
    compact: bool,
) -> CommandReport:
    """Everything after the command exited: the after-identity, display files, rename."""
    files_after, after_error = _source_after(source_root)
    saved = False
    if files_before is not None:
        payload = SourceFiles(before=files_before, after=files_after).model_dump_json().encode()
        if len(payload) <= _SOURCE_JSON_LIMIT:
            _write_file(pending / "source.json", payload)
            saved = True
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
            "source_fingerprint_after": (
                fingerprint_of(files_after) if files_after is not None else None
            ),
            "source_files_saved": saved,
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
