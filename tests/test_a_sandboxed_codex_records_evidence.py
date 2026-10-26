"""A sandboxed agent records evidence and reads the code index (task #21 of #113).

Codex sandboxes its tools: the aisquare store is read-only there, and its board calls run
outside the sandbox from its launch's broker (``services.store_broker``). Two things 0.9.0
added could not: ``aisquare code find|refs|similar``, and the evidence gate (``aisquare brief
…``, ``asq exec``), so ``task done`` on a task a work brief links could never pass for it.

Here the CLI side is the sandbox (the suite's ``running`` fixture holds the store read-only
to it, as ``tests/test_store_broker.py`` does) and the broker is a real process, started as a
launch starts one. The report ``asq exec`` saves goes to the launch's own ``reports/``; the
broker seals it, ``brief evidence --report`` adopts it, and ``task done`` is gated at the store.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.services import briefs, store_broker, team
from aisquare.services import command_reports as reports
from tests.test_store_broker import MINE, running, work  # noqa: F401 - fixtures, by name

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the broker is POSIX's (FIFOs)")

REPORT_ID = re.compile(r"[0-9a-f]{32}")


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t.invalid", "-c", "user.name=t", "-c", "commit.gpgsign=false",
         "-C", str(root), *args],
        check=True, capture_output=True,
    )  # fmt: skip


@pytest.fixture
def tree(work: Path) -> Path:  # noqa: F811 - pytest resolves fixtures by NAME
    """The launch's checkout, with a module and its test committed."""
    (work / "app.py").write_text("def answer():\n    return 42\n")
    (work / "test_app.py").write_text(
        "from app import answer\n\n\ndef test_answer():\n    assert answer() == 42\n"
    )
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "one")
    return work


def _board() -> str:
    return team.resolve_project().id


def _linked(*requirements: str) -> tuple[str, str]:
    """A work brief's id and the id of a task linked to all of its requirements."""
    brief = briefs.create("Answer", list(requirements or ("answer() is 42",)))
    task, _ = team.add_task("Build the answer", role="coder")
    briefs.link(brief.id, task.id, [r.id for r in brief.requirements])
    return brief.id, task.id


