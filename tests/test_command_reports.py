"""``asq exec`` and ``asq reports``: real commands, real pipes, the saved bytes read back.

Part 1 of docs/plans/evidence-gate.md. Each row of its traps table for part 1 has a test
here: an unreadable protected list deletes nothing, a grandchild holding the pipe does
not hang the wrapper, interrupts escalate on the process group, and an unrecorded
``--project`` report survives a burst of newer runs.
"""

from __future__ import annotations

import base64
import json
import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Collection
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from aisquare.cli.command_reports import register
from aisquare.core.source_revision import source_unchanged
from aisquare.core.state import RuntimeState, set_state
from aisquare.services import command_reports as reports

POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="process groups, setsid and POSIX signals"
)

#: A minimal app around the two commands, for a subprocess that must receive real signals.
DRIVER = (
    "import typer\n"
    "from aisquare.cli.command_reports import register\n"
    "app = typer.Typer()\n"
    "@app.callback()\n"
    "def main() -> None: pass\n"
    "register(app)\n"
    "app()\n"
)


def cli() -> typer.Typer:
    app = typer.Typer()

    @app.callback()
    def main(json_output: bool = typer.Option(False, "--json")) -> None:
        set_state(RuntimeState(json_output=json_output))

    register(app)
    return app


def python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _registered_project(root: Path) -> str:
    from aisquare.core.store import store_session
    from aisquare.core.workspace import current_project

    root.mkdir(parents=True, exist_ok=True)
    project = current_project(root)
    with store_session() as store:
        store.ensure_project(project)
    return project.id


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t.invalid",
            "-c",
            "user.name=t",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(root),
            *args,
        ],
        check=True,
        capture_output=True,
    )


# --------------------------------------------------------------------------- running


def test_runs_once_and_the_saved_bytes_are_exact(tmp_path: Path) -> None:
    marker = tmp_path / "runs"
    script = (
        "import sys; from pathlib import Path; p=Path(sys.argv[1]); "
        "p.write_text(p.read_text()+'x' if p.exists() else 'x'); "
        "sys.stdout.buffer.write(bytes([0,27,255,10])); "
        "sys.stderr.buffer.write(b'failure detail\\n'); sys.exit(9)"
    )
    report = reports.run_command([*python(script), str(marker)], cwd=tmp_path)
    assert report.returncode == report.exit_code == 9
    assert report.format == "passthrough" and report.output_complete
    assert reports.read_stream(report.id, "stdout", raw=True) == b"\x00\x1b\xff\n"
    assert reports.read_stream(report.id, "stderr", raw=True) == b"failure detail\n"
    for _ in range(3):
        assert reports.load_report(report.id).argv[-1] == str(marker)
        assert reports.read_stream(report.id, "stdout") == b"\x00\x1b\xff\n"
    result = CliRunner().invoke(cli(), ["reports", "show", report.id])
    assert result.exit_code == 0, "show reads; it does not report the command's status"
    assert marker.read_text() == "x", "reading a report never runs the command again"


def test_no_shell_and_no_argument_is_rewritten(tmp_path: Path) -> None:
    args = ["$(touch NOT_ALLOWED)", "`touch ALSO`", "hello world", "--json", "-q", "*.py"]
    report = reports.run_command(
        python("import json,sys; print(json.dumps(sys.argv[1:]))") + args, cwd=tmp_path
    )
    assert json.loads(reports.read_stream(report.id, "stdout")) == args
    assert not (tmp_path / "NOT_ALLOWED").exists() and not (tmp_path / "ALSO").exists()


def test_the_cli_returns_the_childs_status_and_keeps_its_flags() -> None:
    args = ["--json", "--quiet", "space value", "; exit 88"]
    result = CliRunner().invoke(
        cli(),
        [
            "--json",
            "exec",
            "--",
            *python("import json,sys;print(json.dumps(sys.argv[1:]));sys.exit(5)"),
            *args,
        ],
    )
    assert result.exit_code == 5, result.output
    payload = json.loads(result.stdout)
    assert json.loads(payload["stdout"]) == args
    assert payload["report"]["returncode"] == 5
    assert result.stderr == ""


