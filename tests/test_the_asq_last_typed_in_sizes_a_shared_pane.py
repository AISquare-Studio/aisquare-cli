"""Two asq showing one agent at two terminal sizes: the one its owner last typed in sizes it.

The owner runs the Mac's asq and one over SSH into the same Mac at once. Each forwarded
its own widget's size as ``resize-window``, so every attach and every resize of one undid
the other's and the agent reflowed back and forth. Input from a pane now names its asq in
the window's ``@asq-size-owner`` (``core.tmux.SIZE_OWNER_OPTION``); a pane whose window
another RUNNING asq claimed never resizes it, and says so in row 0's corner while the
sizes differ; the claim's end — that asq gone, the option unset — hands the size back.

Against the fake tmux (``tests/pane_harness.py``), each rule and its negative half. Against
a real tmux, two stand-in asq on one sandbox server — two panes of one app showing ONE
window at two sizes, the second claiming as another process (a running stand-in's pid) —
for the whole exchange: no ping-pong while both resize, the size handed over on input in
either direction, and back when the other ends.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from textual.app import ComposeResult
from textual.containers import Horizontal

from aisquare.cli.ui import terminal as terminal_module
from aisquare.cli.ui.terminal import SelectionHost, TerminalPane, sized_elsewhere
from aisquare.core.tmux import (
    BUNDLED_CONF,
    SIZE_OWNER_OPTION,
    Completed,
    SizeOwner,
    TmuxError,
    TmuxServer,
    _tmux,
)
from tests.pane_harness import FakePane, FakeTmux, click, drag
from tests.test_terminal_pane import Host, rows, run, scroll_event, synced, wait_until

_needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")


@pytest.fixture
def fake() -> FakeTmux:
    """One 80x24 pane — wider and taller than the 60x20 widget the tests show it in."""
    tmux = FakeTmux()
    tmux.panes["%1"] = FakePane(screen=["first row", "second row"], history=["old 1", "old 2"])
    return tmux


@pytest.fixture
def other_asq() -> Iterator[subprocess.Popen[bytes]]:
    """A running process standing in for the other asq: its pid is what its claim names."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        yield process
    finally:
        process.kill()
        process.wait()


def _claim(pid: int, where: str = "ssh") -> str:
    return SizeOwner(pid, "mac-mini", where, time.time_ns()).value()


def _resizes(fake: FakeTmux) -> list[tuple[str, ...]]:
    return [call for call in fake.input if call[0] == "resize-window"]


def _corner(pane: TerminalPane) -> str:
    return rows(pane)[0].text.rstrip()


def _owner_pid(value: str) -> int | None:
    owner = SizeOwner.parse(value)
    return None if owner is None else owner.pid


# --- the claim's value ---------------------------------------------------------------------


def test_a_claim_reads_back_as_written_and_nothing_else_reads_as_one() -> None:
    owner = SizeOwner(4321, "mac-mini", "ssh", 1_790_350_000_123_456_789)
    assert owner.value() == "4321@mac-mini/ssh/1790350000123456789"
    assert SizeOwner.parse(owner.value()) == owner
    assert SizeOwner.parse(f" {owner.value()}\n") == owner  # as ``show-options`` prints it
    for junk in ("", "4321", "4321@mac-mini", "x@mac-mini/ssh/1", "4321@/ssh/1", "\u0661@h/ssh/1"):
        assert SizeOwner.parse(junk) is None, junk


def test_a_claim_says_how_its_terminal_reached_the_machine() -> None:
    def where(environ: dict[str, str], platform: str) -> str:
        return SizeOwner.claiming(7, environ=environ, platform=platform).where

    assert where({"SSH_CONNECTION": "10.0.0.2 5 10.0.0.1 22"}, "darwin") == "ssh"
    assert where({"SSH_TTY": "/dev/ttys004"}, "linux") == "ssh"
    assert where({}, "darwin") == "mac"
    assert where({"SSH_CONNECTION": ""}, "linux") == "local"
    claim = SizeOwner.claiming(7, environ={}, platform="darwin")
    assert claim.pid == 7 and SizeOwner.parse(claim.value()) == claim
    later = SizeOwner.claiming(7, environ={}, platform="darwin")
    assert later.stamp > claim.stamp, "each claim's value is its own"


