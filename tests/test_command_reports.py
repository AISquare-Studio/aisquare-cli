"""Real command execution and recovery: output preservation, not only parser snapshots."""

from __future__ import annotations

import base64
import json
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from aisquare.cli.command_reports import register
from aisquare.core.state import RuntimeState, set_state
from aisquare.services import command_reports as reports


def cli() -> typer.Typer:
    app = typer.Typer()

    @app.callback()
    def main(json_output: bool = typer.Option(False, "--json")) -> None:
        set_state(RuntimeState(json_output=json_output))

    register(app)
    return app


def python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_runs_once_and_recovery_preserves_exact_binary_streams(tmp_path: Path) -> None:
    marker = tmp_path / "number of runs"
    script = (
        "import sys; from pathlib import Path; "
        "p=Path(sys.argv[1]); p.write_text(p.read_text()+'x' if p.exists() else 'x'); "
        "sys.stdout.buffer.write(bytes([0,27,255,10])); "
        "sys.stderr.buffer.write(b'failure detail\\n'); sys.exit(9)"
    )
    report = reports.run_command([sys.executable, "-c", script, str(marker)], cwd=tmp_path)
    assert report.returncode == report.exit_code == 9
    assert report.format == "passthrough"
    assert reports.read_stream(report.id, "stdout", raw=True) == b"\x00\x1b\xff\n"
    assert reports.read_stream(report.id, "stderr", raw=True) == b"failure detail\n"
    for _ in range(3):
        assert reports.load_report(report.id).argv[-1] == str(marker)
        assert reports.read_stream(report.id, "stdout") == b"\x00\x1b\xff\n"
    assert marker.read_text() == "x"
    assert report.metrics()["compaction_removed_bytes"] == 0


def test_no_shell_expansion_or_arg_rewriting(tmp_path: Path) -> None:
    args = [
        "$(touch NOT_ALLOWED)",
        "`touch ALSO_NOT_ALLOWED`",
        "hello world",
        "--json",
        "-q",
        "*.py",
    ]
    report = reports.run_command(
        python("import json,sys; print(json.dumps(sys.argv[1:]))") + args, cwd=tmp_path
    )
    assert json.loads(reports.read_stream(report.id, "stdout")) == args
    assert not (tmp_path / "NOT_ALLOWED").exists()
    assert not (tmp_path / "ALSO_NOT_ALLOWED").exists()


