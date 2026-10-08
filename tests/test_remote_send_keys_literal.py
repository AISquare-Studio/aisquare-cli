"""Typed text reaches the pane byte for byte — the space bar, in particular.

The human: "space not working". Two faults, one of them mine and the cause:

* ``_optional_ref`` was used to read typed text, and it answers "absent" for a
  string that strips to nothing. A 50 ms flush of ``" "`` therefore became
  ``None`` and delivered nothing, while the endpoint answered 200 ``sent: true``
  and the audit line recorded ``text=0ch`` — the trail honestly reporting that
  no text was sent, the loss having happened before it reached it.
* Literal text went through ``send-keys -l --``, where tmux's own argument
  parser reads the string before the pane does. That parser is version-dependent
  (``_data_arg`` exists because ``-l -- ';'`` sends nothing on 3.7c), and every
  character typed from the phone crossed it.

These drive a REAL tmux pane, because the question is what tmux actually
delivers, and a fake would only confirm what we believe about it. Each case
appends a ``|`` sentinel so a trailing space becomes interior and cannot be
hidden by any trimming in the capture.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import shutil
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from aisquare.core.tmux import CHECK_SOCKET_SUFFIX, TmuxError, TmuxServer
from aisquare.services.remote_server import RequestError, _literal, _optional_ref

requires_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux is not installed; the live tests need it"
)
_SOCKETS = itertools.count()


# --- reading the body: whitespace is content, not emptiness ----------------------------


def test_optional_still_treats_a_blank_name_as_absent() -> None:
    """Unchanged, and correct for a NAME: a project ref of spaces is no ref."""
    assert _optional_ref({"project": "   "}, "project") is None
    assert _optional_ref({"project": "prj_x"}, "project") == "prj_x"


def test_literal_keeps_whitespace_because_a_keystroke_is_a_keystroke() -> None:
    assert _literal({"text": " "}, "text") == " "
    assert _literal({"text": "  "}, "text") == "  "
    assert _literal({"text": "\t"}, "text") == "\t"
    assert _literal({"text": "EE "}, "text") == "EE "


def test_literal_still_reports_a_genuinely_absent_value() -> None:
    assert _literal({}, "text") is None
    assert _literal({"text": None}, "text") is None
    assert _literal({"text": ""}, "text") == "", "empty is present but has nothing to send"


def test_literal_refuses_a_value_that_is_not_text() -> None:
    """Read as absent, ``"text": 3`` with ``"enter": true`` sent the Enter alone, which
    takes a dialog's highlighted option, and answered 200 ``sent: true`` (review of #243,
    round 3)."""
    for value in (7, ["x"], True):
        with pytest.raises(RequestError) as refused:
            _literal({"text": value}, "text")
        assert (refused.value.status, refused.value.error) == (400, "invalid")


# --- the write handler -----------------------------------------------------------------


def test_a_lone_space_is_no_longer_nothing_to_do() -> None:
    """It used to raise "give text, keys or enter" — the space WAS the request."""
    from aisquare.services import remote_server

    sent: list[tuple[str, str]] = []

    class _Tmux:
        def pane_facts(self, pane_id: str) -> SimpleNamespace:
            return SimpleNamespace(dead=False, current_command="claude")

        def started_at(self) -> datetime:
            return _Agent.created_at - timedelta(hours=1)

        def send_literal(self, pane_id: str, text: str) -> None:
            sent.append(("literal", text))

        def send_keys(self, pane_id: str, *keys: str) -> None:
            sent.append(("keys", " ".join(keys)))

    class _Agent:
        pane_id = "%1"
        tmux_socket = "asq"
        created_at = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)

    class _Store:
        def fleet_agent_by_label(self, *args: object, **kwargs: object) -> _Agent:
            return _Agent()

        def __enter__(self) -> _Store:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    class _Project:
        id = "p"
        root = Path("/tmp/p")

    import aisquare.core.store as store_module
    from aisquare.services import fleet as fleet_service

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(remote_server, "_resolve_project", lambda ref: _Project())
        patch.setattr(store_module, "store_session", lambda: _Store())
        patch.setattr(fleet_service, "server_for", lambda socket, config=None: _Tmux())
        handler = remote_server.live_writes().handlers["send-keys"]
        result, summary = handler({"agent": "c", "text": " "})
    assert sent == [("literal", " ")]
    assert result["sent"] is True
    assert "text=1ch" in summary, "the audit counts the space it actually sent"


def test_an_empty_body_is_still_refused() -> None:
    from aisquare.services import remote_server

    handler = remote_server.live_writes().handlers["send-keys"]
    with pytest.raises(RequestError, match="give 'text', 'keys' or 'enter'"):
        handler({"agent": "c", "text": ""})


# --- what tmux actually delivers ---------------------------------------------------------


@pytest.fixture
def pane() -> Iterator[tuple[TmuxServer, str]]:
    """A real pane running ``cat``, so the terminal echoes exactly what arrives."""
    server = TmuxServer(f"asq-lit-{os.getpid()}-{next(_SOCKETS)}")
    try:
        window = server.spawn_window(
            "asq-lit", name="w0", cwd=Path("/tmp"), command=["cat"], width=80, height=8
        )
        time.sleep(0.4)
        yield server, window.pane_id
    finally:
        for socket in (server.socket, server.socket + CHECK_SOCKET_SUFFIX):
            with contextlib.suppress(TmuxError):
                TmuxServer(socket).kill_server()
            with contextlib.suppress(OSError):
                TmuxServer(socket).socket_path().unlink()


def _delivered(server: TmuxServer, pane_id: str, *writes: str) -> str:
    """What the pane shows after these writes, with a sentinel pinning the tail."""
    server.send_keys(pane_id, "C-u")
    time.sleep(0.15)
    for text in writes:
        server.send_literal(pane_id, text)
        time.sleep(0.10)
    server.send_literal(pane_id, "|")
    time.sleep(0.25)
    return server.capture(pane_id).lines[0].rstrip()


@requires_tmux
@pytest.mark.parametrize(
    "text",
    [
        " ",
        "EE ",
        "  ",
        "CC DD",
        "a;",
        ";",
        "héllo→",
        " leading",
        "-starts-with-a-dash",
        "quote\" and 'single",
        "$HOME `backtick` $(cmd)",
    ],
    ids=[
        "a lone space",
        "a trailing space",
        "two spaces",
        "a space inside",
        "a trailing semicolon",
        "a lone semicolon",
        "unicode",
        "a leading space",
        "a leading dash",
        "quotes",
        "shell metacharacters",
    ],
)
def test_text_arrives_byte_exact(pane: tuple[TmuxServer, str], text: str) -> None:
    server, pane_id = pane
    assert _delivered(server, pane_id, text) == f"{text}|"


@requires_tmux
def test_a_tab_is_delivered_as_the_tab_byte(pane: tuple[TmuxServer, str]) -> None:
    """Asserted on the BYTE and the cursor, not the screen: a terminal renders 09 as
    movement to the next tab stop, and whether a capture shows that as spaces or as
    a literal tab depends on the tmux version. The screen equality used for
    printable text is the wrong instrument here."""
    server, pane_id = pane
    calls: list[tuple[str, ...]] = []
    original = server.run

    def record(*args: str, stdin: bytes | None = None) -> object:
        calls.append(args)
        return original(*args, stdin=stdin)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(server, "run", record)
        server.send_literal(pane_id, "\t")
    sent = next(call for call in calls if call and call[0] == "send-keys")
    assert list(sent[sent.index("-H") + 1 :]) == ["09"]
    server.send_literal(pane_id, "|")  # a sentinel, so the moved-to column is visible
    time.sleep(0.25)
    # The CURSOR, not the cells: tmux 3.6+ keeps a tab in the grid and captures it as
    # "\t" ("Preserve tabs for copying and capture-pane"); older ones paint spaces.
    # Either way the tab moved the cursor to the next stop and the sentinel followed.
    assert server.capture(pane_id).facts.cursor_x == 9, "it moved the cursor"


@requires_tmux
def test_three_writes_keep_the_space_between_them(pane: tuple[TmuxServer, str]) -> None:
    """The reported symptom: AA, then a space, then BB landed as AABB."""
    server, pane_id = pane
    assert _delivered(server, pane_id, "AA", " ", "BB") == "AA BB|"


@requires_tmux
def test_a_coalesced_run_keeps_its_trailing_space(pane: tuple[TmuxServer, str]) -> None:
    """How it bit typing: the 50 ms flush IS "hello " and the next run follows."""
    server, pane_id = pane
    assert _delivered(server, pane_id, "hello ", "world") == "hello world|"


@requires_tmux
def test_literal_text_goes_through_the_hex_path(pane: tuple[TmuxServer, str]) -> None:
    """No argument parser sees the string — that is the point of the change."""
    server, pane_id = pane
    calls: list[tuple[str, ...]] = []
    original = server.run

    def record(*args: str, stdin: bytes | None = None) -> object:
        calls.append(args)
        return original(*args, stdin=stdin)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(server, "run", record)
        server.send_literal(pane_id, "a; b")
    sends = [call for call in calls if call and call[0] == "send-keys"]
    assert sends and "-H" in sends[0]
    assert "-l" not in sends[0]
    assert list(sends[0][sends[0].index("-H") + 1 :]) == ["61", "3b", "20", "62"]


@requires_tmux
def test_a_long_paste_is_chunked_but_arrives_whole(pane: tuple[TmuxServer, str]) -> None:
    """``-H`` is one argv element per byte, so a paste must not build one huge command."""
    from aisquare.core import tmux as tmux_module

    server, pane_id = pane
    calls: list[tuple[str, ...]] = []
    original = server.run

    def record(*args: str, stdin: bytes | None = None) -> object:
        calls.append(args)
        return original(*args, stdin=stdin)

    payload = "x" * (tmux_module._HEX_CHUNK * 2 + 5)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(server, "run", record)
        server.send_literal(pane_id, payload)
    sends = [call for call in calls if call and call[0] == "send-keys"]
    assert len(sends) == 3, "chunked rather than one enormous argv"
    assert all(len(call) - call.index("-H") - 1 <= tmux_module._HEX_CHUNK for call in sends)
    # Counted across the chunks rather than on the screen: an 80x8 pane holds 640
    # cells, so a 2053-character paste has scrolled most of itself away by the
    # time it can be captured. What matters is that no byte was dropped at a seam.
    delivered = [byte for call in sends for byte in call[call.index("-H") + 1 :]]
    assert delivered == ["78"] * len(payload)


@requires_tmux
def test_empty_text_sends_nothing_at_all(pane: tuple[TmuxServer, str]) -> None:
    server, pane_id = pane
    calls: list[tuple[str, ...]] = []
    original = server.run

    def record(*args: str, stdin: bytes | None = None) -> object:
        calls.append(args)
        return original(*args, stdin=stdin)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(server, "run", record)
        server.send_literal(pane_id, "")
    assert calls == []
