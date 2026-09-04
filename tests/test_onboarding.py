"""The onboarding service: path verdicts, `init` + `doctor` through a scripted runner, fixes.

Every subprocess here is a FAKE — a callable with ``selfcli.run``'s shape that
records what it was asked (argv and cwd) and answers from a script — so the
tests pin what the UI will ask our CLI to do, not what the CLI then does; the
CLI's own suite covers that. Each claim has a positive and a negative control
(CONTRIBUTING, "Writing a guard that still guards"): the parser that reads a
report must also refuse garbage, the mapper that makes a button must also leave
a hint as text.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import pty
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from aisquare.core import paths, selfcli
from aisquare.core.paths import HOME_ENV_VAR
from aisquare.core.selfcli import CliResult
from aisquare.core.store import store_session
from aisquare.core.workspace import project_id_for
from aisquare.models import CheckStatus, DoctorCheck, ProjectInfo, SetupReport
from aisquare.services import onboarding
from aisquare.services.onboarding import (
    FixCommand,
    apply_fix,
    failure_reason,
    fix_commands,
    onboard,
    parse_checks,
    run_doctor,
    summary_line,
    validate_path,
)

# --------------------------------------------------------------------------- fakes


@dataclass
class Scripted:
    """A ``selfcli.run`` stand-in: answers by the first non-``--json`` word, records every ask."""

    answers: dict[str, CliResult]
    calls: list[tuple[list[str], Path | None]] = field(default_factory=list)

    def __call__(self, args: Sequence[str], *, cwd: Path | None = None) -> CliResult:
        argv = list(args)
        self.calls.append((argv, cwd))
        words = [word for word in argv if word != "--json"]
        try:
            answer = self.answers[words[0]]
        except KeyError:
            raise AssertionError(f"unscripted command: {argv}") from None
        return CliResult(
            argv=argv, returncode=answer.returncode, stdout=answer.stdout, stderr=answer.stderr
        )


def _result(stdout: str = "", *, code: int = 0, stderr: str = "") -> CliResult:
    return CliResult(argv=[], returncode=code, stdout=stdout, stderr=stderr)


def _report(root: Path, *, notes: Sequence[str] = ()) -> str:
    project = ProjectInfo(id=project_id_for(root), root=root)
    report = SetupReport(
        home=root / ".home", already_initialized=False, project=project, notes=list(notes)
    )
    return report.model_dump_json()


def _checks_json(checks: Sequence[DoctorCheck]) -> str:
    return json.dumps([check.model_dump(mode="json") for check in checks])


OK = DoctorCheck(name="python", status=CheckStatus.ok, detail="3.12")
WARN_CONNECT = DoctorCheck(
    name="claude-code",
    status=CheckStatus.warn,
    detail="hooks are missing",
    fix="(Re)connect it: aisquare agents connect claude-code",
)
WARN_SNAPSHOT = DoctorCheck(
    name="snapshot",
    status=CheckStatus.warn,
    detail="no codebase snapshot for the active project",
    fix="Pack one: aisquare project onboard",
)
WARN_REPOMIX = DoctorCheck(
    name="repomix",
    status=CheckStatus.warn,
    detail="repomix not found",
    fix="Install Node.js, then: npm install -g repomix",
)
FAIL_HOME = DoctorCheck(
    name="home", status=CheckStatus.fail, detail="/x is missing", fix="Set it up: aisquare init"
)


def _raising(args: Sequence[str], *, cwd: Path | None = None) -> CliResult:
    raise OSError("no such interpreter")


# --------------------------------------------------------------------------- onboard()


def test_onboard_runs_init_then_doctor_in_the_path(tmp_path: Path) -> None:
    """Positive: both commands, both with cwd=path, the id read off init's report."""
    run = Scripted(
        {
            "init": _result(_report(tmp_path, notes=["connected claude-code"])),
            "doctor": _result(_checks_json([OK, WARN_CONNECT]), code=0),
        }
    )
    outcome = onboard(tmp_path, run=run)

    assert outcome.ok and outcome.reason is None
    assert outcome.project_id == project_id_for(tmp_path)
    assert outcome.report is not None and outcome.report.project.root == tmp_path
    assert [check.name for check in outcome.checks] == ["python", "claude-code"]
    assert outcome.doctor_error is None
    # `--no-explainability`: init asks its one question whenever stdin is a tty,
    # and under the TUI it is — see `onboarding.INIT_FLAGS`. The flag's existence
    # is proven against the real CLI further down; here, that it is asked for.
    assert run.calls == [
        (["--json", "init", "--no-explainability", str(tmp_path)], tmp_path),
        (["--json", "doctor"], tmp_path),
    ]
    # The log tells the story in order: the command, its result, the doctor, the warning + fix.
    log = "\n".join(outcome.log)
    assert (
        log.index("$ aisquare --json init")
        < log.index("✓ initialized")
        < log.index("$ aisquare --json doctor")
    )
    assert "note: connected claude-code" in log
    assert "⚠ claude-code: hooks are missing" in log and "→ (Re)connect it" in log