def test_the_hint_names_the_other_asqs_terminal_when_it_is_known() -> None:
    assert sized_elsewhere(SizeOwner(1, "h", "ssh", 1)) == "[sized by the other asq (ssh)]"
    assert sized_elsewhere(SizeOwner(1, "h", "mac", 1)) == "[sized by the other asq (Mac)]"
    assert sized_elsewhere(SizeOwner(1, "h", "local", 1)) == "[sized by the other asq]"


# --- one asq: as before --------------------------------------------------------------------


def test_with_one_asq_the_pane_sizes_its_window_as_before_and_shows_no_hint(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """The same resizes at the same times — at attach, and one per debounced resize — and
    the first key names this asq once, in one process with a resize to the size the window
    already has (the claim's own, which changes nothing there). Nothing says another asq
    sizes anything."""

    async def drive() -> tuple[list[tuple[str, ...]], list[tuple[str, ...]], str, tuple[int, int]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(60, 20)) as pilot:
            pane = host.pane
            pane.focus()
            await wait_until(pilot, lambda: synced(pane))
            at_attach = _resizes(fake)
            for key in "abc":
                await pilot.press(key)
            await pilot.pause(0.2)
            await pilot.resize_terminal(70, 22)
            await wait_until(pilot, lambda: synced(pane) and pane.content_size.width == 70)
            await pilot.pause(0.2)
            return at_attach, _resizes(fake), _corner(pane), _size(pane)

    at_attach, resizes, corner, size = run(drive())
    assert at_attach == [("resize-window", "%1", "60", "20")]
    claimed = ("resize-window", "%1", "60", "20")
    assert resizes == [*at_attach, claimed, ("resize-window", "%1", *map(str, size))], resizes
    assert [_owner_pid(value) for _, value in fake.claims] == [os.getpid()], fake.claims
    assert fake.conditional_resizes == 2, "each resize it decided is one conditional command"
    assert fake.owner_reads == 0, "and nothing asked before it"
    assert "sized by" not in corner, corner


def test_a_claim_left_by_an_asq_that_ended_is_nobodys(fake: FakeTmux, tmp_path: Path) -> None:
    """A dead pid's claim — an asq that quit or crashed — sizes nothing: attached, the pane
    resizes its window as if the option were unset, and says nothing about it."""
    ended = subprocess.Popen([sys.executable, "-c", "pass"])
    ended.wait()
    fake.panes["%1"].size_owner = _claim(ended.pid)

    async def drive() -> str:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(60, 20)) as pilot:
            await wait_until(pilot, lambda: synced(host.pane))
            return _corner(host.pane)

    corner = run(drive())
    assert _resizes(fake) == [("resize-window", "%1", "60", "20")]
    assert "sized by" not in corner, corner


# --- another asq sizes the window ----------------------------------------------------------


def test_a_window_another_running_asq_sized_is_never_resized_from_here(
    fake: FakeTmux, tmp_path: Path, other_asq: subprocess.Popen[bytes]
) -> None:
    """Not at attach, not at a resize of this widget: the pane is shown at the other asq's
    size (its last rows, cropped), and row 0's corner says who sizes it. Asked of tmux
    before the resize this pane would have sent, and nothing claimed: showing is not input."""
    fake.panes["%1"].size_owner = _claim(other_asq.pid, "ssh")

    async def drive() -> tuple[str, str]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(60, 20)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: pane.facts is not None)
            await pilot.pause(0.3)
            first = _corner(pane)
            await pilot.resize_terminal(70, 22)
            await pilot.pause(0.3)
            await pilot.resize_terminal(50, 18)
            await pilot.pause(0.3)
            return first, _corner(pane)

    first, last = run(drive())
    assert _resizes(fake) == [], _resizes(fake)
    assert (fake.panes["%1"].width, fake.panes["%1"].height) == (80, 24)
    assert first.endswith("[sized by the other asq (ssh)]"), first
    assert last.endswith("[sized by the other asq (ssh)]"), last
    assert fake.claims == []