def test_the_full_app_leaves_everything_after_the_separator_to_the_child() -> None:
    from aisquare.cli.app import app

    args = ["--json", "--profile", "child-profile", "--quiet", "--help"]
    result = CliRunner().invoke(
        app,
        ["--json", "exec", "--", *python("import json,sys;print(json.dumps(sys.argv[1:]))"), *args],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(json.loads(result.stdout)["stdout"]) == args


@pytest.mark.parametrize("missing", ["nonexistent", "a-directory"])
def test_a_command_that_cannot_start_is_a_saved_failure(tmp_path: Path, missing: str) -> None:
    target = tmp_path / missing
    if missing == "a-directory":
        target.mkdir()
    result = CliRunner().invoke(cli(), ["--json", "exec", "--", str(target)])
    expected = 127 if missing == "nonexistent" else 126
    assert result.exit_code == expected, result.output
    data = json.loads(result.stdout)
    assert data["report"]["launch_error"]
    assert data["report"]["returncode"] == expected
    assert "Unable to execute" in data["stderr"]
    assert reports.load_report(data["report"]["id"]).launch_error


def test_standard_input_is_inherited() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            DRIVER,
            "exec",
            "--",
            *python("import sys;sys.stdout.write(sys.stdin.read())"),
        ],
        input=b"input bytes\n",
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"input bytes\n"


@POSIX_ONLY
@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGINT])
def test_a_death_by_signal_is_recorded(number: int) -> None:
    report = reports.run_command(python(f"import os;os.kill(os.getpid(),{int(number)})"))
    assert report.returncode == -number
    assert report.signal == number and report.exit_code == 128 + number


@POSIX_ONLY
def test_a_death_by_signal_is_reproduced_after_the_report_is_saved() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            DRIVER,
            "exec",
            "--",
            *python("import os,signal;os.kill(os.getpid(),signal.SIGKILL)"),
        ],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == -signal.SIGKILL
    assert reports.list_reports()[0].signal == signal.SIGKILL


def test_an_invalid_request_runs_nothing(tmp_path: Path) -> None:
    marker = tmp_path / "should-not-exist"
    command = python(f"from pathlib import Path;Path({str(marker)!r}).touch()")
    with pytest.raises(ValueError, match="Output limit"):
        reports.run_command(command, max_output_bytes=0)
    with pytest.raises(ValueError, match="Output limit"):
        reports.run_command(command, max_output_bytes=reports.MAX_STREAM_LIMIT + 1)
    with pytest.raises(ValueError, match="Correlation"):
        reports.run_command(command, task_id="bad\x00id")
    with pytest.raises(ValueError, match="without NUL"):
        reports.run_command([*command, "\x00"])
    assert not marker.exists()
    result = CliRunner().invoke(cli(), ["--json", "exec"])
    assert result.exit_code == 2 and json.loads(result.stdout)["error"] == "usage"


# --------------------------------------------------------------------------- output


def test_both_streams_are_drained_and_truncation_is_marked() -> None:
    report = reports.run_command(
        python(
            "import sys;sys.stdout.buffer.write(b'x'*180000);sys.stderr.buffer.write(b'y'*170000)"
        ),
        max_output_bytes=73,
    )
    assert report.stdout.observed_bytes == 180000 and report.stderr.observed_bytes == 170000
    assert report.stdout.truncated and report.stderr.truncated and not report.output_complete
    assert reports.read_stream(report.id, "stdout", raw=True) == b"x" * 73
    assert reports.read_stream(report.id, "stderr", raw=True) == b"y" * 73
    assert report.metrics()["compaction_removed_bytes"] == 0, "truncation is not shortening"
    shown = CliRunner().invoke(cli(), ["reports", "show", report.id, "--raw"])
    assert "TRUNCATED stdout kept 73/180000" in shown.stderr
    assert "TRUNCATED stderr kept 73/170000" in shown.stderr


def test_the_default_keeps_one_mib_per_stream() -> None:
    report = reports.run_command(python("import sys;sys.stdout.buffer.write(b'z'*(1024*1024+5))"))
    assert report.per_stream_limit == 1024 * 1024
    assert report.stdout.retained_bytes == 1024 * 1024
    assert report.stdout.observed_bytes == 1024 * 1024 + 5


