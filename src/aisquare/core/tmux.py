"""The fleet's private tmux server — every tmux command this package runs.

tmux is the fleet's session substrate (docs/plans/fleet-tui.md §3.1): each
agent is a window in a per-project session on a PRIVATE server (``tmux -L asq``
with a bundled config), so the user's own tmux sessions, config and prefix key
are never touched, agents outlive the UI, and ``tmux -L asq attach`` from any
terminal is the full-fidelity escape hatch.

Only plumbing lives here — no policy, no store, no Textual. The rendering trick
the UI relies on is ``capture`` : one tmux process runs ``capture-pane -e``
(the screen, with colours) and ``display-message`` (cursor, size, dead flag) in
sequence, so a frame costs one fork. Input goes back through ``send-keys`` (named
keys), ``send-keys -l`` (literal text) and ``load-buffer`` + ``paste-buffer -p``
(bracketed paste, so the agent sees one paste and not N Enters).

``_tmux`` is the ONE spawn seam (``core.spawn.SEAMS``), ruled EXCLUDED and
stripped: the server inherits the environment of whoever starts it and hands it
to every window, so an inherited tracing identity here would become every
agent's identity. Each window's agent takes its own through ``aisquare launch``.

Verified against tmux 3.7c, and on 3.4 where a version is named below
(``tests/test_tmux.py`` re-verifies the live ones on whichever tmux is here):

* ``new-session``/``new-window`` accept ``-e KEY=VALUE`` and a multi-argument
  command after ``--``; ``send-keys -l --`` takes literal text that starts with
  ``-``; a lone ``;`` argument separates commands in one invocation, and an
  error in one command stops the chain with exit status 1.
* ``=name`` targets a session exactly (``=prob`` does not match ``probe``;
  bare ``prob`` does). ``.`` and ``:`` in a session name make it untargetable
  by name — tmux reads them as window and pane separators — which is why names
  come from codenames and why :meth:`TmuxServer.spawn_window` refuses them.
* ``display-message -t %gone`` is NOT an error: its target may fail
  (``CMD_FIND_CANFAIL``), so tmux exits 0 and expands every ``pane_*`` field to
  the empty string. ``capture-pane`` and ``send-keys`` on the same target fail
  with ``can't find pane``. :meth:`TmuxServer.pane_facts` turns the former into
  ``None`` rather than a pane of zeros.
* ``capture-pane -p`` prints every row of the screen, blank rows included, so a
  live frame is exactly ``pane_height`` lines; ``-S -k`` is clamped to
  ``history_size`` silently. ``-e`` re-encodes attributes (a reset arrives as
  ``ESC[39m``, not the ``ESC[0m`` the program wrote).
* ``-f file`` at server start SWALLOWS configuration errors: tmux queues them
  for the first attached client, which the fleet never has, and the command
  exits 0. ``source-file`` on a running server reports them on stderr with
  status 1 — so that, on a throwaway server, is :meth:`TmuxServer.check_conf`.
* ``window-size manual`` in the GLOBAL window options is FATAL below tmux 3.7,
  whether the ``-f`` file sets it at server start or a ``set -g`` sets it on a
  running server. Creating a window reads that global (``default_window_size``,
  which has no window yet) and ``clients_calculate_size`` then reads
  ``w->manual_sx`` through the window pointer it was never given: the server
  segfaults and the client says ``server exited unexpectedly``. tmux 3.7 added
  the ``w != NULL`` guard; 3.4 — what ``ubuntu-latest`` ships — dies on the
  first ``new-session``, and on every later ``new-window`` if the option is set
  once the server is up. So the fleet sets ``window-size`` on each window it
  creates instead (:data:`WINDOW_OPTIONS`): a WINDOW's own option is what pins
  its size anyway (``recalculate_size`` reads it, measured on 3.4 — a window
  left at the default ``latest`` is resized to an attaching client's terminal
  the moment that client looks at it, a pinned one is not), it is what tmux's
  own ``resize-window`` sets, and it arms nothing for the next window — the
  fleet's, or one a user opens in the ``tmux -L asq attach`` escape hatch.
* Not setting it globally, though, hands the CREATION-time size rule back to
  tmux's default ``window-size latest`` — so a pin alone is not a sizing model,
  it is a freeze on whatever size tmux chose. ``default_window_size`` with
  ``latest`` measures the most recent CLIENT on the server, any client on any
  session, and only falls back to the session's ``-x``/``-y`` when the server
  has none at all. Measured on tmux 3.4, with one 80x24 ``attach`` open: a
  ``new-window`` in a 200x50 session is born 80x24, and so is a brand new
  ``new-session -d -x 200 -y 50`` on its own session — ``-x``/``-y`` loses to
  the client. Pinning then makes that permanent. So every window the fleet
  creates is sized EXPLICITLY, by ``resize-window`` on the ``@id`` tmux just
  printed — ``new-session`` takes ``-x``/``-y`` but ``new-window`` on 3.4 has
  no such flag at all (``command new-window: unknown flag -x``). That one
  command both records the size the fleet asked for in
  ``w->manual_sx``/``manual_sy`` and — tmux's own doing, in
  ``cmd-resize-window`` — sets that window's ``window-size`` to ``manual``.
* ``window_activity_flag`` is set the moment a window exists — before its
  command has written a byte — and nothing clears it on a server nobody is
  attached to (not output, not a capture, not ``select-window``), so
  ``WindowInfo.activity`` is always true there and carries no signal. A pulse
  needs ``history_size`` or the cursor to change between frames instead.
* ``-c <cwd>`` with a directory that does not exist is NOT an error: tmux
  falls back to ``$HOME`` (then ``/``) and starts the command there, so a
  coder spawned into a worktree that was never created would edit the wrong
  tree without a word. :meth:`TmuxServer.spawn_window` refuses such a ``cwd``.
* One frame — ``capture-pane`` plus ``display-message`` in one process — costs
  about 1.7 ms at 80x24 and 1.9 ms at 200x60 on an idle machine (medians of
  100, a full screen of coloured text), of which ~1.45 ms is the client round
  trip itself (``display-message -p x`` alone) — so the screen's size barely
  matters and the fork does. With a test suite running alongside the same
  frames took 4 to 7 ms. The 50 ms budget of §3.1 has a wide margin either way;
  ``tests/test_tmux.py`` prints the numbers each run (``pytest -s``).
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from aisquare.core import paths
from aisquare.core.spawn import untraced_env

DEFAULT_SOCKET = "asq"
MIN_VERSION = (3, 2)
"""``new-session -e`` and ``extended-keys`` arrived in 3.2 — that is the whole floor.