def test_the_hint_shows_only_while_the_sizes_differ(
    fake: FakeTmux, tmp_path: Path, other_asq: subprocess.Popen[bytes]
) -> None:
    """The other asq sized the window to exactly this widget's size: nothing to explain, no
    hint. It resizes to another size: the hint comes. Named ``(Mac)`` for a Mac's own."""
    fake_pane = fake.panes["%1"]
    fake_pane.width, fake_pane.height = 60, 20
    fake_pane.size_owner = _claim(other_asq.pid, "mac")

    async def drive() -> tuple[str, str]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(60, 20)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await pilot.pause(0.2)
            same = _corner(pane)
            fake_pane.width = 100  # the other asq's terminal grew
            await wait_until(pilot, lambda: pane.facts is not None and pane.facts.width == 100)
            return same, _corner(pane)

    same, differs = run(drive())
    assert "sized by" not in same, same
    assert differs.endswith("[sized by the other asq (Mac)]"), differs
    assert _resizes(fake) == []


def test_typing_takes_the_size_at_once_then_resizes_debounce_as_before(
    fake: FakeTmux, tmp_path: Path, other_asq: subprocess.Popen[bytes]
) -> None:
    """A key in this asq names it the owner and resizes the window to this widget AT ONCE —
    no debounce between the key and the reflow the reader is looking at — and the hint
    goes. The next resize of this widget is debounced, as it always was."""
    fake.panes["%1"].size_owner = _claim(other_asq.pid)

    async def drive() -> tuple[list[tuple[str, ...]], str, list[tuple[str, ...]]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(60, 20)) as pilot:
            pane = host.pane
            pane.focus()
            await wait_until(pilot, lambda: pane.facts is not None)
            await pilot.pause(0.2)
            assert _resizes(fake) == []
            await pilot.press("x")
            at_the_key = _resizes(fake)  # no pause: the debounce has not run
            await wait_until(pilot, lambda: synced(pane))
            corner = _corner(pane)
            await pilot.resize_terminal(70, 22)
            assert _resizes(fake) == at_the_key, "a resize waits out the debounce"
            await wait_until(pilot, lambda: synced(pane) and pane.content_size.width == 70)
            return at_the_key, corner, _resizes(fake)

    at_the_key, corner, resizes = run(drive())
    assert at_the_key == [("resize-window", "%1", "60", "20")]
    assert "sized by" not in corner, corner
    assert resizes == [*at_the_key, ("resize-window", "%1", "70", "22")], resizes
    assert [_owner_pid(value) for _, value in fake.claims] == [os.getpid()]
    assert _owner_pid(fake.panes["%1"].size_owner) == os.getpid()
    assert fake.sent_text() == ["x"], "and the key itself reached the agent"


def test_a_paste_and_a_click_take_the_size_and_the_wheel_does_not(
    fake: FakeTmux, tmp_path: Path, other_asq: subprocess.Popen[bytes]
) -> None:
    """Input is what takes the size: a paste, and a click — which focuses the pane — each
    claim it. Reading is not input: a wheel over the history moves only this view."""
    fake.panes["%1"].size_owner = _claim(other_asq.pid)

    async def drive() -> tuple[int, list[int | None], list[int | None]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(60, 20)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: pane.facts is not None)
            pane.post_message(scroll_event(pane, up=True))
            await wait_until(pilot, lambda: pane.scrollback > 0)
            scrolled = pane.scrollback
            assert fake.claims == [] and _resizes(fake) == []
            await click(pilot, pane, (5, 5))
            await wait_until(pilot, lambda: synced(pane))
            by_click = [_owner_pid(value) for _, value in fake.claims]
            fake.panes["%1"].size_owner = _claim(other_asq.pid)  # the other asq types again
            fake.panes["%1"].width = 90
            await wait_until(pilot, lambda: pane.facts is not None and pane.facts.width == 90)
            pane.paste_text("pasted")
            await wait_until(pilot, lambda: synced(pane))
            return scrolled, by_click, [_owner_pid(value) for _, value in fake.claims]

    scrolled, by_click, claims = run(drive())
    assert scrolled > 0
    assert by_click == [os.getpid()]
    assert claims == [os.getpid(), os.getpid()], "the paste claimed it back"
    assert _resizes(fake) == [
        ("resize-window", "%1", "60", "20"),
        ("resize-window", "%1", "60", "20"),
    ]


