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
The whole identity goes — ``core.spawn.IDENTITY_ENV_VARS``, the headers AND
every name in ``core.spawn.MARKER_ENV_VARS`` (read off the tuple, never listed
here: it has grown once already): an agent that launches untraced (the default)
keeps whatever marker it inherited, and that marker alone is what
``core.insights.run_key`` and the hook's session→Run join file records under.
docs/fleet.md's "the tmux server inherits nothing of a tracing identity from
whoever started it" is that sentence's contract.

Verified against tmux 3.7c (``tests/test_tmux.py`` re-verifies the live ones):

* ``new-session``/``new-window`` accept ``-e KEY=VALUE`` and a multi-argument
  command after ``--``; ``send-keys -l --`` takes literal text that starts with
  ``-``; ANY argument ending in ``;`` separates commands in one invocation —
  ``--`` does not stop that — and an error in one command stops the chain with
  exit status 1. A backslash before that final ``;`` escapes it, which is how
  :func:`_data_arg` keeps caller data out of tmux's command grammar.
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
import itertools
import os
import re
import shutil
import socket as socket_module
import subprocess
import sys
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from aisquare.core import paths
from aisquare.core.spawn import untraced_env

DEFAULT_SOCKET = "asq"
MIN_VERSION = (3, 2)
"""``new-window -e`` and ``extended-keys`` arrived in 3.2; ``S-Enter`` needs 3.5
(3.3/3.4 type extended-only chords literally — ``core.keys.EXTENDED_MINIMUM``)."""
PASTE_BUFFER = "asq-paste"
"""PREFIX for a paste buffer; :meth:`TmuxServer.paste` names each call's own.

ONE fixed name was a cross-pane leak. A paste is TWO invocations — ``load-buffer``
then ``paste-buffer`` — and nothing serialises them across processes: two
``fleet tell``s, a ``fleet spawn --prompt`` beside a ``tell``, the UI's
``on_paste`` beside either. Interleaved as load(A), load(B), paste(A) tmux hands
pane A the text addressed to agent B — and the UI's ``Enter`` submits it —
while B's own paste then fails with ``no buffer asq-paste``, because ``-d``
deleted the one buffer. Verified on 3.7c, and
``test_live_two_pastes_at_once_reach_the_pane_they_were_addressed_to`` keeps
both halves: the fix, and the old behaviour as the control on the mechanism.
"""

#: Per-call suffix, so no two ``load``/``paste`` pairs can cross. The pid
#: separates processes; the counter separates calls within one (``next`` on an
#: ``itertools.count`` is a single C step, so the UI's threads cannot collide on
#: it). A pid is reused only after its process is gone, and ``load-buffer``
#: overwrites, so a leftover from a crashed paste cannot be delivered.
_PASTE_SEQUENCE = itertools.count()
CONF_NAME = "fleet-tmux.conf"
CHECK_SOCKET_SUFFIX = "-check"
"""Where :meth:`TmuxServer.check_conf` starts its throwaway server: ``<socket>-check``."""
_COMMAND_TIMEOUT = 30.0
_ASK_TIMEOUT = 5.0
"""How long a question an upgrade or uninstall plan puts to a server waits for its answer
(:func:`asking_server`): a wedged server (it takes the connect and never answers) held the
plan :data:`_COMMAND_TIMEOUT` per socket, where the socket's own probe gave up after 2 s."""

#: The bundled server configuration. Applied with ``-f`` when the server starts
#: (harmless afterwards). Every line has a reason: docs/plans/fleet-tui.md §3.1.
#: Every option is verified to load, with this value, on tmux 3.7c by
#: ``tests/test_tmux.py::test_live_every_bundled_option_is_applied``.
BUNDLED_CONF = """\
# aisquare fleet — private tmux server configuration (regenerated; do not edit)
set -g status off
set -s escape-time 0
set -g history-limit 50000
# window-size manual is DELIBERATELY absent. tmux 3.4 (ubuntu-24.04, the CI
# runner) crashes on it: in the startup conf the server dies before answering
# ("server exited unexpectedly"), and even a live `set -g window-size manual`
# kills the server on the next window operation — both measured in an
# ubuntu-24.04 container; 3.7c is fine either way. It is also not needed:
# `resize-window` (TmuxServer.resize) pins THAT window to manual sizing itself,
# and -x/-y at session creation set the detached default size.
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
# No prefix key (#147). `fleet attach` is a raw tmux client, and tmux's default
# C-b is Claude Code's "background running tasks": every key goes to the agent,
# and F12 detaches the client — the same key that hands focus back to the
# sidebar in the UI. The UI's own path (`send-keys`) never met the prefix.
set -g prefix None
set -g prefix2 None
bind-key -n F12 detach-client
# A new client's desktop lands in the session's environment (tmux's default
# list has DISPLAY and the SSH agent; these are the rest of what a re-login
# changes), so windows made after `fleet attach` see the current display, bus
# and colour facts. The UI's spawns carry them per window as well (#147,
# services.fleet — a window inherits the SERVER's environment otherwise).
set -ga update-environment WAYLAND_DISPLAY
set -ga update-environment XDG_RUNTIME_DIR
set -ga update-environment DBUS_SESSION_BUS_ADDRESS
set -ga update-environment COLORTERM
set -ga update-environment TERM_PROGRAM
"""

#: What a re-login changes and a window inherits stale from the server it was
#: spawned on (#147): the display (image paste, notifications, `xdg-open`), the
#: user bus, the runtime dir, the SSH agent socket, and the two facts Claude Code
#: reads about the terminal's colour and make. Set on each window at spawn
#: (``new-window -e``) from the SPAWNER's environment; also the names the
#: bundled conf adds to ``update-environment`` for ``fleet attach``.
DESKTOP_ENV_VARS: tuple[str, ...] = (
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "SSH_AUTH_SOCK",
    "COLORTERM",
    "TERM_PROGRAM",
)


def desktop_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The :data:`DESKTOP_ENV_VARS` this process has, to set on a window it spawns.

    Only the variables that ARE set travel: an unset one here says nothing about
    the server's copy (a headless spawn from a cron job must not blank the
    display of an agent started from a desktop), and ``-e`` can only set.
    """
    source = os.environ if environ is None else environ
    return {name: source[name] for name in DESKTOP_ENV_VARS if source.get(name, "").strip()}


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
    "mouse_any_flag",
    "mouse_sgr_flag",
    "mouse_button_flag",
    "mouse_all_flag",
    "start_time",
    "window_activity",
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
    "start_time",
)
_WINDOW_FORMAT = _SEP.join(f"#{{{name}}}" for name in _WINDOW_FIELDS)
_VERSION = re.compile(r"(\d+)\.(\d+)")
#: The first tmux whose ``capture-pane`` takes ``-F`` (a flags column before each
#: line: ``-`` none, ``W`` wrapped into the next, ``X`` extended cells, …).
#: Measured on 3.7c, where ``-p -e -N -F`` prints ``W <escapes><text>`` per row;
#: 3.4 and 3.5 document no such flag, and tmux's CHANGES file does not date it,
#: so the gate is the version it was measured on. A server below it is asked
#: for plain frames: an unknown flag fails the whole capture, and a frame is
#: worth more than a wrap mark.
WRAP_FLAGS_MINIMUM: tuple[int, int] = (3, 7)
_ABSENT = re.compile(r"no server running on |error connecting to .*\(No such file or directory\)")
"""tmux's two ways of saying there is no server behind a socket (see ``server_absent``)."""
#: Characters tmux reads as target separators; a session named with one can be
#: created but never addressed by name again (``=a.b`` → "can't find pane: b").
_UNTARGETABLE = frozenset(".:")

#: tmux's command separator. It is not a shell feature and ``--`` does not stop
#: it: tmux itself ends a command at ANY argument whose LAST character is this —
#: an argument that is only ``;`` included. Measured on 3.7c — ``new-window -d --
#: sh -c 'sleep 60' ';' set -g history-limit 1`` exits 0 and ``show -g history-limit`` says
#: ``1``; with ``'arg;'`` in place of the lone ``;`` the same thing happens, and
#: with ``kill-server`` in place of ``set`` the private server (every agent on
#: it) dies. So every argument that carries CALLER data is passed through
#: :func:`_data_arg` first.
_ARGV_SEPARATOR = ";"