Both from tmux's own CHANGES for 3.1c → 3.2, and the ``-e`` entry there says
what it is: the flag ``new-window`` ALREADY had, given to ``new-session`` too.
:meth:`TmuxServer.spawn_window` uses it on whichever of the two it needs, so the
later arrival is the one that sets the floor — verified against the 3.2 build
here, which takes ``new-session -e ASQ_PROBE=yes`` and reports it back through
``show-environment``.

Raising the floor would not buy back the chords ``aisquare.core.keys`` hand-encodes,
because those are not a missing NAME on any tmux the fleet supports. tmux 3.4 —
what ``ubuntu-latest`` ships — parses every one of them: ``bind-key S-Enter``,
``C-S-a``, ``M-S-BSpace`` … are all accepted and printed back by ``list-keys``
(as tmux's own spelling, ``S-C-a``; only a name it really does not know,
``Bogus`` or ``F13``, answers ``unknown key``). What it will not do is ENCODE
such a chord for a pane in legacy mode, and ``cmd-send-keys`` does not fail when
it cannot: it types the NAME as text. ``send-keys S-Enter`` into a
``stty raw -echo; cat`` pane put the SEVEN bytes ``S-Enter`` there, and
``C-Enter`` put nothing at all (measured on 3.4 here, reading the file the pane
wrote). Which chords, and how many, is ``aisquare.core.keys``'s business and its
docstring carries the count over the vocabulary it emits; this is only about
what the SERVER can be told to do about it.

