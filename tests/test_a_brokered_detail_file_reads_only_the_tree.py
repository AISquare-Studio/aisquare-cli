"""A brokered ``task add --detail-file`` reads only what the sandboxed agent itself could
(review r38 of #113, finding 1).

The broker runs ``task add`` outside the sandbox, as the user, and the board keeps the
contract it read, where the sandbox reads it back (``task show``, ``board --json``):
``--detail-file ~/.codex/auth.json`` put the login on the board, on a member's too since a
parent manager delegates there (#71), past any ``deny_read`` of the sandbox's; a FIFO or
``/dev/zero`` held the broker's worker. Through the real broker (``Broker.answer``), on the
launch's own board and on a member's (``--project api``): a file outside the tree (or
nothing, a directory or an unreadable file there, told alike), a link in the tree to one
outside it, a FIFO, a directory, ``/dev/zero`` and a file over the cap are refused, saying why
with none of the file, and logged; a regular file of the tree is read as an unbrokered command
reads it. An unbrokered ``task add --detail-file`` reads any path, and refuses a directory, as
it always did.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths
from aisquare.core.store import store_session
from aisquare.services import store_broker
from tests.test_store_broker import (  # noqa: F401 - the fixture, by name
    LAUNCH,
    MINE,
    _answer,
    _broker,
    _request,
    parented,
    work,
)

if sys.platform == "win32":  # the broker is POSIX's (FIFOs, descriptors): nothing below runs
    pytest.skip("the broker is POSIX's", allow_module_level=True)

# Not named for what it stands for: CodeQL reads such a word as one and flags its writes.
FOREIGN = "what only the user outside the sandbox may read"
CONTRACT = "objective: ship it\r\nacceptance: it ships\r\nboundaries: src/\n"
#: Long enough for a refusal; a read that never ends would run the broker's 300 s, so the
#: command is ended past this (:func:`_in_time`).
WAIT = 30.0

#: The two refusals' reasons, as the broker's command says them.
OUTSIDE, NOT_REGULAR = "is outside this agent's tree", "is not a regular file"

BOARDS = pytest.mark.parametrize("member", [False, True], ids=["own board", "member's board"])


class Setting:
    """The launch's tree (``work``), a file beside it, a member board and a settled broker."""

    def __init__(self, work: Path, tmp_path: Path) -> None:  # noqa: F811
        self.work = work
        [self.member] = parented(work, "api")
        outside = tmp_path / "outside"
        outside.mkdir()
        self.outside = outside / "auth.json"
        self.outside.write_text(FOREIGN, encoding="utf-8")
        self.broker = _broker(tmp_path)
        self.broker.settle()
        self._rid = 0

    def add(self, detail_file: Path | str, *, member: bool, title: str = "x") -> dict[str, object]:
        """``task add <title> --detail-file <detail_file>`` through the broker, on the
        member's board when ``member``."""
        self._rid += 1
        argv = ["task", "add", title, "--detail-file", str(detail_file)]
        if member:
            argv += ["--project", "api"]
        return _answer(self.broker, _request(argv, self.work, rid=f"{self._rid:032x}"))

    def added(self, detail_file: Path | str, *, member: bool) -> dict[str, object]:
        """The task the add made, from its ``--json``: a new one each time."""
        self._rid += 1
        argv = ["--json", "task", "add", f"contract {self._rid}", "--detail-file", str(detail_file)]
        if member:
            argv += ["--project", "api"]
        answer = _answer(self.broker, _request(argv, self.work, rid=f"{self._rid:032x}"))
        assert answer["code"] == 0, answer
        task: dict[str, object] = json.loads(str(answer["stdout"]))
        return task


@pytest.fixture
def setting(work: Path, tmp_path: Path) -> Iterator[Setting]:  # noqa: F811
    made = Setting(work, tmp_path)
    try:
        yield made
    finally:
        made.broker.close()  # whatever it still runs


