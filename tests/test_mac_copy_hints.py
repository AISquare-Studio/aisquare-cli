"""The help screens name the copy and paste keys a Mac has, and leave every other
platform's wording as it was.

macOS users read "v  c · copy it" and "copied on release" as ⌘C, which Terminal.app
and iTerm2 keep as their own Copy: it never reaches asq. On ``sys.platform ==
"darwin"`` the board's copy row names ``c``, and the fleet's help lists ctrl+c, ⌘V
and the terminal's own selection (Option in iTerm2, Fn in Terminal.app) after
``drag``. docs/fleet.md, "Copy and paste on macOS", says the same.
"""

from __future__ import annotations

import sys

import pytest

from aisquare.cli.ui.app import HelpScreen
from aisquare.cli.ui.board import BOARD_KEYS, MAC_COPY_KEY, board_help, board_keys

DRAG = "select text in a pane (double-click: a word) — copied on release"
BOARD_COPY = "freeze the feed to select text · copy it"


def _rows(text: str) -> list[str]:
    """The help's rows, each with its key column's padding squeezed to one space."""
    return [" ".join(line.split()) for line in text.splitlines()]


def test_on_macos_the_boards_copy_row_names_c_not_cmd_c(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    rows = _rows(board_help().plain)
    assert "v c freeze the feed to select text · c copies (not ⌘C)" in rows
    assert f"v c {BOARD_COPY}" not in rows
    assert [key for key, _what in board_keys()] == [key for key, _what in BOARD_KEYS]
    assert MAC_COPY_KEY in board_keys()


def test_on_linux_the_boards_keys_read_as_they_did(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert board_keys() == BOARD_KEYS
    assert ("v  c", BOARD_COPY) in BOARD_KEYS
    assert f"v c {BOARD_COPY}" in _rows(board_help().plain)
    assert "⌘" not in board_help().plain


def test_on_macos_the_fleet_help_lists_the_macs_copy_and_paste_keys_after_drag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    rows = _rows(HelpScreen("f12").keys_text().plain)
    drag = rows.index(f"drag {DRAG}")
    assert rows[drag + 1 : drag + 5] == [
        "ctrl+c copy a pane's highlight again; ⌘C stays the terminal's",
        "⌘V paste into the focused pane or field",
        "⌥ drag iTerm2: select with the terminal itself, then ⌘C",
        "fn drag Terminal.app, on most Macs: the same",
    ]
    assert "v c freeze the feed to select text · c copies (not ⌘C)" in rows


def test_on_linux_the_fleet_help_reads_as_it_did(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    text = HelpScreen("f12").keys_text().plain
    rows = _rows(text)
    drag = rows.index(f"drag {DRAG}")
    assert rows[drag + 1].startswith("divider drag the line beside the sidebar")
    assert f"v c {BOARD_COPY}" in rows
    assert not {"⌘", "⌥"} & set(text)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_every_help_row_fits_its_box(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    """The fleet's help box shows 72 columns and the board's own 66 (width less border and
    padding); a row past that wraps under the key column."""
    from rich.cells import cell_len

    monkeypatch.setattr(sys, "platform", platform)
    for key, _what in (*HelpScreen.MAC_COPY_KEYS, *board_keys()):
        assert cell_len(key) <= 11, key
    assert all(2 + 11 + 1 + cell_len(what) <= 72 for _key, what in HelpScreen.MAC_COPY_KEYS)
    assert all(2 + 11 + 1 + cell_len(what) <= 66 for _key, what in board_keys())
