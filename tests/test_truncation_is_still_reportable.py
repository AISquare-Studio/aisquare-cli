"""The one moment truncation is knowable must outlive the process that saw it.

``open_store`` announces a zero-length store on stderr and carries on. That is
correct and it is not enough: the announcement goes to whatever process happened
to open the file first, which on a working machine is a HOOK. Measured — a
`session-start` hook against a truncated store exits 0, writes the team block to
stdout, and puts the warning on its own stderr, where neither the agent nor the
operator will see it. One line later the schema is back and the evidence is gone
forever.

That leaves the runbook teaching a tell it cannot confirm: "if the board is
suddenly empty and `doctor` is green, the file was truncated". Doctor IS green,
and has no way to know why — it opens a perfectly valid empty database.

So the fact is recorded where it survives the process that learned it, and
`doctor` reports it. Nothing is repaired and nothing is refused: this is the
same tell-do-not-do line as the announcement it backs, moved somewhere an
operator actually looks.

WHAT THIS IS NOT: a health check on the store's contents. An empty board is
perfectly legitimate — a new machine has one. The claim is narrower and is the
only one the evidence supports: *this file existed, was emptied, and was rebuilt
at this time*.
"""

from __future__ import annotations

from pathlib import Path

from aisquare.core import paths
from aisquare.core.store import open_store
from aisquare.models import CheckStatus
from aisquare.services import diagnostics


def _used_machine() -> Path:
    open_store().close()
    database = paths.db_path()
    assert database.stat().st_size > 0, "the fixture produced no store"
    return database


def test_the_truncation_is_recorded_where_it_outlives_the_process(isolated_home: Path) -> None:
    """THE defect: today the only record is one line of someone's stderr."""
    _used_machine().write_bytes(b"")

    open_store().close()

    assert paths.truncation_marker_path().exists(), (
        "nothing recorded that the store was rebuilt from an emptied file, so "
        "the next command cannot tell an emptied board from a new machine"
    )


def test_doctor_reports_it_afterwards(isolated_home: Path) -> None:
    """The point of recording it: the operator asks doctor, not the hook's stderr."""
    _used_machine().write_bytes(b"")
    open_store().close()

    check = next(c for c in diagnostics.doctor() if c.name == "database")

    assert check.status is CheckStatus.warn, f"doctor still reports {check.status}"
    assert "truncat" in check.detail.lower(), check.detail
    assert check.fix, "no way given to acknowledge and clear it"


def test_a_healthy_machine_records_nothing(isolated_home: Path) -> None:
    """The control, and it is what keeps this from becoming noise."""
    _used_machine()

    open_store().close()

    assert not paths.truncation_marker_path().exists()
    check = next(c for c in diagnostics.doctor() if c.name == "database")
    assert check.status is CheckStatus.ok, check.detail


def test_a_brand_new_machine_records_nothing(isolated_home: Path) -> None:
    """Absent is not truncated — the same boundary the announcement rests on."""
    assert not paths.db_path().exists()

    open_store().close()

    assert not paths.truncation_marker_path().exists()


def test_clearing_the_marker_returns_doctor_to_green(isolated_home: Path) -> None:
    """A warning an operator cannot dismiss is a warning they learn to skip."""
    _used_machine().write_bytes(b"")
    open_store().close()

    paths.truncation_marker_path().unlink()

    check = next(c for c in diagnostics.doctor() if c.name == "database")
    assert check.status is CheckStatus.ok, check.detail


def test_recording_it_never_costs_the_open(isolated_home: Path) -> None:
    """An observer may cost a trace, never the thing it observes.

    If the marker cannot be written — read-only home, full disk — the store must
    still open. Simulated by making the home unwritable for the duration.
    """
    database = _used_machine()
    database.write_bytes(b"")
    # A DIRECTORY where the marker file goes: write_text then fails with an
    # OSError while everything else stays writable. Making the whole home
    # read-only was the first attempt and it proved nothing — SQLite cannot
    # write either, so the open failed for a reason that had nothing to do with
    # the marker.
    paths.truncation_marker_path().mkdir()

    open_store().close()

    assert paths.db_path().stat().st_size > 0, "the store did not open"
