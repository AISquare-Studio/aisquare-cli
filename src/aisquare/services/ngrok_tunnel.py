"""The ngrok agent as a subprocess: spawn, read its JSON log, learn the public URL, stop.

``ngrok http <port> --log=stdout --log-format=json`` prints one JSON object per
line; the one that matters is ``{"msg": "started tunnel", "name": "command_line",
"addr": "http://localhost:<port>", "url": "https://…"}``. Everything else is noise or
an error (``{"lvl": "eror", "err": "…"}``), and an error about the authtoken is the
one a first-time user hits, so it gets its own hint. The binary is the human's job
(PLAN §7); when it is absent this module returns a sentence that says how to get it
— it never raises into the TUI.

``--inspect=false`` turns off ngrok's traffic inspector. Left on, the agent keeps
every request and answer on its local web interface (``127.0.0.1:4040``), which
asks no one for a password and can replay a request: the unlock's passphrase,
every device's cookie, the token in every path and every transcript, for any
user or process on the machine to read.

That web interface is also ngrok's agent API, which asks no one either: any user of
the machine can start another tunnel in this ngrok, or stop Remote's and start it
again with the inspector on. So the panel's ngrok runs with it off: ``web_addr:
false``, in a config of ours that ngrok merges over the human's own
(:func:`api_off_configs`). Where that cannot be done (no config of the human's where
ngrok keeps it, one of a version not known here) ngrok starts as before, and so does
one that says it cannot take ours (a snap that may not read it), started again; its
log then says the API is on, which the panel says (:attr:`NgrokTunnel.api_warning`).
One that ends for a reason of its own is not: started again, it would serve with its
API on for no fault of our config.

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
passphrase. And from the log only for the tunnel ``ngrok http <port>`` asked for,
to this Remote's port: one the API started says so in the same log, and is not
Remote's. A hand-started ngrok is told about with ``serve --public-url``.
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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aisquare.core import paths
from aisquare.core.atomic import write_replacing
from aisquare.core.injection import sanitise_text

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

COMMAND_LINE_TUNNEL = "command_line"
"""ngrok's name, in its log and its API, for the tunnel ``ngrok http <port>`` asks for."""
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
NGROK_CONFIG_NAME = "ngrok.yml"
API_OFF_CONFIGS = {
    "2": 'version: "2"\nweb_addr: false\n',
    "3": 'version: "3"\nagent:\n  web_addr: false\n',
}
"""Our config for each version of ngrok's own, merged over it (``--config``): the local web
interface and agent API off. A version-3 file keeps the agent's settings under ``agent:``."""
_API_OFF_HEADER = (
    "# aisquare's R panel starts ngrok with this merged over your own ngrok.yml, so that\n"
    "# ngrok's local web interface and agent API, which ask no one for a password, are off.\n"
)
_ERROR_LEVELS = frozenset({"eror", "error", "crit"})
"""The levels of ngrok's JSON log that report an error."""
_NO_ERROR = (None, "", "nil", "<nil>")
"""What an ``err`` of ngrok's JSON log holds when there was none."""
_CONFIG_VERSION = re.compile(r"""^version:\s*["']?(\d+)["']?\s*(?:#.*)?$""", re.MULTILINE)
API_ON = (
    "ngrok's local API is on ({addr}): anyone on this machine can use it to change "
    "Remote's tunnel — set web_addr: false in ngrok.yml (ngrok config edit)"
)
"""What the status line says while the panel's ngrok serves its agent API."""
FOREIGN_TUNNEL = (
    "ngrok's local API started a tunnel that is not Remote's ({name} → {addr}), which "
    "Remote does not use: someone on this machine is using that API — set web_addr: false "
    "in ngrok.yml (ngrok config edit)"
)
"""What the status line says once the panel's ngrok announced a tunnel it did not ask for."""


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
    name: str | None = None
    """That tunnel's name: :data:`COMMAND_LINE_TUNNEL` for the one ``ngrok http`` asked for,
    whatever ngrok's agent API named one it started."""
    addr: str | None = None
    """Where that tunnel forwards to: ``http://localhost:<port>``."""
    web: str | None = None
    """Where ngrok's local web interface and agent API listen, when the line says it started
    them (``starting web service``)."""
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
        plain = _one_line(_PLAIN_ERROR.sub("", line.strip(), count=1))
        return LogEvent(plain=plain or None)
    if not isinstance(record, dict):
        return LogEvent()
    message = record.get("msg")
    if message == "started tunnel" and isinstance(record.get("url"), str):
        return LogEvent(url=record["url"], name=_text(record, "name"), addr=_text(record, "addr"))
    if isinstance(message, str) and "starting web service" in message:
        return LogEvent(web=_text(record, "addr") or "its default address")
    if record.get("lvl") in _ERROR_LEVELS:
        err = record.get("err") or message or "ngrok reported an error"
        return LogEvent(error=_error_sentence(_one_line(str(err))))
    return LogEvent()