def _sandboxed(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """``aisquare <argv>`` as the agent's tool runs it: its broker, or itself (:data:`LOCAL`)."""
    code = store_broker.forward(argv)
    if code is None:
        runner = CliRunner()
        done = runner.invoke(app, argv)
        return subprocess.CompletedProcess(argv, done.exit_code, done.output, done.stderr)
    return subprocess.CompletedProcess(argv, code, "", "")


def _exec(task: str, *command: str) -> tuple[str, str]:
    """``asq exec --project … --task … -- command`` run in the sandbox; its report's id and
    everything it printed."""
    done = CliRunner().invoke(app, ["exec", "--project", _board(), "--task", task, "--", *command])
    found = REPORT_ID.findall(done.output + done.stderr)
    assert found, done.output
    return found[-1], done.output + done.stderr


def _through_broker(running: Path, argv: list[str]) -> dict[str, object]:  # noqa: F811
    return store_broker.ask(running, argv)


def _answered(running: Path, argv: list[str]) -> tuple[int, str]:  # noqa: F811
    """What the broker made of ``argv``: its status and everything it printed. A refusal is a
    line, never a crash (Rich prints a traceback with the source lines around the failure, and
    so shows the very words a test looks for)."""
    answer = store_broker.ask(running, argv)
    text = str(answer["stdout"]) + str(answer["stderr"])
    assert "Traceback" not in text, text
    return int(str(answer["code"])), text


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


# --- the symbol index -------------------------------------------------------------------


def test_a_sandboxed_agent_finds_a_definition_through_its_broker(
    running: Path,  # noqa: F811
    tree: Path,
) -> None:
    """``code find|refs|similar`` are reads on the whitelist; the first query builds the
    index, outside the sandbox. ``code index`` writes it, and stays out."""
    code, text = _answered(running, ["--json", "code", "find", "answer"])

    assert code == 0, text
    symbols = json.loads(text)["symbols"]
    assert [s["name"] for s in symbols] == ["answer"] and symbols[0]["path"] == "app.py"
    refs_code, refs_text = _answered(running, ["code", "refs", "answer"])
    similar_code, _ = _answered(running, ["code", "similar", "answer"])
    assert (refs_code, similar_code) == (0, 0), refs_text
    refused, why = _answered(running, ["code", "index"])
    assert refused == 2 and "cannot run from this agent's sandbox" in why


# --- evidence ---------------------------------------------------------------------------


def _record(running: Path, brief: str, task: str, report: str, **more: str) -> tuple[int, str]:  # noqa: F811
    return _answered(
        running,
        [
            "brief", "evidence", brief, "R1", "--task", task,
            "--verdict", more.get("verdict", "pass"), "--summary", "ran the check",
            "--report", report, "--as", MINE[:8],
        ],
    )  # fmt: skip


def _claim(running: Path, task: str) -> None:  # noqa: F811
    code, text = _answered(running, ["task", "claim", task, "--as", MINE[:8]])
    assert code == 0, text


def test_a_sandboxed_exec_is_run_in_the_sandbox_and_its_report_is_the_launchs(
    running: Path,  # noqa: F811
    tree: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``exec`` is never handed to the broker, which would run the agent's argv outside its
    sandbox: the CLI runs it, and saves the report where the sandbox may write."""
    assert store_broker.forward(["exec", "--", "true"]) is None
    assert store_broker.forward(["--json", "reports", "list"]) is None
    assert store_broker.forward(["reports", "show", "0" * 32]) is None
    _, task = _linked()

    report_id, shown = _exec(task, *_py("print('checked')"))

    saved = running / store_broker.REPORTS / report_id
    assert saved.is_dir() and stat.S_IMODE(saved.stat().st_mode) == 0o700
    assert sorted(path.name for path in saved.iterdir()) == [
        "report.json", "source.json", "stderr.bin", "stderr.display",
        "stdout.bin", "stdout.display",
    ]  # fmt: skip
    assert not (Path(os.environ["AISQUARE_HOME"]) / "reports" / report_id).exists()
    report = reports.CommandReport.model_validate_json((saved / "report.json").read_text())
    assert report.project_id == _board() and report.source_known
    assert report.source_root is not None and Path(report.source_root).samefile(tree)
    assert "checked" in shown and "not sealed" not in shown
    seal = running / store_broker.SEALS / report_id
    assert (
        re.fullmatch(r"[0-9a-f]{64}\n", seal.read_text())
        and stat.S_IMODE(seal.stat().st_mode) == 0o600
    )


def test_a_sandboxed_report_backs_a_pass_and_task_done_passes_only_with_it(
    running: Path,  # noqa: F811
    tree: Path,
) -> None:
    """The review's flow: ``exec`` in the sandbox, ``brief evidence --report`` through the
    broker (which adopts the report), ``task done`` through the broker, gated at the store."""
    brief, task = _linked()
    _claim(running, task)

    refused, why = _answered(running, ["task", "done", task, "--as", MINE[:8]])
    assert refused == 1 and "R1 [missing]" in why
    assert team.show_task(task).status == "doing"

    report_id, _ = _exec(task, *_py("print('ok')"))
    assert not (Path(os.environ["AISQUARE_HOME"]) / "reports" / report_id).exists()
    code, text = _record(running, brief, task, report_id)
    assert code == 0 and "recorded R1 pass [report]" in text, text
    adopted = Path(os.environ["AISQUARE_HOME"]) / "reports" / report_id
    assert adopted.is_dir() and stat.S_IMODE(adopted.stat().st_mode) == 0o700
    for name in ("report.json", "stdout.bin", "stderr.bin", "source.json"):
        assert (adopted / name).read_bytes() == (
            running / store_broker.REPORTS / report_id / name
        ).read_bytes()
    evidence = briefs.show(brief).brief.evidence[-1]
    assert (evidence.kind, evidence.report_id) == ("report", report_id)

    done, text = _answered(running, ["task", "done", task, "--as", MINE[:8]])
    assert done == 0, text
    assert team.show_task(task).status == "done"


def test_a_pass_from_a_sandboxed_report_goes_stale_when_the_source_changes(
    running: Path,  # noqa: F811
    tree: Path,
) -> None:
    """The broker reads the source itself at ``task done``: the same files before the run,
    after it and now."""
    brief, task = _linked()
    _claim(running, task)
    report_id, _ = _exec(task, *_py("print('ok')"))
    assert _record(running, brief, task, report_id)[0] == 0

    (tree / "app.py").write_text("def answer():\n    return 41\n")

    code, text = _answered(running, ["task", "done", task, "--as", MINE[:8]])
    assert code == 1 and "R1 [stale]" in text
    assert team.show_task(task).status == "doing"


def test_a_failing_run_cannot_back_a_pass_from_the_sandbox_either(
    running: Path,  # noqa: F811
    tree: Path,
) -> None:
    brief, task = _linked()
    report_id, _ = _exec(task, *_py("raise SystemExit(3)"))

    code, text = _record(running, brief, task, report_id)

    assert code == 1 and "cannot back a pass" in text and "it exited 3" in text


# --- what is refused --------------------------------------------------------------------


def _saved(running: Path, task: str, code: str = "print('ok')") -> tuple[str, Path]:  # noqa: F811
    report_id, _ = _exec(task, *_py(code))
    return report_id, running / store_broker.REPORTS / report_id


def test_a_report_changed_after_its_run_is_refused(
    running: Path,  # noqa: F811
    tree: Path,
) -> None:
    """Sealed when the run ended: the sandbox's later edit, of what the command printed or
    of how it ended, is not what the broker saw. An edit that leaves the files as long as the
    report says is refused by the seal; one that does not, by the report's own lengths."""
    brief, task = _linked()
    year = datetime.now(tz=UTC).strftime("%Y")
    sealed = "changed after its run"
    edits: list[tuple[str, Callable[[Path], object], str]] = [
        ("stdout.bin", lambda p: p.write_bytes(b"X" + p.read_bytes()[1:]), sealed),
        ("report.json", lambda p: p.write_text(p.read_text().replace(year, "2001", 1)), sealed),
        ("stdout.bin", lambda p: p.write_bytes(p.read_bytes() + b"forged\n"), "is not the"),
    ]
    for n, (name, edit, why) in enumerate(edits):
        report_id, directory = _saved(running, task, f"print({name!r}, {n})")
        edit(directory / name)
        code, text = _record(running, brief, task, report_id)
        assert code == 1 and why in text, (name, text)
        assert not (Path(os.environ["AISQUARE_HOME"]) / "reports" / report_id).exists()


def test_a_report_that_was_never_sealed_is_refused(
    running: Path,  # noqa: F811
    tree: Path,
) -> None:
    """A report the sandbox wrote or copied in: no seal, so the broker never saw it end."""
    brief, task = _linked()
    report_id, directory = _saved(running, task)
    (running / store_broker.SEALS / report_id).unlink()
    other = "ab" * 16
    shutil.copytree(directory, running / store_broker.REPORTS / other)
    text_ = (running / store_broker.REPORTS / other / "report.json").read_text()
    (running / store_broker.REPORTS / other / "report.json").write_text(
        text_.replace(report_id, other)
    )

    for wanted in (report_id, other):
        code, text = _record(running, brief, task, wanted)
        assert code == 1 and "was not sealed" in text, text


def test_a_second_seal_of_one_report_is_refused(
    running: Path,  # noqa: F811
    tree: Path,
) -> None:
    """Sealed once: the seal is what the run left, never what the sandbox made of it since."""
    _, task = _linked()
    report_id, _ = _saved(running, task)

    code, text = _answered(running, ["reports", "seal", report_id])

    assert code == 1 and "sealed already" in text


@pytest.mark.parametrize(
    "ref",
    ["../../etc/passwd", "../" + "ab" * 16, "ab" * 16 + "/../" + "cd" * 16, "short", "AB" * 16, ""],
)
def test_a_report_named_by_a_path_is_refused(
    ref: str,
    running: Path,  # noqa: F811
    tree: Path,
) -> None:
    brief, task = _linked()

    code, text = _record(running, brief, task, ref)

    assert (code == 1 and "32-character" in text) or "no readable command report" in text, text


def _forge(running: Path, task: str, **fields: object) -> str:  # noqa: F811
    """A report the sandbox wrote by hand, from a real one with ``fields`` changed, and the
    seal the broker would give it were it asked: the checks must not depend on the seal."""
    _, directory = _saved(running, task)
    forged = "cd" * 16
    target = running / store_broker.REPORTS / forged
    shutil.copytree(directory, target)
    report = reports.CommandReport.model_validate_json((target / "report.json").read_text())
    report = report.model_copy(update={"id": forged, **fields})
    (target / "report.json").write_text(report.model_dump_json(indent=2))
    return forged


@pytest.mark.parametrize(
    ("fields", "why"),
    [
        ({"source_root": "/etc"}, "it fingerprinted"),
        ({"cwd": "/tmp"}, "it ran in"),
        ({"project_id": "prj_0000000000000000"}, "not this agent's"),
    ],
    ids=["source-root", "cwd", "board"],
)
def test_a_report_outside_the_launchs_tree_or_board_is_refused_at_its_seal(
    fields: dict[str, object],
    why: str,
    running: Path,  # noqa: F811
    tree: Path,
) -> None:
    _, task = _linked()
    forged = _forge(running, task, **fields)

    code, text = _answered(running, ["reports", "seal", forged])

    assert code == 1 and why in text, text
    assert not (running / store_broker.SEALS / forged).exists()


def test_a_report_that_is_not_owner_only_is_refused(
    running: Path,  # noqa: F811
    tree: Path,
) -> None:
    _, task = _linked()
    forged = _forge(running, task)
    target = running / store_broker.REPORTS / forged
    os.chmod(target / "stdout.bin", 0o644)

    code, text = _answered(running, ["reports", "seal", forged])
    assert code == 1 and "stdout.bin is not owner-only" in text, text

    os.chmod(target / "stdout.bin", 0o600)
    os.chmod(target, 0o755)
    code, text = _answered(running, ["reports", "seal", forged])
    assert code == 1 and "the report's directory is not owner-only" in text, text


def test_a_report_holding_a_link_a_pipe_or_another_file_is_refused(
    running: Path,  # noqa: F811
    tree: Path,
    tmp_path: Path,
) -> None:
    _, task = _linked()
    forged = _forge(running, task)
    target = running / store_broker.REPORTS / forged
    secret = tmp_path / "secret"
    secret.write_text("not a report")

    (target / "extra.sh").write_text("echo hi")
    code, text = _answered(running, ["reports", "seal", forged])
    assert code == 1 and "extra.sh, which no report does" in text, text
    (target / "extra.sh").unlink()

    original = (target / "stderr.bin").read_bytes()
    (target / "stderr.bin").unlink()
    (target / "stderr.bin").symlink_to(secret)
    code, text = _answered(running, ["reports", "seal", forged])
    assert code == 1 and "stderr.bin cannot be opened" in text, text
    (target / "stderr.bin").unlink()

    os.mkfifo(target / "stderr.bin", 0o600)
    code, text = _answered(running, ["reports", "seal", forged])  # must not block on the pipe
    assert code == 1 and "stderr.bin is not a regular file" in text, text
    (target / "stderr.bin").unlink()
    (target / "stderr.bin").write_bytes(original)
    os.chmod(target / "stderr.bin", 0o600)

    moved = running / store_broker.REPORTS / ("ef" * 16)
    link = running / store_broker.REPORTS / ("12" * 16)
    shutil.move(str(target), str(moved))
    link.symlink_to(moved)
    code, text = _answered(running, ["reports", "seal", "12" * 16])
    assert (
        code == 1
        and "no report" in text
        and not (running / store_broker.SEALS / ("12" * 16)).exists()
    )


def test_a_report_with_missing_or_misreported_files_is_refused(
    running: Path,  # noqa: F811
    tree: Path,
) -> None:
    _, task = _linked()
    forged = _forge(running, task)
    target = running / store_broker.REPORTS / forged
    kept = (target / "stdout.bin").read_bytes()

    (target / "stdout.bin").write_bytes(kept + b"more")
    code, text = _answered(running, ["reports", "seal", forged])
    assert code == 1 and "stdout.bin is not the" in text, text

    (target / "stdout.bin").write_bytes(kept)
    (target / "stderr.display").unlink()
    code, text = _answered(running, ["reports", "seal", forged])
    assert code == 1 and "lacks stderr.display" in text, text


def test_a_report_the_home_holds_already_is_not_copied_over(
    running: Path,  # noqa: F811
    tree: Path,
) -> None:
    """Adoption writes a report that is not there and never one that is: a name the sandbox
    picks cannot replace a report the home keeps."""
    brief, task = _linked()
    report_id, directory = _saved(running, task)
    (running / store_broker.SEALS / report_id).unlink()  # the sandbox's copy is not even sealed
    home = Path(os.environ["AISQUARE_HOME"]) / "reports" / report_id
    shutil.copytree(directory, home)
    (home / "stdout.bin").write_bytes(b"the home's own\n")
    (home / "stdout.display").write_bytes(b"the home's own\n")
    report = json.loads((home / "report.json").read_text())
    report["stdout"] = {"observed_bytes": 15, "retained_bytes": 15, "truncated": False}
    report["display_stdout_bytes"] = 15
    (home / "report.json").write_text(json.dumps(report))

    assert reports.adopt_sandboxed(report_id) is False  # not a broker's command: nothing to do
    code, _ = _record(running, brief, task, report_id)

    assert code == 0
    assert (home / "stdout.bin").read_bytes() == b"the home's own\n"


# --- the paths a brief command reads ---------------------------------------------------


def test_an_artifact_outside_the_launchs_tree_is_not_read(
    running: Path,  # noqa: F811
    tree: Path,
    tmp_path: Path,
) -> None:
    """The broker reads as the user, outside the sandbox, and keeps a copy in the store, which
    the sandbox reads: a path outside the launch's tree is no artifact."""
    brief, task = _linked()
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("not this launch's")
    inside = tree / "shot.txt"
    inside.write_text("a screenshot")

    def record(artifact: Path) -> tuple[int, str]:
        return _answered(
            running,
            ["brief", "evidence", brief, "R1", "--task", task, "--verdict", "pass",
             "--summary", "looked", "--artifact", str(artifact), "--as", MINE[:8]],
        )  # fmt: skip

    refused, text = record(outside)
    assert refused == 1 and "outside this agent's tree" in text
    assert briefs.show(brief).brief.evidence == []
    code, text = record(inside)
    assert code == 0 and "recorded R1 pass [manual]" in text, text
    assert "kept: this" not in text


def test_a_report_whose_json_names_another_report_is_refused(
    running: Path,  # noqa: F811
    tree: Path,
) -> None:
    _, task = _linked()
    report_id, directory = _saved(running, task)
    other = running / store_broker.REPORTS / ("ee" * 16)
    shutil.copytree(directory, other)  # a copy of a report, under a name that is not its own

    code, text = _answered(running, ["reports", "seal", "ee" * 16])

    assert code == 1 and "its report.json names another report" in text, text
    assert report_id != "ee" * 16


def test_every_report_the_broker_refuses_leaves_a_record_in_its_log(
    running: Path,  # noqa: F811
    tree: Path,
) -> None:
    """Whoever asks why a sandboxed agent's evidence did not land reads it in the brokers'
    log: the report, whether the seal or the adoption refused it, and why."""
    from aisquare.core import paths

    brief, task = _linked()
    report_id, directory = _saved(running, task)
    (directory / "stdout.bin").write_bytes(b"X" + (directory / "stdout.bin").read_bytes()[1:])
    forged = _forge(running, task, source_root="/etc")

    assert _record(running, brief, task, report_id)[0] == 1
    assert _answered(running, ["reports", "seal", forged])[0] == 1

    log = (paths.log_dir() / store_broker.LOG_NAME).read_text(encoding="utf-8").splitlines()
    lines = [line for line in log if " report: " in line]
    assert any(
        f"the adoption of report {report_id[:12]} refused" in line
        and "changed after its run" in line
        for line in lines
    ), lines
    assert any(
        f"the seal of report {forged[:12]} refused" in line and "it fingerprinted" in line
        for line in lines
    ), lines