And it is a MODE, not a key-table gap. ``extended-keys always`` makes tmux 3.4
encode such a chord without waiting for the pane to ask for it: same server,
same names, that option the only difference, ``S-Enter`` → ``ESC [ 13 ; 2 u``,
``C-Enter`` → ``ESC [ 13 ; 5 u``, ``C-S-a`` → ``ESC [ 65 ; 6 u``, ``S-Tab`` →
``ESC [ 9 ; 2 u``, with ``Enter``, ``C-c``, ``Tab`` and ``M-x`` byte-identical
under both values (four chords measured, not the whole vocabulary).

The fleet still sets ``extended-keys on`` (:data:`BUNDLED_CONF`) and still
hand-encodes those chords itself, for two measured reasons that ``always`` cannot
be talked out of:

* tmux 3.2 — this very floor — REJECTS the value. With ``set -s extended-keys
  always`` in the ``-f`` file the 3.2 server starts and every other line applies
  (``escape-time`` is 0), but that option keeps its default: ``show -s
  extended-keys`` answers ``off``. Then ``source-file`` on the running server —
  which is exactly what :meth:`TmuxServer.check_conf` runs — answers ``bad
  value: always`` with status 1, so the doctor would condemn a good
  configuration on the oldest tmux the fleet claims to support. 3.2a, 3.3a, 3.4
  and 3.5a all accept it — ``options-table.c`` is where the change is:
  ``extended-keys`` is an ``OPTIONS_TABLE_FLAG`` in 3.2 and an
  ``OPTIONS_TABLE_CHOICE`` over off/on/always from 3.2a.
* on 3.5a — the newest build measured here — ``always`` makes ``send-keys -l``
  DROP control bytes. Sending ``<0x01><0x02>…<0x1f><0x7f>`` as one literal
  string: under ``on`` all 32 arrive; under ``always`` only four survive — TAB
  (0x09), CR (0x0d), ESC (0x1b) and DEL (0x7f) — and the other 28 vanish
  silently. Under ``on`` nothing is dropped on either 3.4 or 3.5a, and on 3.4
  nothing is dropped under ``always`` either, so this is newer than the tmux CI
  runs and squarely in the range users have.
  :meth:`TmuxServer.send_literal` is how the fleet forwards pasted text, so
  ``always`` would quietly damage any paste carrying a control byte.

Letting tmux encode would not even be one answer: on 3.5a under ``always`` the
same ``S-Enter`` arrives as ``ESC [ 27 ; 2 ; 13 ~``, not the CSI-u 3.4 sends,
because ``extended-keys-format`` defaults to ``xterm`` there. Hand-encoding is
what is left, and it is the version-independent half: the bytes the fleet writes
are the bytes the agent receives on every tmux from 3.2 up, and the ESC that
starts each sequence is one of the four ``send-keys -l`` never drops even under
the value the fleet does not use.
"""
PASTE_BUFFER = "asq-paste"
CONF_NAME = "fleet-tmux.conf"
CHECK_SOCKET_SUFFIX = "-check"
"""Prefix of the socket :meth:`TmuxServer.check_conf` starts its throwaway server on.

The socket is ``<socket><CHECK_SOCKET_SUFFIX>-<random>``, never a fixed name.
A fixed one is shared state between concurrent checks, and every way of
guarding it is worse than not sharing it: a check that kills the socket first
kills a CONCURRENT check's server, and one that does not lets a server left
over by a killed run answer in its place — tmux reads ``-f`` only when it
STARTS a server, so that stale server would run some other configuration and
report a clean bill for the file nobody tested. A name no other call can pick
removes both.

What a random name costs is that nothing can ADDRESS it afterwards, so a run
killed without unwinding (SIGKILL, the OOM killer) would strand its probe
server for the life of the box. :meth:`TmuxServer._reclaim_abandoned_probes`
is the answer to that, and this prefix plus :data:`_PROBE_TAIL` is the whole
shape it recognises."""
_COMMAND_TIMEOUT = 30.0

#: The random tail :meth:`TmuxServer.check_conf` gives its socket, as a pattern
#: the sweep matches against the part of a filename AFTER the prefix — never
#: interpolated into a glob. ``FleetSettings.tmux_socket`` is the user's to
#: choose, and a socket named ``asq[1]`` in a ``glob()`` pattern would match
#: another fleet's sockets and kill their servers; ``str.startswith`` on a
#: literal prefix cannot, and this pins the rest to eight hex digits so that a
#: second fleet on a socket merely BEGINNING ``asq-check-`` is left alone too.
_PROBE_TAIL = re.compile(r"[0-9a-f]{8}")

#: How old a ``<socket>-check-<hex>`` socket file must be before the sweep may
#: touch it — the guard that keeps it off a CONCURRENT check's live probe,
#: which is the whole reason the name is random in the first place.
#:
#: It is derived, not guessed: every tmux invocation is bounded by
#: :data:`_COMMAND_TIMEOUT` (:func:`_tmux`), and a check makes exactly two after
#: the socket exists (the probe chain and the ``kill-server`` behind it), so no
#: live check can hold one longer than ``2 * _COMMAND_TIMEOUT``. Twice that is
#: the margin. tmux writes the socket file when it STARTS the server and never
#: touches it again — not on a client connect, not when the server exits
#: (measured on 3.4 here) — so its mtime IS that server's start time.
_PROBE_ABANDONED_AFTER = 4 * _COMMAND_TIMEOUT

#: Wall clock the sweep may spend, so that :meth:`TmuxServer.check_conf` stays
#: flat in the number of stale sockets instead of O(N) ``kill-server`` calls at
#: :data:`_COMMAND_TIMEOUT` each. Nothing is lost by stopping early: what is not
#: reclaimed this call is still there, and still older, for the next one.
_SWEEP_BUDGET = _COMMAND_TIMEOUT

#: The bundled server configuration. Applied with ``-f`` when the server starts
#: (harmless afterwards). Every line has a reason: docs/plans/fleet-tui.md §3.1.
#: Every option is verified to load, with this value, on the tmux running the
#: tests by ``tests/test_tmux.py::test_live_every_bundled_option_is_applied_with_its_value``.
#: ``window-size`` is NOT here and must never be: a global one kills the server
#: below tmux 3.7 (module docstring). It lives in :data:`WINDOW_OPTIONS`, set on
#: each window as it is created, and
#: ``tests/test_tmux.py::test_no_window_option_is_written_into_the_file_the_server_starts_with``
#: fails if it ever reappears here.
BUNDLED_CONF = """\
# aisquare fleet — private tmux server configuration (regenerated; do not edit)
set -g status off
set -s escape-time 0
set -g history-limit 50000
set -g remain-on-exit on
set -g mouse off
set -g default-terminal tmux-256color
set -ga terminal-overrides ',*:Tc'
set -s extended-keys on
set -g set-clipboard off
set -g focus-events on
set -g monitor-activity on
set -g visual-activity off
set -g allow-rename off
set -g automatic-rename off
set -g renumber-windows off
"""

#: Window-SCOPED options the fleet sets on each window it creates, the moment
#: that window exists — the half of its configuration that cannot be in a file
#: the server reads at start (module docstring: a global ``window-size`` is a
#: segfault waiting for the next window on tmux below 3.7). Nothing here may
#: also appear in :data:`BUNDLED_CONF`;
#: ``tests/test_tmux.py::test_no_window_option_is_written_into_the_file_the_server_starts_with``
#: is the guard.
#:
#: ``window-size manual`` says the window keeps the size the fleet gave it, so
#: a user who opens the ``tmux -L asq attach`` escape hatch on an 80x24
#: terminal does not shrink every agent's screen under the UI. It is only half
#: the model: WHAT size it keeps is the ``resize-window`` in
#: :func:`_configure_window`, because the pin freezes whatever tmux picked and
#: what tmux picks without it is the latest client's terminal (module
#: docstring). Applied by :meth:`TmuxServer.spawn_window`, and rehearsed on the
#: probe window by :meth:`TmuxServer.check_conf`.
WINDOW_OPTIONS: tuple[tuple[str, str], ...] = (("window-size", "manual"),)

#: The size :meth:`TmuxServer.spawn_window` gives a window whose caller does not
#: ask for one. Roomy on purpose: an agent starts writing before anything is
#: watching it, and this is the shape that output is wrapped to until the UI
#: puts the pane on screen and ``TerminalPane._sync_size`` calls
#: :meth:`TmuxServer.resize` with the widget's real content area. A headless
#: fleet — ``aisquare fleet spawn`` with no UI open — never gets that call and
#: keeps this size for the life of the agent, which is why it is not 80x24.
DEFAULT_WINDOW_SIZE = (200, 50)

#: Where :meth:`TmuxServer.check_conf` proves the configuration survives a real
#: server: one session, one window running ``true``, in the one directory POSIX
#: guarantees. The name obeys :func:`_require_targetable`; it needs no unique
#: part, because the socket it lives on is unique already
#: (:data:`CHECK_SOCKET_SUFFIX`) and holds nothing else.
_CHECK_SESSION = "asq-conf-check"
_CHECK_COMMAND = ("true",)
_CHECK_CWD = "/"
_CHECK_SIZE = (80, 24)

#: Separator for multi-field ``display-message`` output. Never appears in a pane
#: id, a size or a flag; a command name or title containing it would be perverse.
#: The title is the last field and is split off with ``maxsplit`` so that even a
#: perverse title cannot make every frame unparseable.
_SEP = "|~|"
_FACTS_FIELDS = (
    "pane_id",
    "pane_width",
    "pane_height",
    "cursor_x",
    "cursor_y",
    "cursor_flag",
    "alternate_on",
    "history_size",
    "pane_dead",
    "pane_dead_status",
    "pane_in_mode",
    "pane_current_command",
    "pane_title",
)
_FACTS_FORMAT = _SEP.join(f"#{{{name}}}" for name in _FACTS_FIELDS)
_WINDOW_FIELDS = (
    "window_id",
    "window_name",
    "pane_id",
    "pane_dead",
    "pane_dead_status",
    "pane_current_command",
    "window_activity_flag",
)
_WINDOW_FORMAT = _SEP.join(f"#{{{name}}}" for name in _WINDOW_FIELDS)
_VERSION = re.compile(r"(\d+)\.(\d+)")
#: Characters tmux reads as target separators; a session named with one can be
#: created but never addressed by name again (``=a.b`` → "can't find pane: b").
_UNTARGETABLE = frozenset(".:")


class TmuxError(RuntimeError):
    """A tmux command failed; the message carries tmux's own stderr."""


class TmuxUnavailable(TmuxError):
    """No usable tmux: missing from PATH, or older than :data:`MIN_VERSION`."""


@dataclass(frozen=True)
class Completed:
    """One finished tmux command."""

    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], bytes | None], Completed]
"""How commands run: the real :func:`_tmux`, or a fake in tests."""


def _tmux(argv: Sequence[str], stdin: bytes | None) -> Completed:
    """THE seam: run one tmux invocation with the tracing identity stripped.

    Every way the process can fail to run at all is mapped onto the module's
    own exceptions, so a caller that catches :class:`TmuxError` has caught
    everything: a binary that vanished between ``which`` and ``exec`` is
    :class:`TmuxUnavailable`; a server that stops answering (``_COMMAND_TIMEOUT``)
    or any other OS refusal is :class:`TmuxError`.
    """
    try:
        result = subprocess.run(
            list(argv),
            input=stdin,
            capture_output=True,
            env=untraced_env(),
            timeout=_COMMAND_TIMEOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        raise TmuxUnavailable(f"tmux is not runnable: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise TmuxError(
            f"tmux did not answer within {_COMMAND_TIMEOUT:.0f}s: {' '.join(argv[:6])} …"
        ) from exc
    except OSError as exc:
        raise TmuxError(f"could not run tmux: {exc}") from exc
    return Completed(
        returncode=result.returncode,
        stdout=result.stdout.decode("utf-8", errors="replace"),
        stderr=result.stderr.decode("utf-8", errors="replace"),
    )


def parse_version(text: str) -> tuple[int, int] | None:
    """``tmux 3.7c`` → ``(3, 7)``; ``tmux next-3.5`` → ``(3, 5)``; unparseable → ``None``."""
    match = _VERSION.search(text)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


@dataclass(frozen=True)
class PaneFacts:
    """What ``display-message`` knows about a pane at one instant."""

    pane_id: str
    width: int
    height: int
    cursor_x: int
    cursor_y: int
    cursor_visible: bool
    alternate_on: bool
    history_size: int
    dead: bool
    dead_status: int | None
    in_mode: bool
    current_command: str
    title: str


@dataclass(frozen=True)
class Capture:
    """One frame: the visible rows (with SGR escapes) and the pane's facts."""

    lines: list[str]
    facts: PaneFacts
    scrollback: int
    """How many history lines above the live screen the frame starts at (0 = live).

    The EFFECTIVE offset: tmux clamps a request deeper than ``facts.history_size``
    to the top of history, and this reports where the frame really starts.
    """


@dataclass(frozen=True)
class WindowInfo:
    """One window of a fleet session, as ``list-panes -s`` reports it."""

    session: str
    window_id: str
    name: str
    pane_id: str
    dead: bool
    dead_status: int | None
    current_command: str
    activity: bool
    """``window_activity_flag`` as tmux reports it — which, on a server nobody is
    attached to, is true from the window's creation and never cleared, so it is
    NOT "is printing now"; compare ``PaneFacts.history_size`` and the cursor
    between frames for that (see the module docstring). Kept because it is what
    tmux says and an attached client does clear it."""


def _int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except ValueError:
        return default


def _optional_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def _facts(line: str) -> PaneFacts:
    # maxsplit: the title is last and is the one field a program controls.
    fields = line.split(_SEP, len(_FACTS_FIELDS) - 1)
    if len(fields) != len(_FACTS_FIELDS):
        raise TmuxError(f"unexpected display-message output: {line!r}")
    values = dict(zip(_FACTS_FIELDS, fields, strict=True))
    return PaneFacts(
        pane_id=values["pane_id"],
        width=_int(values["pane_width"]),
        height=_int(values["pane_height"]),
        cursor_x=_int(values["cursor_x"]),
        cursor_y=_int(values["cursor_y"]),
        cursor_visible=values["cursor_flag"] == "1",
        alternate_on=values["alternate_on"] == "1",
        history_size=_int(values["history_size"]),
        dead=values["pane_dead"] == "1",
        dead_status=_optional_int(values["pane_dead_status"]),
        in_mode=values["pane_in_mode"] == "1",
        current_command=values["pane_current_command"],
        title=values["pane_title"],
    )


def _require_targetable(name: str) -> None:
    """Refuse a session name no method of this module could address afterwards."""
    if not name or any(char in _UNTARGETABLE for char in name):
        raise TmuxError(
            f"session name {name!r} cannot be targeted by tmux — '.' and ':' are "
            "window and pane separators, and a name must not be empty"
        )


def _require_directory(cwd: Path) -> None:
    """Refuse a working directory tmux would silently replace with ``$HOME``."""
    if not cwd.is_dir():
        raise TmuxError(
            f"working directory {str(cwd)!r} is not a directory — tmux would start the "
            "command in $HOME instead, without saying so"
        )


def _chain(*commands: Sequence[str]) -> list[str]:
    """Several tmux commands as ONE argument list, separated by tmux's own ``;``.

    One invocation is one fork and one round trip, and tmux runs the commands in
    order, stopping at the first that fails (module docstring) — so a chain is
    also how two steps that must not be interleaved by another process stay
    together.
    """
    argv: list[str] = []
    for command in commands:
        if argv:
            argv.append(";")
        argv.extend(command)
    return argv


def _configure_window(target: str, width: int, height: int) -> list[list[str]]:
    """Everything the fleet does to a window tmux has just created, in order.

    :data:`WINDOW_OPTIONS` first, so from that instant no client attaching to
    the server can resize the window; then ``resize-window``, which writes the
    size the fleet asked for into the window's ``manual_sx``/``manual_sy`` —
    the numbers the pin holds. That order is deliberate and measured on tmux
    3.4 with an 80x24 client attached: pin-then-resize and resize-then-pin both
    end at the requested size, but only pin-first has no instant in which a
    client can size the window, and only resize-LAST makes the requested size
    the final word. Without the resize the window keeps whatever
    ``window-size latest`` gave it at birth — the latest client's terminal —
    frozen there for good (module docstring).

    ``target`` must name a window exactly — an ``@id`` from ``-P -F``, or
    ``=session:`` where the session has the one window. An EMPTY target is not a
    no-op to tmux: it resolves to whatever window is current, which on a server
    holding several agents is somebody else's. Callers make sure it is not.
    """
    commands = [
        ["set-option", "-w", "-t", target, option, value] for option, value in WINDOW_OPTIONS
    ]
    commands.append(
        ["resize-window", "-t", target, "-x", str(max(1, width)), "-y", str(max(1, height))]
    )
    return commands


class TmuxServer:
    """A handle on one private tmux server (``-L socket``).

    Every method raises :class:`TmuxUnavailable` when there is no usable tmux
    and :class:`TmuxError` when a command fails, so callers decide what a
    missing fleet means for them; nothing here prints.
    """

    def __init__(
        self,
        socket: str = DEFAULT_SOCKET,
        *,
        binary: str | None = None,
        conf: Path | None = None,
        runner: Runner | None = None,
    ) -> None:
        self.socket = socket
        self._binary = binary
        self._conf = conf
        self._runner: Runner = runner or _tmux
        #: The bundled conf once this instance has verified it on disk. A frame
        #: is one tmux command, and re-reading the file for each of twenty
        #: frames a second bought nothing: tmux reads it only when the server
        #: starts, and drift is fixed by the next process that starts one.
        self._conf_ready: Path | None = None

    # --- availability -----------------------------------------------------------

    def binary(self) -> str:
        """The tmux executable, or :class:`TmuxUnavailable`."""
        found = shutil.which(self._binary or "tmux")
        if found is None:
            raise TmuxUnavailable("tmux is not installed (or not on PATH) — the fleet needs it")
        return found

    def available(self) -> bool:
        try:
            self.binary()
        except TmuxUnavailable:
            return False
        return True

    def version(self) -> tuple[int, int] | None:
        """The server binary's version, or ``None`` when it cannot be read."""
        completed = self._runner([self.binary(), "-V"], None)
        return parse_version(completed.stdout) if completed.returncode == 0 else None

    def require(self) -> None:
        """Raise :class:`TmuxUnavailable` unless tmux is present and new enough.

        A version string that does not parse passes: refusing would lock a
        fork or a distro build with its own banner out of the fleet on a
        guess. What failing open costs is one confusing error later — an
        option the server rejects, a key it does not know — instead of a
        clear one here.
        """
        version = self.version()
        if version is not None and version < MIN_VERSION:
            raise TmuxUnavailable(
                f"tmux {version[0]}.{version[1]} is too old — the fleet needs "
                f"{MIN_VERSION[0]}.{MIN_VERSION[1]} or newer"
            )

    # --- running commands -----------------------------------------------------------

    def conf_path(self) -> Path:
        """The bundled configuration on disk, rewritten whenever it drifts."""
        if self._conf is not None:
            return self._conf
        if self._conf_ready is not None:
            return self._conf_ready
        path = paths.ensure_home() / CONF_NAME
        try:
            current = path.read_text(encoding="utf-8")
        except OSError:
            current = ""
        if current != BUNDLED_CONF:
            path.write_text(BUNDLED_CONF, encoding="utf-8")
        self._conf_ready = path
        return path

    def _argv_on(self, socket: str, conf: str, *args: str) -> list[str]:
        return [self.binary(), "-L", socket, "-f", conf, *args]

    def argv(self, *args: str) -> list[str]:
        """The full command line for one tmux invocation on this server."""
        return self._argv_on(self.socket, str(self.conf_path()), *args)

    def run(self, *args: str, stdin: bytes | None = None) -> str:
        """Run one tmux command (``;`` arguments chain several) and return stdout."""
        completed = self._runner(self.argv(*args), stdin)
        if completed.returncode != 0:
            raise TmuxError(completed.stderr.strip() or f"tmux {' '.join(args)} failed")
        return completed.stdout

    def check_conf(self, conf: Path | None = None) -> None:
        """Prove THIS tmux RUNS the fleet's configuration, or raise with its complaint.

        Two different failures, caught on one throwaway server in one command:

        * a configuration that KILLS the server. ``-f`` at server start reports
          nothing (it exits 0 whatever the file says), and the death does not
          come at start either: it comes when the first WINDOW is created —
          which is why the server here is started with the file under test and
          immediately asked for a session, and why a check that only sourced the
          file onto an empty server never saw ``window-size manual`` take tmux
          3.4 down (module docstring). A dead server answers
          ``server exited unexpectedly`` with status 1.
        * a configuration tmux does not ACCEPT. Those errors ``-f`` queues for
          the first attached client, which the fleet never has; ``source-file``
          on the now-running server reports them on stderr with status 1.

        The probe window then gets exactly what :meth:`spawn_window` gives a
        real one (:func:`_configure_window`), so the half of the configuration
        that is applied after a window is born is proved here rather than at
        the first spawn.

        The throwaway server is never the fleet's own, so re-sourcing a file it
        already read — which appends ``terminal-overrides`` a second time —
        costs nothing, and neither does the window running ``true``. It is
        never SHARED either: the socket carries a random per-call suffix
        (:data:`CHECK_SOCKET_SUFFIX`), so two checks racing on one fleet cannot
        meet. They used to — each cleared the fixed socket first, and whichever
        created its session second got ``duplicate session: asq-conf-check``,
        which this method can only report as tmux rejecting the user's
        configuration.

        Three layers get rid of that server again, because the socket name is
        random and only the first two of them know it:

        * the chain ENDS by killing its own session. With nothing left to hold
          the server up, tmux's default ``exit-empty on`` reaps it — measured on
          3.2, 3.2a, 3.3a, 3.4 and 3.5a, gone within 0.6s, where the same chain
          without that command left the session listed on every one of them. So
          the ordinary end of a check leaves no server even if this process is
          killed the instant the command returns, and the complaint still comes
          back intact: the ``kill-session`` is behind ``source-file`` and was
          measured not to disturb either the status or the stderr of a rejected
          conf.
        * :meth:`_discard_server` runs from a ``finally``, because a check that
          RAISED — a timeout, an OS refusal, a conf that stopped the chain
          before its last command — used to leave its server up for good.
        * :meth:`_reclaim_abandoned_probes` first, for the one case neither
          covers: a run killed without unwinding, between the server starting
          and the chain reaching its end. It runs BEFORE this call's own socket
          exists, so it can never sweep it.

        The doctor's "private server starts" check is exactly this call.
        """
        self._reclaim_abandoned_probes()
        path = conf if conf is not None else self.conf_path()
        socket = f"{self.socket}{CHECK_SOCKET_SUFFIX}-{secrets.token_hex(4)}"
        width, height = _CHECK_SIZE
        probe = [
            "new-session", "-d", "-s", _CHECK_SESSION, "-c", _CHECK_CWD,
            "-x", str(width), "-y", str(height), "--", *_CHECK_COMMAND,
        ]  # fmt: skip
        argv = self._argv_on(
            socket,
            str(path),
            *_chain(
                probe,
                *_configure_window(f"={_CHECK_SESSION}:", width, height),
                ["source-file", str(path)],
                ["kill-session", "-t", f"={_CHECK_SESSION}"],
            ),
        )
        try:
            completed = self._runner(argv, None)
        finally:
            self._discard_server(socket)
        complaint = completed.stderr.strip()
        if completed.returncode != 0 or complaint:
            raise TmuxError(complaint or f"tmux rejected {path} without saying why")

    def _reclaim_abandoned_probes(self) -> None:
        """Kill probe servers no run can name any more. Best effort, and bounded.

        :meth:`check_conf` gives each probe a socket no other call can pick, and
        records it nowhere: the moment the process holding it dies without
        unwinding, that server is unaddressable and — because the conf under
        test sets ``remain-on-exit on``, so the dead ``true`` pane keeps the
        session and ``exit-empty`` never fires — unable to end itself either.
        The socket FILE tmux left in ``/tmp/tmux-<uid>`` is the only handle on
        it left, and that is what this reads back.

        Three separate guards keep the sweep off things that are not that:

        * the name must START WITH ``<this fleet's socket>-check-`` as a literal
          ``str.startswith``, over ``iterdir()`` — never a ``glob()`` pattern
          with the socket name interpolated, which would turn a socket the user
          named ``asq[1]`` into a class matching another fleet's (see
          :data:`_PROBE_TAIL`) — and the rest must be the eight hex digits
          :meth:`check_conf` writes.
        * it must be older than :data:`_PROBE_ABANDONED_AFTER`, which is twice
          the longest a live check can possibly hold one. Without that, this
          would kill a CONCURRENT doctor's probe mid-check, which is the exact
          harm the random socket exists to prevent.
        * :data:`_SWEEP_BUDGET` caps the whole sweep, so ``check_conf`` stays
          flat rather than paying one ``kill-server`` timeout per stale socket.

        Nothing raised here reaches the caller: this runs before a check whose
        answer is about the user's CONFIGURATION, and litter in ``/tmp`` must
        never be reported as a bad conf.
        """
        prefix = f"{self.socket}{CHECK_SOCKET_SUFFIX}-"
        try:
            entries = sorted(self.socket_path().parent.iterdir())
        except (OSError, TmuxUnavailable):
            return
        abandoned_before = time.time() - _PROBE_ABANDONED_AFTER
        deadline = time.monotonic() + _SWEEP_BUDGET
        for entry in entries:
            name = entry.name
            if not name.startswith(prefix):
                continue
            if _PROBE_TAIL.fullmatch(name[len(prefix) :]) is None:
                continue
            try:
                if entry.stat().st_mtime > abandoned_before:
                    continue
            except OSError:
                continue
            if time.monotonic() >= deadline:
                return
            self._discard_server(name)

    def _discard_server(self, socket: str) -> None:
        """Take a throwaway server down and leave nothing of it behind. Best effort.

        Both steps are needed and neither may raise. ``kill-server`` is what
        ends a probe whose chain did not reach its own ``kill-session``: such a
        server holds a session whose pane the bundled ``remain-on-exit on``
        keeps after ``true`` returns, so ``exit-empty`` never fires and it would
        otherwise sit there for the life of the box (measured on 3.2 through
        3.5a — the session still listed 0.6s on, where the chain that ends in
        ``kill-session`` had already taken the server with it). Then the socket
        FILE, which tmux never unlinks (:meth:`kill_server`) and which a
        per-call socket name would otherwise leave in ``/tmp/tmux-<uid>`` once
        per check.

        Called for the socket this run made (from :meth:`check_conf`'s
        ``finally``) and for one an earlier run abandoned
        (:meth:`_reclaim_abandoned_probes`); ``no server running`` is the
        ordinary answer in both.

        Failure here is not the caller's business — ``no server running`` is an
        ordinary answer, a vanished socket file is the desired state — and it
        must never replace the complaint the check was called to report, which
        is why this runs from a ``finally`` and swallows everything tmux and
        the filesystem can raise.

        The ORDER is not a detail: the file goes only once the kill has come
        back with an answer, whatever that answer says. A kill that never
        completed (``_COMMAND_TIMEOUT``, an OS refusal) leaves a server that may
        still be alive, and unlinking the socket of a live server is how it
        becomes unreachable — nothing later can address it to kill it. A file
        left behind is only litter.
        """
        try:
            self._runner(self._argv_on(socket, os.devnull, "kill-server"), None)
        except (TmuxError, OSError):
            return
        with contextlib.suppress(TmuxError, OSError):
            (self.socket_path().parent / socket).unlink()

    # --- sessions and windows ---------------------------------------------------------

    def list_sessions(self) -> list[str]:
        """Session names on this server; empty when the server is not running."""
        completed = self._runner(self.argv("list-sessions", "-F", "#{session_name}"), None)
        if completed.returncode != 0:
            return []
        return [line for line in completed.stdout.splitlines() if line]

    def has_session(self, name: str) -> bool:
        completed = self._runner(self.argv("has-session", "-t", f"={name}"), None)
        return completed.returncode == 0

    def spawn_window(
        self,
        session: str,
        *,
        name: str,
        cwd: Path,
        command: Sequence[str],
        env: Mapping[str, str] | None = None,
        width: int = DEFAULT_WINDOW_SIZE[0],
        height: int = DEFAULT_WINDOW_SIZE[1],
    ) -> WindowInfo:
        """A new window named ``name`` running ``command`` — creating the session if needed.

        ``env`` pairs are set for the new window only (``-e``); the command is
        passed as separate arguments and executed directly, never through a
        shell, so nothing in it is re-interpreted. Refuses a ``session`` no
        other method here could target afterwards and a ``cwd`` that is not a
        directory (tmux would use ``$HOME`` silently).

        The window IS ``width`` x ``height`` when this returns, whether the
        session was created here or already existed, whether the fleet is
        headless or somebody is attached through the escape hatch on a terminal
        of any shape — and it stays that size until :meth:`resize`. That takes
        an explicit ``resize-window`` on the window's own ``@id``
        (:func:`_configure_window`): ``-x``/``-y`` on ``new-session`` is only a
        fallback tmux uses when no client exists, ``new-window`` on tmux 3.4 has
        no such flag, and the pin that keeps the size would otherwise be
        pinning the latest client's terminal (module docstring).

        All or nothing: if configuring the window fails — a busy box hitting
        ``_COMMAND_TIMEOUT``, the server dying between the two round trips — the
        window is destroyed before the error is raised, so a caller that catches
        :class:`TmuxError` and reports "nothing started" is telling the truth
        and no agent process is left running unsupervised. Configuring cannot be
        folded into the creating command: the pin needs the ``@id`` that command
        has not printed yet, and a target that does not name it exactly
        (``=session:``, whatever window is current) would size somebody else's
        agent.
        """
        _require_targetable(session)
        _require_directory(cwd)
        # tmux answers `width too small` to a 0 on either `new-session -x` or
        # `resize-window -x`, so clamp once, here, and the two commands below
        # cannot disagree about what was asked for. :meth:`resize` clamps the
        # same way for the same reason.
        width, height = max(1, width), max(1, height)
        env_flags = [
            flag for key, value in (env or {}).items() for flag in ("-e", f"{key}={value}")
        ]
        fmt = f"#{{window_id}}{_SEP}#{{pane_id}}"
        if self.has_session(session):
            # Nothing to undo before tmux answers: the session is somebody
            # else's and the window it just added has no name here yet.
            undo: list[str] | None = None
            out = self.run(
                "new-window", "-d", "-P", "-F", fmt, "-t", f"={session}:", "-n", name,
                "-c", str(cwd), *env_flags, "--", *command,
            )  # fmt: skip
        else:
            undo = ["kill-session", "-t", f"={session}"]
            out = self.run(
                "new-session", "-d", "-P", "-F", fmt, "-s", session, "-n", name,
                "-c", str(cwd), "-x", str(width), "-y", str(height), *env_flags,
                "--", *command,
            )  # fmt: skip
        window_id, separator, pane_id = out.strip().partition(_SEP)
        if not (window_id.startswith("@") and separator and pane_id.startswith("%")):
            # Not pedantry: an id this method cannot trust would send the
            # commands below to whatever window is current, and hand the caller
            # a WindowInfo it could never target. `@`/`%` is what every tmux
            # since 1.6 prints for these formats, so an answer without them
            # means something other than tmux answered.
            self._undo(undo)
            raise TmuxError(f"tmux did not name the new window and pane: {out!r}")
        try:
            self.run(*_chain(*_configure_window(window_id, width, height)))
        except TmuxError:
            # Kill the WINDOW, not the session: on the branch that created the
            # session this window is its only one and tmux takes the session
            # with it, and on the other branch the session is not ours to end.
            self._undo(["kill-window", "-t", window_id])
            raise
        return WindowInfo(
            session=session,
            window_id=window_id,
            name=name,
            pane_id=pane_id,
            dead=False,
            dead_status=None,
            current_command=command[0] if command else "",
            activity=False,
        )

    def _undo(self, command: Sequence[str] | None) -> None:
        """Remove what a half-finished :meth:`spawn_window` made. Best effort.

        ``None`` when there is nothing this method can name — a ``new-window``
        whose ``-P -F`` answer did not parse leaves a window with no id, and
        guessing at one (the session's current window, say) could kill an agent
        that is doing its job. Every real tmux prints that answer, so the case
        is a broken binary, not a race.

        Nothing raised here is allowed out: this runs while an error is already
        on its way to the caller and that error is the one worth reporting. What
        a failure costs is the very orphan this is here to prevent, so it is
        deliberately the second line of defence and not the first — the first is
        that only ONE command runs after the window exists.
        """
        if command is None:
            return
        with contextlib.suppress(TmuxError, OSError):
            self.run(*command)

    def list_windows(self, session: str) -> list[WindowInfo]:
        """Every window (one pane each) of ``session``; empty when it does not exist."""
        completed = self._runner(
            self.argv("list-panes", "-s", "-t", f"={session}", "-F", _WINDOW_FORMAT), None
        )
        if completed.returncode != 0:
            return []
        windows: list[WindowInfo] = []
        for line in completed.stdout.splitlines():
            fields = line.split(_SEP)
            if len(fields) != len(_WINDOW_FIELDS):
                continue
            values = dict(zip(_WINDOW_FIELDS, fields, strict=True))
            windows.append(
                WindowInfo(
                    session=session,
                    window_id=values["window_id"],
                    name=values["window_name"],
                    pane_id=values["pane_id"],
                    dead=values["pane_dead"] == "1",
                    dead_status=_optional_int(values["pane_dead_status"]),
                    current_command=values["pane_current_command"],
                    activity=values["window_activity_flag"] == "1",
                )
            )
        return windows

    def pane_facts(self, pane_id: str) -> PaneFacts | None:
        """The pane's facts, or ``None`` when the pane is gone.

        Gone is two shapes: an exit status (a server that is not running) and
        tmux 3.7c's own answer for an unknown pane — status 0 with every field
        empty, because ``display-message`` is allowed to miss its target. A
        client attached elsewhere could make it fall back to ITS current pane,
        so the answer must also be about the pane that was asked for.
        """
        completed = self._runner(
            self.argv("display-message", "-p", "-t", pane_id, _FACTS_FORMAT), None
        )
        if completed.returncode != 0:
            return None
        facts = _facts(completed.stdout.rstrip("\n"))
        if not facts.pane_id or (pane_id.startswith("%") and facts.pane_id != pane_id):
            return None
        return facts

    def kill_window(self, pane_id: str) -> None:
        """Kill the window holding ``pane_id`` (a dead pane included)."""
        self.run("kill-window", "-t", pane_id)

    def kill_session(self, session: str) -> None:
        self.run("kill-session", "-t", f"={session}")

    def kill_server(self) -> None:
        """Stop the whole private server — every session and every agent in it.

        The socket FILE stays behind (tmux never unlinks it; a new server on the
        name replaces it). :meth:`socket_path` says where, for whoever tidies.
        """
        self.run("kill-server")

    def socket_path(self) -> Path:
        """Where tmux keeps this server's socket, up or down.

        tmux's own rule (``make_label``): ``$TMUX_TMPDIR`` when set, else
        ``/tmp``; then ``tmux-<uid>/<socket>``. Reproduced here because tmux
        answers ``#{socket_path}`` only while the server is running, and the
        questions worth asking — is a stale file left over, does the path fit
        an AF_UNIX address (about 100 bytes) — come up when it is not.

        :class:`TmuxUnavailable` where there is no uid (Windows outside WSL):
        the fleet modules must import there (plan §3.9), but there is no tmux
        and so no socket to point at.
        """
        getuid = getattr(os, "getuid", None)
        if getuid is None:
            raise TmuxUnavailable("no tmux socket on this platform — the fleet needs a POSIX host")
        base = os.environ.get("TMUX_TMPDIR") or "/tmp"
        return Path(base) / f"tmux-{getuid()}" / self.socket

    def rename_session(self, old: str, new: str) -> None:
        _require_targetable(new)
        self.run("rename-session", "-t", f"={old}", new)

    def attach_argv(self, session: str) -> list[str]:
        """The command a terminal runs to attach to ``session`` (the escape hatch)."""
        return self.argv("attach-session", "-t", f"={session}")

    # --- the screen -------------------------------------------------------------------

    def capture(self, pane_id: str, *, scrollback: int = 0, height: int | None = None) -> Capture:
        """One frame of ``pane_id`` — the rows with SGR escapes, plus the pane's facts.

        ``scrollback`` is how many history lines above the live screen the
        frame should start at; the caller clamps it to ``facts.history_size``
        and the result reports the offset tmux actually honoured. One process:
        ``capture-pane`` then ``display-message`` via a ``;``.

        ``height`` is the pane height the caller last saw. With it, a scrolled
        frame is bounded to one screen (``-S -k -E H-1-k``, §6) instead of
        history-to-bottom — the difference between 60 rows and 50 000 when the
        user is reading the top of a long run. A stale hint (the pane grew,
        or history shrank under a clear) yields a short frame, which is
        detected and refetched unbounded — one extra process, only then.
        """
        scrollback = max(0, scrollback)
        bound = height if scrollback and height is not None and height > 0 else None
        rows, facts = self._frame(pane_id, scrollback, bound)
        if bound is not None and len(rows) < facts.height:
            rows, facts = self._frame(pane_id, scrollback, None)
        effective = min(scrollback, facts.history_size)
        return Capture(lines=rows[: facts.height], facts=facts, scrollback=effective)

    def _frame(
        self, pane_id: str, scrollback: int, height: int | None
    ) -> tuple[list[str], PaneFacts]:
        window = ["-E", str(height - 1 - scrollback)] if height is not None else []
        out = self.run(
            "capture-pane", "-p", "-e", "-N", "-S", str(-scrollback), *window, "-t", pane_id,
            ";", "display-message", "-p", "-t", pane_id, _FACTS_FORMAT,
        )  # fmt: skip
        body = out.rstrip("\n").split("\n")
        return body[:-1], _facts(body[-1])

    # --- input --------------------------------------------------------------------------

    def send_keys(self, pane_id: str, *keys: str) -> None:
        """Named keys (``Enter``, ``C-c``, ``BTab``…) — tmux's own vocabulary."""
        if keys:
            self.run("send-keys", "-t", pane_id, *keys)

    def send_literal(self, pane_id: str, text: str) -> None:
        """Literal text, exactly as typed (``-l``), even when it starts with ``-``.

        A TRAILING ``;`` is tmux's command separator even after ``-l`` — measured
        on 3.7c: ``send-keys -l -- ';'`` sends nothing and ``'a;'`` sends ``a``,
        while ``'a\\;'`` arrives as ``a;``. So the last ``;`` is escaped.
        """
        if text:
            if text.endswith(";"):
                text = text[:-1] + "\\;"
            self.run("send-keys", "-t", pane_id, "-l", "--", text)

    def paste(self, pane_id: str, text: str) -> None:
        """Bracketed paste: the agent sees one paste, not one Enter per line."""
        if not text:
            return
        self.run("load-buffer", "-b", PASTE_BUFFER, "-", stdin=text.encode("utf-8"))
        self.run("paste-buffer", "-p", "-d", "-b", PASTE_BUFFER, "-t", pane_id)

    def resize(self, pane_id: str, width: int, height: int) -> None:
        """Size the pane's window to the pane the UI is showing it in.

        The same command :func:`_configure_window` runs at birth, and it carries
        the same side effect: ``cmd-resize-window`` writes ``manual_sx``/
        ``manual_sy`` and sets that window's ``window-size`` to ``manual``. So
        this both moves the window and re-establishes the pin that keeps it
        there — a window the UI has resized once is no more exposed to an
        attaching client than one it has not.
        """
        self.run(
            "resize-window", "-t", pane_id, "-x", str(max(1, width)), "-y", str(max(1, height))
        )