def test_read_json_recovers_binary_and_never_executes_again(tmp_path: Path) -> None:
    report = reports.run_command(
        python("import sys;sys.stdout.buffer.write(b'\\xff\\x00');sys.exit(3)"), cwd=tmp_path
    )
    result = CliRunner().invoke(cli(), ["--json", "reports", "show", report.id, "--raw"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["report"]["returncode"] == 3
    assert payload["encoding"] == "base64"
    assert base64.b64decode(payload["stdout"]) == b"\xff\x00"
    assert result.stderr == ""


def test_cli_forwards_argv_and_returns_failure_status() -> None:
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


def test_both_streams_drained_when_large_and_truncation_not_counted_as_compaction() -> None:
    report = reports.run_command(
        python(
            "import sys;sys.stdout.buffer.write(b'x'*180000);sys.stderr.buffer.write(b'y'*170000)"
        ),
        max_output_bytes=73,
    )
    assert report.stdout.observed_bytes == 180000
    assert report.stderr.observed_bytes == 170000
    assert report.stdout.truncated and report.stderr.truncated
    assert reports.read_stream(report.id, "stdout", raw=True) == b"x" * 73
    assert reports.read_stream(report.id, "stderr", raw=True) == b"y" * 73
    assert report.format == "passthrough"
    assert report.metrics()["compaction_removed_bytes"] == 0
    shown = CliRunner().invoke(cli(), ["reports", "show", report.id, "--raw"])
    assert "TRUNCATED" in shown.stderr
    assert "73/180000" in shown.stderr
    assert "73/170000" in shown.stderr


def test_real_pytest_failure_details_survive(tmp_path: Path) -> None:
    (tmp_path / "test_example.py").write_text(
        "def test_ok():\n    assert True\n\ndef test_bad():\n    assert 'actual' == 'expected'\n"
    )
    report = reports.run_command([sys.executable, "-m", "pytest", "-q", "--color=no"], cwd=tmp_path)
    assert report.returncode == 1
    original = reports.read_stream(report.id, "stdout", raw=True)
    output = reports.read_stream(report.id, "stdout")
    assert report.format == "pytest", output.decode()
    assert len(output) < len(original)
    for detail in (b"test_bad", b"AssertionError", b"actual", b"expected", b"1 failed, 1 passed"):
        assert detail in output


def test_real_pytest_success_warning_and_summary_survive(tmp_path: Path) -> None:
    (tmp_path / "test_example.py").write_text(
        "import warnings\ndef test_ok():\n    warnings.warn('important warning')\n    assert True\n"
    )
    report = reports.run_command([sys.executable, "-m", "pytest", "-q", "--color=no"], cwd=tmp_path)
    assert report.returncode == 0
    assert report.format == "pytest"
    output = reports.read_stream(report.id, "stdout")
    assert b"important warning" in output and b"1 passed" in output
    assert int(report.metrics()["compaction_removed_bytes"]) > 0


@pytest.mark.parametrize("suffix", [["-s"], ["--capture=no"], ["--capture", "no"]])
def test_pytest_user_print_output_is_not_compacted(suffix: list[str]) -> None:
    original = b"..... [100%]\nUser output must survive\n1 passed in 1s\n"
    assert reports.compact_stdout(["pytest", *suffix], original, truncated=False) == (
        "passthrough",
        original,
    )


def test_real_git_status_preserves_paths_and_machine_formats(tmp_path: Path) -> None:
    tmp_path = tmp_path / "repo"
    tmp_path.mkdir()
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "file with spaces.py").write_text("example")
    report = reports.run_command(["git", "status"], cwd=tmp_path)
    assert report.returncode == 0
    output = reports.read_stream(report.id, "stdout")
    assert b"file with spaces.py" in output
    assert b"Untracked files:" in output
    assert report.format == "git-status"
    assert int(report.metrics()["compaction_removed_bytes"]) > 0
    short = reports.run_command(["git", "status", "--porcelain=v1", "-z"], cwd=tmp_path)
    assert short.format == "passthrough"
    assert reports.read_stream(short.id, "stdout") == b"?? file with spaces.py\x00"


@pytest.mark.parametrize(
    "data",
    [
        b"unknown\n",
        b"...\nE\n1 passed in 1s\n",
        b"\x1b[31m... [100%]\x1b[0m\n1 passed in 1s\n",
        b"... [100%]\r1 passed in 1s\n",
        b"\xff... [100%]\n1 passed in 1s\n",
    ],
)
def test_unrecognised_and_terminal_effects_passthrough(data: bytes) -> None:
    assert reports.compact_stdout(["pytest"], data, truncated=False) == ("passthrough", data)
    assert reports.compact_stdout(["not-pytest"], data, truncated=False) == ("passthrough", data)


def test_disabled_control_and_correlation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AISQUARE_REPORTS", "off")
    monkeypatch.setenv("AISQUARE_PIPELINE_ID", "trace-run-not-a-session")
    report = reports.run_command(
        python("print('work')"),
        session_id="session1",
        task_id="task1",
        project_id="project1",
        cwd=tmp_path,
    )
    assert report.compaction_enabled is False and report.format == "raw"
    assert report.session_id == "session1" and report.task_id == "task1"
    assert report.project_id == "project1" and report.cwd == str(tmp_path)
    assert report.pipeline_id == "trace-run-not-a-session"
    assert reports.read_stream(report.id, "stdout") == reports.read_stream(
        report.id, "stdout", raw=True
    )


def test_launch_failure_is_saved_and_cli_json_is_strict(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli(), ["--json", "exec", "--", str(tmp_path / "nonexistent")])
    assert result.exit_code == 127
    data = json.loads(result.stdout)
    assert data["report"]["launch_error"]
    assert data["report"]["returncode"] == 127
    assert "Unable to execute" in data["stderr"]
    assert result.stderr == ""


def test_raw_option_and_stats_json() -> None:
    runner = CliRunner()
    result = runner.invoke(cli(), ["--json", "exec", "--raw", "--", *python("print('hello')")])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["report"]["format"] == "raw"
    assert base64.b64decode(payload["stdout"]) == b"hello\n"
    result = runner.invoke(cli(), ["--json", "reports", "stats"])
    metrics = json.loads(result.stdout)
    assert metrics["retained_report_count"] == 1
    assert metrics["observed_bytes"] == metrics["display_body_bytes"] == 6
    assert metrics["estimated_retained_tokens"] == 2
    assert "not provider usage" in metrics["token_estimate_method"]
    assert metrics["compaction_removed_bytes"] == 0


def test_missing_command_and_bad_stream_json_errors() -> None:
    runner = CliRunner()
    result = runner.invoke(cli(), ["--json", "exec"])
    assert result.exit_code == 2 and json.loads(result.stdout)["error"] == "usage"
    result = runner.invoke(cli(), ["--json", "reports", "show", "0" * 32, "--stream", "invalid"])
    assert result.exit_code == 2 and json.loads(result.stdout)["error"] == "usage"


def test_private_atomic_storage_and_explicit_retention(tmp_path: Path) -> None:
    first = reports.run_command(python("print('one')"))
    second = reports.run_command(python("print('two')"))
    root = reports.reports_dir()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for directory in root.iterdir():
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert not directory.name.startswith(".pending")
        for file in directory.iterdir():
            assert stat.S_IMODE(file.stat().st_mode) == 0o600
    pending = root / ".pending-active"
    pending.mkdir()
    untouched = tmp_path / "project-file"
    untouched.write_text("keep")
    assert reports.prune_reports(keep=1) == [first.id]
    assert [item.id for item in reports.list_reports()] == [second.id]
    assert pending.is_dir() and untouched.read_text() == "keep"
    assert reports.prune_reports(keep=0) == [second.id]


def test_automatic_retention_keeps_newest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reports, "RETAIN_REPORTS", 2)
    reports.run_command(python("print(1)"))
    reports.run_command(python("print(2)"))
    latest = reports.run_command(python("print(3)"))
    assert len(reports.list_reports()) == 2
    assert reports.list_reports()[0].id == latest.id