def test_pytest_progress_is_shortened_and_failure_details_survive(tmp_path: Path) -> None:
    (tmp_path / "test_example.py").write_text(
        "def test_ok():\n    assert True\n\ndef test_bad():\n    assert 'actual' == 'expected'\n"
    )
    report = reports.run_command(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--color=no"], cwd=tmp_path
    )
    assert report.returncode == 1
    original = reports.read_stream(report.id, "stdout", raw=True)
    shown = reports.read_stream(report.id, "stdout")
    assert report.format == "pytest", shown.decode()
    assert len(shown) < len(original)
    for detail in (b"test_bad", b"AssertionError", b"actual", b"expected", b"1 failed, 1 passed"):
        assert detail in shown


@pytest.mark.parametrize("flags", [["-s"], ["-sv"], ["--capture=no"], ["--capture", "no"]])
def test_pytest_with_its_capture_changed_passes_through(flags: list[str]) -> None:
    original = b"..... [100%]\nUser output must survive\n1 passed in 1s\n"
    assert reports.compact_stdout(["pytest", *flags], original, truncated=False) == (
        "passthrough",
        original,
    )


def test_pytest_without_a_final_count_passes_through() -> None:
    original = b"tests/test_a.py ..... [100%]\n"
    assert reports.compact_stdout(["pytest"], original, truncated=False)[0] == "passthrough"


def test_a_look_alike_progress_line_after_a_banner_is_kept() -> None:
    data = (
        b"tests/test_a.py ..F [100%]\n"
        b"=== FAILURES ===\n"
        b"captured: ... [ 50%]\n"
        b"... [ 50%]\n"
        b"1 failed, 2 passed in 1s\n"
    )
    name, shown = reports.compact_stdout(["pytest"], data, truncated=False)
    assert name == "pytest"
    assert b"tests/test_a.py ..F [100%]" not in shown
    assert b"\n... [ 50%]\n" in shown


@pytest.mark.parametrize(
    "data",
    [
        b"unknown\n",
        b"\x1b[31m... [100%]\x1b[0m\n1 passed in 1s\n",
        b"... [100%]\r1 passed in 1s\n",
        b"\xff... [100%]\n1 passed in 1s\n",
        b"... [100%]\x00\n1 passed in 1s\n",
    ],
)
def test_unrecognised_binary_and_control_output_passes_through(data: bytes) -> None:
    assert reports.compact_stdout(["pytest"], data, truncated=False) == ("passthrough", data)
    assert reports.compact_stdout(["other"], data, truncated=False) == ("passthrough", data)


def test_truncated_output_is_never_shortened() -> None:
    data = b"... [100%]\n1 passed in 1s\n"
    assert reports.compact_stdout(["pytest"], data, truncated=True) == ("passthrough", data)


def test_git_status_loses_its_hints_and_machine_formats_pass_through(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "file with spaces.py").write_text("example")
    report = reports.run_command(["git", "status"], cwd=root)
    assert report.returncode == 0
    shown = reports.read_stream(report.id, "stdout")
    assert report.format == "git-status"
    assert b"file with spaces.py" in shown and b"Untracked files:" in shown
    hint = b'  (use "git add <file>..." to include in what will be committed)'
    assert hint in reports.read_stream(report.id, "stdout", raw=True)
    assert hint not in shown
    for flags in (["--porcelain=v1", "-z"], ["--short"], ["-sb"], ["-z"]):
        machine = reports.run_command(["git", "status", *flags], cwd=root)
        assert machine.format == "passthrough", flags
        assert reports.read_stream(machine.id, "stdout") == reports.read_stream(
            machine.id, "stdout", raw=True
        )


def test_standard_error_is_never_shortened() -> None:
    noisy = "import sys;sys.stderr.write('... [100%]\\n1 passed in 1s\\n')"
    report = reports.run_command(python(noisy))
    assert reports.read_stream(report.id, "stderr") == b"... [100%]\n1 passed in 1s\n"