def says_trouble_with(line: str, name: str) -> bool:
    """Whether ``line`` of ngrok's output says the file ``name`` is why ngrok could not go on: a
    line of plain text naming it (what ngrok prints of a config it cannot read, as it ends),
    or one of its JSON log naming it with an error; never its note that it opened the file."""
    if name not in line:
        return False
    try:
        record: Any = json.loads(line)
    except ValueError:
        return True
    if not isinstance(record, dict):
        return False
    return record.get("lvl") in _ERROR_LEVELS or record.get("err") not in _NO_ERROR


def _text(record: dict[str, Any], key: str) -> str | None:
    value = record.get(key)
    return (_one_line(value) or None) if isinstance(value, str) else None


def _one_line(text: str) -> str:
    """``text`` as one line the status line may paint, by the house's one policy for outside
    bytes on a terminal (:func:`~aisquare.core.injection.sanitise_text`). ngrok's log is in
    part the agent API's caller's to fill (a tunnel's name, an error about it), and Textual
    paints an ESC in a sentence as it comes, to the terminal the fleet UI runs in."""
    return " ".join(sanitise_text(text).split())


def forwards_to(addr: str, port: int) -> bool:
    """Whether a tunnel's ``addr`` (``http://localhost:8750``, ``localhost:8750``, ``8750``)
    is ``port`` on this machine's loopback."""
    text = addr.strip()
    if text.isdigit():
        return int(text) == port
    try:
        split = urlsplit(text if "://" in text else f"//{text}")
        return split.port == port and (split.hostname or "localhost") in _LOOPBACK
    except ValueError:  # no address at all: not this port
        return False


def ngrok_default_config(
    *,
    platform: str = sys.platform,
    environ: Mapping[str, str] = os.environ,
    home: Path | None = None,
) -> Path:
    """Where ngrok reads its own config when it is given none, as ngrok's docs place it:
    ``$XDG_CONFIG_HOME`` (else ``~/.config``) on Linux, ``~/Library/Application Support`` on
    macOS, ``%LocalAppData%`` on Windows."""
    home = Path.home() if home is None else home
    if platform == "darwin":
        return home / "Library" / "Application Support" / "ngrok" / NGROK_CONFIG_NAME
    if platform == "win32":
        local = environ.get("LOCALAPPDATA")
        return (Path(local) if local else home / "AppData" / "Local") / "ngrok" / NGROK_CONFIG_NAME
    xdg = environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else home / ".config"
    return base / "ngrok" / NGROK_CONFIG_NAME


def api_off_configs(
    *, own: Path | None = None, environ: Mapping[str, str] = os.environ
) -> list[Path] | None:
    """The configs to start ngrok with so that its agent API is off: the human's own
    (``own``, ngrok's default when not given), then ours over it, of the same version.

    ``--config`` names every config ngrok reads, merged in order, so the human's own is
    named first: their authtoken, a reserved domain, a proxy. Without one, ours alone,
    when ``NGROK_AUTHTOKEN`` signs ngrok in. ``None`` when ngrok is best started as it always
    was, its API on: no config of the human's and nothing to sign in with (ngrok will say
    so), one whose version is not known here, or a path with a comma (``--config``
    splits on them), or ours could not be written.
    """
    own = ngrok_default_config(environ=environ) if own is None else own
    try:
        text = own.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        if not environ.get("NGROK_AUTHTOKEN"):
            return None
        version, first = "2", []
    except OSError:
        return None
    else:
        found = _CONFIG_VERSION.search(text)
        version, first = (found.group(1) if found else ""), [own]
    if version not in API_OFF_CONFIGS:
        return None
    ours = _api_off_config(version)
    if ours is None:
        return None
    configs = [*first, ours]
    return None if any("," in str(path) for path in configs) else configs


def _api_off_config(version: str) -> Path | None:
    """Our config for ngrok configs of ``version``, written into the aisquare home if it is not
    there as it should be; ``None`` when it cannot be."""
    path = paths.aisquare_home() / f"remote-ngrok-v{version}.yml"
    body = _API_OFF_HEADER + API_OFF_CONFIGS[version]
    try:
        # With replacement: a byte that is no UTF-8 in it raised a UnicodeDecodeError, no
        # OSError, out of every start, the watchdog's on Textual's thread among them.
        if path.is_file() and path.read_text(encoding="utf-8", errors="replace") == body:
            return path
        paths.ensure_home()
        write_replacing(path, body, keep_mode=False)
    except OSError as exc:
        log.warning("ngrok: %s could not be written (%s); its API stays on", path, exc)
        return None
    return path


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