def _in_time(setting: Setting, ask: Callable[[], dict[str, object]]) -> dict[str, object]:
    """``ask()``'s answer, given within :data:`WAIT`; past it, the broker's commands are ended
    (``Broker.close``) and the test fails, so a read that never ends (a FIFO with no writer,
    ``/dev/zero`` read whole into memory) does not outlive it."""
    answers: list[dict[str, object]] = []
    asking = threading.Thread(target=lambda: answers.append(ask()), daemon=True)
    asking.start()
    asking.join(WAIT)
    held = asking.is_alive()
    if held:
        setting.broker.close()
        asking.join(WAIT)
    assert not held, "the brokered read did not end"
    [answer] = answers
    return answer


def _boards(setting: Setting) -> list[str]:
    """The launch's own board and the member's."""
    with store_session() as store:
        me = store.get_session(MINE)
    assert me is not None
    return [me.project_id, setting.member.id]


def _refusal(setting: Setting, answer: dict[str, object]) -> tuple[str, str]:
    """What a refusal said and the line it left in the brokers' log, once it is checked as
    one: exit 1, naming the option, with no byte of what lies outside, no task on either
    board, and that one line."""
    said = f"{answer['stdout']}{answer['stderr']}"
    assert answer["code"] == 1, answer
    assert "--detail-file" in said and FOREIGN not in said, said
    with store_session() as store:
        for board in _boards(setting):
            assert store.team_tasks(board) == [], board
    log = (paths.log_dir() / store_broker.LOG_NAME).read_text(encoding="utf-8")
    [line] = [line for line in log.splitlines() if " path: --detail-file " in line]
    assert f"launch={LAUNCH} " in line and FOREIGN not in line, line
    return said, line


@BOARDS
def test_a_file_outside_the_tree_is_refused(setting: Setting, member: bool) -> None:
    """The finding's own probe: the login beside the tree."""
    answer = setting.add(setting.outside, member=member)
    said, logged = _refusal(setting, answer)
    assert OUTSIDE in said and OUTSIDE in logged, (said, logged)


@BOARDS
def test_a_path_outside_the_tree_is_refused_alike_whatever_is_there(
    setting: Setting, member: bool
) -> None:
    """The refusal is no oracle of what lies outside: nothing, a directory or a file the user
    may not read is told as a file is. The option's own type checked each as the user before
    the tree was (``stat``, ``access``: "is a directory", "is not readable")."""
    unreadable = setting.outside.with_name("kept.json")
    unreadable.write_text(FOREIGN, encoding="utf-8")
    unreadable.chmod(0)
    log = paths.log_dir() / store_broker.LOG_NAME
    for named in (setting.outside.with_name("not-there.json"), setting.outside.parent, unreadable):
        answer = setting.add(named, member=member)
        said, logged = _refusal(setting, answer)
        assert OUTSIDE in said and OUTSIDE in logged, (said, logged)
        log.unlink()


@BOARDS
def test_a_directory_in_the_tree_is_no_file_to_read(setting: Setting, member: bool) -> None:
    """Refused as a FIFO is, by the broker's own rule, not the option type's."""
    (setting.work / "docs").mkdir()
    answer = setting.add("docs", member=member)
    said, logged = _refusal(setting, answer)
    assert NOT_REGULAR in said and NOT_REGULAR in logged, (said, logged)


@BOARDS
def test_a_link_in_the_tree_to_a_file_outside_it_is_refused(setting: Setting, member: bool) -> None:
    """The path names the tree; the file it reaches is not the tree's. Relative, as an agent
    in its worktree names it."""
    (setting.work / "brief.md").symlink_to(setting.outside)
    answer = setting.add("brief.md", member=member)
    said, logged = _refusal(setting, answer)
    assert OUTSIDE in said and OUTSIDE in logged, (said, logged)


@BOARDS
def test_a_fifo_in_the_tree_is_refused_and_does_not_hold_the_broker(
    setting: Setting, member: bool
) -> None:
    """A FIFO with no writer held the broker's worker until its 300 s ran out."""
    fifo = setting.work / "brief.md"
    os.mkfifo(fifo)
    answer = _in_time(setting, lambda: setting.add(fifo, member=member))
    said, logged = _refusal(setting, answer)
    assert NOT_REGULAR in said and NOT_REGULAR in logged, (said, logged)