def test_onboard_streams_every_line_as_it_happens(tmp_path: Path) -> None:
    run = Scripted(
        {
            "init": _result(_report(tmp_path), stderr="warning: slow disk\n"),
            "doctor": _result(_checks_json([OK])),
        }
    )
    seen: list[str] = []
    outcome = onboard(tmp_path, run=run, on_line=seen.append)
    assert seen == list(outcome.log)
    assert "  warning: slow disk" in seen, "a subprocess's stderr is part of the log"


def test_onboard_init_exit_1_is_a_failed_outcome_with_the_cli_reason(tmp_path: Path) -> None:
    """Negative: init failing (as `fail()` reports it under --json) stops everything."""
    run = Scripted(
        {
            "init": _result(
                json.dumps(
                    {
                        "error": "reinit_would_discard_explainability",
                        "hint": "aisquare init --reinit --yes",
                    }
                ),
                code=1,
                stderr="",
            )
        }
    )
    outcome = onboard(tmp_path, run=run)

    assert not outcome.ok and outcome.project_id is None and outcome.checks == ()
    assert outcome.reason is not None
    assert "init failed" in outcome.reason
    assert "reinit_would_discard_explainability" in outcome.reason
    assert "aisquare init --reinit --yes" in outcome.reason, "the hint is the actionable part"
    assert len(run.calls) == 1, "doctor must not run in a project that was never registered"
    assert outcome.log[-1].startswith("✗ ")


def test_onboard_init_exit_1_without_json_uses_stderr(tmp_path: Path) -> None:
    run = Scripted(
        {
            "init": _result(
                "", code=2, stderr="Usage: aisquare init [OPTIONS]\nError: no such option\n"
            )
        }
    )
    outcome = onboard(tmp_path, run=run)
    assert not outcome.ok
    assert outcome.reason == "init failed (exit 2): Error: no such option"


def test_onboard_init_exit_0_without_a_report_is_still_a_failure(tmp_path: Path) -> None:
    """A green exit with nothing readable names no project; the outcome must say so."""
    run = Scripted({"init": _result("✓ aisquare initialized at ~/.aisquare\n")})
    outcome = onboard(tmp_path, run=run)
    assert not outcome.ok
    assert outcome.reason is not None and "printed no setup report" in outcome.reason
    assert len(run.calls) == 1


def test_onboard_doctor_garbage_is_reported_and_the_project_still_counts(tmp_path: Path) -> None:
    """The project IS registered once init returned; a mute doctor is a fact about the doctor."""
    run = Scripted(
        {
            "init": _result(_report(tmp_path)),
            "doctor": _result("Traceback (most recent call last)\n  boom\n", code=1),
        }
    )
    outcome = onboard(tmp_path, run=run)
    assert outcome.ok and outcome.project_id == project_id_for(tmp_path)
    assert outcome.checks == ()
    assert outcome.doctor_error is not None
    assert "not a list of checks" in outcome.doctor_error
    assert any(line.startswith("⚠ doctor printed") for line in outcome.log)