def test_raw_and_the_environment_switch_turn_shortening_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress = "print('tests/test_a.py ... [100%]');print('3 passed in 1s')"
    argv = [sys.executable, "-m", "pytest"]
    assert (
        reports.compact_stdout(
            argv, b"tests/test_a.py ... [100%]\n3 passed in 1s\n", truncated=False
        )[0]
        == "pytest"
    )
    result = CliRunner().invoke(cli(), ["--json", "exec", "--raw", "--", *python(progress)])
    assert json.loads(result.stdout)["report"]["format"] == "raw"
    monkeypatch.setenv("AISQUARE_REPORTS", "off")
    report = reports.run_command(python(progress))
    assert report.compaction_enabled is False and report.format == "raw"


def test_terminal_control_characters_are_escaped_for_display() -> None:
    text = reports.safe_text(b"ok\x1b]52;c;payload\x07\r\x00\x7f\xc2\x9b\n\t")
    assert "\x1b" not in text and "\x07" not in text and "\r" not in text and "\x9b" not in text
    assert "\\x1b]52" in text and text.endswith("\n\t")


def test_json_show_raw_is_base64_of_the_exact_bytes(tmp_path: Path) -> None:
    report = reports.run_command(
        python("import sys;sys.stdout.buffer.write(b'\\xff\\x00');sys.exit(3)"), cwd=tmp_path
    )
    result = CliRunner().invoke(cli(), ["--json", "reports", "show", report.id, "--raw"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["report"]["returncode"] == 3 and payload["encoding"] == "base64"
    assert base64.b64decode(payload["stdout"]) == b"\xff\x00"


def test_show_stream_stderr_keeps_the_footer_off_stderr() -> None:
    report = reports.run_command(python("import sys;sys.stderr.write('only this\\n')"))
    result = CliRunner().invoke(cli(), ["reports", "show", report.id, "--stream", "stderr"])
    assert result.exit_code == 0
    assert result.stderr == "only this\n"
    assert f"asq report {report.id}" in result.stdout
    bad = CliRunner().invoke(cli(), ["--json", "reports", "show", report.id, "--stream", "x"])
    assert bad.exit_code == 2 and json.loads(bad.stdout)["error"] == "usage"


def test_list_and_stats() -> None:
    first = reports.run_command(python("print('hello')"))
    result = CliRunner().invoke(cli(), ["--json", "reports", "list"])
    assert [item["id"] for item in json.loads(result.stdout)] == [first.id]
    stats = json.loads(CliRunner().invoke(cli(), ["--json", "reports", "stats"]).stdout)
    assert stats["retained_report_count"] == 1
    assert stats["observed_bytes"] == stats["display_body_bytes"] == len(os.linesep) + 5
    assert "not provider usage" in stats["token_estimate_method"]
    text = CliRunner().invoke(cli(), ["reports", "list"])
    assert first.id in text.stdout and "exit=0" in text.stdout


def test_correlation_ids_are_recorded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AISQUARE_PIPELINE_ID", "run-key")
    report = reports.run_command(
        python("print('work')"), session_id="s1", task_id="t1", project_id="p1", cwd=tmp_path
    )
    saved = reports.load_report(report.id)
    assert (saved.session_id, saved.task_id, saved.project_id) == ("s1", "t1", "p1")
    assert saved.cwd == str(tmp_path.resolve()) and saved.pipeline_id == "run-key"


# --------------------------------------------------------------------------- storage


@POSIX_ONLY
def test_storage_is_private_and_a_report_appears_only_once_complete(tmp_path: Path) -> None:
    report = reports.run_command(python("print('one')"))
    root = reports.reports_dir()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for directory in root.iterdir():
        assert not directory.name.startswith(".pending")
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        names = {file.name for file in directory.iterdir()}
        assert names == {
            "report.json",
            "stdout.bin",
            "stderr.bin",
            "stdout.display",
            "stderr.display",
        }
        for file in directory.iterdir():
            assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert reports.load_report(report.id).id == report.id


def test_a_report_is_published_only_after_its_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure while sealing leaves no report and no pending directory behind."""

    def broken(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    real = reports._write_file

    def failing(path: Path, data: bytes) -> None:
        if path.name == "report.json":
            broken()
        real(path, data)

    monkeypatch.setattr(reports, "_write_file", failing)
    with pytest.raises(OSError, match="disk full"):
        reports.run_command(python("print('ran')"))
    assert reports.list_reports() == []
    assert list(reports.reports_dir().iterdir()) == []


@POSIX_ONLY
def test_traversal_symlinks_and_changed_content_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="complete"):
        reports.load_report("../../credentials")
    report = reports.run_command(python("print('saved')"))
    directory = reports.reports_dir() / report.id
    (directory / "stdout.bin").unlink()
    secret = tmp_path / "secret"
    secret.write_text("must not read")
    (directory / "stdout.bin").symlink_to(secret)
    with pytest.raises((OSError, ValueError)):
        reports.read_stream(report.id, "stdout", raw=True)
    (directory / "stderr.bin").write_bytes(b"unexpected mutation")
    with pytest.raises(ValueError, match="byte counts"):
        reports.read_stream(report.id, "stderr", raw=True)


@POSIX_ONLY
def test_a_symlinked_reports_directory_is_refused_before_running(tmp_path: Path) -> None:
    root = reports.reports_dir()
    root.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    root.symlink_to(target, target_is_directory=True)
    marker = tmp_path / "ran"
    with pytest.raises(ValueError, match="real directory"):
        reports.run_command(python(f"open({str(marker)!r},'w')"))
    assert not marker.exists() and list(target.iterdir()) == []


# --------------------------------------------------------------------------- retention


def test_retention_keeps_the_newest_and_never_touches_a_run_in_progress(tmp_path: Path) -> None:
    first = reports.run_command(python("print('one')"))
    second = reports.run_command(python("print('two')"))
    pending = reports.reports_dir() / ".pending-active"
    pending.mkdir()
    (pending / "wrapper.pid").write_text(f"{os.getpid()}\n")
    untouched = tmp_path / "project-file"
    untouched.write_text("keep")
    assert reports.prune_reports(keep=1, protect=set()) == [first.id]
    assert [item.id for item in reports.list_reports()] == [second.id]
    assert pending.is_dir() and untouched.read_text() == "keep"
    assert reports.prune_reports(keep=0, protect=set()) == [second.id]


def test_each_run_applies_retention(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reports, "RETAIN_REPORTS", 2)
    reports.run_command(python("print(1)"))
    reports.run_command(python("print(2)"))
    latest = reports.run_command(python("print(3)"))
    assert reports.list_reports()[0].id == latest.id
    assert len(reports.list_reports()) == 2


def test_reports_older_than_the_window_expire() -> None:
    report = reports.run_command(python("print('old')"))
    _age(report, days=15)
    assert reports.prune_reports(keep=64, days=14, protect=set()) == [report.id]


def _age(report: reports.CommandReport, *, days: int) -> None:
    aged = report.model_copy(update={"finished_at": datetime.now(UTC) - timedelta(days=days)})
    (reports.reports_dir() / report.id / "report.json").write_text(aged.model_dump_json())


def test_a_protected_report_never_expires_and_does_not_count() -> None:
    protected = reports.run_command(python("print('evidence')"))
    _age(protected, days=400)
    plain = [reports.run_command(python(f"print({n})")) for n in range(2)]
    removed = reports.prune_reports(keep=1, days=14, protect={protected.id})
    assert protected.id not in removed
    assert removed == [plain[0].id], "the protected one does not take a place among the kept"


def test_an_unreadable_protected_list_means_unknown_and_prune_deletes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trap: a failed read of the protected list read as "nothing protected", and prune
    deleted reports that backed evidence."""
    ids = [reports.run_command(python(f"print({n})")).id for n in range(3)]
    for report_id in ids:
        _age(reports.load_report(report_id), days=30)

    def unreadable() -> Collection[str]:
        raise RuntimeError("the evidence store is locked")

    monkeypatch.setattr(reports, "protected_reports_provider", unreadable)
    assert reports.protected_report_ids() is None
    assert reports.prune_reports(keep=0, days=0, protect=None) == []
    # The automatic prune inside every run, and the CLI's, fail closed the same way.
    newest = reports.run_command(python("print('another run')"))
    result = CliRunner().invoke(cli(), ["--json", "reports", "prune", "--keep", "0", "--days", "0"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"removed": [], "protection_known": False}
    assert {item.id for item in reports.list_reports()} == {*ids, newest.id}


def test_the_protected_list_is_empty_until_evidence_is_recorded() -> None:
    assert reports.protected_report_ids() == set()


def test_an_unrecorded_project_report_survives_a_burst_of_newer_runs(tmp_path: Path) -> None:
    """Trap: 63 newer commands on the machine pruned a fresh report before its evidence
    was recorded."""
    root = tmp_path / "proj"
    project_id = _registered_project(root)
    (root / "code.py").write_text("x = 1\n")
    candidate = reports.run_command(python("print('proof')"), cwd=root, project_id=project_id)
    assert candidate.source_known
    burst = [reports.run_command([sys.executable, "-S", "-c", "pass"]) for _ in range(66)]
    assert reports.load_report(candidate.id).id == candidate.id
    kept = {item.id for item in reports.list_reports()}
    assert burst[0].id not in kept, "plain reports past the newest 64 are still removed"
    assert len(kept) == reports.RETAIN_REPORTS + 1


def test_an_unrecorded_project_report_still_expires_by_age(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    project_id = _registered_project(root)
    candidate = reports.run_command(python("print('proof')"), cwd=root, project_id=project_id)
    _age(candidate, days=15)
    assert candidate.id in reports.prune_reports(keep=64, days=14, protect=set())


# --------------------------------------------------------------------------- pipes, signals


def test_a_grandchild_holding_the_pipe_does_not_hang_the_wrapper() -> None:
    """Trap: a background process holding the output pipe kept ``asq exec`` blocked
    after the command exited."""
    holder = (
        "import subprocess, sys;"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'],"
        " stdout=sys.stdout, start_new_session=(sys.platform != 'win32'));"
        "print('started', p.pid, flush=True)"
    )
    started = time.monotonic()
    report = reports.run_command(python(holder))
    elapsed = time.monotonic() - started
    stdout = reports.read_stream(report.id, "stdout", raw=True)
    grandchild = int(stdout.split()[-1])
    try:
        assert report.returncode == 0, stdout
        assert stdout.startswith(b"started ")
        assert report.output_pipes_held_open and not report.output_complete
        assert reports.load_report(report.id).output_pipes_held_open
        assert elapsed < 12, f"capture waited {elapsed:.1f}s for the held-open pipe"
        shown = CliRunner().invoke(cli(), ["reports", "show", report.id])
        assert "held the output open" in shown.stderr
    finally:
        with suppress(OSError):
            os.kill(grandchild, getattr(signal, "SIGKILL", signal.SIGTERM))


def test_trailing_output_inside_the_drain_window_is_kept() -> None:
    """The negative half: output that is still arriving after the exit is not cut off."""
    late = (
        "import subprocess, sys;"
        "subprocess.Popen([sys.executable, '-c',"
        " 'import time,sys; time.sleep(0.5); print(\"late\", flush=True)'], stdout=sys.stdout);"
        "print('early', flush=True)"
    )
    report = reports.run_command(python(late))
    stdout = reports.read_stream(report.id, "stdout", raw=True).split()
    assert stdout == [b"early", b"late"]
    assert not report.output_pipes_held_open


def test_a_normal_command_records_no_held_open_pipe() -> None:
    report = reports.run_command(python("print('done')"))
    assert not report.output_pipes_held_open and report.output_complete


def test_the_escalation_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 1st interrupt is passed on as sent, the 2nd as SIGTERM, the 3rd as SIGKILL,
    each to the command's process group."""
    if sys.platform == "win32":
        pytest.skip("process groups, setsid and POSIX signals")
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, number: sent.append((pid, number)))

    class Child:
        pid = 4242

    for count in (1, 2, 3, 4):
        reports._escalate(Child(), signal.SIGINT, count)  # type: ignore[arg-type]
    assert sent == [
        (4242, signal.SIGINT),
        (4242, signal.SIGTERM),
        (4242, signal.SIGKILL),
        (4242, signal.SIGKILL),
    ]


@POSIX_ONLY
def test_interrupts_reach_the_process_group_and_escalate(tmp_path: Path) -> None:
    """Trap: a child that ignores interrupts survived the escalation. Here the command
    and a grandchild in its group both ignore SIGINT and SIGTERM; the third interrupt
    kills the group, the report is saved with what was sent, and the wrapper dies of
    the same signal as the command."""
    ready = tmp_path / "ready"
    grandchild_pid = tmp_path / "grandchild"
    child = (
        "import signal,subprocess,sys,time;from pathlib import Path;"
        "signal.signal(signal.SIGINT,signal.SIG_IGN);signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "g=subprocess.Popen([sys.executable,'-c','import signal,time;"
        "signal.signal(signal.SIGINT,signal.SIG_IGN);signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "time.sleep(60)']);"
        f"Path({str(grandchild_pid)!r}).write_text(str(g.pid));"
        f"time.sleep(0.3);Path({str(ready)!r}).write_text('ready');"
        "print('before',flush=True);time.sleep(60)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", DRIVER, "exec", "--", *python(child)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 20
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "the command never started"
        for _ in range(3):
            process.send_signal(signal.SIGINT)
            time.sleep(0.4)
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == -signal.SIGKILL, stderr
        assert b"before" in stdout
        report = reports.list_reports()[0]
        assert report.interrupted_by == signal.SIGINT
        assert report.signal == signal.SIGKILL
        grandchild = int(grandchild_pid.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail("the grandchild in the command's group survived SIGKILL")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@POSIX_ONLY
def test_a_child_that_exits_0_after_an_interrupt_still_records_it(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    child = (
        "import signal,sys,time;from pathlib import Path;"
        "signal.signal(signal.SIGINT,lambda *a: sys.exit(0));"
        f"Path({str(ready)!r}).write_text('ready');time.sleep(60)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", DRIVER, "exec", "--", *python(child)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 20
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        process.send_signal(signal.SIGINT)
        _, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stderr
        report = reports.list_reports()[0]
        assert report.returncode == 0 and report.interrupted_by == signal.SIGINT
        assert b"interrupted by signal" in stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


# --------------------------------------------------------------------------- source identity


def test_without_project_no_source_is_hashed(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(root: Path) -> dict[str, str]:
        raise AssertionError("a source scan is opt-in")

    monkeypatch.setattr(reports, "source_file_hashes", forbidden)
    report = reports.run_command(python("print('normal')"))
    assert report.source_root is None and report.source_fingerprint_before is None
    assert report.source_fingerprint_after is None and report.source_capture_error is None
    assert not report.source_known


def test_project_records_the_identity_before_and_after(tmp_path: Path) -> None:
    root = tmp_path / "source"
    project_id = _registered_project(root)
    (root / "code.py").write_text("version1\n")
    same = reports.run_command(python("print('checked')"), cwd=root, project_id=project_id)
    assert same.source_root == str(root.resolve()) and same.source_known
    assert same.source_fingerprint_before == same.source_fingerprint_after
    changed = reports.run_command(
        python("from pathlib import Path;Path('code.py').write_text('version2\\n')"),
        cwd=root,
        project_id=project_id,
    )
    assert changed.returncode == 0 and changed.source_known
    assert changed.source_fingerprint_before != changed.source_fingerprint_after
    files = reports.load_source_files(changed.id)
    assert files is not None and files.after is not None
    assert not source_unchanged(files.before, files.after)


def test_a_per_run_output_file_is_new_while_a_changed_or_vanished_file_is_not(
    tmp_path: Path,
) -> None:
    """The fingerprint row: a file the check wrote is ignored per file; a pre-existing
    one that changed or vanished is not."""
    root = tmp_path / "source"
    project_id = _registered_project(root)
    (root / "app.py").write_text("x = 1\n")
    (root / "other.py").write_text("y = 1\n")

    def files_of(code: str) -> reports.SourceFiles:
        report = reports.run_command(python(code), cwd=root, project_id=project_id)
        assert report.source_known, report.source_capture_error
        files = reports.load_source_files(report.id)
        assert files is not None and files.after is not None
        return files

    output = files_of("open('report.xml','w').write('<testsuite/>')")
    assert output.after is not None and "report.xml" in output.after
    assert "report.xml" not in output.before
    assert source_unchanged(output.before, output.after), "a new output file is ignored"
    edited = files_of("open('app.py','w').write('x = 2\\n')")
    assert edited.after is not None and not source_unchanged(edited.before, edited.after)
    vanished = files_of("import os;os.remove('other.py')")
    assert vanished.after is not None and not source_unchanged(vanished.before, vanished.after)
    regenerated = files_of("open('report.xml','w').write('<testsuite tests=\"2\"/>')")
    assert regenerated.after is not None
    assert not source_unchanged(regenerated.before, regenerated.after), (
        "once it existed before the run, report.xml is a pre-existing file like any other"
    )


def test_a_failed_capture_leaves_the_proof_unknown_and_the_command_runs(tmp_path: Path) -> None:
    marker = tmp_path / "did-run"
    report = reports.run_command(
        python(f"from pathlib import Path;Path({str(marker)!r}).touch()"),
        project_id="nonexistent-project",
        cwd=tmp_path,
    )
    assert marker.exists() and report.returncode == 0
    assert report.source_fingerprint_before is None and not report.source_known
    assert report.source_capture_error and "not registered" in report.source_capture_error
    shown = CliRunner().invoke(cli(), ["reports", "show", report.id])
    assert "source identity unknown" in shown.stderr


def test_a_capture_that_fails_after_the_command_is_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "source"
    project_id = _registered_project(root)
    (root / "code.py").write_text("x\n")
    from aisquare.core.source_revision import source_file_hashes as real

    calls: list[Path] = []

    def second_fails(path: Path) -> dict[str, str]:
        calls.append(path)
        if len(calls) > 1:
            raise ValueError("source changed while checking")
        return real(path)

    monkeypatch.setattr(reports, "source_file_hashes", second_fails)
    report = reports.run_command(python("print(1)"), cwd=root, project_id=project_id)
    assert report.source_fingerprint_before is not None
    assert report.source_fingerprint_after is None and not report.source_known
    assert report.source_capture_error and report.source_capture_error.startswith("After")


def test_a_worktree_hashes_its_own_files(tmp_path: Path) -> None:
    """The fingerprint row: a worktree is fingerprinted as its own files, under the
    board's one project."""
    from aisquare.core.store import store_session
    from aisquare.core.workspace import current_project

    main = tmp_path / "main"
    worktree = tmp_path / "worktree"
    main.mkdir()
    _git(main, "init", "-q")
    (main / "file.py").write_text("main source")
    _git(main, "add", "file.py")
    _git(main, "commit", "-qm", "fixture")
    _git(main, "worktree", "add", "-q", "--detach", str(worktree))
    project = current_project(main)
    assert current_project(worktree).id == project.id, "one board for both checkouts"
    with store_session() as store:
        store.ensure_project(project)
    report = reports.run_command(python("print('check')"), cwd=worktree, project_id=project.id)
    assert report.source_root == str(worktree.resolve()) and report.source_known
    (main / "file.py").write_text("an edit in the principal checkout")
    again = reports.run_command(python("print('check')"), cwd=worktree, project_id=project.id)
    assert again.source_fingerprint_before == report.source_fingerprint_after
    (worktree / "file.py").write_text("an edit in the worktree")
    edited = reports.run_command(python("print('check')"), cwd=worktree, project_id=project.id)
    assert edited.source_fingerprint_before != report.source_fingerprint_after


def test_a_command_outside_the_named_project_is_unknown(tmp_path: Path) -> None:
    project_id = _registered_project(tmp_path / "proj")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    report = reports.run_command(python("print(1)"), cwd=elsewhere, project_id=project_id)
    assert report.returncode == 0 and not report.source_known
    assert report.source_capture_error and "outside" in report.source_capture_error