@BOARDS
def test_dev_zero_is_refused_and_does_not_hold_the_broker(setting: Setting, member: bool) -> None:
    """A device that never ends a read, named directly and through a link in the tree."""
    answer = _in_time(setting, lambda: setting.add("/dev/zero", member=member))
    said, logged = _refusal(setting, answer)
    assert OUTSIDE in said and OUTSIDE in logged, (said, logged)
    (setting.work / "zero.md").symlink_to("/dev/zero")
    (paths.log_dir() / store_broker.LOG_NAME).unlink()
    answer = _in_time(setting, lambda: setting.add("zero.md", member=member))
    said, logged = _refusal(setting, answer)
    assert OUTSIDE in said and OUTSIDE in logged, (said, logged)


@BOARDS
def test_a_file_in_the_tree_over_the_cap_is_refused(setting: Setting, member: bool) -> None:
    """The cap is what the agent could hand its broker inline (``MAX_REQUEST``); a file one
    byte over it is refused whole, never cut."""
    big = setting.work / "big.md"
    big.write_bytes(b"objective: " + b"x" * (store_broker.MAX_DETAIL_FILE - 11 + 1))
    assert big.stat().st_size == store_broker.MAX_DETAIL_FILE + 1
    answer = setting.add(big, member=member)
    said, logged = _refusal(setting, answer)
    over = f"is over {store_broker.MAX_DETAIL_FILE} bytes"
    assert over in said and over in logged, (said, logged)


def test_the_cap_is_the_brokers_request_cap() -> None:
    """Named, and no more than the broker takes inline."""
    assert store_broker.MAX_DETAIL_FILE == store_broker.MAX_REQUEST == 1_000_000


@BOARDS
def test_a_regular_file_of_the_tree_is_read_as_an_unbrokered_add_reads_it(
    setting: Setting, member: bool
) -> None:
    """By an absolute path, a relative one, through a link that stays in the tree, and one
    exactly at the cap: the detail is the text ``read_text`` gives (its line ends too), on the
    board the add names."""
    docs = setting.work / "docs"
    docs.mkdir()
    brief = docs / "brief.md"
    brief.write_bytes(CONTRACT.encode())
    (setting.work / "link.md").symlink_to(brief)
    full = setting.work / "full.md"
    full.write_bytes(b"x" * store_broker.MAX_DETAIL_FILE)
    board = setting.member.id if member else _boards(setting)[0]

    def unbrokered(path: Path) -> object:
        result = CliRunner().invoke(
            app, ["--json", "task", "add", path.name, "--detail-file", str(path)]
        )
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)["detail"]

    expected = unbrokered(brief)
    assert expected == CONTRACT.replace("\r\n", "\n").strip(), expected
    for named in (brief, "docs/brief.md", "link.md"):
        task = setting.added(named, member=member)
        assert task["project_id"] == board, task
        assert task["detail"] == expected, (named, task["detail"])
    task = setting.added(full, member=member)
    assert task["detail"] == unbrokered(full) == "x" * store_broker.MAX_DETAIL_FILE


@BOARDS
def test_a_missing_file_in_the_tree_says_so_as_an_unbrokered_add_does(
    setting: Setting, member: bool
) -> None:
    """Not a refusal of the broker's: the tree is the agent's to read, and nothing is logged."""
    answer = setting.add(setting.work / "nope.md", member=member)
    said = f"{answer['stdout']}{answer['stderr']}"
    assert answer["code"] == 2 and "No such file or directory" in said, answer
    assert not (paths.log_dir() / store_broker.LOG_NAME).exists()