def test_onboard_doctor_exit_1_still_yields_the_report(tmp_path: Path) -> None:
    """`doctor` exits 1 when a check FAILS and prints the report anyway; the report wins."""
    run = Scripted(
        {
            "init": _result(_report(tmp_path)),
            "doctor": _result(_checks_json([OK, FAIL_HOME]), code=1),
        }
    )
    outcome = onboard(tmp_path, run=run)
    assert outcome.ok and outcome.doctor_error is None
    assert [check.status for check in outcome.checks] == [CheckStatus.ok, CheckStatus.fail]
    assert "✓ doctor: ✓ 1 · ⚠ 0 · ✗ 1" in outcome.log


def test_onboard_never_raises_when_the_runner_does(tmp_path: Path) -> None:
    outcome = onboard(tmp_path, run=_raising)
    assert not outcome.ok
    assert outcome.reason is not None
    assert outcome.reason.startswith("could not run aisquare --json init")
    assert "no such interpreter" in outcome.reason


# --------------------------------------------------------------------------- the parsers


def test_parse_checks_reads_a_report_and_refuses_garbage() -> None:
    checks, error = parse_checks(_checks_json([OK, WARN_CONNECT]))
    assert error is None and [c.name for c in checks] == ["python", "claude-code"]
    for garbage in ("", "not json", '{"error": "x"}', '[{"name": "x"}]'):
        checks, error = parse_checks(garbage)
        assert checks == [] and error is not None, garbage


def test_parse_checks_finds_the_report_after_fix_echo_lines() -> None:
    """`doctor --fix` echoes `fix: …` before the JSON; the report is still the last line."""
    checks, error = parse_checks("fix: enabled tracing\nfix: wrote target\n" + _checks_json([OK]))
    assert error is None and [c.name for c in checks] == ["python"]


def test_run_doctor_reports_a_silent_non_zero_exit_by_its_stderr(tmp_path: Path) -> None:
    run = Scripted({"doctor": _result("", code=1, stderr="✗ context.db is unreadable\n")})
    checks, error = run_doctor(tmp_path, run=run)
    assert checks == [] and error == "doctor failed (exit 1): ✗ context.db is unreadable"
    assert run.calls == [(["--json", "doctor"], tmp_path)]


def test_failure_reason_prefers_the_json_error_then_stderr_then_the_exit_code() -> None:
    assert failure_reason(_result('{"error":"nope","detail":"disk"}', code=1), "init") == (
        "init failed: nope — disk"
    )
    assert failure_reason(_result("", code=3, stderr="a\nlast line\n"), "init") == (
        "init failed (exit 3): last line"
    )
    assert failure_reason(_result("", code=4), "init") == "init failed with exit 4 and said nothing"


def test_failure_reason_reads_the_usage_handler_message() -> None:
    """Measured: ``--json init --bogus`` prints ``{"error": "usage", "message": …}``."""
    usage = _result('{"error": "usage", "message": "No such option: --bogus"}', code=2)
    assert failure_reason(usage, "init") == "init failed: usage — No such option: --bogus"


#: What a real `init` failure looked like on stderr through a pipe (AISQUARE_HOME
#: pointing at a file), abridged: Rich boxes the frames and WRAPS the final line at
#: 80 columns, so the last line is the tail of a path.
_WRAPPED_TRACEBACK = (
    "╭───────────────────── Traceback (most recent call last) ──────────────────────╮\n"
    "│ /x/src/aisquare/cli/root.py:72 in init                                       │\n"
    "│ ❱  72 │   │   │   report = lifecycle_service.initialize(                     │\n"
    "╰──────────────────────────────────────────────────────────────────────────────╯\n"
    "FileExistsError: [Errno 17] File exists: \n"
    "'/tmp/claude-1000/-home-anmol-Code-AISquare-ws2-aisquare-cli/b443836d-4548-4a8f-\n"
    "911b-96c0b440fafb/scratchpad/live2/not-a-dir'\n"
)