def test_path_traversal_symlinks_and_corrupt_content_are_rejected(tmp_path: Path) -> None:
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


def test_symlink_root_refused_before_execution(tmp_path: Path) -> None:
    root = reports.reports_dir()
    root.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    root.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="real directory"):
        reports.run_command(python("raise Exception('should never execute')"))
    assert list(target.iterdir()) == []


def test_terminal_control_characters_are_escaped() -> None:
    text = reports.safe_text(b"ok\x1b]52;c;payload\x07\r\x00\x7f\xc2\x9b\n\t")
    assert "\x1b" not in text and "\x07" not in text and "\r" not in text
    assert "\x9b" not in text
    assert "\\x1b]52" in text
    assert text.endswith("\n\t")


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGINT])
def test_child_signal_identity_saved(number: int) -> None:
    report = reports.run_command(python(f"import os,signal;os.kill(os.getpid(),{number})"))
    assert report.returncode == -number
    assert report.signal == number and report.exit_code == 128 + number
    assert reports.load_report(report.id).signal == number


def test_cli_signal_forwarding_and_originals_recoverable(tmp_path: Path) -> None:
    marker = tmp_path / "child-started"
    driver = (
        "from aisquare.cli.command_reports import register; import typer;"
        "a=typer.Typer();register(a);a()"
    )
    child_code = (
        "import sys,time;from pathlib import Path;"
        "Path(sys.argv[1]).write_text('ready');"
        "print('before signal',flush=True);time.sleep(20)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", driver, "exec", "--", *python(child_code), str(marker)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 8
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists()
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=8)
        assert process.returncode == -signal.SIGTERM
        assert b"before signal" in stdout
        assert b"asq report" in stderr
        report = reports.list_reports()[0]
        assert report.signal == signal.SIGTERM
        assert reports.read_stream(report.id, "stdout", raw=True) == b"before signal\n"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_child_stdin_is_inherited(tmp_path: Path) -> None:
    driver = (
        "from aisquare.cli.command_reports import register;"
        "import typer;a=typer.Typer();register(a);a()"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            driver,
            "exec",
            "--",
            *python("import sys;sys.stdout.write(sys.stdin.read())"),
        ],
        input=b"input bytes\n",
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout == b"input bytes\n"