_HEX_CHUNK = 512
"""Bytes per ``send-keys -H`` call — one argument each, so a paste is cut up.

tmux 3.7 refuses a command of more than 1000 arguments ("Limit MSG_COMMAND
argument to between 0 and 1000", CHANGES 3.6b to 3.7) with "command too long",
and ``send-keys -t <pane> -H`` spends four of them: measured on 3.7c, 996 bytes
go and 997 do not, so chunks of 1024 failed every paste of 997 bytes or more.
512 leaves room for a flag added to the call later. Older tmux has no such
limit, CI's included, so ``tests/test_tmux.py`` counts the arguments instead."""


class TmuxError(RuntimeError):
    """A tmux command failed; the message carries tmux's own stderr."""


class TmuxUnavailable(TmuxError):
    """No usable tmux: missing from PATH, or older than :data:`MIN_VERSION`."""


class TmuxTimedOut(TmuxError):
    """The server did not answer in time (:func:`_tmux`). Its client is killed, but what it
    asked may be queued in a server that is wedged, which runs it once it recovers: unlike a
    refusal, it may still happen."""


class TmuxPartlyTyped(TmuxError):
    """A write of several tmux calls that failed after its first went through
    (:meth:`TmuxServer.send_bytes`): ``typed`` of its ``size`` bytes reached the pane, and
    ``cause`` is how the next call failed. The calls after that one were never sent, so of
    the rest only the chunk a :class:`TmuxTimedOut` asked for may still arrive. Taken for a
    plain timeout, it was "the text may still arrive", true of a prefix only; taken for a
    refusal, "nothing was typed" (review of #259, round 11, 7/13)."""

    def __init__(self, cause: TmuxError, typed: int, size: int) -> None:
        super().__init__(f"{cause} (after {typed} of its {size} bytes)")
        self.cause, self.typed, self.size = cause, typed, size


#: How tmux says a LIVE server has no such session/window/pane — a benign
#: "not there", distinct from the server being GONE (:data:`_ABSENT`) and from a
#: socket that refused the probe. Measured on 3.7c: ``can't find session: NAME``
#: (``has-session``) and ``can't find window: NAME`` (``list-panes``); a refusal
#: reads ``(Permission denied)`` and matches neither, so a strict query raises.
_NO_TARGET = re.compile(r"can't find ")


@dataclass(frozen=True)
class Completed:
    """One finished tmux command."""

    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], bytes | None], Completed]
"""How commands run: the real :func:`_tmux`, or a fake in tests."""