def test_failure_reason_reads_the_exception_out_of_a_wrapped_traceback() -> None:
    """The reason is the exception and its message, not the box and not a path tail."""
    reason = failure_reason(_result("", code=1, stderr=_WRAPPED_TRACEBACK), "init")
    assert reason.startswith("init failed (exit 1): FileExistsError: [Errno 17] File exists:")
    assert reason.endswith("scratchpad/live2/not-a-dir'")
    assert "╰" not in reason and "❱" not in reason and "root.py" not in reason
    # Negative control: stderr with no exception line is still read by its last line,
    # and a frame line that merely CONTAINS the word Error is not mistaken for one.
    plain = failure_reason(_result("", code=2, stderr="Usage: x\nno such option\n"), "init")
    assert plain == "init failed (exit 2): no such option"
    framed = failure_reason(
        _result("", code=1, stderr="│ except FileNotFoundError: │\nthe actual last line\n"),
        "init",
    )
    assert framed == "init failed (exit 1): the actual last line"


def test_summary_line_counts_each_status() -> None:
    assert summary_line([OK, WARN_CONNECT, WARN_SNAPSHOT, FAIL_HOME]) == "✓ 1 · ⚠ 2 · ✗ 1"
    assert summary_line([]) == "✓ 0 · ⚠ 0 · ✗ 0"


# --------------------------------------------------------------------------- validate_path()


def _no_store(project_id: str) -> ProjectInfo | None:
    return None


