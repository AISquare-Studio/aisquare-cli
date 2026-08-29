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

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from aisquare.core import paths
from aisquare.core.spawn import untraced_env

DEFAULT_SOCKET = "asq"
MIN_VERSION = (3, 2)
"""``new-window -e`` and ``extended-keys`` arrived in 3.2; ``S-Enter`` needs 3.4."""
PASTE_BUFFER = "asq-paste"
CONF_NAME = "fleet-tmux.conf"
CHECK_SOCKET_SUFFIX = "-check"
"""Where :meth:`TmuxServer.check_conf` starts its throwaway server: ``<socket>-check``."""
_COMMAND_TIMEOUT = 30.0

#: The bundled server configuration. Applied with ``-f`` when the server starts
#: (harmless afterwards). Every line has a reason: docs/plans/fleet-tui.md §3.1.
#: Every option is verified to load, with this value, on the tmux running the
#: tests by ``tests/test_tmux.py::test_live_every_bundled_option_is_applied_with_its_value``.
#: ``window-size`` is NOT here and must never be: a global one kills the server
#: below tmux 3.7 (module docstring). It lives in :data:`WINDOW_OPTIONS`.
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

#: What the fleet sets on each window it creates, the moment that window exists
#: — the rest of its configuration, and the half that cannot be in a file the
#: server reads at start (module docstring: a global ``window-size`` is a
#: segfault waiting for the next window on tmux below 3.7).
#:
#: ``window-size manual`` is the fleet's sizing model: a window keeps the size
#: it was born with — its session's, from ``new-session -x -y`` — until
#: :meth:`TmuxServer.resize` says otherwise, and a user who opens the escape
#: hatch on an 80x24 terminal does not shrink every agent's screen under the UI.
#: Applied by :meth:`TmuxServer.spawn_window` and checked, with the file, by
#: :meth:`TmuxServer.check_conf`.
WINDOW_OPTIONS: tuple[tuple[str, str], ...] = (("window-size", "manual"),)

#: Where :meth:`TmuxServer.check_conf` proves the configuration survives a real
#: server: one session, one window running ``true``, in the one directory POSIX
#: guarantees. The name obeys :func:`_require_targetable`.
_CHECK_SESSION = "asq-conf-check"
_CHECK_COMMAND = ("true",)
_CHECK_CWD = "/"

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


def _window_option_commands(target: str) -> list[list[str]]:
    """:data:`WINDOW_OPTIONS` as ``set-option -w`` commands against one window.

    ``target`` must name a window exactly — a ``@id`` from ``-P -F``, or
    ``=session:`` where the session has the one window. An EMPTY target is not a
    no-op to tmux: it resolves to whatever window is current, which on a server
    holding several agents is somebody else's. Callers make sure it is not.
    """
    return [["set-option", "-w", "-t", target, option, value] for option, value in WINDOW_OPTIONS]


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

        :data:`WINDOW_OPTIONS` go on the probe window in between, so the half of
        the configuration that is applied after a window is born is proved here
        too instead of at the first spawn.

        The throwaway server (``-L <socket>-check``) is never the fleet's own,
        so re-sourcing a file it already read — which appends
        ``terminal-overrides`` a second time — costs nothing, and neither does
        the window running ``true``. The check must OWN that server, though, so
        it is killed on both sides: one left running by a killed run would
        absorb the ``-f`` (tmux reads it only when it STARTS a server) and then
        refuse the session by name, turning a stale socket into a rejected
        configuration. Both kills are best effort — ``no server running`` is the
        ordinary answer to the first, and this server holds a session, so
        ``exit-empty`` will not end it by itself.

        The doctor's "private server starts" check is exactly this call.
        """
        path = conf if conf is not None else self.conf_path()
        socket = self.socket + CHECK_SOCKET_SUFFIX
        probe = ["new-session", "-d", "-s", _CHECK_SESSION, "-c", _CHECK_CWD, "--", *_CHECK_COMMAND]
        argv = self._argv_on(
            socket,
            str(path),
            *_chain(
                probe,
                *_window_option_commands(f"={_CHECK_SESSION}:"),
                ["source-file", str(path)],
            ),
        )
        tidy = self._argv_on(socket, os.devnull, "kill-server")
        self._runner(tidy, None)
        completed = self._runner(argv, None)
        self._runner(tidy, None)
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
        width: int = 200,
        height: int = 50,
    ) -> WindowInfo:
        """A new window named ``name`` running ``command`` — creating the session if needed.

        ``env`` pairs are set for the new window only (``-e``); the command is
        passed as separate arguments and executed directly, never through a
        shell, so nothing in it is re-interpreted. ``width``/``height`` size a
        NEW session's first window and become that session's ``default-size``,
        which is the size a window added to it later is born at; from there
        :data:`WINDOW_OPTIONS` pins the window and only :meth:`resize` moves it.
        Refuses a ``session`` no other method here could target afterwards and
        a ``cwd`` that is not a directory (tmux would use ``$HOME`` silently).

        The pin is a second command, against the ``@id`` tmux just printed, so
        it reaches that window and no other — the option cannot be given at
        creation, and it cannot be left standing globally for the next window to
        walk into (module docstring). The window is unpinned only between the
        two commands, when no client can yet be looking at it.
        """
        _require_targetable(session)
        _require_directory(cwd)
        env_flags = [
            flag for key, value in (env or {}).items() for flag in ("-e", f"{key}={value}")
        ]
        fmt = f"#{{window_id}}{_SEP}#{{pane_id}}"
        if self.has_session(session):
            out = self.run(
                "new-window", "-d", "-P", "-F", fmt, "-t", f"={session}:", "-n", name,
                "-c", str(cwd), *env_flags, "--", *command,
            )  # fmt: skip
        else:
            out = self.run(
                "new-session", "-d", "-P", "-F", fmt, "-s", session, "-n", name,
                "-c", str(cwd), "-x", str(width), "-y", str(height), *env_flags,
                "--", *command,
            )  # fmt: skip
        window_id, separator, pane_id = out.strip().partition(_SEP)
        if not window_id or not separator or not pane_id:
            # Not pedantry: an empty id would send the pin below to whatever
            # window is current, and the caller a WindowInfo it cannot target.
            raise TmuxError(f"tmux did not name the new window and pane: {out!r}")
        pin = _chain(*_window_option_commands(window_id))
        if pin:  # an empty WINDOW_OPTIONS must not become `tmux` with no command
            self.run(*pin)
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

        ``resize-window`` sets that window's ``window-size`` to ``manual`` as it
        goes — tmux's own doing, and the same pin :data:`WINDOW_OPTIONS` already
        put there when the window was created.
        """
        self.run(
            "resize-window", "-t", pane_id, "-x", str(max(1, width)), "-y", str(max(1, height))
        )
