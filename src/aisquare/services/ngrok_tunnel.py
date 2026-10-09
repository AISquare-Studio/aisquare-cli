"""The ngrok agent as a subprocess: spawn, read its JSON log, learn the public URL, stop.

``ngrok http <port> --log=stdout --log-format=json`` prints one JSON object per
line; the one that matters is ``{"msg": "started tunnel", "url": "https://…"}``.
Everything else is noise or an error (``{"lvl": "eror", "err": "…"}``), and an
error about the authtoken is the one a first-time user hits, so it gets its own
hint. The binary is the human's job (PLAN §7); when it is absent this module
returns a sentence that says how to get it — it never raises into the TUI.

``--inspect=false`` turns off ngrok's traffic inspector. Left on, the agent keeps
every request and answer on its local web interface (``127.0.0.1:4040``), which
asks no one for a password and can replay a request: the unlock's passphrase,
every device's cookie, the token in every path and every transcript, for any
user or process on the machine to read.

ngrok runs in a process group of its own, and stopping it stops the group: the
``ngrok`` on a PATH may be a launcher that runs the real binary as its child (pyngrok's
console script, the npm package's, a wrapper without ``exec``), and a signal to the
launcher alone left the real ngrok up, its tunnel and its static domain held, and
the log's pipe open, so stopping waited on it for good (sweep of #243).

Pure functions (:func:`build_public_url`, :func:`parse_log_line`,
:func:`missing_binary_message`) carry the logic, so PLAN §4-H's proxies for the
"ngrok present" check — the URL builder and the missing-binary message — are unit
tests; the real tunnel is Rabia's QA.

A free ngrok URL changes every time ngrok starts, and with it the link, the
cookie's origin and a home-screen app. ``AISQUARE_REMOTE_NGROK_URL`` names a
static domain instead (``--url``), which survives restarts (SPEC §5.8).

Where phones reach the server is learned from this process's own ngrok, by its
log, never from ngrok's local agent API on ``127.0.0.1:4040``: any user of the
machine can listen there before the human's ngrok does (which then moves to
4041) and name any https host, and a push link is where the human types the
passphrase. A hand-started ngrok is told about with ``serve --public-url``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

INSTALL_HINT = (
    "ngrok is not installed. Install it: https://ngrok.com/download "
    "(macOS: brew install ngrok · Linux: snap install ngrok, or unzip the binary "
    "onto your PATH), then add your authtoken: ngrok config add-authtoken <token> "
    "(from https://dashboard.ngrok.com/get-started/your-authtoken)."
)
AUTHTOKEN_HINT = (
    "ngrok needs an authtoken: run  ngrok config add-authtoken <token>  "
    "(from https://dashboard.ngrok.com/get-started/your-authtoken) and turn Remote on again."
)
TOO_OLD_HINT = (
    "this ngrok is too old for --url (the static domain in AISQUARE_REMOTE_NGROK_URL) — "
    "run  ngrok update  and turn Remote on again."
)
NGROK_URL_ENV = "AISQUARE_REMOTE_NGROK_URL"
"""A static ngrok domain (``name.ngrok-free.app``, with or without ``https://``) to serve on."""

log = logging.getLogger(__name__)

_TOO_OLD_FOR_URL = re.compile(r"(?:unknown flag|flag provided but not defined):\s*-{1,2}url\b")
"""What ngrok v3 (``unknown flag: --url``) and v2 (``flag provided but not defined: -url``)
print as plain text on stderr, before any JSON log, when they do not know ``--url``."""
_PLAIN_ERROR = re.compile(r"\AERROR:\s*")
"""How ngrok begins each line of an error it prints as plain text: ``ERROR:  <the cause>``."""
PLAIN_CAUSE_MAX = 300
"""The most of a plain-text line the status line says: a cause, not a page of output."""
EXIT_CODE_WAIT_SECONDS = 1.0
"""How long the log reader waits, once ngrok's log ended, for the exit code it says: the log
ends a moment before the process does, and ``poll()`` then read ``None`` (6 runs in 40)."""
STOP_SECONDS = 5.0
"""How long stopping gives ngrok, and all its process group, to end after SIGTERM, and then
again after SIGKILL."""
READER_JOIN_SECONDS = 1.0
"""How long stopping waits, once ngrok's group is gone, for the log reader to see its end."""
_OWN_GROUP: dict[str, Any] = {} if sys.platform == "win32" else {"process_group": 0}
"""Popen's word for a process group of ngrok's own: POSIX only, and the fleet UI runs there."""


def build_public_url(host: str, token: str) -> str:
    """``https://<host>/r/<token>/`` — the text the modal shows and the QR encodes.

    ``host`` may be a bare host (``abc.ngrok-free.app``) or the URL ngrok logged
    (``https://abc.ngrok-free.app``, with or without a trailing slash); the
    output is one canonical form either way, so the link row and the QR agree.
    The trailing slash matches the server's ``Mount("/r/{token}")`` and its own
    ``url_local``, so the phone lands on the SPA without a redirect hop.
    """
    bare = host.strip()
    for scheme in ("https://", "http://"):
        bare = bare.removeprefix(scheme)
    bare = bare.rstrip("/")
    return f"https://{bare}/r/{token}/"


def missing_binary_message(binary: str = "ngrok") -> str:
    """The actionable sentence shown when the binary cannot be found on PATH."""
    return (
        INSTALL_HINT if binary == "ngrok" else INSTALL_HINT.replace("ngrok is", f"{binary} is", 1)
    )


@dataclass(frozen=True)
class LogEvent:
    """One parsed ngrok log line — only the fields the lifecycle acts on."""

    url: str | None = None
    """The public URL, when the line announced a started tunnel."""
    error: str | None = None
    """An error message, when the line reported one."""
    plain: str | None = None
    """A line that is no JSON, as it reads without ngrok's ``ERROR:`` in front: what ngrok,
    or whatever runs in its place (a launcher, a version manager's shim), prints before
    its JSON log starts, a failure to start among it."""


def _error_sentence(text: str) -> str:
    """What the status line says for an error ngrok reported: its own words, but for the
    two a hint says better (no authtoken, an ngrok too old for ``--url``)."""
    if "authtoken" in text.lower() or "ERR_NGROK_4018" in text:
        return AUTHTOKEN_HINT
    if _TOO_OLD_FOR_URL.search(text):
        return TOO_OLD_HINT
    return text


def parse_log_line(line: str) -> LogEvent:
    """What one log line means for us; an irrelevant line means nothing.

    The lines are JSON, but for what is printed before the JSON log starts: an ngrok
    too old for ``--url`` says so in plain text, and that becomes :data:`TOO_OLD_HINT`;
    any other such line is kept as it reads (:attr:`LogEvent.plain`), for the reader to
    say should ngrok end without a tunnel.
    """
    try:
        record: Any = json.loads(line)
    except ValueError:
        if _TOO_OLD_FOR_URL.search(line):
            return LogEvent(error=TOO_OLD_HINT)
        plain = _PLAIN_ERROR.sub("", line.strip(), count=1).strip()
        return LogEvent(plain=plain or None)
    if not isinstance(record, dict):
        return LogEvent()
    if record.get("msg") == "started tunnel" and isinstance(record.get("url"), str):
        return LogEvent(url=record["url"])
    if record.get("lvl") in ("eror", "error", "crit"):
        err = record.get("err") or record.get("msg") or "ngrok reported an error"
        return LogEvent(error=_error_sentence(str(err)))
    return LogEvent()


def _exit_code(process: subprocess.Popen[str]) -> int | None:
    """ngrok's exit code once its log has ended, waited for a moment; ``None`` while it runs on."""
    try:
        return process.wait(timeout=EXIT_CODE_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        return None


def _own_group(process: subprocess.Popen[str]) -> int | None:
    """The process group ``process`` leads, as it was started in one of its own; ``None`` where
    there are none (Windows), or for a stand-in with no pid, which stopping signals alone."""
    pid = getattr(process, "pid", None)
    if sys.platform == "win32" or not isinstance(pid, int):
        return None
    try:
        return pid if os.getpgid(pid) == pid else None
    except OSError:  # gone already, and reaped: there is nothing of it to stop
        return None


_LIVE: set[NgrokTunnel] = set()
"""Every tunnel whose ngrok is up, or not yet seen to end with all of its group: what
:func:`end_every_tunnel_now` signals. Changed under :data:`_LIVE_LOCK`; read without it."""
_LIVE_LOCK = threading.Lock()


def end_every_tunnel_now() -> None:
    """SIGTERM every ngrok this process started and has not seen end, and their groups, at once,
    taking no lock and waiting for nothing: for a signal handler, as the process ends
    (``aisquare.cli.ui.remote_control.ngrok_ends_with``). Every one, not only the tunnel a
    Remote holds: a dead one the watchdog is still stopping, one a start has just spawned."""
    for tunnel in tuple(_LIVE):  # one step under the GIL: no lock a handler could wait on
        tunnel.signal_now()


def _track(tunnel: NgrokTunnel) -> None:
    with _LIVE_LOCK:
        _LIVE.add(tunnel)


def _untrack(tunnel: NgrokTunnel) -> None:
    with _LIVE_LOCK:
        _LIVE.discard(tunnel)


def _signal_group(group: int, signum: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(group, signum)


def _group_gone(group: int, deadline: float) -> bool:
    """Whether every process of ``group`` has ended, by ``deadline`` (``time.monotonic``).

    A member that may not be signalled (another user's) counts as gone: nothing here
    could end it anyway."""
    while True:
        try:
            os.killpg(group, 0)
        except (ProcessLookupError, PermissionError):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _ended(process: subprocess.Popen[str], group: int | None) -> bool:
    """Wait :data:`STOP_SECONDS` for ngrok, and its group, to end; whether they did. ngrok is
    reaped first: a child not yet waited for still counts as a member of its group."""
    deadline = time.monotonic() + STOP_SECONDS
    try:
        process.wait(timeout=STOP_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    return group is None or _group_gone(group, deadline)


def _end_process(process: subprocess.Popen[str], group: int | None) -> None:
    """SIGTERM ngrok and its whole group, and SIGKILL what is left of them after
    :data:`STOP_SECONDS`; ngrok alone where it has no group of its own.

    The group, and not ngrok's process: a launcher that runs the real ngrok as its child
    and is signalled alone ends, and leaves the real one up, holding its tunnel, its
    static domain and the log's pipe. A group whose leader already ended (a launcher
    killed from outside) is signalled all the same, for what is left in it.
    """
    if group is None:
        if process.poll() is not None:
            return
        process.terminate()
        if not _ended(process, None):
            process.kill()
            _ended(process, None)
        return
    _signal_group(group, signal.SIGTERM)
    if not _ended(process, group):
        _signal_group(group, signal.SIGKILL)
        _ended(process, group)


def ngrok_static_host(raw: str | None) -> str | None:
    """The host ``--url`` takes, from a static domain however it was written; ``None`` if blank.

    ``name.ngrok-free.app``, ``https://name.ngrok-free.app`` and either with a
    trailing slash or a path all give ``name.ngrok-free.app``.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return urlsplit(text if "://" in text else f"https://{text}").hostname or None
    except ValueError:  # an unparseable value is no domain; ngrok picks a random one
        return None


def ngrok_command(port: int, binary: str = "ngrok", *, url: str | None = None) -> list[str]:
    """``ngrok http <port>`` with its log as JSON lines and its traffic inspector off, on the
    static domain ``url`` if one."""
    command = [binary, "http", str(port), "--log=stdout", "--log-format=json", "--inspect=false"]
    return [*command, f"--url={url}"] if url else command


class NgrokTunnel:
    """One ngrok subprocess: ``start_tunnel`` spawns it, ``stop_tunnel`` ends it.

    The public URL arrives from its log.

    ``which`` and ``popen`` are seams: tests hand a fake binary (a Python script
    that prints the JSON lines) and the missing-binary path needs no ngrok at all.
    """

    def __init__(
        self,
        port: int,
        *,
        binary: str = "ngrok",
        which: Callable[[str], str | None] = shutil.which,
        popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        command: Sequence[str] | None = None,
    ) -> None:
        self.port = port
        self.binary = binary
        self._which = which
        self._popen = popen
        self._command = list(command) if command is not None else None
        self._process: subprocess.Popen[str] | None = None
        self._group: int | None = None
        """The process group ngrok leads, all that it started in it, until stopping has seen
        it gone; ``None`` without one (Windows, a stand-in)."""
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()
        self._url_ready = threading.Event()
        self.public_url: str | None = None
        self.error: str | None = None
        self._plain: str | None = None
        """The first line ngrok's output held that was no JSON: why it ended, when it ends
        before its JSON log says anything (:meth:`_read_log`). An ``ERROR:`` line takes the
        place of any other that came before it."""
        self._plain_is_error = False
        self.on_announce: Callable[[str], None] | None = None
        """Told each URL ngrok announces, on the log reader's thread, however late it comes:
        whoever called :meth:`wait_for_url` may have stopped waiting long before (ngrok
        retrying its session on a network that is not up yet). Set it before
        :meth:`start_tunnel`."""
        self.static_host = ngrok_static_host(os.environ.get(NGROK_URL_ENV))
        """The static domain ngrok serves on (``--url``), from ``AISQUARE_REMOTE_NGROK_URL``;
        ``None``: ngrok picks a new URL every time it starts."""

    # --- lifecycle ------------------------------------------------------------------

    def start_tunnel(self) -> str | None:
        """Spawn ngrok; ``None`` on success, else the sentence to show (no binary, spawn failed)."""
        command = self._command
        if command is None:
            if self._which(self.binary) is None:
                self.error = missing_binary_message(self.binary)
                return self.error
            command = ngrok_command(self.port, self.binary, url=self.static_host)
        try:
            # UTF-8, as ngrok writes it, whatever the locale: decoded with the locale's
            # codec, a home like C:\Users\Иван in ngrok's first lines (cp1251 has no 0x98)
            # raised out of the log reader, which ended without a word, and the panel
            # waited out its 15 s while ngrok ran on with nobody draining its log.
            # Its own process group, for stopping (stop_tunnel), and nothing to read from
            # the terminal the fleet UI holds.
            process = self._popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **_OWN_GROUP,
            )
        except OSError as exc:
            self.error = f"could not start {command[0]}: {exc}"
            return self.error
        with self._lock:
            self._process, self._group = process, _own_group(process)
        _track(self)
        self._reader = threading.Thread(target=self._read_log, name="ngrok-log", daemon=True)
        self._reader.start()
        return None

    def wait_for_url(self, timeout: float = 15.0) -> str | None:
        """Block up to ``timeout`` seconds for the public URL; ``None`` when it did not arrive."""
        self._url_ready.wait(timeout)
        return self.public_url

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def stop_tunnel(self) -> None:
        """End ngrok, and all it started, in :data:`STOP_SECONDS` (twice that for one that
        ignores SIGTERM).

        The log's pipe is never closed here: the reader holds it while blocked in a read,
        so a close waits for that read, and a launcher's ngrok that outlived the launcher
        kept the read going for as long as it logged nothing, which an idle ngrok does
        not. With ngrok's group gone the log ends, and the reader closes it itself; one
        still held open by a process that left the group is left to the reader.
        """
        with self._lock:
            process, group, self._process = self._process, self._group, None
        if process is None:
            return
        _end_process(process, group)
        with self._lock:
            if self._group == group:
                self._group = None  # gone: its number may name another group from now on
        _untrack(self)
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(READER_JOIN_SECONDS)
            if reader.is_alive():
                log.warning("ngrok: its log is still held open by a process outside its group")

    def signal_now(self) -> None:
        """SIGTERM ngrok and its group at once, taking no lock and waiting for nothing: for a
        signal handler, as the process ends (:func:`end_every_tunnel_now`)."""
        group, process = self._group, self._process
        if group is not None:
            _signal_group(group, signal.SIGTERM)
        elif process is not None:
            with contextlib.suppress(OSError):
                process.terminate()

    # --- the log reader ---------------------------------------------------------------

    def _read_log(self) -> None:
        """Hand each line of ngrok's log to :meth:`handle_line` until it ends, then say why
        no tunnel came, if none did, and wake whoever waits for one.

        Nothing closes the pipe under the reader (:meth:`stop_tunnel`), so a ``ValueError``
        is said: read as a closed pipe, a line the reader could not take ended it without
        a word, the panel waiting out its 15 s for a URL that ngrok, still up, had no one
        left to tell. The reader closes the pipe itself once the log has ended.
        """
        process = self._process
        if process is None or process.stdout is None:
            return
        stdout = process.stdout
        try:
            for line in stdout:
                self.handle_line(line)
        except ValueError as exc:
            with self._lock:
                if self.error is None:
                    self.error = f"ngrok's log could not be read: {exc}"
        finally:
            with contextlib.suppress(OSError, ValueError):
                stdout.close()
        code = _exit_code(process)
        self._forget_if_gone(process, code)
        if self.public_url is None and self.error is None:
            self.error = self._exit_sentence(code)
        self._url_ready.set()  # a URL that never comes must not block a waiter forever

    def _forget_if_gone(self, process: subprocess.Popen[str], code: int | None) -> None:
        """Once ngrok has exited with its log ended, its group too if nothing is left in it.

        ngrok is reaped then, and with its group empty the group's number is free: a stop
        that came later (the watchdog's, or turning off a Remote whose first ngrok never came
        up, hours after) signalled whatever group took that number since, another program
        of the human's. A group with something left in it keeps its number, and is kept."""
        if code is None:
            return
        with self._lock:
            group = self._group if self._process is process else None
            if group is None or not _group_gone(group, deadline=0.0):
                return
            self._group = None
        _untrack(self)

    def _exit_sentence(self, code: int | None) -> str:
        """Why there is no tunnel, when ngrok ended without one and logged no error: what it
        printed before its JSON log, if anything (a config it could not read, a launcher
        or a shim that could not run it), which the status line said nothing of."""
        ended = (
            f"ngrok exited (code {code}) before it announced a tunnel"
            if code is not None
            else "ngrok's log ended before it announced a tunnel"
        )
        cause = self._plain
        if cause is None:
            return ended
        sentence = _error_sentence(cause)
        if sentence != cause:  # a hint says it better than ngrok's own words
            return sentence
        if len(cause) > PLAIN_CAUSE_MAX:
            cause = cause[: PLAIN_CAUSE_MAX - 1] + "…"
        return f"{ended}: {cause}"

    def handle_line(self, line: str) -> None:
        event = parse_log_line(line)
        with self._lock:
            if event.url is not None:
                self.public_url = event.url
                self._url_ready.set()
            elif event.error is not None and self.error is None:
                self.error = event.error
            elif event.plain is not None and not self._plain_is_error:
                if _PLAIN_ERROR.match(line.strip()):
                    self._plain, self._plain_is_error = event.plain, True
                elif self._plain is None:
                    self._plain = event.plain
        announced = self.on_announce
        if event.url is not None and announced is not None:
            try:
                announced(event.url)
            except Exception:  # the reader must go on: a log nobody reads stalls ngrok
                log.warning("ngrok: the announced URL could not be taken", exc_info=True)
