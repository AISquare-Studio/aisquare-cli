"""Onboarding a project from the fleet UI: a path verdict, `init` + `doctor`, one-click fixes.

The UI hosts many projects and must never ``os.chdir`` (docs/plans/fleet-tui.md
§5.6), so everything that resolves the project from the process cwd runs here as
a subprocess of our own CLI through :func:`aisquare.core.selfcli.run` with
``cwd=<path>``. This module is otherwise pure: every function takes the runner as
a parameter so a test can hand it a scripted one, and NOTHING here raises — a
step that fails is an outcome that says why, because the caller is a background
worker whose only way to report is what it was handed back.

Three questions, three functions:

- :func:`validate_path` — what the user typed, expanded and judged (exists? a
  directory? which root ``init`` would register? already in the store?).
- :func:`onboard` — ``aisquare --json init --no-explainability <path>`` then
  ``aisquare --json doctor``, both with ``cwd=path``, parsed into a
  :class:`SetupReport` and a list of :class:`DoctorCheck`, with every stderr
  line kept for the log (:data:`INIT_FLAGS` says why the flag).
- :func:`fix_commands` / :func:`apply_fix` — which of the doctor's ``fix``
  hints are OUR OWN commands, safe to run at a click, and the click itself.
  Anything else — ``pipx install``, ``npm install -g repomix``, a command with a
  ``<placeholder>`` the user must fill in — stays text, on purpose.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import TypeAdapter, ValidationError

from aisquare.core import selfcli
from aisquare.core.selfcli import CliResult
from aisquare.core.store import store_session
from aisquare.core.workspace import find_project_root, git_common_root, project_id_for
from aisquare.models import CheckStatus, DoctorCheck, ProjectInfo, SetupReport


class Runner(Protocol):
    """The shape of :func:`aisquare.core.selfcli.run` a test can stand in for."""

    def __call__(self, args: Sequence[str], *, cwd: Path | None = None) -> CliResult: ...


Lookup = Callable[[str], "ProjectInfo | None"]
"""``project_id -> the stored project``; the store read :func:`validate_path` performs."""

FixScope = Literal["machine", "project"]

_CHECKS = TypeAdapter(list[DoctorCheck])
_SYMBOL = {CheckStatus.ok: "✓", CheckStatus.warn: "⚠", CheckStatus.fail: "✗"}


# --------------------------------------------------------------------------- the path


@dataclass(frozen=True)
class PathVerdict:
    """What the Onboard view says under the path input, as data."""

    text: str
    """What the user typed, untouched."""
    path: Path | None
    """``text`` with ``~`` and ``$VAR`` expanded and made absolute; None when empty."""
    exists: bool
    is_dir: bool
    root: Path | None
    """The root ``init`` would register — ``find_project_root`` of ``path``."""
    registered: ProjectInfo | None
    """The stored project at that root, when there is one."""
    is_git: bool
    store_error: str | None = None
    """Why ``registered`` could not be read. Failing open here costs exactly one
    thing — the 'already registered' notice — and that is what this records."""

    @property
    def ok(self) -> bool:
        """Whether Onboard may run: a directory, whatever else it is or is not."""
        return self.path is not None and self.is_dir

    @property
    def project_id(self) -> str | None:
        """The id ``init`` will assign — stable per resolved root."""
        return project_id_for(self.root) if self.root is not None else None

    def describe(self) -> str:
        """The one-line verdict, for the Static under the input."""
        if self.path is None:
            return "type a path, or pick a directory in the tree"
        if not self.exists:
            return f"{self.path} does not exist"
        if not self.is_dir:
            return f"{self.path} is a file, not a directory"
        root = self.root if self.root is not None else self.path
        if root == self.path.resolve():
            where = f"will register {root}"
        else:
            where = f"will register {root} — the repository containing {self.path}"
        kind = "git repository" if self.is_git else "not a git repository, which is fine"
        line = f"{where} ({kind})"
        if self.registered is not None:
            line += f" · already registered as {self.registered.id}; init is idempotent"
        elif self.store_error is not None:
            line += (
                " · could not read the store, so 'already registered' is unknown: "
                f"{self.store_error}"
            )
        return line


def expand_path(text: str) -> Path | None:
    """``~`` and ``$VAR`` expanded, made absolute against the process cwd; None for blank.

    Symlinks are NOT resolved here: the user typed ``~/proj`` and should read
    ``/home/me/proj`` back, not wherever it points. ``find_project_root`` resolves
    before hashing, so the id is stable either way.
    """
    stripped = text.strip()
    if not stripped:
        return None
    return Path(os.path.expandvars(stripped)).expanduser().absolute()


def _lookup_in_store(project_id: str) -> ProjectInfo | None:
    with store_session() as store:
        return store.get_project(project_id)


def validate_path(text: str, *, lookup: Lookup | None = None) -> PathVerdict:
    """Judge a typed path. Never raises; a store that will not open is reported, not thrown."""
    path = expand_path(text)
    if path is None:
        return PathVerdict(
            text=text,
            path=None,
            exists=False,
            is_dir=False,
            root=None,
            registered=None,
            is_git=False,
        )
    exists = path.exists()
    is_dir = path.is_dir()
    if not is_dir:
        return PathVerdict(
            text=text,
            path=path,
            exists=exists,
            is_dir=False,
            root=None,
            registered=None,
            is_git=False,
        )
    root = find_project_root(path)
    is_git = (root / ".git").exists() or git_common_root(path) is not None
    registered: ProjectInfo | None = None
    store_error: str | None = None
    try:
        registered = (lookup or _lookup_in_store)(project_id_for(root))
    except Exception as exc:  # the store is the one thing here that can be wedged
        store_error = str(exc) or type(exc).__name__
    return PathVerdict(
        text=text,
        path=path,
        exists=True,
        is_dir=True,
        root=root,
        registered=registered,
        is_git=is_git,
        store_error=store_error,
    )


# --------------------------------------------------------------------------- init + doctor


#: What ``init`` is run with, beyond ``--json`` and the path. ``--no-explainability``
#: is what keeps the plan's promise that onboarding never prompts (§4.2): ``init``
#: asks the shipping question whenever ``sys.stdin.isatty()`` and the SDK extra is
#: installed, and ``selfcli.run`` captures stdout and stderr but leaves stdin as it
#: found it — which, under the TUI, is the terminal. A fake runner cannot see this;
#: it was found by reading ``cli/root.py::_explainability_decision``. Declining is
#: the one answer that changes nothing (``lifecycle._explainability_step``: "#50's
#: first acceptance clause is that declining leaves ZERO behavioural change"), so a
#: machine that already ships keeps shipping, and the consent stays where §4.2 puts
#: it — the ``explainability enable`` button.
INIT_FLAGS: tuple[str, ...] = ("--no-explainability",)


@dataclass(frozen=True)
class OnboardOutcome:
    """What running ``init`` then ``doctor`` in ``path`` produced.

    ``ok`` means the project is registered: ``init`` ran and returned a report.
    The doctor can still have failed to answer — ``doctor_error`` says so and
    ``checks`` is empty — and the project is onboarded regardless, because it
    is. ``reason`` is set only when ``ok`` is False.
    """

    path: Path
    project_id: str | None = None
    report: SetupReport | None = None
    checks: tuple[DoctorCheck, ...] = ()
    doctor_error: str | None = None
    reason: str | None = None
    log: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.project_id is not None


def _invoke(
    run: Runner, args: Sequence[str], cwd: Path | None
) -> tuple[CliResult | None, str | None]:
    """Run one command; a failure to ASK (no interpreter, a timeout) is a reason, not a raise."""
    try:
        return run(args, cwd=cwd), None
    except Exception as exc:
        return None, f"could not run aisquare {' '.join(args)}: {exc or type(exc).__name__}"


def _json_object(stdout: str) -> dict[str, object] | None:
    """The last JSON object on stdout, if that is what stdout is."""
    try:
        payload = json.loads(stdout.strip() or "null")
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


#: Every character Rich draws a box out of, plus the space that pads a row and the
#: ``+-|`` it substitutes for all of them when the child's stderr encoding is not
#: UTF-8 (``rich.box.Box.substitute``) — a C locale renders the SAME traceback in
#: ASCII, and a border we do not recognise is a border that reaches the user.
_BOX_DRAWING = frozenset("─━│┃╭╮╰╯┌┐└┘├┤┬┴┼═║╔╗╚╝╠╣╦╩╬+-| ")

#: CSI (``\x1b[…m``), OSC and the two-character escapes, so a styled line can be
#: read as the text it is. See :func:`_plain` for why a child on a PIPE styles.
_ANSI = re.compile(r"\x1b\[[0-9;:?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def _plain(text: str) -> str:
    r"""``text`` with the terminal control sequences a child may have written removed.

    Our children write to a pipe, so by rights nothing they say is styled — but
    ``typer.rich_utils`` forces a terminal whenever ``GITHUB_ACTIONS``,
    ``FORCE_COLOR`` or ``PY_COLORS`` is set in the environment we hand them, and
    on GitHub Actions the first of those is always set. The traceback then
    arrives as ``\x1b[1mFileExistsError: \x1b[0m…``: the same words wrapped in
    SGR sequences, invisible in a log and fatal to anything that reads the first
    character of a line. ``NO_COLOR`` is no defence — it drops the colours and
    keeps the bold. So stderr is un-styled ONCE, here, before it is read or
    shown, and every environment reduces to the same text.
    """
    return _ANSI.sub("", text)


def _stderr_lines(result: CliResult) -> list[str]:
    """stderr as lines worth showing: styling, blank lines and Rich's box borders dropped."""
    lines: list[str] = []
    for raw in _plain(result.stderr).splitlines():
        line = raw.strip().strip("│┃║|").strip()
        if line and not set(line) <= _BOX_DRAWING:
            lines.append(line)
    return lines


def _is_border(line: str) -> bool:
    """Whether ``line`` is one of Rich's box rules — nothing but border and padding."""
    stripped = line.strip()
    return bool(stripped) and set(stripped) <= _BOX_DRAWING


def _unwrap(lines: Sequence[str], width: int) -> str:
    """Rich's soft wrap undone: the paragraph it broke over ``width`` columns, whole.

    Rich leaves the space it broke at on the line it broke (``Text.rstrip_end``
    removes only the whitespace that would reach BEYOND the width), so the
    pieces concatenate straight back into the original. Two cases have no space
    to carry, and the line lengths tell them apart: a row that fills the width
    and holds no space at all is a chunk of a single word too long to fit —
    those halves belong together with nothing between them, which is how a path
    survives a narrow terminal intact; any other row that does not already end
    in a space was broken between two words and gets exactly one back.
    """
    out: list[str] = []
    for index, line in enumerate(lines):
        if index:
            previous = lines[index - 1]
            folded = len(previous) >= width and " " not in previous
            if not (previous.endswith(" ") or folded):
                out.append(" ")
        out.append(line)
    return "".join(out).strip()


def _stderr_verdict(result: CliResult) -> str | None:
    """The one line of stderr that says what went wrong.

    A Rich traceback is a BOX of frames followed by the ``ExcType: message`` the
    exception actually is, soft-wrapped to the console width — 80 whenever the
    child is on a pipe, which is always. So the answer is neither the last line
    (measured: the tail of a path) nor the last line that LOOKS like an
    exception (a guess about how the type is spelt, which a styled line or an
    ``Abort`` defeats): it is everything after the last border the box drew,
    with the wrap undone. The LAST border, so a chained traceback reports the
    exception that actually escaped rather than the one it was raised from.

    Anything that is not a traceback — a usage error, one ``✗`` line — has no
    box, and is read by its last line as before.
    """
    lines = _plain(result.stderr).splitlines()
    borders = [index for index, line in enumerate(lines) if _is_border(line)]
    if borders:
        closing = borders[-1]
        summary = [line for line in lines[closing + 1 :] if line.strip()]
        if summary:
            return _unwrap(summary, len(lines[closing].strip()))
    shown = _stderr_lines(result)
    return shown[-1] if shown else None


def failure_reason(result: CliResult, step: str) -> str:
    """Why a non-zero step failed, in the words the CLI itself used.

    Under ``--json`` a failing command puts an object on stdout — ``{"error",
    "hint", "detail"}`` from ``cli.common.fail``, ``{"error": "usage",
    "message"}`` from the usage handler; that is the first choice. Otherwise
    stderr, read by :func:`_stderr_verdict`.
    """
    payload = _json_object(result.stdout)
    if payload is not None and isinstance(payload.get("error"), str):
        parts = [str(payload["error"])]
        for key in ("message", "detail", "hint"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                parts.append(value)
        return f"{step} failed: {' — '.join(parts)}"
    verdict = _stderr_verdict(result)
    if verdict is not None:
        return f"{step} failed (exit {result.returncode}): {verdict}"
    return f"{step} failed with exit {result.returncode} and said nothing"


def parse_checks(stdout: str) -> tuple[list[DoctorCheck], str | None]:
    """``aisquare --json doctor`` output → (checks, why-there-are-none).

    The whole of stdout first, then its last line: ``doctor --fix`` echoes one
    ``fix: …`` line per repair before the report, and the report is still there.
    """
    text = stdout.strip()
    candidates = [text] if text else []
    if "\n" in text:
        candidates.append(text.rsplit("\n", 1)[-1].strip())
    failure: str | None = None
    for candidate in candidates:
        try:
            return _CHECKS.validate_json(candidate), None
        except (ValidationError, ValueError) as exc:
            failure = exc.__class__.__name__
    head = text.splitlines()[0][:80] if text else "(nothing)"
    return (
        [],
        f"doctor printed something that is not a list of checks: {head!r} ({failure or 'empty'})",
    )


def run_doctor(
    cwd: Path | None, *, run: Runner = selfcli.run, say: Callable[[str], None] | None = None
) -> tuple[list[DoctorCheck], str | None]:
    """``aisquare --json doctor`` in ``cwd``: (checks, error).

    ``doctor`` exits 1 whenever a check FAILS and still prints the full report,
    so the exit code is not what decides here — the report is. Only a report
    that cannot be read is an error.
    """
    args = ["--json", "doctor"]
    if say is not None:
        say(f"$ aisquare {' '.join(args)}")
    result, asked = _invoke(run, args, cwd)
    if result is None:
        return [], asked
    if say is not None:
        for line in _stderr_lines(result):
            say(f"  {line}")
    checks, error = parse_checks(result.stdout)
    if error is not None and result.returncode != 0 and not result.stdout.strip():
        error = failure_reason(result, "doctor")
    return checks, error


def summary_line(checks: Iterable[DoctorCheck]) -> str:
    """``✓ 11 · ⚠ 2 · ✗ 0``."""
    listed = list(checks)
    counts = {status: sum(1 for c in listed if c.status is status) for status in CheckStatus}
    return " · ".join(f"{_SYMBOL[status]} {counts[status]}" for status in CheckStatus)


def onboard(
    path: Path,
    *,
    run: Runner = selfcli.run,
    on_line: Callable[[str], None] | None = None,
) -> OnboardOutcome:
    """``init --no-explainability <path>`` then ``doctor``, both with ``cwd=path``; never raises.

    ``on_line`` receives the log as it happens — the commands run, every stderr
    line each one wrote, and the result of each step — so a worker can stream
    it; the same lines come back in ``outcome.log``. See :data:`INIT_FLAGS` for
    why ``init`` is told the answer to the one question it could ask.
    """
    lines: list[str] = []

    def say(line: str) -> None:
        lines.append(line)
        if on_line is not None:
            on_line(line)

    def failed(reason: str) -> OnboardOutcome:
        say(f"✗ {reason}")
        return OnboardOutcome(path=path, reason=reason, log=tuple(lines))

    init_args = ["--json", "init", *INIT_FLAGS, str(path)]
    say(f"$ aisquare {' '.join(init_args)}")
    result, asked = _invoke(run, init_args, path)
    if result is None:
        return failed(asked or "init could not be run")
    for line in _stderr_lines(result):
        say(f"  {line}")
    if not result.ok:
        return failed(failure_reason(result, "init"))
    try:
        report = SetupReport.model_validate_json(result.stdout)
    except (ValidationError, ValueError) as exc:
        return failed(
            f"init exited 0 but printed no setup report ({exc.__class__.__name__}); "
            "nothing was registered that we can name"
        )
    project = report.project
    verb = "already initialized" if report.already_initialized else "initialized"
    say(f"✓ {verb} — project {project.id} at {project.root}")
    for note in report.notes:
        say(f"  note: {note}")

    checks, doctor_error = run_doctor(path, run=run, say=say)
    if doctor_error is not None:
        say(f"⚠ {doctor_error}")
    else:
        say(f"✓ doctor: {summary_line(checks)}")
        for check in checks:
            if check.status is CheckStatus.ok:
                continue
            say(f"  {_SYMBOL[check.status]} {check.name}: {check.detail}")
            if check.fix:
                say(f"      → {check.fix}")
    return OnboardOutcome(
        path=path,
        project_id=project.id,
        report=report,
        checks=tuple(checks),
        doctor_error=doctor_error,
        log=tuple(lines),
    )


# --------------------------------------------------------------------------- fixes


@dataclass(frozen=True)
class _KnownFix:
    mention: str
    """How the doctor writes it, after the word ``aisquare``."""
    argv: tuple[str, ...]
    """What we actually run — the non-interactive form of the same command."""
    scope: FixScope
    flags: frozenset[str] = frozenset()
    """Bare flags the hint may carry that ``argv`` already covers."""
    valued: frozenset[str] = frozenset()
    """Flags the hint may carry WITH one value we pass through verbatim."""


#: The doctor hints that become buttons. ``init`` is deliberately absent — from
#: the UI that is the Onboard flow, not a fix — and so is ``team distill``,
#: which starts a model process. Extending this table is the whole review
#: surface for "what can one click run".
KNOWN_FIXES: tuple[_KnownFix, ...] = (
    _KnownFix(
        "agents connect claude-code",
        ("agents", "connect", "claude-code"),
        "machine",
        valued=frozenset({"--config-dir"}),
    ),
    _KnownFix(
        "project onboard", ("project", "onboard", "--refresh"), "project", frozenset({"--refresh"})
    ),
    _KnownFix("doctor --fix", ("doctor", "--fix", "--yes"), "machine", frozenset({"--yes", "-y"})),
    _KnownFix("explainability enable", ("explainability", "enable"), "machine"),
)

_MENTION = re.compile(
    r"aisquare (?P<mention>"
    + "|".join(re.escape(known.mention) for known in KNOWN_FIXES)
    + r")(?P<rest>[^;)`\n]*)"
)
_BY_MENTION = {known.mention: known for known in KNOWN_FIXES}


@dataclass(frozen=True)
class FixCommand:
    """One doctor hint that is our own command, ready to run."""

    check: str
    """The check whose ``fix`` this came from."""
    argv: tuple[str, ...]
    """Arguments after ``aisquare``; ``apply_fix`` prepends ``--json``."""
    scope: FixScope
    """``project`` fixes need the project's cwd; ``machine`` fixes do not care."""
    source: str
    """The hint text, verbatim, for a tooltip."""

    @property
    def label(self) -> str:
        return "aisquare " + " ".join(self.argv)


def _argv_for(known: _KnownFix, rest: str) -> tuple[str, ...] | None:
    """The argv for one mention, or None when the hint carries something we will not run."""
    tokens = rest.strip().rstrip(".,:").split()
    argv = list(known.argv)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in known.flags:
            index += 1
        elif token in known.valued and index + 1 < len(tokens):
            argv += [token, tokens[index + 1]]
            index += 2
        else:
            return None
    return tuple(argv)


def fix_commands(checks: Iterable[DoctorCheck]) -> list[FixCommand]:
    """The buttons: one per distinct runnable command among the non-ok checks' hints.

    A hint becomes a button only when it names one of :data:`KNOWN_FIXES` and
    carries nothing beyond the flags that entry admits. ``aisquare explainability
    enable --proxy-url <url>`` therefore stays text: the user has to supply the
    URL, and a button cannot. Several checks pointing at the same command make
    ONE button.
    """
    fixes: list[FixCommand] = []
    seen: set[tuple[str, ...]] = set()
    for check in checks:
        if check.status is CheckStatus.ok or not check.fix:
            continue
        for match in _MENTION.finditer(check.fix):
            known = _BY_MENTION[match.group("mention")]
            argv = _argv_for(known, match.group("rest"))
            if argv is None or argv in seen:
                continue
            seen.add(argv)
            fixes.append(
                FixCommand(check=check.name, argv=argv, scope=known.scope, source=check.fix)
            )
    return fixes


@dataclass(frozen=True)
class FixResult:
    """What one click did."""

    fix: FixCommand
    returncode: int | None
    reason: str | None = None
    """Why it did not work; None when it did."""

    @property
    def ok(self) -> bool:
        return self.reason is None

    def summary(self) -> str:
        if self.ok:
            return f"✓ {self.fix.label}"
        return f"✗ {self.fix.label}: {self.reason}"


def apply_fix(fix: FixCommand, cwd: Path | None, *, run: Runner = selfcli.run) -> FixResult:
    """Run one fix as ``aisquare --json <argv>`` in ``cwd``; never raises.

    ``doctor --fix --yes`` exits 1 when a check still fails AFTER repairing,
    which is a report, not a failed fix — so for that one command a non-zero
    exit with a readable check list on stdout counts as done, and the re-run
    the view performs shows what is still wrong.
    """
    result, asked = _invoke(run, ["--json", *fix.argv], cwd)
    if result is None:
        return FixResult(fix=fix, returncode=None, reason=asked)
    if result.ok:
        return FixResult(fix=fix, returncode=0)
    if fix.argv[:2] == ("doctor", "--fix") and parse_checks(result.stdout)[1] is None:
        return FixResult(fix=fix, returncode=result.returncode)
    return FixResult(
        fix=fix, returncode=result.returncode, reason=failure_reason(result, fix.label)
    )