def _tmux(argv: Sequence[str], stdin: bytes | None, *, timeout: float | None = None) -> Completed:
    """THE seam: run one tmux invocation with the tracing identity stripped.

    Every way the process can fail to run at all is mapped onto the module's
    own exceptions, so a caller that catches :class:`TmuxError` has caught
    everything: a binary that vanished between ``which`` and ``exec`` is
    :class:`TmuxUnavailable`; a server that stops answering (``timeout``,
    :data:`_COMMAND_TIMEOUT` unless given) is :class:`TmuxTimedOut`, and any other
    OS refusal :class:`TmuxError`.
    """
    timeout = _COMMAND_TIMEOUT if timeout is None else timeout
    try:
        result = subprocess.run(
            list(argv),
            input=stdin,
            capture_output=True,
            env=untraced_env(),
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise TmuxUnavailable(f"tmux is not runnable: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise TmuxTimedOut(
            f"tmux did not answer within {timeout:.0f}s: {' '.join(argv[:6])} …"
        ) from exc
    except OSError as exc:
        raise TmuxError(f"could not run tmux: {exc}") from exc
    return Completed(
        returncode=result.returncode,
        stdout=result.stdout.decode("utf-8", errors="replace"),
        stderr=result.stderr.decode("utf-8", errors="replace"),
    )


#: Where Linux names this boot: a random UUID the kernel draws as it boots.
_BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
#: Where macOS keeps ``sysctl``, for a PATH without ``/usr/sbin`` (launchd, cron).
_SYSCTL = ("/usr/sbin/sysctl", "/sbin/sysctl")
#: Where Linux says when it booted (``btime``, seconds since the epoch).
_PROC_STAT = Path("/proc/stat")


#: This boot's identity once :func:`boot_id` has found it; ``None`` until then.
_boot: str | None = None


def boot_id() -> str | None:
    """This boot's identity, or ``None`` where the machine keeps none.

    No process outlives a reboot, so a fleet row recorded in another boot
    (``FleetAgent.boot_id``) is no running agent. An identity, not a time:
    Linux's ``btime`` is the wall clock less the time since boot, and moves
    whenever the wall clock is stepped (a WSL2 VM resumed after the host slept,
    NTP on a board with no clock), so rows from before the step read as from
    before the boot (round 25 of #257). Linux draws ``boot_id`` at boot; macOS
    keeps ``kern.bootsessionuuid``, asked of ``sysctl`` on PATH or where macOS
    keeps it. The BSDs keep only a time, which moves the same way: ``None``.

    Kept for the process once found, since the boot cannot change while it runs: on macOS
    each question is a ``sysctl`` run, and asq asked once per spawn and again in each
    upgrade or uninstall plan (round 26 of #257). Only once found: asq is one process that
    spawns every agent, and a ``sysctl`` that failed or timed out once, kept, left every
    later spawn with no boot recorded (round 28).
    """
    global _boot
    if _boot is None:
        _boot = _ask_boot_id()
    return _boot


def _ask_boot_id() -> str | None:
    """:func:`boot_id`, asked of the machine."""
    with contextlib.suppress(OSError, UnicodeDecodeError):
        said = _BOOT_ID.read_text(encoding="ascii").strip()
        if said:
            return said
    if sys.platform != "darwin":
        return None
    said = _sysctl("kern.bootsessionuuid")
    return said or None


def _sysctl(name: str) -> str | None:
    """What ``sysctl -n <name>`` says, from ``sysctl`` on PATH or where macOS keeps it;
    ``None`` when it cannot be asked or does not answer."""
    sysctl = shutil.which("sysctl") or next(
        (place for place in _SYSCTL if os.access(place, os.X_OK)), None
    )
    if sysctl is None:
        return None
    try:
        completed = subprocess.run(
            [sysctl, "-n", name], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


#: When this boot started once :func:`boot_started` has found it; ``None`` until then.
_booted_at: datetime | None = None


def boot_started() -> datetime | None:
    """When this machine booted, by the wall clock, where it says (Linux's ``btime``, the
    ``kern.boottime`` of macOS and the BSDs); ``None`` elsewhere. Kept once found, as
    :func:`boot_id` is.

    Not an identity: it is the wall clock less the time since boot, so it moves whenever
    the clock is stepped (a WSL2 VM resumed after the host slept, NTP on a board with no
    clock), and a row from before the step reads as from before the boot (round 25 of
    #257). :func:`server_may_run` asks it only for a row with no boot to compare.
    """
    global _booted_at
    if _booted_at is None:
        _booted_at = _ask_boot_started()
    return _booted_at


def _ask_boot_started() -> datetime | None:
    """:func:`boot_started`, asked of the machine."""
    with contextlib.suppress(OSError, UnicodeDecodeError, ValueError, IndexError):
        for line in _PROC_STAT.read_text(encoding="ascii").splitlines():
            if line.startswith("btime "):
                return datetime.fromtimestamp(int(line.split()[1]), tz=UTC)
    if sys.platform.startswith(("linux", "win")):
        return None  # Linux said above, or could not; Windows keeps no tmux
    found = re.search(r"\bsec = (\d+)", _sysctl("kern.boottime") or "")  # { sec = 17…, usec
    return datetime.fromtimestamp(int(found.group(1)), tz=UTC) if found else None


SocketAnswer = Literal["answers", "gone", "unasked", "another"]
"""What a server's socket file says when asked directly (:func:`socket_answer`): a listener
(``"answers"``), no file there (``"gone"``), or no answer to be had (``"unasked"``); and
what :func:`server_may_run` adds, a listener that is not the server the row was started on
(``"another"``)."""


def socket_answer(path: Path | None) -> SocketAnswer | None:
    """Whether a server may be listening on the socket at ``path``, asked of the socket
    itself (no tmux needed), and why. ``None`` is a host with no POSIX uid, where no tmux
    server can exist.

    ``None`` only on evidence that none is, because the cost of being wrong is ending the
    row of a running agent, or removing hooks (with ``uninstall --purge``, the home) under
    it: a file nobody listens on. tmux leaves its socket file when its server exits (tmux
    3.7, measured), so a stale file is that evidence. No file is not (``"gone"``):
    something else took it while the server may run on, systemd-tmpfiles ageing out
    ``/tmp/tmux-<uid>``, logind removing ``/run/user/<uid>`` at logout, or a private
    ``/tmp`` (round 26 of #257). Neither is a refusal to look, a denied directory, or a
    connect that timed out (``"unasked"``).
    """
    family = getattr(socket_module, "AF_UNIX", None)
    if path is None or family is None:
        return None  # no POSIX uid or no AF_UNIX, so no tmux server can exist here
    probe = socket_module.socket(family, socket_module.SOCK_STREAM)
    probe.settimeout(2.0)
    try:
        probe.connect(str(path))
    except ConnectionRefusedError:
        return None
    except FileNotFoundError:
        return "gone"
    except OSError:
        return "unasked"
    finally:
        probe.close()
    return "answers"


class SocketReads:
    """What :func:`server_may_run` asks of the machine for one read of the fleet (a plan, a
    run, a sweep, a listing), each asked at most once: a socket's listener, and when the
    server that answers there started, per socket; and this boot's identity and start.

    The answers belong to the server, not to the row, and every caller asks them row by row
    over rows that share a server. With ten live agents on one answering server, a plan ran
    ten ``display-message`` processes and ten socket connects for one answer, and its run
    as many again (review of #259, round 3). A start that could not be asked is ``None``
    here too, as :func:`server_may_run` has always judged it.
    """

    def __init__(self) -> None:
        self._listeners: dict[object, SocketAnswer | None] = {}
        self._starts: dict[object, datetime | None] = {}
        self._booted: tuple[datetime | None] | None = None
        self._boot: tuple[str | None] | None = None

    @staticmethod
    def _key(srv: TmuxServer, recorded: Path | None) -> object:
        """The socket asked: the path a row recorded, else where ``-L`` finds ``srv``'s."""
        return recorded if recorded is not None else (srv.socket, srv.path)

    def listener(self, srv: TmuxServer, recorded: Path | None) -> SocketAnswer | None:
        """:meth:`TmuxServer.listener` at ``recorded``, asked once per socket."""
        key = self._key(srv, recorded)
        if key not in self._listeners:
            self._listeners[key] = srv.listener(recorded)
        return self._listeners[key]

    def started(self, srv: TmuxServer, recorded: Path | None) -> datetime | None:
        """When the server answering at ``recorded`` started, asked once per socket; ``None``
        when it could not be asked (no usable tmux, a denied socket)."""
        key = self._key(srv, recorded)
        if key not in self._starts:
            try:
                self._starts[key] = (srv if recorded is None else srv.at(recorded)).started_at()
            except TmuxError:  # TmuxUnavailable included: a server listens, and may be the row's
                self._starts[key] = None
        return self._starts[key]

    def heard_listener(
        self, srv: TmuxServer, recorded: Path | None, answer: SocketAnswer | None
    ) -> None:
        """Keep what the socket at ``recorded`` said, as a caller read it already, so
        :meth:`listener` does not ask it again."""
        self._listeners.setdefault(self._key(srv, recorded), answer)

    def heard_started(self, srv: TmuxServer, recorded: Path | None, started: datetime) -> None:
        """Keep when the server answering at ``recorded`` started, as an answer that named one
        of its panes said it (``#{start_time}``), so :meth:`started` does not ask it again."""
        self._starts.setdefault(self._key(srv, recorded), started)

    def boot_started(self) -> datetime | None:
        """:func:`boot_started`, asked once."""
        if self._booted is None:
            self._booted = (boot_started(),)
        return self._booted[0]

    def boot_id(self) -> str | None:
        """:func:`boot_id`, asked once: it keeps only an id it found (round 28 of #257), so
        where ``sysctl`` fails each question is another ``sysctl`` run, with a 5 s timeout."""
        if self._boot is None:
            self._boot = (boot_id(),)
        return self._boot[0]


def server_may_run(
    srv: TmuxServer,
    recorded: Path | None,
    booted: str | None,
    created: datetime | None = None,
    started: datetime | None = None,
    *,
    reads: SocketReads | None = None,
) -> SocketAnswer | None:
    """Whether the agent of a live fleet row may still be running on ``srv``, and why; ``None``
    when it cannot be. The one rule ``upgrade``, ``uninstall``, ``fleet shutdown``, ``stop``
    and ``reap --server-down`` ask, so a row one of them counts as running is never one
    another ends as gone (round 27 of #257).

    Asked of what its spawn recorded, never inferred from the shell asking (round 25). A row
    from another boot (``booted``, against this one's, :meth:`SocketReads.boot_id`) cannot
    be running: no process outlives a reboot. Otherwise its server's socket is asked
    (:meth:`TmuxServer.listener`) at ``recorded``, where the server said it listened,
    whatever ``TMUX_TMPDIR`` this shell has and wherever this tmux build keeps sockets; a
    row from before v25 recorded neither and is asked where this shell's tmux looks.

    A listener there is not thereby the row's server. One that lost its socket file runs
    on, and the next ``tmux -L`` there starts a second server at the freed path, which
    answers for the row from then on while its agent runs where no client reaches it
    (follow-up of #257). So the server that answers is asked when it started
    (:meth:`TmuxServer.started_at`) and compared with ``started``, what the row's server
    said at spawn (``FleetAgent.server_started_at``): another time is ``"another"``, which
    ``stop`` refuses, ``shutdown`` holds until ``--forget``, and ``uninstall`` counts. A
    row that recorded no start, or a start that cannot be asked now (no usable tmux, a
    denied socket), leaves ``"answers"`` standing: a server listens, which may be its.

    With no boot to compare (a row from before v25, or a boot id that could not be had: the
    BSDs, a macOS whose ``sysctl`` failed), a socket the row's server no longer holds, gone
    or taken by ``"another"``, is explained by a reboot when the row was ``created`` before
    this boot started (:func:`boot_started`): a reboot clears ``/tmp``, and the first spawn
    after it starts the server that answers there now. Without it, every row a 0.7.0 fleet
    left behind at a reboot was held until ``--forget`` (round 28), and so was every row
    after a reboot on such a host, against the next server (review of #259, round 1). That
    start moves with the clock, so it explains only a socket the row's server lost, never
    one that answers as the row's or could not be asked: a row misjudged by a stepped
    clock is ended only when its server's socket is gone as well.

    ``reads`` is the caller's :class:`SocketReads` for one read of the fleet, so rows on
    one server ask it once; each call asks afresh without one. This boot is asked there too,
    and only for a row that recorded one: callers asked it of :func:`boot_id` themselves, for
    every read, beside the ``reads`` that asks it too, so one read could get two answers
    where ``sysctl`` fails now and then (review of #259, round 7, 5/9).
    """
    reads = reads or SocketReads()
    this_boot = None if booted is None else reads.boot_id()
    if booted is not None and this_boot is not None and booted != this_boot:
        return None
    answer = reads.listener(srv, recorded)
    if answer == "answers" and started is not None:
        now = reads.started(srv, recorded)
        if now is not None and now != started:
            answer = "another"
    unbooted = booted is None or this_boot is None
    if answer in ("gone", "another") and unbooted and created is not None:
        booted_at = reads.boot_started()
        if booted_at is not None and created < booted_at:
            return None
    return answer


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
    mouse_on: bool = False
    """The program in the pane has turned mouse reporting on (``?1000``/``?1002``/
    ``?1003``) — it wants the wheel itself. Claude Code's fullscreen TUI does."""
    mouse_sgr: bool = False
    """…and asked for SGR encoding (``?1006``), the form every modern program uses."""
    mouse_drag: bool = False
    """…and asked for motion while a button is held (``?1002``, button-event
    tracking) or for every motion (``?1003``). Without either, a program gets
    presses and releases only, and a drag forwarded to it would be a report it
    never asked for (#148)."""
    server_started: datetime | None = None
    """When the server that answered started (``#{start_time}``), as
    :meth:`TmuxServer.started_at` says it, and ``None`` when tmux does not say.
    Asked in the same command as the rest, so a frame says which server's
    lifetime its pane id belongs to without a second process: the remote's live
    stream asked separately, once per watched pane per tick."""
    last_output: datetime | None = None
    """When the pane's window last printed (``#{window_activity}``), to the second rounded
    down; ``None`` when tmux does not say. Asked with the rest for the same reason: the
    remote's needs-you asked whether a pane was quiet in a third process, after two for
    the facts and the start, on every quarter-second poll of an agent action."""


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
    wrapped: list[bool] | None = None
    """Per row of ``lines``, whether tmux soft-wrapped it into the row below —
    the ``W`` of ``capture-pane -F`` — or ``None`` when the flags were not asked
    for (:meth:`TmuxServer.capture` with ``flags=False``, the default). Read
    with the frame they describe, so a copy that joins wrapped rows joins the
    rows it shows and never rows a later screen would have shown.
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
    resize_refused: str | None = None
    """Set by :meth:`TmuxServer.spawn_window` alone: tmux's words when it refused
    the resize that gives a window added to an existing session its geometry, so
    the window runs at the session's size (#149). ``None`` when that resize
    landed, when there was none to make, and on every window a listing reports.
    The spawn fails open on it; this is how the refusal still reaches the
    caller's receipt (review of #162, round 1)."""
    server_started: datetime | None = None
    """When the server that listed the window started (``#{start_time}``), as
    :meth:`TmuxServer.started_at` says it, ``None`` when tmux does not say: asked in the same
    ``list-panes``, as :attr:`PaneFacts.server_started` is, so a listing says which server's
    lifetime its pane ids belong to without a second process (review of #259, round 3)."""


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


def _epoch(epoch: str) -> datetime | None:
    """A time as tmux prints one (``#{start_time}``, ``#{window_activity}``), whole seconds
    since the epoch; ``None`` for anything else, a tmux too old to know the variable (an
    empty answer) included."""
    epoch = epoch.strip()
    return datetime.fromtimestamp(int(epoch), tz=UTC) if epoch.isdigit() else None


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
        mouse_on=values["mouse_any_flag"] == "1",
        mouse_sgr=values["mouse_sgr_flag"] == "1",
        mouse_drag=values["mouse_button_flag"] == "1" or values["mouse_all_flag"] == "1",
        server_started=_epoch(values["start_time"]),
        last_output=_epoch(values["window_activity"]),
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


def _data_arg(value: str) -> str:
    """One argument tmux must read as DATA, never as the end of the command.

    A trailing ``;`` is tmux's own separator (:data:`_ARGV_SEPARATOR`) and a
    single backslash before it escapes it: measured on 3.7c, ``-- sh -c … 'a\\;'``
    reaches the program as ``a;`` and nothing after it is run as a tmux command.
    Only the LAST character matters, so ``a;b`` is already data and is untouched.
    """
    if value.endswith(_ARGV_SEPARATOR):
        return value[:-1] + "\\" + _ARGV_SEPARATOR
    return value


DEFAULT_WINDOW_WIDTH = 120
DEFAULT_WINDOW_HEIGHT = 40
"""The geometry a window is born with when nobody says (#149).

It was 200x50, and the number that matters is 144: Claude Code's fullscreen
renderer opens its diff panel BY ITSELF "once Claude starts editing files, if
your terminal is at least 144 columns wide" (interactive-mode reference, "Diff
panel"), and remembers "opened once → open on every edit" for the session and
later ones. Every headless spawn — a manager starting coders, a `fleet spawn`
with no UI open — therefore ran wide enough to grow a panel the UI could
neither close (clicks are not forwarded, #148) nor reach with `/diff` while
Claude was busy (typed input is queued). 120 keeps a headless window under that
line and above 110, the width Claude Code needs to open the panel ON DEMAND
(`/diff`), so nothing is lost; a window the UI shows adopts the pane's real
size on first attach (``TerminalPane._sync_size``), as before.
"""


def _tmux_asking(argv: Sequence[str], stdin: bytes | None) -> Completed:
    """:func:`_tmux`, given :data:`_ASK_TIMEOUT` (:func:`asking_server`)."""
    return _tmux(argv, stdin, timeout=_ASK_TIMEOUT)


def asking_server(socket: str = DEFAULT_SOCKET) -> TmuxServer:
    """A handle on ``socket`` for what the upgrade and uninstall plans ask of it, which write
    nothing and are put quickly.

    Every command of a :class:`TmuxServer` hands tmux its conf (:meth:`TmuxServer.conf_path`),
    which makes the aisquare home and rewrites a conf that drifted: a plan that asked a
    server when it started (:func:`server_may_run`) could do both, as #253 kept its store
    read from doing. This one hands tmux ``-f /dev/null`` instead: the conf is read only
    when a server starts, and no question asked here starts one. And it waits
    :data:`_ASK_TIMEOUT` for an answer, not :data:`_COMMAND_TIMEOUT` (round 4 of #259). A
    handle at a recorded socket (:meth:`TmuxServer.at`) keeps both.
    """
    return TmuxServer(socket, conf=Path(os.devnull), runner=_tmux_asking)


class TmuxServer:
    """A handle on one private tmux server (``-L socket``, or ``-S path``: :meth:`at`).

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
        path: Path | None = None,
    ) -> None:
        self.socket = socket
        self.path = path
        """Where this handle asks for the socket (``tmux -S``), set by :meth:`at`; ``None``
        asks for it by name (``tmux -L``), in the directory this shell's tmux looks in."""
        self._binary = binary
        self._conf = conf
        self._runner: Runner = runner or _tmux
        #: The bundled conf once this instance has verified it on disk. A frame
        #: is one tmux command, and re-reading the file for each of twenty
        #: frames a second bought nothing: tmux reads it only when the server
        #: starts, and drift is fixed by the next process that starts one.
        self._conf_ready: Path | None = None
        #: What :meth:`conf_path` failing open cost, once it has been called and
        #: something did fail; ``None`` while the conf is the bundled one. Read
        #: it to TELL somebody — nothing here prints.
        self.conf_fallback: str | None = None

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
        """The version of the tmux binary on PATH, or ``None`` when it cannot be read.

        That is the version a server started NOW would run, which is what
        :meth:`require` gates. It is not the running server's
        (:meth:`server_version`) once the package was upgraded in place.
        """
        completed = self._runner([self.binary(), "-V"], None)
        return parse_version(completed.stdout) if completed.returncode == 0 else None

    def server_version(self) -> tuple[int, int] | None:
        """The RUNNING server's version, asked of the socket; ``None`` when it will not say.

        tmux parses a command's flags in the server, not in the client, so a
        flag or a key name the server does not know is refused (or typed out)
        whatever the binary on PATH knows. Upgraded in place (brew, dnf, apt)
        while the private server keeps running the old binary — the case
        :meth:`server_absent` names — ``tmux -V`` said 3.7 to a 3.4 server:
        ``capture-pane -F`` failed every frame and ``S-Enter`` was typed into
        the agent (final review of #203, F2; measured with a 3.7c client on a
        3.4 server, which talk without complaint). ``display-message -p
        '#{version}'`` is the question :meth:`reachable` already puts, and the
        server answers it with its own version. A refusal (no server, a denied
        socket) is ``None``; a question that could not be put (no binary, a
        timeout) raises :class:`TmuxError`, as :meth:`version` does.
        """
        completed = self._runner(self.argv("display-message", "-p", "#{version}"), None)
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
        """The bundled configuration on disk, rewritten whenever it drifts.

        FAILS OPEN — no ``OSError`` leaves this method. :meth:`argv` calls it,
        so it runs on EVERY tmux command, and a home it cannot write (a
        root-owned conf after one ``sudo asq``, a read-only home, ENOSPC) used
        to put a ``PermissionError`` through ``has_session``, ``run``, … past
        this class's contract and past ``cli/fleet.py``'s ``except FleetError``:
        a raw traceback, and zero bytes on stdout under ``--json``. Mapping it
        to :class:`TmuxError` was the other option and is not enough on its own
        — the CLI catches ``FleetError``, so ``fleet attach`` would still
        traceback — so the write is repair, not a precondition, and a failed
        repair costs the repair only. :attr:`conf_fallback` records what it
        cost, for a caller that wants to say so; the doctor's conf check reads
        the file itself (``diagnostics._fleet_conf``) and is unaffected.

        Why the branches differ, measured on tmux 3.7c: ``-f`` is read only when
        a server STARTS, a MISSING ``-f`` file is not an error (exit 0, built-in
        defaults), and an UNREADABLE one kills the start outright ("server
        exited unexpectedly", exit 1). So a conf that could be read but not
        rewritten is handed over STALE — the drift is whatever an upgrade
        changed, and the next process with a writable home repairs it — while
        one that could not be read is replaced by ``os.devnull``, which costs
        the bundled options on a server this process starts (status bar on,
        escape-time back to 500 ms, extended keys off, so ``S-Enter`` never
        reaches an agent) and never the command.
        """
        if self._conf is not None:
            return self._conf
        if self._conf_ready is not None:
            return self._conf_ready
        path, cost = self._ensure_conf()
        # Cached like a successful write: a home that refused one write refuses
        # every later one too, and retrying per command would pay for it twenty
        # times a second while the UI renders.
        self._conf_ready = path
        self.conf_fallback = cost
        return path

    def _ensure_conf(self) -> tuple[Path, str | None]:
        """``(the conf to hand tmux, what failing open cost)`` — ``None`` when nothing did."""
        try:
            path = paths.ensure_home() / CONF_NAME
        except OSError as exc:
            return Path(os.devnull), (
                f"could not create the aisquare home ({exc.strerror or exc}) — the fleet's "
                "tmux server starts with tmux's built-in options, not the bundled ones"
            )
        absent = False
        readable = True
        try:
            current = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            current, absent = "", True
        except OSError:
            current, readable = "", False
        if current == BUNDLED_CONF:
            return path, None
        try:
            path.write_text(BUNDLED_CONF, encoding="utf-8")
        except OSError as exc:
            why = f"could not write {path} ({exc.strerror or exc})"
            if absent:
                return path, (
                    f"{why} — the fleet's tmux server starts with tmux's built-in options, "
                    "not the bundled ones"
                )
            if readable:
                return path, (
                    f"{why} — tmux gets the file that is there, whose options are "
                    "whatever the last version wrote"
                )
            return Path(os.devnull), (
                f"{why}, and it cannot be read either — an unreadable -f file makes tmux "
                "exit at startup, so the server gets /dev/null and tmux's built-in options"
            )
        return path, None

    def _argv_on(self, socket: str, conf: str, *args: str) -> list[str]:
        return [self.binary(), "-L", socket, "-f", conf, *args]

    def argv(self, *args: str) -> list[str]:
        """The full command line for one tmux invocation on this server."""
        if self.path is not None:
            return [self.binary(), "-S", str(self.path), "-f", str(self.conf_path()), *args]
        return self._argv_on(self.socket, str(self.conf_path()), *args)

    def at(self, path: Path | None) -> TmuxServer:
        """This server asked for at ``path`` (``tmux -S``), the socket a recorded row's server
        said it listened on (``FleetAgent.socket_path``); this handle when ``path`` is ``None``.

        ``-L`` finds a socket by name in the directory this shell's tmux looks in. A server
        started under another ``TMUX_TMPDIR`` is not there, and another server on the same
        name may be, whose pane ids, numbered from ``%0`` by every server, name other agents
        (round 28 of #257). tmux answers at a path as it does for a name, measured on 3.7c:
        ``no server running on <path>`` for a stale file, ``error connecting to <path> (No
        such file or directory)`` for none.
        """
        if path is None or path == self.path:
            return self
        return TmuxServer(
            self.socket, binary=self._binary, conf=self._conf, runner=self._runner, path=path
        )

    def run(self, *args: str, stdin: bytes | None = None) -> str:
        """Run one tmux command (``;`` arguments chain several) and return stdout."""
        completed = self._runner(self.argv(*args), stdin)
        if completed.returncode != 0:
            raise TmuxError(completed.stderr.strip() or f"tmux {' '.join(args)} failed")
        return completed.stdout

    def check_conf(self, conf: Path | None = None) -> None:
        """Prove THIS tmux accepts the configuration, or raise with tmux's complaint.

        ``-f`` at server start swallows configuration errors (see the module
        docstring), so the check that can actually fail is ``source-file`` on a
        running server. It runs on a throwaway server — ``-L <socket>-check``,
        started with ``-f /dev/null`` — so the fleet's own server is never
        touched and never accumulates a second ``-ga`` append. That server has
        no session and exits on its own (``exit-empty``); a best-effort
        ``kill-server`` follows so nothing is left behind if it has not yet.

        The doctor's "private server starts" check is exactly this call.
        """
        path = conf if conf is not None else self.conf_path()
        socket = self.socket + CHECK_SOCKET_SUFFIX
        completed = self._runner(
            self._argv_on(socket, os.devnull, "start-server", ";", "source-file", str(path)),
            None,
        )
        self._runner(self._argv_on(socket, os.devnull, "kill-server"), None)
        complaint = completed.stderr.strip()
        if completed.returncode != 0 or complaint:
            raise TmuxError(complaint or f"tmux rejected {path} without saying why")

    # --- sessions and windows ---------------------------------------------------------

    def list_sessions(self) -> list[str]:
        """Session names on this server; empty when the server is not running."""
        completed = self._runner(self.argv("list-sessions", "-F", "#{session_name}"), None)
        if completed.returncode != 0:
            return []
        return [line for line in completed.stdout.splitlines() if line]

    def sessions_or_raise(self) -> list[str]:
        """:meth:`list_sessions`, except a server that could not be ASKED raises.

        The lenient twin returns ``[]`` for EVERY non-zero exit, so its caller
        cannot tell an empty or absent server from one it failed to reach — the
        class of bug the review kept finding at one more shutdown call site
        (review of #121, round 8). Here ``[]`` means only what tmux confirmed: a
        server that is gone (:data:`_ABSENT`) holds no sessions. Anything else
        non-zero is a :class:`TmuxError` carrying tmux's own words, so the
        ``--all`` sweep cannot read a denied socket as "no sessions to kill".
        """
        completed = self._runner(self.argv("list-sessions", "-F", "#{session_name}"), None)
        if completed.returncode == 0:
            return [line for line in completed.stdout.splitlines() if line]
        if _ABSENT.search(completed.stderr):
            return []
        raise TmuxError(completed.stderr.strip() or "tmux list-sessions could not be reached")

    def has_session(self, name: str) -> bool:
        completed = self._runner(self.argv("has-session", "-t", f"={name}"), None)
        return completed.returncode == 0

    def has_session_or_raise(self, name: str) -> bool:
        """:meth:`has_session`, except a server that could not be ASKED raises.

        ``False`` is the answer for a session tmux SAYS is not there — its server
        gone (:data:`_ABSENT`), or a live server that holds no such session
        (:data:`_NO_TARGET`) — never for a probe that could not run. The lenient
        twin returns ``False`` on ANY non-zero exit, which let a denied socket
        report an existing session as absent and clear its pause while the
        session survived (review of #121, round 8).
        """
        completed = self._runner(self.argv("has-session", "-t", f"={name}"), None)
        if completed.returncode == 0:
            return True
        if _ABSENT.search(completed.stderr) or _NO_TARGET.search(completed.stderr):
            return False
        raise TmuxError(completed.stderr.strip() or "tmux has-session could not be reached")

    def answers(self) -> bool:
        """Whether a server is listening on this socket at all — not what it holds.

        The reachability question, asked exactly. Every other wrapper here
        swallows a non-zero exit and reports an unreachable server the way it
        reports an absent pane (``[]`` / ``None``), so a caller cannot tell
        "asked and answered" from "could not ask" — which is how a fleet-wide
        ``reap`` came to read a missing socket as every pane being gone.

        ``display-message -p '#{version}'`` is the cheapest question that
        separates all three states, measured on tmux 3.7c: no server exits 1
        (``error connecting to /tmp/tmux-<uid>/<socket>``); a server holding a
        session exits 0 with the version; and a server holding NOTHING (only
        reachable with ``exit-empty off``, which :data:`BUNDLED_CONF` does not
        set) also exits 0 with the version — where ``list-sessions`` exits 0
        with no output and is therefore indistinguishable from absence.

        Never raises: an unavailable binary is not a server that answered. A
        caller that must tell "could not ask" from "no server" — the one place
        that ends rows on that distinction is ``fleet shutdown``'s final pass —
        uses :meth:`reachable`, which propagates :class:`TmuxUnavailable`.
        """
        try:
            return self.reachable()
        except TmuxError:  # unavailable client, denied socket, timeout: not an answer
            return False

    def reachable(self) -> bool:
        """:meth:`answers`, except that "could not ask" is raised, not swallowed.

        :class:`TmuxUnavailable` covers both a binary ``which`` cannot find and
        one that fails at EXECUTION — deleted between the check and the exec, or
        a shim whose interpreter is gone — because ``_tmux`` maps the
        ``FileNotFoundError`` from ``subprocess.run`` to the same class (review of
        #121, round 5). And a non-zero exit is NOT proof of absence: an
        inaccessible LIVE socket also exits 1, with ``Permission denied`` where
        an absent server says ``No such file or directory`` (round 7). So
        ``False`` means exactly one thing — the client ran and tmux said there is
        no server on this socket — and every other failed probe is a
        :class:`TmuxError` carrying tmux's own words.
        """
        completed = self._runner(self.argv("display-message", "-p", "#{version}"), None)
        if completed.returncode == 0:
            return True
        detail = completed.stderr.strip()
        if _ABSENT.search(detail):
            return False
        raise TmuxError(detail or f"tmux display-message exited {completed.returncode}")

    def server_absent(self) -> bool:
        """Whether tmux itself says NO SERVER is behind this socket — positive evidence.

        Not the complement of :meth:`answers`. A client exits non-zero for a
        protocol version mismatch too (the tmux package upgraded in place while
        the private server keeps running the old binary), and for a wedged
        server, and for a socket that lives under a different ``TMUX_TMPDIR``
        than this shell's — in every one of those the agents are alive. Only two
        answers mean the server is gone, and tmux spells both out on stderr
        (measured on 3.4): ``no server running on <path>`` (the socket file is
        there, nothing listens) and ``error connecting to <path> (No such file or
        directory)`` (the file itself is gone — a reboot swept ``/tmp``). A
        missing binary is not evidence either way.

        One residual this cannot settle: a shell whose ``TMUX_TMPDIR`` differs
        from the spawning shell's asks at a path that never had a server, and
        gets the second message for a fleet that is alive elsewhere. That is why
        ``fleet reap --server-down`` stays the operator's word — this predicate
        narrows what the word may act on, it does not replace it — and why
        ``reap`` then asks :func:`server_may_run` of each row there: the second
        message is also a socket file taken from under a server that runs on,
        and such a row is held, not marked (follow-up of #257).

        A question that could not be put — no client, a wedged server's 30 s
        timeout, an OS refusal — is no evidence either, like :meth:`answers`.
        Catching the missing client alone let a timeout raise straight through
        ``reap --server-down``, the command ``doctor`` prescribes for exactly
        the silent server that times out (round 7 of #203).
        """
        try:
            completed = self._runner(self.argv("display-message", "-p", "#{version}"), None)
        except TmuxError:  # TmuxUnavailable included
            return False
        if completed.returncode == 0:
            return False
        return _ABSENT.search(completed.stderr) is not None

    def started_at(self) -> datetime | None:
        """When the server on this socket started (``#{start_time}``), to the whole second.

        A pane id names a pane of ONE server's lifetime: the next server on the
        socket numbers its panes from ``%0`` again (measured on 3.7c,
        ``test_live_a_new_server_reuses_pane_ids_and_says_when_it_started``). So an
        id recorded earlier is only worth asking about once the server answering
        is known to be the one it was recorded on, and this is what tells them
        apart. tmux reports the start in whole seconds, rounded down.

        ``None`` when no server is listening (:data:`_ABSENT`), or when the
        answer is not a number. A question that could not be put — a denied
        socket, a wedged server's timeout — raises :class:`TmuxError`, as the
        strict reads do.
        """
        completed = self._runner(self.argv("display-message", "-p", "#{start_time}"), None)
        if completed.returncode != 0:
            if _ABSENT.search(completed.stderr):
                return None
            raise TmuxError(completed.stderr.strip() or "tmux display-message could not be reached")
        return _epoch(completed.stdout)

    def spawn_window(
        self,
        session: str,
        *,
        name: str,
        cwd: Path,
        command: Sequence[str],
        env: Mapping[str, str] | None = None,
        width: int = DEFAULT_WINDOW_WIDTH,
        height: int = DEFAULT_WINDOW_HEIGHT,
    ) -> WindowInfo:
        """A new window named ``name`` running ``command`` — creating the session if needed.

        ``env`` pairs are set for the new window (``-e``) and are THIS WINDOW'S
        ALONE. When the window opens a new session tmux also writes them into
        the session environment, which every window opened later in that
        session inherits; they are taken back out
        (:meth:`_forget_session_environment`), so a window opened by hand in
        the session inherits the server's environment and nothing of any
        agent's. The command is
        passed as separate arguments and executed directly, never through a
        shell, so no shell re-interprets it. TMUX still would: an argument that
        ends in ``;`` ends the tmux command even after ``--`` and runs the rest
        as a second one on the private server (``… -- sh -c … ';' kill-server``
        would kill every agent), so ``name``, ``cwd``, ``env`` and every element
        of ``command`` go through :func:`_data_arg` and arrive verbatim.

        ``width``/``height`` size the window either way. A NEW session's first
        window takes them on ``new-session -x -y``; a window added to an
        EXISTING session is born at the session's size — 200x50 for every
        session made before #149, or whatever its first window was — or, while
        a ``fleet attach`` client is attached, at that client's size, and is
        therefore resized right after ``new-window`` through :meth:`resize`,
        which also pins it to manual sizing (the global ``window-size manual``
        crashes tmux 3.4 — see :data:`BUNDLED_CONF`). Without that second
        step a coder spawned into a running manager's session was wide enough
        for Claude Code to open its diff panel on its own (#149).

        The pin is kept on purpose. Under tmux's default ``window-size latest``
        an unpinned window follows an attached terminal and keeps that width
        after the terminal detaches, and a window born while one is attached
        takes its size whatever the session's ``default-size`` says (both
        measured on 3.7c, at 230 columns with ``default-size 120x40``) — so
        neither leaving the window unpinned nor sizing the session instead
        keeps a coder under 144 columns across a wide ``fleet attach`` (review
        of #162, round 1). The cost: under ``fleet attach`` such a window keeps
        its own size, panned in a smaller terminal and padded in a larger one.
        The session's first window gets no resize, so it follows an attached
        terminal until a pane has shown it.

        The resize fails open: a window tmux refuses to resize is still
        returned, at the session's size, with tmux's words in
        :attr:`WindowInfo.resize_refused` so the caller can say why. A pane's
        first attach corrects that size (``TerminalPane._sync_size``); a
        headless window keeps it until a pane shows it — nothing sizes a
        window nobody opens.

        Refuses a ``session`` no other method here could target afterwards and
        a ``cwd`` that is not a directory (tmux would use ``$HOME`` silently).
        """
        _require_targetable(session)
        _require_directory(cwd)
        env_flags = [
            flag
            for key, value in (env or {}).items()
            for flag in ("-e", _data_arg(f"{key}={value}"))
        ]
        args = [_data_arg(arg) for arg in command]
        fmt = f"#{{window_id}}{_SEP}#{{pane_id}}"
        existing = self.has_session(session)
        if existing:
            out = self.run(
                "new-window", "-d", "-P", "-F", fmt, "-t", f"={session}:", "-n", _data_arg(name),
                "-c", _data_arg(str(cwd)), *env_flags, "--", *args,
            )  # fmt: skip
        else:
            out = self.run(
                "new-session", "-d", "-P", "-F", fmt, "-s", _data_arg(session), "-n",
                _data_arg(name), "-c", _data_arg(str(cwd)),
                "-x", str(width), "-y", str(height), *env_flags,
                "--", *args,
            )  # fmt: skip
            self._forget_session_environment(session, env)
        window_id, _, pane_id = out.strip().partition(_SEP)
        refused: str | None = None
        if existing and pane_id:
            # Born at the session's size, not the caller's (see the docstring);
            # the resize is what makes the geometry argument mean the same
            # thing on both branches. Fail-open: the window exists and is
            # recorded whatever tmux says about its size — a refused resize
            # costs geometry, never the agent. The geometry is the session's:
            # a pane that shows the window resizes it (the UI's own sync, which
            # retries); a headless window keeps it until a pane shows it, so the
            # refusal is handed back rather than swallowed.
            try:
                self.resize(pane_id, width, height)
            except TmuxError as exc:
                refused = str(exc)
        return WindowInfo(
            session=session,
            window_id=window_id,
            name=name,
            pane_id=pane_id,
            dead=False,
            dead_status=None,
            current_command=command[0] if command else "",
            activity=False,
            resize_refused=refused,
        )

    def _forget_session_environment(self, session: str, env: Mapping[str, str] | None) -> None:
        """Take the first window's ``-e`` pairs back out of the SESSION environment.

        ``new-window -e`` sets a variable for that window alone, but
        ``new-session -e`` writes it into the session environment, which every
        later window of the session inherits — measured on 3.7c:
        ``show-environment`` listed it, a window opened by hand read it, and
        after ``set-environment -u`` a third window did not. Every pair goes,
        because every pair is one agent's: ``AISQUARE_FLEET_AGENT`` is an
        identity, so a window the operator opens by hand in the fleet's session
        (``prefix c``, ``fleet attach``) would have called itself the first
        agent (review of #135); the account pins are one agent's slot and
        aisquare home, so a later ``fleet spawn`` with no ``--account`` — which
        sets none of its own — ran under whichever account the FIRST spawn of
        the session happened to use, wrong slot and wrong ``context.db``; and
        the native-teams opt-out is for "the sessions the fleet starts — a
        user's own ``claude`` sessions keep whatever they had" (§7.6), which a
        hand-opened window is (review of #203, rounds 3 and 4). What a later
        window needs, its own spawn sets. The process in the first window
        already has its copy; only the session's is removed. Best effort,
        because the window is up either way: a failed unset costs exactly the
        leak it was closing.
        """
        for key in env or {}:
            with contextlib.suppress(TmuxError):
                self.run("set-environment", "-u", "-t", f"={session}", _data_arg(key))

    def _parse_windows(self, session: str, stdout: str) -> list[WindowInfo]:
        windows: list[WindowInfo] = []
        for line in stdout.splitlines():
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
                    server_started=_epoch(values["start_time"]),
                )
            )
        return windows

    def list_windows(self, session: str) -> list[WindowInfo]:
        """Every window (one pane each) of ``session``; empty when it does not exist."""
        completed = self._runner(
            self.argv("list-panes", "-s", "-t", f"={session}", "-F", _WINDOW_FORMAT), None
        )
        if completed.returncode != 0:
            return []
        return self._parse_windows(session, completed.stdout)

    def windows_or_raise(self, session: str) -> list[WindowInfo]:
        """:meth:`list_windows`, except a server that could not be ASKED raises.

        ``[]`` is the answer only for a session tmux CONFIRMS is not there — its
        server gone (:data:`_ABSENT`) or the session itself (:data:`_NO_TARGET`).
        The lenient twin's ``[]`` on any non-zero exit let a transient socket
        failure read as "this session holds no left-live pane", and a session
        whose agent was verified alive was killed anyway (review of #121, round
        8). So the spare rule fails closed: it never kills on an enumeration
        that did not answer.
        """
        completed = self._runner(
            self.argv("list-panes", "-s", "-t", f"={session}", "-F", _WINDOW_FORMAT), None
        )
        if completed.returncode != 0:
            if _ABSENT.search(completed.stderr) or _NO_TARGET.search(completed.stderr):
                return []
            raise TmuxError(completed.stderr.strip() or "tmux list-panes could not be reached")
        return self._parse_windows(session, completed.stdout)

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

    def pane_pid(self, pane_id: str) -> int | None:
        """The pid of the process tmux started in the pane, or ``None`` when it is gone.

        Asked on its own rather than as one more :class:`PaneFacts` field: the
        facts are polled for every frame of the UI, and this is read once per
        session start by the hook that has to decide whether the process asking
        is the pane's own (``services.team``). ``aisquare launch`` execs the
        agent, so the pid tmux started IS the agent's — the number Claude Code
        hands its hooks as ``CLAUDE_PID``. The same two shapes of "gone" as
        :meth:`pane_facts` — a non-zero exit, and 3.7c's empty answer for a
        target it could not find — and the same guard against an attached
        client's current pane answering for the one that was asked about.
        """
        fmt = f"#{{pane_id}}{_SEP}#{{pane_pid}}"
        completed = self._runner(self.argv("display-message", "-p", "-t", pane_id, fmt), None)
        if completed.returncode != 0:
            return None
        answered, _, pid = completed.stdout.strip().partition(_SEP)
        if not answered or (pane_id.startswith("%") and answered != pane_id):
            return None
        return _optional_int(pid)

    def pane_facts_or_raise(self, pane_id: str) -> PaneFacts | None:
        """:meth:`pane_facts`, except a server that could not be ASKED raises.

        ``None`` means the pane is gone as tmux CONFIRMED it: an empty answer
        from a live server (``display-message`` may miss its target and still
        exit 0) or a server that is gone (:data:`_ABSENT`). A non-zero exit that
        is neither — ``Permission denied`` on a live socket — is a
        :class:`TmuxError`, not a dead pane. The lenient twin's ``None`` there
        ended a still-running late agent's row and released its claims (review
        of #121, round 8).
        """
        completed = self._runner(
            self.argv("display-message", "-p", "-t", pane_id, _FACTS_FORMAT), None
        )
        if completed.returncode != 0:
            if _ABSENT.search(completed.stderr):
                return None
            raise TmuxError(completed.stderr.strip() or "tmux display-message could not be reached")
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
        and so no socket to point at. A handle asked for at a path (:meth:`at`)
        answers that path.
        """
        if self.path is not None:
            return self.path
        getuid = getattr(os, "getuid", None)
        if getuid is None:
            raise TmuxUnavailable("no tmux socket on this platform — the fleet needs a POSIX host")
        base = os.environ.get("TMUX_TMPDIR") or "/tmp"
        return Path(base) / f"tmux-{getuid()}" / self.socket

    def listener(self, recorded: Path | None = None) -> SocketAnswer | None:
        """:func:`socket_answer` for this server's socket: at ``recorded``, the path the server
        said it listened on (``FleetAgent.socket_path``), else at :meth:`socket_path`. Asked of
        the socket, so it needs no tmux. Whether that path's directory is there settles
        nothing: tmux makes it as it looks, so any probe made it there (round 24 and 25 of
        #257)."""
        if recorded is None:
            try:
                recorded = self.socket_path()
            except TmuxUnavailable:
                return None  # no POSIX uid: no tmux server can exist here
        return socket_answer(recorded)

    def server_socket_path(self) -> Path | None:
        """The socket the RUNNING server listens on, as it says (``#{socket_path}``).

        :meth:`socket_path` rebuilds the path from the asking shell, and a server
        started under another ``TMUX_TMPDIR``, or by a tmux built to keep its
        sockets elsewhere (Termux), is not there. Asked once a window is made on
        the server and kept on the fleet row (``FleetAgent.socket_path``), so a
        later question about the row goes to that path whatever shell asks it.
        ``None`` when the server does not answer with an absolute path; a question
        that could not be put raises :class:`TmuxError`.
        """
        completed = self._runner(self.argv("display-message", "-p", "#{socket_path}"), None)
        said = completed.stdout.strip()
        return Path(said) if completed.returncode == 0 and os.path.isabs(said) else None

    def rename_session(self, old: str, new: str) -> None:
        _require_targetable(new)
        self.run("rename-session", "-t", f"={old}", new)

    def attach_argv(self, session: str) -> list[str]:
        """The command a terminal runs to attach to ``session`` (the escape hatch)."""
        return self.argv("attach-session", "-t", f"={session}")

    # --- the screen -------------------------------------------------------------------

    def capture(
        self,
        pane_id: str,
        *,
        scrollback: int = 0,
        height: int | None = None,
        flags: bool = False,
    ) -> Capture:
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

        ``flags`` asks for ``-F`` as well, and the frame then carries which of
        its rows tmux soft-wrapped (:attr:`Capture.wrapped`) — in the SAME
        process, so a copy that joins wrapped rows never has to ask a second
        time and compare two screens (review of #135). Only for a server that
        knows the flag (:data:`WRAP_FLAGS_MINIMUM`): the caller checks the
        version, because an unknown flag fails the whole frame.
        """
        scrollback = max(0, scrollback)
        bound = height if scrollback and height is not None and height > 0 else None
        rows, wrapped, facts = self._frame(pane_id, scrollback, bound, flags)
        if bound is not None and len(rows) < facts.height:
            rows, wrapped, facts = self._frame(pane_id, scrollback, None, flags)
        effective = min(scrollback, facts.history_size)
        return Capture(
            lines=rows[: facts.height],
            facts=facts,
            scrollback=effective,
            wrapped=None if wrapped is None else wrapped[: facts.height],
        )

    def capture_history(self, pane_id: str, *, history: int) -> Capture:
        """Scrollback PLUS the live screen, oldest row first, in ONE contiguous frame.

        :meth:`capture` answers "what is on this screen now" and so slices to
        exactly one screen height; this answers "what has this pane said", a
        different question, and keeping the rows above the screen is the whole
        point of it.

        Still one process: ``capture-pane -S -<n>`` with no ``-E`` runs from n
        lines above the screen top to the bottom of the live screen, and tmux
        clamps n to the history it actually has. The alternative — calling
        :meth:`capture` once per screenful and stitching — costs a process per
        ~50 rows AND cannot produce a consistent snapshot, because a live pane
        moves between the calls, so the seams would duplicate or drop rows.

        ``Capture.scrollback`` is MEASURED here rather than inferred: the frame
        keeps every row, so the count above the screen is ``len(rows) - height``
        and does not have to be predicted from ``history_size``.
        """
        history = max(0, history)
        rows, _, facts = self._frame(pane_id, history, None, False)
        return Capture(lines=rows, facts=facts, scrollback=max(0, len(rows) - facts.height))

    def _frame(
        self, pane_id: str, scrollback: int, height: int | None, flags: bool
    ) -> tuple[list[str], list[bool] | None, PaneFacts]:
        window = ["-E", str(height - 1 - scrollback)] if height is not None else []
        marks = ["-F"] if flags else []
        out = self.run(
            "capture-pane", "-p", "-e", "-N", *marks, "-S", str(-scrollback), *window,
            "-t", pane_id,
            ";", "display-message", "-p", "-t", pane_id, _FACTS_FORMAT,
        )  # fmt: skip
        body = out.rstrip("\n").split("\n")
        rows, facts = body[:-1], _facts(body[-1])
        if not flags:
            return rows, None, facts
        # ``-F`` puts the line's flags, then one space, before the line — before
        # its escapes too (measured on 3.7c: ``W \x1b[31m…``). A blank row is
        # ``- `` and an empty flags column never happens, so ``partition`` is
        # exact; a flag other than ``W`` (``X`` extended cells, ``H`` hyperlinks)
        # rides in the same column and is ignored here.
        lines: list[str] = []
        wrapped: list[bool] = []
        for row in rows:
            mark, _, text = row.partition(" ")
            lines.append(text)
            wrapped.append("W" in mark)
        return lines, wrapped, facts

    # --- input --------------------------------------------------------------------------

    def send_keys(self, pane_id: str, *keys: str) -> None:
        """Named keys (``Enter``, ``C-c``, ``BTab``…) — tmux's own vocabulary, and only that.

        Every argument after ``-t <pane>`` used to go to tmux as it came, and a
        key is caller data: measured on 3.7c, ``'Enter;' set -g @x 1`` set the
        option and ``';' run-shell …`` ran the shell command, because tmux ends
        a command at any argument whose LAST character is ``;``
        (:data:`_ARGV_SEPARATOR`), and a key starting with ``-`` parsed as a
        flag. So ``--`` ends the flags (``-l`` is the KEY ``-l``, sent as the
        text it spells), :func:`_data_arg` escapes a trailing separator, and a
        key that is empty or holds whitespace or a control character is
        refused: no key name has one, and tmux would split or mangle it. This
        is the floor under every caller; the remote's allowlist
        (``remote_server.check_remote_key_names``) is the boundary itself.

        A control character is any of Unicode's (``Cc``): C0, DEL and the C1 controls,
        ``"\\x9b"`` (CSI) among them. ``ord(char) < 0x20`` let DEL and C1 through (review of
        #259, round 2). So is a lone surrogate (``Cs``), which the argv's encoding turns
        into the raw byte it escapes, ``"\\udc9b"`` into that CSI. A printable character
        of any script stays a key: an alt chord sends ``M-é``.
        """
        if not keys:
            return
        for key in keys:
            if not key or any(
                char.isspace() or unicodedata.category(char) in ("Cc", "Cs") for char in key
            ):
                raise TmuxError(f"{key!r} is not a tmux key name")
        self.run("send-keys", "-t", pane_id, "--", *(_data_arg(key) for key in keys))

    def send_literal(self, pane_id: str, text: str) -> None:
        """Literal text, byte for byte, through the hex path — nothing parses it.

        This used to go through ``send-keys -l -- <arg>``, where tmux's own
        argument handling reads the string before the pane does: measured on
        3.7c, ``-l -- ';'`` sends NOTHING and ``'a;'`` sends ``a``, which is why
        :func:`_data_arg` exists to escape a trailing separator. That workaround
        is one known quirk of a parser whose behaviour varies by version, and
        every character typed from the remote page crosses it.

        ``-H`` takes the bytes as hex pairs, so there is no string for any
        version of any parser to interpret — a space, a trailing space, a tab, a
        lone ``;`` and a multi-byte character all arrive exactly as sent. The
        cost is argv length, which :meth:`send_bytes` bounds.
        """
        if text:
            self.send_bytes(pane_id, text.encode("utf-8"))

    def send_bytes(self, pane_id: str, data: bytes) -> None:
        """Raw bytes, one hex pair per argument (``-H``).

        The one way to put a byte above 0x7f in front of a program: ``-l`` takes
        a string and tmux re-emits it as UTF-8, so ``chr(0x98)`` arrives as
        ``C2 98`` — measured on 3.7c, the X10 mouse encoding's column byte for
        any cell past 95 split in two, with the row byte then read as text.

        Sent in chunks of :data:`_HEX_CHUNK` bytes because ``-H`` costs one argv
        element PER BYTE: a pasted paragraph would otherwise build a command tmux
        3.7 refuses (more than 1000 arguments) and, longer still, a command line
        that fails with E2BIG. Chunks go in order down one synchronous path, so
        the pane sees one uninterrupted stream; a typed run is one call. A chunk
        after the first that fails is :class:`TmuxPartlyTyped`: the chunks before
        it are in the pane.
        """
        for start in range(0, len(data), _HEX_CHUNK):
            chunk = data[start : start + _HEX_CHUNK]
            try:
                self.run("send-keys", "-t", pane_id, "-H", *(f"{byte:02x}" for byte in chunk))
            except TmuxError as exc:
                if not start:
                    raise
                raise TmuxPartlyTyped(exc, start, len(data)) from exc

    def paste(self, pane_id: str, text: str) -> None:
        """Bracketed paste: the agent sees one paste, not one Enter per line.

        The buffer belongs to THIS call (:data:`PASTE_BUFFER` says what one
        shared name cost), so ``-d`` deletes exactly the buffer this call
        created and an interleaved paste elsewhere cannot be delivered here.

        A ``load-buffer`` that timed out is a plain :class:`TmuxError`: ``paste-buffer``
        was never sent, so no server, recovered or not, can type the text. It was a
        :class:`TmuxTimedOut`, "it may still arrive" (review of #259, round 11, 7/13).
        """
        if not text:
            return
        buffer_name = f"{PASTE_BUFFER}-{os.getpid()}-{next(_PASTE_SEQUENCE)}"
        try:
            self.run("load-buffer", "-b", buffer_name, "-", stdin=text.encode("utf-8"))
        except TmuxTimedOut as exc:
            # A wedged server may still load it, and keep it: tidied as below, behind it.
            self._tidy("delete-buffer", "-b", buffer_name)
            raise TmuxError(f"{exc} — nothing was pasted") from exc
        try:
            self.run("paste-buffer", "-p", "-d", "-b", buffer_name, "-t", pane_id)
        except TmuxError:
            # The pane died between the two calls. ``-d`` never ran, so the text
            # is still a buffer on a server the user can attach to, where
            # `prefix-]` would deliver an agent's prompt to whatever pane they
            # are looking at — and a per-call name is never reused, so nothing
            # overwrites it. Tidy it away; the error the caller gets is the
            # paste's, never the tidy-up's.
            self._tidy("delete-buffer", "-b", buffer_name)
            raise

    def _tidy(self, *args: str) -> None:
        """Run ``args`` after a command failed, to leave nothing of it behind: never raising,
        and waiting :data:`_ASK_TIMEOUT` at most, not :data:`_COMMAND_TIMEOUT`. The server may
        be wedged: a paste that timed out held the agent's lock 30 s more for its tidy-up
        before its ``maybe`` was answered (review of #259, round 11, 7/13). Queued in a
        wedged server, it runs after the command it tidies once the server recovers."""
        runner = _tmux_asking if self._runner is _tmux else self._runner
        with contextlib.suppress(TmuxError):
            runner(self.argv(*args), None)

    def list_buffers(self) -> list[str]:
        """The server's paste buffers by name, the most recently written first.

        A copy that names no buffer — copy mode's, a program's OSC 52,
        ``set-buffer`` or ``load-buffer`` without ``-b`` — makes a NEW buffer,
        ``buffer<N>`` with ``N`` never reused while the server runs, even when
        it holds the text the newest one already does (measured on 3.7c). So a
        name this list did not have before is a copy made since, and the text
        cannot say that: the same words copied twice read as no copy at all
        (``test_live_every_copy_is_a_new_buffer_even_with_the_same_text``).
        A server with no buffers lists none, with exit 0.
        """
        return [
            name for name in self.run("list-buffers", "-F", "#{buffer_name}").splitlines() if name
        ]

    def show_buffer(self, name: str) -> str | None:
        """The text of the paste buffer ``name``, or ``None`` when the server holds no such buffer.

        Printed as the program wrote it. A buffer that is not there answers
        ``no buffer <name>`` and exits 1 — an answer, not a failure: one listed a
        moment ago may be gone, as a paste's own buffer is once ``paste-buffer
        -d`` ran. Every other error is raised.
        """
        try:
            return self.run("show-buffer", "-b", name)
        except TmuxError as exc:
            if "no buffer" in str(exc).lower():
                return None
            raise

    def resize(self, pane_id: str, width: int, height: int) -> None:
        """Size the pane's window to the pane the UI is showing it in."""
        self.run(
            "resize-window", "-t", pane_id, "-x", str(max(1, width)), "-y", str(max(1, height))
        )