def _signal_group(group: int, *, kill: bool = False) -> None:
    """SIGTERM every process of ``group``, or SIGKILL it with ``kill``.

    Process groups are POSIX: on Windows :func:`_own_group` gives none, so nothing calls
    this there, and the ``sys.platform`` check is what lets mypy's run on the Windows leg
    read past ``os.killpg`` and ``SIGKILL``, which that platform has not."""
    if sys.platform == "win32":
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(group, signal.SIGKILL if kill else signal.SIGTERM)


def _group_gone(group: int, deadline: float) -> bool:
    """Whether every process of ``group`` has ended, by ``deadline`` (``time.monotonic``).

    A member that may not be signalled (another user's) counts as gone: nothing here
    could end it anyway. Windows has no groups (:func:`_signal_group`)."""
    if sys.platform == "win32":
        return True
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
    _signal_group(group)
    if not _ended(process, group):
        _signal_group(group, kill=True)
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


def ngrok_command(
    port: int,
    binary: str = "ngrok",
    *,
    url: str | None = None,
    configs: Sequence[Path] | None = None,
) -> list[str]:
    """``ngrok http <port>`` with its log as JSON lines and its traffic inspector off, on the
    static domain ``url`` if one, with ``configs`` in place of ngrok's own config if given
    (:func:`api_off_configs`)."""
    command = [binary, "http", str(port), "--log=stdout", "--log-format=json", "--inspect=false"]
    if url:
        command.append(f"--url={url}")
    if configs:
        command.append(f"--config={','.join(str(path) for path in configs)}")
    return command


