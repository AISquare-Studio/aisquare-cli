"""One tmux control-mode client per fleet server: the pipe the pane on screen talks through.

docs/plans/fleet-tui.md §3.1 put this off as "Phase 2": polling came first because
it was fifty lines and measurable, and the measurement is what brought this in.
Every polled tmux call is a fork+exec of ~5 ms on the UI thread (measured
2026-10-20 on an M4 Pro, tmux 3.8) — one per key (``send-keys``) and one per
frame (``capture-pane``) — so a key reached the screen 60 ms later at best.
:class:`ControlClient` is ONE ``tmux -C`` process per server, started when a pane
is first on screen. Every command of the panes on that server goes down its
stdin as a line, every answer comes back framed by ``%begin``/``%end``, and
``%output`` says when the pane on screen printed.

What it is, and what it is not:

* ``%output`` is a DIRTY SIGNAL and nothing more. Its bytes are never read into
  a screen: emulating a terminal from them is option A of §3.1, rejected there.
  A pane that printed is captured — the two commands ``core.tmux`` builds
  (:func:`~aisquare.core.tmux.frame_commands`), down the same pipe — and the
  frame is their answer, parsed as a polled frame is.
* It follows the pane on screen (:meth:`ControlClient.watch`). tmux sends
  ``%output`` only for the panes of the session a client is attached to, so the
  client moves to that pane's session (``switch-client -t %pane`` — each project
  is its own ``asq-<codename>`` session) and PAUSES every other pane of it, so a
  hidden or busy background agent sends nothing down the pipe.
* Its bookkeeping — following, muting — answers on the reader thread; everything
  a caller asked for is answered through ``deliver``, the caller's own way onto
  its thread (the UI's is ``App.call_later``, which Textual makes thread-safe).

Measured on tmux 3.2a (ubuntu-22.04), 3.4 (ubuntu-24.04, as in CI) and 3.8,
2026-10-20 (``tests/test_terminal_pane.py`` re-measures what a real tmux can):

* ``refresh-client -A %N:off``, the mute §3.1 named, FREEZES the pane while
  every attached client is a control client that turned it off: tmux stops
  reading it (``server_client_check_pane_buffer``), the program blocks on its
  next write and ``#{window_activity}`` — the busy pulse — stops. A ticker's
  history went 1 → 1 in 1.5 s. With no other client attached, the fleet's usual
  state, that is every hidden agent frozen. ``:pause`` drops the output for this
  client alone and the pane goes on being read (25 → 213 in the same 1.5 s), so
  ``pause`` and ``continue`` are the mute and ``off`` is never sent.
* A pause belongs to the client and outlives ``switch-client``. ``refresh-client
  -f`` that changes ``no-output`` DROPS every pause (``control_reset_offsets``).
  So the client attaches with ``no-output`` — nothing flows before it knows the
  pane on screen — and its first batch turns that off BEFORE it pauses anything,
  in the same write.
* ``ignore-size``, and never ``refresh-client -C``: tmux leaves a control client
  that has not been given a size out of window sizing, so no fleet window changes
  size when it attaches or moves — pinned by ``resize-window`` or not.
* Each command is answered by ONE ``%begin``/``%end`` (or ``%error``) block whose
  last field is 1 for a command of this client — a line that does not parse
  included. Commands chained with ``;`` on one line run back to back and stop at
  the first error: the ones after it run not at all and are answered by nothing,
  which the client counts (:attr:`_Pending.skips`). A frame's two commands are
  chained, as the polled frame's are, and must be: on tmux 3.2a, ``display-message``
  asked a GONE pane's ``#{window_activity}`` kills the server — every agent on it
  — from any client (fixed by 3.4). Chained behind the ``capture-pane`` that fails
  for that pane, it is never asked. An EMPTY line detaches the client, so none is
  ever sent.
* The client is ``focused`` in tmux's eyes. With ``focus-events on``, the pane
  it follows is sent focus in (``ESC [ I``) when the program asked for focus
  reports (``?1004``), and focus out when the client moves on or ends: the agent
  on screen is the focused one, as in any terminal. Before this client, no pane
  was ever told either.
"""

