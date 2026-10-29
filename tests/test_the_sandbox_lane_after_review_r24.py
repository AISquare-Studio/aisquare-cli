"""The sandbox lane after review r24 of #113 (task #32, #21's findings 6, 7 and 9).

A sandboxed Codex's board calls run in its launch's broker, outside the sandbox, as the user:
the broker must never read or record anything outside the tree that launch was made in.

* F6: ``brief evidence --artifact`` asked :func:`outside_tree` about a name and then opened the
  name again, so a directory or the file swapped for a link in between sent the read outside
  (5 leaks in 669 flips). The file is opened below the tree with no link followed
  (:func:`store_broker.open_in_tree`) and read from that descriptor.
* F7: a relative ``cwd`` or ``source_root`` in a report was resolved against the broker's own
  directory, which is in the tree; it is refused, and a sibling that begins with the tree's
  name is pinned as outside it.

The pins finding 9 asked for (the seal's report-id check, the ``reports/`` directory's owner
check, the widening of ``latest_in_tail``) are in ``test_the_pins_review_r24_asked_for.py``.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest

from aisquare.services import briefs, store_broker
from tests.test_a_sandboxed_codex_records_evidence import (  # noqa: F401 - fixtures, by name
    _answered,
    _forge,
    _linked,
    _saved,
    tree,
)
from tests.test_store_broker import MINE, running, work  # noqa: F401 - fixtures, by name

if sys.platform == "win32":  # the broker is POSIX's (FIFOs, descriptors): nothing below runs
    pytest.skip("the broker is POSIX's", allow_module_level=True)

# Not named "secret": CodeQL reads the word as one and flags the writes of it (alerts #21, #22).
FOREIGN = "not the tree's"
OWN = "the tree's own"


@pytest.fixture
def brokered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, Path]]:
    """``tree/`` (the launch's) and ``outside/`` beside it, in the environment of a command a
    broker runs, from the tree."""
    tree_ = (tmp_path / "tree").resolve()
    outside = (tmp_path / "outside").resolve()
    tree_.mkdir()
    outside.mkdir()
    monkeypatch.setenv(store_broker.CALLER_ENV, "1")
    monkeypatch.setenv(store_broker.ROOTS_ENV, str(tree_))
    monkeypatch.chdir(tree_)
    yield tree_, outside


def _swapped_after_the_check(monkeypatch: pytest.MonkeyPatch, swap: Callable[[], None]) -> None:
    """The sandbox wins the race: ``swap()`` runs right after the placement of the path, which
    is where the name was checked and before it is opened."""
    real = store_broker._placed

    def placed(path: Path, roots: Sequence[Path]) -> tuple[Path, Path] | str:
        found = real(path, roots)
        swap()
        return found

    monkeypatch.setattr(store_broker, "_placed", placed)


# --- F6: the artifact is read from the descriptor that was checked ----------------------------


def test_a_directory_swapped_for_a_link_after_the_check_is_not_followed(
    brokered: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    tree_, outside = brokered
    (tree_ / "shots").mkdir()
    (tree_ / "shots" / "a.txt").write_text(OWN)
    (outside / "a.txt").write_text(FOREIGN)

    def swap() -> None:
        (tree_ / "shots").rename(tree_ / "shots-away")
        (tree_ / "shots").symlink_to(outside)

    _swapped_after_the_check(monkeypatch, swap)

    with pytest.raises(store_broker.OutOfScope, match="cannot be opened below"):
        briefs._read_artifact(tree_ / "shots" / "a.txt")


def test_the_file_swapped_for_a_link_after_the_check_is_not_followed(
    brokered: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    tree_, outside = brokered
    (tree_ / "a.txt").write_text(OWN)
    (outside / "a.txt").write_text(FOREIGN)

    def swap() -> None:
        (tree_ / "a.txt").unlink()
        (tree_ / "a.txt").symlink_to(outside / "a.txt")

    _swapped_after_the_check(monkeypatch, swap)

    with pytest.raises(store_broker.OutOfScope, match="cannot be opened below"):
        briefs._read_artifact(tree_ / "a.txt")


def test_the_bytes_are_read_from_the_descriptor_that_was_opened_and_not_from_the_name_again(
    brokered: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opened below the tree, and then the way in is swapped for a link: what is read is the
    file that was opened, which is the tree's own, and not what the name leads to now."""
    tree_, outside = brokered
    (tree_ / "shots").mkdir()
    (tree_ / "shots" / "a.txt").write_text(OWN)
    (outside / "a.txt").write_text(FOREIGN)
    real = store_broker.open_in_tree

    def opened_then_swapped(path: Path, environ: object = None) -> int | None:
        descriptor = real(path)
        (tree_ / "shots").rename(tree_ / "shots-away")
        (tree_ / "shots").symlink_to(outside)
        return descriptor

    monkeypatch.setattr(store_broker, "open_in_tree", opened_then_swapped)

    read = briefs._read_artifact(tree_ / "shots" / "a.txt")

    assert read.body == OWN.encode()


def test_the_file_swapped_for_a_fifo_after_the_check_is_refused_and_does_not_block(
    brokered: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review's other flip: a FIFO held a broker worker for up to 300 s."""
    tree_, _ = brokered
    target = tree_ / "a.txt"
    target.write_text(OWN)

    def swap() -> None:
        target.unlink()
        os.mkfifo(target)

    _swapped_after_the_check(monkeypatch, swap)
    outcome: list[BaseException] = []

    def read() -> None:
        try:
            briefs._read_artifact(target)
        except BaseException as exc:  # what the read said is the test
            outcome.append(exc)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(10)
    blocked = reader.is_alive()
    if blocked:  # let the blocked open go, so the thread does not outlive the test
        writer = os.open(target, os.O_WRONLY | os.O_NONBLOCK)
        os.close(writer)
        reader.join(5)
    assert not blocked, "the read waited for a writer of a FIFO"
    [error] = outcome
    assert isinstance(error, ValueError) and "not a regular file" in str(error), error


def test_a_path_that_runs_through_a_link_inside_the_tree_is_read_as_it_was(
    brokered: tuple[Path, Path],
) -> None:
    """The kernel's answer for the name comes first, as it did: a link in the tree to a file
    in the tree is a way to that file. What is refused is a link that appears afterwards."""
    tree_, _ = brokered
    (tree_ / "real").mkdir()
    (tree_ / "real" / "a.txt").write_text(OWN)
    (tree_ / "link").symlink_to(tree_ / "real")

    read = briefs._read_artifact(tree_ / "link" / "a.txt")

    assert read.body == OWN.encode()
    assert read.source == tree_ / "real" / "a.txt"


def test_a_link_in_the_tree_to_a_file_outside_it_is_refused(brokered: tuple[Path, Path]) -> None:
    tree_, outside = brokered
    (outside / "a.txt").write_text(FOREIGN)
    (tree_ / "a.txt").symlink_to(outside / "a.txt")

    with pytest.raises(store_broker.OutOfScope, match="outside this agent's tree"):
        briefs._read_artifact(tree_ / "a.txt")


def test_a_directory_is_no_artifact_and_a_command_that_is_not_brokered_reads_as_it_did(
    brokered: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    tree_, outside = brokered
    (tree_ / "dir").mkdir()
    (outside / "a.txt").write_text(FOREIGN)
    with pytest.raises(ValueError, match="not a regular file"):
        briefs._read_artifact(tree_ / "dir")

    monkeypatch.delenv(store_broker.CALLER_ENV)  # the user's own `brief evidence`
    assert briefs._read_artifact(outside / "a.txt").body == FOREIGN.encode()
    with pytest.raises(ValueError, match="not a regular file"):
        briefs._read_artifact(tree_ / "dir")


def test_flipping_a_directory_of_the_way_never_reads_the_file_outside(
    brokered: tuple[Path, Path],
) -> None:
    """The review's probe in miniature: one thread flips ``real`` between a directory and a
    link out of the tree, while the artifact under it is read over and over. Every read is the
    tree's own file or a refusal; none is the file outside."""
    tree_, outside = brokered
    (tree_ / "real").mkdir()
    (tree_ / "real" / "a.txt").write_text(OWN)
    (outside / "a.txt").write_text(FOREIGN)
    stop = threading.Event()

    def flip() -> None:
        while not stop.is_set():
            try:
                (tree_ / "real").rename(tree_ / "real-away")
                (tree_ / "real").symlink_to(outside)
                (tree_ / "real").unlink()
                (tree_ / "real-away").rename(tree_ / "real")
            except OSError:
                pass

    flipper = threading.Thread(target=flip, daemon=True)
    flipper.start()
    seen: set[str] = set()
    try:
        for _ in range(400):
            with contextlib.suppress(ValueError, store_broker.OutOfScope):
                seen.add(briefs._read_artifact(tree_ / "real" / "a.txt").body.decode())
    finally:
        stop.set()
        flipper.join(10)
    assert FOREIGN not in seen, "the file outside the tree was read"


# --- F7: a path that is not absolute, a sibling that begins with the tree's name --------------


@pytest.mark.parametrize("where", ["", ".", "..", "sub", "sub/dir", "./sub", "../tree"])
def test_a_relative_path_is_no_place_in_the_tree(brokered: tuple[Path, Path], where: str) -> None:
    """Resolved against the broker's own directory, which is in the tree, ``.`` and ``sub``
    passed; the evidence then kept the string, and ``brief check`` resolved it elsewhere."""
    tree_, _ = brokered
    (tree_ / "sub" / "dir").mkdir(parents=True)

    why = store_broker.outside_tree(Path(where))

    assert why is not None and "is not an absolute path" in why, why


def test_the_trees_own_paths_are_in_it_and_a_sibling_that_begins_with_its_name_is_not(
    brokered: tuple[Path, Path], tmp_path: Path
) -> None:
    tree_, outside = brokered
    (tree_ / "sub").mkdir()
    evil = Path(f"{tree_}-evil")
    evil.mkdir()
    (evil / "x").write_text(FOREIGN)
    (tree_ / "to-evil").symlink_to(evil)

    assert store_broker.outside_tree(tree_) is None
    assert store_broker.outside_tree(tree_ / "sub") is None
    assert store_broker.outside_tree(tree_ / "not-made-yet") is None
    for refused in (evil, evil / "x", outside, tree_ / ".." / "tree-evil", tree_ / "to-evil" / "x"):
        why = store_broker.outside_tree(refused)
        assert why is not None and "outside this agent's tree" in why, (refused, why)
    assert store_broker.outside_tree(Path("/etc/hosts")) is not None
    assert store_broker.outside_tree(Path("~")) is not None


def test_a_command_that_is_not_brokered_is_asked_nothing(
    brokered: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(store_broker.CALLER_ENV)
    assert store_broker.outside_tree(Path("sub")) is None
    assert store_broker.outside_tree(Path("/etc/hosts")) is None
    assert store_broker.open_in_tree(Path("/etc/hosts")) is None


@pytest.mark.parametrize(
    "fields",
    [
        {"cwd": ""},
        {"cwd": "."},
        {"source_root": ""},
        {"source_root": "sub"},
        {"source_root": "../tree"},
    ],
    ids=["cwd-empty", "cwd-dot", "source-root-empty", "source-root-sub", "source-root-up-down"],
)
def test_a_report_with_a_relative_cwd_or_source_root_is_refused_at_its_seal(
    fields: dict[str, object],
    running: Path,  # noqa: F811
    tree: Path,  # noqa: F811
) -> None:
    _, task = _linked()
    forged = _forge(running, task, **fields)

    code, text = _answered(running, ["reports", "seal", forged])

    assert code == 1 and "is not an absolute path" in text, text
    assert not (running / store_broker.SEALS / forged).exists()


def test_an_artifact_in_a_sibling_of_the_tree_or_behind_a_link_is_refused_for_evidence_and_finding(
    running: Path,  # noqa: F811
    tree: Path,  # noqa: F811
) -> None:
    brief, task = _linked()
    evil = Path(f"{tree}-evil")
    evil.mkdir()
    (evil / "shot.txt").write_text(FOREIGN)
    (tree / "link.txt").symlink_to(evil / "shot.txt")

    for artifact in (evil / "shot.txt", tree / "link.txt"):
        for argv in (
            ["brief", "evidence", brief, "R1", "--task", task, "--verdict", "pass",
             "--summary", "looked", "--artifact", str(artifact), "--as", MINE[:8]],
            ["brief", "finding", brief, "R1", "--task", task,
             "--summary", "broken", "--artifact", str(artifact), "--as", MINE[:8]],
        ):  # fmt: skip
            code, text = _answered(running, argv)
            assert code == 1 and "outside this agent's tree" in text, (argv[1], artifact, text)
    assert briefs.show(brief).brief.evidence == []