def test_validate_path_accepts_a_plain_directory_that_is_not_a_repo(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    verdict = validate_path(str(plain), lookup=_no_store)
    assert verdict.ok and verdict.exists and verdict.is_dir
    assert verdict.path == plain and verdict.root == plain.resolve()
    assert not verdict.is_git
    assert verdict.registered is None and verdict.store_error is None
    assert verdict.project_id == project_id_for(plain.resolve())
    line = verdict.describe()
    assert line.startswith(f"will register {plain.resolve()}") and "not a git repository" in line


def test_validate_path_rejects_a_missing_path_and_a_file(tmp_path: Path) -> None:
    missing = validate_path(str(tmp_path / "nope"), lookup=_no_store)
    assert not missing.ok and not missing.exists and missing.root is None
    assert missing.describe().endswith("does not exist")

    a_file = tmp_path / "notes.txt"
    a_file.write_text("x", encoding="utf-8")
    verdict = validate_path(str(a_file), lookup=_no_store)
    assert not verdict.ok and verdict.exists and not verdict.is_dir and verdict.root is None
    assert verdict.describe().endswith("is a file, not a directory")


def test_validate_path_names_the_root_a_subdirectory_would_register(tmp_path: Path) -> None:
    """A marker above the typed path is the root `init` registers — the verdict says which."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    src = repo / "src"
    src.mkdir()
    verdict = validate_path(str(src), lookup=_no_store)
    assert verdict.ok and verdict.root == repo.resolve() and verdict.is_git
    assert verdict.describe().startswith(
        f"will register {repo.resolve()} — the repository containing {src}"
    )


def test_validate_path_reports_an_already_registered_root(tmp_path: Path) -> None:
    """Positive through the REAL store, and the negative control beside it."""
    known = tmp_path / "known"
    known.mkdir()
    unknown = tmp_path / "unknown"
    unknown.mkdir()
    with store_session() as store:
        store.ensure_project(ProjectInfo(id=project_id_for(known.resolve()), root=known.resolve()))

    registered = validate_path(str(known))
    assert registered.ok and registered.registered is not None
    assert registered.registered.id == project_id_for(known.resolve())
    assert "already registered as" in registered.describe()

    fresh = validate_path(str(unknown))
    assert fresh.ok and fresh.registered is None and fresh.store_error is None
    assert "already registered" not in fresh.describe()


def test_validate_path_fails_open_when_the_store_will_not_answer(tmp_path: Path) -> None:
    """A wedged store costs the 'already registered' notice and nothing else — and says so."""
    plain = tmp_path / "plain"
    plain.mkdir()

    def wedged(project_id: str) -> ProjectInfo | None:
        raise RuntimeError("file is not a database")

    verdict = validate_path(str(plain), lookup=wedged)
    assert verdict.ok, "the store is not what decides whether a directory can be onboarded"
    assert verdict.registered is None
    assert verdict.store_error == "file is not a database"
    assert (
        "could not read the store" in verdict.describe()
        and "file is not a database" in verdict.describe()
    )


def test_validate_path_expands_tilde_and_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ASQ_TEST_ROOT", str(tmp_path))
    (tmp_path / "proj").mkdir()
    for typed in ("~/proj", "$ASQ_TEST_ROOT/proj", f"  {tmp_path}/proj  "):
        verdict = validate_path(typed, lookup=_no_store)
        assert verdict.path == tmp_path / "proj", typed
        assert verdict.ok, typed
        assert verdict.text == typed, "what the user typed is kept verbatim"


def test_validate_path_blank_asks_for_a_path() -> None:
    for blank in ("", "   "):
        verdict = validate_path(blank, lookup=_no_store)
        assert not verdict.ok and verdict.path is None and verdict.project_id is None
        assert verdict.describe() == "type a path, or pick a directory in the tree"


# --------------------------------------------------------------------------- fix_commands()


def _fix(name: str, fix: str, status: CheckStatus = CheckStatus.warn) -> DoctorCheck:
    return DoctorCheck(name=name, status=status, detail="d", fix=fix)


def test_fix_commands_maps_each_known_hint_to_its_non_interactive_argv() -> None:
    checks = [
        WARN_CONNECT,
        WARN_SNAPSHOT,
        _fix("explainability", "Turn it on: aisquare explainability enable"),
        _fix(
            "explainability-config",
            "tracing is off (turn it on with: aisquare explainability enable)",
        ),
        _fix("sdk", "Repair what can be repaired: aisquare doctor --fix"),
    ]
    fixes = fix_commands(checks)
    assert [(fix.check, fix.argv, fix.scope) for fix in fixes] == [
        ("claude-code", ("agents", "connect", "claude-code"), "machine"),
        ("snapshot", ("project", "onboard", "--refresh"), "project"),
        ("explainability", ("explainability", "enable"), "machine"),
        ("sdk", ("doctor", "--fix", "--yes"), "machine"),
    ]
    assert fixes[0].label == "aisquare agents connect claude-code"
    assert fixes[0].source == WARN_CONNECT.fix
    assert fixes[1].label == "aisquare project onboard --refresh"


def test_fix_commands_leaves_everything_that_is_not_our_command_as_text() -> None:
    """Negative: mentions of `aisquare` that are NOT a runnable fix must make no button."""
    not_buttons = [
        WARN_REPOMIX,  # npm, not us
        _fix(
            "install", "Install as a global tool: pipx install aisquare"
        ),  # our NAME, not our command
        FAIL_HOME,  # `aisquare init` from the UI is the Onboard flow, not a fix
        _fix("config", "Fix or reset: aisquare init --reinit"),
        _fix(
            "brain", "It initialises on the first distill: aisquare team distill"
        ),  # a model process
        _fix(
            "proxy", "aisquare explainability enable --proxy-url <url>"
        ),  # a value we cannot supply
        _fix(
            "target",
            "Point it at a deployment: aisquare explainability enable --target prod --gateway-url https://x",
        ),
        _fix("odd", "aisquare project onboarding is not a thing"),  # a longer word, not the command
    ]
    assert fix_commands(not_buttons) == []


def test_fix_commands_ignores_ok_checks_and_deduplicates_by_command() -> None:
    passing = _fix(
        "claude-code", "(Re)connect it: aisquare agents connect claude-code", CheckStatus.ok
    )
    assert fix_commands([passing]) == [], "a fix hint on a healthy check is history, not a button"
    twice = [WARN_SNAPSHOT, _fix("snapshot-2", "Try: aisquare project onboard")]
    assert [fix.argv for fix in fix_commands(twice)] == [("project", "onboard", "--refresh")]


def test_fix_commands_admits_only_the_flags_the_table_allows() -> None:
    """`--config-dir <path>` on connect is real doctor output; two dirs make two buttons."""
    two_dirs = _fix(
        "claude-code",
        "aisquare agents connect claude-code --config-dir /home/me/.claude4; "
        "aisquare agents connect claude-code --config-dir /home/me/.claude5",
    )
    assert [fix.argv for fix in fix_commands([two_dirs])] == [
        ("agents", "connect", "claude-code", "--config-dir", "/home/me/.claude4"),
        ("agents", "connect", "claude-code", "--config-dir", "/home/me/.claude5"),
    ]
    already_flagged = _fix("snapshot", "Re-pack: aisquare project onboard --refresh.")
    assert [fix.argv for fix in fix_commands([already_flagged])] == [
        ("project", "onboard", "--refresh")
    ]
    unknown_flag = _fix("snapshot", "aisquare project onboard --force")
    assert fix_commands([unknown_flag]) == []


# --------------------------------------------------------------------------- apply_fix()


CONNECT = FixCommand(
    check="claude-code",
    argv=("agents", "connect", "claude-code"),
    scope="machine",
    source="(Re)connect it: aisquare agents connect claude-code",
)


def test_apply_fix_runs_the_command_with_json_in_the_given_cwd(tmp_path: Path) -> None:
    run = Scripted({"agents": _result('{"agent":"claude-code","hooks":4}')})
    result = apply_fix(CONNECT, tmp_path, run=run)
    assert result.ok and result.returncode == 0 and result.reason is None
    assert result.summary() == "✓ aisquare agents connect claude-code"
    assert run.calls == [(["--json", "agents", "connect", "claude-code"], tmp_path)]


def test_apply_fix_reports_a_failure_in_the_cli_words_and_never_raises(tmp_path: Path) -> None:
    run = Scripted({"agents": _result('{"error":"not_installed","ref":"claude-code"}', code=1)})
    result = apply_fix(CONNECT, tmp_path, run=run)
    assert not result.ok and result.returncode == 1
    assert result.summary() == (
        "✗ aisquare agents connect claude-code: "
        "aisquare agents connect claude-code failed: not_installed"
    )
    crashed = apply_fix(CONNECT, tmp_path, run=_raising)
    assert not crashed.ok and crashed.returncode is None
    assert crashed.reason is not None and "no such interpreter" in crashed.reason


def test_apply_fix_treats_doctor_fix_exit_1_with_a_report_as_done(tmp_path: Path) -> None:
    """`doctor --fix --yes` exits 1 for a check still FAILING after repairs — that is a report."""
    doctor_fix = FixCommand(
        check="sdk", argv=("doctor", "--fix", "--yes"), scope="machine", source="x"
    )
    with_report = Scripted(
        {"doctor": _result("fix: enabled\n" + _checks_json([FAIL_HOME]), code=1)}
    )
    assert apply_fix(doctor_fix, tmp_path, run=with_report).ok
    without = Scripted({"doctor": _result("", code=1, stderr="boom\n")})
    assert not apply_fix(doctor_fix, tmp_path, run=without).ok


# --------------------------------------------------------------------------- the real CLI


@dataclass
class Live:
    """``selfcli.run`` itself — the registered seam — in a hermetic environment, recording."""

    env: dict[str, str]
    calls: list[tuple[list[str], Path | None]] = field(default_factory=list)

    def __call__(self, args: Sequence[str], *, cwd: Path | None = None) -> CliResult:
        self.calls.append((list(args), cwd))
        return selfcli.run(args, cwd=cwd, env=self.env, timeout=120.0)


def _hermetic_env(**overrides: str) -> dict[str, str]:
    """This process's environment (the suite's isolated ``AISQUARE_HOME`` included).

    ``CLAUDE_CONFIG_DIR`` is always one of ``overrides``: the developer's real
    one must not be read. On POSIX the PATH is the bare default, so a machine
    with Node does not pack the directory with its repomix — CI has none, and
    the answer must be the same on both.
    """
    env = {**os.environ, **overrides, "NO_COLOR": "1"}
    if os.name == "posix":
        env["PATH"] = os.defpath
    return env


def test_onboard_runs_the_real_cli_in_a_throwaway_home(tmp_path: Path) -> None:
    """The premise of every fake above, checked once against the real thing.

    CONTRIBUTING: a fixture agrees with itself forever; only a live run shows
    that ``--json init --no-explainability <path>`` is a command the CLI accepts
    and answers with a ``SetupReport``, and that ``--json doctor`` answers with
    a list of checks. The assertion that matters is on the STATE the work should
    have produced: the project is in the store this process reads.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "README.md").write_text("hello\n", encoding="utf-8")
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    run = Live(_hermetic_env(CLAUDE_CONFIG_DIR=str(claude_dir)))

    outcome = onboard(proj, run=run)

    assert outcome.ok, outcome.reason
    assert [argv[:3] for argv, _ in run.calls] == [
        ["--json", "init", "--no-explainability"],
        ["--json", "doctor"],
    ]
    assert outcome.project_id == project_id_for(proj.resolve())
    assert outcome.report is not None and outcome.report.project.root == proj.resolve()
    assert not any(note.startswith("Explainability") for note in outcome.report.notes), (
        "the question was not asked and declining leaves no trace"
    )
    assert outcome.doctor_error is None, outcome.doctor_error
    by_name = {check.name: check for check in outcome.checks}
    assert {"home", "config", "database"} <= set(by_name), sorted(by_name)
    assert by_name["home"].status is CheckStatus.ok, by_name["home"]
    assert by_name["database"].status is CheckStatus.ok, by_name["database"]
    # The subprocess wrote where THIS process reads: the isolated home reached it.
    # Checked before the store is opened here, which would create the file itself.
    db = paths.db_path()
    assert db.is_relative_to(tmp_path) and db.exists(), db
    with store_session() as store:
        stored = store.get_project(outcome.project_id)
    assert stored is not None and stored.root == proj.resolve()