@BOARDS
def test_an_unbrokered_add_reads_any_file_as_it_always_did(setting: Setting, member: bool) -> None:
    """A human's, or an agent's whose tools run unsandboxed: no broker reads for it, and the
    path is read as it was, beside the tree and through a link out of it alike."""
    assert not store_broker.brokered_call()
    (setting.work / "brief.md").symlink_to(setting.outside)
    for n, named in enumerate((setting.outside, setting.work / "brief.md")):
        argv = ["--json", "task", "add", f"unbrokered {n}", "--detail-file", str(named)]
        if member:
            argv += ["--project", "api", "--as", MINE[:8]]
        result = CliRunner().invoke(app, argv)
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["detail"] == FOREIGN, result.output
    directory = CliRunner().invoke(app, ["task", "add", "d", "--detail-file", str(setting.work)])
    assert directory.exit_code == 2 and "is a directory" in directory.output, directory.output


# --- reopened: a `..` the walk climbed out of the tree (review r38 of #113, finding 1) --------
#
# On 3.11 and 3.12 ``Path.resolve`` raises on a link loop, and the placement fell back to the
# path as written, `..` and all: ``is_relative_to`` is lexical, so ``<tree>/A/../../outside/x``
# was in the tree. With ``A`` a link (into the tree, onto a loop) while it was resolved and a
# directory while it was walked, the walk's `..` climbed out of the tree and read the file there
# (O_NOFOLLOW stops a link, never a `..`). Three guards, each pinned and each killed alone:
# the placement never falls back (``_real``); ``read_in_tree`` hands the walk the strictly
# resolved path and refuses one that no longer resolves; the walk refuses a `..` or `.` whoever
# placed it.

#: The two ways the attack is refused: placed nowhere (3.11, 3.12), or placed and then not
#: resolved (3.13, whose ``resolve`` goes on past a loop by the name).
UNRESOLVED = "cannot be resolved"


def _loop_layout(tree: Path, outside: Path) -> tuple[Path, Path, Path]:
    """The tester's layout, all of it written in the tree: ``A``, a link deep into the tree,
    ``Aswap``, a directory to swap it with, and a loop where ``A/../..`` leads while ``A`` is
    the link. Returns ``A``, ``Aswap`` and the path that names ``outside`` through ``A``."""
    deep = tree / "D1" / "D2" / "D3"
    deep.mkdir(parents=True)
    (tree / "D1" / "outside").mkdir()
    loop = tree / "D1" / outside.parent.name / outside.name
    loop.symlink_to(loop)
    (tree / "A").symlink_to(deep)
    (tree / "Aswap").mkdir()
    named = Path(f"{tree}/A/../../{outside.relative_to(tree.parent)}")
    return tree / "A", tree / "Aswap", named


@pytest.fixture
def in_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """``tree/`` (a broker's launch's) and ``outside/auth.json`` beside it, in the environment
    of a command that broker runs, from the tree."""
    tree = (tmp_path / "tree").resolve()
    tree.mkdir()
    outside = tree.parent / "outside" / "auth.json"
    outside.parent.mkdir()
    outside.write_text(FOREIGN, encoding="utf-8")
    monkeypatch.setenv(store_broker.CALLER_ENV, "1")
    monkeypatch.setenv(store_broker.ROOTS_ENV, str(tree))
    monkeypatch.chdir(tree)
    return tree, outside