def test_full_app_global_flags_do_not_consume_child_options() -> None:
    from aisquare.cli.app import app

    args = ["--json", "--profile", "child-profile", "--quiet", "--help"]
    result = CliRunner().invoke(
        app,
        ["--json", "exec", "--", *python("import json,sys;print(json.dumps(sys.argv[1:]))"), *args],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(json.loads(result.stdout)["stdout"]) == args
    assert result.stderr == ""


def test_cli_child_sigkill_is_preserved_after_saving() -> None:
    driver = (
        "from aisquare.cli.command_reports import register;"
        "import typer;a=typer.Typer();register(a);a()"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            driver,
            "exec",
            "--",
            *python("import os,signal;os.kill(os.getpid(),signal.SIGKILL)"),
        ],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == -signal.SIGKILL
    assert reports.list_reports()[0].signal == signal.SIGKILL


def test_invalid_request_never_executes(tmp_path: Path) -> None:
    marker = tmp_path / "should-not-exist"
    command = python(f"from pathlib import Path;Path({str(marker)!r}).touch()")
    with pytest.raises(ValueError, match="Output limit"):
        reports.run_command(command, max_output_bytes=0)
    with pytest.raises(ValueError, match="Correlation"):
        reports.run_command(command, task_id="bad\x00id")
    with pytest.raises(ValueError, match="without NUL"):
        reports.run_command([*command, "\x00"])
    assert not marker.exists()


def _registered_project(root: Path) -> str:
    from aisquare.core.store import store_session
    from aisquare.core.workspace import current_project

    root.mkdir()
    project = current_project(root)
    with store_session() as store:
        store.ensure_project(project)
    return project.id


def test_project_proof_captures_before_and_after_content(tmp_path: Path) -> None:
    root = tmp_path / "source"
    project_id = _registered_project(root)
    source = root / "code.py"
    source.write_text("version1\n")
    unchanged = reports.run_command(python("print('checked')"), cwd=root, project_id=project_id)
    assert unchanged.completed
    assert unchanged.source_root == str(root)
    assert unchanged.source_capture_error is None
    assert unchanged.source_fingerprint_before == unchanged.source_fingerprint_after
    assert unchanged.source_fingerprint_before is not None
    changed = reports.run_command(
        python("from pathlib import Path;Path('code.py').write_text('version2\\n')"),
        cwd=root,
        project_id=project_id,
    )
    assert changed.returncode == 0
    assert changed.source_fingerprint_before != changed.source_fingerprint_after
    assert (
        reports.load_report(changed.id).source_fingerprint_after == changed.source_fingerprint_after
    )


def test_source_proof_unavailable_does_not_block_execution(tmp_path: Path) -> None:
    marker = tmp_path / "did-run"
    report = reports.run_command(
        python(f"from pathlib import Path;Path({str(marker)!r}).touch()"),
        project_id="nonexistent-project",
        cwd=tmp_path,
    )
    assert marker.exists() and report.returncode == 0
    assert report.source_fingerprint_before is None
    assert report.source_capture_error and "not registered" in report.source_capture_error


def test_unscoped_commands_do_not_hash_source(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(root: Path) -> str:
        raise AssertionError("source scan must be opt-in")

    monkeypatch.setattr(reports, "source_fingerprint", forbidden)
    report = reports.run_command(python("print('normal')"))
    assert (
        report.source_root
        is report.source_fingerprint_before
        is report.source_fingerprint_after
        is None
    )
    assert report.source_capture_error is None


def test_source_proof_uses_actual_linked_worktree(tmp_path: Path) -> None:
    from aisquare.core.store import store_session
    from aisquare.core.workspace import current_project

    main = tmp_path / "main"
    worktree = tmp_path / "worktree"
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    (main / "file.py").write_text("main source")
    subprocess.run(["git", "-C", str(main), "add", "file.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(main),
            "-c",
            "user.name=E2E",
            "-c",
            "user.email=e2e@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "--detach", str(worktree)],
        check=True,
        capture_output=True,
    )
    project = current_project(main)
    with store_session() as store:
        store.ensure_project(project)
    report = reports.run_command(
        python("print('check worktree')"), cwd=worktree, project_id=project.id
    )
    assert report.source_root == str(worktree)
    assert report.source_fingerprint_before == report.source_fingerprint_after
    assert report.source_capture_error is None
    (worktree / "file.py").write_text("different checkout source")
    updated = reports.run_command(python("print('new check')"), cwd=worktree, project_id=project.id)
    assert updated.source_fingerprint_after != report.source_fingerprint_after
    assert (main / "file.py").read_text() == "main source"