from __future__ import annotations

import contextlib
import os
import queue
import re
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial

from aisquare.core import tmux as tmux_module
from aisquare.core.spawn import untraced_env
from aisquare.core.tmux import (
    MIN_VERSION,
    Capture,
    TmuxError,
    TmuxServer,
    TmuxUnavailable,
    check_key_names,
    frame_bound,
    frame_commands,
    framed,
    parse_frame,
    paste_buffer_name,
)

CONTROL_MINIMUM: tuple[int, int] = MIN_VERSION
"""The oldest server the client runs on: ``attach-session -f``, ``ignore-size``,
``no-output`` and ``refresh-client -A`` with ``pause``/``continue`` are all 3.2's,
and 3.2a is measured. Below it a pane polls."""
CLIENT_FLAGS = "ignore-size,no-output"
"""How the client attaches: out of window sizing, and silent until its first batch."""

Deliver = Callable[[Callable[[], object]], object]
"""Run a callable on the consumer's thread, later — never on the reader thread."""


@dataclass(frozen=True)
class Reply:
    """One command's answer: whether tmux ran it, and the lines it printed."""

    ok: bool
    lines: list[str]

    def error(self) -> TmuxError:
        return TmuxError("\n".join(self.lines).strip() or "tmux refused the command")


Done = Callable[[Reply], None]

_PLAIN = re.compile(r"%\d+|[A-Za-z0-9@=:._,/+-]+")
"""A token tmux's command parser reads as itself. ``%`` only as a pane id: a token
starting with ``%`` that is not all digits is a ``%if``-style directive to the
parser, and ``%1:pause`` unquoted is a parse error."""


def quote(arg: str) -> str:
    """``arg`` as ONE token of tmux's command language, whatever it holds.

    A control client's commands are lines tmux PARSES — quotes, ``;``, ``$VAR``,
    ``~``, ``#``, braces and ``%`` mean something there — where an argv handed to a
    tmux process is not parsed at all. A plain token goes as it is. Anything else is
    double-quoted, with the three characters tmux reads inside double quotes (``\\``,
    ``"``, ``$``) escaped and every control character spelt as an octal escape, so a
    newline cannot end the line. A NUL cannot be carried: tmux's strings end at it.
    """
    if _PLAIN.fullmatch(arg):
        return arg
    if "\x00" in arg:
        raise TmuxError("a NUL cannot go down a control client's pipe")
    out = ['"']
    for char in arg:
        if char in '\\"$':
            out.append("\\" + char)
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\{ord(char):03o}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _command(args: Sequence[str]) -> str:
    return " ".join(quote(arg) for arg in args)


def _spawn(argv: list[str]) -> subprocess.Popen[bytes]:
    """THE seam (``core.spawn.SEAMS``): start one ``tmux -C`` client, identity stripped.

    Stripped like ``core.tmux._tmux``: ``attach-session`` starts a server when none
    is running, and a server inherits the environment of whoever started it. Its
    own session (``start_new_session``), so the outer terminal's job-control signals
    — ``SIGINT``, ``SIGTSTP``, ``SIGWINCH`` — reach the UI and never the client.
    """
    try:
        return subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=untraced_env(),
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise TmuxUnavailable(f"tmux is not runnable: {exc}") from exc
    except OSError as exc:
        raise TmuxError(f"could not start a tmux control client: {exc}") from exc


@dataclass
class _Pending:
    """A command sent and not answered yet."""

    sent: float
    done: Done | None
    here: bool
    """Answered on the reader thread — the client's own bookkeeping — not delivered."""
    skips: int = 0
    """Commands chained after this one on its line: when it fails tmux runs none of
    them and answers none, and they are answered with its failure."""