def _rows_pane(fake: FakeTmux) -> FakePane:
    """The fake pane, as tall as the 60x20 widget and wider: every row on screen, the hint
    up (the sizes differ), and text to select on each row."""
    pane = fake.panes["%1"]
    pane.width, pane.height = 80, 20
    pane.screen = [f"row {n} of the agent" for n in range(20)]
    return pane


def test_a_drag_select_in_the_other_asq_takes_no_size_and_keeps_its_highlight(
    fake: FakeTmux, tmp_path: Path, other_asq: subprocess.Popen[bytes]
) -> None:
    """Selecting is reading, as the wheel is. Textual makes a ``Click`` of a drag whose press
    and release land on the pane, and that click took the size: the agent was resized under
    the highlight being made, and the reflow wiped it (tester of #463). A drag claims
    nothing, resizes nothing, and its highlight stands; a click in one cell still claims."""
    _rows_pane(fake).size_owner = _claim(other_asq.pid)

    async def drive() -> tuple[str | None, bool, list[tuple[str, str]], list[int | None]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(60, 20)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: pane.facts is not None)
            await pilot.pause(0.2)
            await drag(pilot, pane, (0, 2), (8, 3))
            await pilot.pause(0.4)  # past the debounce, and frames after it
            selected = pane.selected_text()
            standing = pane.text_selection is not None
            after_drag = list(fake.claims)
            await click(pilot, pane, (4, 6))
            await wait_until(pilot, lambda: synced(pane))
            return selected, standing, after_drag, [_owner_pid(v) for _, v in fake.claims]

    selected, standing, after_drag, after_click = run(drive())
    assert selected == "row 2 of the agent\nrow 3 of ", repr(selected)
    assert standing, "the highlight stands after the drag"
    assert after_drag == [], after_drag
    assert after_click == [os.getpid()], "a click in one cell takes the size"
    assert _resizes(fake) == [("resize-window", "%1", "60", "20")], "the click's, only"


def test_a_drag_the_program_takes_claims_no_size_either(
    fake: FakeTmux, tmp_path: Path, other_asq: subprocess.Popen[bytes]
) -> None:
    """Under a program that owns the mouse (Claude Code's fullscreen view), a drag goes to it
    as press, motion and release — its own selection, which a reflow would wipe as well. It
    reaches the program and claims nothing; a click forwarded in one cell claims."""
    pane_fake = _rows_pane(fake)
    pane_fake.alternate_on = pane_fake.mouse_on = pane_fake.mouse_sgr = True
    pane_fake.mouse_drag = True
    pane_fake.size_owner = _claim(other_asq.pid)

    async def drive() -> tuple[list[str], list[tuple[str, str]], list[int | None]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(60, 20)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: pane.facts is not None)
            await pilot.pause(0.2)
            await drag(pilot, pane, (3, 2), (9, 4))
            await pilot.pause(0.4)
            reports, after_drag = fake.sent_text(), list(fake.claims)
            await click(pilot, pane, (5, 5))
            await wait_until(pilot, lambda: synced(pane))
            return reports, after_drag, [_owner_pid(v) for _, v in fake.claims]

    reports, after_drag, after_click = run(drive())
    joined = "".join(reports)
    assert joined.startswith("\x1b[<0;4;3M") and joined.endswith("\x1b[<0;10;5m"), repr(joined)
    assert after_drag == [], after_drag
    assert after_click == [os.getpid()]
    assert _resizes(fake) == [("resize-window", "%1", "60", "20")], "the click's, only"


