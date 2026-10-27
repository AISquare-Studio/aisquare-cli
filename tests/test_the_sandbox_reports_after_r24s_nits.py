"""The sandbox's reports after the nits of review r24 of #113 (nits 3 to 5 of finding 12; #32).

* 3: ``brokered_call()`` is an environment flag the broker sets, not a proof of who asks. The
  refusal for a process that was not started by the broker no longer says "only a command the
  agent's broker runs", and what holds when a process sets the flag itself is pinned: it still
  cannot write a seal.
* 4: adopting a sandbox's report applied no retention, so a sandbox that saved and sealed report
  after report (each up to ~97 MiB) filled the home until some later ``exec`` pruned.
* 5: the sandbox's report directory was listed whole before the check for names no report has.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aisquare.services import command_reports as reports
from aisquare.services import store_broker
from tests.test_a_sandboxed_codex_records_evidence import (  # noqa: F401 - fixtures, by name
    _linked,
    _record,
    _saved,
    tree,
)
from tests.test_store_broker import running, work  # noqa: F401 - fixtures, by name

if sys.platform == "win32":  # the broker is POSIX's (descriptors, modes): nothing below runs
    pytest.skip("the broker is POSIX's", allow_module_level=True)

REPORT_ID = "ab" * 16


# --- 5: a directory of a million names is not read whole -----------------------------------


def test_a_report_directory_of_thousands_of_names_is_refused_after_a_few_are_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "reports"
    root.mkdir(mode=0o700)
    directory = root / REPORT_ID
    directory.mkdir(mode=0o700)
    for n in range(3_000):
        (directory / f"stray-{n}").touch()
    seen: list[str] = []
    real = os.scandir

    class Counting:
        def __init__(self, entries: Any) -> None:
            self._entries = entries

        def __enter__(self) -> Counting:
            self._entries.__enter__()
            return self

        def __exit__(self, *exc: object) -> object:
            return self._entries.__exit__(*exc)

        def __iter__(self) -> Iterator[os.DirEntry[str]]:
            return self

        def __next__(self) -> os.DirEntry[str]:
            entry: os.DirEntry[str] = next(self._entries)
            seen.append(entry.name)
            return entry

    monkeypatch.setattr(os, "scandir", lambda path: Counting(real(path)))

    def listed(*args: object, **kwargs: object) -> list[str]:
        raise AssertionError("the whole directory was listed")

    monkeypatch.setattr(os, "listdir", listed)

    with pytest.raises(reports.SandboxReportError, match="which no report does"):
        reports._read_sandboxed(root, REPORT_ID)

    assert len(seen) <= len(reports._SANDBOX_FILES) + 1, len(seen)


def test_one_stray_name_listed_after_all_of_a_reports_files_is_still_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound is one name more than a report has: stopping at the count itself would let the
    seventh, a stranger, go unseen when the filesystem lists it last."""
    root = tmp_path / "reports"
    root.mkdir(mode=0o700)
    directory = root / REPORT_ID
    directory.mkdir(mode=0o700)
    for name in (*reports._SANDBOX_FILES, "stranger"):
        (directory / name).touch(mode=0o600)
    real = os.scandir

    class StrangerLast:
        def __init__(self, path: Any) -> None:
            with real(path) as entries:
                self._entries = sorted(entries, key=lambda e: e.name == "stranger")

        def __enter__(self) -> StrangerLast:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def __iter__(self) -> Iterator[os.DirEntry[str]]:
            return iter(self._entries)

    monkeypatch.setattr(os, "scandir", StrangerLast)

    with pytest.raises(reports.SandboxReportError, match="stranger, which no report does"):
        reports._read_sandboxed(root, REPORT_ID)


def test_a_report_directory_of_exactly_a_reports_files_is_still_read_whole(
    tmp_path: Path,
) -> None:
    """The bound is the number of files a report has: the six, and a seventh is a stranger."""
    root = tmp_path / "reports"
    root.mkdir(mode=0o700)
    directory = root / REPORT_ID
    directory.mkdir(mode=0o700)
    for name in reports._REQUIRED_FILES[:-1]:  # one missing: the run did not finish
        (directory / name).touch(mode=0o600)

    with pytest.raises(reports.SandboxReportError, match="the run did not finish"):
        reports._read_sandboxed(root, REPORT_ID)


# --- 3: the flag is not proof, and what holds when it is set by hand -------------------------


def test_a_process_the_broker_did_not_start_is_told_how_a_broker_starts_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(store_broker.CALLER_ENV, raising=False)

    with pytest.raises(reports.SandboxReportError) as caught:
        reports.seal_sandboxed(REPORT_ID)

    said = str(caught.value)
    assert "this command was not started so" in said, said
    assert "only a command the agent's broker runs" not in said, said


def test_a_process_that_sets_the_brokers_flag_itself_still_cannot_write_a_seal(
    running: Path,  # noqa: F811
    tree: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag opens the door to reading a sandbox's reports and sealing one; what keeps the
    sandbox out is that the launch's ``seals/`` is a directory it cannot write."""
    _, task = _linked()
    report_id, _ = _saved(running, task)
    seals = running / store_broker.SEALS
    (seals / report_id).unlink()
    mode = seals.stat().st_mode & 0o777
    seals.chmod(0o500)  # what the sandbox's profile does to it, for this process
    monkeypatch.setenv(store_broker.CALLER_ENV, "1")
    monkeypatch.setenv(store_broker.REPORTS_ENV, str(running / store_broker.REPORTS))
    monkeypatch.setenv(store_broker.SEALS_ENV, str(seals))
    monkeypatch.setenv(store_broker.ROOTS_ENV, str(tree))
    try:
        with pytest.raises(reports.SandboxReportError, match="the seal cannot be written"):
            reports.seal_sandboxed(report_id)
    finally:
        seals.chmod(mode)
    assert not (seals / report_id).exists()


# --- 4: adopting a report applies the retention a run's own report has -----------------------


def test_adopting_a_report_prunes_the_home_as_a_run_does(
    running: Path,  # noqa: F811
    tree: Path,  # noqa: F811
) -> None:
    brief, task = _linked()
    report_id, directory = _saved(running, task)
    home = Path(os.environ["AISQUARE_HOME"]) / "reports"
    home.mkdir(parents=True, exist_ok=True)
    template = reports.CommandReport.model_validate_json((directory / "report.json").read_text())
    for n in range(reports.RETAIN_REPORTS + 6):  # reports of the home's own, with no project
        old = f"{n:032x}"
        shutil_copy = home / old
        shutil_copy.mkdir()
        for member in directory.iterdir():
            (shutil_copy / member.name).write_bytes(member.read_bytes())
        forged = template.model_copy(update={"id": old, "project_id": None, "task_id": None})
        (shutil_copy / "report.json").write_text(forged.model_dump_json(indent=2))

    def held() -> int:
        return len([p for p in home.iterdir() if reports._REPORT_ID.fullmatch(p.name)])

    assert held() == reports.RETAIN_REPORTS + 6
    code, text = _record(running, brief, task, report_id)

    assert code == 0, text
    assert held() <= reports.RETAIN_REPORTS, held()
    assert (home / report_id).is_dir(), "the report just adopted is the one kept"