def test_onboard_reports_the_real_cli_failing_in_its_own_words(tmp_path: Path) -> None:
    """Negative control with the real CLI: a home that is a FILE stops ``init``.

    That failure used to be a Rich traceback, and this docstring used to say so.
    It is now the CLI's one-line convention: ``init`` writes config inside
    ``expected_config_write_errors``, which translates it to
    ``{"error":"home_not_creatable",…}``
    (``tests/test_config_write_failure_surface.py`` pins that end). This test is
    written to accept either — both say what could not be created — and neither
    may reach the UI as a box or a wrapped path tail.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    not_a_dir = tmp_path / "not-a-dir"
    not_a_dir.write_text("", encoding="utf-8")
    run = Live(
        _hermetic_env(**{HOME_ENV_VAR: str(not_a_dir), "CLAUDE_CONFIG_DIR": str(tmp_path / "c")})
    )

    outcome = onboard(proj, run=run)

    assert not outcome.ok and outcome.project_id is None and outcome.checks == ()
    assert len(run.calls) == 1, "doctor must not run after a failed init"
    assert outcome.reason is not None and outcome.reason.startswith("init failed")
    assert "File exists" in outcome.reason or "directory" in outcome.reason.lower(), outcome.reason
    assert "╰" not in outcome.reason and "❱" not in outcome.reason


def _is_process_site(node: ast.AST) -> bool:
    """``import subprocess`` either way, or a call to ``os.exec*``, ``os.spawn*``, ``os.system``."""
    if isinstance(node, ast.Import):
        return any(alias.name == "subprocess" for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        return node.module == "subprocess"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        owner = node.func.value
        starts = node.func.attr.startswith(("exec", "spawn", "posix_spawn"))
        return (
            isinstance(owner, ast.Name)
            and owner.id == "os"
            and (starts or node.func.attr == "system")
        )
    return False


def _process_sites(tree: ast.AST) -> list[int]:
    """Lines where a module would start a process on its own.

    AST, not grep: the word "subprocess" appears in this module's docstring —
    describing the seam it goes THROUGH — and a grep-based guard failed on
    exactly that line while being written.
    """
    return [
        node.lineno
        for node in ast.walk(tree)
        if _is_process_site(node) and isinstance(node, ast.stmt | ast.expr)
    ]


def test_the_process_site_matcher_sees_what_it_claims_to() -> None:
    """Positive control for the guard below, one case per shape it claims to catch."""
    assert _process_sites(ast.parse("import subprocess\n")) == [1]
    assert _process_sites(ast.parse("from subprocess import run\n")) == [1]
    assert _process_sites(ast.parse("import os\nos.execv('/bin/sh', [])\n")) == [2]
    assert _process_sites(ast.parse("import os\nos.system('ls')\n")) == [2]
    assert _process_sites(ast.parse("import os\nos.path.exists('/')\n")) == []


def test_the_service_starts_no_process_of_its_own() -> None:
    """`onboarding` reaches processes only through the registered seam `selfcli.run`."""
    module = Path(onboarding.__file__)
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    assert _process_sites(tree) == []
    for function in (onboarding.onboard, onboarding.apply_fix, onboarding.run_doctor):
        default = inspect.signature(function).parameters["run"].default
        assert default is selfcli.run, function.__name__


# --------------------------------------------------------------------------- INIT_FLAGS


#: Reports whether the process it runs in has a terminal on stdin.
_TTY_PROBE = "import sys; print('tty' if sys.stdin.isatty() else 'no-tty')"


def test_the_child_that_runs_init_never_has_a_terminal_on_stdin() -> None:
    """The fact ``INIT_FLAGS``' comment rests on, pinned so the comment cannot rot.

    That comment used to justify ``--no-explainability`` by saying ``selfcli.run``
    "leaves stdin as it found it — which, under the TUI, is the terminal". It does
    not: ``core/selfcli.py`` passes ``stdin=subprocess.DEVNULL`` on the one path
    ``onboard`` uses, so ``sys.stdin.isatty()`` is already False in the child and
    ``init`` could not have asked its question. The flag is belt to that brace, not
    the only thing holding §4.2's promise — and if the runner ever stops setting
    DEVNULL, this test says so rather than leaving the comment to lie again.

    Measured through the real seam. ``argv_for`` is swapped for the probe because
    the property under test is what ``run`` does with the child's stdin, not what
    it puts in argv (``test_onboard_runs_the_real_cli_in_a_throwaway_home`` covers
    that).

    THIS TEST NEEDS THE PTY. Written first without it, it passed with
    ``stdin=subprocess.DEVNULL`` deleted from ``selfcli.run`` — measured — because
    under pytest this process's own fd 0 is not a terminal, so an inherited stdin
    answers "no-tty" too and the assertion could not fail for the reason it exists.
    Putting a real terminal on fd 0 for the duration is what makes the two answers
    different, and the control below proves the terminal is actually there.
    """
    import subprocess

    probe = [sys.executable, "-c", _TTY_PROBE]
    master, slave = pty.openpty()
    saved_stdin = os.dup(0)
    try:
        os.dup2(slave, 0)
        # The control, taken at the same moment and from the same fd 0: a child
        # that INHERITS this process's stdin does see a terminal. Without it,
        # "no-tty" below is consistent with a probe that can only say one thing.
        inheriting = subprocess.run(probe, capture_output=True, text=True, check=True)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(selfcli, "argv_for", lambda args: probe)
            through_the_seam = selfcli.run(["--json", "init"])
    finally:
        os.dup2(saved_stdin, 0)
        os.close(saved_stdin)
        os.close(master)
        os.close(slave)

    assert inheriting.stdout.strip() == "tty", "the pty is not on fd 0 — result meaningless"
    assert through_the_seam.stdout.strip() == "no-tty", through_the_seam
    # The flag stays regardless: §4.2 puts the consent on the `explainability
    # enable` button, not on a prompt in a subprocess nobody is looking at.
    assert "--no-explainability" in onboarding.INIT_FLAGS
