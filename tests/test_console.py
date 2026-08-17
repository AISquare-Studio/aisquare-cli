"""Console factories — stream encoding for redirected Windows output."""

from __future__ import annotations

import io

import pytest

from aisquare.core import console

# The symbols `doctor` prints; none of them survive cp1252.
SYMBOLS = "✓ ⚠ ✗ →"


def _cp1252_stream() -> io.TextIOWrapper:
    """A stream like a redirected Windows pipe: ANSI codepage, not UTF-8."""
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252")


def test_a_cp1252_stream_cannot_carry_the_output_symbols() -> None:
    """The failure this fix exists to prevent, pinned so it stays real."""
    stream = _cp1252_stream()
    with pytest.raises(UnicodeEncodeError):
        stream.write(SYMBOLS)
        stream.flush()


def test_redirected_windows_stream_is_promoted_to_utf8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("aisquare.core.console.sys.platform", "win32")
    stream = _cp1252_stream()

    console._ensure_utf8(stream)

    assert (stream.encoding or "").lower().replace("-", "") == "utf8"
    stream.write(SYMBOLS)  # would raise UnicodeEncodeError before the fix
    stream.flush()


def test_a_utf8_windows_stream_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Attached to a console the stream is already UTF-8 — don't touch it."""
    monkeypatch.setattr("aisquare.core.console.sys.platform", "win32")
    stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")

    console._ensure_utf8(stream)

    assert (stream.encoding or "").lower().replace("-", "") == "utf8"


def test_posix_streams_are_never_reconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """POSIX terminals honour the locale; reconfiguring would override it."""
    monkeypatch.setattr("aisquare.core.console.sys.platform", "linux")
    stream = _cp1252_stream()

    console._ensure_utf8(stream)

    assert (stream.encoding or "").lower() == "cp1252"


def test_a_non_wrapper_stream_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Captured/replaced stdout (pytest, Typer's runner) has no reconfigure."""
    monkeypatch.setattr("aisquare.core.console.sys.platform", "win32")

    console._ensure_utf8(io.StringIO())  # must not raise
