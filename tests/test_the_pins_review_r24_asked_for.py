"""The pins review r24 of #113 found missing (finding 9; task #32).

The review mutated the code one change at a time and listed the guards that were right and
that nothing would notice if they were wrong: the sandbox seal's report-id check, the owner
and mode of the launch's ``reports/`` directory, and the way ``latest_in_tail`` widens its
window by four and stops at the first window that holds a request or at the whole file.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import transcripts
from aisquare.services import command_reports as reports
from aisquare.services import store_broker
from tests.test_a_sandboxed_codex_records_evidence import (  # noqa: F401 - fixtures, by name
    _answered,
    _linked,
    _saved,
    tree,
)
from tests.test_store_broker import running, work  # noqa: F401 - fixtures, by name

if sys.platform == "win32":  # the broker is POSIX's (FIFOs, descriptors): nothing below runs
    pytest.skip("the broker is POSIX's", allow_module_level=True)


# --- the seal's report-id check, the reports/ directory's owner check --------------------------


@pytest.mark.parametrize(
    "report_id",
    ["", "ab" * 15, "ab" * 17, "AB" * 16, "zz" * 16, "../" + "a" * 29, "a" * 31 + "/"],
    ids=["empty", "30-chars", "34-chars", "uppercase", "not-hex", "dots-and-slash", "slash"],
)
def test_the_seal_takes_a_complete_report_id_and_nothing_else(
    report_id: str,
    running: Path,  # noqa: F811
    tree: Path,  # noqa: F811
) -> None:
    """The id names a directory in the sandbox's ``reports/``: a name the sandbox picks is
    never joined to a path unless it is 32 lowercase hex characters (the test of the broker's
    other path says 'validated twice'; the seal has this one check)."""
    code, text = _answered(running, ["reports", "seal", report_id])

    assert code == 1 and "use the complete 32-character report ID" in text, text


def test_the_report_directory_must_be_owner_only_for_the_seal_to_read_anything_in_it(
    running: Path,  # noqa: F811
    tree: Path,  # noqa: F811
) -> None:
    """The sandbox writes in ``reports/``; if anyone else could change what lies beside its
    reports, the seal could not say they were left as the run left them."""
    _, task = _linked()
    report_id, _directory = _saved(running, task)
    directory = running / store_broker.REPORTS
    mode = stat.S_IMODE(directory.stat().st_mode)
    directory.chmod(0o755)
    try:
        code, text = _answered(running, ["reports", "seal", report_id])
    finally:
        directory.chmod(mode)

    assert code == 1 and "the launch's report directory is not owner-only" in text, text


def test_a_directory_of_another_user_or_open_to_others_is_not_owner_only(tmp_path: Path) -> None:
    here = tmp_path / "d"
    here.mkdir(mode=0o700)
    here.chmod(0o700)
    reports._owner_only(os.stat(here), os.getuid(), "d")  # the control: it is
    with pytest.raises(reports.SandboxReportError, match="not owned by the user"):
        reports._owner_only(os.stat(here), os.getuid() + 1, "d")
    here.chmod(0o750)
    with pytest.raises(reports.SandboxReportError, match="not owner-only"):
        reports._owner_only(os.stat(here), os.getuid(), "d")
    here.chmod(0o702)
    with pytest.raises(reports.SandboxReportError, match="not owner-only"):
        reports._owner_only(os.stat(here), os.getuid(), "d")


# --- latest_in_tail widens by four and stops ---------------------------------------------------


def _request(tokens: int) -> str:
    return json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": tokens}}})


def _noise(count: int) -> list[str]:
    """Entries that are no request, each about a hundred bytes."""
    return [json.dumps({"type": "user", "message": {"content": "x" * 70}}) for _ in range(count)]


def _watched(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The width of every tail ``latest_in_tail`` asks for."""
    windows: list[int] = []
    real = transcripts.tail_entries

    def spy(path: Path, tail_bytes: int, since: int = 0) -> Iterator[dict[str, Any]]:
        windows.append(tail_bytes)
        return real(path, tail_bytes, since)

    monkeypatch.setattr(transcripts, "tail_entries", spy)
    return windows


def test_the_tail_widens_fourfold_until_a_request_is_in_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One turn's tool results can outweigh the first tail: the request is further back than
    that, and than four times that, and is found."""
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join([_request(77), *_noise(60)]) + "\n")  # the request, 6 KB back
    windows = _watched(monkeypatch)

    found = transcripts.latest_in_tail(path, transcripts.request_tokens, tail_bytes=500)

    assert found == 77
    assert windows == [500, 2_000, 8_000], "four times the last, and no more once it is found"


def test_the_tail_stops_at_the_first_window_that_holds_a_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join([*_noise(60), _request(5), *_noise(2)]) + "\n")
    windows = _watched(monkeypatch)

    assert transcripts.latest_in_tail(path, transcripts.request_tokens, tail_bytes=1_000) == 5
    assert windows == [1_000], "found in the first window: nothing wider is read"


def test_the_latest_request_in_a_window_is_the_one_it_gives(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text(
        "\n".join([_request(1), *_noise(2), _request(2), _request(3), *_noise(1)]) + "\n"
    )

    assert transcripts.latest_in_tail(path, transcripts.request_tokens, tail_bytes=4_000) == 3


def test_a_transcript_with_no_request_is_read_whole_once_and_gives_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(_noise(30)) + "\n")
    size = path.stat().st_size
    windows = _watched(monkeypatch)

    assert transcripts.latest_in_tail(path, transcripts.request_tokens, tail_bytes=400) is None

    assert windows[0] == 400 and windows == sorted(windows)
    assert windows[-1] >= size and all(window < size for window in windows[:-1]), windows


def test_a_tail_as_wide_as_the_file_is_not_widened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(_noise(5)) + "\n")
    windows = _watched(monkeypatch)

    assert (
        transcripts.latest_in_tail(path, transcripts.request_tokens, tail_bytes=path.stat().st_size)
        is None
    )
    assert windows == [path.stat().st_size]
    assert (
        transcripts.latest_in_tail(tmp_path / "missing.jsonl", transcripts.request_tokens) is None
    )
