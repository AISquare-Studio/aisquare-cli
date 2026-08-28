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

Verified against tmux 3.7c: ``new-session``/``new-window`` accept
``-e KEY=VALUE`` and a multi-argument command after ``--``; ``send-keys -l --``
takes literal text that starts with ``-``; a lone ``;`` argument separates
commands in one invocation; ``=name`` targets a session exactly; ``.`` and ``:``
in a session name make it untargetable, which is why names come from codenames.
"""

from __future__ import annotations

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
_COMMAND_TIMEOUT = 30.0

#: The bundled server configuration. Applied with ``-f`` when the server starts
#: (harmless afterwards). Every line has a reason: docs/plans/fleet-tui.md §3.1.
BUNDLED_CONF = """\
# aisquare fleet — private tmux server configuration (regenerated; do not edit)
set -g status off
set -s escape-time 0
set -g history-limit 50000
set -g window-size manual
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

#: Separator for multi-field ``display-message`` output. Never appears in a pane
#: id, a size or a flag; a command name or title containing it would be perverse.
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
    """THE seam: run one tmux invocation with the tracing identity stripped."""
    result = subprocess.run(
        list(argv),
        input=stdin,
        capture_output=True,
        env=untraced_env(),
        timeout=_COMMAND_TIMEOUT,
        check=False,
    )
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
    """How many history lines above the live screen the frame starts at (0 = live)."""


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


def _int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except ValueError:
        return default


def _optional_int(value: str) -> int | None:
    return int(value) if value.strip().lstrip("-").isdigit() else None


def _facts(line: str) -> PaneFacts:
    fields = line.split(_SEP)
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
        """Raise :class:`TmuxUnavailable` unless tmux is present and new enough."""
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
        path = paths.ensure_home() / CONF_NAME
        try:
            current = path.read_text(encoding="utf-8")
        except OSError:
            current = ""
        if current != BUNDLED_CONF:
            path.write_text(BUNDLED_CONF, encoding="utf-8")
        return path

    def argv(self, *args: str) -> list[str]:
        """The full command line for one tmux invocation on this server."""
        return [self.binary(), "-L", self.socket, "-f", str(self.conf_path()), *args]

    def run(self, *args: str, stdin: bytes | None = None) -> str:
        """Run one tmux command (``;`` arguments chain several) and return stdout."""
        completed = self._runner(self.argv(*args), stdin)
        if completed.returncode != 0:
            raise TmuxError(completed.stderr.strip() or f"tmux {' '.join(args)} failed")
        return completed.stdout

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
        shell, so nothing in it is re-interpreted.
        """
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
        window_id, _, pane_id = out.strip().partition(_SEP)
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
        """The pane's facts, or ``None`` when the pane is gone."""
        completed = self._runner(
            self.argv("display-message", "-p", "-t", pane_id, _FACTS_FORMAT), None
        )
        if completed.returncode != 0:
            return None
        return _facts(completed.stdout.rstrip("\n"))

    def kill_window(self, pane_id: str) -> None:
        """Kill the window holding ``pane_id`` (a dead pane included)."""
        self.run("kill-window", "-t", pane_id)

    def kill_session(self, session: str) -> None:
        self.run("kill-session", "-t", f"={session}")

    def rename_session(self, old: str, new: str) -> None:
        self.run("rename-session", "-t", f"={old}", new)

    def attach_argv(self, session: str) -> list[str]:
        """The command a terminal runs to attach to ``session`` (the escape hatch)."""
        return self.argv("attach-session", "-t", f"={session}")

    # --- the screen -------------------------------------------------------------------

    def capture(self, pane_id: str, *, scrollback: int = 0) -> Capture:
        """One frame of ``pane_id`` — the rows with SGR escapes, plus the pane's facts.

        ``scrollback`` is how many history lines above the live screen the
        frame should start at; the caller clamps it to ``facts.history_size``.
        One process: ``capture-pane`` then ``display-message`` via a ``;``.
        """
        scrollback = max(0, scrollback)
        out = self.run(
            "capture-pane", "-p", "-e", "-N", "-S", str(-scrollback), "-t", pane_id,
            ";", "display-message", "-p", "-t", pane_id, _FACTS_FORMAT,
        )  # fmt: skip
        body = out.rstrip("\n").split("\n")
        facts = _facts(body[-1])
        lines = body[:-1]
        if scrollback:
            lines = lines[: facts.height]
        return Capture(lines=lines, facts=facts, scrollback=scrollback)

    # --- input --------------------------------------------------------------------------

    def send_keys(self, pane_id: str, *keys: str) -> None:
        """Named keys (``Enter``, ``C-c``, ``BTab``…) — tmux's own vocabulary."""
        if keys:
            self.run("send-keys", "-t", pane_id, *keys)

    def send_literal(self, pane_id: str, text: str) -> None:
        """Literal text, exactly as typed (``-l``), even when it starts with ``-``."""
        if text:
            self.run("send-keys", "-t", pane_id, "-l", "--", text)

    def paste(self, pane_id: str, text: str) -> None:
        """Bracketed paste: the agent sees one paste, not one Enter per line."""
        if not text:
            return
        self.run("load-buffer", "-b", PASTE_BUFFER, "-", stdin=text.encode("utf-8"))
        self.run("paste-buffer", "-p", "-d", "-b", PASTE_BUFFER, "-t", pane_id)

    def resize(self, pane_id: str, width: int, height: int) -> None:
        """Size the pane's window to the pane the UI is showing it in."""
        self.run(
            "resize-window", "-t", pane_id, "-x", str(max(1, width)), "-y", str(max(1, height))
        )