def test_keys_after_the_claim_claim_nothing_more(fake: FakeTmux, tmp_path: Path) -> None:
    """Written once per change of hands, never per key: tmux redraws every attached client
    at an option write."""

    async def drive() -> None:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(60, 20)) as pilot:
            host.pane.focus()
            await wait_until(pilot, lambda: synced(host.pane))
            for key in "twentykeysinarowhere":
                await pilot.press(key)
            await pilot.pause(0.2)

    run(drive())
    assert len(fake.sent()) == 20
    assert len(fake.claims) == 1, fake.claims


def test_a_claim_that_did_not_land_is_made_again_but_not_at_every_key(
    fake: FakeTmux, tmp_path: Path, other_asq: subprocess.Popen[bytes]
) -> None:
    """tmux refused the claim: the frames go on naming the other asq. The keys right after
    it do not ask again — an option write per key is what tmux redraws every client for —
    and the first one past :attr:`TerminalPane.CLAIM_GAP` does (the gap set per phase,
    so a loaded runner cannot stretch the first past it)."""
    fake.panes["%1"].size_owner = _claim(other_asq.pid)
    fake.refuse_claims = True

    async def drive() -> tuple[int, int]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(60, 20)) as pilot:
            pane = host.pane
            pane.CLAIM_GAP = 3600.0
            pane.focus()
            await wait_until(pilot, lambda: pane.facts is not None)
            for key in "abcde":
                await pilot.press(key)
            await pilot.pause(0.1)
            within = fake.claim_attempts
            pane.CLAIM_GAP = 0.0
            await pilot.press("f")
            return within, fake.claim_attempts

    within, past = run(drive())
    assert within == 1, within
    assert past == 2, past
    assert fake.claims == [] and _resizes(fake) == [], "nothing claimed, nothing resized"
    assert fake.sent_text() == list("abcdef"), "and every key reached the agent"


@pytest.mark.parametrize("how", ["ended", "unset"])
def test_the_size_comes_back_when_the_other_asq_lets_go(
    fake: FakeTmux, tmp_path: Path, other_asq: subprocess.Popen[bytes], how: str
) -> None:
    """The other asq ended, or its claim was unset: the pane that showed it at the other's
    size sizes it again — after the debounce, as a resize would — and the hint goes. No
    input needed, and nothing claimed: the window is nobody's until someone types."""
    fake.panes["%1"].size_owner = _claim(other_asq.pid)

    async def drive() -> tuple[str, str]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(60, 20)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: pane.facts is not None)
            await pilot.pause(0.3)
            before = _corner(pane)
            assert _resizes(fake) == []
            if how == "ended":
                other_asq.kill()
                other_asq.wait()
            else:
                fake.panes["%1"].size_owner = ""
            await wait_until(pilot, lambda: synced(pane), timeout=3.0)
            await pilot.pause(0.1)
            return before, _corner(pane)

    before, after = run(drive())
    assert before.endswith("[sized by the other asq (ssh)]"), before
    assert "sized by" not in after, after
    assert _resizes(fake) == [("resize-window", "%1", "60", "20")]
    assert fake.claims == []


# --- the real tmux: two stand-in asq on one server -----------------------------------------


class _TwoAsq(SelectionHost):
    """One window shown twice, side by side at two widths: ``mine`` as this asq, ``theirs``
    as another (a running stand-in's pid, :attr:`TerminalPane.owner_pid`). One app, one
    control client: what the second asq would bring of its own — a client, stamps — is not
    what is under test; whose size the window takes is."""

    CSS = """
    #mine { width: 2fr; }
    #theirs { width: 1fr; }
    """

    def __init__(self, server: TmuxServer, pane_id: str, their_pid: int) -> None:
        super().__init__()
        self._server = server
        self._pane_id = pane_id
        self._their_pid = their_pid

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield TerminalPane(self._pane_id, server=self._server, id="mine")
            theirs = TerminalPane(self._pane_id, server=self._server, id="theirs")
            theirs.owner_pid = self._their_pid
            yield theirs

    def pane(self, which: str) -> TerminalPane:
        return self.query_one(f"#{which}", TerminalPane)