def _swapping_on_the_walk(monkeypatch: pytest.MonkeyPatch, link: Path, directory: Path) -> None:
    """The sandbox wins the race: as the walk below the tree's root opens ``link``'s name, the
    link is gone and the directory is there in its place."""
    real_open = os.open
    swapped = [False]

    def hooked(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if not swapped[0] and path == link.name and kwargs.get("dir_fd") is not None:
            swapped[0] = True
            link.unlink()
            directory.rename(link)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", hooked)


def test_a_path_that_does_not_resolve_is_placed_nowhere(
    in_tree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard 1, the placement (``_placed``, ``outside_tree``, ``Scope.holds``): where
    ``resolve`` raises, or leaves a `..`, the path is in no tree, never the path as written.
    ``resolve`` is made to do each here, as 3.11 and 3.12 do on a loop, whatever runs this."""
    tree, outside = in_tree
    _, _, named = _loop_layout(tree, outside)
    real_resolve = Path.resolve
    scope = store_broker.Scope("b", "p", (tree,))
    for answer in ("raise", "dotdot"):

        def resolve(self: Path, strict: bool = False, answer: str = answer) -> Path:
            if "/A/../" not in str(self):
                return real_resolve(self, strict=strict)
            if answer == "raise":
                raise RuntimeError(f"Symlink loop from {self}")
            return self.absolute()

        monkeypatch.setattr(Path, "resolve", resolve)
        assert store_broker._real(named) is None, answer
        assert store_broker._placed(named, [tree]) == (
            f"{named} is outside this agent's tree: its broker reads only there"
        ), answer
        assert store_broker.outside_tree(named) is not None, answer
        assert not scope.holds(named), answer
    monkeypatch.setattr(Path, "resolve", real_resolve)
    placed = store_broker._placed(named, [tree])
    # unpatched: refused (3.11, 3.12), or placed where the kernel resolves it now (3.13)
    assert isinstance(placed, str) or os.pardir not in placed[1].parts, placed


def test_the_walk_is_handed_the_path_resolved_with_no_link_and_no_dotdot(
    in_tree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard 2, ``read_in_tree``: ``open_in_tree`` walks the strictly resolved path, never the
    name as given, through a `..` or a link in the tree alike."""
    tree, _ = in_tree
    (tree / "sub").mkdir()
    (tree / "deep" / "x").mkdir(parents=True)
    (tree / "brief.md").write_text("objective: one\n", encoding="utf-8")
    (tree / "deep" / "brief.md").write_text("objective: two\n", encoding="utf-8")
    (tree / "L").symlink_to(tree / "deep" / "x")
    walked: list[Path] = []
    real_open = store_broker.open_in_tree

    def spy(path: Path, environ: Mapping[str, str] | None = None) -> int | None:
        walked.append(path)
        return real_open(path, environ)

    monkeypatch.setattr(store_broker, "open_in_tree", spy)
    one = store_broker.read_in_tree(Path("sub/../brief.md"), 100)
    two = store_broker.read_in_tree(tree / "L" / ".." / "brief.md", 100)
    assert (one, two) == (b"objective: one\n", b"objective: two\n")
    assert walked == [tree / "brief.md", tree / "deep" / "brief.md"], walked


def test_a_path_placed_in_the_tree_that_no_longer_resolves_is_refused_not_read(
    in_tree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard 2, ``read_in_tree``: placed by the old lexical rule (as a race placed it, or 3.13
    by a loop's name), a path whose strict resolution meets a loop is refused, and no `..` of
    it is walked."""
    tree, outside = in_tree
    link, directory, named = _loop_layout(tree, outside)

    def lexical(path: Path, roots: object) -> tuple[Path, Path]:
        return tree, path.absolute()

    monkeypatch.setattr(store_broker, "_placed", lexical)
    _swapping_on_the_walk(monkeypatch, link, directory)
    with pytest.raises(store_broker.OutOfScope, match=UNRESOLVED):
        store_broker.read_in_tree(named, store_broker.MAX_DETAIL_FILE)


def test_the_walk_climbs_no_dotdot_whoever_placed_the_path(
    in_tree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard 3, ``open_in_tree``: a `..` that a placement let through, as the old fallback did,
    is refused before the walk, which O_NOFOLLOW would have let climb above the tree's root."""
    tree, outside = in_tree
    (tree / "A").mkdir()
    named = Path(f"{tree}/A/../../{outside.relative_to(tree.parent)}")

    def lexical(path: Path, roots: object) -> tuple[Path, Path]:
        return tree, path.absolute()

    monkeypatch.setattr(store_broker, "_placed", lexical)
    with pytest.raises(store_broker.OutOfScope, match="is outside this agent's tree"):
        store_broker.open_in_tree(named)
    with pytest.raises(store_broker.OutOfScope, match="is outside this agent's tree"):
        store_broker.open_in_tree(Path(f"{tree}/A/./../../{outside.relative_to(tree.parent)}"))


@BOARDS
def test_a_link_swapped_for_a_directory_mid_walk_never_leads_out(
    setting: Setting, member: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reopen's attack, deterministic: the command as the broker runs it (its environment),
    with ``A`` swapped for a directory as the walk opens it. Refused, on the board the add names
    and the other alike, with none of the file."""
    link, directory, named = _loop_layout(setting.work, setting.outside)
    scope = setting.broker.scope()
    env = {
        store_broker.CALLER_ENV: "1",
        store_broker.BOARD_ENV: scope.board,
        store_broker.POOL_ENV: scope.pool,
        store_broker.MEMBERS_ENV: ",".join(scope.members),
        store_broker.ROOTS_ENV: os.pathsep.join(str(root) for root in scope.roots),
    }
    argv = ["task", "add", "escape", "--detail-file", str(named)]
    if member:
        argv += ["--project", "api"]
    _swapping_on_the_walk(monkeypatch, link, directory)
    result = CliRunner().invoke(app, argv, env=env)
    said = result.output
    assert result.exit_code == 1, said
    assert OUTSIDE in said or UNRESOLVED in said, said
    assert FOREIGN not in said, said
    with store_session() as store:
        for board in _boards(setting):
            assert store.team_tasks(board) == [], board


#: Swaps two names atomically, over and over: macOS's ``renamex_np(RENAME_SWAP)``, Linux's
#: ``renameat2(RENAME_EXCHANGE)``; both flags are 2.
SWAPPER = r"""
import ctypes, sys
libc = ctypes.CDLL(None, use_errno=True)
a, b = sys.argv[1].encode(), sys.argv[2].encode()
if sys.platform == "darwin":
    swap = lambda: libc.renamex_np(a, b, 2)
else:
    swap = lambda: libc.renameat2(-100, a, -100, b, 2)
while True:
    swap()
"""


def _can_swap(tmp_path: Path) -> bool:
    """Whether this system swaps two names atomically (the race needs it)."""
    import ctypes

    one, two = tmp_path / "swap-one", tmp_path / "swap-two"
    one.mkdir()
    two.symlink_to(one)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if sys.platform == "darwin":
            done = libc.renamex_np(bytes(one), bytes(two), 2)
        else:
            done = libc.renameat2(-100, bytes(one), -100, bytes(two), 2)
    except (AttributeError, OSError):
        return False
    return done == 0 and one.is_symlink()


#: Bounds of the race through the real broker: the tester's leaks came at attempts 1 to 47.
RACE_TRIES, RACE_SECONDS = 50, 30.0


@BOARDS
def test_the_swap_raced_through_the_broker_never_leads_out(
    setting: Setting, member: bool, tmp_path: Path
) -> None:
    """The reopen's attack as the sandbox runs it: a process of its own swaps ``A`` with a
    directory while the add is asked again and again through ``Broker.answer``. Every answer is
    a refusal, and neither board holds a byte of the file, for as many tries as the bounds give."""
    if not _can_swap(tmp_path):
        pytest.skip("no atomic swap of two names here (renamex_np, renameat2)")
    link, directory, named = _loop_layout(setting.work, setting.outside)
    swapper = subprocess.Popen([sys.executable, "-c", SWAPPER, str(link), str(directory)])
    answers: list[dict[str, object]] = []
    deadline = time.monotonic() + RACE_SECONDS
    try:
        while len(answers) < RACE_TRIES and time.monotonic() < deadline:
            answers.append(setting.add(named, member=member, title=f"race {len(answers)}"))
    finally:
        swapper.terminate()
        swapper.wait(10)
    assert answers, "the race asked nothing"
    for answer in answers:
        said = f"{answer['stdout']}{answer['stderr']}"
        assert answer["code"] not in (0, None) and FOREIGN not in said, answer
    with store_session() as store:
        for board in _boards(setting):
            assert store.team_tasks(board) == [], board
