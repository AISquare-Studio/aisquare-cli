"""``core.process_table``: one ``ps -A`` read, and the tree under a pid summed.

Every table here is ``ps`` text written by hand, in the shapes macOS and procps print it; one
test reads the real ``ps`` for this process alone.
"""

from __future__ import annotations

import os
import sys

import pytest

from aisquare.core import process_table
from aisquare.core.process_table import Proc, Table, TreeUse

#: An agent's pane: claude (500) → node MCP server (501), zsh (502) → pytest (503) → its
#: worker (504); and two strangers (1: launchd, 900: someone else's shell).
PS = """\
    1     0   4.2  24464 02-12:20:20
  500     1  12.5 512000    1:02:03
  501   500   1.0  80000      05:09
  502   500   0.0   4000      04:00
  503   502  85.0 300000      00:40
  504   503  40.5 150000      00:39
  900     1   3.0  10000   10-00:00:00
"""


def test_etime_reads_every_shape_ps_writes() -> None:
    assert process_table.parse_etime("05:09") == 309
    assert process_table.parse_etime("1:02:03") == 3723
    assert process_table.parse_etime("02-12:20:20") == 2 * 86400 + 12 * 3600 + 20 * 60 + 20
    assert process_table.parse_etime("  00:00 ") == 0
    for bad in ("", "5", "a:b", "x-01:00", "1:2:3:4"):
        assert process_table.parse_etime(bad) is None, bad


def test_a_tree_sums_cpu_memory_and_processes_under_its_root() -> None:
    table = process_table.parse(PS)

    assert table.tree(500) == TreeUse(cpu=139.0, rss_kb=1_046_000, processes=5, uptime=3723)
    # a subtree is a tree too: the pytest and its worker
    assert table.tree(503) == TreeUse(cpu=125.5, rss_kb=450_000, processes=2, uptime=40)
    assert table.tree(504) == TreeUse(cpu=40.5, rss_kb=150_000, processes=1, uptime=39)


def test_a_pid_not_in_the_table_is_no_tree() -> None:
    """The pane's process exited between the tmux read and the ps read."""
    assert process_table.parse(PS).tree(777) is None


def test_the_readers_own_subtree_is_skipped() -> None:
    """An agent running ``fleet top`` is not charged for the aisquare and ps it started."""
    table = process_table.parse(PS)

    assert table.tree(500, skip=503) == TreeUse(cpu=13.5, rss_kb=596_000, processes=3, uptime=3723)
    assert table.tree(500, skip=500) == table.tree(500), "the root itself is never skipped"


def test_a_child_whose_parent_exited_before_the_read_is_not_counted() -> None:
    """``ps`` lists the orphan under launchd (1): the table is one instant, never patched."""
    orphaned = PS.replace("  504   503", "  504     1").replace(
        "  503   502  85.0 300000      00:40\n", ""
    )
    table = process_table.parse(orphaned)

    assert table.tree(500) == TreeUse(cpu=13.5, rss_kb=596_000, processes=3, uptime=3723)


def test_a_cycle_of_recycled_pids_does_not_loop_the_walk() -> None:
    table = Table.of(
        [Proc(10, 1, 1.0, 100, 5), Proc(11, 12, 2.0, 200, 5), Proc(12, 11, 3.0, 300, 5)]
    )
    table.children.setdefault(10, []).append(11)

    tree = table.tree(10)

    assert tree is not None and tree.processes == 3


def test_odd_lines_cost_their_process_and_nothing_else() -> None:
    text = PS + "garbage\n  950   1  x.y 100 00:01\n  951 1 2,5 100 00:02\n  952 1 1.0 100\n"
    table = process_table.parse(text)

    assert set(table.procs) == {1, 500, 501, 502, 503, 504, 900, 951}
    assert table.procs[951].cpu == 2.5, "a comma as the decimal point reads as a point"


def test_a_reader_that_reads_nothing_is_no_table() -> None:
    assert process_table.read(lambda: None) is None
    empty = process_table.read(lambda: "")
    assert empty is not None and empty.procs == {}


@pytest.mark.skipif(sys.platform == "win32", reason="no ps on Windows: the fleet has no tmux")
def test_the_real_ps_lists_this_process_under_its_parent() -> None:
    argv = process_table.ps_argv()
    assert argv is not None and argv[1:] == ["-A", "-o", "pid=,ppid=,pcpu=,rss=,etime="]

    table = process_table.read()

    assert table is not None
    me = table.procs.get(os.getpid())
    assert me is not None and me.ppid == os.getppid() and me.rss_kb > 0
    assert me.elapsed is not None and me.elapsed >= 0


def test_windows_has_no_ps_to_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    assert process_table.ps_argv() is None
    assert process_table.ps_text() is None