Call = tuple[tuple[str, ...], Completed]
"""One tmux process a pane started: its argv, and tmux's answer."""


class _Counting:
    """The real tmux runner, every process it starts kept with its answer. ``before_resize``
    runs once, as the next resize — plain or conditional — reaches tmux and before tmux runs
    it: another asq's claim landing between a pane's last look and its resize."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.before_resize: Callable[[], None] | None = None

    def __call__(self, argv: Sequence[str], stdin: bytes | None) -> Completed:
        args = tuple(argv)
        hook = self.before_resize
        if hook is not None and any(arg.startswith("resize-window") for arg in args):
            self.before_resize = None
            hook()
        said = _tmux(argv, stdin)
        self.calls.append((args, said))
        return said


@pytest.fixture
def counting() -> _Counting:
    return _Counting()


@pytest.fixture
def live(tmp_path: Path, counting: _Counting) -> Iterator[TmuxServer]:
    """A private real server whose tmux processes are counted; control mode allowed."""
    conf = tmp_path / "tmux.conf"
    conf.write_text(BUNDLED_CONF, encoding="utf-8")
    server = TmuxServer(f"asq-test-{os.getpid()}-size", conf=conf, runner=counting, control=True)
    terminal_module.forget_server_versions()
    try:
        yield server
    finally:
        with contextlib.suppress(TmuxError):
            server.run("kill-server")
        terminal_module.forget_server_versions()


def _window_size(server: TmuxServer, pane_id: str) -> tuple[int, int]:
    """The window's size, asked with the plain runner: the counted one is the panes' own."""
    argv = server.argv("display-message", "-p", "-t", pane_id, "#{window_width} #{window_height}")
    width, height = _tmux(argv, None).stdout.split()
    return int(width), int(height)


def _resized_to(calls: list[Call]) -> list[tuple[int, int]]:
    """The sizes tmux resized the window to, in order: each plain resize that ran, and each
    conditional one it did not refuse (refused, it prints the claim instead)."""
    sizes: list[tuple[int, int]] = []
    for argv, said in calls:
        if said.returncode != 0:
            continue
        if "resize-window" in argv:
            words = list(argv)
        elif "if-shell" in argv and not said.stdout.strip():
            words = argv[-2].split()
        else:
            continue
        sizes.append((int(words[words.index("-x") + 1]), int(words[words.index("-y") + 1])))
    return sizes


def _size(pane: TerminalPane) -> tuple[int, int]:
    return pane.content_size.width, pane.content_size.height


_MINE_TERMINALS = [(110, 28), (130, 32), (100, 26), (124, 30)]
_THEIR_TERMINALS = [(116, 29), (102, 27), (128, 31), (112, 27)]
"""The sizes the app is resized through while each asq owns the window: each gives the two
panes two different widths, and each phase ENDS at a size other than the one it started
at. A phase that ended where its claim had sized the window passed with no resize at all
when a loaded loop let the debounce fold every size into that one (1 in 240 runs, at a
load average of 98): right, but not what the phase is there to show."""


@_needs_tmux
def test_real_tmux_two_asq_on_one_window_never_resize_it_back_and_forth(
    live: TmuxServer,
    tmp_path: Path,
    counting: _Counting,
    other_asq: subprocess.Popen[bytes],
) -> None:
    """Acceptance 5. The window takes the size of the asq last typed in, and only that one
    resizes it, through any number of resizes of both; the size hands over on input either
    way, at once; nothing is resized while nobody does anything; and when the other asq
    ends, this one sizes the window again."""
    pane_id = live.spawn_window(
        "asq-size", name="w", cwd=tmp_path, command=["cat"], width=100, height=30
    ).pane_id

    calls = counting.calls

    async def drive() -> dict[str, Any]:
        host = _TwoAsq(live, pane_id, other_asq.pid)
        seen: dict[str, Any] = {}
        async with host.run_test(size=(120, 30)) as pilot:
            mine, theirs = host.pane("mine"), host.pane("theirs")

            def window_is(pane: TerminalPane) -> Callable[[], bool]:
                return lambda: _window_size(live, pane_id) == _size(pane)

            def hint(pane: TerminalPane) -> Callable[[], bool]:
                return lambda: "[sized by the other asq" in _corner(pane)

            async def resize_both(
                owner: TerminalPane, other: TerminalPane, terminals: list[tuple[int, int]]
            ) -> dict[str, Any]:
                """Resize the app through ``terminals``; what reached tmux, and whose it was."""
                calls.clear()
                owner_sizes, other_sizes = set(), set()
                for terminal in terminals:
                    await pilot.resize_terminal(*terminal)
                    await pilot.pause(0.25)
                    owner_sizes.add(_size(owner))
                    other_sizes.add(_size(other))
                await wait_until(pilot, window_is(owner), 5.0)
                await wait_until(pilot, hint(other), 5.0)
                resized = _resized_to(calls)
                return {
                    "resized": resized,
                    "owner's": set(resized) <= owner_sizes,
                    "other's": set(resized) & other_sizes,
                    "owner hint": hint(owner)(),
                    "questions": [argv for argv, _ in calls if "show-options" in argv],
                    # What each resize command got back, for a failure to say what happened.
                    "sent": [
                        (
                            argv[-2] if "if-shell" in argv else " ".join(argv[5:]),
                            said.stdout.strip(),
                        )
                        for argv, said in calls
                        if any(arg.startswith("resize-window") for arg in argv)
                    ],
                    "views": {
                        pane.id: (
                            pane.facts.size_owner if pane.facts else None,
                            pane._sized_by,
                            pane._synced,
                        )
                        for pane in (owner, other)
                    },
                }

            await wait_until(pilot, lambda: mine.facts is not None and theirs.facts is not None)
            mine.focus()
            await pilot.press("m", "i", "n", "e", "enter")
            await wait_until(pilot, window_is(mine), 3.0)
            seen["mine typed"] = await resize_both(mine, theirs, _MINE_TERMINALS)

            # A drag-select in the other asq reads: no claim, no resize, the highlight stands.
            # Over text both rows of its view already show: a frame that changes the text
            # under a highlight drops it (the pane's own rule), and the burst of resizes
            # just before may leave a frame to come.
            def shows_both() -> bool:
                return [row.text[:4] for row in rows(theirs)[:2]] == ["mine", "mine"]

            await wait_until(pilot, shows_both, 3.0)
            await pilot.pause(0.6)  # an idle frame past the last resize
            calls.clear()
            await drag(pilot, theirs, (0, 1), (3, 1))
            await pilot.pause(0.5)
            seen["their drag"] = {
                "selected": theirs.selected_text() or "",
                "owner": _owner_pid(theirs.facts.size_owner if theirs.facts else ""),
                "resized": _resized_to(calls),
                "window is mine's": window_is(mine)(),
            }

            calls.clear()
            theirs.focus()
            await pilot.press("t")
            # Claimed and resized in one chain down the pipe: no process, and no debounce.
            seen["at their key"] = (_resized_to(calls), theirs._resize_timer is None)
            await wait_until(pilot, window_is(theirs), 3.0)
            seen["they typed"] = await resize_both(theirs, mine, _THEIR_TERMINALS)

            calls.clear()
            await pilot.pause(1.2)
            seen["idle"] = _resized_to(calls)

            other_asq.kill()  # the other asq ends: this one sizes the window, unasked
            other_asq.wait()
            await wait_until(pilot, window_is(mine), 3.0)
            await wait_until(pilot, lambda: not hint(mine)(), 3.0)
            seen["owner pids"] = {
                pane.id: _owner_pid(pane.facts.size_owner if pane.facts else "")
                for pane in (mine, theirs)
            }
        return seen

    seen = run(drive())
    for phase in ("mine typed", "they typed"):
        found = seen[phase]
        assert found["resized"], f"{phase}: the owner resized as it always did"
        assert found["owner's"], f"{phase}: only the owner's sizes reached tmux: {found}"
        assert not found["other's"], f"{phase}: the other asq resized it: {found}"
        assert not found["owner hint"], f"{phase}: the owner says nothing: {found}"
        assert found["questions"] == [], f"{phase}: a show-options process: {found}"
    assert seen["their drag"] == {
        "selected": "mine",
        "owner": os.getpid(),
        "resized": [],
        "window is mine's": True,
    }, seen["their drag"]
    assert seen["at their key"] == ([], True), "their key resized at once, down the pipe"
    assert seen["idle"] == [], f"resized while nobody did anything: {seen['idle']}"
    assert seen["owner pids"] == {"mine": other_asq.pid, "theirs": other_asq.pid}, (
        "the claim stays the dead asq's until input: nobody's, read as unset"
    )


@_needs_tmux
def test_real_tmux_a_claim_landing_between_a_look_and_a_resize_is_never_undone(
    live: TmuxServer,
    tmp_path: Path,
    counting: _Counting,
    other_asq: subprocess.Popen[bytes],
) -> None:
    """The ordering that failed the two-asq test 7 in 121 runs at d14c39b1, forced: this pane
    last saw nobody's claim; another asq claims the window, and sizes it to its own widget,
    as this pane's resize reaches tmux. The resize must not undo the claim — the window keeps
    the claimer's size, and this pane says who sized it. Asked first and resized on the
    answer, the resize ran after the claim and the window took this pane's size for good."""
    pane_id = live.spawn_window(
        "asq-race", name="w", cwd=tmp_path, command=["cat"], width=100, height=30
    ).pane_id
    theirs = SizeOwner(other_asq.pid, "mac-mini", "ssh", time.time_ns()).value()

    def their_claim() -> None:
        claim = ["set-option", "-w", "-t", pane_id, SIZE_OWNER_OPTION, theirs]
        resize = ["resize-window", "-t", pane_id, "-x", "50", "-y", "15"]
        assert _tmux(live.argv(*claim, ";", *resize), None).returncode == 0

    async def drive() -> tuple[tuple[int, int], str, str]:
        host = Host(live, pane_id)
        async with host.run_test(size=(100, 30)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: pane._serving() is not None and synced(pane), 5.0)
            assert pane.facts is not None and pane.facts.size_owner == "", "the premise"
            counting.before_resize = their_claim
            await pilot.resize_terminal(90, 28)
            await wait_until(pilot, lambda: counting.before_resize is None, 3.0)
            await pilot.pause(0.8)  # past the debounce, and an idle frame after it
            return _window_size(live, pane_id), pane.facts.size_owner, _corner(pane)

    window, owner, corner = run(drive())
    assert owner == theirs
    assert window == (50, 15), f"the claimer's size, not this pane's 90x28: {window}"
    assert corner.endswith("[sized by the other asq (ssh)]"), corner


