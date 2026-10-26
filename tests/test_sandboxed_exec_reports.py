"""Where a sandboxed ``asq exec`` saves its report and what the CLI does about it (task #21).

The seal and the adoption, which the launch's broker does, are run against a real broker in
``tests/test_a_sandboxed_codex_records_evidence.py``. These are the sandbox's own side, in
process: the report directory, the source it fingerprints without a store, the seal it asks
for when the run ends, and the paths a brokered command may read.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.services import command_reports as reports
from aisquare.services import store_broker

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the broker is POSIX's (FIFOs)")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@t.invalid"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "one"], check=True, capture_output=True
    )
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A launch's broker directory, and this process in its sandbox: the home read-only."""
    directory = tmp_path / "home" / "broker" / "launch"
    for name in (store_broker.REPORTS, store_broker.SEALS):
        (directory / name).mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(store_broker, "brokered", lambda environ=None: directory)
    return directory


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


# --- where a report is saved -------------------------------------------------------------


def test_a_report_is_saved_in_the_home_and_in_a_sandbox_in_the_launchs_own_directory(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_reports = Path(os.environ["AISQUARE_HOME"]) / "reports"
    assert reports.reports_dir() == home_reports
    assert reports.sandbox_dir() is None
    directory = tmp_path / "launch"
    monkeypatch.setattr(store_broker, "brokered", lambda environ=None: directory)

    assert reports.sandbox_dir() == directory
    assert reports.reports_dir() == directory / store_broker.REPORTS


def test_a_sandboxed_run_saves_a_whole_report_in_the_launchs_directory(
    repo: Path, launch: Path
) -> None:
    report = reports.run_command(_py("print('seen')"), cwd=repo)

    saved = launch / store_broker.REPORTS / report.id
    assert saved.is_dir() and stat.S_IMODE(saved.stat().st_mode) == 0o700
    assert not (Path(os.environ["AISQUARE_HOME"]) / "reports" / report.id).exists()
    assert reports.read_stream(report.id, "stdout") == b"seen\n"
    assert [r.id for r in reports.list_reports()] == [report.id]


def test_a_sandboxed_run_protects_no_report_and_asks_no_store(
    repo: Path, launch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its reports are scratch copies, and its store cannot be read: nothing is asked of it."""

    def refuses() -> set[str]:
        raise AssertionError("the store was asked")

    monkeypatch.setattr(reports, "protected_reports_provider", refuses)

    assert reports.protected_report_ids() == set()


def test_outside_a_sandbox_the_evidence_store_still_says_which_reports_are_kept(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reports, "protected_reports_provider", lambda: {"a" * 32})

    assert reports.protected_report_ids() == {"a" * 32}


def test_a_sandboxed_run_with_a_project_fingerprints_the_tree_it_runs_in_without_a_store(
    repo: Path, launch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store is read-only there and cannot say which project a directory is in: the
    source is the checkout the command ran in, and the broker checks it is the launch's."""

    def no_store() -> None:
        raise AssertionError("the store was opened")

    monkeypatch.setattr(reports, "store_session", no_store)
    sub = repo / "pkg"
    sub.mkdir()

    report = reports.run_command(_py("pass"), cwd=sub, project_id="prj_whatever")

    assert report.source_known and report.source_root is not None
    assert Path(report.source_root).samefile(repo)
    assert report.source_fingerprint_before == report.source_fingerprint_after
    assert report.project_id == "prj_whatever"


def test_outside_a_sandbox_the_project_is_asked_of_the_store(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = reports.run_command(_py("pass"), cwd=repo, project_id="prj_unknown")

    assert not report.source_known
    assert report.source_capture_error is not None
    assert "project ID is not registered" in report.source_capture_error


# --- the seal it asks for ------------------------------------------------------------------


def _exec(repo: Path, *command: str) -> tuple[int, str]:
    done = CliRunner().invoke(app, ["exec", "--", *command])
    return done.exit_code, done.output + done.stderr


def test_exec_in_a_sandbox_asks_its_broker_to_seal_the_report_it_saved(
    repo: Path, launch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[tuple[Path, list[str]]] = []

    def ask(broker: Path, argv: list[str], **kw: object) -> dict[str, object]:
        asked.append((broker, argv))
        return {"code": 0, "stdout": "sealed", "stderr": ""}

    monkeypatch.setattr(store_broker, "ask", ask)

    code, shown = _exec(repo, *_py("print('ok')"))

    assert code == 0 and "not sealed" not in shown
    [(broker, argv)] = asked
    [saved] = (launch / store_broker.REPORTS).iterdir()
    assert broker == launch and argv == ["reports", "seal", saved.name]


@pytest.mark.parametrize(
    ("answer", "why"),
    [
        ({"code": 1, "stdout": "", "stderr": "aisquare: it holds a link\n"}, "it holds a link"),
        ({"code": 2, "stdout": "", "stderr": ""}, "refused"),
        (
            store_broker.BrokerUnavailable("this agent's broker is gone"),
            "this agent's broker is gone",
        ),
    ],
    ids=["refused", "refused-silently", "unavailable"],
)
def test_a_report_the_broker_would_not_seal_is_said_and_the_commands_status_stands(
    answer: object, why: str, repo: Path, launch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def ask(broker: Path, argv: list[str], **kw: object) -> dict[str, object]:
        if isinstance(answer, Exception):
            raise answer
        assert isinstance(answer, dict)
        return answer

    monkeypatch.setattr(store_broker, "ask", ask)

    code, shown = _exec(repo, *_py("raise SystemExit(3)"))

    assert code == 3, "the command's own status"
    [saved] = (launch / store_broker.REPORTS).iterdir()
    assert (
        f"asq: report {saved.name} is not sealed ({why}); it cannot back `brief evidence`" in shown
    )


def test_exec_outside_a_sandbox_asks_no_broker(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def ask(*args: object, **kw: object) -> dict[str, object]:
        raise AssertionError("a broker was asked")

    monkeypatch.setattr(store_broker, "ask", ask)

    code, shown = _exec(repo, *_py("print('ok')"))

    assert code == 0 and "ok" in shown


def test_a_command_the_cli_runs_itself_is_never_handed_to_the_broker(
    launch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def ask(*args: object, **kw: object) -> dict[str, object]:
        raise AssertionError("handed to the broker")

    monkeypatch.setattr(store_broker, "ask", ask)

    for argv in (["exec", "--", "true"], ["--json", "exec", "--", "true"], ["reports", "list"],
                 ["-q", "reports", "show", "0" * 32], ["reports", "seal", "0" * 32]):  # fmt: skip
        assert store_broker.forward(argv) is None, argv
    assert frozenset({"exec", "reports"}) == store_broker.LOCAL


# --- what a brokered command may read ------------------------------------------------------


def test_a_path_is_in_the_launchs_tree_or_it_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    (tree / "out").symlink_to(other)
    environ = {store_broker.CALLER_ENV: "1", store_broker.ROOTS_ENV: str(tree.resolve())}

    assert store_broker.outside_tree(tree / "sub" / "a.png", environ) is None
    assert store_broker.outside_tree(tree, environ) is None
    for where in (other / "a", tree / "sub" / ".." / ".." / "other", tree / "out" / "a"):
        why = store_broker.outside_tree(where, environ)
        assert why is not None and "outside this agent's tree" in why, where
    # A command that is not a broker's reads what it likes; so does one with no tree named.
    assert store_broker.outside_tree(other / "a", {}) is None
    assert store_broker.outside_tree(other / "a", {store_broker.CALLER_ENV: "1"}) is None
    monkeypatch.setenv("HOME", str(tmp_path))
    assert store_broker.outside_tree(Path("~/other/a"), environ) is not None


# --- the checks a report is held to ---------------------------------------------------------


def _status(mode: int, uid: int) -> os.stat_result:
    return os.stat_result((mode, 1, 1, 1, uid, 1, 0, 0, 0, 0))


def test_a_file_must_belong_to_the_user_the_broker_runs_as_and_be_theirs_alone() -> None:
    if sys.platform == "win32":  # mypy narrows on this, not on the skipif that keeps it off
        pytest.skip("no user ids on Windows")
    me = os.getuid()
    reports._owner_only(_status(stat.S_IFREG | 0o600, me), me, "stdout.bin")
    reports._owner_only(_status(stat.S_IFDIR | 0o700, me), me, "the report's directory")
    with pytest.raises(reports.SandboxReportError, match="not owned by the user the broker"):
        reports._owner_only(_status(stat.S_IFREG | 0o600, me + 1), me, "stdout.bin")
    for mode in (0o640, 0o604, 0o660, 0o606, 0o666, 0o755):
        with pytest.raises(reports.SandboxReportError, match="not owner-only"):
            reports._owner_only(_status(stat.S_IFREG | mode, me), me, "stdout.bin")


def test_the_digest_of_a_report_cannot_be_matched_by_moving_bytes_between_its_files() -> None:
    """Names and lengths are part of what is sealed: a file that holds the next file's name
    and body is not the report that has them as two."""
    two = {"report.json": b"", "stderr.bin": b"x"}
    one = {"report.json": b"stderr.bin\0x"}

    assert reports._digest(two) != reports._digest(one)
    assert reports._digest(two) == reports._digest(dict(reversed(two.items())))
    assert reports._digest({"report.json": b"a"}) != reports._digest({"report.json": b"b"})
    assert len(reports._digest(two)) == 64
