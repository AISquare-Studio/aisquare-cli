"""The findings-loop page names fields and routes; those names are pinned.

The page tells the planner to drive its cycle from ``joins.jsonl`` — read
``started_at`` as the cursor, resolve the Run by ``pipeline_id``, and quote the
``session_id`` in the task it files. Those are not illustrations, they are the
loop's interface, and a page that names a field the writer stopped emitting
reads perfectly while being wrong.

So the mechanical claims are pinned against ``record_join``'s actual output,
and the prose deliberately is not: pinning wording makes a page harder to
improve without making it more correct. This caught a real error while it was
being written — the first draft told the planner to look for ``joined: false``
rows, a field removed when the join moved to the hook seam.
"""

from __future__ import annotations

from pathlib import Path

from aisquare.core import paths
from aisquare.services.explainability import join_records, record_join

_PAGE = Path(__file__).resolve().parents[1] / "docs" / "planner-findings-loop.md"


def _join_row(isolated_home: Path) -> dict[str, object]:
    record_join(
        session_id="board-1",
        pipeline_id="run-1",
        agent_name="aisquare-planner",
        role="planner",
    )
    (row,) = join_records()
    return row


def _documented_fields() -> set[str]:
    """The field names in the page's join-log table, and only those.

    Parsed rather than searched. A substring check would pass on a token that
    happens to appear anywhere else on the page — which it does, in the diagram
    and in the lookup warning — so it would keep passing while the table itself
    went wrong. Verified to fail when a row is renamed.
    """
    fields: set[str] = set()
    for line in _PAGE.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| `"):
            continue
        fields.add(line.split("`")[1])
    return fields


def test_the_documented_fields_are_exactly_the_fields_written(
    isolated_home: Path,
) -> None:
    """The page's table IS the loop's interface, so it must match the writer.

    Both directions matter. A field the page names and the code stopped
    writing sends the planner looking for something that is not there; a field
    the code writes and the page omits is a capability nobody knows they have.
    """
    row = _join_row(isolated_home)

    assert _documented_fields() == set(row), (
        "the join-log table and record_join have drifted: "
        f"documented={sorted(_documented_fields())} written={sorted(row)}"
    )


def test_the_page_names_no_join_field_that_was_removed(isolated_home: Path) -> None:
    """The failure this test was written for.

    ``joined`` was a real field until the join moved off the launcher and onto
    the hook, where both halves are always known and a boolean saying so
    stopped meaning anything. A page still telling the planner to filter on it
    would send the loop looking for rows that cannot exist.
    """
    row = _join_row(isolated_home)
    page = _PAGE.read_text(encoding="utf-8")

    for gone in ("`joined`", "joined: false", "joined: true"):
        assert gone not in page, f"the page names {gone}, which is not a field any more"
    assert "joined" not in row


def test_the_page_keeps_the_lookup_keyed_on_the_pipeline_id(isolated_home: Path) -> None:
    """The one instruction that is silently wrong if reversed.

    On a launch that could not be pinned the board session id and the Run key
    differ, so resolving a Run by ``session_id`` misses exactly the
    wrapper-bound and resumed sessions. The page says so; this keeps it saying
    so, and proves the two ids really can diverge.
    """
    page = _PAGE.read_text(encoding="utf-8")
    assert "with `pipeline_id`, never with `session_id`" in page

    record_join(session_id="the-agents-own-id", pipeline_id="the-run-we-minted")
    unpinned = join_records()[-1]
    assert unpinned["session_id"] != unpinned["pipeline_id"], (
        "if these could never differ the page's warning would be noise"
    )


def test_the_page_points_at_the_join_log_the_code_actually_writes(
    isolated_home: Path,
) -> None:
    """The path is an instruction to a human at 08:00; it has to be the path."""
    _join_row(isolated_home)
    page = _PAGE.read_text(encoding="utf-8")

    written = paths.explainability_joins_path()
    assert written.exists()
    assert written.name in page
    assert "explainability" in str(written.parent)


def test_the_page_tells_the_planner_to_dedupe_repeated_session_starts(
    isolated_home: Path,
) -> None:
    """The trap the cursor does not catch, so the page has to.

    The join log records session STARTS. A ``/clear`` or a resume appends a
    SECOND row for a session already triaged — and that row has a newer
    ``started_at``, so a cursor-only reader sees new work rather than a repeat
    and files the same finding as a task twice. The writer's own docstring
    says readers dedupe; this pins that the page passes that on, and proves
    the duplicate is real rather than theoretical.
    """
    record_join(session_id="board-1", pipeline_id="run-1", role="planner")
    record_join(session_id="board-1", pipeline_id="run-1", role="planner")
    rows = join_records()

    assert len(rows) == 2, "the log appends per session start — that is the premise"
    assert rows[0]["session_id"] == rows[1]["session_id"]
    assert rows[0]["pipeline_id"] == rows[1]["pipeline_id"]
    assert len({(r["session_id"], r["pipeline_id"]) for r in rows}) == 1, (
        "…and they collapse to one session under the key the page names"
    )

    page = _PAGE.read_text(encoding="utf-8")
    assert "`(session_id, pipeline_id)`" in page, "the page must name the dedupe key"