def test_a_claim_landing_between_a_look_and_a_resize_is_never_undone(
    fake: FakeTmux, tmp_path: Path, other_asq: subprocess.Popen[bytes]
) -> None:
    """The same ordering against the fake tmux, which runs ``if-shell -F`` as tmux does:
    another asq's claim lands as this pane's resize reaches tmux. Refused, the window keeps
    the claimer's 50x15; nothing but the attach's resize ever ran."""
    fake_pane = fake.panes["%1"]
    theirs = _claim(other_asq.pid)

    def their_claim(pane: FakePane) -> None:
        fake.before_resize = None
        pane.size_owner = theirs
        pane.width, pane.height = 50, 15

    async def drive() -> str:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(60, 20)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            fake.before_resize = their_claim
            await pilot.resize_terminal(70, 22)
            await wait_until(pilot, lambda: fake.before_resize is None)
            await pilot.pause(0.6)  # past the debounce, and an idle frame after it
            return _corner(pane)

    corner = run(drive())
    size = (fake_pane.width, fake_pane.height)
    assert size == (50, 15), f"the claimer's size, not this pane's 70x22: {size}"
    assert fake_pane.size_owner == theirs
    assert corner.endswith("[sized by the other asq (ssh)]"), corner
    assert _resizes(fake) == [("resize-window", "%1", "60", "20")], "the attach's alone"