class ControlClient:
    """One ``tmux -C`` client on one server, and the panes it is told are on screen.

    ``on_output`` is delivered the set of watched panes that printed since it was
    last delivered — at most one delivery waiting at a time, however much they
    print. ``on_exit`` is delivered once, with tmux's reason, when the client ends
    without :meth:`close`, after every command still waiting has been answered as
    failed.
    """

    def __init__(
        self,
        server: TmuxServer,
        *,
        deliver: Deliver,
        on_output: Callable[[set[str]], None],
        on_exit: Callable[[str], None],
    ) -> None:
        self.server = server
        self._deliver = deliver
        self._on_output = on_output
        self._on_exit = on_exit
        self._lock = threading.Lock()
        self._pending: deque[_Pending] = deque()
        self._outbox: queue.SimpleQueue[bytes | None] = queue.SimpleQueue()
        self._process: subprocess.Popen[bytes] | None = None
        self._alive = False
        self._closed = False
        self._dirty: set[str] = set()
        self._waking = False
        self._wanted: list[str] = []
        """The panes on screen, the most recently watched last: the one followed."""
        self._followed: str | None = None
        self._first = True
        """No batch has run yet: the next one turns ``no-output`` off first."""
        self._muted: set[str] = set()
        """Panes this client paused, so an output already in flight is not paused twice."""
        self._block: list[str] | None = None
        """The lines of the answer being read, between its ``%begin`` and its end."""
        self._tag = b""
        """That ``%begin``'s time, number and flags: its ``%end`` repeats them exactly."""
        self._mine = False
        """Whether that block answers a command of this client (flags 1)."""
        self.answered = False
        """tmux has answered a command of this client: it started, and ran."""
        self.outputs: dict[str, int] = {}
        """``%output`` lines received per pane (instrumentation, and the mute's proof)."""
        self.reason: str | None = None
        """Why the client ended, once it has."""

    # --- life -------------------------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._alive

    @property
    def pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    def start(self, pane_id: str) -> None:
        """Attach to ``pane_id``'s session and follow it. Raises :class:`TmuxError` when no
        process could be started; one that starts and is refused ends through ``on_exit``."""
        argv = self.server.argv("-C", "attach-session", "-t", pane_id, "-f", CLIENT_FLAGS)
        self._process = _spawn(argv)
        self._alive = True
        threading.Thread(target=self._read, name="tmux-control-read", daemon=True).start()
        threading.Thread(target=self._write, name="tmux-control-write", daemon=True).start()
        self.watch(pane_id)

    def close(self) -> None:
        """End the client on purpose: nothing is delivered for it any more."""
        with self._lock:
            self._closed = True
            self._alive = False
            self._pending.clear()
        self._outbox.put(None)
        if self._process is not None:
            with contextlib.suppress(OSError):
                self._process.terminate()

    def kill(self) -> None:
        """End a client that stopped answering; it ends through ``on_exit`` like any other."""
        if self._process is not None:
            with contextlib.suppress(OSError):
                self._process.kill()

    def stalled(self, limit: float) -> bool:
        """Whether the oldest command waiting was sent more than ``limit`` seconds ago."""
        with self._lock:
            return bool(self._pending) and time.monotonic() - self._pending[0].sent > limit

    # --- following the pane on screen --------------------------------------------------

    def watch(self, pane_id: str) -> None:
        """``pane_id`` is on screen: deliver its output, and follow it."""
        with self._lock:
            if pane_id in self._wanted:
                self._wanted.remove(pane_id)
            self._wanted.append(pane_id)
            self._muted.discard(pane_id)
            follow = self._followed != pane_id
            self._followed = pane_id
        if follow:
            self._follow(pane_id)
        else:
            self._quietly([["refresh-client", "-A", f"{pane_id}:continue"]])

    def unwatch(self, pane_id: str) -> None:
        """``pane_id`` left the screen: pause it, and follow the pane on screen before it."""
        with self._lock:
            if pane_id in self._wanted:
                self._wanted.remove(pane_id)
            self._muted.add(pane_id)
            after = self._wanted[-1] if self._wanted else None
            if self._followed == pane_id:
                self._followed = None
        self._quietly([["refresh-client", "-A", f"{pane_id}:pause"]])
        if after is not None and self._followed is None:
            self.watch(after)

    def _follow(self, pane_id: str) -> None:
        """Move to ``pane_id``'s session with every other pane of it paused first.

        The panes are asked for (``list-panes -s``), then ONE write pauses all but the
        watched ones, continues those, and switches — so tmux reads the batch together
        and nothing of a hidden pane flows in between. The first batch turns
        ``no-output`` off ahead of its pauses (the module docstring says why first).
        """

        def listed(reply: Reply) -> None:
            if not reply.ok:
                return  # the pane is gone: its own commands will say so
            panes = [line.strip() for line in reply.lines if line.strip()]
            with self._lock:
                if self._followed != pane_id:
                    return  # watched and left again before tmux answered
                wanted = set(self._wanted)
                first, self._first = self._first, False
                self._muted.update(pane for pane in panes if pane not in wanted)
            batch: list[list[str]] = []
            if first:
                batch.append(["refresh-client", "-f", "!no-output"])
            for pane in panes:
                state = "continue" if pane in wanted else "pause"
                batch.append(["refresh-client", "-A", f"{pane}:{state}"])
            batch.append(["switch-client", "-t", pane_id])
            self._quietly(batch)

        with contextlib.suppress(TmuxError):
            self._send([(["list-panes", "-s", "-t", pane_id, "-F", "#{pane_id}"], listed, True)])

    def _quietly(self, batch: list[list[str]]) -> None:
        """The client's own bookkeeping, in one write; its answers matter to nobody."""
        with contextlib.suppress(TmuxError):
            self._send([(args, None, True) for args in batch])

    # --- what callers ask for -----------------------------------------------------------

    def _send(
        self, commands: Sequence[tuple[Sequence[str], Done | None, bool]], *, chain: bool = False
    ) -> None:
        """Queue ``commands`` as one write, each answered in order. Raises when ended.

        ``chain`` puts them on ONE line, joined by ``;``: tmux runs them back to back,
        and none after the first that fails.
        """
        try:
            separator = " ; " if chain else "\n"
            data = (separator.join(_command(args) for args, _, _ in commands) + "\n").encode(
                "utf-8"
            )
        except UnicodeEncodeError as exc:
            raise TmuxError(f"not text tmux can be sent: {exc}") from exc
        with self._lock:
            if not self._alive:
                raise TmuxError(self.reason or "the tmux control client has ended")
            now = time.monotonic()
            last = len(commands) - 1
            self._pending.extend(
                _Pending(now, done, here, last - index if chain else 0)
                for index, (_, done, here) in enumerate(commands)
            )
            self._outbox.put(data)

    def command(self, *args: str, done: Done | None = None) -> None:
        """Run one tmux command; ``done`` is delivered its :class:`Reply`."""
        self._send([(args, done, False)])

    def capture(
        self,
        pane_id: str,
        *,
        scrollback: int = 0,
        height: int | None = None,
        flags: bool = False,
        done: Callable[[Capture | TmuxError], None],
    ) -> None:
        """:meth:`TmuxServer.capture` down the pipe: ``done`` is delivered the frame, or why not."""
        scrollback = max(0, scrollback)
        self._frame(pane_id, scrollback, frame_bound(scrollback, height), flags, done)

    def _frame(
        self,
        pane_id: str,
        scrollback: int,
        bound: int | None,
        flags: bool,
        done: Callable[[Capture | TmuxError], None],
    ) -> None:
        capture, display = frame_commands(pane_id, scrollback, bound, flags)
        rows: list[Reply] = []

        def shown(reply: Reply) -> None:
            first = rows[0] if rows else Reply(False, ["no capture answered"])
            if not first.ok:
                done(first.error())
                return
            if not reply.ok or not reply.lines:
                done(reply.error())
                return
            try:
                lines, wrapped, facts = parse_frame(first.lines, reply.lines[-1], flags)
            except TmuxError as error:
                done(error)
                return
            if bound is not None and len(lines) < facts.height:
                # A stale height hint gave a short frame: asked again unbounded,
                # as the polled capture does.
                try:
                    self._frame(pane_id, scrollback, None, flags, done)
                except TmuxError as error:
                    done(error)
                return
            done(framed(lines, wrapped, facts, scrollback))

        # Chained, never two lines: see the module docstring for what a lone
        # ``display-message`` of a gone pane does to tmux 3.2a.
        self._send([(capture, rows.append, False), (display, shown, False)], chain=True)

    def send_keys(self, pane_id: str, *keys: str, done: Done | None = None) -> None:
        """:meth:`TmuxServer.send_keys` down the pipe — the same names refused.

        Quoted (:func:`quote`), so a key's trailing ``;`` is a character of the key
        and never tmux's separator, which the argv path needs ``_data_arg`` for.
        """
        if not keys:
            return
        check_key_names(keys)
        self._send([(["send-keys", "-t", pane_id, "--", *keys], done, False)])

    def send_literal(self, pane_id: str, text: str, *, done: Done | None = None) -> None:
        """:meth:`TmuxServer.send_literal` down the pipe: the text's bytes, as hex."""
        if text:
            self.send_bytes(pane_id, text.encode("utf-8"), done=done)

    def send_bytes(self, pane_id: str, data: bytes, *, done: Done | None = None) -> None:
        """:meth:`TmuxServer.send_bytes` down the pipe: hex pairs, in the same chunks, one
        write. ``done`` is delivered once, after the last chunk: failed if any chunk was."""
        chunk = tmux_module._HEX_CHUNK
        pieces = [data[start : start + chunk] for start in range(0, len(data), chunk)]
        if not pieces:
            return
        failed: list[Reply] = []

        def each(reply: Reply) -> None:
            if not reply.ok:
                failed.append(reply)

        def last(reply: Reply) -> None:
            each(reply)
            if done is not None:
                done(failed[0] if failed else reply)

        self._send(
            [
                (
                    ["send-keys", "-t", pane_id, "-H", *(f"{byte:02x}" for byte in piece)],
                    last if index == len(pieces) - 1 else each,
                    False,
                )
                for index, piece in enumerate(pieces)
            ],
            chain=True,  # as the polled path, no chunk after one that failed
        )

    def paste(self, pane_id: str, text: str, *, done: Done | None = None) -> bool:
        """:meth:`TmuxServer.paste` down the pipe: ``set-buffer`` then ``paste-buffer -p -d``.

        ``load-buffer -`` reads the client's stdin, which is this pipe, so the text goes
        as ``set-buffer``'s argument instead, quoted. False, with nothing sent, for text
        that cannot be carried that way (a NUL): the caller pastes it the polled way.
        A failed paste deletes its buffer, as the polled one does.
        """
        if not text:
            return True
        if "\x00" in text:
            return False
        name = paste_buffer_name()
        loaded: list[Reply] = []

        def pasted(reply: Reply) -> None:
            outcome = loaded[0] if loaded and not loaded[0].ok else reply
            if not outcome.ok:
                self._quietly([["delete-buffer", "-b", name]])
            if done is not None:
                done(outcome)

        self._send(
            [
                (["set-buffer", "-b", name, "--", text], loaded.append, False),
                (["paste-buffer", "-p", "-d", "-b", name, "-t", pane_id], pasted, False),
            ],
            chain=True,
        )
        return True

    # --- the pipe ----------------------------------------------------------------------

    def _write(self) -> None:
        process = self._process
        assert process is not None and process.stdin is not None
        fd = process.stdin.fileno()
        while (data := self._outbox.get()) is not None:
            try:
                while data:
                    data = data[os.write(fd, data) :]
            except OSError:
                break  # the client is gone; the reader hears its end
        with contextlib.suppress(OSError):
            process.stdin.close()

    def _read(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        fd = process.stdout.fileno()
        rest = b""
        try:
            while chunk := os.read(fd, 65536):
                lines = (rest + chunk).split(b"\n")
                rest = lines.pop()
                for line in lines:
                    self._heard(line)
        except OSError:
            pass
        finally:
            self._ended(process)

    def _heard(self, line: bytes) -> None:
        """One line from tmux: a block's line, its end, or a notification."""
        if self._block is not None:
            if line in (b"%end " + self._tag, b"%error " + self._tag):
                body, self._block = self._block, None
                if self._mine:
                    self._answer(line.startswith(b"%end"), body)
            else:
                self._block.append(line.decode("utf-8", errors="replace"))
            return
        if line.startswith(b"%begin "):
            self._tag = line[len(b"%begin ") :]
            self._mine = self._tag.rsplit(b" ", 1)[-1] == b"1"
            self._block = []
        elif line.startswith((b"%output ", b"%extended-output ")):
            start = line.index(b" ") + 1
            end = line.find(b" ", start)
            self._printed(line[start : end if end != -1 else None].decode("ascii", "replace"))
        elif line.startswith(b"%pane-mode-changed "):
            self._printed(line.split(b" ", 1)[1].strip().decode("ascii", "replace"), mute=False)
        elif line.startswith(b"%exit"):
            self.reason = line[len(b"%exit") :].strip().decode("utf-8", "replace") or None

    def _answer(self, ok: bool, body: list[str]) -> None:
        skipped: list[_Pending] = []
        with self._lock:
            pending = self._pending.popleft() if self._pending else None
            if pending is not None and not ok:
                for _ in range(min(pending.skips, len(self._pending))):
                    skipped.append(self._pending.popleft())
            self.answered = True
        reply = Reply(ok, body)
        for item in (pending, *skipped):
            if item is None or item.done is None:
                continue
            if item.here:
                item.done(reply)
            else:
                self._deliver(partial(item.done, reply))

    def _printed(self, pane: str, *, mute: bool = True) -> None:
        """``pane`` printed (or changed mode): wake the consumer, or pause a pane nobody shows."""
        wake = pause = False
        with self._lock:
            if mute:
                self.outputs[pane] = self.outputs.get(pane, 0) + 1
            if pane in self._wanted:
                self._dirty.add(pane)
                wake, self._waking = not self._waking, True
            elif mute and pane not in self._muted:
                # A pane that came into the session after it was followed — a window
                # spawned since — is not paused yet: it is now, once.
                self._muted.add(pane)
                pause = True
        if pause:
            self._quietly([["refresh-client", "-A", f"{pane}:pause"]])
        if wake:
            self._deliver(self._wake)

    def _wake(self) -> None:
        with self._lock:
            panes, self._dirty, self._waking = self._dirty, set(), False
        if panes:
            self._on_output(panes)

    def _ended(self, process: subprocess.Popen[bytes]) -> None:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            process.wait(timeout=5)
        complaint = b""
        if process.stderr is not None:
            with contextlib.suppress(OSError, ValueError):
                complaint = process.stderr.read()
        with self._lock:
            closed = self._closed
            self._alive = False
            pending = list(self._pending)
            self._pending.clear()
        self._outbox.put(None)
        if self.reason is None:
            said = complaint.decode("utf-8", "replace").strip()
            self.reason = said or f"tmux control client exited ({process.returncode})"
        if closed:
            return
        failure = Reply(False, [self.reason])
        for item in pending:
            if item.done is not None and not item.here:
                self._deliver(partial(item.done, failure))
        self._deliver(partial(self._on_exit, self.reason))
