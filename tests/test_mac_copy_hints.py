"""The help screens name the copy and paste keys a Mac has, and leave every other
platform's wording as it was.

macOS users read "v  c · copy it" and "copied on release" as ⌘C, which Terminal.app
and iTerm2 keep as their own Copy: it never reaches asq. When the user types on a Mac
(``core.terminal.mac_keyboard``: a Mac's own terminal, or a local session on a Mac) the
board's copy row names ``c``, and the fleet's help lists ctrl+c, ⌘V and the terminal's
own selection (Option in iTerm2, Fn in Terminal.app) after ``drag``. docs/fleet.md,
"Copy and paste on macOS", says the same.
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


@pytest.mark.parametrize(
    ("platform", "env", "mac"),
    [
        ("darwin", {}, True),
        ("darwin", {"TERM": "xterm-kitty"}, True),  # kitty on the Mac itself
        ("linux", {}, False),
        # A Mac user SSHed into Linux: iTerm2 names itself across ssh by LC_TERMINAL.
        ("linux", {"SSH_CONNECTION": "10.0.0.2 1 10.0.0.1 22", "LC_TERMINAL": "iTerm2"}, True),
        ("linux", {"TERM_PROGRAM": "Apple_Terminal"}, True),
        # A Linux or Windows client SSHed into a Mac types on its own keyboard.
        ("darwin", {"SSH_CONNECTION": "10.0.0.2 1 10.0.0.1 22", "TERM": "xterm-ghostty"}, False),
        ("darwin", {"SSH_TTY": "/dev/ttys003"}, False),
        # Terminal.app over SSH names itself to nothing: the plain help.
        ("linux", {"SSH_CONNECTION": "10.0.0.2 1 10.0.0.1 22", "TERM": "xterm-256color"}, False),
    ],
    ids=[
        "mac-local",
        "mac-local-kitty",
        "linux-local",
        "iterm2-into-linux",
        "terminal-app-on-linux-env",
        "ghostty-into-a-mac",
        "ssh-into-a-mac",
        "terminal-app-over-ssh",
    ],
)
def test_the_hints_follow_the_users_keyboard_not_the_hosts_os(
    platform: str, env: dict[str, str], mac: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gated on the HOST's ``sys.platform``, a Mac user SSHed into Linux (where OSC 52 is
    the only route, and the toast tells them to Option-drag) never saw the hints, and a
    Linux client SSHed into a Mac saw ⌘ and ⌥ (review of #266, round 1)."""
    from aisquare.core.terminal import mac_keyboard

    monkeypatch.setattr(sys, "platform", platform)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert mac_keyboard() is mac
    assert (MAC_COPY_KEY in board_keys()) is mac
    rows = _rows(HelpScreen("f12").keys_text().plain)
    assert ("⌘V paste into the focused pane or field" in rows) is mac