class NgrokTunnel:
    """One ngrok subprocess: ``start_tunnel`` spawns it, ``stop_tunnel`` ends it.

    The public URL arrives from its log.

    ``which`` and ``popen`` are seams: tests hand a fake binary (a Python script
    that prints the JSON lines) and the missing-binary path needs no ngrok at all.
    ``api_off`` starts ngrok with its agent API off (:func:`api_off_configs`), as the
    panel's ngrok always is; ``configs`` is that function, a seam too.
    """

    def __init__(
        self,
        port: int,
        *,
        binary: str = "ngrok",
        which: Callable[[str], str | None] = shutil.which,
        popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        command: Sequence[str] | None = None,
        api_off: bool = False,
        configs: Callable[[], list[Path] | None] = api_off_configs,
    ) -> None:
        self.port = port
        self.binary = binary
        self._which = which
        self._popen = popen
        self._command = list(command) if command is not None else None
        self.api_off = api_off
        self._configs = configs
        self._as_before: list[str] | None = None
        """ngrok's command without our config, for an ngrok that cannot take it."""
        self._ours: str | None = None
        """The file name of our config, while ngrok runs with it."""
        self._ours_refused = False
        """Whether ngrok's output said our config is why it could not go on
        (:func:`says_trouble_with`)."""
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
        self.api_addr: str | None = None
        """Where ngrok's local web interface and agent API listen, once its log said it started
        them; ``None`` while they are off, as the panel starts ngrok."""
        self.api_refused: str | None = None
        """Why ngrok would not start with its API off, once it was started as before."""
        self.foreign: str | None = None
        """A tunnel ngrok announced that is not this one, as the status line says it: started
        through ngrok's agent API, by whoever is using it."""
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
            configs = self._our_configs()
            if configs is not None:
                self._as_before, self._ours = command, configs[-1].name
                command = ngrok_command(
                    self.port, self.binary, url=self.static_host, configs=configs
                )
        try:
            process = self._spawn(command)
        except OSError as exc:
            self.error = f"could not start {command[0]}: {exc}"
            return self.error
        with self._lock:
            self._process, self._group = process, _own_group(process)
        _track(self)
        self._reader = threading.Thread(target=self._read_log, name="ngrok-log", daemon=True)
        self._reader.start()
        return None

    def _our_configs(self) -> list[Path] | None:
        """The configs that turn ngrok's API off (:attr:`api_off`); ``None`` to start ngrok as
        before, should they not be had: this runs on the watchdog's start, on Textual's thread,
        where anything raised ends the fleet UI."""
        if not self.api_off:
            return None
        try:
            return self._configs()
        except Exception:
            log.warning("ngrok: the config that turns its API off could not be had", exc_info=True)
            return None

    def _spawn(self, command: list[str]) -> subprocess.Popen[str]:
        """Start ``command``: ngrok, its log on a pipe this reads.

        UTF-8, as ngrok writes it, whatever the locale: decoded with the locale's codec, a
        home like C:\\Users\\Иван in ngrok's first lines (cp1251 has no 0x98) raised out
        of the log reader, which ended without a word, and the panel waited out its 15 s
        while ngrok ran on with nobody draining its log. In a process group of its own,
        for stopping (:meth:`stop_tunnel`), with nothing to read from the terminal the
        fleet UI holds.
        """
        return self._popen(
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
            _signal_group(group)
        elif process is not None:
            with contextlib.suppress(OSError):
                process.terminate()

    # --- the log reader ---------------------------------------------------------------

    def _read_log(self) -> None:
        """Hand each line of ngrok's log to :meth:`handle_line` until it ends, then say why
        no tunnel came, if none did, and wake whoever waits for one.

        An ngrok started with its API off that ends before it announced a tunnel, saying our
        config is why, is started again as before, once (:meth:`_start_as_before`), and its
        log read on: ngrok's merging of our config into the human's own is ngrok's to judge,
        and a Remote with its API on is better than none.
        """
        process = self._process
        while process is not None and process.stdout is not None:
            self._read_to_the_end(process)
            code = _exit_code(process)
            self._forget_if_gone(process, code)
            if self.public_url is not None:
                break
            process = self._start_as_before(process, code)
            if process is None and self.error is None:
                self.error = self._exit_sentence(code)
        self._url_ready.set()  # a URL that never comes must not block a waiter forever

    def _read_to_the_end(self, process: subprocess.Popen[str]) -> None:
        """Every line of ``process``'s log, until it ends; then its pipe closed.

        Nothing closes the pipe under the reader (:meth:`stop_tunnel`), so a ``ValueError``
        is said: read as a closed pipe, a line the reader could not take ended it without
        a word, the panel waiting out its 15 s for a URL that ngrok, still up, had no one
        left to tell.
        """
        stdout = process.stdout
        assert stdout is not None
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

    def _start_as_before(
        self, ended: subprocess.Popen[str], code: int | None
    ) -> subprocess.Popen[str] | None:
        """ngrok started again without our config, when ``ended`` was started with it, said
        that config is why it could not go on, and this tunnel was not stopped meanwhile;
        ``None`` otherwise, or when it cannot start.

        Only for our config: an ngrok that ended for a reason of its own (no authtoken, the
        static domain still held by the session a watchdog's restart replaces) was started
        again all the same, and one that came up then served its API, which our config is
        there to keep off, for the rest of its run.
        """
        with self._lock:
            command = self._as_before
            if command is None or self._process is not ended or not self._ours_refused:
                return None
            self._as_before, self._ours, self._ours_refused = None, None, False
            refused = self.error or self._exit_sentence(code)
            self.error, self._plain, self._plain_is_error = None, None, False
            self.api_addr = None
            try:
                process = self._spawn(command)
            except OSError as exc:
                self.error = f"could not start {command[0]}: {exc}"
                return None
            self._process, self._group = process, _own_group(process)
            self.api_refused = refused
        _track(self)
        log.warning("ngrok would not start with its local API off (%s); started as before", refused)
        return process

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
        ours = self._ours
        if ours is not None and says_trouble_with(line, ours):
            with self._lock:
                self._ours_refused = True
        if event.url is not None and not self._announces_this_tunnel(event):
            # Started through ngrok's agent API, which any user of the machine can use: its
            # URL is never the link, nor where a notification leads.
            log.warning(
                "ngrok: a tunnel Remote did not start was announced (%s → %s); not used",
                event.name,
                event.addr,
            )
            with self._lock:
                self.foreign = FOREIGN_TUNNEL.format(
                    name=event.name or "a tunnel", addr=event.addr or "somewhere"
                )
            return
        with self._lock:
            if event.web is not None:
                self.api_addr = event.web
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

    def _announces_this_tunnel(self, event: LogEvent) -> bool:
        """Whether a started tunnel is the one ``ngrok http <port>`` asked for: named
        :data:`COMMAND_LINE_TUNNEL`, forwarding to this port. Its log is read whatever
        ngrok's agent API did, and a tunnel started there, under any other name or to any
        other address, became the link, the QR and every notification's origin: a tap
        carried the token to a host of the API's caller, and the passphrase after it. A
        line naming neither is taken as ever; every ngrok names both."""
        if event.name is not None and event.name != COMMAND_LINE_TUNNEL:
            return False
        return event.addr is None or forwards_to(event.addr, self.port)

    @property
    def api_warning(self) -> str | None:
        """What the status line says of ngrok's agent API while ngrok is up: a tunnel it
        started that is not this one, or that it is on at all; ``None`` while it is off, and
        once ngrok has ended, the API with it."""
        if not self.running:
            return None
        if self.foreign is not None:
            return self.foreign
        if self.api_addr is not None:
            return API_ON.format(addr=self.api_addr)
        return None
